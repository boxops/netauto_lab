"""Purpose: Capture network device data and synchronise interfaces with Nautobot."""

import ipaddress

from netmiko import ConnectHandler
from netaddr import EUI
from netutils.ip import is_ip
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from nautobot.dcim.models import Interface
from nautobot.dcim.choices import InterfaceTypeChoices
from nautobot.ipam.models import IPAddress, Prefix, Namespace
from nautobot.extras.models import Status
from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar

from custom_jobs.modules.tools import (
    apply_device_filters,
    get_device_connection_info,
    parse_command_output,
    parallel_execution,
    JobLogBuffer,
    JobProxy,
    DeviceFormEntry,
)
from custom_jobs.onboarding.onboard_serial_numbers import OnboardSerial
from custom_jobs.onboarding.onboard_software_version import OnboardVersion

name = "Onboarding"

SUPPORTED_PLATFORMS = [
    "cisco_xr",
    "arista_eos",
]

# Per-platform CLI command, TextFSM template, and normalised field-name mapping.
# An empty string value means the field is unavailable from that platform's output.
PLATFORM_CONFIG = {
    "cisco_xr": {
        "command": "show interfaces",
        "template": "cisco_xr_show_interfaces.textfsm",
        "field_map": {
            "name":        "INTERFACE",
            "link_status": "LINK_STATUS",
            "description": "DESCRIPTION",
            "mac_address": "MAC_ADDRESS",
            "mtu":         "MTU",
            "ip_address":  "IP_ADDRESS",
            "speed":       "SPEED",
            "duplex":      "DUPLEX",
        },
    },
    "arista_eos": {
        "command": "show interfaces",
        "template": "arista_eos_show_interfaces.textfsm",
        "field_map": {
            "name":        "INTERFACE",
            "link_status": "LINK_STATUS",
            "description": "DESCRIPTION",
            "mac_address": "MAC_ADDRESS",
            "mtu":         "MTU",
            "ip_address":  "IP_ADDRESS",
            "speed":       "",
            "duplex":      "",
        },
    },
}


