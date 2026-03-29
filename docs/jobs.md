# Nautobot Jobs

Nautobot Jobs are Python classes that run as Celery tasks. They power all scheduled and on-demand automation in this stack — from device backups to compliance checks to reachability sweeps.

---

## Directory Layout

```
nautobot/scripts/jobs/
├── custom_jobs/                  # All custom job classes
│   ├── __init__.py               # Job registry (imports every class)
│   ├── job_template.py           # Canonical template — copy this to start a new job
│   ├── configuration/            # Config backup, deployment, compliance
│   ├── documentation/            # Doc generation jobs
│   ├── inventory/                # LLDP, ARP, optics inventory
│   ├── monitoring/               # Reachability, interface errors, Prometheus sync
│   ├── onboarding/               # Device onboarding, show version capture
│   ├── operations/               # Command runner, password prober, VLAN provisioning
│   ├── orchestration/            # Change window, mass rollback
│   ├── reporting/                # Serial numbers, EOS alerts, CVE scanner
│   ├── security/                 # SSH audit, AAA compliance, port shutdown
│   ├── syncing/                  # SSoT integration jobs
│   ├── troubleshooting/          # MTU mismatch, packet capture, BGP anomaly
│   └── upgrading/                # Firmware upgrade, readiness check, decommission
├── modules/
│   └── tools.py                  # Shared utilities (see Shared Utilities)
└── backends/
    └── tachyon.py                # Tachyon OS SSH backend
```

---

## Creating a New Job

### Step 1 — Copy the template

```bash
cp nautobot/scripts/jobs/custom_jobs/job_template.py \
   nautobot/scripts/jobs/custom_jobs/<category>/<your_job_name>.py
```

Use the existing category subfolder that best fits the job's purpose (see layout above).

### Step 2 — Edit the file

