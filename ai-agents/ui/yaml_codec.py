"""
Policy / Intent ↔ YAML codec for the web UI.

Converts DB rows to human-editable YAML (and back, with validation) for the
YAML editors on the Policies and Intents pages, plus the starter blueprints.
Extracted from ui/main.py — pure functions, no FastAPI or store dependencies.
"""
from __future__ import annotations

import textwrap

import yaml as _yaml


def policy_to_yaml(p: dict) -> str:
    """Convert a policy DB row to a human-editable YAML string."""
    import json as _j
    doc: dict = {}
    for k in ("name", "alertname", "fix_type", "device_role", "environment", "description"):
        doc[k] = p.get(k) or ""

    doc["gate"] = {
        "level":          p.get("autonomy_level", "L2"),
        "min_confidence": p.get("min_confidence", "low"),
        "max_risk":       p.get("max_risk", "high"),
        "promotable":     bool(p.get("promotable", True)),
    }

    conditions: list = []
    if p.get("conditions"):
        try:
            conditions = _j.loads(p["conditions"])
        except Exception:
            conditions = []

    rca: dict = {}
    if p.get("rca_template"):
        try:
            rca = _j.loads(p["rca_template"])
        except Exception:
            rca = {}

    fix: dict = {}
    if p.get("fix_template"):
        try:
            fix = _j.loads(p["fix_template"])
        except Exception:
            fix = {}

    if conditions or rca or fix:
        fp: dict = {}
        if conditions:
            fp["conditions"] = conditions
        if rca:
            fp["rca"] = {
                "diagnosis":      rca.get("diagnosis", ""),
                "confidence":     rca.get("confidence", "high"),
                "action":         rca.get("action", ""),
                "upstream_cause": rca.get("upstream_cause", ""),
            }
        if fix:
            fp["fix"] = {
                "fix_type":   fix.get("fix_type", "config_change"),
                "commands":   fix.get("commands", ""),
                "risk":       fix.get("risk", "low"),
                "confidence": fix.get("confidence", "high"),
                "reason":     fix.get("reason", ""),
            }
        doc["fast_path"] = fp

    return _yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False, width=100)


