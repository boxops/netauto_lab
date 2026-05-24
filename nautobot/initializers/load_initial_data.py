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
    required = {"name", "role", "device_type", "location", "platform", "secrets_group"}
    for device in data.get("devices", []):
        missing = sorted(required - set(device.keys()))
        if missing:
            raise ValueError(f"Device '{device.get('name', '<unknown>')}' is missing required keys: {', '.join(missing)}")

        for iface in device.get("interfaces", []):
            missing_iface = sorted({"name", "status", "type"} - set(iface.keys()))
            if missing_iface:
                raise ValueError(
                    f"Device '{device['name']}' has interface missing required keys: {', '.join(missing_iface)}"
                )

            for ip_item in iface.get("ip_addresses", []):
                if "address" not in ip_item:
                    raise ValueError(
                        f"Device '{device['name']}' interface '{iface['name']}' has ip_addresses entry without 'address'"
                    )


def validate_cable_definitions(data: dict[str, Any]) -> None:
    required = {"a_device", "a_interface", "b_device", "b_interface"}
    for idx, cable in enumerate(data.get("cables", []), start=1):
        missing = sorted(required - set(cable.keys()))
        if missing:
            raise ValueError(f"Cable #{idx} is missing required keys: {', '.join(missing)}")


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

    def get_interface(self, device_id: str, interface_name: str) -> Any:
        matches = self.nb.dcim.interfaces.filter(device_id=device_id, name=interface_name)
        if not matches:
            raise RuntimeError(f"Interface '{interface_name}' not found on device id '{device_id}'")
        return matches[0]

    def ensure_interface(self, device_id: str, interface_def: dict[str, Any]) -> Any:
        payload = {
            "device": device_id,
            "name": interface_def["name"],
            "type": interface_def["type"],
            "status": interface_def["status"],
            "enabled": bool(interface_def.get("enabled", True)),
        }

        # Optional fields aligned with Nautobot API payload keys.
        for key in ["description", "mtu", "mode", "mac_address", "label"]:
            if key in interface_def:
                payload[key] = interface_def[key]

        return self.create_or_get(
            self.nb.dcim.interfaces,
            {"device_id": device_id, "name": interface_def["name"]},
            payload,
        )

    def ensure_interface_ip(self, interface: Any, ip_item: dict[str, Any]) -> Any:
        address = ip_item["address"]
        ip_obj = None
        for candidate in self.nb.ipam.ip_addresses.filter(address=address):
            assigned_obj = getattr(candidate, "assigned_object", None)
            if assigned_obj and getattr(assigned_obj, "id", None) == interface.id:
                ip_obj = candidate
                break

        if ip_obj is None:
            payload = {
                "address": address,
                "namespace": self.namespace.id,
                "status": ip_item.get("status", "Active"),
            }
            if "role" in ip_item:
                payload["role"] = ip_item["role"]
            if "dns_name" in ip_item:
                payload["dns_name"] = ip_item["dns_name"]

            ip_obj = self.create_or_get(self.nb.ipam.ip_addresses, {"address": address}, payload)

        self.create_or_get(
            self.nb.ipam.ip_address_to_interface,
            {"ip_address": ip_obj.id, "interface": interface.id},
            {"ip_address": ip_obj.id, "interface": interface.id},
        )
        return ip_obj

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

            ip_by_address: dict[str, Any] = {}
            for interface_def in item.get("interfaces", []):
                interface = self.ensure_interface(device.id, interface_def)
                for ip_item in interface_def.get("ip_addresses", []):
                    ip_obj = self.ensure_interface_ip(interface, ip_item)
                    ip_by_address[ip_item["address"]] = ip_obj

            update_payload = {"platform": platform.id, "secrets_group": secrets_group.id}

            primary_ip4 = item.get("primary_ip4")
            if primary_ip4:
                ip_obj = ip_by_address.get(primary_ip4)
                if not ip_obj:
                    raise RuntimeError(f"Device '{item['name']}' primary_ip4 '{primary_ip4}' is not in any interface ip_addresses")
                update_payload["primary_ip4"] = ip_obj.id

            primary_ip6 = item.get("primary_ip6")
            if primary_ip6:
                ip_obj = ip_by_address.get(primary_ip6)
                if not ip_obj:
                    raise RuntimeError(f"Device '{item['name']}' primary_ip6 '{primary_ip6}' is not in any interface ip_addresses")
                update_payload["primary_ip6"] = ip_obj.id

            # Backward-compatible fallback for older YAML while transitioning to API-like keys.
            if (not primary_ip4) and (not primary_ip6) and item.get("primary_ip"):
                fallback_addr = item["primary_ip"]
                ip_obj = ip_by_address.get(fallback_addr)
                if not ip_obj:
                    raise RuntimeError(
                        f"Device '{item['name']}' primary_ip '{fallback_addr}' is not in any interface ip_addresses"
                    )
                update_payload[device_primary_ip_field(fallback_addr)] = ip_obj.id

            device.update(update_payload)
            display_primary = primary_ip4 or primary_ip6 or item.get("primary_ip", "none")
            print(f"  Device: {item['name']} ({display_primary})")

    def ensure_cables(self) -> None:
        for item in self.data.get("cables", []):
            a_device = self.get_by_name(self.nb.dcim.devices, item["a_device"], "Device")
            b_device = self.get_by_name(self.nb.dcim.devices, item["b_device"], "Device")
            a_intf = self.get_interface(a_device.id, item["a_interface"])
            b_intf = self.get_interface(b_device.id, item["b_interface"])

            payload = {
                "termination_a_type": "dcim.interface",
                "termination_a_id": a_intf.id,
                "termination_b_type": "dcim.interface",
                "termination_b_id": b_intf.id,
                "status": item.get("status", "Connected"),
                "type": item.get("type", "cat6"),
            }

            try:
                self.nb.dcim.cables.create(payload)
            except Exception as exc:
                msg = str(exc).lower()
                # Idempotency: if cable already exists or interface is already connected, continue.
                if (
                    "already" not in msg
                    and "connected" not in msg
                    and "duplicate" not in msg
                    and "unique set" not in msg
                ):
                    raise

            print(
                f"  Cable: {item['a_device']}:{item['a_interface']} <-> "
                f"{item['b_device']}:{item['b_interface']}"
            )

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
        self.ensure_cables()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Nautobot initial data from YAML.")
    parser.add_argument("--data-file", default=str(DEFAULT_DATA_FILE), help="Path to YAML seed data file")
    return parser.parse_args()


def main() -> int:
    import pynautobot

    args = parse_args()
    data = load_data(args.data_file)
    validate_device_definitions(data)
    validate_cable_definitions(data)

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
