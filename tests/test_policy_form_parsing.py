"""
Unit tests for the three policy form parsing helpers in ui/main.py:
  _parse_condition_rows(), _parse_rca_template(), _parse_fix_template()

These functions take a form-data dict (mimicking FastAPI's FormData) and
return a JSON string (or None) for storage in action_policies.

Run: .venv-host/bin/python -m pytest tests/test_policy_form_parsing.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add ai-agents to sys.path so we can import from ui.main
AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

# Import the three helpers directly; they are module-level functions in ui/main.py.
# We use importlib to avoid triggering the full FastAPI app startup.
import importlib.util, types

_main_path = AI_AGENTS_DIR / "ui" / "main.py"

def _load_helpers():
    """
    Extract only the three helper functions from ui/main.py without
    executing the FastAPI app startup code.
    """
    source = _main_path.read_text()
    # Isolate just the three helper functions by exec-ing the relevant portion
    # after injecting the json import.
    helpers_src = "import json\n"
    in_fn = False
    fn_names = {"_parse_condition_rows", "_parse_rca_template", "_parse_fix_template"}
    current_fn = None
    lines = source.splitlines()
    collected: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("def _parse_"):
            fname = stripped.split("(")[0].replace("def ", "")
            if fname in fn_names:
                in_fn = True
                current_fn = fname
                collected.append(line)
                continue
        if in_fn:
            if stripped and not stripped[0].isspace() and stripped[0] not in ("#", '"', "'"):
                # New top-level definition — stop collecting
                in_fn = False
                current_fn = None
            else:
                collected.append(line)
    helpers_src += "\n".join(collected)
    ns: dict = {}
    exec(compile(helpers_src, str(_main_path), "exec"), ns)
    return ns["_parse_condition_rows"], ns["_parse_rca_template"], ns["_parse_fix_template"]


_parse_condition_rows, _parse_rca_template, _parse_fix_template = _load_helpers()


# ── Helpers ───────────────────────────────────────────────────────────────────

def fd(**kwargs) -> dict:
    """Minimal form-data dict that supports .get()."""
    return kwargs


# ── _parse_condition_rows ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestParseConditionRows:
    def test_empty_returns_none(self):
        assert _parse_condition_rows(fd()) is None

    def test_single_metric_eq(self):
        result = _parse_condition_rows(fd(
            c_type_0="metric",
            c_query_0="interface_ifAdminStatus{sysName=\"{device}\"}",
            c_match_0="eq",
            c_value_0="2",
        ))
        parsed = json.loads(result)
        assert parsed == [{"type": "metric",
                           "query": 'interface_ifAdminStatus{sysName="{device}"}',
                           "expect": "2"}]

    def test_single_metric_ne(self):
        result = _parse_condition_rows(fd(
            c_type_0="metric",
            c_query_0="bgp_peer_bgpPeerState{sysName=\"{device}\"}",
            c_match_0="ne",
            c_value_0="6",
        ))
        parsed = json.loads(result)
        assert parsed == [{"type": "metric",
                           "query": 'bgp_peer_bgpPeerState{sysName="{device}"}',
                           "expect_ne": "6"}]

    def test_metric_defaults_to_eq_when_match_absent(self):
        result = _parse_condition_rows(fd(
            c_type_0="metric",
            c_query_0="some_metric",
            c_value_0="1",
        ))
        parsed = json.loads(result)
        assert "expect" in parsed[0]
        assert "expect_ne" not in parsed[0]

    def test_show_command_condition(self):
        result = _parse_condition_rows(fd(
            c_type_0="show_command",
            c_cmd_0="show interfaces {interface} status",
            c_contains_0="disabled",
        ))
        parsed = json.loads(result)
        assert parsed == [{"type": "show_command",
                           "command": "show interfaces {interface} status",
                           "expect_contains": "disabled"}]

    def test_nautobot_condition(self):
        result = _parse_condition_rows(fd(
            c_type_0="nautobot",
            c_path_0="/api/dcim/interfaces/?name={interface}&device={device}",
            c_field_0="results[0].enabled",
            c_nbexpect_0="false",
        ))
        parsed = json.loads(result)
        assert parsed == [{"type": "nautobot",
                           "path": "/api/dcim/interfaces/?name={interface}&device={device}",
                           "field": "results[0].enabled",
                           "expect": "false"}]

    def test_multiple_conditions(self):
        result = _parse_condition_rows(fd(
            c_type_0="metric", c_query_0="q1", c_match_0="eq", c_value_0="1",
            c_type_1="show_command", c_cmd_1="show bgp", c_contains_1="Established",
        ))
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[0]["type"] == "metric"
        assert parsed[1]["type"] == "show_command"

    def test_unknown_type_skipped(self):
        result = _parse_condition_rows(fd(
            c_type_0="unknown_type",
            c_type_1="metric", c_query_1="q", c_match_1="eq", c_value_1="1",
        ))
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["type"] == "metric"

    def test_stops_at_first_missing_type(self):
        # indices 0, 2 present but 1 absent — loop breaks at 1
        result = _parse_condition_rows(fd(
            c_type_0="metric", c_query_0="q", c_match_0="eq", c_value_0="1",
            c_type_2="metric", c_query_2="q2", c_match_2="eq", c_value_2="2",
        ))
        parsed = json.loads(result)
        assert len(parsed) == 1  # stops at gap


# ── _parse_rca_template ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestParseRcaTemplate:
    def test_empty_diagnosis_returns_none(self):
        assert _parse_rca_template(fd()) is None

    def test_full_rca_template(self):
        result = _parse_rca_template(fd(
            rca_diagnosis="{interface} on {device} is admin-down",
            rca_confidence="high",
            rca_action="no shutdown",
            rca_upstream_cause="",
        ))
        parsed = json.loads(result)
        assert parsed["diagnosis"] == "{interface} on {device} is admin-down"
        assert parsed["confidence"] == "high"
        assert parsed["action"] == "no shutdown"
        assert parsed["affected_device"] == "{device}"

    def test_is_leaf_symptom_false_by_default(self):
        result = _parse_rca_template(fd(rca_diagnosis="test"))
        parsed = json.loads(result)
        assert parsed["is_leaf_symptom"] is False

    def test_is_leaf_symptom_true_when_checked(self):
        result = _parse_rca_template(fd(rca_diagnosis="test", rca_is_leaf_symptom="1"))
        parsed = json.loads(result)
        assert parsed["is_leaf_symptom"] is True

    def test_result_is_valid_json(self):
        result = _parse_rca_template(fd(rca_diagnosis="BGP session down on {device}"))
        json.loads(result)  # should not raise


# ── _parse_fix_template ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestParseFixTemplate:
    def test_empty_commands_returns_none(self):
        assert _parse_fix_template(fd()) is None

    def test_full_fix_template(self):
        result = _parse_fix_template(fd(
            fix_type_val="config_change",
            fix_commands="interface {interface}\n no shutdown",
            fix_risk="low",
            fix_confidence="high",
            fix_reason="Restore admin-down interface",
        ))
        parsed = json.loads(result)
        assert parsed["fix_type"] == "config_change"
        assert parsed["risk"] == "low"
        assert parsed["confidence"] == "high"
        assert parsed["reason"] == "Restore admin-down interface"

    def test_multiline_commands_preserved(self):
        commands = "interface {interface}\n no shutdown\n description restored"
        result = _parse_fix_template(fd(fix_commands=commands))
        parsed = json.loads(result)
        assert "\n" in parsed["commands"]
        assert "no shutdown" in parsed["commands"]

    def test_result_is_valid_json(self):
        result = _parse_fix_template(fd(fix_commands="clear ip bgp * soft"))
        json.loads(result)  # should not raise

    def test_whitespace_only_commands_returns_none(self):
        assert _parse_fix_template(fd(fix_commands="   \n  ")) is None