1. Update the module docstring (`"""Purpose: ...`).
2. Set `name = "Category"` to match the subfolder name.
3. Rename `TemplateJob` → your class name (e.g. `BGPSessionAudit`).
4. Rename `TemplateHelper` to match (e.g. `BGPAuditHelper`).
5. Fill in `class Meta` fields (see [Meta Reference](#meta-reference)).
6. Add or remove form variables.
7. Implement `TemplateHelper.run()` and any private helpers.

### Step 3 — Register the job

Add an import to `custom_jobs/__init__.py` under the matching category block:

```python
# ── Monitoring ────────────────────────────────────────────────────────────────
from .monitoring.bgp_session_audit import BGPSessionAudit
```

Nautobot's job loader discovers jobs via `register_jobs()` at the bottom of the module file **and** via the import in `__init__.py`. Both are required.

### Step 4 — Restart the worker

```bash
docker compose restart nautobot nautobot-worker nautobot-scheduler
```

If startup succeeds, the job appears in **Jobs → Jobs** in the Nautobot UI.

---

## Job File Structure

```
custom_jobs/<category>/<job_name>.py
```

Every job file follows this top-to-bottom order:

| Section | Purpose |
|---|---|
| Module docstring | `"""Purpose: ...` — one-line summary |
| Standard library imports | `os`, `csv`, `json`, `datetime`, … |
| Third-party imports | `netmiko`, `ncclient`, `requests`, … |
| Django imports | `django.conf.settings`, … |
| Nautobot imports | `register_jobs`, `Job`, variable types |
| Custom jobs imports | from `custom_jobs.modules.tools` |
| `name = "Category"` | UI grouping string |
| Module-level constants | `SUPPORTED_PLATFORMS`, per-platform maps |
| Job class | Inherits `Job` + `DeviceFormEntry` |
| Helper class(es) | Per-device business logic |
| `register_jobs(MyJob)` | Registers class with Nautobot |

---

## Anatomy of a Job Class

```python
class MyJob(Job, DeviceFormEntry):
    """Short description for the UI card."""

    # Form variables — inherited from DeviceFormEntry plus job-specific ones
    parallel_task = BooleanVar(description="...", default=False, required=False)
    max_workers   = IntegerVar(description="...", default=10, min_value=1, max_value=20, required=False)

    class Meta:
        name = "Human Readable Name"
        description = "Shown in the UI.  List supported platforms here."
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit      = 2400
        task_queues = [settings.CELERY_TASK_DEFAULT_QUEUE, "priority", "bulk"]

    def run(self, ..., parallel_task=False, max_workers=10):
        all_devices = apply_device_filters(set(), ...)
        ...
```

### Meta Reference

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Display name in the Nautobot UI job list |
| `description` | `str` | Longer description; rendered on the job detail page |
| `has_sensitive_variables` | `bool` | Set `True` if the job accepts passwords/tokens — masks them in logs |
| `soft_time_limit` | `int` | Seconds before Celery raises `SoftTimeLimitExceeded`; job can log a clean error |
| `time_limit` | `int` | Seconds before Celery hard-kills the task |
| `task_queues` | `list[str]` | Which Celery queues accept this job |

Recommended time limits:

| Job type | `soft_time_limit` | `time_limit` |
|---|---|---|
| Quick audits / single device | 300 (5 min) | 600 (10 min) |
| Standard sweep (< 100 devices) | 1800 (30 min) | 2400 (40 min) |
| Large-scale / firmware upgrade | 3600 (60 min) | 4500 (75 min) |

### Form Variable Types

| Class | Use for |
|---|---|
| `BooleanVar` | Toggle / flag |
| `IntegerVar` | Numeric input; supports `min_value` / `max_value` |
| `StringVar` | Free-text single-line input |
| `TextVar` | Free-text multi-line input |
| `ChoiceVar` | Static dropdown; `choices=[("val", "Label"), ...]` |
| `ObjectVar` | Single object picker; requires `model=` |
| `MultiObjectVar` | Multi-object picker; requires `model=` |
| `IPAddressVar` | Validated IP address |
| `IPNetworkVar` | Validated CIDR prefix |
| `FileVar` | File upload |

All `DeviceFormEntry` filter fields (`tenant_group`, `tenant`, `location`, `rack_group`, `rack`, `role`, `manufacturer`, `platform`, `device_type`, `device`, `tags`, `status`) are inherited automatically — no need to declare them unless you want to override defaults.

---

## Device Filtering

`apply_device_filters()` translates the `DeviceFormEntry` form values into a `set` of `Device` objects:

```python
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
# Merge any directly-specified devices:
if device:
    all_devices.update(device)
```

Always guard against an empty set:

```python
if not all_devices:
    self.logger.warning("No devices matched the filter criteria.")
    return
```

---

## Parallel Execution

Jobs that need to process many devices benefit from parallelism. The pattern uses three components from `modules/tools.py`:

| Component | Role |
|---|---|
| `JobLogBuffer` | Collects `debug/info/warning/error/critical` calls from a worker thread |
| `JobProxy` | Wraps `JobLogBuffer` so helper classes can call `self.job.logger.*` normally |
| `parallel_execution()` | Thread pool; drains each buffer to the real logger on the main thread |

### Why the buffer is necessary

Nautobot's job logger writes `JobLogEntry` rows to PostgreSQL. Calling it concurrently from multiple threads causes database connection conflicts and dropped or interleaved log entries. `JobLogBuffer` serialises all DB writes by flushing to the real logger only after each future completes.

### Implementation pattern

```python
def run(self, ..., parallel_task=False, max_workers=10):
    all_devices = ...

    def _run_device(dev):
        """Process one device — must return a JobLogBuffer."""
        buf = JobLogBuffer()
        try:
            if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                buf.info(f"{dev} Platform not supported. Skipping.")
                return buf
            buf.info(f"{dev} Processing...")
            MyHelper(job=JobProxy(buf), device=dev).run()
        except Exception as e:
            buf.error(f"{dev} Error: {e}")
        return buf  # ← MUST return buf

    if parallel_task:
        parallel_execution(
            _run_device,
            all_devices,
            max_workers=max_workers,
            job_logger=self.logger,  # ← main-thread logger
        )
    else:
        for dev in all_devices:
            _run_device(dev).drain_to(self.logger)
```

### Rules

- **Never** call `self.logger` from inside `_run_device` or any method it calls.
- **Always** return `buf` from `_run_device` — even in the error path.
- `parallel_execution` calls `close_old_connections()` per thread automatically; do not call it yourself.
- Use a `threading.Lock` to protect any shared mutable data structures written from worker threads (dicts, lists).

---

## Helper Classes

Move per-device logic into a separate helper class to keep `run()` readable and to enable unit testing without a running Celery worker.

```python
class MyHelper:
    """Per-device work for MyJob.

    Accepts ``job`` (real Job or JobProxy) so logging is always routed through
    self.job.logger — making the class safe to use in both serial and parallel mode.
    """

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def run(self):
        conn_info = get_device_connection_info(self.device)
        if not conn_info:
            self.job.logger.error(f"{self.device} No credentials found.")
            return

        with ConnectHandler(**conn_info) as conn:
            raw = conn.send_command("show version")
            self.job.logger.info(f"{self.device} Got output.")

    def _parse(self, raw):
        """Private helpers stay private — no need to expose them."""
        ...
```

**Conventions:**

- `__init__` takes `job` and `device` as the first two arguments.
- Log only through `self.job.logger` — never through `print()` or the root logger.
- Name private helpers with a leading underscore.
- Keep SSH/NETCONF sessions ephemeral — open and close within the method that needs them.

---

## Shared Utilities (`modules/tools.py`)

| Function / Class | Description |
|---|---|
| `apply_device_filters(**kwargs)` | Returns a `set` of `Device` objects matching the given filters |
| `get_device_connection_info(device)` | Returns a Netmiko-compatible `dict` with `host`, `username`, `password`, `device_type`, etc. |
| `parse_command_output(raw, command, platform)` | Parses CLI text via TextFSM; returns a list of dicts |
| `ping_device(host)` | Returns `True` if the host responds to a single ICMP ping |
| `xml_to_dict(xml_string)` | Converts an XML string to a nested Python dict |
| `diff_files(backup, intended)` | Yields unified diff lines between two config files |
| `parallel_execution(task_func, devices, max_workers, job_logger)` | Thread-pool runner with log-buffer draining |
| `JobLogBuffer` | Thread-safe log collector; use in worker threads |
| `JobProxy` | Routes `self.job.logger` to a `JobLogBuffer` |
| `DeviceFormEntry` | Mixin that adds standard device-filter form fields |

---

## Job Output Files

Use `self.create_file(filename, content)` to attach files to a job run. They appear on the job result page in the Nautobot UI.

```python
import csv, io

rows = [["device", "status", "detail"], ["router1", "ok", "version 17.3"]]
buf = io.StringIO()
writer = csv.writer(buf)
writer.writerows(rows)
self.create_file("report.csv", buf.getvalue())
```

Common output formats:

| Format | When to use |
|---|---|
| `.csv` | Tabular data (inventory, audit results) |
| `.txt` | Human-readable summaries, diff output |
| `.json` | Structured data for downstream consumption |

---

## Platform Support

Declare supported platforms at the module level as `SUPPORTED_PLATFORMS`. The `network_driver` string comes from the Nautobot `Platform` object and maps to Netmiko device types.

```python
SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xr",
    "cisco_xe",
    "cisco_nxos",
    "arista_eos",
    "fortinet",
    "mikrotik_routeros",
]
```

Check at the top of `_run_device()` before opening any SSH connections:

```python
if dev.platform is None or dev.platform.network_driver not in SUPPORTED_PLATFORMS:
    buf.info(f"{dev} Platform not supported. Skipping.")
    return buf
```

---

## Logging Guidelines

| Level | When to use |
|---|---|
| `logger.debug()` | Verbose detail useful for troubleshooting — not shown by default |
| `logger.info()` | Normal progress: "Device X processed successfully" |
| `logger.warning()` | Recoverable issues: skipped device, unexpected but non-fatal state |
| `logger.error()` | Failures that affect a specific device but don't abort the whole job |
| `logger.critical()` | Fatal errors that should stop execution |

Always prefix log messages with the device name: `f"{dev} <message>"`.

---

## Existing Jobs Reference

| Category | Job | Class | Description |
|---|---|---|---|
| Configuration | Backup Device Configurations | `CustomDeviceBackup` | SSH/NETCONF config backup to disk + Nautobot Golden Config |
| Configuration | Intended Configurations | `CustomDeviceIntended` | Render intended configs from Jinja2 templates |
| Configuration | Configuration Compliance | `CustomDeviceCompliance` | Run Golden Config compliance checks |
| Configuration | Deploy Configurations | `DeployConfigurations` | Push rendered configs to devices |
| Configuration | NTP Compliance | `NTPComplianceCheck` | Verify NTP server configuration |
| Configuration | Banner Compliance | `BannerComplianceCheck` | Verify login/MOTD banners |
| Configuration | SNMP Validation | `SNMPValidation` | Verify SNMP community strings and targets |
| Inventory | LLDP Neighbor Discovery | `LLDPNeighborDiscovery` | Discover and sync LLDP neighbors to Nautobot |
| Inventory | ARP/MAC Sync | `ARPMACSync` | Sync ARP and MAC tables to IP address records |
| Inventory | Interface Capacity Audit | `InterfaceCapacityAudit` | Report utilization against capacity thresholds |
| Inventory | Optics Transceiver Inventory | `OpticsTransceiverInventory` | Collect DOM data; export CSV |
| Monitoring | Reachability Sweep | `ReachabilitySweep` | ICMP ping sweep; optionally update device status |
| Monitoring | Interface Error Alerting | `InterfaceErrorAlerting` | Detect interfaces exceeding error thresholds |
| Monitoring | Prometheus Target Sync | `PrometheusTargetSync` | Sync active devices to Prometheus static file targets |
| Onboarding | Onboard Device | `CustomDeviceOnboarding` | Create Device + interfaces from discovered data |
| Onboarding | Get Show Version | `GetShowVersion` | Capture and store software version strings |
| Operations | Command Runner | `CommandRunner` | Run an arbitrary command on a device set |
| Operations | Password Prober | `PasswordProber` | Test credential reachability across devices |
| Reporting | Backup State Checker | `BackupStateChecker` | Alert on devices missing recent backup records |
| Reporting | Hardware EOS Alert | `HardwareEOLAlert` | Cross-reference device types against EOS dates |
| Reporting | CVE Vulnerability Scanner | `CVEVulnerabilityScanner` | Match software versions against CVE database |
| Security | SSH Audit | `SSHAudit` | Verify SSH version, ciphers, and key exchange |
| Security | AAA Compliance | `AAAComplianceCheck` | Verify RADIUS/TACACS configuration |
| Troubleshooting | MTU Mismatch Detector | `MTUMismatchDetector` | Cross-ref MTU + LLDP data to find mismatched links |
| Troubleshooting | BGP Prefix Anomaly | `BGPPrefixAnomalyDetector` | Detect unexpected prefix count changes |
| Upgrading | Firmware Upgrade | `FirmwareUpgrade` | Orchestrate staged firmware upgrade |
| Upgrading | Readiness Check | `ReadinessCheck` | Pre-upgrade health check |
| Upgrading | Device Decommission | `DeviceDecommission` | Remove device records and revoke credentials |

---

## Common Pitfalls

| Mistake | Correct approach |
|---|---|
| Calling `self.logger` from a worker thread | Write to `JobLogBuffer`; drain on the main thread |
| Materialising an unbounded queryset | Use `apply_device_filters()` which returns a `set`; never call `.all()` without a limit |
| Opening SSH inside `__init__` | Open connections inside methods; use `with ConnectHandler(...) as conn:` |
| Forgetting to return `buf` from `_run_device` | All code paths must `return buf` |
| Writing to a shared list/dict from threads | Protect with `threading.Lock()` |
| Not registering in `__init__.py` | Add an import — both `register_jobs()` and the `__init__.py` import are required |
| Setting `time_limit` too low | Long-running SSH commands block; give at least 2× your expected runtime |
