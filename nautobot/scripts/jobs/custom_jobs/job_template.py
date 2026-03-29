"""Purpose: One-line description of what this job does.

This file is the canonical template for all Nautobot Jobs in this repository.
Copy it to the appropriate category subfolder and rename the class.

See docs/jobs.md for the full authoring guide.
"""

# ── Standard Library ──────────────────────────────────────────────────────────
import csv
import json
import os
from datetime import datetime

# ── Third-party ───────────────────────────────────────────────────────────────
# from netmiko import ConnectHandler       # SSH via Netmiko
# from ncclient import manager             # NETCONF
# from requests import Session             # REST APIs

# ── Django ────────────────────────────────────────────────────────────────────
from django.conf import settings

# ── Nautobot ──────────────────────────────────────────────────────────────────
from nautobot.apps.jobs import (
    register_jobs,
    Job,
    BooleanVar,
    IntegerVar,
    StringVar,
    # ObjectVar,        # Single-object picker — supply model= and optional query_params=
    # MultiObjectVar,   # Multi-object picker
    # ChoiceVar,        # Static dropdown: choices=[("val", "Label"), ...]
    # TextVar,          # Free-form multi-line text input
    # FileVar,          # File upload
    # IPAddressVar,     # IP address input (validated)
    # IPNetworkVar,     # IP network (CIDR) input (validated)
)

# ── Custom Jobs ───────────────────────────────────────────────────────────────
from custom_jobs.modules.tools import (
    apply_device_filters,       # Build a device queryset from DeviceFormEntry kwargs
    get_device_connection_info, # Return Netmiko-compatible connection dict for a device
    parse_command_output,       # Parse CLI output via TextFSM
    DeviceFormEntry,            # Mixin: adds standard device-filter form fields
    parallel_execution,         # Thread-pool helper with thread-safe log draining
    JobLogBuffer,               # Captures log calls from a worker thread
    JobProxy,                   # Routes self.job.logger → a JobLogBuffer
)

# ── Module grouping name (shown in the Nautobot UI job list) ──────────────────
# Must match the category subfolder name. Valid values:
#   "Configuration" | "Inventory" | "Monitoring" | "Onboarding" | "Operations"
#   "Orchestration" | "Reporting" | "Security" | "Syncing" | "Troubleshooting"
#   "Upgrading"
name = "Category"


# ── Module-level constants ─────────────────────────────────────────────────────
# List every network_driver string this job supports.  Unsupported platforms
# are skipped inside _run_device() before any SSH connection is attempted.
SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xr",
    "cisco_xe",
    "cisco_nxos",
    "arista_eos",
    # Add more as needed.
]


