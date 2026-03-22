"""Sample data-target Job."""

from django.urls import reverse
from uuid import UUID
import requests
import re
from typing import Optional, Mapping
from django.templatetags.static import static

from diffsync import DiffSync
from diffsync.enum import DiffSyncFlags
from diffsync import DiffSync, DiffSyncModel

from nautobot.extras.jobs import StringVar
from nautobot.tenancy.models import Tenant
from nautobot_ssot.contrib import NautobotModel, NautobotAdapter
from nautobot_ssot.jobs.base import DataTarget, DataMapping
from nautobot.apps.jobs import register_jobs, Job


# In a more complex Job, you would probably want to move the DiffSyncModel subclasses into a separate Python module(s).

name = "Custom Syncing"  # pylint: disable=invalid-name


def slugify(value):
    """Converts to lowercase, removes non-word characters, and converts spaces to hyphens."""
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


class BaseTenantModel(DiffSyncModel):
    """Shared data model representing a Tenant in either of the local or remote instances."""

    # Metadata about this model
    _model = Tenant
    _modelname = "tenant"
    _identifiers = ("name",)
    _children = {}

    name: str


class NautobotTenantModel(BaseTenantModel, NautobotModel):
    """DiffSync model for Tenants in Nautobot."""


class NetboxTenantModel(BaseTenantModel):
    """DiffSync model for Tenants in Netbox."""

    pk: Optional[int] = None


class TenantRemoteModel(NetboxTenantModel):
    """Implementation of Tenant create/update/delete methods for updating remote Netbox data."""

    @classmethod
    def create(cls, diffsync, ids, attrs):
        """Create a new Tenant in remote Netbox."""
        diffsync.post(
            "/api/tenancy/tenants/",
            {"name": ids["name"], "slug": slugify(ids["name"])},
        )
        return super().create(diffsync, ids=ids, attrs=attrs)

    def update(self, attrs):
        """Updating tenants is not supported because we don't have any attributes."""
        raise NotImplementedError("Can't update tenants - they only have a name.")

    def delete(self):
        """Delete a Tenant in remote Netbox."""
        self.diffsync.delete(f"/api/tenancy/tenants/{self.pk}/")
        return super().delete()


class NetboxRemoteAdapter(DiffSync):
    """DiffSync adapter class for loading data from a remote Netbox instance using Python requests."""

    # Model classes used by this adapter class
    tenant = TenantRemoteModel

    # Top-level class labels, i.e. those classes that are handled directly rather than as children of other models
    top_level = ["tenant"]

    def __init__(self, *args, url=None, token=None, **kwargs):
        """Instantiate this class, but do not load data immediately from the remote system.

        Args:
            url (str): URL of the remote Netbox system
            token (str): REST API authentication token
            job (Job): The running Job instance that owns this DiffSync adapter instance
        """
        super().__init__(*args, **kwargs)
        if not url or not token:
            raise ValueError("Both url and token must be specified!")
        # if not url.startswith("http"):
        #     raise ValueError("The url must start with a schema.")
        self.url = url
        self.token = token
        # self.job = job
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Token {self.token}",
        }

    def _get_api_data(self, url_path: str) -> Mapping:
        """Returns data from a url_path using pagination."""
        return requests.get(
            f"{self.url}/{url_path}", headers=self.headers, timeout=60, verify=False
        ).json()

    def load_tenants(self):
        """Load Tenants data from the remote Netbox instance."""
        for tenant_entry in self._get_api_data("api/tenancy/tenants/")["results"]:
            print(tenant_entry)
            tenant = self.tenant(
                name=tenant_entry["name"],
                pk=tenant_entry["id"],
            )
            self.add(tenant)

    def load(self):
        """Load data from the remote Netbox instance."""
        self.load_tenants()

    def post(self, path, data):
        """Send an appropriately constructed HTTP POST request."""
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
        """Send an appropriately constructed HTTP DELETE request."""
        response = requests.delete(
            f"{self.url}{path}", headers=self.headers, timeout=60, verify=False
        )
        response.raise_for_status()
        return response


class NautobotLocalAdapter(NautobotAdapter):
    """DiffSync adapter class for loading data from the local Nautobot instance."""

    # Model classes used by this adapter class
    tenant = NautobotTenantModel

    # Top-level class labels, i.e. those classes that are handled directly rather than as children of other models
    top_level = ["tenant"]

    def __init__(self, *args, job, sync, **kwargs):
        super().__init__(*args, job=job, sync=sync, **kwargs)
        self.job = job
        self.sync = sync


class SyncTenants(DataTarget, Job):
    """Sync tenants from the local Nautobot instance to a remote Netbox instance."""

    target_url = StringVar(
        description="Remote Netbox instance URL", default="https://192.168.31.50"
    )
    target_token = StringVar(
        description="REST API authentication token for remote Netbox instance",
        default="8db35b613cb8ccf5a390cc602ea984a6da4bbaa9",
    )

    def __init__(self):
        """Initialize SyncTenants."""
        super().__init__()
        # Skip unmatched remote objects deletion
        # self.diffsync_flags = (
        #     self.diffsync_flags | DiffSyncFlags.SKIP_UNMATCHED_DST
        # )

    class Meta:
        """Metaclass attributes of SyncTenants."""

        name = "Sync Tenants from Nautobot to Netbox"
        description = "SSoT Example Data Target"
        has_sensitive_variables = False
        data_target = "Netbox (remote)"
        # data_target_icon = static("img/nautobot_logo.png")

    @classmethod
    def data_mappings(cls):
        """This Job maps Tenant objects from the local system to the remote system."""
        return (
            DataMapping(
                "Tenant (local)",
                reverse("tenancy:tenant_list"),
                "Tenant (remote)",
                None,
            ),
        )

    def run(self, target_url, target_token, dryrun, memory_profiling, *args, **kwargs):
        self.target_url = target_url
        self.target_token = target_token
        self.dryrun = dryrun
        self.memory_profiling = memory_profiling
        super().run(dryrun, memory_profiling, *args, **kwargs)

    def load_source_adapter(self):
        """Method to instantiate and load the SOURCE adapter into `self.source_adapter`."""
        self.source_adapter = NautobotLocalAdapter(job=self, sync=self.sync)
        self.source_adapter.load()

    def load_target_adapter(self):
        """Method to instantiate and load the TARGET adapter into `self.target_adapter`."""
        self.target_adapter = NetboxRemoteAdapter(
            url=self.target_url, token=self.target_token
        )
        self.target_adapter.load()

    def lookup_object(self, model_name, unique_id):
        """Look up a Nautobot object based on the DiffSync model name and unique ID."""
        if model_name == "tenant":
            try:
                return Tenant.objects.get(name=unique_id)
            except Tenant.DoesNotExist:
                pass
        return None


register_jobs(SyncTenants)
