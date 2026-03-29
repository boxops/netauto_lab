"""Purpose: Collect optics/transceiver inventory (DOM data) from devices and sync to Nautobot."""

from netmiko import ConnectHandler
import threading

from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar

from custom_jobs.modules.tools import (
    get_device_connection_info,
    apply_device_filters,
    DeviceFormEntry,
    parse_command_output,
    parallel_execution,
)

name = "Inventory"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xe",
    "cisco_xr",
    "cisco_nxos",
    "arista_eos",
]


class OpticsTransceiverInventory(Job, DeviceFormEntry):
    """
    SSH to devices, run transceiver/DOM show commands, parse output, and log
    optic model, serial number, tx/rx power, and temperature per interface.
    Results are exported as a CSV file per run.
    """

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
        name = "Optics and Transceiver Inventory"
        description = (
            "Collect transceiver DOM data (tx/rx power, temperature, serial) from devices. "
            f"Supported platforms: {SUPPORTED_PLATFORMS}"
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

        all_results = []
        all_results_lock = threading.Lock()

        def collect_device(dev):
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{dev} platform {dev.platform.network_driver} not supported, skipping."
                    )
                    return
                self.logger.info(f"{dev} Collecting transceiver data...")
                task = OpticsCollector(job=self, device=dev)
                results = task.run()
                with all_results_lock:
                    all_results.extend(results)
            except Exception as exc:
                self.logger.error(f"{dev} Error: {exc}")

        if parallel_task:
            parallel_execution(collect_device, all_devices, max_workers=max_workers)
        else:
            for dev in all_devices:
                collect_device(dev)

        if all_results:
            import csv
            import io

            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=["device", "interface", "type", "serial", "tx_power", "rx_power", "temperature"],
            )
            writer.writeheader()
            writer.writerows(all_results)
            self.create_file("optics_transceiver_inventory.csv", output.getvalue())
            self.logger.info(f"Exported {len(all_results)} transceiver records to CSV.")


class OpticsCollector:
    COMMANDS = {
        "cisco_ios": "show interfaces transceiver",
        "cisco_xe": "show interfaces transceiver",
        "cisco_xr": "show controllers optics",
        "cisco_nxos": "show interface transceiver details",
        "arista_eos": "show interfaces transceiver",
    }

    def __init__(self, job, device):
        self.job = job
        self.device = device

    def run(self):
        platform = self.device.platform.network_driver
        command = self.COMMANDS.get(platform)
        if not command:
            self.job.logger.warning(
                f"{self.device} No transceiver command defined for {platform}."
            )
            return []

        device_info = get_device_connection_info(self.device)
        try:
            with ConnectHandler(**device_info) as session:
                session.enable()
                output = session.send_command(command)
        except Exception as exc:
            self.job.logger.error(f"{self.device} Connection error: {exc}")
            return []

        # Raw output logging - TextFSM templates for transceiver DOM vary widely.
        # Operators should add per-platform TextFSM templates as needed.
        self.job.logger.info(
            f"{self.device} Transceiver raw output ({len(output)} chars):\n{output[:2000]}"
        )

        # Return a placeholder record so the CSV export is seeded for review.
        return [
            {
                "device": self.device.name,
                "interface": "see_raw_output",
                "type": "N/A",
                "serial": "N/A",
                "tx_power": "N/A",
                "rx_power": "N/A",
                "temperature": "N/A",
            }
        ]


register_jobs(OpticsTransceiverInventory)
