from typing import Optional, Mapping
from diffsync import DiffSync, DiffSyncModel
from diffsync.enum import DiffSyncFlags
from django.urls import reverse
from uuid import UUID

from nautobot.ipam.models import VLAN, VLANGroup
from nautobot.apps.jobs import register_jobs  # , Job
from nautobot_ssot.contrib import NautobotModel, NautobotAdapter
from nautobot_ssot.jobs.base import DataTarget, DataMapping
from nautobot.extras.jobs import ObjectVar, StringVar, Job

import requests
import json

name = "Syncing"


# Step 1.1 - VLAN Data Modeling
class BaseVLANModel(DiffSyncModel):
    """Base model for VLANs."""

    _model = VLAN
    _modelname = "vlan"
    _identifiers = (
        "vid",
        "name",
        "vlan_group__name",
    )  # TODO: Retest: 400 - "The fields group, name must make a unique set."
    _attributes = ("description", "status__name")

    vid: int
    name: str
    status__name: str
    vlan_group__name: Optional[str] = None
    description: Optional[str] = None


class VLANNautobotModel(BaseVLANModel, NautobotModel):
    """DiffSync model for VLANs in Nautobot."""


class VLANNetboxModel(BaseVLANModel):
    """DiffSync model for VLANs in Netbox."""

    pk: Optional[int] = None


# Step 1.2 - VLAN Group Data Modeling
class BaseVLANGroupModel(DiffSyncModel):
    """Base model for VLAN Groups."""

    _model = VLANGroup
    _modelname = "vlan_group"
    _identifiers = ("name",)
    _attributes = ("description",)

    name: str
    description: Optional[str] = None
    # min_vid: int
    # max_vid: int


class VLANGroupNautobotModel(BaseVLANGroupModel, NautobotModel):
    """DiffSync model for VLANs in Nautobot."""


class VLANGroupNetboxModel(BaseVLANGroupModel):
    """DiffSync model for VLANs in Netbox."""

    pk: Optional[int] = None


# Step 2 - The Nautobot Adapter
class MySSoTNautobotAdapter(NautobotAdapter):
    """DiffSync adapter for Nautobot."""

    vlan = VLANNautobotModel
    top_level = ("vlan",)


# Step 3.1 - The Remote VLAN CRUD operations
class VLANRemoteModel(VLANNetboxModel):
    """Implementation of VLAN create/update/delete methods for updating the remote Netbox data."""

    # def __init__(self, *args, url, token, job, **kwargs):
    #     super().__init__(*args, **kwargs)
    #     self.url = url
    #     self.token = token
    #     self.job = job
    #     self.headers = {
    #         "Authorization": f"Token {self.token}",
    #         "Accept": "application/json",
    #     }

    @classmethod
    def create(cls, diffsync, ids, attrs):
        """Create a VLAN record in the remote system."""
        # vlan_group_id = None
        # url_path = f"{self.url}/api/ipam/vlans/?name={ids['vlan_group__name']}"
        # response = requests.get(self.url + url_path, headers=self.headers, verify=False)
        # if response.status_code == 200:
        #     data = response.json()
        #     vlan_group_id = data["results"][0]["id"]

        data = {
            "vid": ids["vid"],
            "name": ids["name"],
            "status": attrs["status__name"].lower(),
            "group": 1,  # vlan_group_id,
            "description": attrs["description"],
        }
        diffsync.post("/api/ipam/vlans/", data)
        return super().create(diffsync, ids=ids, attrs=attrs)

    def update(self, attrs):
        """Update an existing VLAN record in the remote system."""
        data = {}
        if "description" in attrs:
            data["description"] = attrs["description"]
        if "status__name" in attrs:
            data["status"] = attrs["status__name"].lower()
        self.diffsync.patch(f"/api/ipam/vlans/{self.pk}/", data)
        return super().update(attrs)

    def delete(self):
        """Delete an existing VLAN record from the remote system."""
        self.diffsync.delete(f"/api/ipam/vlans/{self.pk}/")
        return super().delete()


# Step 3.2 - The Remote VLAN Group CRUD operations
class VLANGroupRemoteModel(VLANGroupNetboxModel):
    """Implementation of VLAN create/update/delete methods for updating the remote Netbox data."""

    @classmethod
    def create(cls, diffsync, ids, attrs):
        """Create a VLAN record in the remote system."""
        data = {
            "name": ids["name"],
            "description": attrs["description"],
            "min_vid": 1,  # default
            "max_vid": 4094,  # default
        }
        diffsync.post("/api/ipam/vlan-groups/", data)
        return super().create(diffsync, ids=ids, attrs=attrs)

    def update(self, attrs):
        """Update an existing VLAN record in the remote system."""
        data = {}
        if "description" in attrs:
            data["description"] = attrs["description"]
        self.diffsync.patch(f"/api/ipam/vlan-groups/{self.pk}/", data)
        return super().update(attrs)

    def delete(self):
        """Delete an existing VLAN record from the remote system."""
        self.diffsync.delete(f"/api/ipam/vlan-groups/{self.pk}/")
        return super().delete()


