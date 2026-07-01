# Nautobot: Data Loader and Jobs

---

## Data Loader

Declarative YAML-driven reconciliation of Nautobot Source-of-Truth objects. Supports full CRUD — create, update, no-op detection, and managed-scope deletion.

**Source:** `nautobot/data_loader/data.yml`  
**Script:** `nautobot/data_loader/load_data.py`

**Object coverage:** location types · locations · roles · manufacturers · device types · platforms · namespaces · prefixes · VLANs · config contexts · custom fields · secrets/secret groups · devices · interfaces · interface IPs · cables

### Make targets

```bash
make apply-data    # full CRUD reconciliation (primary command)
make plan-data     # dry-run — preview what will change, no mutations
make lint-data     # validate YAML syntax
```

### Workflow

1. Edit `nautobot/data_loader/data.yml` with desired state changes.
2. `make plan-data` — review the action summary (`create` / `update` / `noop` / `delete`).
3. `make apply-data` — apply changes.
4. `make plan-data` again — verify clean no-op.

State is tracked in `/tmp/nautobot-data-loader.state.json` (configurable via `--state-file`). Apply deletes are scoped to managed objects only — objects not in `data.yml` are not deleted unless previously managed by the loader.

---

## Nautobot Jobs

Python classes that run as Celery tasks. They power all scheduled and on-demand automation — config backups, compliance checks, reachability sweeps, and the agent's action tools (`CommandRunner`, `DeployConfigurations`).

### Directory layout

```
nautobot/scripts/jobs/
├── custom_jobs/
│   ├── __init__.py          # Job registry (imports every class)
│   ├── job_template.py      # Copy this to start a new job
│   ├── configuration/       # Backup, deploy, compliance
│   ├── inventory/           # LLDP, ARP, optics
│   ├── monitoring/          # Reachability, Prometheus sync
│   ├── onboarding/          # Device onboarding
│   ├── operations/          # Command runner, VLAN provisioning
│   ├── reporting/           # EOS alerts, CVE scanner
│   ├── security/            # SSH audit, AAA compliance
│   └── troubleshooting/     # MTU mismatch, BGP anomaly
├── modules/tools.py         # Shared utilities
└── backends/tachyon.py      # Tachyon OS SSH backend
```

### Creating a new job

```bash
cp nautobot/scripts/jobs/custom_jobs/job_template.py \
   nautobot/scripts/jobs/custom_jobs/<category>/<your_job>.py
```

1. Update module docstring; set `name = "Category"`.
2. Rename `TemplateJob` and `TemplateHelper` to your class names.
3. Fill in `class Meta` fields (see below).
4. Register in `custom_jobs/__init__.py`: `from .category.your_job import YourJob`
5. Refresh Nautobot: `docker compose exec nautobot nautobot-server post_upgrade && docker compose restart nautobot nautobot-worker nautobot-scheduler`
6. In the Nautobot UI: **Jobs → Jobs** → Edit the new job record → check **Enabled**.

### Job class anatomy

```python
class MyJob(Job, DeviceFormEntry):
    """Short description shown in the UI."""
    parallel_task = BooleanVar(description="...", default=False, required=False)
    max_workers   = IntegerVar(description="...", default=10, min_value=1, max_value=20, required=False)

    class Meta:
        name = "Human Readable Name"
        description = "Shown in the UI. List supported platforms here."
        has_sensitive_variables = False
        soft_time_limit = 1800   # seconds before clean error
        time_limit      = 2400   # seconds before hard kill
        task_queues = [settings.CELERY_TASK_DEFAULT_QUEUE, "priority", "bulk"]

    def run(self, ..., parallel_task=False, max_workers=10):
        all_devices = apply_device_filters(set(), ...)
        if not all_devices:
            self.logger.warning("No devices matched.")
            return
```

**`Meta` time limits by job type:**

| Job type | `soft_time_limit` | `time_limit` |
|---|---|---|
| Quick audits / single device | 300 s | 600 s |
| Standard sweep (< 100 devices) | 1800 s | 2400 s |
| Large-scale / firmware upgrade | 3600 s | 4500 s |

**Form variable types:** `BooleanVar`, `IntegerVar`, `StringVar`, `TextVar`, `ChoiceVar`, `ObjectVar`, `MultiObjectVar`, `IPAddressVar`, `IPNetworkVar`, `FileVar`. All `DeviceFormEntry` filter fields are inherited automatically.