# ── Job class ─────────────────────────────────────────────────────────────────
class TemplateJob(Job, DeviceFormEntry):
    """One-line job summary displayed in the Nautobot UI job list card."""

    # ── Form variables ────────────────────────────────────────────────────────
    # All DeviceFormEntry filter fields are inherited automatically.
    # Declare only the job-specific inputs below.

    parallel_task = BooleanVar(
        description="Execute tasks in parallel across devices",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Maximum number of concurrent worker threads (parallel mode only)",
        default=10,
        min_value=1,
        max_value=20,
        required=False,
    )
    # example_string = StringVar(
    #     description="A free-text input",
    #     default="",
    #     required=False,
    # )

    class Meta:
        name = "Human Readable Job Name"
        description = (
            "Longer description shown in the UI. "
            f"Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        #
        # Time limits — Celery raises SoftTimeLimitExceeded at soft_time_limit
        # so the job can log a clean error before the hard kill at time_limit.
        soft_time_limit = 1800   # 30 minutes
        time_limit      = 2400   # 40 minutes
        #
        # task_queues controls which Celery work queues accept this job.
        # Include "priority" for urgent/short jobs, "bulk" for long-running sweeps.
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(
        self,
        # ── DeviceFormEntry filter kwargs ── keep only the fields you need ──
        tenant_group=None,
        tenant=None,
        location=None,
        rack_group=None,
        rack=None,
        role=None,
        manufacturer=None,
        platform=None,
        device_type=None,
        device=None,
        tags=None,
        status=None,
        # ── Job-specific kwargs ───────────────────────────────────────────────
        parallel_task=False,
        max_workers=10,
        # example_string="",
    ):
        # ── 1. Build device set ───────────────────────────────────────────────
        all_devices = apply_device_filters(
            set(),
            tenant_group=tenant_group,
            tenant=tenant,
            location=location,
            rack_group=rack_group,
            rack=rack,
            role=role,
            manufacturer=manufacturer,
            platform=platform,
            device_type=device_type,
            tags=tags,
            status=status,
        )
        if device:
            all_devices.update(device)

        if not all_devices:
            self.logger.warning("No devices matched the filter criteria.")
            return

        self.logger.info(f"Processing {len(all_devices)} device(s).")

        # ── 2. Per-device task ────────────────────────────────────────────────
        def _run_device(dev):
            """Process one device. Returns a JobLogBuffer for thread-safe logging.

            IMPORTANT: Do NOT call self.logger from inside this closure when
            parallel_task=True.  Write to ``buf`` instead; parallel_execution
            drains it to self.logger on the main thread once the future settles.
            """
            buf = JobLogBuffer()
            try:
                if dev.platform is None or dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    buf.info(f"{dev} Platform not supported. Skipping.")
                    return buf
                buf.info(f"{dev} Processing...")
                TemplateHelper(job=JobProxy(buf), device=dev).run()
            except Exception as e:
                buf.error(f"{dev} Unhandled error: {e}")
            return buf

        # ── 3. Execute ────────────────────────────────────────────────────────
        if parallel_task:
            parallel_execution(
                _run_device,
                all_devices,
                max_workers=max_workers,
                job_logger=self.logger,
            )
        else:
            for dev in all_devices:
                _run_device(dev).drain_to(self.logger)

        # ── 4. Optional: produce a job output file ────────────────────────────
        # results = [["device", "result"], ["router1", "ok"]]
        # output = io.StringIO()
        # csv.writer(output).writerows(results)
        # self.create_file("report.csv", output.getvalue())


# ── Helper class ──────────────────────────────────────────────────────────────
class TemplateHelper:
    """Performs the per-device work for TemplateJob.

    Accepts a ``job`` argument — either the real Job object or a JobProxy — so
    that all logging goes through ``self.job.logger`` and is thread-safe when
    called from worker threads via the JobLogBuffer pattern.

    Keep business logic here; keep Nautobot model reads/writes minimal and
    always call close_old_connections() at the top of any thread entry point
    (handled automatically by parallel_execution).
    """

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def run(self):
        """Entry point called by TemplateJob._run_device().

        Replace this method (and add private helpers as needed) with the
        actual work for your job.
        """
        conn_info = get_device_connection_info(self.device)
        if not conn_info:
            self.job.logger.error(f"{self.device} No connection credentials found.")
            return

        # Example: open an SSH session and run a command.
        # with ConnectHandler(**conn_info) as conn:
        #     raw = conn.send_command("show version")
        #     parsed = parse_command_output(raw, "show_version", self.device.platform.network_driver)
        #     self.job.logger.info(f"{self.device} Version: {parsed[0].get('version', 'unknown')}")

        self.job.logger.info(f"{self.device} TemplateHelper.run() — replace with real logic.")

    # ── Private helpers ───────────────────────────────────────────────────────
    # def _parse_output(self, raw: str) -> dict:
    #     """Parse raw CLI output and return a structured dict."""
    #     ...


# ── Registration ──────────────────────────────────────────────────────────────
# register_jobs() makes the class visible to Nautobot's job registry.
# Also add an import in custom_jobs/__init__.py:
#   from .category.your_job_file import YourJobClass
register_jobs(TemplateJob)
