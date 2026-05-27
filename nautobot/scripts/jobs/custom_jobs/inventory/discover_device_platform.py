"""
Purpose:
Auto-discover the platform of devices already in Nautobot (e.g. imported from SolarWinds)
using a 4-stage probe pipeline:

  Stage 1 — ICMP ping          : skip unreachable devices immediately.
  Stage 2 — SSH banner grab    : unauthenticated socket read; keyword-match against
                                  BANNER_PLATFORM_MAP. No credentials required.
  Stage 3 — SNMP v2c sysDescr  : puresnmp GET of OID 1.3.6.1.2.1.1.1.0; keyword-match
                                  against the same map. No SSH credentials required.
  Stage 4 — Netmiko SSHDetect  : authenticated autodetect. Only attempted when stages 2
                                  and 3 both fail and a SecretsGroup credential is provided.

Once identified, the detected network_driver is matched to an existing Nautobot Platform
record (by network_driver field) and written to the device. Unknown drivers (no matching
Platform record) are flagged as failures without creating new Platform records.

Optionally, after the platform is set, the standard data-capture pipeline
(interfaces, VLANs, serial, version, LLDP, ARP/MAC, transceivers) can be triggered
automatically — mirroring what CustomDeviceOnboarding does for freshly onboarded devices.
"""

import shutil
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.db import close_old_connections
from netmiko import ConnectHandler, SSHDetect

from nautobot.apps.jobs import (
    BooleanVar,
    IntegerVar,
    Job,
    ObjectVar,
    StringVar,
    register_jobs,
)
from nautobot.dcim.models import Device, Platform
from nautobot.extras.models.secrets import (
    SecretsGroup,
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)

from custom_jobs.framework import FrameworkJobMixin
from custom_jobs.modules.tools import (
    DeviceFormEntry,
    JobLogBuffer,
    apply_device_filters,
    get_device_connection_info,
)

name = "Inventory"

# ---------------------------------------------------------------------------
# Platform keyword map
# SSH banner text or SNMP sysDescr substring → Nautobot Platform network_driver.
# Listed longest / most-specific first so the first match wins.
# ---------------------------------------------------------------------------
BANNER_PLATFORM_MAP: Dict[str, str] = {
    "Cisco IOS XR":  "cisco_xr",
    "NX-OS":         "cisco_nxos",
    "IOS-XE":        "cisco_xe",
    "Cisco IOS":     "cisco_ios",
    "Arista":        "arista_eos",
    "EOS":           "arista_eos",
    "RouterOS":      "mikrotik_routeros",
    "Mikrotik":      "mikrotik_routeros",
    "ROSSSH":        "mikrotik_routeros",   # MikroTik RouterOS SSH daemon banner (SSH-2.0-ROSSSH)
    "FSOS":          "fiberstore_fsos",
    "FS.com":        "fiberstore_fsos",
    "FortiOS":       "fortinet",
    "FortiGate":     "fortinet",
    "Keymile":       "keymile_nos",
    "Cambium":       "cambium_cnmatrix",
    "Netonix":       "netonix_os",
    "Ceragon":       "ceragon_os",
    "Siklu":         "siklu_os",
    "AirOS":         "ubiquiti_airos",
    "EdgeRouter":    "ubiquiti_edge",
    "EdgeSwitch":    "ubiquiti_edgeswitch",
    "JunOS":         "juniper_junos",
    "Juniper":       "juniper_junos",
    "VRP":           "huawei_vrp",
    "Huawei":        "huawei_vrp",
    "PAN-OS":        "paloalto_panos",
    "Palo Alto":     "paloalto_panos",
}

# Stage 2 constants
SSH_BANNER_READ_BYTES = 512
SSH_BANNER_TIMEOUT = 3  # seconds

# Stage 3 SNMP constants
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SNMP_PORT = 161
SNMP_TIMEOUT = 3  # seconds


# ---------------------------------------------------------------------------
# Probe helper functions
# Each function is stateless and safe to call from worker threads.
# ---------------------------------------------------------------------------

