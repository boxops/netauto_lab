"""Subnet discovery job with structured output support."""

import csv
import io
import ipaddress
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, StringVar, register_jobs

from custom_jobs.framework import FrameworkJobMixin

name = "Reporting"

# ---------------------------------------------------------------------------
# Module-level flags
# ---------------------------------------------------------------------------
ENABLE_ONBOARDING = False  # Set True to wire in OnboardDevice after discovery

# ---------------------------------------------------------------------------
# SSH banner → network_driver mapping (longest / most specific first)
# ---------------------------------------------------------------------------
BANNER_PLATFORM_MAP: Dict[str, str] = {
    "Cisco IOS XR": "cisco_xr",
    "NX-OS": "cisco_nxos",
    "Cisco IOS": "cisco_ios",
    "IOS-XE": "cisco_xe",
    "Arista": "arista_eos",
    "EOS": "arista_eos",
    "JunOS": "juniper_junos",
    "Juniper": "juniper_junos",
    "FSOS": "fiberstore_fsos",
    "VRP": "huawei_vrp",
    "Huawei": "huawei_vrp",
    "RouterOS": "mikrotik_routeros",
    "MikroTik": "mikrotik_routeros",
    "FortiOS": "fortinet_fortios",
    "FortiGate": "fortinet_fortios",
    "EdgeOS": "ubiquiti_edgeos",
    "Palo Alto": "paloalto_panos",
    "PAN-OS": "paloalto_panos",
}

SSH_BANNER_READ_BYTES = 512
SSH_BANNER_TIMEOUT = 3  # seconds

# ---------------------------------------------------------------------------
# SNMP constants
# ---------------------------------------------------------------------------
# Standard SNMP OIDs used for platform identification
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_LOCATION = "1.3.6.1.2.1.1.6.0"

SNMP_PORT = 161
SNMP_TIMEOUT = 3  # seconds
SNMP_RETRIES = 1


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _ping(ip: str, count: int) -> bool:
    """Return True if the host responds to ICMP ping."""
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


def _probe_ssh(ip: str, port: int) -> tuple:
    """
    Attempt a TCP connection to *port* and read the SSH banner bytes.

    Returns:
        (port_open: bool, banner: str, detected_platform: str)
    """
    try:
        with socket.create_connection((ip, port), timeout=SSH_BANNER_TIMEOUT) as sock:
            raw = sock.recv(SSH_BANNER_READ_BYTES)
        banner = raw.decode("utf-8", errors="replace").strip()
        platform = _detect_platform(banner)
        return True, banner, platform
    except (OSError, socket.timeout):
        return False, "", ""


def _detect_platform(banner: str) -> str:
    """Match banner text against known keyword → platform mappings."""
    for keyword, driver in BANNER_PLATFORM_MAP.items():
        if keyword.lower() in banner.lower():
            return driver
    return ""


# ---------------------------------------------------------------------------
# SNMP helpers (puresnmp v2c)
# ---------------------------------------------------------------------------

def _snmp_get(ip: str, community: str, oid: str) -> Optional[str]:
    """
    Issue a single SNMPv2c GET for *oid* using *community*.

    Returns the value as a string, or None on any error.
    puresnmp raises exceptions on auth failure, timeout, or network error.
    """
    try:
        import puresnmp  # noqa: PLC0415 — deferred to avoid hard import
        raw = puresnmp.get(ip, community, oid, port=SNMP_PORT, timeout=SNMP_TIMEOUT)
        return str(raw)
    except Exception:  # noqa: BLE001
        return None


def _probe_snmp(ip: str, communities: List[str]) -> Tuple[bool, str, str, str, str]:
    """
    Try each community string in order until sysDescr responds.

    Returns:
        (responded: bool, matched_community: str, sys_descr: str,
         sys_name: str, sys_location: str)
    """
    for community in communities:
        sys_descr = _snmp_get(ip, community, OID_SYS_DESCR)
        if sys_descr is not None:
            sys_name = _snmp_get(ip, community, OID_SYS_NAME) or ""
            sys_location = _snmp_get(ip, community, OID_SYS_LOCATION) or ""
            return True, community, sys_descr, sys_name, sys_location
    return False, "", "", "", ""


