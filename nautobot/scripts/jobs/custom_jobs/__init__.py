"""The __init__.py module is required for Nautobot to load the jobs via Git."""

import sys as _sys
import importlib as _importlib

# ── Compatibility shims ────────────────────────────────────────────────────────
# modules/ and backends/ live at JOBS_ROOT level (not inside this package).
# Pre-populate sys.modules so that `from custom_jobs.modules.X import ...`
# and `from custom_jobs.backends.X import ...` resolve correctly.
for _alias, _real in [
    ('custom_jobs.modules',            'modules'),
    ('custom_jobs.modules.tools',      'modules.tools'),
    ('custom_jobs.modules.git',        'modules.git'),
    ('custom_jobs.modules.diff_utils', 'modules.diff_utils'),
    ('custom_jobs.backends',           'backends'),
    ('custom_jobs.backends.tachyon',   'backends.tachyon'),
]:
    if _alias not in _sys.modules:
        try:
            if _real not in _sys.modules:
                _importlib.import_module(_real)
            _sys.modules[_alias] = _sys.modules[_real]
        except ImportError:
            pass
del _sys, _importlib

# ── Documentation ────────────────────────────────────────────────────────────
from .documentation.generate_site_docs import GenerateSiteDocs

# ── Configuration ──────────────────────────────────────────────────────────────
from .configuration.backup_configurations import CustomDeviceBackup
from .configuration.intended_configurations import CustomDeviceIntended
from .configuration.configuration_compliance import CustomDeviceCompliance
from .configuration.deploy_configurations import DeployConfigurations, RemediateCompliance
from .configuration.run_all_compliance_jobs import RunAllConfigComplianceJobs

# ── Inventory ─────────────────────────────────────────────────────────────────
from .inventory.lldp_neighbor_discovery import LLDPNeighborDiscovery
from .inventory.arp_mac_sync import ARPMACSync
from .inventory.interface_capacity_audit import InterfaceCapacityAudit
from .inventory.optics_transceiver_inventory import OpticsTransceiverInventory
from .inventory.capture_network_device_data import CustomCaptureDeviceData
from .inventory.onboard_device import CustomDeviceOnboarding

# ── Monitoring ────────────────────────────────────────────────────────────────
from .monitoring.prometheus_target_sync import PrometheusTargetSync
from .monitoring.reachability_sweep import ReachabilitySweep
from .monitoring.interface_error_alerting import InterfaceErrorAlerting

# ── Operations ────────────────────────────────────────────────────────────────
from .operations.command_runner import CommandRunner
from .operations.password_prober import PasswordProber
from .operations.send_email import SendEmail
from .operations.reboot_devices import CustomDeviceReboot
from .operations.maintenance_window import MaintenanceWindow
from .operations.vlan_provisioning import VLANProvisioning
from .operations.ip_address_allocation import IPAddressAllocation
from .operations.device_decommission import DeviceDecommission

# ── Orchestration ─────────────────────────────────────────────────────────────
from .orchestration.change_window_orchestrator import ChangeWindowOrchestrator
from .orchestration.mass_rollback import MassRollback

# ── Reporting ─────────────────────────────────────────────────────────────────
from .reporting.check_device_serial_numbers import SerialNumberReport
from .reporting.hostname_validation import ValidateHostname
from .reporting.hardware_eos_alert import HardwareEOLAlert
from .reporting.generate_solarwinds_undp_reports import SolarWindsUNDPReport
from .reporting.check_cisco_package_compliance import CiscoPackageCompliance
from .reporting.backup_state_checker import BackupStateChecker
from .reporting.cve_vulnerability_scanner import CVEVulnerabilityScanner
from .reporting.software_version_compliance import SoftwareVersionComplianceReport

# ── Security ──────────────────────────────────────────────────────────────────
from .security.ssh_audit import SSHAudit
from .security.unused_port_shutdown import UnusedPortShutdown
from .security.aaa_compliance import AAAComplianceCheck

# ── Syncing ───────────────────────────────────────────────────────────────────
from .syncing.ssot_example_data_source import ExampleDataSource
from .syncing.ssot_example_data_target import ExampleDataTarget
from .syncing.ssot_example_data_target_two import SyncTenants
from .syncing.sync_network_data import SyncNetworkData

# ── Troubleshooting ───────────────────────────────────────────────────────────
from .troubleshooting.packet_capture import PacketCapture
from .troubleshooting.trace_route_analyzer import TraceRouteAnalyzer
from .troubleshooting.mtu_mismatch_detector import MTUMismatchDetector
from .troubleshooting.bgp_prefix_anomaly import BGPPrefixAnomalyDetector

# ── Upgrading ─────────────────────────────────────────────────────────────────
from .upgrading.firmware_upgrade import FirmwareUpgrade
from .upgrading.readiness_check import ReadinessCheck


__all__ = [
    ### Configuration
    "CustomDeviceBackup",
    "CustomDeviceIntended",
    "CustomDeviceCompliance",
    "DeployConfigurations",
    "RemediateCompliance",
    "RunAllConfigComplianceJobs",
    ### Inventory
    "LLDPNeighborDiscovery",
    "ARPMACSync",
    "InterfaceCapacityAudit",
    "OpticsTransceiverInventory",
    ### Monitoring
    "PrometheusTargetSync",
    "ReachabilitySweep",
    "InterfaceErrorAlerting",
    ### Onboarding
    "CustomDeviceOnboarding",
    "CustomCaptureDeviceData",
    ### Operations
    "CommandRunner",
    "PasswordProber",
    "SendEmail",
    "CustomDeviceReboot",
    "MaintenanceWindow",
    "VLANProvisioning",
    "IPAddressAllocation",
    "DeviceDecommission",
    ### Orchestration
    "ChangeWindowOrchestrator",
    "MassRollback",
    ### Reporting
    "SerialNumberReport",
    "ValidateHostname",
    "HardwareEOLAlert",
    "SolarWindsUNDPReport",
    "CiscoPackageCompliance",
    "BackupStateChecker",
    "CVEVulnerabilityScanner",
    "SoftwareVersionComplianceReport",
    ### Security
    "SSHAudit",
    "UnusedPortShutdown",
    "AAAComplianceCheck",
    ### Syncing
    "ExampleDataSource",
    "ExampleDataTarget",
    "SyncTenants",
    "SyncNetworkData",
    ### Troubleshooting
    "PacketCapture",
    "TraceRouteAnalyzer",
    "MTUMismatchDetector",
    "BGPPrefixAnomalyDetector",
    ### Documentation
    "GenerateSiteDocs",
    ### Upgrading
    "FirmwareUpgrade",
    "ReadinessCheck",
]
