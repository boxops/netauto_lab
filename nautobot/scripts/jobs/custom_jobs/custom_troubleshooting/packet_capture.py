"""
Purpose:
- Automate packet capture processes for detailed network analysis.
"""

# TODO: Written by Copilot, review and test the code
import os
import pyshark
import time
from datetime import datetime

from nautobot.apps.jobs import Job, ObjectVar, StringVar
from nautobot.extras.models import Status
from nautobot.extras.choices import StatusChoices

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution

name = "Custom Troubleshooting"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xr",
    "arista_eos",
]


class PacketCapture(Job, DeviceFormEntry):
    """Job to capture packets from a device interface."""

    interface = StringVar(
        description="Interface to capture packets from",
        label="Interface",
    )

    duration = StringVar(
        description="Duration of the packet capture",
        label="Duration",
    )

    class Meta:
        name = "Packet Capture"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        soft_time_limit = 3600
        time_limit = 4800
        task_queues = [
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
        interface=None,
        duration=None,
    ):
        """Execute the packet capture process."""

        # Get device connection info
        device_info = get_device_connection_info(
            tenant_group=tenant_group,
            tenant=tenant,
            location=location,
            rack_group=rack_group,
            rack=rack,
        )

        # Apply device filters
        devices = apply_device_filters(device_info, self.logger)

        # Execute packet capture process
        self.logger.info("Starting packet capture process...")

        for device in devices:
            self.logger.info(f"Capturing packets from device: {device}")

            # Start packet capture
            capture = pyshark.LiveCapture(
                interface=device.platform.device_connection,
                output_file=f"{device.name}-{interface}-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.pcap",
            )

            # Start the capture
            capture.sniff(timeout=int(duration))

            self.logger.info(f"Packet capture completed for device: {device}")

        self.logger.info("Packet capture process completed.")
        return Status(
            status=StatusChoices.STATUS_COMPLETED,
            message="Packet capture process completed.",
        )