def _ping(ip: str, count: int) -> bool:
    """Return True if the host responds to ICMP echo requests."""
    ping_bin = shutil.which("ping") or "/bin/ping"
    try:
        result = subprocess.run(
            [ping_bin, "-c", str(count), "-W", "2", ip],
            capture_output=True,
            timeout=count * 3,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _detect_platform(text: str) -> str:
    """Return the first network_driver whose keyword appears in text, or ''."""
    for keyword, driver in BANNER_PLATFORM_MAP.items():
        if keyword.lower() in text.lower():
            return driver
    return ""


def _probe_ssh_banner(ip: str, port: int) -> Tuple[bool, str, str]:
    """
    Open a raw TCP connection to port and read the SSH banner (no authentication).
    Returns (port_open, banner_text, detected_driver).
    """
    try:
        with socket.create_connection((ip, port), timeout=SSH_BANNER_TIMEOUT) as sock:
            raw = sock.recv(SSH_BANNER_READ_BYTES)
        banner = raw.decode("utf-8", errors="replace").strip()
        return True, banner, _detect_platform(banner)
    except (OSError, socket.timeout):
        return False, "", ""


def _probe_snmp(ip: str, communities: List[str]) -> Tuple[bool, str, str, str]:
    """
    Try each SNMP v2c community string and GET sysDescr.
    Returns (responded, community, sys_descr, detected_driver).
    puresnmp is installed in the Nautobot container (see Dockerfile).
    """
    try:
        import puresnmp
    except ImportError:
        return False, "", "", ""

    for community in communities:
        try:
            raw = puresnmp.get(ip, community, OID_SYS_DESCR, port=SNMP_PORT, timeout=SNMP_TIMEOUT)
            sys_descr = str(raw)
            return True, community, sys_descr, _detect_platform(sys_descr)
        except Exception:
            continue
    return False, "", "", ""


def _ssh_autodetect(ip: str, credential: SecretsGroup, buf: JobLogBuffer) -> str:
    """
    Use Netmiko SSHDetect to autodetect the platform. Requires valid credentials.
    Returns a network_driver string (e.g. 'cisco_ios') or '' on failure.
    Logs into buf so it can be replayed safely on the main thread.
    """
    try:
        username = credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_USERNAME,
        )
        password = credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
        )
    except Exception as exc:
        buf.warning(f"SSHDetect skipped for {ip}: could not read credentials from SecretsGroup ({exc}).")
        return ""

    if not username or not password:
        buf.warning(f"SSHDetect skipped for {ip}: SecretsGroup is missing username or password.")
        return ""

    try:
        device_info = {
            "device_type": "autodetect",
            "host": ip,
            "username": username,
            "password": password,
            "timeout": 15,
            "banner_timeout": 15,
            "auth_timeout": 15,
            # Compatibility with older SSH implementations
            "disabled_algorithms": {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]},
            "allow_agent": False,
            "ssh_strict": False,
        }
        guesser = SSHDetect(**device_info)
        best = guesser.autodetect()
        # 'generic' means SSHDetect gave up — treat as no result
        return best if (best and best != "generic") else ""
    except Exception as exc:
        buf.warning(f"SSHDetect error for {ip}: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Per-device discovery orchestrator
# ---------------------------------------------------------------------------

def _discover_platform(
    device: Device,
    credential: Optional[SecretsGroup],
    snmp_communities: List[str],
    ssh_port: int,
    ping_count: int,
) -> dict:
    """
    Run the 4-stage discovery pipeline against a single device.
    Returns a result dict consumed by DiscoverDevicePlatform._apply_result().
    Thread-safe: uses JobLogBuffer for logging, no direct DB writes.

    Result keys:
        device    — the Device ORM instance
        buf       — JobLogBuffer (drain to self.logger on main thread)
        driver    — detected network_driver string, or '' if not found
        stage     — probe stage that found the match, or ''
        reachable — bool: True if ICMP succeeded
        no_ip     — bool: True if device has no primary IP
    """
    buf = JobLogBuffer()

    ip = device.primary_ip4.host if device.primary_ip4 else None
    if not ip:
        buf.warning(f"{device} has no primary IP — skipping discovery.")
        return {"device": device, "buf": buf, "driver": "", "stage": "", "reachable": False, "no_ip": True}

    # ------------------------------------------------------------------
    # Build effective probe parameters from device-level config first,
    # then fall back to job-level values.
    # ------------------------------------------------------------------
    # SNMP: device custom field 'snmpcommunity' takes priority over the
    # job-level community strings.
    device_cf_community = ((device.custom_field_data or {}).get("snmpcommunity") or "").strip()
    if device_cf_community:
        effective_communities = [device_cf_community] + [
            c for c in snmp_communities if c != device_cf_community
        ]
        if device_cf_community not in snmp_communities:
            buf.info(
                f"{device} Using device SNMP community from custom field as primary "
                f"(job-level communities appended as fallback)."
            )
    else:
        effective_communities = snmp_communities

    # SSH credentials: device.secrets_group takes priority over the job-level credential.
    # Both are attempted in order so Stage 4 can fall back automatically.
    device_secrets_group = device.secrets_group  # already select_related
    effective_credentials: list = []
    if device_secrets_group:
        effective_credentials.append(device_secrets_group)
        if device_secrets_group != credential:
            buf.info(
                f"{device} Using device SecretsGroup '{device_secrets_group}' as primary "
                f"SSH credential (job-level credential appended as fallback)."
            )
    if credential and (not device_secrets_group or credential.pk != device_secrets_group.pk):
        effective_credentials.append(credential)

    # ------------------------------------------------------------------
    # Stage 1 — ICMP ping
    # ------------------------------------------------------------------
    if not _ping(ip, ping_count):
        buf.warning(f"{device} ({ip}) unreachable via ICMP — skipping all probes.")
        return {"device": device, "buf": buf, "driver": "", "stage": "", "reachable": False, "no_ip": False}

    buf.info(f"{device} ({ip}) ICMP reachable.")

    # ------------------------------------------------------------------
    # Stage 2 — SSH banner grab (unauthenticated)
    # ------------------------------------------------------------------
    ssh_open, banner, driver = _probe_ssh_banner(ip, ssh_port)
    if driver:
        buf.info(
            f"{device} ({ip}) Platform detected via SSH banner: '{driver}' "
            f"(banner excerpt: {banner[:80]!r})"
        )
        return {"device": device, "buf": buf, "driver": driver, "stage": "ssh_banner", "reachable": True, "no_ip": False}
    elif ssh_open:
        buf.info(
            f"{device} ({ip}) SSH port {ssh_port} open but banner did not match any known platform "
            f"(banner excerpt: {banner[:80]!r})."
        )
    else:
        buf.info(f"{device} ({ip}) SSH port {ssh_port} closed or timed out — no banner available.")

    # ------------------------------------------------------------------
    # Stage 3 — SNMP v2c sysDescr
    # ------------------------------------------------------------------
    if effective_communities:
        snmp_ok, community, sys_descr, driver = _probe_snmp(ip, effective_communities)
        if driver:
            buf.info(
                f"{device} ({ip}) Platform detected via SNMP sysDescr: '{driver}' "
                f"(community: {community!r}, descr excerpt: {sys_descr[:80]!r})"
            )
            return {"device": device, "buf": buf, "driver": driver, "stage": "snmp", "reachable": True, "no_ip": False}
        elif snmp_ok:
            buf.info(
                f"{device} ({ip}) SNMP responded (community: {community!r}) but sysDescr did not "
                f"match any known platform (descr: {sys_descr[:80]!r})."
            )
        else:
            buf.info(f"{device} ({ip}) SNMP: no response from any supplied community string.")
    else:
        buf.info(f"{device} ({ip}) SNMP probing disabled (no community strings provided).")


    # ------------------------------------------------------------------
    # Stage 4 — Netmiko SSHDetect (authenticated autodetect)
    # Tries device SecretsGroup first, then job-level credential.
    # ------------------------------------------------------------------
    if effective_credentials:
        buf.info(f"{device} ({ip}) Attempting Netmiko SSHDetect autodetect (Stage 4)...")
        for cred in effective_credentials:
            driver = _ssh_autodetect(ip, cred, buf)
            if driver:
                buf.info(f"{device} ({ip}) Platform detected via Netmiko SSHDetect: '{driver}'")
                return {"device": device, "buf": buf, "driver": driver, "stage": "ssh_autodetect", "reachable": True, "no_ip": False}
        buf.warning(f"{device} ({ip}) Netmiko SSHDetect could not determine platform (tried {len(effective_credentials)} credential(s)).")
    else:
        buf.info(f"{device} ({ip}) No credential available — Netmiko SSHDetect (Stage 4) skipped.")

    buf.warning(
        f"{device} ({ip}) Platform could not be determined by any probe method "
        f"(SSH banner, SNMP, SSHDetect all failed or returned no match)."
    )
    return {"device": device, "buf": buf, "driver": "", "stage": "", "reachable": True, "no_ip": False}


# ---------------------------------------------------------------------------
# Job class
# ---------------------------------------------------------------------------

class DiscoverDevicePlatform(FrameworkJobMixin, Job, DeviceFormEntry):
    """
    Auto-discover the platform of devices already in Nautobot.

    Designed for use after importing devices from external sources (e.g. SolarWinds)
    where platform data may be missing or cannot be trusted. The job probes each
    targeted device through a 4-stage pipeline:

      1. ICMP ping          — skip unreachable devices
      2. SSH banner grab    — unauthenticated; no credentials needed
      3. SNMP v2c sysDescr  — community strings configurable below
      4. Netmiko SSHDetect  — authenticated; only runs if stages 2+3 fail

    Once a platform is identified it is matched to an existing Nautobot Platform
    record (by network_driver) and written to the device. Unknown drivers produce
    a failure record — no new Platform records are created automatically.

    After the platform is set, optionally trigger the full device data-capture
    pipeline (interfaces, VLANs, serial, software version, LLDP neighbors,
    ARP/MAC table, transceivers) — the same pipeline run by CustomDeviceOnboarding.
    """

    # ------------------------------------------------------------------
    # Job-specific form fields
    # ------------------------------------------------------------------
    credential = ObjectVar(
        model=SecretsGroup,
        label="SSH Credentials (SecretsGroup)",
        description=(
            "Required for Stage 4 (Netmiko SSHDetect). "
            "Also assigned as device.secrets_group when 'Run Data Capture After' is enabled."
        ),
        required=False,
    )
    snmp_communities = StringVar(
        label="SNMP Community Strings",
        description=(
            "Comma-separated SNMP v2c community strings for Stage 3 "
            "(e.g. public,private,mycommunity). Leave blank to skip SNMP probing."
        ),
        default="public",
        required=False,
    )
    ssh_port = IntegerVar(
        label="SSH Port",
        description="TCP port used for SSH banner grab (Stage 2) and SSHDetect (Stage 4).",
        default=22,
        min_value=1,
        max_value=65535,
        required=False,
    )
    ping_count = IntegerVar(
        label="Ping Count",
        description="Number of ICMP echo requests per device (Stage 1).",
        default=2,
        min_value=1,
        max_value=10,
        required=False,
    )
    limit_to_no_platform = BooleanVar(
        label="Limit to Devices Without a Platform",
        description=(
            "When enabled, only process devices that have no platform assigned. "
            "Recommended when targeting devices freshly imported from SolarWinds."
        ),
        default=True,
        required=False,
    )
    dry_run = BooleanVar(
        label="Dry Run (Report Only)",
        description=(
            "Probe devices and report what platform would be set, "
            "without writing any changes to Nautobot."
        ),
        default=False,
        required=False,
    )
    run_capture_after = BooleanVar(
        label="Run Data Capture After Discovery",
        description=(
            "After writing the platform, connect to the device and capture interfaces, "
            "VLANs, serial number, software version, LLDP neighbors, and ARP/MAC table. "
            "A credential (SecretsGroup) must be selected for this to work."
        ),
        default=False,
        required=False,
    )
    parallel_task = BooleanVar(
        label="Run in Parallel",
        description="Probe multiple devices concurrently.",
        default=True,
        required=False,
    )
    max_workers = IntegerVar(
        label="Max Parallel Workers",
        description="Maximum number of concurrent device probes.",
        default=10,
        min_value=1,
        max_value=50,
        required=False,
    )

    class Meta:
        name = "Discover Device Platform"
        description = (
            "Auto-discover device platforms by probing live devices via ICMP, SSH banner, "
            "SNMP sysDescr, and Netmiko SSHDetect. Intended for devices imported from "
            "external sources (e.g. SolarWinds) where platform data is missing or unreliable. "
            "Identified platforms are written back to Nautobot. Optionally triggers full "
            "data capture (interfaces, serial, version, LLDP, ARP/MAC)."
        )
        has_sensitive_variables = False
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400       # 40 minutes
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(self, **kwargs):
        # Extract our job-specific fields from kwargs so the remainder
        # can be passed directly to apply_device_filters().
        credential         = kwargs.pop("credential", None)
        snmp_communities   = kwargs.pop("snmp_communities", "public")
        ssh_port           = kwargs.pop("ssh_port", 22)
        ping_count         = kwargs.pop("ping_count", 2)
        limit_no_platform  = kwargs.pop("limit_to_no_platform", True)
        dry_run            = kwargs.pop("dry_run", False)
        run_capture_after  = kwargs.pop("run_capture_after", False)
        parallel_task      = kwargs.pop("parallel_task", True)
        max_workers        = kwargs.pop("max_workers", 10)

        self.begin_framework_run(inputs={
            "credential": str(credential) if credential else None,
            "snmp_communities": snmp_communities,
            "ssh_port": ssh_port,
            "ping_count": ping_count,
            "limit_to_no_platform": limit_no_platform,
            "dry_run": dry_run,
            "run_capture_after": run_capture_after,
            "parallel_task": parallel_task,
            "max_workers": max_workers,
        })

        # When limiting to devices with no platform the DeviceFormEntry 'platform'
        # filter must be ignored — the two options are mutually exclusive.
        if limit_no_platform:
            kwargs.pop("platform", None)

        all_devices = apply_device_filters(set(), **kwargs)
        if not all_devices:
            self.logger.warning("No devices matched the selected filters.")
            self.record_event(level="warning", message="No devices matched filters", context={})
            self.finalize_framework_run(filename_prefix="discover_platform_report")
            return

        if limit_no_platform:
            all_devices = [device for device in all_devices if device.platform is None]

        device_list = list(all_devices)
        if not device_list:
            self.logger.warning(
                "No devices to process after applying filters "
                "(all matched devices may already have a platform assigned)."
            )
            self.record_event(level="warning", message="No devices after filtering", context={})
            self.finalize_framework_run(filename_prefix="discover_platform_report")
            return

        communities = (
            [c.strip() for c in snmp_communities.split(",") if c.strip()]
            if snmp_communities
            else []
        )

        self.logger.info(
            f"Starting platform discovery on {len(device_list)} device(s). "
            f"dry_run={dry_run}, parallel={parallel_task}, workers={max_workers}, "
            f"snmp_communities={communities or 'disabled'}, ssh_port={ssh_port}"
        )

        # ------------------------------------------------------------------
        # Probe phase — all network I/O; no DB writes
        # Worker threads populate `results`; main thread does ORM updates.
        # ------------------------------------------------------------------
        results: list = []
        results_lock = threading.Lock()

        def _probe(device: Device) -> JobLogBuffer:
            r = _discover_platform(device, credential, communities, ssh_port, ping_count)
            with results_lock:
                results.append(r)
            return r["buf"]

        if parallel_task:
            def _worker(dev: Device) -> JobLogBuffer:
                close_old_connections()
                return _probe(dev)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_worker, dev): dev for dev in device_list}
                for future in as_completed(futures):
                    dev = futures[future]
                    try:
                        buf = future.result()
                        buf.drain_to(self.logger)
                    except Exception as exc:
                        self.logger.error(f"{dev} Unexpected probe error: {exc}")
        else:
            for dev in device_list:
                _probe(dev).drain_to(self.logger)

        # ------------------------------------------------------------------
        # Apply phase — ORM writes and structured record_* calls on main thread
        # ------------------------------------------------------------------
        for result in results:
            self._apply_result(result, dry_run, run_capture_after, credential)

        identified = sum(1 for r in results if r["driver"])
        unreachable = sum(1 for r in results if not r["reachable"] and not r.get("no_ip"))
        no_ip = sum(1 for r in results if r.get("no_ip"))

        self.logger.info(
            f"Discovery complete: {len(results)} probed, {identified} platforms identified, "
            f"{unreachable} unreachable, {no_ip} skipped (no primary IP)."
        )

        self.record_event(
            level="info",
            message="Platform discovery completed",
            context={
                "total": len(results),
                "identified": identified,
                "unreachable": unreachable,
                "no_ip": no_ip,
                "dry_run": dry_run,
            },
        )
        self.finalize_framework_run(filename_prefix="discover_platform_report")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_result(
        self,
        result: dict,
        dry_run: bool,
        run_capture_after: bool,
        credential: Optional[SecretsGroup],
    ) -> None:
        """
        Process a single _discover_platform() result on the main thread:
          - Resolve the detected driver to a Nautobot Platform record.
          - Write device.platform (unless dry_run).
          - Optionally run the full data-capture pipeline.
          - Call FrameworkJobMixin record_success / record_failure / record_skipped.
        """
        device = result["device"]
        driver = result["driver"]
        stage  = result["stage"]

        # No primary IP — skip entirely
        if result.get("no_ip"):
            self.record_skipped(
                target=str(device),
                message="No primary IP configured — discovery skipped",
                details={"reason": "no_primary_ip"},
            )
            return

        # Unreachable — skip entirely
        if not result["reachable"]:
            self.record_skipped(
                target=str(device),
                message="Device unreachable via ICMP — discovery skipped",
                details={"reason": "icmp_unreachable"},
            )
            return

        # Reachable but no platform identified
        if not driver:
            self.record_failure(
                target=str(device),
                message="Platform could not be determined by any probe method",
                details={"reason": "no_match", "stages_attempted": ["ssh_banner", "snmp", "ssh_autodetect"]},
            )
            return

        # Resolve driver → Nautobot Platform record, respecting manufacturer compatibility.
        # A Platform with manufacturer=None is universally compatible.
        # A Platform with manufacturer set is only valid for devices whose device_type
        # belongs to that same manufacturer (Nautobot enforces this with a ValidationError).
        device_manufacturer = (
            device.device_type.manufacturer if device.device_type else None
        )
        candidate_platforms = list(Platform.objects.filter(network_driver=driver).select_related("manufacturer"))

        if not candidate_platforms:
            self.logger.warning(
                f"{device} Detected network_driver='{driver}' has no matching Platform record "
                f"in Nautobot. Create a Platform with network_driver='{driver}' to enable "
                f"automatic assignment. Skipping platform write."
            )
            self.record_failure(
                target=str(device),
                message=f"No Nautobot Platform with network_driver='{driver}'",
                details={"detected_driver": driver, "stage": stage},
            )
            return

        # Pick the best compatible platform:
        #   1. Manufacturer matches the device's device_type manufacturer exactly.
        #   2. Platform has no manufacturer restriction (None) — universally compatible.
        platform_obj = None
        # Priority 1: platform whose manufacturer matches the device's manufacturer exactly (by PK).
        for p in candidate_platforms:
            if p.manufacturer is not None and device_manufacturer is not None and p.manufacturer.pk == device_manufacturer.pk:
                platform_obj = p
                break
        # Priority 2: unrestricted platform (manufacturer=None) — compatible with all device types.
        if platform_obj is None:
            for p in candidate_platforms:
                if p.manufacturer is None:
                    platform_obj = p
                    break
        # Priority 3: case-insensitive name match — handles duplicate manufacturer records
        # that differ only in capitalisation (e.g. "Mikrotik" vs "MikroTik").
        if platform_obj is None and device_manufacturer is not None:
            for p in candidate_platforms:
                if (
                    p.manufacturer is not None
                    and p.manufacturer.name.lower() == device_manufacturer.name.lower()
                ):
                    platform_obj = p
                    self.logger.warning(
                        f"{device} Matched platform '{p}' via case-insensitive manufacturer name "
                        f"(platform manufacturer: '{p.manufacturer}', device manufacturer: "
                        f"'{device_manufacturer}'). These are duplicate manufacturer records — "
                        f"run the 'Merge Duplicate Manufacturers' job to clean up the data."
                    )
                    break
        if platform_obj is None:
            # All candidates have a manufacturer restriction incompatible with this device.
            candidate_info = ", ".join(
                f"'{p}' (manufacturer: {p.manufacturer})" for p in candidate_platforms
            )
            self.logger.warning(
                f"{device} Found {len(candidate_platforms)} Platform record(s) with "
                f"network_driver='{driver}' but none are compatible with this device's "
                f"manufacturer '{device_manufacturer}': {candidate_info}. "
                f"Ensure the correct Platform has a matching or no manufacturer restriction."
            )
            self.record_failure(
                target=str(device),
                message=f"Platform(s) with network_driver='{driver}' are not compatible with device manufacturer '{device_manufacturer}'",
                details={
                    "detected_driver": driver,
                    "stage": stage,
                    "device_manufacturer": str(device_manufacturer),
                    "candidate_platforms": [str(p) for p in candidate_platforms],
                },
            )
            return

        # Dry run — report only, no writes
        if dry_run:
            self.logger.info(
                f"{device} [DRY RUN] Would assign platform '{platform_obj}' "
                f"(network_driver='{driver}', detected via {stage})"
            )
            self.record_success(
                target=str(device),
                message=f"[DRY RUN] Platform identified: '{platform_obj}'",
                details={"driver": driver, "platform": str(platform_obj), "stage": stage, "dry_run": True},
            )
            return

        # Write platform to device
        device.platform = platform_obj
        device.validated_save()
        self.logger.info(
            f"{device} Platform set to '{platform_obj}' (network_driver='{driver}', "
            f"detected via {stage})"
        )

        # Optionally run data capture
        if run_capture_after:
            if not credential:
                self.logger.warning(
                    f"{device} run_capture_after=True but no credential provided — capture skipped."
                )
                self.record_success(
                    target=str(device),
                    message=f"Platform set: '{platform_obj}' (capture skipped — no credential selected)",
                    details={"driver": driver, "stage": stage, "capture": "skipped_no_credential"},
                )
                return

            # Assign the SecretsGroup so get_device_connection_info() can retrieve credentials
            if device.secrets_group_id != credential.pk:
                device.secrets_group = credential
                device.validated_save()

            try:
                self._run_capture(device)
                self.record_success(
                    target=str(device),
                    message=f"Platform set and data captured: '{platform_obj}'",
                    details={"driver": driver, "stage": stage, "capture": "success"},
                )
            except Exception as exc:
                self.logger.error(f"{device} Data capture failed: {exc}")
                self.record_success(
                    target=str(device),
                    message=f"Platform set: '{platform_obj}' (capture failed — see logs)",
                    details={"driver": driver, "stage": stage, "capture": "failed", "error": str(exc)},
                )
        else:
            self.record_success(
                target=str(device),
                message=f"Platform set: '{platform_obj}'",
                details={"driver": driver, "stage": stage},
            )

    def _run_capture(self, device: Device) -> None:
        """
        Connect to an existing Nautobot device and run the full data-capture pipeline.
        Mirrors OnboardDevice._run_full_capture() from onboard_device.py but operates
        on an already-existing device record (avoids creating a duplicate device).

        Requires device.platform and device.secrets_group to both be set.
        """
        # Lazy imports to keep startup time low and avoid circular imports.
        from custom_jobs.inventory.capture_network_device_data import (  # noqa: PLC0415
            ARPMACCollector,
            CaptureDeviceData,
            LLDPDiscovery,
            OnboardSerial,
            OnboardVersion,
            SUPPORTED_PLATFORMS_ARP,
            SUPPORTED_PLATFORMS_INTERFACES,
            SUPPORTED_PLATFORMS_LLDP,
            SUPPORTED_PLATFORMS_SERIAL,
            SUPPORTED_PLATFORMS_VERSION,
        )
        from custom_jobs.inventory.optics_transceiver_inventory import (  # noqa: PLC0415
            SUPPORTED_PLATFORMS_TRANSCEIVERS,
            TransceiverInventory,
        )

        driver = device.platform.network_driver
        self.logger.info(f"{device} Starting full data capture (driver: {driver}).")

        with ConnectHandler(**get_device_connection_info(device)) as session:
            session.enable()
            if driver in SUPPORTED_PLATFORMS_INTERFACES:
                CaptureDeviceData(self, device).execute(session)
            if driver in SUPPORTED_PLATFORMS_SERIAL:
                OnboardSerial(self, device).onboard(session)
            if driver in SUPPORTED_PLATFORMS_VERSION:
                OnboardVersion(self, device).onboard(session)
            if driver in SUPPORTED_PLATFORMS_LLDP:
                LLDPDiscovery(self, device).run(session)
            if driver in SUPPORTED_PLATFORMS_ARP:
                ARPMACCollector(self, device).run(session)
            if driver in SUPPORTED_PLATFORMS_TRANSCEIVERS:
                TransceiverInventory(self, device).run(session)

        self.logger.info(f"{device} Data capture completed.")


register_jobs(DiscoverDevicePlatform)