class CustomCaptureDeviceData(Job, DeviceFormEntry):
    """Capture live interface data from devices and synchronise Nautobot records."""

    onboard_serial_numbers = BooleanVar(
        description="Also onboard device serial numbers (all platforms)",
        default=True,
        required=False,
    )
    onboard_software_version = BooleanVar(
        description="Also onboard device software versions (all platforms)",
        default=True,
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of parallel workers",
        default=20,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Capture Network Device Data"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2400
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(
        self,
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
        onboard_serial_numbers=False,
        onboard_software_version=False,
        parallel_task=False,
        max_workers=20,
    ):
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
            self.logger.warning("No devices matched the selected filters.")
            return

        def capture_device_data(dev):
            buf = JobLogBuffer()
            try:
                if dev.platform.network_driver in SUPPORTED_PLATFORMS:
                    buf.info(f"{dev} Starting interface capture.")
                    CaptureDeviceData(JobProxy(buf), dev).execute()
                else:
                    buf.warning(
                        f"{dev} platform {dev.platform.network_driver} not supported "
                        f"for interface capture, skipping."
                    )
                if onboard_serial_numbers:
                    buf.info(f"{dev} Onboarding serial number.")
                    OnboardSerial(JobProxy(buf), dev).onboard()
                if onboard_software_version:
                    buf.info(f"{dev} Onboarding software version.")
                    OnboardVersion(JobProxy(buf), dev).onboard()
            except Exception as exc:
                buf.error(f"{dev} Error: {exc}")
            return buf

        if parallel_task:
            parallel_execution(
                capture_device_data, all_devices, max_workers, job_logger=self.logger
            )
        else:
            for dev in all_devices:
                capture_device_data(dev).drain_to(self.logger)


class CaptureDeviceData:
    """Handles SSH connection, data capture, and Nautobot sync for a single device."""

    def __init__(self, job, device):
        self.job = job
        self.device = device
        self.session = None
        self.captured_data = {}

    # ---- connection management ----

    def open(self):
        """Open a Netmiko SSH session."""
        self.session = ConnectHandler(**get_device_connection_info(self.device))
        self.session.enable()

    def close(self):
        """Disconnect if the session is open."""
        if self.session:
            self.session.disconnect()
            self.session = None

    # ---- data collection ----

    def capture_interfaces(self):
        """Fetch and parse interface data from the device CLI."""
        platform = self.device.platform.network_driver
        config = PLATFORM_CONFIG[platform]
        output = self.session.send_command(config["command"])
        parsed = parse_command_output(output, config["template"])
        self.captured_data["interfaces"] = parsed
        self.job.logger.info(
            f"{self.device} Captured {len(parsed)} interface entries."
        )

    # ---- nautobot sync ----

    def import_interfaces(self):
        """Sync parsed interface entries to Nautobot."""
        platform = self.device.platform.network_driver
        field_map = PLATFORM_CONFIG[platform]["field_map"]
        active_status = Status.objects.get(name="Active")

        for item in self.captured_data.get("interfaces", []):
            intf_name = item.get(field_map["name"], "")
            if not intf_name:
                continue

            link_status = item.get(field_map["link_status"], "") if field_map["link_status"] else ""
            description = item.get(field_map["description"], "") if field_map["description"] else ""
            mac_address = item.get(field_map["mac_address"], "") if field_map["mac_address"] else ""
            mtu         = item.get(field_map["mtu"],         "") if field_map["mtu"]         else ""
            ip_address  = item.get(field_map["ip_address"],  "") if field_map["ip_address"]  else ""

            interface = self.get_or_create_interface(
                intf_name, self.device, InterfaceTypeChoices.TYPE_1GE_FIXED, active_status
            )

            if link_status:
                self.update_interface_enabled(interface, link_status)
            if description:
                self.update_interface_description(interface, description)
            if mac_address:
                self.update_interface_mac_address(interface, mac_address)
            if mtu:
                self.update_interface_mtu(interface, mtu)

            # Save before IP assignment so the interface has a PK for the M2M relation.
            interface.validated_save()

            if ip_address and ip_address not in ("", "unassigned"):
                try:
                    self.update_interface_ip_address(interface, ip_address)
                except Exception as exc:
                    self.job.logger.error(
                        f"{self.device} Failed to set IP on {intf_name}: {exc}"
                    )

            self.job.logger.info(f"{self.device} Updated interface {intf_name}.")

    def execute(self):
        """Open a session, capture interface data, and sync to Nautobot."""
        try:
            self.open()
            self.capture_interfaces()
            self.import_interfaces()
        except Exception as exc:
            self.job.logger.error(f"{self.device} Error: {exc}")
        finally:
            self.close()

    # ---- record helpers ----

    def get_or_create_interface(self, name, device, iface_type, status):
        """Return an existing Interface or an unsaved new one."""
        try:
            return Interface.objects.get(name=name, device=device)
        except ObjectDoesNotExist:
            return Interface(name=name, device=device, type=iface_type, status=status)

    def get_or_create_prefix(self, ip_address):
        """Ensure the parent prefix exists in Nautobot, creating it if absent."""
        ip_network = ipaddress.ip_network(ip_address, strict=False)
        ip_prefix = str(ip_network)
        try:
            return Prefix.objects.get(prefix=ip_prefix)
        except ObjectDoesNotExist:
            prefix = Prefix.objects.create(
                prefix=ip_prefix,
                status=Status.objects.get(name="Active"),
                namespace=Namespace.objects.get(name="Global"),
            )
            self.job.logger.info(f"{self.device} Created prefix {ip_prefix}.")
            return prefix

    # ---- field updaters ----

    def update_interface_description(self, interface, description):
        if interface.description != description:
            interface.description = description

    def update_interface_enabled(self, interface, link_status):
        interface.enabled = "up" in link_status.lower()

    def update_interface_mac_address(self, interface, mac_address):
        try:
            converted = EUI(mac_address)
        except Exception:
            return
        if interface.mac_address != converted:
            interface.mac_address = converted

    def update_interface_mtu(self, interface, mtu):
        if interface.mtu != mtu:
            interface.mtu = mtu

    def update_interface_ip_address(self, interface, ip_address):
        """Ensure the IP exists in IPAM and is assigned to this interface."""
        if not is_ip(ip_address.split("/")[0]):
            self.job.logger.error(f"{self.device} Invalid IP: {ip_address}")
            return

        self.get_or_create_prefix(ip_address)

        try:
            existing_ip = IPAddress.objects.get(address=ip_address)
        except ObjectDoesNotExist:
            existing_ip = IPAddress.objects.create(
                address=ip_address,
                namespace=Namespace.objects.get(name="Global"),
                status=Status.objects.get(name="Active"),
            )
            self.job.logger.info(f"{self.device} Created IP {ip_address}.")

        if not interface.ip_addresses.filter(id=existing_ip.id).exists():
            interface.ip_addresses.add(existing_ip)
            self.job.logger.info(
                f"{self.device} Assigned {ip_address} to {interface}."
            )


register_jobs(CustomCaptureDeviceData)
