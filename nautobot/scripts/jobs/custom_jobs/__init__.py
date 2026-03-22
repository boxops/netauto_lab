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

# Custom Configuration
from .custom_configuration.backup_configurations import CustomDeviceBackup
from .custom_configuration.intended_configurations import CustomDeviceIntended
from .custom_configuration.configuration_compliance import CustomDeviceCompliance
from .custom_configuration.deploy_configurations import DeployConfigurations
from .custom_configuration.run_all_compliance_jobs import RunAllConfigComplianceJobs

# Custom Monitoring
from .custom_monitoring.provision_nodes_on_solarwinds import ProvisionNodesOnSolarWinds

# Custom Onboarding
from .custom_onboarding.onboard_device import CustomDeviceOnboarding
from .custom_onboarding.onboard_software_version import GetShowVersion
from .custom_onboarding.onboard_serial_numbers import OnboardSerialNumbers
from .custom_onboarding.capture_network_device_data import CustomCaptureDeviceData

# Custom Operations
from .custom_operations.command_runner import CommandRunner
from .custom_operations.password_prober import PasswordProber
from .custom_operations.send_email import SendEmail
from .custom_operations.reboot_devices import CustomDeviceReboot
from .custom_operations.oxidized_inventory import OxidizedInventoryGenerator

# Custom Reporting
from .custom_reporting.check_device_serial_numbers import SerialNumberReport
from .custom_reporting.hostname_validation import ValidateHostname
from .custom_reporting.hardware_eos_alert import HardwareEOLAlert
from .custom_reporting.generate_solarwinds_undp_reports import SolarWindsUNDPReport
from .custom_reporting.check_cisco_package_compliance import CiscoPackageCompliance
from .custom_reporting.backup_state_checker import BackupStateChecker

# Custom Syncing
from .custom_syncing.ssot_example_data_source import ExampleDataSource
from .custom_syncing.ssot_example_data_target import ExampleDataTarget
from .custom_syncing.ssot_example_data_target_two import SyncTenants
from .custom_syncing.sync_network_data import SyncNetworkData

# Custom Upgrading
from .custom_upgrading.firmware_upgrade import FirmwareUpgrade

# from .custom_upgrading.readiness_check import ReadinessCheck


__all__ = [
    ### Custom Configuration
    "CustomDeviceBackup",
    "CustomDeviceIntended",
    "CustomDeviceCompliance",
    "DeployConfigurations",
    "RunAllConfigComplianceJobs",
    ### Custom Monitoring
    "ProvisionNodesOnSolarWinds",
    ### Custom Onboarding
    "CustomDeviceOnboarding",
    "GetShowVersion",
    "OnboardSerialNumbers",
    "CustomCaptureDeviceData",
    ### Custom Operations
    "CommandRunner",
    "PasswordProber",
    "SendEmail",
    "CustomDeviceReboot",
    ### Custom Reporting
    "SerialNumberReport",
    "ValidateHostname",
    "HardwareEOLAlert",
    "SolarWindsUNDPReport",
    "CiscoPackageCompliance",
    "BackupStateChecker",
    ### Custom Syncing
    "ExampleDataSource",
    "ExampleDataTarget",
    "SyncTenants",
    "SyncNetworkData",
    ### Custom Upgrading
    "FirmwareUpgrade",
    # "ReadinessCheck",
]