def yaml_to_policy(yaml_str: str, tenant_id: str = "default") -> dict:
    """Parse a YAML string into a policy data dict ready for create/update.
    Raises ValueError with a user-readable message on any problem."""
    import json as _j
    try:
        doc = _yaml.safe_load(yaml_str)
    except _yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc

    if not isinstance(doc, dict):
        raise ValueError("Expected a YAML mapping at the top level.")

    name = (doc.get("name") or "").strip()
    if not name:
        raise ValueError("'name' is required.")

    valid_levels = {"L0", "L1", "L2", "L3", "L4", "L5"}
    valid_conf   = {"low", "medium", "high", "certain"}
    valid_risk   = {"low", "medium", "high"}

    gate = doc.get("gate") or {}
    level = (gate.get("level") or "L2").strip()
    if level not in valid_levels:
        raise ValueError(f"gate.level must be one of {sorted(valid_levels)}, got '{level}'.")
    min_conf = (gate.get("min_confidence") or "low").strip()
    if min_conf not in valid_conf:
        raise ValueError(f"gate.min_confidence must be one of {sorted(valid_conf)}, got '{min_conf}'.")
    max_risk = (gate.get("max_risk") or "high").strip()
    if max_risk not in valid_risk:
        raise ValueError(f"gate.max_risk must be one of {sorted(valid_risk)}, got '{max_risk}'.")

    data: dict = {
        "name":           name,
        "alertname":      (doc.get("alertname") or "").strip(),
        "fix_type":       (doc.get("fix_type") or "").strip(),
        "device_role":    (doc.get("device_role") or "").strip(),
        "environment":    (doc.get("environment") or "").strip(),
        "description":    (doc.get("description") or "").strip(),
        "autonomy_level": level,
        "min_confidence": min_conf,
        "max_risk":       max_risk,
        "promotable":     bool(gate.get("promotable", True)),
        "tenant_id":      tenant_id,
        "conditions":     None,
        "rca_template":   None,
        "fix_template":   None,
    }

    fp = doc.get("fast_path")
    if isinstance(fp, dict):
        conds = fp.get("conditions")
        if conds:
            if not isinstance(conds, list):
                raise ValueError("fast_path.conditions must be a list.")
            valid_ctypes = {"metric", "show_command", "nautobot"}
            for i, c in enumerate(conds):
                if not isinstance(c, dict):
                    raise ValueError(f"fast_path.conditions[{i}] must be a mapping.")
                if c.get("type") not in valid_ctypes:
                    raise ValueError(f"fast_path.conditions[{i}].type must be one of {sorted(valid_ctypes)}.")
            data["conditions"] = _j.dumps(conds)

        rca = fp.get("rca")
        if isinstance(rca, dict) and rca.get("diagnosis"):
            rconf = (rca.get("confidence") or "high").strip()
            if rconf not in valid_conf:
                raise ValueError(f"fast_path.rca.confidence must be one of {sorted(valid_conf)}.")
            data["rca_template"] = _j.dumps({
                "diagnosis":       (rca.get("diagnosis") or "").strip(),
                "confidence":      rconf,
                "action":          (rca.get("action") or "").strip(),
                "affected_device": "{device}",
                "upstream_cause":  (rca.get("upstream_cause") or "").strip(),
                "is_leaf_symptom": False,
            })

        fix = fp.get("fix")
        valid_ftypes = {"config_change", "runbook", "escalate_human", "no_action"}
        if isinstance(fix, dict) and fix.get("commands"):
            ftype = (fix.get("fix_type") or "config_change").strip()
            if ftype not in valid_ftypes:
                raise ValueError(f"fast_path.fix.fix_type must be one of {sorted(valid_ftypes)}.")
            frisk = (fix.get("risk") or "low").strip()
            if frisk not in valid_risk:
                raise ValueError(f"fast_path.fix.risk must be one of {sorted(valid_risk)}.")
            fconf = (fix.get("confidence") or "high").strip()
            if fconf not in valid_conf:
                raise ValueError(f"fast_path.fix.confidence must be one of {sorted(valid_conf)}.")
            data["fix_template"] = _j.dumps({
                "fix_type":   ftype,
                "commands":   fix.get("commands", "").strip(),
                "risk":       frisk,
                "confidence": fconf,
                "reason":     (fix.get("reason") or "").strip(),
            })

    return data


# ── Intent ↔ YAML serialisation ──────────────────────────────────────────────

def intent_to_yaml(i: dict) -> str:
    """Convert an intent DB row to a human-editable YAML string."""
    doc: dict = {
        "name":        i.get("name", ""),
        "type":        i.get("intent_type", "monitor"),
        "description": i.get("description", ""),
        "device":      i.get("device", ""),
        "alertname":   i.get("alertname", ""),
        "enabled":     bool(i.get("enabled", True)),
    }

    itype = i.get("intent_type", "monitor")
    if itype == "monitor":
        doc["monitor"] = {
            "query":            i.get("metric_query", ""),
            "threshold":        i.get("threshold", ""),
            "interval_seconds": int(i.get("interval_seconds") or 300),
            "cooldown_minutes": int(i.get("cooldown_minutes") or 0),
            "priority":         i.get("priority") or "normal",
        }
    elif itype == "chaos_schedule":
        doc["chaos"] = {
            "schedule": i.get("schedule", ""),
            "action":   i.get("action", ""),
        }

    return _yaml.dump(doc, default_flow_style=False, allow_unicode=True,
                      sort_keys=False, width=100)


