"""Purpose: Sync VRFs from network devices into Nautobot and link them to devices."""

from netmiko import ConnectHandler
from django.conf import settings

from nautobot.ipam.models import VRF, VRFDeviceAssignment, Namespace
from nautobot.extras.models import Status
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
    "cisco_xr",
]


class VRFOnboard(Job, DeviceFormEntry):
    """Sync VRFs from live devices into Nautobot and link them to their source device."""

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
        name = "VRF Onboard"
        description = (
            "Sync VRFs from live devices into Nautobot and link them to their source device. "
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
                buf.warning(f"{dev} Platform {driver} not supported for VRF onboarding, skipping.")
                return buf
            buf.info(f"{dev} Syncing VRFs.")
            OnboardVRFs(proxy, dev).onboard()
            return buf

        if parallel_task:
            parallel_execution(process_device, all_devices, max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                process_device(dev).drain_to(self.logger)


class OnboardVRFs:
    """Sync VRFs from show vrf all detail into Nautobot VRF records and link to device."""

    # VRF names that are management-plane only — skip them.
    SKIP_VRF_NAMES = frozenset({"management", "Mgmt-intf", "Mgmt-vrf", "mgmt"})

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def onboard(self, session=None):
        own_session = session is None
        try:
            if own_session:
                session = ConnectHandler(**get_device_connection_info(self.device))
                session.enable()
            try:
                output = session.send_command_timing("show vrf all detail")
                rows = parse_command_output(output, "cisco_xr_show_vrf_all_detail.textfsm")
            except Exception as exc:
                self.job.logger.error(f"{self.device} Error fetching VRFs: {exc}")
                return

            active_status = Status.objects.get(name="Active")
            namespace = Namespace.objects.get(name="Global")
            created = updated = skipped = 0

            for row in rows:
                vrf_name = (row.get("VRF") or "").strip()
                if not vrf_name or vrf_name in self.SKIP_VRF_NAMES:
                    continue

                rd_raw    = (row.get("RD") or "").strip()
                rd        = None if rd_raw in ("not set", "") else rd_raw
                descr_raw = (row.get("DESCRIPTION") or "").strip()
                description = "" if descr_raw == "not set" else descr_raw

                try:
                    vrf, was_created = VRF.objects.get_or_create(
                        name=vrf_name,
                        namespace=namespace,
                        defaults={
                            "rd": rd,
                            "description": description,
                            "status": active_status,
                        },
                    )
                    if was_created:
                        created += 1
                        self.job.logger.debug(f"{self.device} Created VRF '{vrf_name}' RD={rd}")
                    else:
                        changed = False
                        if rd and vrf.rd != rd:
                            vrf.rd = rd
                            changed = True
                        if description and vrf.description != description:
                            vrf.description = description
                            changed = True
                        if changed:
                            vrf.validated_save()
                            updated += 1
                            self.job.logger.debug(f"{self.device} Updated VRF '{vrf_name}'")
                        else:
                            skipped += 1

                    # Link VRF to this device if not already assigned.
                    VRFDeviceAssignment.objects.get_or_create(vrf=vrf, device=self.device)

                except Exception as exc:
                    self.job.logger.error(f"{self.device} Error syncing VRF '{vrf_name}': {exc}")

            self.job.logger.info(
                f"{self.device} VRFs: {created} created, {updated} updated, {skipped} unchanged."
            )
        except Exception as exc:
            self.job.logger.error(f"{self.device} Error onboarding VRFs: {exc}")
        finally:
            if own_session and session:
                session.disconnect()


register_jobs(VRFOnboard)
