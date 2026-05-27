"""Focused tests for custom Nautobot jobs and shared helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


JOBS_ROOT = Path(__file__).resolve().parents[1] / "nautobot" / "scripts" / "jobs"
if str(JOBS_ROOT) not in sys.path:
    sys.path.insert(0, str(JOBS_ROOT))


def test_removed_duplicate_inventory_jobs_are_gone():
    assert not (JOBS_ROOT / "custom_jobs" / "inventory" / "serial_onboard.py").exists()
    assert not (JOBS_ROOT / "custom_jobs" / "inventory" / "version_onboard.py").exists()


def test_framework_mixin_records_events_and_finalizes():
    framework_path = JOBS_ROOT / "custom_jobs" / "framework.py"
    spec = importlib.util.spec_from_file_location("custom_jobs.framework", framework_path)
    framework = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(framework)

    class DummyLogger:
        def __init__(self):
            self.messages = []

        def info(self, message):
            self.messages.append(("info", message))

        def warning(self, message):
            self.messages.append(("warning", message))

        def error(self, message):
            self.messages.append(("error", message))

        def debug(self, message):
            self.messages.append(("debug", message))

    class DummyJob(framework.FrameworkJobMixin):
        def __init__(self):
            self.logger = DummyLogger()
            self.files = {}

        def create_file(self, name, content):
            self.files[name] = content

    job = DummyJob()
    job.begin_framework_run(inputs={"foo": "bar"})
    job.record_success("dev1", "ok", {"x": 1})
    job.record_failure("dev2", "bad")
    job.record_skipped("dev3", "skip")
    job.finalize_framework_run("dummy_report")

    assert job.framework_inputs == {"foo": "bar"}
    assert len(job.framework_events) == 3
    assert "dummy_report.json" in job.files


def test_new_framework_helpers_import_cleanly():
    discover = (JOBS_ROOT / "custom_jobs" / "inventory" / "discover_device_platform.py").read_text()
    circuits = (JOBS_ROOT / "custom_jobs" / "inventory" / "onboard_circuits.py").read_text()
    subnet = (JOBS_ROOT / "custom_jobs" / "reporting" / "subnet_discovery.py").read_text()
    visualization = (JOBS_ROOT / "custom_jobs" / "monitoring" / "visualization_sync.py").read_text()

    assert "from custom_jobs.framework import FrameworkJobMixin" in discover
    assert "from custom_jobs.framework import FrameworkJobMixin" in circuits
    assert "from custom_jobs.framework import FrameworkJobMixin" in subnet
    assert "from .topology_dashboard_sync import generate_topology_artifacts, DEFAULT_TOPOLOGY_OUTPUT_DIR" in visualization


def test_alert_event_orchestrator_module_exists_and_is_registered():
    orchestrator = (JOBS_ROOT / "custom_jobs" / "monitoring" / "alert_event_orchestrator.py").read_text()
    registry = (JOBS_ROOT / "custom_jobs" / "__init__.py").read_text()

    assert "class AlertEventOrchestrator(Job):" in orchestrator
    assert "register_jobs(AlertEventOrchestrator)" in orchestrator
    assert "queue_pending_intents" in orchestrator
    assert "JobResultStatusChoices.STATUS_PENDING" in orchestrator
    assert "JobResult.objects.create(" in orchestrator
    assert "from .monitoring.alert_event_orchestrator import AlertEventOrchestrator" in registry
    assert '"AlertEventOrchestrator"' in registry


def test_alert_intent_executor_module_exists_and_is_registered():
    executor = (JOBS_ROOT / "custom_jobs" / "monitoring" / "alert_intent_executor.py").read_text()
    registry = (JOBS_ROOT / "custom_jobs" / "__init__.py").read_text()

    assert "class AlertIntentExecutor(Job):" in executor
    assert "register_jobs(AlertIntentExecutor)" in executor
    assert "execute_check_mode" in executor
    assert "JobResultStatusChoices.STATUS_PENDING" in executor
    assert "from .monitoring.alert_intent_executor import AlertIntentExecutor" in registry
    assert '"AlertIntentExecutor"' in registry


def test_monitoring_visualization_sync_is_registered_in_main_jobs_registry():
    registry = (JOBS_ROOT / "custom_jobs" / "__init__.py").read_text()

    assert "from .monitoring.visualization_sync import MonitoringVisualizationSync" in registry
    assert '"MonitoringVisualizationSync"' in registry