def yaml_to_intent(yaml_str: str, tenant_id: str = "default") -> dict:
    """Parse a YAML string into an intent data dict ready for create/update.
    Raises ValueError with a user-readable message on any problem."""
    try:
        doc = _yaml.safe_load(yaml_str)
    except _yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc

    if not isinstance(doc, dict):
        raise ValueError("Expected a YAML mapping at the top level.")

    name = (doc.get("name") or "").strip()
    if not name:
        raise ValueError("'name' is required.")

    valid_types    = {"suppress", "escalate", "monitor", "chaos_schedule"}
    valid_priority = {"low", "normal", "high"}

    itype = (doc.get("type") or "monitor").strip()
    if itype not in valid_types:
        raise ValueError(f"'type' must be one of {sorted(valid_types)}, got '{itype}'.")

    data: dict = {
        "name":        name,
        "intent_type": itype,
        "description": (doc.get("description") or "").strip(),
        "device":      (doc.get("device") or "").strip(),
        "alertname":   (doc.get("alertname") or "").strip(),
        "enabled":     bool(doc.get("enabled", True)),
        "tenant_id":   tenant_id,
        # defaults — overridden below for monitor intents
        "metric_query":     "",
        "threshold":        "",
        "interval_seconds": 300,
        "cooldown_minutes": 0,
        "priority":         "normal",
        "schedule":         "",
        "action":           "",
    }

    if itype == "monitor":
        mon = doc.get("monitor") or {}
        if not isinstance(mon, dict):
            raise ValueError("'monitor' must be a mapping.")
        query = (mon.get("query") or "").strip()
        if not query:
            raise ValueError("monitor.query is required for monitor intents.")
        data["metric_query"] = query
        data["threshold"]    = (mon.get("threshold") or "").strip()
        try:
            data["interval_seconds"] = max(60, int(mon.get("interval_seconds") or 300))
        except (ValueError, TypeError):
            raise ValueError("monitor.interval_seconds must be an integer >= 60.")
        try:
            data["cooldown_minutes"] = max(0, int(mon.get("cooldown_minutes") or 0))
        except (ValueError, TypeError):
            raise ValueError("monitor.cooldown_minutes must be a non-negative integer.")
        priority = (mon.get("priority") or "normal").strip()
        if priority not in valid_priority:
            raise ValueError(f"monitor.priority must be one of {sorted(valid_priority)}.")
        data["priority"] = priority

    elif itype == "chaos_schedule":
        chaos = doc.get("chaos") or {}
        if not isinstance(chaos, dict):
            raise ValueError("'chaos' must be a mapping.")
        data["schedule"] = (chaos.get("schedule") or "").strip()
        data["action"]   = (chaos.get("action") or "").strip()

    return data


INTENT_BLUEPRINTS: dict[str, tuple[str, str]] = {
    "config_drift_monitor": (
        "Config Drift Monitor",
        textwrap.dedent("""\
            name: Nautobot config drift monitor
            type: monitor
            description: Detect configuration drift on all devices via Nautobot Golden Config
            device: ""
            alertname: ""
            enabled: true

            monitor:
              query: nautobot://plugins/golden-config/config-compliance/?compliance=false
              threshold: ""
              interval_seconds: 300
              cooldown_minutes: 60
              priority: normal
            """),
    ),
    "prometheus_monitor": (
        "Prometheus Threshold Monitor",
        textwrap.dedent("""\
            name: My metric monitor
            type: monitor
            description: Fire an RCA task when a Prometheus metric breaches a threshold
            device: ""
            alertname: ""
            enabled: true

            monitor:
              query: 'up{job="telegraf"}'
              threshold: "< 1"
              interval_seconds: 120
              cooldown_minutes: 30
              priority: normal
            """),
    ),
    "suppress_intent": (
        "Suppress Alert",
        textwrap.dedent("""\
            name: Suppress leaf1 InterfaceDown
            type: suppress
            description: Suppress investigation for a known-flapping link during maintenance
            device: leaf1
            alertname: InterfaceDown
            enabled: true
            """),
    ),
    "chaos_schedule": (
        "Chaos Schedule",
        textwrap.dedent("""\
            name: Weekly BGP flap test
            type: chaos_schedule
            description: Scheduled chaos scenario — runs via the agent on a cron expression
            device: leaf1
            alertname: ""
            enabled: true

            chaos:
              schedule: "0 2 * * 1"
              action: "Simulate BGP flap on leaf1 — run flap_bgp_neighbor with check_mode=True"
            """),
    ),
}


# ── Policy blueprints ─────────────────────────────────────────────────────────

