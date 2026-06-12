"""
Policy synthesis: compile repeated LLM successes into fast-path policy drafts.

When the AI pipeline resolves the same (alertname, fix_type, device_role)
pattern successfully N times, the expensive LLM investigation is no longer
buying anything — the fix is known. This module detects those patterns and
synthesizes a *draft* fast-path policy (conditions + rca/fix templates) so the
next occurrence resolves programmatically with zero LLM calls.

Safety properties:
- Drafts are created **disabled** — an operator must review and enable them.
- Only alert types with a known, verifiable condition signature are
  synthesized (_CONDITION_RECIPES); unknown alert types are never compiled.
- The generalized fix commands must be identical across ALL contributing
  resolutions — if the LLM fixed the same alert differently each time, the
  pattern is not stable enough to compile.
- Fast-path resolutions are excluded from the evidence (already compiled).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DRAFT_PREFIX = "DRAFT (synthesized): "

# Per-alertname recipes for verifiable fast-path conditions.
# Double braces survive _fmt() substitution as literal PromQL braces; only
# {device} / {interface} are substituted by the policy resolver.
_CONDITION_RECIPES: dict[str, list[dict]] = {
    "InterfaceAdminDown": [{
        "type": "metric",
        "query": 'interface_ifAdminStatus{{ifDescr="{interface}",sysName="{device}"}}',
        "expect": "2",
    }],
    "InterfaceDown": [{
        "type": "metric",
        "query": 'interface_ifAdminStatus{{ifDescr="{interface}",sysName="{device}"}}',
        "expect": "2",
    }],
    "BGPPeerDown": [{
        "type": "metric",
        "query": 'bgp_peer_bgpPeerState{{sysName="{device}"}}',
        "expect_ne": "6",
    }],
}

_INTERFACE_RE = re.compile(
    r"\b(?:Ethernet|GigabitEthernet|TenGigE|Management|Port-Channel|xe-|ge-|et-)[\d/.]+\b",
    re.IGNORECASE,
)


def _generalize_commands(commands: str, device: str) -> str:
    """Replace concrete device / interface tokens with template placeholders."""
    out = commands or ""
    if device:
        out = out.replace(device, "{device}")
    out = _INTERFACE_RE.sub("{interface}", out)
    return out.strip()


class PolicySynthesizer:
    """
    Scans verified-resolved AI pipeline tasks and creates draft fast-path
    policies for stable, repeated fix patterns.
    """

    def __init__(self, task_store, min_successes: int = 3, scan_limit: int = 500) -> None:
        self._ts           = task_store
        self.min_successes = min_successes
        self.scan_limit    = scan_limit

    # ── public ─────────────────────────────────────────────────────────────────

    def synthesize(self, tenant_id: str = "default", environment: str = "lab") -> list[dict]:
        """Create draft policies for qualifying patterns. Returns the drafts."""
        groups = self._collect_evidence(tenant_id)
        existing = self._ts.list_policies(tenant_id=tenant_id)
        drafts: list[dict] = []

        for (alertname, fix_type, device_role), samples in groups.items():
            if len(samples) < self.min_successes:
                continue
            if alertname not in _CONDITION_RECIPES:
                logger.debug("PolicySynthesizer: no condition recipe for %s — skipping", alertname)
                continue
            if self._already_covered(existing, alertname, device_role):
                continue

            commands = {s["generalized_commands"] for s in samples}
            if len(commands) != 1 or not next(iter(commands)):
                logger.info(
                    "PolicySynthesizer: %s/%s fixes are not consistent across %d successes — skipping",
                    alertname, fix_type, len(samples),
                )
                continue

            draft = self._build_draft(
                alertname, fix_type, device_role,
                commands=next(iter(commands)),
                sample=samples[-1],
                n=len(samples),
                tenant_id=tenant_id,
                environment=environment,
            )
            created = self._ts.create_policy(draft)
            drafts.append(created)
            logger.info(
                "PolicySynthesizer: created draft policy %s for %s (%d successes) — review and enable",
                created["id"], alertname, len(samples),
            )
        return drafts

    # ── internals ──────────────────────────────────────────────────────────────

    def _collect_evidence(self, tenant_id: str) -> dict[tuple, list[dict]]:
        """Group verified-resolved, non-fast-path rca tasks by fix pattern."""
        tasks = self._ts.list_tasks(type="rca", tenant_id=tenant_id, limit=self.scan_limit)
        groups: dict[tuple, list[dict]] = {}
        for t in tasks:
            content = self._content(t)
            alertname = content.get("alertname", "")
            if not alertname:
                continue

            resolved, fast_path, rca = False, False, content.get("rca") or {}
            for ev in self._ts.get_task_events(t["id"]):
                etype  = ev.get("event_type", "")
                detail = ev.get("detail") or {}
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail) if detail else {}
                    except Exception:
                        detail = {}
                if etype == "fast_path_resolved":
                    fast_path = True
                elif etype == "execution_verified" and detail.get("alert_resolved"):
                    resolved = True
                elif etype == "rca_complete" and detail:
                    rca = detail

            if not resolved or fast_path:
                continue

            fix      = content.get("fix_proposal") or {}
            fix_type = fix.get("fix_type", "")
            device   = content.get("device", "")
            commands = content.get("commands") or fix.get("commands") or ""
            if not fix_type or not commands or commands == "none":
                continue

            key = (alertname, fix_type, content.get("device_role", ""))
            groups.setdefault(key, []).append({
                "task_id":              t["id"],
                "device":               device,
                "rca":                  rca,
                "fix":                  fix,
                "generalized_commands": _generalize_commands(commands, device),
            })
        return groups

    @staticmethod
    def _already_covered(existing: list[dict], alertname: str, device_role: str) -> bool:
        """True if any fast-path policy (enabled or draft) already targets this alertname
        with the same or wildcard role."""
        for p in existing:
            if not p.get("conditions"):
                continue
            if p.get("alertname") != alertname:
                continue
            if p.get("device_role", "") in ("", device_role):
                return True
        return False

    @staticmethod
    def _build_draft(
        alertname: str,
        fix_type: str,
        device_role: str,
        commands: str,
        sample: dict,
        n: int,
        tenant_id: str,
        environment: str,
    ) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rca   = sample["rca"]
        fix   = sample["fix"]
        return {
            "name":        f"{DRAFT_PREFIX}{alertname} fast path",
            "description": (
                f"Synthesized on {today} from {n} verified AI resolutions of "
                f"{alertname} ({fix_type}). Disabled until reviewed: check the "
                "conditions and commands, then enable."
            ),
            "alertname":           alertname,
            "fix_type":            fix_type,
            "device_role":         device_role,
            "environment":         environment,
            "min_confidence":      "high",
            "max_risk":            (fix.get("risk") or "low").lower(),
            "min_prior_successes": 0,
            "autonomy_level":      "L3",   # human gate on deploy; promotable later
            "enabled":             False,  # operator must review and enable
            "promotable":          True,
            "tenant_id":           tenant_id,
            "conditions":   json.dumps(_CONDITION_RECIPES[alertname]),
            "rca_template": json.dumps({
                "diagnosis": _generalize_commands(
                    str(rca.get("diagnosis", f"{alertname} on {{device}} — known pattern.")),
                    sample["device"],
                ),
                "confidence":      "high",
                "affected_device": "{device}",
                "action":          str(rca.get("action", "")),
                "upstream_cause":  "",
                "is_leaf_symptom": False,
            }),
            "fix_template": json.dumps({
                "fix_type":   fix_type,
                "commands":   commands,
                "risk":       (fix.get("risk") or "low").lower(),
                "confidence": "high",
                "reason": (
                    f"Synthesized fast path: {alertname} resolved identically "
                    f"{n} times by the AI pipeline."
                ),
            }),
        }

    @staticmethod
    def _content(task: dict) -> dict:
        raw = task.get("content") or "{}"
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            return {}