# Step 3.2 - The Remote Adapter
class MySSoTRemoteAdapter(DiffSync):
    """DiffSync adapter for remote system."""

    def __init__(self, *args, url, token, job, **kwargs):
        super().__init__(*args, **kwargs)
        self.url = url
        self.token = token
        self.job = job
        self.headers = {
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
        }

    # vlan = VLANRemoteModel(url=self.target_url, token=self.target_token, job=self)
    vlan = VLANRemoteModel
    vlan_group = VLANGroupRemoteModel

    top_level = ("vlan_group", "vlan")

    def _get_api_data(self, url_path: str) -> Mapping:
        requests.packages.urllib3.disable_warnings()

        # # GraphQL implementation
        # query = """
        # query {
        #   vlan_list {
        #     id
        #     vid
        #     name
        #     status
        #     description
        #     group {
        #       id
        #       name
        #     }
        #   }
        # }
        # """
        # response = requests.post(
        #     self.url + url_path,
        #     headers=self.headers,
        #     verify=False,
        #     json={"query": query}
        # )
        # response.raise_for_status()
        # data = response.json()
        # self.job.logger.info(json.dumps(data["data"]["vlan_list"], indent=2))
        # return data["data"]["vlan_list"]

        response = requests.get(self.url + url_path, headers=self.headers, verify=False)
        response.raise_for_status()
        data = response.json()
        self.job.logger.info(json.dumps(data["results"], indent=2))
        return data["results"]

    def load_vlans(self):
        # for item in self._get_api_data("/graphql/"):
        for item in self._get_api_data("/api/ipam/vlans/"):
            loaded_vlan = self.vlan(
                pk=int(item["id"]),
                vid=item["vid"],
                name=item["name"],
                status__name=item["status"]["label"],  # "active",
                description=item["description"] if item["description"] else "",
                vlan_group__name=item["group"]["name"] if item["group"] else None,
            )
            self.job.logger.info(f"Loaded VLAN: {loaded_vlan}")
            self.add(loaded_vlan)

    def load_vlan_groups(self):
        for item in self._get_api_data("/api/ipam/vlan-groups/"):
            loaded_vlan_group = self.vlan_group(
                pk=int(item["id"]),
                name=item["name"],
                description=item["description"] if item["description"] else "",
            )
            self.job.logger.info(f"Loaded VLAN Group: {loaded_vlan_group}")
            self.add(loaded_vlan_group)

    def load(self):
        self.load_vlan_groups()
        self.load_vlans()

    def post(self, path, data):
        """Send an appropriately constructed HTTP POST request."""
        self.job.logger.info(f"POST Data: {data}")
        response = requests.post(
            f"{self.url}{path}",
            headers=self.headers,
            json=data,
            timeout=60,
            verify=False,
        )
        response.raise_for_status()
        return response

    def patch(self, path, data):
        """Send an appropriately constructed HTTP PATCH request."""
        self.job.logger.info(f"PATCH Data: {data}")
        response = requests.patch(
            f"{self.url}{path}",
            headers=self.headers,
            json=data,
            timeout=60,
            verify=False,
        )
        response.raise_for_status()
        return response

    def delete(self, path):
        self.job.logger.info("DELETE")
        """Send an appropriately constructed HTTP DELETE request."""
        response = requests.delete(
            f"{self.url}{path}", headers=self.headers, timeout=60, verify=False
        )
        response.raise_for_status()
        return response


# Step 4 - The Job
class ExampleDataTarget(DataTarget, Job):
    """SSoT Job class."""

    target_url = StringVar(
        description="URL for remote Netbox", default="https://192.168.31.50"
    )
    target_token = StringVar(
        description="REST API authentication token for remote Netbox",
        default="8db35b613cb8ccf5a390cc602ea984a6da4bbaa9",
    )

    def __init__(self):
        super().__init__()
        # self.diffsync_flags = (self.diffsync_flags | DiffSyncFlags.SKIP_UNMATCHED_DST)

    class Meta:
        name = "Sync VLANs from Nautobot to Netbox"
        description = "SSoT Example Data Target"
        has_sensitive_variables = False
        data_target = "Netbox (remote)"

    @classmethod
    def data_mappings(cls):
        """This Job maps objects from the remote system to the local system."""
        return (DataMapping("VLANs", reverse("ipam:vlan_list"), "VLANs", None),)

    def run(self, target_url, target_token, dryrun, memory_profiling, *args, **kwargs):
        self.target_url = target_url
        self.target_token = target_token
        self.dryrun = dryrun
        self.memory_profiling = memory_profiling
        super().run(dryrun, memory_profiling, *args, **kwargs)

    def load_source_adapter(self):
        self.source_adapter = MySSoTNautobotAdapter(job=self, sync=self.sync)
        self.source_adapter.load()

    def load_target_adapter(self):
        self.target_adapter = MySSoTRemoteAdapter(
            url=self.target_url, token=self.target_token, job=self
        )
        self.target_adapter.load()


register_jobs(ExampleDataTarget)
