"""
Unified LangGraph incident workflow for the AI agent pipeline.

One Ops Agent handles the full pipeline sequentially inside the same process:
  Phase 1 (automated): investigate → propose_fix → validate → approval_gate → END

Each stage's result is stored as an event on the single rca task. The approval
gate changes the task status to 'awaiting_approval'; no separate task is created.

  Phase 2 (triggered by human approval via /workflow/resume/{task_id}):
    resume_execution() → execute fix with check_mode=False → verify_resolution
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import TypedDict

import httpx
from langchain_core.messages import HumanMessage, ToolMessage, AIMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from shared.config import settings
from shared.kb_store import kb_store as _kb_store
from shared.llm import get_llm
from shared.pipeline_models import RcaResult, FixProposalResult, ValidationResult, ExecutionResult, AutonomyDecision, autonomy_level_index
from shared.structured_output import parse_structured
from shared.task_store import TaskStore
from shared.rate_limiter import RateLimiter, BudgetExceededError
from shared.status_tracker import StatusCallbackHandler
from shared.tools import OPS_TOOLS
from shared.policy_registry import PolicyRegistry
from shared.policy_resolver import PolicyResolver
from shared.intent_registry import IntentRegistry
from shared.topology_correlator import TopologyCorrelator
from shared.learning_engine import LearningEngine

logger = logging.getLogger(__name__)

PROMETHEUS_URL  = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
VERIFY_DELAY    = settings.execution_verify_delay
AGENT           = "workflow"
_RATE_LIMIT_BACKOFF = 45   # seconds to wait after a 429 before retrying a stage


def _parse_retry_after(exc: Exception) -> int:
    """Extract retry-after seconds from an OpenAI rate-limit error message."""
    m = re.search(r"try again in (\d+(?:\.\d+)?)(ms|s)", str(exc))
    if not m:
        return _RATE_LIMIT_BACKOFF
    value, unit = float(m.group(1)), m.group(2)
    return max(5, int(value / 1000 if unit == "ms" else value) + 2)

_ALERT_FOCUS: dict[str, str] = {
    "InterfaceDown": (
        "Interface is operationally down. CRITICAL DIAGNOSTIC STEP: call get_device_metrics(device) "
        "and check interface_ifAdminStatus AND interface_ifOperStatus. "
        "If ifAdminStatus=2 (admin-down): the interface was deliberately shut down — propose 'no shutdown' "
        "as a config_change fix unless you have evidence the shutdown was planned maintenance. "
        "If ifAdminStatus=1 but ifOperStatus=2: physical or remote-side failure — check topology for "
        "connected peer, check peer interface state. Do NOT diagnose physical failure when ifAdminStatus=2."
    ),
    "InterfaceAdminDown": (
        "Interface was administratively shut down (ifAdminStatus=2). "
        "This is a deliberate config action. Your job is to determine if it was INTENTIONAL "
        "(planned maintenance, chaos experiment) or UNINTENTIONAL (misconfiguration, rogue change). "
        "Check recent task events and chaos scheduler history. "
        "If no maintenance window or planned event is found: fix_type=config_change, "
        "COMMANDS='interface {ifDescr}\\n no shutdown', RISK=low. "
        "Only use escalate_human if you have explicit evidence the shutdown was planned."
    ),
    "BGPPeerDown":              "BGP session is not Established — check for link flaps, config drift, or route policy issues. Also check if the peer's connected interface is down (InterfaceDown/InterfaceAdminDown on same device).",
    "DeviceDown":               "device is unreachable via ICMP — check reachability, upstream links, and power",
    "HighInterfaceUtilization": "interface utilization is high — identify the traffic source and affected flows",
    "InterfaceHighErrorRate":   "interface has elevated error rate — check for hardware or cabling issues",
    "BGPPrefixCountDecreased":  "BGP prefix count dropped significantly — possible route withdrawal or peering issue",
}


# ── State ──────────────────────────────────────────────────────────────────────

class IncidentState(TypedDict):
    """Full pipeline state threaded through every graph node."""

    # Alert input
    alertname:   str
    severity:    str
    device:      str
    instance:    str
    summary:     str
    description: str
    fingerprint: str
    event:       dict

    # Topology context (injected by alert_poller or check_intents)
    blast_radius:    list
    is_leaf_symptom: bool
    intent_match:    str | None   # intent ID if a standing intent fired

    # Single task ID for the entire pipeline
    rca_task_id: str | None

    # Stage outputs
    rca:          dict | None
    fix_proposal: dict | None
    validation:   dict | None

    # Routing signal
    pipeline_decision: str | None

    # Set to the matched policy ID when the programmatic fast path resolves the alert
    fast_path_policy_id: str | None

    # Flags
    in_maintenance:       bool
    do_not_auto_execute:  bool
    priority:             str
    session_id:           str
    tenant_id:            str

    incident_id: str | None
    error: str | None


# ── Workflow class ─────────────────────────────────────────────────────────────

class IncidentWorkflow:
    """
    LangGraph StateGraph that runs the full incident pipeline as a single task.
    All pipeline stages are recorded as events on the one rca task.
    """

    def __init__(
        self,
        task_store:      TaskStore,
        rate_limiter:    RateLimiter,
        status_handler:  StatusCallbackHandler,
        ai_enabled_fn=None,
    ) -> None:
        self._ts    = task_store
        self._rl    = rate_limiter
        self._sh    = status_handler
        self._stop  = threading.Event()
        self._ai_enabled_fn = ai_enabled_fn or (lambda: settings.ai_enabled)
        # Limit concurrent LLM-heavy pipeline runs to prevent TPM stampede.
        # Each incident workflow can use ~6-10K tokens; cap at 2 concurrent.
        self._pipeline_sem = threading.Semaphore(2)

        self._llm = get_llm(temperature=0.1)

        from ops_agent.chaos_tools import CHAOS_TOOLS as _CHAOS
        self._all_tools = OPS_TOOLS + _CHAOS

        # CLANO framework components
        self._policy_registry  = PolicyRegistry(task_store)
        self._policy_resolver  = PolicyResolver()
        self._intent_registry  = IntentRegistry(task_store)
        self._topology         = TopologyCorrelator(
            settings.nautobot_url,
            settings.nautobot_token,
            cache_ttl=settings.topology_cache_ttl,
            blast_radius_hops=settings.topology_blast_radius_hops,
        )
        self._learning_engine  = LearningEngine(self._policy_registry, task_store)

        self._graph = self._build_graph()

    # ── graph construction ────────────────────────────────────────────────────

    def _build_graph(self):
        builder = StateGraph(IncidentState)

        builder.add_node("check_intents",        self._node_check_intents)
        builder.add_node("policy_fast_path",     self._node_policy_fast_path)
        builder.add_node("no_ai_gate",           self._node_no_ai_gate)
        builder.add_node("investigate",          self._node_investigate)
        builder.add_node("propose_fix",          self._node_propose_fix)
        builder.add_node("validate",             self._node_validate)
        builder.add_node("create_approval_gate", self._node_create_approval_gate)
        builder.add_node("create_low_conf_gate", self._node_create_low_conf_gate)

        builder.set_entry_point("check_intents")

        builder.add_conditional_edges(
            "check_intents",
            self._route_after_intent_check,
            {
                "no_action":        END,
                "policy_fast_path": "policy_fast_path",
                # escalate_human from suppress→escalate goes straight to gate
                "escalate":         "create_low_conf_gate",
            },
        )
        builder.add_conditional_edges(
            "policy_fast_path",
            self._route_after_fast_path,
            {
                "fast_path_resolved": "create_approval_gate",
                "investigate":        "investigate",
                "no_ai":              "no_ai_gate",
            },
        )
        builder.add_edge("no_ai_gate", END)
        builder.add_conditional_edges(
            "investigate",
            self._route_after_rca,
            {
                "no_action":               END,
                "low_confidence_escalate": "create_low_conf_gate",
                "propose_fix":             "propose_fix",
            },
        )
        builder.add_conditional_edges(
            "propose_fix",
            self._route_after_fix,
            {
                "no_action":        END,
                "skip_validation":  "create_approval_gate",
                "needs_validation": "validate",
            },
        )
        builder.add_edge("validate",             "create_approval_gate")
        builder.add_edge("create_approval_gate", END)
        builder.add_edge("create_low_conf_gate", END)

        return builder.compile()

    # ── routing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _route_after_intent_check(state: IncidentState) -> str:
        decision = state.get("pipeline_decision")
        if decision == "no_action":
            return "no_action"
        if decision == "escalate_human":
            return "escalate"
        return "policy_fast_path"

    def _route_after_fast_path(self, state: IncidentState) -> str:
        if state.get("pipeline_decision") == "fast_path_resolved":
            return "fast_path_resolved"
        if not self._ai_enabled_fn():
            return "no_ai"
        return "investigate"

    @staticmethod
    def _route_after_rca(state: IncidentState) -> str:
        return state.get("pipeline_decision") or "no_action"

    @staticmethod
    def _route_after_fix(state: IncidentState) -> str:
        return state.get("pipeline_decision") or "no_action"

    # ── helpers ───────────────────────────────────────────────────────────────

    def _make_agent(self, session_id: str):
        from ops_agent.agent import SYSTEM_PROMPT
        return create_react_agent(
            model=self._llm,
            tools=self._all_tools,
            checkpointer=MemorySaver(),
            prompt=SYSTEM_PROMPT,
        ), {"configurable": {"thread_id": session_id}, "callbacks": [self._sh]}

    # ── node: policy_fast_path ────────────────────────────────────────────────

    def _node_policy_fast_path(self, state: IncidentState) -> dict:
        """
        Programmatic fast path: try to resolve the alert using policies that
        have `conditions` + `rca_template` + `fix_template` defined.

        If a policy's conditions all pass the alert is resolved without any
        LLM calls and the graph routes directly to create_approval_gate.
        If no policy matches (or conditions fail) the graph falls through to
        the normal AI `investigate` node.
        """
        alertname = state["alertname"]
        tenant_id = state["tenant_id"]
        task_id   = state.get("rca_task_id")
        device    = state.get("device", "")

        # Look up device_role for policy specificity filtering. Uses depth=1 to get
        # the role name — Nautobot 2.x dropped the legacy slug field.
        device_role = ""
        try:
            resp = httpx.get(
                f"{settings.nautobot_url}/api/dcim/devices/",
                params={"name": device, "limit": 1, "depth": 1},
                headers={"Authorization": f"Token {settings.nautobot_token}"},
                timeout=5,
            )
            if resp.is_success:
                results = resp.json().get("results", [])
                if results:
                    role_obj = (results[0].get("role") or results[0].get("device_role") or {})
                    device_role = (role_obj.get("name") or role_obj.get("slug") or "").lower()
        except Exception:
            pass

        candidates = self._policy_registry.get_fast_path_policies(
            alertname=alertname,
            tenant_id=tenant_id,
            device_role=device_role,
            strict_role=True,
        )

        if not candidates:
            logger.debug("Workflow: no fast-path policies for alertname=%s", alertname)
            return {"pipeline_decision": None, "fast_path_policy_id": None}

        for policy in candidates:
            result = self._policy_resolver.resolve(state["event"], policy)
            if result is None:
                continue

            # Fast path matched — create the RCA task (normally created by _node_investigate)
            # so create_approval_gate has a valid task_id to work with.
            if not task_id:
                fp       = state.get("fingerprint", "")
                device   = state.get("device", "")
                instance = state.get("instance", "")
                task_id = self._ts.create_task(
                    type="rca",
                    created_by=AGENT,
                    assigned_to="ops_agent",
                    title=f"{'[MAINT] ' if state['in_maintenance'] else ''}⚡ {alertname}: {device or instance}",
                    alert_fingerprint=fp,
                    priority=state["priority"],
                    maintenance_window=state["in_maintenance"],
                    do_not_auto_execute=state["do_not_auto_execute"],
                    incident_id=state.get("incident_id"),
                    tenant_id=tenant_id,
                    content={
                        "alertname": alertname,
                        "severity":  state.get("severity", ""),
                        "device":    device,
                        "instance":  instance,
                        "summary":   state.get("summary", ""),
                        "description": state.get("description", ""),
                        "fingerprint": fp,
                    },
                )["id"]
                self._ts.claim_task(task_id, AGENT)
                self._ts.start_task(task_id, AGENT)

            logger.info(
                "Workflow: fast-path resolved via policy=%s (%s) for alertname=%s task=%s",
                result.policy_id, result.policy_name, alertname, task_id,
            )
            self._ts.add_event(
                task_id, AGENT, "fast_path_resolved",
                {"policy_id": result.policy_id, "policy_name": result.policy_name,
                 "conditions_matched": len(result.matched_conditions)},
            )
            return {
                "pipeline_decision":   "fast_path_resolved",
                "fast_path_policy_id": result.policy_id,
                "rca_task_id":         task_id,
                "rca":                 result.rca,
                "fix_proposal":        result.fix,
            }

        logger.info(
            "Workflow: fast-path candidates found but none matched for alertname=%s — falling back to AI",
            alertname,
        )
        return {"pipeline_decision": None, "fast_path_policy_id": None}

    # ── node: check_intents ───────────────────────────────────────────────────

    def _node_check_intents(self, state: IncidentState) -> dict:
        """
        Pre-investigation gate: check standing intents before running the LLM.
        suppress intents → no_action (skip investigation entirely)
        escalate intents → create low-confidence gate immediately
        No match         → continue to investigate
        """
        device    = state["device"]
        alertname = state["alertname"]
        tenant_id = state["tenant_id"]

        # Topology correlation: enrich state with blast_radius and is_leaf_symptom
        blast_radius    = []
        is_leaf_symptom = False
        try:
            blast_radius = self._topology.get_blast_radius(device)
            # Check if this device is downstream of an already-active RCA
            existing_rca_devices: list[str] = []
            active_rcas = self._ts.list_tasks(type="rca", status="running", limit=20)
            for t in active_rcas:
                try:
                    c = json.loads(t.get("content") or "{}")
                    d = c.get("device", "")
                    if d and d != device:
                        existing_rca_devices.append(d)
                except Exception:
                    pass
            if existing_rca_devices:
                root = self._topology.find_root_cause(
                    alert_device=device,
                    existing_rca_devices=existing_rca_devices,
                )
                if root:
                    is_leaf_symptom = True
                    logger.info(
                        "Workflow: %s identified as downstream symptom of root %s",
                        device, root,
                    )
        except Exception:
            logger.debug("Workflow: topology enrichment failed for %s — skipping", device)

        # Standing intent check
        matches = self._intent_registry.matching(device, alertname, tenant_id)
        for m in matches:
            self._ts.touch_intent(m.intent_id)
            if m.intent_type == "suppress":
                logger.info(
                    "Workflow: intent '%s' suppressed investigation for %s/%s",
                    m.intent_id, device, alertname,
                )
                return {
                    "pipeline_decision": "no_action",
                    "intent_match":      m.intent_id,
                    "blast_radius":      blast_radius,
                    "is_leaf_symptom":   is_leaf_symptom,
                }
            if m.intent_type == "escalate":
                logger.info(
                    "Workflow: intent '%s' escalated %s/%s",
                    m.intent_id, device, alertname,
                )
                return {
                    "pipeline_decision": "escalate_human",
                    "intent_match":      m.intent_id,
                    "blast_radius":      blast_radius,
                    "is_leaf_symptom":   is_leaf_symptom,
                }

        return {
            "pipeline_decision": None,
            "intent_match":      None,
            "blast_radius":      blast_radius,
            "is_leaf_symptom":   is_leaf_symptom,
        }

    # ── node: investigate ─────────────────────────────────────────────────────

    def _node_investigate(self, state: IncidentState) -> dict:
        alertname   = state["alertname"]
        severity    = state["severity"]
        device      = state["device"]
        instance    = state["instance"]
        summary     = state["summary"]
        description = state["description"]
        fp          = state["fingerprint"]
        session_id  = state["session_id"]

        focus = _ALERT_FOCUS.get(alertname, "investigate the alert and identify root cause")
        device_hint = (
            f"Note: '{device}' appears to be an IP address. Use get_all_devices() "
            f"to find the hostname, or search_nautobot('{device}') to resolve it."
            if device and device.replace(".", "").isdigit() else ""
        )

        blast_radius    = state.get("blast_radius", [])
        is_leaf_symptom = state.get("is_leaf_symptom", False)
        topology_hint   = ""
        if is_leaf_symptom:
            topology_hint = (
                "\n\nIMPORTANT: Topology analysis indicates this alert may be a DOWNSTREAM SYMPTOM "
                "of an upstream failure. Check get_topology() and any active alerts on connected "
                "upstream devices before concluding this device is the root cause. "
                "If the root cause is an upstream device, set AFFECTED to that device."
            )
        elif blast_radius:
            topology_hint = (
                f"\n\nBlast radius: the following devices may also be affected if this "
                f"device is the root cause: {', '.join(blast_radius[:5])}."
            )

        admin_shutdown_rule = (
            "\n\nCRITICAL RULE — Admin-shutdown vs physical failure:\n"
            "- Call get_device_metrics(device) and inspect interface_ifAdminStatus:\n"
            "  * ifAdminStatus=2 → interface was deliberately shut down (admin action)\n"
            "  * ifAdminStatus=1 + ifOperStatus=2 → physical or remote-side failure\n"
            "- NEVER diagnose 'physical link failure' when ifAdminStatus=2.\n"
            "- For admin-shutdown with no evidence of planned maintenance: ACTION should be 'no shutdown'.\n"
            "- For downstream effects (peer interface down): find the root device first."
            if alertname in ("InterfaceDown", "InterfaceAdminDown") else ""
        )

        prompt = (
            f"AUTOMATED ALERT INVESTIGATION\n\n"
            f"A new {severity.upper()} alert requires investigation:\n\n"
            f"  Alert:       {alertname}\n"
            f"  Device:      {device or 'unknown'}\n"
            f"  Instance:    {instance}\n"
            f"  Severity:    {severity}\n"
            f"  Summary:     {summary}\n"
            f"  Description: {description}\n"
            f"  Fingerprint: {fp}\n\n"
            f"Focus: {focus}"
            + admin_shutdown_rule
            + topology_hint
            + (f"\n\n{device_hint}" if device_hint else "")
            + f"\n\nUse your full toolkit in this order:\n"
            f"1. get_active_alerts() — confirm what is currently firing\n"
            f"2. get_device_metrics('{device or 'device'}') — check ifAdminStatus, ifOperStatus, reachability\n"
            f"3. run_show_commands('{device or 'device'}', 'show interfaces status') — verify live device state\n"
            f"4. get_interface_events(device) / get_bgp_events(device) — check syslog for recent events\n"
            f"5. get_topology() — assess blast radius and upstream/downstream links\n\n"
            f"End your response with:\n"
            f"DIAGNOSIS: <one sentence root cause — specify admin-shutdown or physical failure>\n"
            f"AFFECTED: <device name or 'unknown'>\n"
            f"ACTION: <exact recommended next step — for admin-shutdown specify 'no shutdown'>\n"
            f"CONFIDENCE: high | medium | low"
        )

        task_id = self._ts.create_task(
            type="rca",
            created_by=AGENT,
            assigned_to="ops_agent",
            title=f"{'[MAINT] ' if state['in_maintenance'] else ''}{alertname}: {device or instance}",
            alert_fingerprint=fp,
            priority=state["priority"],
            maintenance_window=state["in_maintenance"],
            do_not_auto_execute=state["do_not_auto_execute"],
            incident_id=state.get("incident_id"),
            tenant_id=state["tenant_id"],
            content={
                "alertname": alertname, "severity": severity,
                "device": device, "instance": instance,
                "summary": summary, "description": description, "fingerprint": fp,
            },
        )["id"]

        self._ts.claim_task(task_id, AGENT)
        self._ts.start_task(task_id, AGENT)

        try:
            self._rl.check_budget("ops_agent")
        except BudgetExceededError as exc:
            self._ts.fail_task(task_id, AGENT, str(exc))
            return {"pipeline_decision": "no_action", "error": str(exc), "rca_task_id": task_id}

        self._sh.set_context(session_id=session_id, task_id=task_id, task_type="rca")
        agent, config = self._make_agent(session_id)

        for attempt in range(2):
            try:
                result = agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=config)
                response = result["messages"][-1].content
                tool_calls = _extract_tool_calls(result["messages"])

                rca, _, rca_parse_failed = parse_structured(self._llm, response, RcaResult, config)
                if rca_parse_failed:
                    self._ts.add_event(task_id, AGENT, "parse_warning",
                                       {"stage": "rca", "detail": "structured output parsing failed — fields may be empty"})

                rca_data = {
                    "response":         response,
                    "tool_calls":       len(tool_calls),
                    "diagnosis":        rca.diagnosis,
                    "affected":         rca.affected,
                    "action":           rca.action,
                    "confidence":       rca.confidence,
                    "affected_devices": rca.affected_devices or ([device] + blast_radius),
                    "upstream_cause":   rca.upstream_cause,
                    "is_leaf_symptom":  is_leaf_symptom,
                    "completed_at":     datetime.now(timezone.utc).isoformat(),
                }
                self._ts.complete_task(task_id, AGENT, rca_data)
                self._ts.add_event(task_id, AGENT, "rca_complete", rca_data)
                logger.info("Workflow: RCA complete task=%s confidence=%s", task_id, rca.confidence)

                no_action = any(kw in rca.action.lower() for kw in
                                ("no action", "no fix", "already resolved", "self-healed", "monitor only"))
                if no_action:
                    decision = "no_action"
                elif rca.confidence == "low":
                    decision = "low_confidence_escalate"
                else:
                    decision = "propose_fix"

                self._sh.clear_context()
                return {
                    "rca_task_id":       task_id,
                    "rca":               rca.model_dump(),
                    "pipeline_decision": decision,
                    "error":             None,
                }

            except Exception as exc:
                is_rate_limit = "rate_limit_exceeded" in str(exc) or "429" in str(exc)
                if is_rate_limit and attempt == 0:
                    wait_s = _parse_retry_after(exc)
                    self._ts.add_event(task_id, AGENT, "rate_limit_retry",
                                       {"stage": "rca", "wait_seconds": wait_s, "attempt": 1})
                    logger.warning("Workflow: rate limit in investigate, retrying in %ds task=%s",
                                   wait_s, task_id)
                    time.sleep(wait_s)
                    continue
                # Escalate to human rather than silently failing — a failed
                # investigate means the operator should be notified so they
                # can investigate manually.
                err_msg = str(exc)[:300]
                self._ts.add_event(task_id, AGENT, "investigate_failed",
                                   {"error": err_msg, "will_escalate": True})
                logger.exception("Workflow: investigate node failed task=%s — escalating to human", task_id)
                self._sh.clear_context()
                # Include the original alert details so human reviewers have context
                # even when AI investigation fails (e.g. rate-limit errors).
                diag_prefix = (
                    f"[AUTO-INVESTIGATION FAILED] {alertname} on {device or instance or 'unknown'}.\n"
                    f"Summary: {summary or 'N/A'}\n"
                    f"Failure reason: {err_msg[:200]}"
                )
                fallback_rca = {
                    "diagnosis":   diag_prefix,
                    "affected":    device,
                    "action":      (
                        f"Manual investigation required. "
                        f"Alert: {alertname} on {device or instance}. "
                        f"Summary: {summary}"
                    ),
                    "confidence":  "low",
                    "affected_devices": [device] if device else [],
                    "upstream_cause": "",
                    "is_leaf_symptom": is_leaf_symptom,
                    "response": "",
                }
                return {
                    "rca_task_id":       task_id,
                    "rca":               fallback_rca,
                    "pipeline_decision": "low_confidence_escalate",
                    "error":             err_msg,
                }

        self._sh.clear_context()
        return {"rca_task_id": task_id, "pipeline_decision": "no_action"}

    # ── node: propose_fix ─────────────────────────────────────────────────────

    def _node_propose_fix(self, state: IncidentState) -> dict:
        rca        = state["rca"] or {}
        fp         = state["fingerprint"]
        task_id    = state["rca_task_id"]
        session_id = f"fix-{state['session_id']}"
        alertname  = state["alertname"]

        affected_device = rca.get("affected") or state["device"] or "unknown"
        diagnosis       = rca.get("diagnosis", "")
        action          = rca.get("action", "")
        confidence      = rca.get("confidence", "")
        rca_response    = rca.get("response", "")

        admin_fix_rule = (
            "\n\nCRITICAL FIX RULE — Admin-shutdown:\n"
            "If the diagnosis indicates the interface was administratively shut down "
            "(ifAdminStatus=2 or 'admin-shutdown' in diagnosis), then:\n"
            "  FIX_TYPE: config_change\n"
            "  COMMANDS: interface <ifname>\\n no shutdown\n"
            "  RISK: low\n"
            "Do NOT use escalate_human for admin-shutdown — it is a simple, reversible config change.\n"
            "Only escalate_human for: hardware failure, unknown cause, or confirmed planned maintenance."
            if alertname in ("InterfaceDown", "InterfaceAdminDown") else ""
        )

        prompt = (
            f"AUTOMATED FIX GENERATION REQUEST\n\n"
            f"Root cause analysis is complete. Now generate the remediation fix.\n\n"
            f"  Alert:      {alertname}\n"
            f"  Device:     {affected_device}\n"
            f"  Diagnosis:  {diagnosis}\n"
            f"  Action hint from investigation: {action}\n"
            f"  Confidence: {confidence}\n\n"
            f"Investigation summary (last 1500 chars):\n"
            f"---\n{str(rca_response)[-1500:]}\n---"
            + admin_fix_rule
            + f"\n\nSteps:\n"
            f"1. get_runbook('{alertname}') — look up the canonical fix procedure first\n"
            f"2. get_device_info('{affected_device}') — confirm platform and current status\n"
            f"3. run_show_commands('{affected_device}', 'show interfaces status') — verify live device state\n"
            f"4. run_config_commands(device, config_lines, check_mode=True) — simulate the fix\n\n"
            f"End your response with exactly these lines:\n"
            f"FIX_TYPE: config_change | runbook | no_action | escalate_human\n"
            f"DEVICE: <exact device hostname>\n"
            f"COMMANDS: <config lines to apply, or 'none'>\n"
            f"RISK: low | medium | high\n"
            f"CONFIDENCE: high | medium | low\n"
            f"REASON: <one sentence explaining the fix>"
        )

        try:
            self._rl.check_budget("ops_agent")
        except BudgetExceededError as exc:
            self._ts.add_event(task_id, AGENT, "fix_proposal_failed", {"error": str(exc)})
            return {"pipeline_decision": "no_action", "error": str(exc)}

        self._sh.set_context(session_id=session_id, task_id=task_id, task_type="fix_proposal")
        agent, config = self._make_agent(session_id)

        for attempt in range(2):
            try:
                result = agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=config)
                response = result["messages"][-1].content
                tool_calls = _extract_tool_calls(result["messages"])

                fix, _, fix_parse_failed = parse_structured(self._llm, response, FixProposalResult, config)
                if fix_parse_failed:
                    self._ts.add_event(task_id, AGENT, "parse_warning",
                                       {"stage": "fix_proposal", "detail": "structured output parsing failed — fix fields may be empty"})

                config_diff = ""
                if fix.fix_type != "no_action" and fix.commands != "none":
                    config_diff = _fetch_config_diff(fix.device, fix.commands)

                fix_data = {
                    "fix_type":      fix.fix_type,
                    "device":        fix.device,
                    "commands":      fix.commands,
                    "risk":          fix.risk,
                    "confidence":    fix.confidence,
                    "reason":        fix.reason,
                    "config_diff":   config_diff,
                    "tool_calls":    len(tool_calls),
                    "full_response": response[-3000:],
                    "completed_at":  datetime.now(timezone.utc).isoformat(),
                }
                self._ts.add_event(task_id, AGENT, "fix_proposal_complete", fix_data)
                logger.info("Workflow: fix_proposal complete task=%s fix_type=%s risk=%s",
                            task_id, fix.fix_type, fix.risk)

                if fix.fix_type == "no_action":
                    decision = "no_action"
                elif fix.fix_type == "escalate_human" or fix.risk == "high":
                    decision = "skip_validation"
                else:
                    decision = "needs_validation"

                fix_state = fix.model_dump()
                fix_state["config_diff"] = config_diff

                self._sh.clear_context()
                return {
                    "fix_proposal":      fix_state,
                    "pipeline_decision": decision,
                    "error":             None,
                }

            except Exception as exc:
                is_rate_limit = "rate_limit_exceeded" in str(exc) or "429" in str(exc)
                if is_rate_limit and attempt == 0:
                    wait_s = _parse_retry_after(exc)
                    self._ts.add_event(task_id, AGENT, "rate_limit_retry",
                                       {"stage": "fix_proposal", "wait_seconds": wait_s, "attempt": 1})
                    logger.warning("Workflow: rate limit in propose_fix, retrying in %ds task=%s",
                                   wait_s, task_id)
                    time.sleep(wait_s)
                    continue

                # Final failure — synthesise escalate_human fix so the pipeline always
                # reaches a human-reviewable approval gate rather than dying silently.
                # Pre-populate commands from the RCA's action field so the operator
                # doesn't have to guess what to type at the approval gate.
                self._ts.add_event(task_id, AGENT, "fix_proposal_failed",
                                   {"error": str(exc)[:500], "rca_action_used": bool(action)})
                logger.error("Workflow: propose_fix failed task=%s (attempt %d): %s",
                             task_id, attempt + 1, exc)
                fallback_fix = {
                    "fix_type":    "escalate_human",
                    "device":      affected_device,
                    "commands":    action or "none",  # carry forward RCA action as hint
                    "risk":        "unknown",
                    "confidence":  "low",
                    "reason": (
                        f"Fix generation failed ({type(exc).__name__}). "
                        f"RCA suggests: {action[:200] if action else diagnosis[:200]}. "
                        "Verify and approve the pre-populated commands, or enter new ones."
                        if action else
                        f"Fix generation failed ({type(exc).__name__}). "
                        f"RCA: {diagnosis[:200]}. "
                        "Please enter the fix commands manually in the operator field."
                    ),
                    "config_diff": "",
                }
                self._sh.clear_context()
                return {
                    "fix_proposal":      fallback_fix,
                    "pipeline_decision": "skip_validation",
                    "error":             str(exc)[:300],
                }

        self._sh.clear_context()
        return {"fix_proposal": None, "pipeline_decision": "no_action"}

    # ── node: validate ────────────────────────────────────────────────────────

    def _node_validate(self, state: IncidentState) -> dict:
        fix        = state["fix_proposal"] or {}
        rca        = state["rca"] or {}
        task_id    = state["rca_task_id"]
        session_id = f"val-{state['session_id']}"

        fix_type   = fix.get("fix_type", "unknown")
        device     = fix.get("device", "unknown")
        commands   = fix.get("commands", "none")
        risk       = fix.get("risk", "unknown")
        fix_reason = fix.get("reason", "")
        diagnosis  = rca.get("diagnosis", "")

        prompt = (
            f"AUTOMATED FIX VALIDATION REQUEST\n\n"
            f"A fix has been proposed for a network alert. "
            f"Validate whether this fix is correct and safe.\n\n"
            f"Context:\n"
            f"  Root cause:           {diagnosis}\n"
            f"  Proposed fix type:    {fix_type}\n"
            f"  Target device:        {device}\n"
            f"  Configuration commands:\n"
            f"    {commands}\n"
            f"  Assessed risk:        {risk}\n"
            f"  Reasoning:            {fix_reason}\n\n"
            f"Validation steps:\n"
            f"1. get_topology() — check blast radius\n"
            f"2. get_device_metrics('{device}') — confirm current device state\n"
            f"3. get_connected_devices('{device}') — identify dependencies\n"
            f"4. get_active_alerts() — verify original alert is still firing\n"
            f"5. run_show_commands('{device}', 'show running-config') — read current config\n\n"
            f"End your response with exactly these lines:\n"
            f"VERDICT: correct | incorrect | partial | unverifiable\n"
            f"CONFIDENCE: high | medium | low\n"
            f"RISK_CONFIRMED: low | medium | high\n"
            f"NOTES: <one sentence summarising your validation finding>"
        )

        try:
            self._rl.check_budget("ops_agent")
        except BudgetExceededError as exc:
            self._ts.add_event(task_id, AGENT, "validation_failed", {"error": str(exc)})
            return {"error": str(exc)}

        self._sh.set_context(session_id=session_id, task_id=task_id, task_type="validation")
        agent, config = self._make_agent(session_id)

        for attempt in range(2):
            try:
                result = agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=config)
                response = result["messages"][-1].content
                tool_calls = _extract_tool_calls(result["messages"])

                val, _, val_parse_failed = parse_structured(self._llm, response, ValidationResult, config)
                if val_parse_failed:
                    self._ts.add_event(task_id, AGENT, "parse_warning",
                                       {"stage": "validation", "detail": "structured output parsing failed"})

                val_data = {
                    "verdict":        val.verdict,
                    "confidence":     val.confidence,
                    "risk_confirmed": val.risk_confirmed,
                    "notes":          val.notes,
                    "tool_calls":     len(tool_calls),
                    "full_response":  response[-3000:],
                    "completed_at":   datetime.now(timezone.utc).isoformat(),
                }
                self._ts.add_event(task_id, AGENT, "validation_complete", val_data)
                logger.info("Workflow: validation complete task=%s verdict=%s", task_id, val.verdict)

                self._sh.clear_context()
                return {"validation": val.model_dump(), "error": None}

            except Exception as exc:
                is_rate_limit = "rate_limit_exceeded" in str(exc) or "429" in str(exc)
                if is_rate_limit and attempt == 0:
                    wait_s = _parse_retry_after(exc)
                    self._ts.add_event(task_id, AGENT, "rate_limit_retry",
                                       {"stage": "validation", "wait_seconds": wait_s, "attempt": 1})
                    logger.warning("Workflow: rate limit in validate, retrying in %ds task=%s",
                                   wait_s, task_id)
                    time.sleep(wait_s)
                    continue
                self._ts.add_event(task_id, AGENT, "validation_failed", {"error": str(exc)[:500]})
                logger.error("Workflow: validate node failed task=%s (attempt %d): %s",
                             task_id, attempt + 1, exc)
                self._sh.clear_context()
                # Proceed to approval_gate without validation data — the graph has an
                # unconditional edge validate → create_approval_gate.
                return {"error": str(exc)[:300]}

        self._sh.clear_context()
        return {"error": "validation exhausted retries"}

    # ── node: no_ai_gate ─────────────────────────────────────────────────────

    def _node_no_ai_gate(self, state: IncidentState) -> dict:
        """
        Reached when ai_enabled=False and no programmatic fast-path policy matched.
        Creates a task in awaiting_approval so the operator can review and act manually.
        Records a no_ai_skipped event so the alert is visible in the UI and live feed.
        """
        alertname = state.get("alertname", "")
        device    = state.get("device", "") or state.get("instance", "")
        task_id   = state.get("rca_task_id")

        try:
            if not task_id:
                task = self._ts.create_task(
                    type="rca",
                    created_by=AGENT,
                    assigned_to="ops_agent",
                    title=f"[NO-AI] {alertname}: {device}",
                    alert_fingerprint=state.get("fingerprint", ""),
                    priority=state["priority"],
                    maintenance_window=state["in_maintenance"],
                    do_not_auto_execute=True,
                    tenant_id=state["tenant_id"],
                    content={
                        "alertname": alertname,
                        "device":    device,
                        "instance":  state.get("instance", ""),
                        "summary":   state.get("summary", ""),
                        "reason":    "AI investigation suppressed (ai_enabled=False). No programmatic fast-path policy matched. Manual review required.",
                    },
                )
                task_id = task["id"]

            self._ts.add_event(task_id, AGENT, "no_ai_skipped", {
                "reason":    "ai_enabled=False — LLM investigation suppressed",
                "alertname": alertname,
                "device":    device,
            })
            self._ts.request_approval(task_id, AGENT)
            logger.warning(
                "Workflow: AI disabled, no fast-path matched — task=%s alertname=%s device=%s",
                task_id, alertname, device,
            )
            return {"rca_task_id": task_id, "pipeline_decision": "no_ai"}

        except Exception as exc:
            logger.error("Workflow: no_ai_gate failed: %s", exc)
            return {"pipeline_decision": "no_ai", "error": str(exc)[:300]}

    # ── node: create_approval_gate ────────────────────────────────────────────

    def _node_create_approval_gate(self, state: IncidentState) -> dict:
        fix        = state["fix_proposal"] or {}
        rca        = state["rca"] or {}
        val        = state.get("validation") or {}
        task_id    = state["rca_task_id"]
        do_not_auto = state["do_not_auto_execute"]

        device   = fix.get("device", "unknown")
        fix_type = fix.get("fix_type", "config_change")
        risk     = fix.get("risk", "medium").lower()
        confidence = fix.get("confidence", "low").lower()

        # Look up device role for policy matching. depth=1 expands the role object
        # so we can read the name — Nautobot 2.x dropped the legacy slug field.
        device_role = ""
        try:
            resp = httpx.get(
                f"{settings.nautobot_url}/api/dcim/devices/",
                params={"name": device, "limit": 1, "depth": 1},
                headers={"Authorization": f"Token {settings.nautobot_token}"},
                timeout=5,
            )
            if resp.is_success:
                results = resp.json().get("results", [])
                if results:
                    role_obj = (results[0].get("role") or results[0].get("device_role") or {})
                    device_role = (role_obj.get("name") or role_obj.get("slug") or "").lower()
        except Exception:
            pass

        prior_success_count = self._ts.count_successful_executions(device, fix_type)

        # If the alert was resolved by the programmatic fast path, use that policy's
        # autonomy level directly — it already encodes the operator's intent and has
        # already been matched by its own conditions.  Only fall back to a fresh query
        # when the AI pipeline produced the fix (no fast_path_policy_id in state).
        fast_path_policy_id = state.get("fast_path_policy_id")
        if fast_path_policy_id:
            fp_policy = self._ts.get_policy(fast_path_policy_id)
        else:
            fp_policy = None

        if fp_policy:
            level = fp_policy["autonomy_level"]
            idx   = autonomy_level_index(level)
            policy_decision = AutonomyDecision(
                autonomy_level=level,
                requires_approval=idx <= 3,
                allow_execution=idx >= 4,
                policy_id=fp_policy["id"],
                reason=f"Fast-path policy '{fp_policy['name']}' governs this gate ({level}).",
            )
        else:
            policy_decision = self._policy_registry.query(
                fix_type=fix_type,
                device_role=device_role,
                environment=settings.environment,
                confidence=confidence,
                risk=risk,
                prior_success_count=prior_success_count,
                alertname=state["alertname"],
                tenant_id=state["tenant_id"],
            )
        auto = policy_decision.allow_execution and not do_not_auto

        # Store full approval context into the task content so the UI can display it
        approval_content = {
            "alertname":           state["alertname"],
            "fix_proposal":        fix,
            "rca":                 rca,
            "validation_verdict":  val.get("verdict", ""),
            "risk_confirmed":      val.get("risk_confirmed", risk),
            "chaos_notes":         val.get("notes", ""),
            "commands":            fix.get("commands", "none"),
            "device":              device,
            "device_role":         device_role,
            "config_diff":         fix.get("config_diff", ""),
            "do_not_auto_execute": do_not_auto,
            "policy_id":           policy_decision.policy_id,
            "autonomy_level":      policy_decision.autonomy_level,
            "reason": (
                "Device in maintenance window — auto-execution suppressed."
                if do_not_auto
                else f"Pipeline complete — human approval required to execute fix on {device}."
            ),
        }
        try:
            self._ts.update_task_content(task_id, approval_content)
            # Update task title to reflect approval stage
            from sqlalchemy import text as _text
            with self._ts._lock, self._ts._connect() as conn:
                title = f"{'AUTO-APPROVED' if auto else 'APPROVAL REQUIRED'}: {fix_type} on {device} [risk={risk}]"
                conn.execute(_text("UPDATE tasks SET title=:t WHERE id=:id"), {"t": title, "id": task_id})

            if auto:
                self._ts.add_event(
                    task_id, AGENT, "auto_approved",
                    {
                        "reason": policy_decision.reason,
                        "policy_id": policy_decision.policy_id,
                        "autonomy_level": policy_decision.autonomy_level,
                    },
                )
                self._ts.approve_task(task_id, "system")
                logger.info("Workflow: AUTO-APPROVED task=%s device=%s level=%s",
                            task_id, device, policy_decision.autonomy_level)
                # Immediately launch Phase 2 execution in a background thread.
                # Human-approved tasks go through POST /workflow/resume; auto-approved
                # tasks skip that HTTP round-trip and call resume_execution directly.
                threading.Thread(
                    target=self.resume_execution,
                    args=(task_id, "system", ""),
                    daemon=True,
                    name=f"auto-execute-{task_id}",
                ).start()
            else:
                self._ts.add_event(
                    task_id, AGENT, "approval_policy",
                    {
                        "policy_id":      policy_decision.policy_id,
                        "autonomy_level": policy_decision.autonomy_level,
                        "reason":         policy_decision.reason,
                    },
                )
                self._ts.request_approval(task_id, AGENT)
                logger.info("Workflow: approval requested task=%s device=%s level=%s",
                            task_id, device, policy_decision.autonomy_level)

            return {"pipeline_decision": "complete"}

        except Exception as exc:
            logger.error("Workflow: failed to create approval gate: %s", exc)
            return {"pipeline_decision": "complete", "error": str(exc)[:300]}

    # ── node: create_low_confidence_gate ──────────────────────────────────────

    def _node_create_low_conf_gate(self, state: IncidentState) -> dict:
        rca       = state["rca"] or {}
        task_id   = state["rca_task_id"]
        alertname = state["alertname"]
        device    = rca.get("affected") or state["device"] or "unknown"
        severity  = state["severity"]

        try:
            gate_content = {
                "alertname":         alertname,
                "alert":             state["event"],
                "escalation_reason": "low_confidence_rca",
                "device":            device,
                "severity":          severity,
                "rca": {
                    "diagnosis":          rca.get("diagnosis", ""),
                    "affected_device":    device,
                    "recommended_action": rca.get("action", ""),
                    "confidence":         rca.get("confidence", "low"),
                },
                "reason": (
                    f"Automated investigation failed for {alertname} on {device}. "
                    f"Automated remediation skipped. Please investigate manually."
                    if rca.get("diagnosis", "").startswith("[AUTO-INVESTIGATION FAILED]")
                    else (
                        f"Ops Agent has low confidence in its diagnosis for {alertname} "
                        f"on {device}. Automated remediation skipped. "
                        "Please investigate manually."
                    )
                ),
            }
            self._ts.update_task_content(task_id, gate_content)

            from sqlalchemy import text as _text
            with self._ts._lock, self._ts._connect() as conn:
                title = f"LOW CONFIDENCE — Manual review required: {alertname} on {device}"
                conn.execute(_text("UPDATE tasks SET title=:t WHERE id=:id"), {"t": title, "id": task_id})

            self._ts.request_approval(task_id, AGENT)
            logger.info("Workflow: low-confidence gate task=%s alert=%s", task_id, alertname)
            return {"pipeline_decision": "complete"}

        except Exception as exc:
            logger.error("Workflow: failed to create low-confidence gate: %s", exc)
            return {"pipeline_decision": "complete", "error": str(exc)[:300]}

    # ── public entry points ───────────────────────────────────────────────────

    def run(
        self,
        event:          dict,
        incident_id:    str | None  = None,
        in_maintenance: bool | None = None,
        priority:       str | None  = None,
    ) -> None:
        """Phase 1: investigation → fix → validate → approval gate."""
        alertname = event.get("alertname", "UnknownAlert")
        severity  = event.get("severity", "unknown")
        labels    = event.get("labels", {})
        device    = (
            event.get("device")
            or labels.get("sysName")
            or labels.get("agent_host")
            or event.get("instance", "").split(":")[0]
        )
        fp = event.get("fingerprint", "")

        if in_maintenance is None:
            in_maintenance = self._check_maintenance(device)
        if priority is None:
            priority = "low" if in_maintenance else ("high" if severity == "critical" else "normal")

        initial: IncidentState = {
            "alertname":          alertname,
            "severity":           severity,
            "device":             device or "",
            "instance":           event.get("instance", ""),
            "summary":            event.get("summary", ""),
            "description":        event.get("description", ""),
            "fingerprint":        fp,
            "event":              event,
            "incident_id":        incident_id,
            "rca_task_id":        None,
            "rca":                None,
            "fix_proposal":       None,
            "validation":         None,
            "pipeline_decision":  None,
            "fast_path_policy_id": None,
            "in_maintenance":     in_maintenance,
            "do_not_auto_execute": in_maintenance,
            "priority":           priority,
            "session_id":         f"wf-{fp[:12]}",
            "tenant_id":          settings.agent_tenant_id,
            "error":              None,
            "blast_radius":       [],
            "is_leaf_symptom":    False,
            "intent_match":       None,
        }

        with self._pipeline_sem:
            try:
                self._graph.invoke(initial)
                logger.info("Workflow: Phase 1 complete fingerprint=%s", fp[:12])
            except Exception as exc:
                logger.exception("Workflow: Phase 1 failed fingerprint=%s: %s", fp[:12], exc)

    def resume_execution(self, task_id: str, approved_by: str, operator_commands: str = "") -> None:
        """
        Phase 2: Execute the approved fix (check_mode=False) and verify resolution.
        Called in a daemon thread from POST /workflow/resume/{task_id}.

        operator_commands: optional override supplied by the human operator via
        the UI approval form.  Used when the agent produced fix_type=escalate_human
        with commands="none" and the operator types the actual fix themselves.
        """
        from ops_agent.agent import SYSTEM_PROMPT

        task = self._ts.get_task(task_id)
        if not task:
            logger.error("Workflow: resume — task %s not found", task_id)
            return

        approved_event = any(
            e.get("event_type") == "approved"
            for e in task.get("events", [])
        )
        if not approved_event:
            logger.warning("Workflow: resume called on task=%s with no approved event", task_id)
            return

        content = {}
        try:
            content = json.loads(task.get("content") or "{}")
        except Exception:
            pass

        fix_proposal = content.get("fix_proposal", {})
        _raw_device  = fix_proposal.get("device") or content.get("device", "unknown")
        # Strip interface-qualified names like "Ethernet1 on spine2" → "spine2"
        device   = _raw_device.split(" on ")[-1].strip() if " on " in _raw_device else _raw_device
        commands = fix_proposal.get("commands") or content.get("commands", "none")
        fix_type = fix_proposal.get("fix_type", "config_change")

        already_started = any(
            e.get("event_type") == "execution_started"
            for e in task.get("events", [])
        )
        if already_started:
            logger.info("Workflow: resume task=%s already executing — skipping", task_id)
            return

        if commands == "none" or fix_type == "no_action":
            if operator_commands:
                # Human supplied manual fix commands at the approval gate — use them
                commands = operator_commands
                fix_type = "config_change"
                self._ts.add_event(task_id, AGENT, "operator_commands_override",
                                   {"commands": commands, "reason": "operator provided at approval gate"})
                logger.info("Workflow: using operator-supplied commands for task=%s", task_id)
            else:
                self._ts.add_event(task_id, AGENT, "execution_complete",
                                   {"result": "No configuration commands to apply (escalate_human with no operator override)."})
                return

        if content.get("do_not_auto_execute"):
            self._ts.add_event(
                task_id, AGENT, "execution_suppressed",
                {"reason": "device in maintenance window — auto-execution disabled"},
            )
            return

        self._ts.add_event(task_id, AGENT, "execution_started",
                           {"device": device, "commands": commands})

        if not self._validate_in_lab(task_id, device, commands):
            self._ts.add_event(
                task_id, AGENT, "execution_aborted",
                {"reason": "lab validation failed — fix did not resolve alert in lab"},
            )
            return

        prompt = (
            f"CONFIRM: A human operator has reviewed and approved this fix. "
            f"Apply it now with check_mode=False — no further confirmation needed.\n\n"
            f"  Task ID:   {task_id}\n"
            f"  Device:    {device}\n"
            f"  Commands:  {commands}\n\n"
            f"Steps (execute in order):\n"
            f"1. run_show_commands('{device}', 'show interfaces status') "
            f"— verify current live state before applying\n"
            f"2. run_config_commands('{device}', '{commands}', check_mode=False) "
            f"— apply the approved fix\n"
            f"3. run_show_commands('{device}', 'show interfaces status') "
            f"— confirm the interface is now up\n\n"
            f"End your response with exactly these lines:\n"
            f"EXECUTION_STATUS: success | failed\n"
            f"DEVICE: <hostname>\n"
            f"CHANGES_APPLIED: <brief description of what was applied or why it failed>"
        )

        session_id = f"exec-{task_id}"
        try:
            self._rl.check_budget("ops_agent")
        except BudgetExceededError as exc:
            self._ts.add_event(task_id, AGENT, "execution_failed",
                               {"error": f"Budget exceeded: {exc}"})
            return

        self._sh.set_context(session_id=session_id, task_id=task_id, task_type="approval_gate")
        agent, config = self._make_agent(session_id)

        try:
            result = agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=config)
            response = result["messages"][-1].content
            tool_calls = _extract_tool_calls(result["messages"])

            execution, _, exec_parse_failed = parse_structured(self._llm, response, ExecutionResult, config)
            if exec_parse_failed:
                self._ts.add_event(task_id, AGENT, "parse_warning",
                                   {"stage": "execution", "detail": "structured output parsing failed"})
            config_check: dict = {}
            if execution.execution_status == "success" and commands != "none":
                config_check = _verify_config_applied(task_id, device, commands)

            self._ts.add_event(
                task_id, AGENT, "execution_complete",
                {
                    "status":          execution.execution_status,
                    "device":          device,
                    "changes_applied": execution.changes_applied,
                    "tool_calls":      len(tool_calls),
                    "config_applied":  config_check.get("config_applied"),
                    "found_lines":     config_check.get("found_lines", []),
                    "missing_lines":   config_check.get("missing_lines", []),
                },
            )
            logger.info("Workflow: execution complete task=%s status=%s",
                        task_id, execution.execution_status)

            if execution.execution_status == "success":
                try:
                    _auto_save_kb(task, content, execution, self._ts)
                except Exception:
                    logger.exception("Workflow: KB auto-save failed for task=%s — non-fatal", task_id)

            fp       = task.get("alert_fingerprint", "")
            rca_info = _get_rca_info(self._ts, fp)
            t = threading.Thread(
                target=_verify_resolution,
                args=(task_id, rca_info, self._ts, self._stop),
                kwargs={"learning_engine": self._learning_engine},
                daemon=True,
            )
            t.start()

        except BudgetExceededError as exc:
            self._ts.add_event(task_id, AGENT, "execution_failed",
                               {"error": f"Budget exceeded: {exc}"})
        except Exception as exc:
            self._ts.add_event(task_id, AGENT, "execution_failed",
                               {"error": str(exc)[:500]})
            logger.exception("Workflow: execution failed task=%s", task_id)
        finally:
            self._sh.clear_context()

    # ── maintenance check ─────────────────────────────────────────────────────

    def _check_maintenance(self, device: str) -> bool:
        if not settings.maintenance_check_enabled or not device:
            return False
        try:
            resp = httpx.get(
                f"{settings.nautobot_url}/api/dcim/devices/",
                params={"name": device, "limit": 1},
                headers={"Authorization": f"Token {settings.nautobot_token}"},
                timeout=5,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return False
            dev = results[0]
            status_slug = (dev.get("status") or {}).get("value", "").lower()
            maint_statuses = {s.strip().lower()
                              for s in settings.maintenance_statuses.split(",")}
            if status_slug in maint_statuses:
                return True
            tags = [t.get("slug", "") for t in dev.get("tags", [])]
            return settings.maintenance_tag.lower() in tags
        except Exception:
            return False

    # ── lab validation ────────────────────────────────────────────────────────

    def _validate_in_lab(self, task_id: str, device: str, commands: str) -> bool:
        if not settings.lab_validation_enabled:
            return True
        lab_device = settings.lab_device_prefix + device
        try:
            from shared.tools import run_config_commands as _rcc
            lab_result_str = _rcc.func(
                device_name=lab_device, config_lines=commands,
                check_mode=False, timeout=60,
            )
            lab_result = json.loads(lab_result_str)
            lab_status = (lab_result.get("status") or {})
            if isinstance(lab_status, dict):
                lab_status = lab_status.get("value", "")
            if str(lab_status).upper() not in ("COMPLETED", "SUCCESS", ""):
                self._ts.add_event(task_id, AGENT, "lab_validation_failed",
                                   {"reason": "lab fix application failed", "lab_device": lab_device})
                return False
            self._ts.add_event(task_id, AGENT, "lab_fix_applied", {"lab_device": lab_device})
            self._stop.wait(settings.lab_verify_delay)
            if self._stop.is_set():
                return True
            r = httpx.get(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=8)
            r.raise_for_status()
            firing_devices = {
                a["labels"].get("sysName", a["labels"].get("agent_host", ""))
                for a in r.json().get("data", {}).get("alerts", [])
                if a.get("state") == "firing"
            }
            cleared = lab_device not in firing_devices
            self._ts.add_event(task_id, AGENT,
                               "lab_validated" if cleared else "lab_validation_failed",
                               {"lab_device": lab_device, "alert_cleared": cleared})
            return cleared
        except Exception as exc:
            logger.warning("Workflow: lab validation error task=%s: %s — proceeding", task_id, exc)
            self._ts.add_event(task_id, AGENT, "lab_validation_error", {"error": str(exc)[:300]})
            return True  # fail-open


# ── module-level helpers ───────────────────────────────────────────────────────

def _extract_tool_calls(messages: list) -> list[dict]:
    calls = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            calls.append({"tool_name": msg.name, "output_summary": (msg.content or "")[:300]})
        elif isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                calls.append({"tool_name": tc.get("name", ""), "input_summary": str(tc.get("args", ""))[:200]})
    return calls


def _fetch_config_diff(device: str, commands: str) -> str:
    proposed = [l.strip() for l in commands.splitlines()
                if l.strip() and not l.strip().startswith("!")]
    if not proposed:
        return ""
    simple = "\n".join(f"+ {l}" for l in proposed)
    try:
        from shared.tools import run_show_commands
        section_hint = ""
        for line in proposed:
            words = line.split()
            if len(words) >= 2 and words[0].lower() in ("interface", "router", "ip", "neighbor", "vlan"):
                section_hint = f"{words[0]} {words[1]}"
                break
        show_cmd = (f"show running-config | section {section_hint}"
                    if section_hint else "show running-config")
        raw_result = json.loads(run_show_commands.func(device_name=device, commands=show_cmd, timeout=20))
        raw_output = raw_result.get("output", "")
        if isinstance(raw_output, list):
            raw_output = "\n".join(raw_output)
        current = [l for l in raw_output.splitlines() if l.strip()]
        if not current:
            return simple
        after = list(current)
        for pl in proposed:
            if pl not in after:
                after.append(pl)
        diff_lines = list(difflib.unified_diff(
            current, after, fromfile="current config", tofile="proposed config", lineterm="",
        ))
        return "\n".join(diff_lines) if diff_lines else simple
    except Exception:
        return simple


def _verify_config_applied(task_id: str, device: str, commands: str) -> dict:
    # Normalise literal \\n sequences (stored in DB) to real newlines before splitting
    normalised = commands.replace("\\n", "\n")
    # Positive lines only — "no X" commands remove config so can't be verified by presence
    applied = [
        ln.strip() for ln in normalised.splitlines()
        if ln.strip() and not ln.strip().startswith("!") and not ln.strip().lower().startswith("no ")
    ]
    # Lines being removed (e.g. "no shutdown") — verify they are ABSENT in running-config
    removed = [
        ln.strip()[3:].strip() for ln in normalised.splitlines()
        if ln.strip().lower().startswith("no ") and ln.strip().lower() != "no shutdown"
    ]
    if not applied and not removed:
        return {"config_applied": None, "note": "no verifiable positive config lines"}
    try:
        from shared.tools import run_show_commands
        raw = run_show_commands.func(device_name=device, commands="show running-config", timeout=30)
        result  = json.loads(raw)
        output  = result.get("output", "")
        if isinstance(output, list):
            output = "\n".join(str(x) for x in output)
        if not output.strip():
            output = "\n".join(str(e.get("message", "")) for e in result.get("log_entries", []))
        output_lower = output.lower()
        found   = [ln for ln in applied if ln.lower() in output_lower]
        missing = [ln for ln in applied if ln.lower() not in output_lower]
        removed_ok = [ln for ln in removed if ln.lower() not in output_lower]
        removed_still = [ln for ln in removed if ln.lower() in output_lower]
        return {
            "config_applied": len(missing) == 0 and len(removed_still) == 0,
            "found_lines":    found,
            "missing_lines":  missing + removed_still,
            "checked_lines":  len(applied) + len(removed),
        }
    except Exception as exc:
        return {"config_applied": None, "error": str(exc)[:200]}


def _auto_save_kb(gate_task: dict, gate_content: dict, execution, task_store: TaskStore) -> None:
    """
    Persist a KB entry after a successful pipeline execution.
    Extracts symptom/root_cause from the parent rca task and resolution from the gate.
    """
    fix_proposal = gate_content.get("fix_proposal") or {}
    alertname    = gate_content.get("alertname", "")
    device       = fix_proposal.get("device") or gate_content.get("device", "")
    commands     = fix_proposal.get("commands", "")
    changes      = execution.changes_applied or ""

    # Pull diagnosis from the linked rca task
    fp = gate_task.get("alert_fingerprint", "")
    diagnosis = ""
    device_role = ""
    if fp:
        rcas = task_store.list_tasks(type="rca", alert_fingerprint=fp, limit=1)
        if rcas:
            rca_result = rcas[0].get("result") or "{}"
            try:
                r = json.loads(rca_result) if isinstance(rca_result, str) else rca_result
                diagnosis = r.get("diagnosis", "")
                if not alertname:
                    alertname = json.loads(rcas[0].get("content") or "{}").get("alertname", "")
                device_role = r.get("device_role", "")
            except Exception:
                pass

    symptom    = f"{alertname} on {device}" if (alertname and device) else (alertname or device or "unknown")
    root_cause = diagnosis or fix_proposal.get("diagnosis", "") or "See pipeline task"
    resolution = changes or (f"Applied: {commands}" if commands and commands != "none" else "Escalated to human")

    if not (symptom and root_cause and resolution):
        return

    _kb_store.save(
        symptom=symptom,
        root_cause=root_cause,
        resolution=resolution,
        alert_type=alertname or None,
        device_type=device_role or None,
        source="pipeline",
        task_id=gate_task.get("id"),
    )


def _get_rca_info(task_store: TaskStore, fingerprint: str) -> dict:
    if not fingerprint:
        return {}
    try:
        tasks = task_store.list_tasks(type="rca", alert_fingerprint=fingerprint, limit=1)
        if not tasks:
            return {}
        content = json.loads(tasks[0].get("content") or "{}")
        info = {
            "alertname":  content.get("alertname", ""),
            "device":     content.get("device", ""),
            "instance":   content.get("instance", ""),
            "created_at": tasks[0].get("created_at"),
            "fix_type":   "",
            "device_role": "",
        }
        # Pull fix_type and device_role from fix_proposal event if available
        task_id = tasks[0].get("id", "")
        if task_id:
            for ev in task_store.get_task_events(task_id):
                if ev.get("event_type") == "fix_proposed":
                    detail = ev.get("detail") or {}
                    if isinstance(detail, str):
                        detail = json.loads(detail)
                    info["fix_type"]    = detail.get("fix_type", "")
                    info["device_role"] = detail.get("device_role", "")
                    break
        return info
    except Exception:
        return {}


def _verify_resolution(
    task_id: str, rca_info: dict, task_store: TaskStore, stop: threading.Event,
    learning_engine=None,
) -> None:
    if stop.wait(VERIFY_DELAY):
        return
    alertname   = rca_info.get("alertname", "")
    sysname     = rca_info.get("device", "")
    rca_created = rca_info.get("created_at")
    resolved    = False
    try:
        r = httpx.get(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=8)
        r.raise_for_status()
        firing = [a for a in r.json().get("data", {}).get("alerts", [])
                  if a.get("state") == "firing"]
        if alertname:
            still_firing = any(
                a["labels"].get("alertname") == alertname
                and (not sysname
                     or a["labels"].get("sysName", a["labels"].get("agent_host", "")) == sysname)
                for a in firing
            )
            resolved = not still_firing
    except Exception as exc:
        logger.warning("Workflow: Prometheus check failed for task=%s: %s", task_id, exc)

    ttr_s = 0
    if rca_created:
        try:
            t0    = datetime.strptime(rca_created, "%Y-%m-%d %H:%M:%S UTC")
            ttr_s = int((datetime.utcnow() - t0).total_seconds())
        except Exception:
            pass

    task_store.add_event(
        task_id, AGENT, "execution_verified",
        {
            "alert_resolved": resolved,
            "ttr_seconds":    ttr_s,
            "alertname":      alertname,
            "device":         sysname,
            "check_at":       datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )
    logger.info("Workflow: verification task=%s alert_resolved=%s ttr=%ds",
                task_id, resolved, ttr_s)

    if learning_engine is not None:
        # Extract policy_id from the auto_approved or approval_requested event
        policy_id  = None
        fix_type   = rca_info.get("fix_type", "")
        device_role = rca_info.get("device_role", "")
        fp_policy_id = None
        try:
            events = task_store.get_task_events(task_id)
            for ev in events:
                etype = ev.get("event_type", "")
                if etype in ("auto_approved", "approval_policy") and not policy_id:
                    detail = ev.get("detail") or {}
                    if isinstance(detail, str):
                        detail = json.loads(detail) if detail else {}
                    policy_id = detail.get("policy_id")
                if etype == "fast_path_resolved" and not fp_policy_id:
                    detail = ev.get("detail") or {}
                    if isinstance(detail, str):
                        detail = json.loads(detail) if detail else {}
                    fp_policy_id = detail.get("policy_id")
        except Exception:
            pass
        learning_engine.record_outcome(
            policy_id=policy_id,
            fix_type=fix_type,
            device_role=device_role,
            alert_resolved=resolved,
            ttr_seconds=ttr_s,
        )
        # Fix 2: track fast-path policy outcomes independently of gate policy
        if fp_policy_id:
            try:
                task_store.increment_fast_path_outcome(fp_policy_id, success=resolved)
                logger.info("Workflow: fast-path outcome policy=%s resolved=%s", fp_policy_id, resolved)
            except Exception:
                pass
