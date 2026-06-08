"""
PolicyResolver — programmatic fast-path for known alert patterns.

When an Autonomy Policy has `conditions` defined, the resolver runs them
against live data (Nautobot, Prometheus, device show commands) before the AI
investigation starts. If all conditions pass it renders the `rca_template` and
`fix_template` and returns a ResolverResult, allowing the workflow to skip the
LLM stages entirely. If any condition fails (or an error occurs) it returns
None and the caller falls through to the normal AI path.

Condition types
---------------
show_command  — run a read-only show command via Nautobot and check the output
metric        — run an instant PromQL query and compare the scalar result
nautobot      — HTTP GET a Nautobot API path and compare a field value

Template variables
------------------
{device}, {interface}, {alertname}, {severity}, {device_ip}, {instance}
All extracted from alert labels; missing keys are left as-is.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)

# ── helpers reused from tools.py (duplicated here to avoid LangChain import) ─


def _nautobot_headers() -> dict:
    return {"Authorization": f"Token {settings.nautobot_token}"}


def _run_show(device_name: str, command: str, timeout: int = 60) -> str:
    """Submit a show command via Nautobot Jobs and return the output text."""
    base = settings.nautobot_url.rstrip("/")
    headers = _nautobot_headers()

    # Resolve job ID for "Commands Runner"
    jobs_resp = httpx.get(f"{base}/api/extras/jobs/", headers=headers,
                          params={"limit": 200}, timeout=20)
    jobs_resp.raise_for_status()
    job_id = None
    for job in jobs_resp.json().get("results", []):
        if job.get("name") == "Commands Runner":
            job_id = job["id"]
            break
    if not job_id:
        raise RuntimeError("Nautobot job 'Commands Runner' not found")

    # Resolve device ID
    dev_resp = httpx.get(f"{base}/api/dcim/devices/", headers=headers,
                         params={"name": device_name}, timeout=10)
    dev_resp.raise_for_status()
    results = dev_resp.json().get("results", [])
    if not results:
        raise RuntimeError(f"Device '{device_name}' not found in Nautobot")
    device_id = results[0]["id"]

    # Submit job
    run_resp = httpx.post(
        f"{base}/api/extras/jobs/{job_id}/run/",
        headers={**headers, "Content-Type": "application/json"},
        json={"data": {"device": device_id, "commands": command}},
        timeout=20,
    )
    run_resp.raise_for_status()
    result_id = (run_resp.json().get("job_result") or {}).get("id")
    if not result_id:
        raise RuntimeError("Job submission returned no result ID")

    # Poll until done
    terminal = {"SUCCESS", "FAILURE", "ERRORED", "FAILED"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        poll = httpx.get(f"{base}/api/extras/job-results/{result_id}/",
                         headers=headers, timeout=10)
        poll.raise_for_status()
        status = (poll.json().get("status") or {}).get("value", "UNKNOWN")
        if status in terminal:
            break
        time.sleep(3)

    # Fetch log text
    logs_resp = httpx.get(f"{base}/api/extras/job-results/{result_id}/logs/",
                          headers=headers, timeout=10)
    logs_resp.raise_for_status()
    log_data = logs_resp.json()
    entries = log_data if isinstance(log_data, list) else log_data.get("results", [])
    return "\n".join(e.get("message", "") for e in entries)


def _prometheus_instant(promql: str) -> str:
    """Run an instant PromQL query and return the first scalar value as a string."""
    resp = httpx.get(
        f"{settings.prometheus_url}/api/v1/query",
        params={"query": promql},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("data", {}).get("result", [])
    if not results:
        return ""
    # Return the value part of the first result (instant vector: [timestamp, value])
    return str(results[0].get("value", ["", ""])[1])


# ── ResolverResult ────────────────────────────────────────────────────────────

@dataclass
class ResolverResult:
    """Returned by PolicyResolver.resolve() on a successful programmatic match."""
    rca: dict          # same shape as _node_investigate output
    fix: dict          # same shape as _node_propose_fix output
    policy_id: str
    policy_name: str = ""
    matched_conditions: list[dict] = field(default_factory=list)


# ── PolicyResolver ────────────────────────────────────────────────────────────

class PolicyResolver:
    """
    Programmatic fast-path resolver.  Call resolve() before AI investigation;
    returns None if the policy has no conditions or they don't match.
    """

    def resolve(self, alert: dict, policy: dict) -> ResolverResult | None:
        """
        Try to resolve the alert using the policy's conditions + templates.

        Returns ResolverResult on success, None on condition failure or error.
        Never raises — any exception is logged and treated as a condition failure.
        """
        raw_conditions = policy.get("conditions")
        if not raw_conditions:
            return None  # post-hoc gate only — no fast path defined

        try:
            conditions = json.loads(raw_conditions) if isinstance(raw_conditions, str) else raw_conditions
        except json.JSONDecodeError:
            logger.warning("PolicyResolver: invalid conditions JSON in policy %s", policy.get("id"))
            return None

        if not conditions:
            return None

        ctx = self._build_context(alert, policy)

        matched: list[dict] = []
        for cond in conditions:
            try:
                ok = self._check(cond, ctx)
            except Exception as exc:
                logger.info("PolicyResolver: condition check raised %s — treating as no-match", exc)
                return None
            if not ok:
                logger.info(
                    "PolicyResolver: condition failed (type=%s) for policy=%s — falling back to AI",
                    cond.get("type"), policy.get("id"),
                )
                return None
            matched.append(cond)

        # All conditions passed — render templates
        try:
            rca = self._render_rca(policy, ctx)
            fix = self._render_fix(policy, ctx)
        except Exception as exc:
            logger.warning("PolicyResolver: template render failed for policy %s: %s", policy.get("id"), exc)
            return None

        logger.info(
            "PolicyResolver: all %d condition(s) passed for policy=%s (%s)",
            len(matched), policy.get("id"), policy.get("name"),
        )
        return ResolverResult(
            rca=rca,
            fix=fix,
            policy_id=policy.get("id", ""),
            policy_name=policy.get("name", ""),
            matched_conditions=matched,
        )

    # ── condition dispatch ────────────────────────────────────────────────────

    def _check(self, cond: dict, ctx: dict) -> bool:
        ctype = cond.get("type")
        if ctype == "show_command":
            return self._check_show_command(cond, ctx)
        if ctype == "metric":
            return self._check_metric(cond, ctx)
        if ctype == "nautobot":
            return self._check_nautobot(cond, ctx)
        logger.warning("PolicyResolver: unknown condition type %r — skipping", ctype)
        return False

    def _check_show_command(self, cond: dict, ctx: dict) -> bool:
        command = self._fmt(cond.get("command", ""), ctx)
        expect  = self._fmt(cond.get("expect_contains", ""), ctx)
        device  = ctx.get("device", "")
        if not command or not device:
            return False
        output = _run_show(device, command)
        return expect.lower() in output.lower()

    def _check_metric(self, cond: dict, ctx: dict) -> bool:
        query  = self._fmt(cond.get("query", ""), ctx)
        expect = self._fmt(str(cond.get("expect", "")), ctx)
        if not query:
            return False
        value = _prometheus_instant(query)
        return value == expect

    def _check_nautobot(self, cond: dict, ctx: dict) -> bool:
        path   = self._fmt(cond.get("path", ""), ctx)
        fpath  = cond.get("field", "")          # e.g. "results[0].enabled"
        expect = self._fmt(str(cond.get("expect", "")), ctx)
        if not path:
            return False
        base = settings.nautobot_url.rstrip("/")
        resp = httpx.get(f"{base}/{path.lstrip('/')}", headers=_nautobot_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        value = str(self._dig(data, fpath))
        return value.lower() == expect.lower()

    # ── template rendering ────────────────────────────────────────────────────

    def _render_rca(self, policy: dict, ctx: dict) -> dict:
        raw = policy.get("rca_template")
        if not raw:
            raise ValueError("Policy has no rca_template")
        tmpl = json.loads(raw) if isinstance(raw, str) else raw
        return {
            "diagnosis":       self._fmt(tmpl.get("diagnosis", ""), ctx),
            "affected":        self._fmt(tmpl.get("affected_device", ctx.get("device", "")), ctx),
            "action":          self._fmt(tmpl.get("action", ""), ctx),
            "confidence":      tmpl.get("confidence", "high"),
            "affected_devices": [ctx.get("device", "")] if ctx.get("device") else [],
            "upstream_cause":  self._fmt(tmpl.get("upstream_cause", ""), ctx),
            "is_leaf_symptom": bool(tmpl.get("is_leaf_symptom", False)),
            "response":        self._fmt(tmpl.get("diagnosis", ""), ctx),
        }

    def _render_fix(self, policy: dict, ctx: dict) -> dict:
        raw = policy.get("fix_template")
        if not raw:
            raise ValueError("Policy has no fix_template")
        tmpl = json.loads(raw) if isinstance(raw, str) else raw
        return {
            "fix_type":   tmpl.get("fix_type", "config_change"),
            "device":     ctx.get("device", ""),
            "commands":   self._fmt(tmpl.get("commands", ""), ctx),
            "risk":       tmpl.get("risk", "low"),
            "confidence": tmpl.get("confidence", "high"),
            "reason":     self._fmt(tmpl.get("reason", "Programmatic fast-path match."), ctx),
            "config_diff": "",
        }

    # ── utilities ─────────────────────────────────────────────────────────────

    def _build_context(self, alert: dict, policy: dict) -> dict:
        """Extract template variables from the alert event dict."""
        labels = alert.get("labels", alert)  # alert may be the full event or just labels
        ctx = {
            "device":    labels.get("sysName") or labels.get("device") or "",
            "interface": labels.get("ifDescr") or labels.get("interface") or "",
            "alertname": labels.get("alertname", ""),
            "severity":  labels.get("severity", ""),
            "instance":  labels.get("instance", ""),
            "device_ip": "",
        }
        # Resolve primary IP for metric queries
        if ctx["device"]:
            try:
                resp = httpx.get(
                    f"{settings.nautobot_url}/api/dcim/devices/",
                    headers=_nautobot_headers(),
                    params={"name": ctx["device"]},
                    timeout=10,
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                if results:
                    ip = (results[0].get("primary_ip") or {}).get("address", "")
                    ctx["device_ip"] = ip.split("/")[0]
            except Exception:
                pass
        return ctx

    @staticmethod
    def _fmt(template: str, ctx: dict) -> str:
        """Safe str.format_map — leaves unknown keys as-is."""
        class _Safe(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"
        try:
            return template.format_map(_Safe(ctx))
        except Exception:
            return template

    @staticmethod
    def _dig(data: dict | list, field_path: str) -> object:
        """
        Navigate a nested structure via a dotted path with optional [n] indexing.
        e.g. "results[0].enabled"
        """
        cur: object = data
        for part in re.split(r"[\.\[\]]", field_path):
            if part == "":
                continue
            try:
                cur = cur[int(part)] if isinstance(cur, list) else cur[part]
            except (KeyError, IndexError, TypeError):
                return ""
        return cur
