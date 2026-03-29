"""Purpose: Sync device interfaces from actual devices to Nautobot."""

try:
    from typing import Annotated  # Python>=3.9
except ImportError:
    from typing_extensions import Annotated  # Python<3.9

from typing import Optional, Mapping, List
from diffsync import DiffSync, DiffSyncModel
from diffsync.enum import DiffSyncFlags
from django.urls import reverse

from nautobot.dcim.models import Interface, Device
from nautobot.apps.jobs import register_jobs, Job
from nautobot_ssot.contrib import NautobotModel, NautobotAdapter, CustomFieldAnnotation
from nautobot_ssot.jobs.base import DataSource, DataMapping
from nautobot.extras.models import Status
from nautobot.extras.jobs import ObjectVar, StringVar

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import parse_command_output
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import convert_flat_config_to_dict

from netmiko import ConnectHandler
from textfsm import TextFSM
import requests
import json

name = "Syncing"

SUPPORTED_PLATFORMS = [
    "fiberstore_fsos",
]


class DeviceCLIScraper:
    def __init__(self, device, job):
        self.device = device
        self.job = job
        self.session = None
        self.device_info = get_device_connection_info(self.device)

    def connect(self):
        """Open an SSH connection to the device."""
        self.session = ConnectHandler(**self.device_info)
        self.session.enable()
        self.job.logger.info(f"Device info: {self.device_info}")

    def disconnect(self):
        """Close the SSH connection to the device."""
        if self.session:
            self.session.disconnect()
            self.job.logger.info("Disconnected from device")

    def get_interfaces(self):
        """Get a list of interfaces from the device."""
        platform_commands = {
            "fiberstore_fsos": (
                "show interfaces",
                "fiberstore_fsos_show_interfaces.textfsm",
            ),
        }
        try:
            command, template = platform_commands[self.device.platform.network_driver]
            self.connect()
            output = self.session.send_command(command)
            parsed_output = parse_command_output(output, template)
            return parsed_output
        except Exception as e:
            self.job.logger.error(f"Failed to get interfaces: {e}")
            return []
        finally:
            self.disconnect()

    def get_serial_number(self):
        """Get the serial number of the device."""
        platform_commands = {
            "fiberstore_fsos": ("show version", "fiberstore_fsos_show_version.textfsm"),
        }
        try:
            command, template = platform_commands[self.device.platform.network_driver]
            self.connect()
            output = self.session.send_command(command)
            parsed_output = parse_command_output(output, template)
            return parsed_output[0]["SERIAL_NUMBER"]
        except Exception as e:
            self.job.logger.error(f"Failed to get serial number: {e}")
            return None
        finally:
            self.disconnect()


# Step 1 - Data Modeling
class SyncNetworkDataDevice(NautobotModel):
    """Shared data model representing a Device."""

    _modelname = "device"
    _model = Device
    _identifiers = (
        "name",
        "serial",  # TODO
    )
    _attributes = ("last_network_data_sync",)
    # _children = {"interface": "interfaces"}

    name: str
    serial: str

    last_network_data_sync: Annotated[
        Optional[str],
        CustomFieldAnnotation(
            key="last_network_data_sync", name="last_network_data_sync"
        ),
    ] = None

    # interfaces: List[InterfaceModel] = []

    @classmethod
    def create(cls, adapter, ids, attrs):
        """
        Do not create new devices.

        Network devices need to exist in Nautobot prior to syncing data and
        need to be included in the queryset generated based on job form inputs.
        """
        adapter.job.logger.error(
            f"Network device {ids} is not included in the Nautobot devices "
            "selected for syncing. This device either does not exist in Nautobot "
            "or was not included based on filter criteria provided on the job form."
        )
        return None

    def delete(self):
        """Prevent device deletion."""
        self.adapter.job.logger.error(f"{self} will not be deleted.")
        return None


# Step 2.1 - The Nautobot Adapter
class MySSoTNautobotAdapter(NautobotAdapter):
    """DiffSync adapter for Nautobot."""

    device = SyncNetworkDataDevice
    top_level = ("device",)

    def __init__(self, *args, devices, job, **kwargs):
        super().__init__(*args, job=job, **kwargs)
        self.devices = devices
        self.job = job

    def load(self):
        for device in self.devices:
            loaded_device = self.device(
                name=device.name,
                serial=device.serial,
                # last_network_data_sync=device.last_network_data_sync,
            )
            self.job.logger.info(f"Loaded device: {loaded_device}")
            self.add(loaded_device)


# Step 2.2 - The Remote Adapter
class MySSoTRemoteAdapter(DiffSync):
    """DiffSync adapter for remote system."""

    device = SyncNetworkDataDevice
    top_level = ("device",)

    def __init__(self, *args, devices, job, **kwargs):
        super().__init__(*args, **kwargs)
        self.devices = devices
        self.job = job

    def load(self):
        for device in self.devices:
            scraper = DeviceCLIScraper(device, self.job)
            serial_number = scraper.get_serial_number()
            if not serial_number:
                self.job.logger.error(f"Failed to get serial number for {device}")
                continue
            loaded_device = self.device(
                name=device.name,
                serial=serial_number,
                # last_network_data_sync=device.last_network_data_sync,
            )
            self.job.logger.info(f"Loaded device: {loaded_device}")
            self.add(loaded_device)


# Step 3 - The Job
class SyncNetworkData(DataSource, DeviceFormEntry):
    """SSoT Job class."""

    def __init__(self):
        super().__init__()
        self.all_devices = set()

    class Meta:
        name = "Sync network device data to Nautobot"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        data_source = "Network Device (remote)"

    @classmethod
    def data_mappings(cls):
        """This Job maps objects from the remote system to the local system."""
        return (DataMapping("Devices", None, "Devices", reverse("dcim:device_list")),)

    def run(
        self,
        dryrun,
        memory_profiling,
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
        *args,
        **kwargs,
    ):
        self.all_devices = apply_device_filters(
            self.all_devices,
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
            self.all_devices.update(device)

        self.dryrun = dryrun
        self.memory_profiling = memory_profiling
        super().run(dryrun, memory_profiling, *args, **kwargs)

    def load_source_adapter(self):
        self.source_adapter = MySSoTRemoteAdapter(devices=self.all_devices, job=self)
        self.source_adapter.load()

    def load_target_adapter(self):
        self.target_adapter = MySSoTNautobotAdapter(devices=self.all_devices, job=self)
        self.target_adapter.load()


register_jobs(SyncNetworkData)
