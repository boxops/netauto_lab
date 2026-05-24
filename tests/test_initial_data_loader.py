import importlib.util
import os
from pathlib import Path

import pytest
import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER_FILE = REPO_ROOT / "nautobot" / "initializers" / "load_initial_data.py"
DATA_FILE = REPO_ROOT / "nautobot" / "initializers" / "data.yml"


spec = importlib.util.spec_from_file_location("load_initial_data", LOADER_FILE)
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


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
def test_device_primary_ip_field():
    assert loader.device_primary_ip_field("10.10.10.1/32") == "primary_ip4"
    assert loader.device_primary_ip_field("2001:db8::10/128") == "primary_ip6"


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
