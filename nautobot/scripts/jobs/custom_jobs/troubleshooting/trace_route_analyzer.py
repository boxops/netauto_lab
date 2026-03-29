"""
Purpose:
- Perform and analyze traceroute operations to identify network bottlenecks.
"""

# TODO: Written by Copilot, review and test the code
import os
import time
from datetime import datetime

from nautobot.apps.jobs import Job, ObjectVar, StringVar, TextVar
from nautobot.extras.models import Status

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution

name = "Troubleshooting"

SUPPORTED_PLATFORMS = [
    "cisco_ios",
    "cisco_xr",
    "arista_eos",
]


class TraceRouteAnalyzer(Job, DeviceFormEntry):
    """Job to perform and analyze traceroute operations."""

    destination = StringVar(
        description="Destination IP address to traceroute to",
        label="Destination IP",
    )

    count = StringVar(
        description="Number of packets to send",
        label="Count",
    )

    class Meta:
        name = "Trace Route Analyzer"
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
        destination=None,
        count=None,
    ):
        """Execute the traceroute process."""

        # Get device connection info
        device_info = get_device_connection_info(
            self,
            tenant_group=tenant_group,
            tenant=tenant,
            location=location,
            rack_group=rack_group,
            rack=rack,
        )

        # Apply device filters
        devices = apply_device_filters(
            device_info,
            platform=SUPPORTED_PLATFORMS,
        )

        # Execute the traceroute process
        results = parallel_execution(
            devices=devices,
            function=self._traceroute,
            function_kwargs={
                "destination": destination,
                "count": count,
            },
        )

        # Return the results
        return results

    def _traceroute(self, device, destination, count):
        """Perform the traceroute operation."""

        # Execute the traceroute command
        traceroute_command = f"traceroute {destination} -n -q {count}"
        traceroute_output = device.send_command(traceroute_command)

        # Analyze the traceroute output
        # Extract the IP addresses of the hops
        hops = []
        for line in traceroute_output.splitlines():
            if "ms" in line:
                hop = line.split()[1]
                hops.append(hop)

        # Return the hop IP addresses
        return hops