def _detect_platform_from_snmp(sys_descr: str) -> str:
    """
    Use sysDescr text to identify the platform (same keyword map as SSH banner).
    sysDescr is typically more verbose, so this supplements SSH detection.
    """
    return _detect_platform(sys_descr)


def _process_ip(
    ip: str,
    port: int,
    ping_count: int,
    probe_ssh_flag: bool,
    snmp_communities: List[str],
) -> dict:
    """Probe a single IP (ping -> SSH banner -> SNMP) and return a result dict."""
    reachable = _ping(ip, ping_count)

    ssh_open = False
    banner = ""
    ssh_platform = ""
    snmp_responded = False
    snmp_community = ""
    sys_descr = ""
    sys_name = ""
    sys_location = ""
    snmp_platform = ""
    notes_parts = []

    if reachable:
        if probe_ssh_flag:
            ssh_open, banner, ssh_platform = _probe_ssh(ip, port)
            if not ssh_open:
                notes_parts.append(f"SSH port {port} closed or filtered")

        if snmp_communities:
            snmp_responded, snmp_community, sys_descr, sys_name, sys_location = _probe_snmp(
                ip, snmp_communities
            )
            if snmp_responded:
                snmp_platform = _detect_platform_from_snmp(sys_descr)
            else:
                notes_parts.append("SNMP no response (all communities tried)")
    else:
        notes_parts.append("No ICMP response")

    # Prefer SSH platform; fall back to SNMP-derived platform
    detected_platform = ssh_platform or snmp_platform

    return {
        "ip_address": ip,
        "reachable": "yes" if reachable else "no",
        "ssh_port_open": "yes" if ssh_open else "no",
        "ssh_banner": banner[:120],
        "snmp_responded": "yes" if snmp_responded else "no",
        "snmp_community": snmp_community,
        "sys_name": sys_name,
        "sys_descr": sys_descr[:200],  # truncate for CSV readability
        "sys_location": sys_location,
        "detected_platform": detected_platform,
        "notes": "; ".join(notes_parts),
    }


# ---------------------------------------------------------------------------
# Job class
# ---------------------------------------------------------------------------

