"""Purpose: Sync hardware inventory items from network devices into Nautobot InventoryItems."""

from netmiko import ConnectHandler
from django.conf import settings

from nautobot.dcim.models import InventoryItem
from nautobot.apps.jobs import register_jobs, Job, BooleanVar, IntegerVar

from custom_jobs.modules.tools import (
    apply_device_filters,
    get_device_connection_info,
    parse_command_output,
    parallel_execution,
    JobLogBuffer,
    JobProxy,
    DeviceFormEntry,
)

name = "Inventory"

SUPPORTED_PLATFORMS = [
    "cisco_xr", "cisco_ios", "cisco_xe", "cisco_nxos",
]


class InventoryItemsOnboard(Job, DeviceFormEntry):
    """Sync all hardware components from show inventory into Nautobot InventoryItem records."""

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
        name = "Inventory Items Onboard"
        description = (
            "Sync all hardware components from show inventory into Nautobot InventoryItem records. "
            f"Supported platforms: {SUPPORTED_PLATFORMS}"
        )
        has_sensitive_variables = False
        hidden = True
        soft_time_limit = 1800
        time_limit = 2400
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(self, **kwargs):
        parallel_task = kwargs.pop("parallel_task", False)
        max_workers = kwargs.pop("max_workers", 10)

        all_devices = apply_device_filters(set(), **kwargs)

        if not all_devices:
            self.logger.warning("No devices matched the selected filters.")
            return

        def process_device(dev):
            buf = JobLogBuffer()
            proxy = JobProxy(buf)
            driver = dev.platform.network_driver if dev.platform else None
            if driver not in SUPPORTED_PLATFORMS:
                buf.warning(f"{dev} Platform {driver} not supported for inventory item onboarding, skipping.")
                return buf
            buf.info(f"{dev} Syncing hardware inventory items.")
            OnboardInventoryItems(proxy, dev).onboard()
            return buf

        if parallel_task:
            parallel_execution(process_device, all_devices, max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                process_device(dev).drain_to(self.logger)


class OnboardInventoryItems:
    """Sync all rows from show inventory into Nautobot InventoryItem records."""

    # (command, template, serial_field)
    PLATFORM_CONFIG = {
        "cisco_xr":   ("show inventory", "cisco_xr_show_inventory.textfsm",   "SERIAL_NUMBER"),
        "cisco_ios":  ("show inventory", "cisco_ios_show_inventory.textfsm",   "SN"),
        "cisco_xe":   ("show inventory", "cisco_xe_show_inventory.textfsm",    "SERIAL_NUMBER"),
        "cisco_nxos": ("show inventory", "cisco_nxos_show_inventory.textfsm",  "SERIAL_NUMBER"),
    }

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def onboard(self, session=None):
        own_session = session is None
        try:
            if own_session:
                session = ConnectHandler(**get_device_connection_info(self.device))
                session.enable()
            platform = self.device.platform.network_driver
            cfg = self.PLATFORM_CONFIG.get(platform)
            if not cfg:
                self.job.logger.warning(f"{self.device} No inventory config for platform {platform}.")
                return

            command, template, serial_field = cfg
            try:
                output = session.send_command_timing(command)
                rows = parse_command_output(output, template)
            except Exception as exc:
                self.job.logger.error(f"{self.device} Error fetching inventory: {exc}")
                return

            mfr = self.device.device_type.manufacturer if self.device.device_type else None
            created = updated = skipped = 0

            for row in rows:
                name = (row.get("NAME") or "").strip()
                if not name:
                    continue

                descr   = (row.get("DESCR") or "").strip()
                part_id = (row.get("PID") or "").strip()
                serial  = (row.get(serial_field) or "").strip()

                try:
                    inv_item, was_created = InventoryItem.objects.get_or_create(
                        device=self.device,
                        name=name,
                        defaults={
                            "description": descr,
                            "part_id": part_id,
                            "serial": serial,
                            "manufacturer": mfr,
                            "discovered": True,
                        },
                    )
                    if was_created:
                        created += 1
                        self.job.logger.debug(
                            f"{self.device} Created InventoryItem '{name}' [{part_id}] SN={serial}"
                        )
                    else:
                        changed = False
                        if descr and inv_item.description != descr:
                            inv_item.description = descr
                            changed = True
                        if part_id and inv_item.part_id != part_id:
                            inv_item.part_id = part_id
                            changed = True
                        if serial and inv_item.serial != serial:
                            inv_item.serial = serial
                            changed = True
                        if mfr and inv_item.manufacturer != mfr:
                            inv_item.manufacturer = mfr
                            changed = True
                        if changed:
                            inv_item.validated_save()
                            updated += 1
                            self.job.logger.debug(f"{self.device} Updated InventoryItem '{name}'")
                        else:
                            skipped += 1
                except Exception as exc:
                    self.job.logger.error(f"{self.device} Error syncing InventoryItem '{name}': {exc}")

            self.job.logger.info(
                f"{self.device} InventoryItems: {created} created, {updated} updated, "
                f"{skipped} unchanged ({len(rows)} total from device)."
            )
        except Exception as exc:
            self.job.logger.error(f"{self.device} Error onboarding inventory items: {exc}")
        finally:
            if own_session and session:
                session.disconnect()


register_jobs(InventoryItemsOnboard)
