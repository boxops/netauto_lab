import importlib.util
import copy
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER_FILE = REPO_ROOT / "nautobot" / "data_loader" / "load_data.py"
DATA_FILE = REPO_ROOT / "nautobot" / "data_loader" / "data.yml"
LOADER_RUN_TIMEOUT_SECONDS = 240
NAUTOBOT_READY_TIMEOUT_SECONDS = 180


spec = importlib.util.spec_from_file_location("load_data", LOADER_FILE)
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


def _wait_for_nautobot_ready() -> None:
    nautobot_url = os.getenv("NAUTOBOT_URL", "http://localhost:8080").rstrip("/")
    deadline = time.time() + NAUTOBOT_READY_TIMEOUT_SECONDS
    last_error = "unknown"

    while time.time() < deadline:
        try:
            resp = requests.get(f"{nautobot_url}/api/", timeout=5)
            if resp.status_code < 500:
                return
            last_error = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(2)

    raise AssertionError(
        "Nautobot API was not ready before running loader command. "
        f"Last error: {last_error}"
    )


def _run_loader_mode(mode: str, data_file: Path) -> None:
    _wait_for_nautobot_ready()
    container_data_file = f"/opt/nautobot/data_loader/{data_file.name}"
    base_cmd = (
        "docker compose exec -T nautobot python /opt/nautobot/data_loader/load_data.py "
        f"--data-file {container_data_file} --mode {mode}"
    )
    result = subprocess.run(
        ["bash", "-lc", base_cmd],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=LOADER_RUN_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return

    # Use non-interactive sudo fallback so tests never block waiting for a password prompt.
    sudo_result = subprocess.run(
        ["bash", "-lc", f"sudo -n {base_cmd}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=LOADER_RUN_TIMEOUT_SECONDS,
    )
    if sudo_result.returncode == 0:
        return

    primary_stderr = (result.stderr or "").strip()
    sudo_stderr = (sudo_result.stderr or "").strip()
    combined_stdout = ((result.stdout or "") + "\n" + (sudo_result.stdout or "")).strip()
    combined_stderr = "\n".join(s for s in [primary_stderr, sudo_stderr] if s)
    raise AssertionError(
        "Loader command failed via docker and sudo fallback. "
        f"docker_rc={result.returncode}, sudo_rc={sudo_result.returncode}.\n"
        f"STDERR:\n{combined_stderr or '<empty>'}\n"
        f"STDOUT:\n{combined_stdout or '<empty>'}"
    )


def _api_get_count(nautobot_url: str, headers: dict[str, str], endpoint: str, params: dict[str, str] | None = None) -> int:
    resp = requests.get(f"{nautobot_url}{endpoint}", headers=headers, params=params or {}, timeout=20)
    assert resp.status_code == 200, f"API query failed: {endpoint}"
    return int(resp.json().get("count", 0))


def _write_temp_fixture(data: dict) -> Path:
    temp_file = REPO_ROOT / "nautobot" / "data_loader" / f"data.crud.{uuid.uuid4().hex}.yml"
    temp_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return temp_file


@pytest.mark.unit
def test_load_data_yaml_exists_and_has_devices():
    data = loader.load_data(DATA_FILE)
    assert data
    assert "devices" in data
    assert len(data["devices"]) > 0


@pytest.mark.unit
def test_validate_device_definitions_rejects_missing_fields():
    bad = {
        "devices": [
            {
                "name": "leaf99",
                "role": "Leaf",
                "location": "site-lab",
            }
        ]
    }
    with pytest.raises(ValueError):
        loader.validate_device_definitions(bad)


@pytest.mark.unit
def test_validate_device_definitions_rejects_incomplete_interface():
    bad = {
        "devices": [
            {
                "name": "leaf99",
                "role": "Leaf",
                "device_type": "cEOS",
                "location": "site-lab",
                "platform": "Arista EOS",
                "secrets_group": "lab-ssh-creds",
                "interfaces": [{"name": "Eth0"}],
            }
        ]
    }
    with pytest.raises(ValueError):
        loader.validate_device_definitions(bad)


@pytest.mark.unit
def test_validate_device_definitions_rejects_interface_ip_without_address():
    bad = {
        "devices": [
            {
                "name": "leaf99",
                "role": "Leaf",
                "device_type": "cEOS",
                "location": "site-lab",
                "platform": "Arista EOS",
                "secrets_group": "lab-ssh-creds",
                "interfaces": [
                    {
                        "name": "Eth0",
                        "status": "Active",
                        "type": "1000base-t",
                        "ip_addresses": [{"status": "Active"}],
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError):
        loader.validate_device_definitions(bad)


@pytest.mark.unit
def test_validate_cable_definitions_rejects_missing_fields():
    bad = {
        "cables": [
            {
                "a_device": "spine1",
                "a_interface": "Eth1",
                "b_device": "leaf1",
            }
        ]
    }
    with pytest.raises(ValueError):
        loader.validate_cable_definitions(bad)


@pytest.mark.unit
def test_device_primary_ip_field():
    assert loader.device_primary_ip_field("10.10.10.1/32") == "primary_ip4"
    assert loader.device_primary_ip_field("2001:db8::10/128") == "primary_ip6"


@pytest.mark.unit
def test_normalize_for_compare_handles_choice_dicts():
    got = loader.NautobotDataLoader._normalize_for_compare({"value": "active", "label": "Active"})
    assert got == "active"


@pytest.mark.unit
def test_create_or_get_updates_drifted_object():
    class DummyObj:
        def __init__(self):
            self.name = "obj1"
            self.status = "Active"
            self.updated: dict[str, str] | None = None

        def update(self, payload):
            self.updated = payload
            for key, value in payload.items():
                setattr(self, key, value)

    class DummyEndpoint:
        def __init__(self):
            self.obj = DummyObj()
            self.created = None

        def filter(self, **kwargs):
            if kwargs.get("name") == "obj1":
                return [self.obj]
            return []

        def create(self, data):
            self.created = data
            return self.obj

    endpoint = DummyEndpoint()
    dl = loader.NautobotDataLoader(nb=None, data={})
    dl.create_or_get(
        endpoint,
        {"name": "obj1"},
        {"name": "obj1", "status": "Planned"},
        object_type="Dummy",
        identity="obj1",
    )

    assert endpoint.obj.updated == {"status": "Planned"}
    assert any(a["action"] == "update" and a["object_type"] == "Dummy" for a in dl.actions)


@pytest.mark.unit
def test_desired_interface_sets_extract_names_and_ips():
    data = {
        "devices": [
            {
                "name": "leaf1",
                "interfaces": [
                    {
                        "name": "Eth0",
                        "status": "Active",
                        "type": "1000base-t",
                        "ip_addresses": [{"address": "10.10.100.21/32"}],
                    },
                    {
                        "name": "Eth1",
                        "status": "Active",
                        "type": "1000base-t",
                    },
                ],
            }
        ]
    }
    dl = loader.NautobotDataLoader(nb=None, data=data)
    iface_sets, ip_sets = dl._desired_interface_sets()
    assert iface_sets["leaf1"] == {"Eth0", "Eth1"}
    assert ip_sets[("leaf1", "Eth0")] == {"10.10.100.21/32"}
    assert ip_sets[("leaf1", "Eth1")] == set()


@pytest.mark.unit
def test_desired_cable_pairs_are_order_independent():
    data = {
        "cables": [
            {
                "a_device": "spine1",
                "a_interface": "Eth1",
                "b_device": "leaf1",
                "b_interface": "Eth1",
            }
        ]
    }
    dl = loader.NautobotDataLoader(nb=None, data=data)
    pairs = dl._desired_cable_pairs()
    assert frozenset(["spine1:Eth1", "leaf1:Eth1"]) in pairs


@pytest.mark.unit
def test_create_or_get_plan_mode_does_not_call_create_or_update():
    class DummyObj:
        def __init__(self):
            self.name = "obj1"
            self.status = "Active"
            self.update_called = False

        def update(self, payload):
            self.update_called = True

    class DummyEndpoint:
        def __init__(self):
            self.obj = DummyObj()
            self.create_called = False

        def filter(self, **kwargs):
            if kwargs.get("name") == "obj1":
                return [self.obj]
            return []

        def create(self, data):
            self.create_called = True
            return DummyObj()

    endpoint = DummyEndpoint()
    dl = loader.NautobotDataLoader(nb=None, data={}, mode="plan")
    dl.create_or_get(
        endpoint,
        {"name": "obj1"},
        {"name": "obj1", "status": "Planned"},
        object_type="Dummy",
        identity="obj1",
    )

    assert endpoint.obj.update_called is False
    assert endpoint.create_called is False
    assert any(a["action"] == "update" and a["object_type"] == "Dummy" for a in dl.actions)


@pytest.mark.unit
def test_create_or_get_plan_mode_returns_planned_object_for_create():
    class DummyEndpoint:
        def filter(self, **kwargs):
            return []

        def create(self, data):
            raise AssertionError("create() should not be called in plan mode")

    endpoint = DummyEndpoint()
    dl = loader.NautobotDataLoader(nb=None, data={}, mode="plan")
    obj = dl.create_or_get(
        endpoint,
        {"name": "new1"},
        {"name": "new1", "status": "Active"},
        object_type="Dummy",
        identity="new1",
    )

    assert getattr(obj, "name", "") == "new1"
    assert str(getattr(obj, "id", "")).startswith("planned-Dummy-")
    assert any(a["action"] == "create" and a["identity"] == "new1" for a in dl.actions)


@pytest.mark.integration
def test_loader_integration_devices_are_automation_ready():
    nautobot_url = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
    nautobot_token = os.getenv("NAUTOBOT_SUPERUSER_API_TOKEN", "")
    if not nautobot_token:
        pytest.skip("NAUTOBOT_SUPERUSER_API_TOKEN not set")

    data = yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))
    headers = {"Authorization": f"Token {nautobot_token}"}

    for dev in data.get("devices", []):
        resp = requests.get(
            f"{nautobot_url}/api/dcim/devices/",
            params={"name": dev["name"]},
            headers=headers,
            timeout=15,
        )
        assert resp.status_code == 200, f"device query failed for {dev['name']}"
        payload = resp.json()
        assert payload.get("count", 0) >= 1, f"device {dev['name']} not created"

        obj = payload["results"][0]
        assert obj.get("platform") is not None, f"device {dev['name']} missing platform"
        assert obj.get("secrets_group") is not None, f"device {dev['name']} missing secrets group"
        assert (obj.get("primary_ip4") is not None) or (obj.get("primary_ip6") is not None), (
            f"device {dev['name']} missing primary IP"
        )

        for iface in dev.get("interfaces", []):
            i_resp = requests.get(
                f"{nautobot_url}/api/dcim/interfaces/",
                params={"device_id": obj["id"], "name": iface["name"]},
                headers=headers,
                timeout=15,
            )
            assert i_resp.status_code == 200, f"interface query failed for {dev['name']}:{iface['name']}"
            i_payload = i_resp.json()
            assert i_payload.get("count", 0) >= 1, f"interface {dev['name']}:{iface['name']} not created"

    if data.get("cables"):
        c_resp = requests.get(
            f"{nautobot_url}/api/dcim/cables/",
            params={"limit": 1000},
            headers=headers,
            timeout=15,
        )
        assert c_resp.status_code == 200, "cable query failed"
        c_payload = c_resp.json()
        assert c_payload.get("count", 0) >= len(data["cables"]), "expected cable records not found"


@pytest.mark.integration
def test_loader_plan_mode_is_non_mutating_for_counts():
    nautobot_url = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
    nautobot_token = os.getenv("NAUTOBOT_SUPERUSER_API_TOKEN", "")
    if not nautobot_token:
        pytest.skip("NAUTOBOT_SUPERUSER_API_TOKEN not set")

    # Start from a known desired-state baseline so count comparisons are stable.
    _run_loader_mode("prune", DATA_FILE)

    headers = {"Authorization": f"Token {nautobot_token}"}
    before = {
        "devices": _api_get_count(nautobot_url, headers, "/api/dcim/devices/"),
        "interfaces": _api_get_count(nautobot_url, headers, "/api/dcim/interfaces/"),
        "cables": _api_get_count(nautobot_url, headers, "/api/dcim/cables/"),
        "ip_assignments": _api_get_count(nautobot_url, headers, "/api/ipam/ip-address-to-interface/"),
    }

    _run_loader_mode("plan", DATA_FILE)

    after = {
        "devices": _api_get_count(nautobot_url, headers, "/api/dcim/devices/"),
        "interfaces": _api_get_count(nautobot_url, headers, "/api/dcim/interfaces/"),
        "cables": _api_get_count(nautobot_url, headers, "/api/dcim/cables/"),
        "ip_assignments": _api_get_count(nautobot_url, headers, "/api/ipam/ip-address-to-interface/"),
    }

    assert after == before, "plan mode should not mutate object counts"


@pytest.mark.integration
def test_loader_crud_for_managed_network_scope_with_temp_fixture():
    nautobot_url = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
    nautobot_token = os.getenv("NAUTOBOT_SUPERUSER_API_TOKEN", "")
    if not nautobot_token:
        pytest.skip("NAUTOBOT_SUPERUSER_API_TOKEN not set")

    headers = {"Authorization": f"Token {nautobot_token}"}
    base_data = yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))
    create_update_data = copy.deepcopy(base_data)

    leaf1 = next(d for d in create_update_data["devices"] if d["name"] == "leaf1")
    spine1 = next(d for d in create_update_data["devices"] if d["name"] == "spine1")

    leaf1_eth1 = next(i for i in leaf1["interfaces"] if i["name"] == "Eth1")
    leaf1_eth1["description"] = "crud-updated-desc"

    leaf1["interfaces"].append(
        {
            "name": "Eth99",
            "status": "Active",
            "type": "1000base-t",
            "enabled": True,
            "ip_addresses": [{"address": "10.10.199.21/32", "status": "Active"}],
        }
    )
    spine1["interfaces"].append(
        {
            "name": "Eth99",
            "status": "Active",
            "type": "1000base-t",
            "enabled": True,
        }
    )
    create_update_data["cables"].append(
        {
            "a_device": "spine1",
            "a_interface": "Eth99",
            "b_device": "leaf1",
            "b_interface": "Eth99",
            "type": "cat6",
            "status": "Connected",
        }
    )
    create_update_data["vlans"].append(
        {
            "vid": 999,
            "name": "crud-temp-vlan",
            "status": "Active",
        }
    )
    create_update_data["prefixes"].append(
        {
            "prefix": "10.254.254.0/24",
            "status": "Active",
            "description": "crud-temp-prefix",
        }
    )
    create_update_data["config_contexts"].append(
        {
            "name": "crud-temp-context",
            "weight": 300,
            "is_active": True,
            "description": "CRUD temporary config context",
            "data": {"crud": {"enabled": True}},
        }
    )
    create_update_data["roles"].append(
        {
            "name": "CRUD-Role",
            "color": "ff00aa",
            "content_types": ["dcim.device"],
        }
    )
    create_update_data["platforms"].append(
        {
            "name": "CRUD Platform",
            "manufacturer": "Cisco Systems",
            "network_driver": "crud_driver",
        }
    )
    create_update_data["manufacturers"].append("CRUD Manufacturer")
    create_update_data["device_types"].append(
        {
            "model": "CRUD Device Type",
            "manufacturer": "CRUD Manufacturer",
            "u_height": 1,
        }
    )
    create_update_data["location_types"].append(
        {
            "name": "CRUD Region",
            "nestable": True,
        }
    )
    create_update_data["locations"].append(
        {
            "name": "CRUD Location",
            "location_type": "CRUD Region",
            "status": "Active",
        }
    )

    temp_apply = _write_temp_fixture(create_update_data)
    try:
        _run_loader_mode("apply", temp_apply)

        dev_resp = requests.get(
            f"{nautobot_url}/api/dcim/devices/",
            params={"name": "leaf1"},
            headers=headers,
            timeout=20,
        )
        assert dev_resp.status_code == 200
        dev_id = dev_resp.json()["results"][0]["id"]

        eth1_resp = requests.get(
            f"{nautobot_url}/api/dcim/interfaces/",
            params={"device_id": dev_id, "name": "Eth1"},
            headers=headers,
            timeout=20,
        )
        assert eth1_resp.status_code == 200
        assert eth1_resp.json()["results"][0].get("description", "") == "crud-updated-desc"

        eth99_resp = requests.get(
            f"{nautobot_url}/api/dcim/interfaces/",
            params={"device_id": dev_id, "name": "Eth99"},
            headers=headers,
            timeout=20,
        )
        assert eth99_resp.status_code == 200
        assert eth99_resp.json().get("count", 0) >= 1

        cable_count_with_extra = _api_get_count(nautobot_url, headers, "/api/dcim/cables/")
        vlan_999_before = _api_get_count(nautobot_url, headers, "/api/ipam/vlans/", {"vid": "999"})
        prefix_before = _api_get_count(nautobot_url, headers, "/api/ipam/prefixes/", {"prefix": "10.254.254.0/24"})
        context_before = _api_get_count(
            nautobot_url,
            headers,
            "/api/extras/config-contexts/",
            {"name": "crud-temp-context"},
        )
        assert vlan_999_before >= 1
        assert prefix_before >= 1
        assert context_before >= 1

        role_before = _api_get_count(nautobot_url, headers, "/api/extras/roles/", {"name": "CRUD-Role"})
        platform_before = _api_get_count(nautobot_url, headers, "/api/dcim/platforms/", {"name": "CRUD Platform"})
        mfg_before = _api_get_count(
            nautobot_url,
            headers,
            "/api/dcim/manufacturers/",
            {"name": "CRUD Manufacturer"},
        )
        dtype_before = _api_get_count(
            nautobot_url,
            headers,
            "/api/dcim/device-types/",
            {"model": "CRUD Device Type"},
        )
        ltype_before = _api_get_count(
            nautobot_url,
            headers,
            "/api/dcim/location-types/",
            {"name": "CRUD Region"},
        )
        loc_before = _api_get_count(
            nautobot_url,
            headers,
            "/api/dcim/locations/",
            {"name": "CRUD Location"},
        )
        assert role_before >= 1
        assert platform_before >= 1
        assert mfg_before >= 1
        assert dtype_before >= 1
        assert ltype_before >= 1
        assert loc_before >= 1

        prune_data = copy.deepcopy(base_data)
        temp_prune = _write_temp_fixture(prune_data)
        try:
            _run_loader_mode("prune", temp_prune)
        finally:
            temp_prune.unlink(missing_ok=True)

        eth99_after = requests.get(
            f"{nautobot_url}/api/dcim/interfaces/",
            params={"device_id": dev_id, "name": "Eth99"},
            headers=headers,
            timeout=20,
        )
        assert eth99_after.status_code == 200
        assert eth99_after.json().get("count", 0) == 0

        cable_count_after_prune = _api_get_count(nautobot_url, headers, "/api/dcim/cables/")
        assert cable_count_after_prune < cable_count_with_extra

        vlan_999_after = _api_get_count(nautobot_url, headers, "/api/ipam/vlans/", {"vid": "999"})
        prefix_after = _api_get_count(nautobot_url, headers, "/api/ipam/prefixes/", {"prefix": "10.254.254.0/24"})
        context_after = _api_get_count(
            nautobot_url,
            headers,
            "/api/extras/config-contexts/",
            {"name": "crud-temp-context"},
        )
        assert vlan_999_after == 0
        assert prefix_after == 0
        assert context_after == 0

        role_after = _api_get_count(nautobot_url, headers, "/api/extras/roles/", {"name": "CRUD-Role"})
        platform_after = _api_get_count(nautobot_url, headers, "/api/dcim/platforms/", {"name": "CRUD Platform"})
        mfg_after = _api_get_count(
            nautobot_url,
            headers,
            "/api/dcim/manufacturers/",
            {"name": "CRUD Manufacturer"},
        )
        dtype_after = _api_get_count(
            nautobot_url,
            headers,
            "/api/dcim/device-types/",
            {"model": "CRUD Device Type"},
        )
        ltype_after = _api_get_count(
            nautobot_url,
            headers,
            "/api/dcim/location-types/",
            {"name": "CRUD Region"},
        )
        loc_after = _api_get_count(
            nautobot_url,
            headers,
            "/api/dcim/locations/",
            {"name": "CRUD Location"},
        )
        assert role_after == 0
        assert platform_after == 0
        assert mfg_after == 0
        assert dtype_after == 0
        assert ltype_after == 0
        assert loc_after == 0
    finally:
        temp_apply.unlink(missing_ok=True)