### Parallel execution

```python
from modules.tools import JobLogBuffer, JobProxy, parallel_execution

def run(self, ..., parallel_task=False, max_workers=10):
    all_devices = apply_device_filters(set(), ...)

    def _run_device(dev):
        buf = JobLogBuffer()
        try:
            if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                buf.info(f"{dev} Platform not supported. Skipping.")
                return buf
            MyHelper(job=JobProxy(buf), device=dev).run()
        except Exception as e:
            buf.error(f"{dev} Error: {e}")
        return buf   # MUST return buf in all paths

    if parallel_task:
        parallel_execution(_run_device, all_devices, max_workers=max_workers, job_logger=self.logger)
    else:
        for dev in all_devices:
            _run_device(dev).drain_to(self.logger)
```

**Rules:** Never call `self.logger` from inside `_run_device`. Always return `buf`. Use `threading.Lock` for any shared mutable state written from worker threads.

### Shared utilities (`modules/tools.py`)

| Function / Class | Description |
|---|---|
| `apply_device_filters(**kwargs)` | Returns `set[Device]` matching the given filters |
| `get_device_connection_info(device)` | Netmiko-compatible dict (host, username, password, device_type) |
| `parse_command_output(output, template_file)` | TextFSM parse → list of dicts |
| `parallel_execution(func, devices, max_workers, job_logger)` | Thread-pool with log-buffer draining |
| `JobLogBuffer` | Thread-safe log collector for worker threads |
| `JobProxy` | Routes `self.job.logger` to a `JobLogBuffer` |
| `DeviceFormEntry` | Mixin that adds standard device-filter form fields |

### Existing jobs reference

| Category | Job | Class | Description |
|---|---|---|---|
| Configuration | Backup Device Configurations | `CustomDeviceBackup` | SSH/NETCONF backup → Nautobot Golden Config |
| Configuration | Deploy Configurations | `DeployConfigurations` | Push rendered configs to devices (used by agent) |
| Configuration | Configuration Compliance | `CustomDeviceCompliance` | Golden Config compliance checks |
| Configuration | NTP / Banner / SNMP Compliance | `NTPComplianceCheck`, `BannerComplianceCheck`, `SNMPValidation` | Policy compliance checks |
| Inventory | LLDP Neighbor Discovery | `LLDPNeighborDiscovery` | Discover and sync LLDP neighbors |
| Inventory | ARP/MAC Sync | `ARPMACSync` | Sync ARP and MAC tables to IP address records |
| Inventory | Optics Transceiver Inventory | `OpticsTransceiverInventory` | Collect DOM data; export CSV |
| Monitoring | Reachability Sweep | `ReachabilitySweep` | ICMP ping sweep; optionally update device status |
| Monitoring | Alert Event Orchestrator | `AlertEventOrchestrator` | Convert alert events into remediation proposals |
| Monitoring | Prometheus Target Sync | `PrometheusTargetSync` | Sync active devices to Prometheus static targets |
| Operations | Command Runner | `CommandRunner` | Run arbitrary commands (used by agent) |
| Onboarding | Onboard Device | `CustomDeviceOnboarding` | Create device + interfaces from discovered data |
| Reporting | Hardware EOS Alert | `HardwareEOLAlert` | Cross-reference device types against EOS dates |
| Reporting | CVE Vulnerability Scanner | `CVEVulnerabilityScanner` | Match software versions against CVE database |
| Security | SSH Audit | `SSHAudit` | Verify SSH version, ciphers, key exchange |
| Troubleshooting | MTU Mismatch Detector | `MTUMismatchDetector` | Cross-ref MTU + LLDP to find mismatched links |
| Upgrading | Firmware Upgrade | `FirmwareUpgrade` | Orchestrate staged firmware upgrade |

### Common pitfalls

| Mistake | Correct approach |
|---|---|
| Calling `self.logger` from a worker thread | Write to `JobLogBuffer`; drain on main thread |
| Materialising an unbounded queryset | Use `apply_device_filters()` — never `.all()` without a limit |
| Opening SSH inside `__init__` | Open connections inside methods; use `with ConnectHandler(...):` |
| Forgetting to return `buf` from `_run_device` | All code paths must `return buf` |
| Not registering in `__init__.py` | Both `register_jobs()` and the `__init__.py` import are required |
