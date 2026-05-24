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
    def __init__(self, nb: Any, data: dict[str, Any], mode: str = "apply"):
        self.nb = nb
        self.data = data
        self.namespace = None
        self.mode = mode
        self.actions: list[dict[str, Any]] = []
        self._planned_named_objects: dict[tuple[int, str], Any] = {}
        self._planned_device_types_by_model: dict[str, Any] = {}

    @property
    def is_plan(self) -> bool:
        return self.mode == "plan"

    @staticmethod
    def _normalize_for_compare(value: Any) -> Any:
        if hasattr(value, "id"):
            return getattr(value, "id")

        if isinstance(value, dict):
            if "id" in value:
                return value["id"]
            if "value" in value and len(value) <= 2:
                return value["value"]
            return {k: NautobotDataLoader._normalize_for_compare(v) for k, v in value.items()}

        if isinstance(value, (list, tuple, set)):
            normalized = [NautobotDataLoader._normalize_for_compare(v) for v in value]
            return sorted(normalized, key=lambda item: str(item))

        return value

    def _object_matches_filter(self, obj: Any, filter_kwargs: dict[str, Any]) -> bool:
        for key, expected in filter_kwargs.items():
            if key.endswith("_id"):
                rel_attr = key[:-3]
                rel_obj = getattr(obj, rel_attr, None)
                actual = getattr(rel_obj, "id", rel_obj)
            else:
                actual = getattr(obj, key, None)
            norm_actual = self._normalize_for_compare(actual)
            norm_expected = self._normalize_for_compare(expected)
            if norm_actual == norm_expected:
                continue
            if str(norm_actual) == str(norm_expected):
                continue
            return False
        return True

    def _record_action(self, action: str, object_type: str, identity: str, fields: list[str] | None = None) -> None:
        self.actions.append(
            {
                "action": action,
                "object_type": object_type,
                "identity": identity,
                "fields": fields or [],
            }
        )

    @staticmethod
    def _build_planned_object(payload: dict[str, Any], synthetic_id: str) -> Any:
        class PlannedObject:
            def __init__(self, data: dict[str, Any], sid: str):
                self.id = data.get("id", sid)
                for key, value in data.items():
                    setattr(self, key, value)

            def update(self, patch: dict[str, Any]) -> None:
                for key, value in patch.items():
                    setattr(self, key, value)

            def serialize(self) -> dict[str, Any]:
                return self.__dict__.copy()

        return PlannedObject(payload, synthetic_id)

    def _cache_identity(self, endpoint: Any, filter_kwargs: dict[str, Any], obj: Any, data: dict[str, Any]) -> None:
        endpoint_key = id(endpoint)
        name = filter_kwargs.get("name")
        if isinstance(name, str):
            self._planned_named_objects[(endpoint_key, name)] = obj

        model = filter_kwargs.get("model") or data.get("model")
        if isinstance(model, str):
            self._planned_device_types_by_model[model] = obj

    def update_if_needed(
        self,
        obj: Any,
        payload: dict[str, Any],
        *,
        object_type: str,
        identity: str,
        compare_keys: list[str] | None = None,
    ) -> None:
        keys = compare_keys or list(payload.keys())
        update_payload: dict[str, Any] = {}
        for key in keys:
            desired = payload.get(key)
            current = getattr(obj, key, None)
            if self._normalize_for_compare(current) != self._normalize_for_compare(desired):
                update_payload[key] = desired

        if not update_payload:
            self._record_action("noop", object_type, identity)
            return

        if not self.is_plan:
            obj.update(update_payload)
        self._record_action("update", object_type, identity, sorted(update_payload.keys()))

    def create_or_get(
        self,
        endpoint: Any,
        filter_kwargs: dict[str, Any],
        data: dict[str, Any],
        *,
        object_type: str = "object",
        identity: str | None = None,
        update_existing: bool = True,
        compare_keys: list[str] | None = None,
    ) -> Any:
        object_id = identity or str(filter_kwargs)
        existing = [obj for obj in endpoint.filter(**filter_kwargs) if self._object_matches_filter(obj, filter_kwargs)]
        if existing:
            obj = existing[0]
            self._cache_identity(endpoint, filter_kwargs, obj, data)
            if update_existing:
                self.update_if_needed(
                    obj,
                    data,
                    object_type=object_type,
                    identity=object_id,
                    compare_keys=compare_keys,
                )
            else:
                self._record_action("noop", object_type, object_id)
            return obj

        if self.is_plan:
            synthetic_id = f"planned-{object_type}-{len(self.actions) + 1}"
            obj = self._build_planned_object(data, synthetic_id)
        else:
            obj = endpoint.create(data)
        self._cache_identity(endpoint, filter_kwargs, obj, data)
        self._record_action("create", object_type, object_id, sorted(data.keys()))
        return obj

    def get_by_name(self, endpoint: Any, name: str, label: str) -> Any:
        cached = self._planned_named_objects.get((id(endpoint), name))
        if cached:
            return cached
        obj = endpoint.get(name=name)
        if not obj:
            raise RuntimeError(f"{label} '{name}' not found")
        self._planned_named_objects[(id(endpoint), name)] = obj
        return obj

    def get_device_type_by_model(self, model: str) -> Any:
        cached = self._planned_device_types_by_model.get(model)
        if cached:
            return cached
        matches = self.nb.dcim.device_types.filter(model=model)
        if not matches:
            raise RuntimeError(f"DeviceType model '{model}' not found")
        self._planned_device_types_by_model[model] = matches[0]
        return matches[0]

    def get_interface(self, device_id: str, interface_name: str) -> Any:
        matches = self.nb.dcim.interfaces.filter(device_id=device_id, name=interface_name)
        if not matches:
            raise RuntimeError(f"Interface '{interface_name}' not found on device id '{device_id}'")
        return matches[0]

    def _desired_interface_def(self, device_name: str, interface_name: str) -> dict[str, Any] | None:
        for device in self.data.get("devices", []):
            if device.get("name") != device_name:
                continue
            for interface in device.get("interfaces", []):
                if interface.get("name") == interface_name:
                    return interface
        return None

    def _resolve_content_type_ids(self, raw_content_types: list[Any]) -> list[Any]:
        resolved: list[Any] = []
        for item in raw_content_types:
            if isinstance(item, str):
                app_label, model = item.split(".", 1)
                ct = self.nb.extras.content_types.get(app_label=app_label, model=model)
                if not ct:
                    raise RuntimeError(f"ContentType '{item}' not found")
                resolved.append(ct.id)
                continue

            if isinstance(item, dict):
                if item.get("id"):
                    resolved.append(item["id"])
                    continue
                if item.get("app_label") and item.get("model"):
                    ct = self.nb.extras.content_types.get(
                        app_label=item["app_label"],
                        model=item["model"],
                    )
                    if not ct:
                        raise RuntimeError(
                            f"ContentType '{item['app_label']}.{item['model']}' not found"
                        )
                    resolved.append(ct.id)
                    continue

            resolved.append(item)

        return resolved

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
        self.namespace = self.create_or_get(
            self.nb.ipam.namespaces,
            {"name": namespace_name},
            {"name": namespace_name},
            object_type="Namespace",
            identity=namespace_name,
        )
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

            obj = self.create_or_get(
                self.nb.dcim.location_types,
                {"name": item["name"]},
                payload,
                object_type="LocationType",
                identity=item["name"],
            )
            location_type_ids[item["name"]] = obj.id

            if item.get("content_types"):
                existing_cts = [
                    ct if isinstance(ct, str) else f"{ct['app_label']}.{ct['model']}"
                    for ct in (obj.content_types or [])
                ]
                merged = sorted(set(existing_cts + item["content_types"]))
                if merged != sorted(existing_cts):
                    self.update_if_needed(
                        obj,
                        {"content_types": merged},
                        object_type="LocationType",
                        identity=item["name"],
                        compare_keys=["content_types"],
                    )

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
                object_type="Location",
                identity=item["name"],
            )
            print(f"  Location: {item['name']}")

    def ensure_roles(self) -> None:
        for item in self.data.get("roles", []):
            self.create_or_get(
                self.nb.extras.roles,
                {"name": item["name"]},
                item,
                object_type="Role",
                identity=item["name"],
            )
            print(f"  Role: {item['name']}")

    def ensure_manufacturers(self) -> None:
        for name in self.data.get("manufacturers", []):
            self.create_or_get(
                self.nb.dcim.manufacturers,
                {"name": name},
                {"name": name},
                object_type="Manufacturer",
                identity=name,
            )
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
                object_type="DeviceType",
                identity=item["model"],
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
            self.create_or_get(
                self.nb.dcim.platforms,
                {"name": item["name"]},
                payload,
                object_type="Platform",
                identity=item["name"],
            )
            print(f"  Platform: {item['name']}")

    def ensure_prefixes(self) -> None:
        for item in self.data.get("prefixes", []):
            payload = dict(item)
            payload["namespace"] = self.namespace.id
            self.create_or_get(
                self.nb.ipam.prefixes,
                {"prefix": item["prefix"], "namespace": self.namespace.id},
                payload,
                object_type="Prefix",
                identity=item["prefix"],
            )
            print(f"  Prefix: {item['prefix']}")

    def ensure_vlans(self) -> None:
        for item in self.data.get("vlans", []):
            self.create_or_get(
                self.nb.ipam.vlans,
                {"vid": item["vid"]},
                item,
                object_type="VLAN",
                identity=str(item["vid"]),
            )
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
            self.create_or_get(
                self.nb.extras.config_contexts,
                {"name": item["name"]},
                payload,
                object_type="ConfigContext",
                identity=item["name"],
            )
            print(f"  ConfigContext: {item['name']}")

    def ensure_custom_fields(self) -> None:
        for item in self.data.get("custom_fields", []):
            existing = []
            try:
                existing = list(self.nb.extras.custom_fields.filter(name=item["name"]))
            except Exception:
                existing = []

            if existing:
                obj = existing[0]
                self.update_if_needed(
                    obj,
                    item,
                    object_type="CustomField",
                    identity=item["name"],
                    compare_keys=["label", "type", "default", "description", "content_types"],
                )
            else:
                if not self.is_plan:
                    self.nb.extras.custom_fields.create(item)
                self._record_action("create", "CustomField", item["name"], sorted(item.keys()))
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
            object_type="SecretsGroup",
            identity=sg_cfg["name"],
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
                object_type="SecretsGroupAssociation",
                identity=f"{sg_cfg['name']}:{assoc['access_type']}:{assoc['secret_type']}",
                update_existing=False,
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
            device = self.create_or_get(
                self.nb.dcim.devices,
                {"name": item["name"]},
                payload,
                object_type="Device",
                identity=item["name"],
            )

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

            self.update_if_needed(device, update_payload, object_type="Device", identity=item["name"])
            display_primary = primary_ip4 or primary_ip6 or item.get("primary_ip", "none")
            print(f"  Device: {item['name']} ({display_primary})")

    def ensure_cables(self) -> None:
        for item in self.data.get("cables", []):
            a_device = self.get_by_name(self.nb.dcim.devices, item["a_device"], "Device")
            b_device = self.get_by_name(self.nb.dcim.devices, item["b_device"], "Device")

            try:
                a_intf = self.get_interface(a_device.id, item["a_interface"])
            except RuntimeError:
                desired_a = self._desired_interface_def(item["a_device"], item["a_interface"])
                if not desired_a:
                    raise
                a_intf = self.ensure_interface(a_device.id, desired_a)

            try:
                b_intf = self.get_interface(b_device.id, item["b_interface"])
            except RuntimeError:
                desired_b = self._desired_interface_def(item["b_device"], item["b_interface"])
                if not desired_b:
                    raise
                b_intf = self.ensure_interface(b_device.id, desired_b)

            cable_identity = f"{item['a_device']}:{item['a_interface']}<->{item['b_device']}:{item['b_interface']}"

            payload = {
                "termination_a_type": "dcim.interface",
                "termination_a_id": a_intf.id,
                "termination_b_type": "dcim.interface",
                "termination_b_id": b_intf.id,
                "status": item.get("status", "Connected"),
                "type": item.get("type", "cat6"),
            }

            cable_exists = False
            for cable in self.nb.dcim.cables.filter(limit=1000):
                data = cable.serialize()
                terms = {str(data.get("termination_a")), str(data.get("termination_b"))}
                if terms == {str(a_intf.id), str(b_intf.id)}:
                    cable_exists = True
                    break

            if self.is_plan:
                if cable_exists:
                    self._record_action("noop", "Cable", cable_identity)
                else:
                    self._record_action("create", "Cable", cable_identity, sorted(payload.keys()))
                print(
                    f"  Cable: {item['a_device']}:{item['a_interface']} <-> "
                    f"{item['b_device']}:{item['b_interface']}"
                )
                continue

            try:
                self.nb.dcim.cables.create(payload)
                self._record_action("create", "Cable", cable_identity, sorted(payload.keys()))
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
                self._record_action("noop", "Cable", cable_identity)

            print(
                f"  Cable: {item['a_device']}:{item['a_interface']} <-> "
                f"{item['b_device']}:{item['b_interface']}"
            )

    def _managed_devices(self) -> dict[str, Any]:
        managed: dict[str, Any] = {}
        for item in self.data.get("devices", []):
            dev = self.nb.dcim.devices.get(name=item["name"])
            if dev:
                managed[item["name"]] = dev
        return managed

    def _desired_interface_sets(self) -> tuple[dict[str, set[str]], dict[tuple[str, str], set[str]]]:
        desired_interfaces: dict[str, set[str]] = {}
        desired_ips: dict[tuple[str, str], set[str]] = {}
        for dev in self.data.get("devices", []):
            dev_name = dev["name"]
            desired_interfaces[dev_name] = set()
            for iface in dev.get("interfaces", []):
                iface_name = iface["name"]
                desired_interfaces[dev_name].add(iface_name)
                desired_ips[(dev_name, iface_name)] = {ip["address"] for ip in iface.get("ip_addresses", [])}
        return desired_interfaces, desired_ips

    def _desired_cable_pairs(self) -> set[frozenset[str]]:
        pairs: set[frozenset[str]] = set()
        for item in self.data.get("cables", []):
            a = f"{item['a_device']}:{item['a_interface']}"
            b = f"{item['b_device']}:{item['b_interface']}"
            pairs.add(frozenset([a, b]))
        return pairs

    def _desired_config_context_names(self) -> set[str]:
        return {item["name"] for item in self.data.get("config_contexts", [])}

    def _desired_vlan_vids(self) -> set[int]:
        return {int(item["vid"]) for item in self.data.get("vlans", [])}

    def _desired_prefixes(self) -> set[str]:
        return {item["prefix"] for item in self.data.get("prefixes", [])}

    def _desired_role_names(self) -> set[str]:
        return {item["name"] for item in self.data.get("roles", [])}

    def _desired_platform_names(self) -> set[str]:
        return {item["name"] for item in self.data.get("platforms", [])}

    def _desired_device_type_models(self) -> set[str]:
        return {item["model"] for item in self.data.get("device_types", [])}

    def _desired_manufacturer_names(self) -> set[str]:
        return set(self.data.get("manufacturers", []))

    def _desired_location_names(self) -> set[str]:
        return {item["name"] for item in self.data.get("locations", [])}

    def _desired_location_type_names(self) -> set[str]:
        return {item["name"] for item in self.data.get("location_types", [])}

    @staticmethod
    def _safe_delete(obj: Any) -> bool:
        try:
            obj.delete()
            return True
        except Exception as exc:
            print(f"  WARN: failed to delete {obj}: {exc}")
            return False

    def prune_managed_global_state(self) -> None:
        desired_ctx_names = self._desired_config_context_names()
        desired_vlan_vids = self._desired_vlan_vids()
        desired_prefixes = self._desired_prefixes()
        desired_roles = self._desired_role_names()
        desired_platforms = self._desired_platform_names()
        desired_device_types = self._desired_device_type_models()
        desired_manufacturers = self._desired_manufacturer_names()
        desired_locations = self._desired_location_names()
        desired_location_types = self._desired_location_type_names()

        for ctx in self.nb.extras.config_contexts.filter(limit=1000):
            name = getattr(ctx, "name", None)
            if not name or name in desired_ctx_names:
                continue
            if not self.is_plan:
                ctx.delete()
            self._record_action("delete", "ConfigContext", name)

        for vlan in self.nb.ipam.vlans.filter(limit=1000):
            vid = int(getattr(vlan, "vid", 0))
            if vid in desired_vlan_vids:
                continue
            if not self.is_plan:
                vlan.delete()
            self._record_action("delete", "VLAN", str(vid))

        for prefix in self.nb.ipam.prefixes.filter(namespace=self.namespace.id, limit=1000):
            value = getattr(prefix, "prefix", None)
            if not value or value in desired_prefixes:
                continue
            if not self.is_plan:
                prefix.delete()
            self._record_action("delete", "Prefix", value)

        for role in self.nb.extras.roles.filter(limit=1000):
            name = getattr(role, "name", None)
            if not name or name in desired_roles:
                continue
            if not self.is_plan:
                if not self._safe_delete(role):
                    continue
            self._record_action("delete", "Role", name)

        for platform in self.nb.dcim.platforms.filter(limit=1000):
            name = getattr(platform, "name", None)
            if not name or name in desired_platforms:
                continue
            if not self.is_plan:
                if not self._safe_delete(platform):
                    continue
            self._record_action("delete", "Platform", name)

        for dtype in self.nb.dcim.device_types.filter(limit=1000):
            model = getattr(dtype, "model", None)
            if not model or model in desired_device_types:
                continue
            if not self.is_plan:
                if not self._safe_delete(dtype):
                    continue
            self._record_action("delete", "DeviceType", model)

        for manufacturer in self.nb.dcim.manufacturers.filter(limit=1000):
            name = getattr(manufacturer, "name", None)
            if not name or name in desired_manufacturers:
                continue
            if not self.is_plan:
                if not self._safe_delete(manufacturer):
                    continue
            self._record_action("delete", "Manufacturer", name)

        for location in self.nb.dcim.locations.filter(limit=1000):
            name = getattr(location, "name", None)
            if not name or name in desired_locations:
                continue
            if not self.is_plan:
                if not self._safe_delete(location):
                    continue
            self._record_action("delete", "Location", name)

        for location_type in self.nb.dcim.location_types.filter(limit=1000):
            name = getattr(location_type, "name", None)
            if not name or name in desired_location_types:
                continue
            if not self.is_plan:
                if not self._safe_delete(location_type):
                    continue
            self._record_action("delete", "LocationType", name)

    def prune_managed_network_state(self) -> None:
        managed_devices = self._managed_devices()
        desired_interfaces, desired_ips = self._desired_interface_sets()
        desired_cables = self._desired_cable_pairs()

        managed_iface_id_to_label: dict[str, str] = {}
        managed_iface_label_to_obj: dict[str, Any] = {}

        for dev_name, dev_obj in managed_devices.items():
            for iface in self.nb.dcim.interfaces.filter(device_id=dev_obj.id):
                label = f"{dev_name}:{iface.name}"
                managed_iface_id_to_label[str(iface.id)] = label
                managed_iface_label_to_obj[label] = iface

        # Delete stale cables first, otherwise interface deletion may fail due to in-use links.
        for cable in self.nb.dcim.cables.filter(limit=1000):
            data = cable.serialize()
            a_label = managed_iface_id_to_label.get(str(data.get("termination_a")))
            b_label = managed_iface_id_to_label.get(str(data.get("termination_b")))
            if not a_label or not b_label:
                continue

            key = frozenset([a_label, b_label])
            if key not in desired_cables:
                if not self.is_plan:
                    cable.delete()
                self._record_action("delete", "Cable", " <-> ".join(sorted(key)))

        # Remove stale interface IP assignments (and orphaned IP objects when no bindings remain).
        for (dev_name, iface_name), wanted_addrs in desired_ips.items():
            iface = managed_iface_label_to_obj.get(f"{dev_name}:{iface_name}")
            if not iface:
                continue

            for assignment in self.nb.ipam.ip_address_to_interface.filter(interface=iface.id):
                assignment_data = assignment.serialize()
                ip_id = assignment_data.get("ip_address")
                ip_obj = self.nb.ipam.ip_addresses.get(id=ip_id)
                if not ip_obj:
                    continue

                if ip_obj.address not in wanted_addrs:
                    if not self.is_plan:
                        assignment.delete()
                    self._record_action(
                        "delete",
                        "IPAddressAssignment",
                        f"{dev_name}:{iface_name}:{ip_obj.address}",
                    )

                    remaining = list(self.nb.ipam.ip_address_to_interface.filter(ip_address=ip_obj.address))
                    if not remaining:
                        if not self.is_plan:
                            ip_obj.delete()
                        self._record_action("delete", "IPAddress", ip_obj.address)

        # Remove stale interfaces on managed devices.
        for dev_name, dev_obj in managed_devices.items():
            wanted_ifaces = desired_interfaces.get(dev_name, set())
            for iface in self.nb.dcim.interfaces.filter(device_id=dev_obj.id):
                if iface.name in wanted_ifaces:
                    continue
                if not self.is_plan:
                    iface.delete()
                self._record_action("delete", "Interface", f"{dev_name}:{iface.name}")

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
        if self.mode in ("prune", "plan"):
            self.prune_managed_network_state()
            self.prune_managed_global_state()

        summary = {
            "create": sum(1 for a in self.actions if a["action"] == "create"),
            "update": sum(1 for a in self.actions if a["action"] == "update"),
            "noop": sum(1 for a in self.actions if a["action"] == "noop"),
            "delete": sum(1 for a in self.actions if a["action"] == "delete"),
        }
        print(
            "\n=== Data loader summary: "
            f"create={summary['create']} update={summary['update']} delete={summary['delete']} noop={summary['noop']} ==="
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Nautobot data from YAML.")
    parser.add_argument("--data-file", default=str(DEFAULT_DATA_FILE), help="Path to YAML seed data file")
    parser.add_argument("--mode", choices=["apply", "plan", "prune"], default="apply", help="Data loader mode")
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

    print("=== Nautobot data loader starting ===")
    print(f"  Data file: {args.data_file}")
    print(f"  Mode: {args.mode}")

    loader = NautobotDataLoader(nb, data, mode=args.mode)
    loader.run()

    print("\n=== Nautobot data loader complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
