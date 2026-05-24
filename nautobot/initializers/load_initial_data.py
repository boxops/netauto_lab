#!/usr/bin/env python3
"""Load Nautobot seed data from YAML and create automation-ready devices."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DATA_FILE = Path(__file__).with_name("data.yml")


def load_data(data_file: str | Path) -> dict[str, Any]:
    with open(data_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def device_primary_ip_field(address: str) -> str:
    return "primary_ip6" if ":" in address else "primary_ip4"


def validate_device_definitions(data: dict[str, Any]) -> None:
    required = {"name", "role", "device_type", "location", "platform", "primary_ip", "secrets_group"}
    for device in data.get("devices", []):
        missing = sorted(required - set(device.keys()))
        if missing:
            raise ValueError(f"Device '{device.get('name', '<unknown>')}' is missing required keys: {', '.join(missing)}")


class NautobotDataLoader:
    def __init__(self, nb: Any, data: dict[str, Any]):
        self.nb = nb
        self.data = data
        self.namespace = None

    @staticmethod
    def create_or_get(endpoint: Any, filter_kwargs: dict[str, Any], data: dict[str, Any]) -> Any:
        existing = endpoint.filter(**filter_kwargs)
        if existing:
            return existing[0]
        return endpoint.create(data)

    def get_by_name(self, endpoint: Any, name: str, label: str) -> Any:
        obj = endpoint.get(name=name)
        if not obj:
            raise RuntimeError(f"{label} '{name}' not found")
        return obj

    def get_device_type_by_model(self, model: str) -> Any:
        matches = self.nb.dcim.device_types.filter(model=model)
        if not matches:
            raise RuntimeError(f"DeviceType model '{model}' not found")
        return matches[0]

    def ensure_namespace(self) -> None:
        namespace_name = self.data.get("ipam_namespace", "Global")
        self.namespace = self.create_or_get(self.nb.ipam.namespaces, {"name": namespace_name}, {"name": namespace_name})
        print(f"  Namespace: {namespace_name}")

    def ensure_location_types(self) -> None:
        location_type_ids: dict[str, Any] = {}
        for item in self.data.get("location_types", []):
            payload = {
                "name": item["name"],
                "nestable": bool(item.get("nestable", False)),
            }
            parent_name = item.get("parent")
            if parent_name:
                parent_id = location_type_ids.get(parent_name)
                if not parent_id:
                    parent_obj = self.nb.dcim.location_types.get(name=parent_name)
                    if not parent_obj:
                        raise RuntimeError(f"LocationType parent '{parent_name}' must exist before '{item['name']}'")
                    parent_id = parent_obj.id
                payload["parent"] = parent_id

            if item.get("content_types"):
                payload["content_types"] = item["content_types"]

            obj = self.create_or_get(self.nb.dcim.location_types, {"name": item["name"]}, payload)
            location_type_ids[item["name"]] = obj.id

            if item.get("content_types"):
                existing_cts = [
                    ct if isinstance(ct, str) else f"{ct['app_label']}.{ct['model']}"
                    for ct in (obj.content_types or [])
                ]
                merged = sorted(set(existing_cts + item["content_types"]))
                if merged != sorted(existing_cts):
                    obj.update({"content_types": merged})

            print(f"  LocationType: {item['name']}")

    def ensure_locations(self) -> None:
        for item in self.data.get("locations", []):
            location_type = self.get_by_name(self.nb.dcim.location_types, item["location_type"], "LocationType")
            payload = {
                "name": item["name"],
                "location_type": location_type.id,
                "status": item.get("status", "Active"),
            }
            if item.get("parent"):
                payload["parent"] = self.get_by_name(self.nb.dcim.locations, item["parent"], "Location").id

            self.create_or_get(
                self.nb.dcim.locations,
                {"name": item["name"], "location_type": location_type.id},
                payload,
            )
            print(f"  Location: {item['name']}")

    def ensure_roles(self) -> None:
        for item in self.data.get("roles", []):
            self.create_or_get(self.nb.extras.roles, {"name": item["name"]}, item)
            print(f"  Role: {item['name']}")

    def ensure_manufacturers(self) -> None:
        for name in self.data.get("manufacturers", []):
            self.create_or_get(self.nb.dcim.manufacturers, {"name": name}, {"name": name})
            print(f"  Manufacturer: {name}")

    def ensure_device_types(self) -> None:
        for item in self.data.get("device_types", []):
            manufacturer = self.get_by_name(self.nb.dcim.manufacturers, item["manufacturer"], "Manufacturer")
            payload = {
                "model": item["model"],
                "manufacturer": manufacturer.id,
                "u_height": item.get("u_height", 1),
            }
            self.create_or_get(
                self.nb.dcim.device_types,
                {"model": item["model"], "manufacturer": manufacturer.id},
                payload,
            )
            print(f"  DeviceType: {item['model']}")

    def ensure_platforms(self) -> None:
        for item in self.data.get("platforms", []):
            manufacturer = self.get_by_name(self.nb.dcim.manufacturers, item["manufacturer"], "Manufacturer")
            payload = {
                "name": item["name"],
                "manufacturer": manufacturer.id,
                "network_driver": item.get("network_driver", ""),
            }
            self.create_or_get(self.nb.dcim.platforms, {"name": item["name"]}, payload)
            print(f"  Platform: {item['name']}")

    def ensure_prefixes(self) -> None:
        for item in self.data.get("prefixes", []):
            payload = dict(item)
            payload["namespace"] = self.namespace.id
            self.create_or_get(self.nb.ipam.prefixes, {"prefix": item["prefix"], "namespace": self.namespace.id}, payload)
            print(f"  Prefix: {item['prefix']}")

    def ensure_vlans(self) -> None:
        for item in self.data.get("vlans", []):
            self.create_or_get(self.nb.ipam.vlans, {"vid": item["vid"]}, item)
            print(f"  VLAN: {item['vid']} - {item['name']}")

    def ensure_config_contexts(self) -> None:
        for item in self.data.get("config_contexts", []):
            payload = {
                "name": item["name"],
                "weight": item.get("weight", 100),
                "is_active": bool(item.get("is_active", True)),
                "description": item.get("description", ""),
                "data": item.get("data", {}),
            }
            if item.get("roles"):
                payload["roles"] = [self.get_by_name(self.nb.extras.roles, n, "Role").id for n in item["roles"]]
            if item.get("platforms"):
                payload["platforms"] = [self.get_by_name(self.nb.dcim.platforms, n, "Platform").id for n in item["platforms"]]
            self.create_or_get(self.nb.extras.config_contexts, {"name": item["name"]}, payload)
            print(f"  ConfigContext: {item['name']}")

    def ensure_custom_fields(self) -> None:
        for item in self.data.get("custom_fields", []):
            try:
                self.nb.extras.custom_fields.create(item)
            except Exception as exc:
                msg = str(exc).lower()
                if "already exists" not in msg and "duplicate" not in msg:
                    raise
            print(f"  CustomField: {item['name']}")

    def ensure_secrets_and_group(self) -> None:
        secret_objs = {}
        for item in self.data.get("secrets", []):
            payload = {
                "name": item["name"],
                "provider": "environment-variable",
                "parameters": {"variable": item["variable"]},
                "description": item.get("description", ""),
            }
            secret_objs[item["name"]] = self.create_or_get(self.nb.extras.secrets, {"name": item["name"]}, payload)
            print(f"  Secret: {item['name']}")

        sg_cfg = self.data.get("secrets_group", {})
        if not sg_cfg:
            return

        sg = self.create_or_get(
            self.nb.extras.secrets_groups,
            {"name": sg_cfg["name"]},
            {"name": sg_cfg["name"], "description": sg_cfg.get("description", "")},
        )
        print(f"  SecretsGroup: {sg_cfg['name']}")

        for assoc in sg_cfg.get("associations", []):
            secret_name = assoc["secret"]
            self.create_or_get(
                self.nb.extras.secrets_groups_associations,
                {
                    "secrets_group": sg.id,
                    "access_type": assoc["access_type"],
                    "secret_type": assoc["secret_type"],
                },
                {
                    "secrets_group": sg.id,
                    "access_type": assoc["access_type"],
                    "secret_type": assoc["secret_type"],
                    "secret": secret_objs[secret_name].id,
                },
            )
            print(f"  SecretsGroupAssociation: {assoc['access_type']}/{assoc['secret_type']} -> {secret_name}")

    def ensure_devices(self) -> None:
        for item in self.data.get("devices", []):
            role = self.get_by_name(self.nb.extras.roles, item["role"], "Role")
            device_type = self.get_device_type_by_model(item["device_type"])
            location = self.get_by_name(self.nb.dcim.locations, item["location"], "Location")
            platform = self.get_by_name(self.nb.dcim.platforms, item["platform"], "Platform")
            secrets_group = self.get_by_name(self.nb.extras.secrets_groups, item["secrets_group"], "SecretsGroup")

            payload = {
                "name": item["name"],
                "role": role.id,
                "device_type": device_type.id,
                "location": location.id,
                "status": item.get("status", "Active"),
                "platform": platform.id,
                "secrets_group": secrets_group.id,
            }
            device = self.create_or_get(self.nb.dcim.devices, {"name": item["name"]}, payload)

            interface_name = item.get("management_interface", "Management0")
            interface = self.create_or_get(
                self.nb.dcim.interfaces,
                {"device_id": device.id, "name": interface_name},
                {
                    "device": device.id,
                    "name": interface_name,
                    "type": item.get("management_interface_type", "1000base-t"),
                    "status": "Active",
                    "enabled": True,
                },
            )

            primary_ip = item["primary_ip"]
            ip_obj = None
            existing_ips = list(self.nb.ipam.ip_addresses.filter(address=primary_ip))
            for candidate in existing_ips:
                assigned_obj = getattr(candidate, "assigned_object", None)
                if assigned_obj and getattr(assigned_obj, "id", None) == interface.id:
                    ip_obj = candidate
                    break

            if ip_obj is None:
                for candidate in existing_ips:
                    try:
                        candidate.delete()
                    except Exception:
                        pass

                ip_obj = self.nb.ipam.ip_addresses.create(
                    {
                        "address": primary_ip,
                        "namespace": self.namespace.id,
                        "status": "Active",
                    }
                )

            self.create_or_get(
                self.nb.ipam.ip_address_to_interface,
                {"ip_address": ip_obj.id, "interface": interface.id},
                {"ip_address": ip_obj.id, "interface": interface.id},
            )

            update_payload = {
                "platform": platform.id,
                "secrets_group": secrets_group.id,
                device_primary_ip_field(primary_ip): ip_obj.id,
            }
            device.update(update_payload)
            print(f"  Device: {item['name']} ({primary_ip})")

    def run(self) -> None:
        self.ensure_location_types()
        self.ensure_locations()
        self.ensure_roles()
        self.ensure_manufacturers()
        self.ensure_device_types()
        self.ensure_platforms()
        self.ensure_namespace()
        self.ensure_prefixes()
        self.ensure_vlans()
        self.ensure_config_contexts()
        self.ensure_custom_fields()
        self.ensure_secrets_and_group()
        self.ensure_devices()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Nautobot initial data from YAML.")
    parser.add_argument("--data-file", default=str(DEFAULT_DATA_FILE), help="Path to YAML seed data file")
    return parser.parse_args()


def main() -> int:
    import pynautobot

    args = parse_args()
    data = load_data(args.data_file)
    validate_device_definitions(data)

    nautobot_url = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
    nautobot_token = os.getenv("NAUTOBOT_SUPERUSER_API_TOKEN", "")
    nb = pynautobot.api(nautobot_url, token=nautobot_token)

    print("=== Nautobot initializer starting ===")
    print(f"  Data file: {args.data_file}")

    loader = NautobotDataLoader(nb, data)
    loader.run()

    print("\n=== Nautobot initializer complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