class SubnetDiscovery(FrameworkJobMixin, Job):
    """Ping-sweep a subnet, fingerprint reachable hosts via SSH banner, export CSV."""

    class Meta:
        name = "Subnet Discovery"
        description = (
            "Sweep a subnet with ICMP ping, attempt SSH banner detection on live hosts, "
            "and export a CSV discovery report."
        )
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2400
        task_queues = ["default", "priority", "bulk"]

    subnet = StringVar(
        description="Subnet to scan in CIDR notation, e.g. 10.31.10.0/24",
        label="Subnet (CIDR)",
        default="",
    )
    ssh_port = IntegerVar(
        description="TCP port used for SSH banner probing",
        label="SSH Port",
        default=22,
        min_value=1,
        max_value=65535,
    )
    ping_count = IntegerVar(
        description="Number of ICMP packets to send per host",
        label="Ping Count",
        default=2,
        min_value=1,
        max_value=10,
    )
    probe_ssh = BooleanVar(
        description="Attempt SSH banner fingerprinting on reachable hosts",
        label="Probe SSH Banner",
        default=True,
    )
    snmp_communities = StringVar(
        description=(
            "Comma-separated SNMP v2c community strings to try (e.g. public,private). "
            "Leave blank to skip SNMP probing."
        ),
        label="SNMP Communities",
        default="public",
    )
    max_workers = IntegerVar(
        description="Number of parallel worker threads",
        label="Max Workers",
        default=30,
        min_value=1,
        max_value=100,
    )

    def run(self, subnet, ssh_port, ping_count, probe_ssh, snmp_communities, max_workers):  # noqa: D102
        self.begin_framework_run(
            inputs={
                "subnet": subnet,
                "ssh_port": ssh_port,
                "ping_count": ping_count,
                "probe_ssh": probe_ssh,
                "snmp_communities": snmp_communities,
                "max_workers": max_workers,
            }
        )

        # ------------------------------------------------------------------
        # Validate and expand subnet
        # ------------------------------------------------------------------
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError as exc:
            self.logger.error(f"Invalid subnet '{subnet}': {exc}")
            raise

        # Exclude network and broadcast addresses for /24 and smaller
        if network.prefixlen < 31:
            ip_list = [str(ip) for ip in list(network.hosts())]
        else:
            ip_list = [str(ip) for ip in network]

        # Parse community strings — strip whitespace, drop empties
        communities: List[str] = (
            [c.strip() for c in snmp_communities.split(",") if c.strip()]
            if snmp_communities
            else []
        )

        total = len(ip_list)
        self.logger.info(
            f"Starting subnet discovery on {subnet} — {total} addresses to probe "
            f"(ping_count={ping_count}, ssh_port={ssh_port}, probe_ssh={probe_ssh}, "
            f"snmp_communities={communities or 'disabled'}, max_workers={max_workers})"
        )

        # ------------------------------------------------------------------
        # Parallel sweep (bounded thread pool)
        # ------------------------------------------------------------------
        results: List[dict] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _process_ip,
                    ip,
                    ssh_port,
                    ping_count,
                    probe_ssh,
                    communities,
                ): ip
                for ip in ip_list
            }
            for future in as_completed(futures):
                ip = futures[future]
                try:
                    row = future.result()
                    results.append(row)
                    if row["reachable"] == "yes":
                        self.record_success(
                            target=ip,
                            message="Host reachable and probed",
                            details={
                                "detected_platform": row["detected_platform"],
                                "ssh_port_open": row["ssh_port_open"],
                                "snmp_responded": row["snmp_responded"],
                            },
                        )
                    else:
                        self.record_skipped(
                            target=ip,
                            message=row["notes"] or "Host unreachable",
                            details={"reason": "unreachable"},
                        )
                except Exception as exc:
                    self.record_failure(
                        target=ip,
                        message=f"Probe failed: {exc}",
                    )
                    self.logger.error(f"{ip} probe failed: {exc}")

        # ------------------------------------------------------------------
        # Sort results by IP address
        # ------------------------------------------------------------------
        results.sort(key=lambda r: ipaddress.ip_address(r["ip_address"]))

        # ------------------------------------------------------------------
        # Build CSV
        # ------------------------------------------------------------------
        fieldnames = [
            "ip_address",
            "reachable",
            "ssh_port_open",
            "ssh_banner",
            "snmp_responded",
            "snmp_community",
            "sys_name",
            "sys_descr",
            "sys_location",
            "detected_platform",
            "notes",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
        csv_content = buf.getvalue()

        try:
            self.create_file("subnet_discovery.csv", csv_content)
        except Exception:
            self.logger.info("Subnet discovery CSV generated but not attached to a job request context.")

        # ------------------------------------------------------------------
        # Summary logging
        # ------------------------------------------------------------------
        reachable_count = sum(1 for r in results if r["reachable"] == "yes")
        ssh_open_count = sum(1 for r in results if r["ssh_port_open"] == "yes")
        snmp_count = sum(1 for r in results if r["snmp_responded"] == "yes")
        detected_count = sum(1 for r in results if r["detected_platform"])

        self.logger.info(
            f"Discovery complete: {total} probed, {reachable_count} reachable, "
            f"{ssh_open_count} SSH open, {snmp_count} SNMP responded, "
            f"{detected_count} platforms identified."
        )

        if reachable_count:
            reachable_ips = [r["ip_address"] for r in results if r["reachable"] == "yes"]
            self.logger.info("Reachable hosts: " + ", ".join(reachable_ips))

        # ------------------------------------------------------------------
        # Onboarding hook (disabled — set ENABLE_ONBOARDING = True to enable)
        # ------------------------------------------------------------------
        if ENABLE_ONBOARDING:
            # from custom_jobs.inventory.onboard_device import OnboardDevice
            # for row in results:
            #     if row["reachable"] == "yes" and row["detected_platform"]:
            #         try:
            #             OnboardDevice(
            #                 ip_address=row["ip_address"],
            #                 platform=row["detected_platform"],
            #             ).run()
            #         except Exception as exc:
            #             self.logger.warning(
            #                 f"Onboarding {row['ip_address']} failed: {exc}"
            #             )
            pass

        self.record_event(
            level="info",
            message="Subnet discovery completed",
            context={
                "subnet": subnet,
                "total_addresses": total,
                "reachable": reachable_count,
                "ssh_open": ssh_open_count,
                "snmp_responded": snmp_count,
                "identified_platforms": detected_count,
            },
        )
        self.finalize_framework_run(filename_prefix="subnet_discovery_report")


register_jobs(SubnetDiscovery)
