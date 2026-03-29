"""Purpose: Collect ARP and MAC address tables from devices and sync IP-to-MAC mappings into Nautobot."""

from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar
from nautobot.ipam.models import IPAddress

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parse_command_output,
    parallel_execution,
    JobLogBuffer,
    JobProxy,
)

name = "Inventory"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_xr",
    "cisco_nxos",
    "arista_eos",
]


class ARPMACSync(Job, DeviceFormEntry):
    """Collect ARP and MAC tables from devices and update Nautobot IP address records."""

    dry_run = BooleanVar(
        description="Preview changes without writing to Nautobot",
        default=False,
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of parallel workers",
        default=10,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "ARP and MAC Table Sync"
        description = (
            "Collect ARP and MAC address tables from devices and sync IP-to-MAC "
            f"mappings into Nautobot. Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2400
        task_queues = ["default", "priority", "bulk"]

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
        dry_run=True,
        parallel_task=False,
        max_workers=10,
    ):
        all_devices = set()
        all_devices = apply_device_filters(
            all_devices,
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

        def sync_device(dev):
            buf = JobLogBuffer()
            try:
                if dev.platform is None or dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    buf.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return buf
                buf.info(f"{dev} Collecting ARP/MAC table...")
                task = ARPMACCollector(job=JobProxy(buf), device=dev, dry_run=dry_run)
                task.run()
            except Exception as exc:
                buf.error(f"{dev} Error: {exc}")
            return buf

        if parallel_task:
            parallel_execution(
                sync_device, all_devices, max_workers=max_workers, job_logger=self.logger
            )
        else:
            for dev in all_devices:
                sync_device(dev).drain_to(self.logger)


class ARPMACCollector:
    ARP_COMMANDS = {
        "cisco_ios":  "show ip arp",
        "cisco_xe":   "show ip arp",
        "cisco_xr":   "show arp",
        "cisco_nxos": "show ip arp",
        "arista_eos": "show ip arp",
    }
    ARP_TEMPLATES = {
        "cisco_ios":  "cisco_ios_show_ip_arp.textfsm",
        "cisco_xe":   "cisco_ios_show_ip_arp.textfsm",
        "cisco_nxos": "cisco_nxos_show_ip_arp.textfsm",
        "arista_eos": "arista_eos_show_ip_arp.textfsm",
    }
    # Maps template filename → (ip_key, mac_key, intf_key) as produced by TextFSM.
    # Run `grep '^Value' <template>` to find the exact names for each template.
    TEMPLATE_FIELD_MAP = {
        "arista_eos_show_ip_arp.textfsm":  ("IPV4_ADDRESS", "HARDWARE_ADDR", "INTERFACE"),
        "cisco_ios_show_ip_arp.textfsm":   ("ADDRESS",      "MAC",           "INTERFACE"),
        "cisco_nxos_show_ip_arp.textfsm":  ("IP_ADDRESS",   "MAC",           "INTERFACE"),
    }

    def __init__(self, job, device, dry_run=True):
        self.job = job
        self.device = device
        self.dry_run = dry_run

    def run(self):
        platform = self.device.platform.network_driver
        command = self.ARP_COMMANDS.get(platform)
        template = self.ARP_TEMPLATES.get(platform)

        if not command:
            self.job.logger.warning(f"{self.device} No ARP command defined for {platform}.")
            return

        if template and template not in self.TEMPLATE_FIELD_MAP:
            self.job.logger.warning(
                f"{self.device} No field map defined for template '{template}'. "
                "Add an entry to TEMPLATE_FIELD_MAP."
            )
            return

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                arp_output = session.send_command(command)

            if template:
                try:
                    arp_entries = parse_command_output(arp_output, template)
                except FileNotFoundError:
                    self.job.logger.warning(
                        f"{self.device} TextFSM template '{template}' not found. "
                        "Logging raw output only."
                    )
                    self.job.logger.info(f"{self.device} Raw ARP output:\n{arp_output}")
                    return

                ip_key, mac_key, intf_key = self.TEMPLATE_FIELD_MAP[template]
                self.job.logger.info(
                    f"{self.device} Parsed {len(arp_entries)} ARP entries."
                )
                for entry in arp_entries:
                    self._process_arp_entry(entry, ip_key, mac_key, intf_key)
            else:
                self.job.logger.info(
                    f"{self.device} No TextFSM template configured — raw ARP output:\n{arp_output}"
                )
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")

    @staticmethod
    def _normalise_mac(raw_mac: str) -> str:
        """Normalise any common MAC notation to colon-separated lowercase.

        Handles:
          aabb.ccdd.eeff  (Cisco/Arista dotted)
          aa:bb:cc:dd:ee:ff
          aa-bb-cc-dd-ee-ff
        """
        digits = raw_mac.replace(":", "").replace("-", "").replace(".", "").lower()
        if len(digits) != 12:
            return raw_mac  # unrecognised format — return as-is
        return ":".join(digits[i:i+2] for i in range(0, 12, 2))

    def _process_arp_entry(self, entry, ip_key, mac_key, intf_key):
        ip_str    = entry.get(ip_key, "").strip()
        mac_raw   = entry.get(mac_key, "").strip()
        intf_str  = entry.get(intf_key, "").strip()

        if not ip_str or not mac_raw:
            return

        mac_str = self._normalise_mac(mac_raw)

        self.job.logger.info(
            f"{self.device} ARP: IP={ip_str} MAC={mac_str} Interface={intf_str}"
        )

        if self.dry_run:
            return

        try:
            ip_obj = IPAddress.objects.filter(host=ip_str).first()
            if ip_obj:
                ip_obj.cf["mac_address"] = mac_str
                ip_obj.cf["arp_source_device"] = self.device.name
                ip_obj.validated_save()
                self.job.logger.info(
                    f"{self.device} Updated IP {ip_str} with MAC {mac_str}."
                )
            else:
                self.job.logger.warning(
                    f"{self.device} IP {ip_str} not found in Nautobot, skipping MAC update."
                )
        except Exception as exc:
            self.job.logger.error(
                f"{self.device} Failed to update IP {ip_str}: {exc}"
            )


register_jobs(ARPMACSync)