POLICY_BLUEPRINTS: dict[str, tuple[str, str]] = {
    "interface_admin_down": (
        "Interface Down Recovery",
        textwrap.dedent("""\
            name: InterfaceDown — lab spine
            alertname: InterfaceDown
            fix_type: config_change
            device_role: spine
            environment: lab
            description: Auto-restore admin-down spine interfaces in the lab

            gate:
              level: L2
              min_confidence: high
              max_risk: low
              promotable: true

            fast_path:
              conditions:
                - type: metric
                  query: "interface_ifAdminStatus{sysName='{device}',ifDescr='{interface}'}"
                  expect: "2"
                - type: nautobot
                  path: "/api/dcim/interfaces/?name={interface}&device={device}"
                  field: "results[0].enabled"
                  expect: "true"

              rca:
                diagnosis: "{interface} on {device} is administratively shut down"
                confidence: high
                action: "no shutdown"
                upstream_cause: ""

              fix:
                fix_type: config_change
                commands: |
                  interface {interface}
                   no shutdown
                risk: low
                confidence: high
                reason: "Restore admin-down interface {interface} on {device}"
            """),
    ),
    "bgp_peer_down": (
        "BGP Peer Down",
        textwrap.dedent("""\
            name: BGPPeerDown — leaf nodes
            alertname: BGPPeerDown
            fix_type: config_change
            device_role: leaf
            environment: ""
            description: Investigate and recover dropped BGP sessions on leaf switches

            gate:
              level: L2
              min_confidence: high
              max_risk: medium
              promotable: true

            fast_path:
              conditions:
                - type: metric
                  query: "bgp_peers_established{device='{device}'}"
                  expect: "0"

              rca:
                diagnosis: "BGP session to {peer} on {device} is down"
                confidence: high
                action: "clear ip bgp {peer} soft"
                upstream_cause: ""

              fix:
                fix_type: config_change
                commands: |
                  clear ip bgp {peer} soft
                risk: medium
                confidence: high
                reason: "Soft-reset BGP peer {peer} on {device}"
            """),
    ),
    "device_unreachable": (
        "Device Unreachable — Escalate",
        textwrap.dedent("""\
            name: DeviceUnreachable — escalate
            alertname: DeviceDown
            fix_type: escalate_human
            device_role: ""
            environment: ""
            description: Always escalate unreachable devices to a human — never auto-fix

            gate:
              level: L1
              min_confidence: low
              max_risk: high
              promotable: false

            # No fast_path — AI investigates, human decides
            """),
    ),
    "high_utilisation": (
        "High Interface Utilisation — Monitor Only",
        textwrap.dedent("""\
            name: HighInterfaceUtilisation — monitor
            alertname: HighInterfaceUtilization
            fix_type: no_action
            device_role: ""
            environment: ""
            description: Log and surface high-utilisation alerts without taking any action

            gate:
              level: L0
              min_confidence: low
              max_risk: high
              promotable: false

            # No fast_path — observe only, no commands executed
            """),
    ),
    "config_drift": (
        "Config Drift — Human Gate",
        textwrap.dedent("""\
            name: ConfigDrift — production gate
            alertname: ConfigDrift
            fix_type: config_change
            device_role: ""
            environment: production
            description: Config drift in production always requires explicit human approval

            gate:
              level: L3
              min_confidence: high
              max_risk: low
              promotable: false

            fast_path:
              rca:
                diagnosis: "Running config on {device} deviates from intended state"
                confidence: high
                action: "Review diff and apply intended config"
                upstream_cause: ""

              fix:
                fix_type: config_change
                commands: |
                  # Commands will be determined by the AI based on the diff
                  # Human must review before execution
                risk: high
                confidence: medium
                reason: "Reconcile config drift on {device} — human approval required"
            """),
    ),
    "lab_autonomous": (
        "Lab — Full Autonomy (L5)",
        textwrap.dedent("""\
            name: Lab autonomous — all alerts
            alertname: ""
            fix_type: ""
            device_role: ""
            environment: lab
            description: "Full autonomy for all alert types in the lab environment. Use only for testing."

            gate:
              level: L5
              min_confidence: low
              max_risk: high
              promotable: false

            # No fast_path — AI handles everything end-to-end
            """),
    ),
}
