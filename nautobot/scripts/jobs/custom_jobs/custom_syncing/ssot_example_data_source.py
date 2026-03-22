"""Purpose: Sync VLANs from Netbox to Nautobot."""

from typing import Optional, Mapping
from diffsync import DiffSync
from diffsync.enum import DiffSyncFlags
from django.urls import reverse

from nautobot.ipam.models import VLAN, VLANGroup
from nautobot.apps.jobs import register_jobs, Job
from nautobot_ssot.contrib import NautobotModel, NautobotAdapter
from nautobot_ssot.jobs.base import DataSource, DataMapping
from nautobot.extras.models import Status
from nautobot.extras.jobs import ObjectVar, StringVar

import requests
import json

name = "Custom Syncing"


# Step 1 - Data Modeling
class VLANModel(NautobotModel):
    """DiffSync model for VLANs."""

    _model = VLAN
    _modelname = "vlan"
    _identifiers = ("vid", "name", "vlan_group__name")
    _attributes = ("description", "status__name")

    vid: int
    name: str
    status__name: str
    vlan_group__name: Optional[str] = None
    description: Optional[str] = None


class VLANGroupModel(NautobotModel):
    """DiffSync model for VLAN Groups."""

    _model = VLANGroup
    _modelname = "vlangroup"
    _identifiers = ("name",)
    _attributes = ("description",)

    name: str
    description: Optional[str] = None


# Step 2.1 - The Nautobot Adapter
class MySSoTNautobotAdapter(NautobotAdapter):
    """DiffSync adapter for Nautobot."""

    vlan = VLANModel
    vlangroup = VLANGroupModel
    top_level = ("vlangroup", "vlan")

    def __init__(self, *args, job, **kwargs):
        super().__init__(*args, job=job, **kwargs)


# Step 2.2 - The Remote Adapter
class MySSoTRemoteAdapter(DiffSync):
    """DiffSync adapter for remote system."""

    vlan = VLANModel
    vlangroup = VLANGroupModel
    top_level = ("vlangroup", "vlan")

    def __init__(self, *args, url, token, **kwargs):
        super().__init__(*args, **kwargs)
        self.url = url
        self.token = token
        self.headers = {
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
        }

    def _get_api_data(self, url_path: str) -> Mapping:
        requests.packages.urllib3.disable_warnings()
        data = requests.get(
            self.url + url_path, headers=self.headers, verify=False
        ).json()
        result_data = data["results"]
        print(f"Response: {result_data}")
        return result_data

    def load(self):
        # Load VLAN Groups first
        for item in self._get_api_data(url_path="/api/ipam/vlan-groups/"):
            loaded_vlangroup = self.vlangroup(
                name=item["name"],
                description=item["description"],
            )
            print(f"Loaded VLAN Group: {loaded_vlangroup}")
            self.add(loaded_vlangroup)

        # Load VLANs next
        for item in self._get_api_data(url_path="/api/ipam/vlans/"):
            loaded_vlan = self.vlan(
                vid=item["vid"],
                name=item["name"],
                status__name="Active",
                vlan_group__name=item["group"]["name"] if item["group"] else None,
                description=item["description"],
            )
            print(f"Loaded VLAN: {loaded_vlan}")
            self.add(loaded_vlan)


# Step 3 - The Job
class ExampleDataSource(DataSource):
    """SSoT Job class."""

    target_url = StringVar(
        description="Remote Netbox instance to update",
        default="https://netbox.air-band.net",
    )
    target_token = StringVar(
        description="REST API authentication token for remote Netbox instance",
        default="bc9de0a1638439fc6fe92c539237fdf791e6c673",
    )

    def __init__(self):
        super().__init__()

    class Meta:
        name = "Sync VLANs from Netbox to Nautobot"
        description = "SSoT Example Data Source"
        has_sensitive_variables = False
        data_source = "Netbox (remote)"

    @classmethod
    def data_mappings(cls):
        """This Job maps objects from the remote system to the local system."""
        return (DataMapping("VLANs", None, "VLANs", reverse("ipam:vlan_list")),)

    def run(self, target_url, target_token, dryrun, memory_profiling, *args, **kwargs):
        self.target_url = target_url
        self.target_token = target_token
        self.dryrun = dryrun
        self.memory_profiling = memory_profiling
        super().run(dryrun, memory_profiling, *args, **kwargs)

    def load_source_adapter(self):
        self.source_adapter = MySSoTRemoteAdapter(
            url=self.target_url, token=self.target_token
        )
        self.source_adapter.load()

    def load_target_adapter(self):
        self.target_adapter = MySSoTNautobotAdapter(job=self, sync=self.sync)
        self.target_adapter.load()


register_jobs(ExampleDataSource)
