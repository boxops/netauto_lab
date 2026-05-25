#!/usr/bin/env python3
"""Load Nautobot seed data from YAML and create automation-ready devices."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse
import uuid

import yaml

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from state_store import StateStore
from cache_store import CacheStore
from cache_refresh import refresh_cache_snapshot

DEFAULT_DATA_FILE = Path(__file__).with_name("data.yml")
DEFAULT_STATE_FILE = Path(os.getenv("DATA_LOADER_STATE_FILE", "/tmp/nautobot-data-loader.state.json"))
DEFAULT_CACHE_FILE = Path(os.getenv("DATA_LOADER_CACHE_FILE", "/tmp/nautobot-data-loader.cache.json"))


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
    def __init__(
        self,
        nb: Any,
        data: dict[str, Any],
        mode: str = "apply",
        cache_mode: str = "off",
        cache_payload: dict[str, Any] | None = None,
    ):
        self.nb = nb
        self.data = data
        self.namespace = None
        self.mode = mode
        self.cache_mode = cache_mode
        self.cache_payload = cache_payload
        # Experimental optimization: keep off by default until validated per Nautobot version.
        self.prefetch_enabled = os.getenv("DATA_LOADER_PREFETCH", "0") != "0"
        self.actions: list[dict[str, Any]] = []
        self._planned_named_objects: dict[tuple[int, str], Any] = {}
        self._planned_device_types_by_model: dict[str, Any] = {}
        self._prefetch_cache: dict[int, list[Any] | None] = {}
        self._snapshot_query_cache: dict[tuple[str, tuple[tuple[str, str], ...]], list[Any]] = {}
        self._filter_cache: dict[tuple[int, tuple[tuple[str, str], ...]], list[Any]] = {}
        self._cables_snapshot: list[Any] | None = None
        self._endpoint_paths: dict[str, str] = self._build_endpoint_paths()
        self.cache_stats: dict[str, Any] = {
            "hits": 0,
            "misses": 0,
            "by_endpoint": {},
        }
        self._config_context_relations_supported: bool | None = None
        self._ip_namespace_supported: bool | None = None

    @property
    def is_plan(self) -> bool:
        return self.mode == "plan"

    @staticmethod
    def _normalize_for_compare(value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return str(value)

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
            if self._values_equal(actual, expected):
                continue
            return False
        return True

    @staticmethod
    def _values_equal(current: Any, desired: Any) -> bool:
        # Treat empty optional values as equivalent.
        if (current is None and desired == "") or (desired is None and current == ""):
            return True
        if (current is None and desired == []) or (desired is None and current == []):
            return True

        # Nautobot often returns choice/related objects while desired YAML stores labels/values.
        if isinstance(desired, str):
            for attr in ("value", "name", "label", "slug"):
                cur_attr = getattr(current, attr, None)
                if isinstance(cur_attr, str) and cur_attr.strip().lower() == desired.strip().lower():
                    return True
            if isinstance(current, dict):
                for key in ("value", "name", "label", "slug"):
                    cur_val = current.get(key)
                    if isinstance(cur_val, str) and cur_val.strip().lower() == desired.strip().lower():
                        return True

        norm_current = NautobotDataLoader._normalize_for_compare(current)
        norm_desired = NautobotDataLoader._normalize_for_compare(desired)

        if norm_current == norm_desired:
            return True

        if str(norm_current) == str(norm_desired):
            return True

        if isinstance(norm_current, str) and isinstance(norm_desired, str):
            if norm_current.strip().lower() == norm_desired.strip().lower():
                return True

        return False

    def _record_action(
        self,
        action: str,
        object_type: str,
        identity: str,
        fields: list[str] | None = None,
        object_id: Any | None = None,
    ) -> None:
        self.actions.append(
            {
                "action": action,
                "object_type": object_type,
                "identity": identity,
                "fields": fields or [],
                "object_id": str(object_id) if object_id is not None else None,
            }
        )

    @staticmethod
    def _resource_address(object_type: str, identity: str) -> str:
        safe_identity = identity.replace(" ", "_").replace(":", "_").replace("<->", "__")
        safe_identity = safe_identity.replace("/", "_").replace(".", "_")
        return f"{object_type.lower()}.{safe_identity}"

    def build_state_snapshot(self, previous_state: dict[str, Any], data_file: str) -> dict[str, Any]:
        resources = dict(previous_state.get("resources", {}))

        for action in self.actions:
            address = self._resource_address(action["object_type"], action["identity"])
            if action["action"] == "delete":
                resources.pop(address, None)
                continue

            resources[address] = {
                "object_type": action["object_type"],
                "identity": action["identity"],
                "object_id": action.get("object_id"),
                "last_action": action["action"],
            }

        return {
            "data_file": data_file,
            "mode": self.mode,
            "resources": resources,
        }

    def print_state_diff(self) -> None:
        creates = [a for a in self.actions if a["action"] == "create"]
        updates = [a for a in self.actions if a["action"] == "update"]
        deletes = [a for a in self.actions if a["action"] == "delete"]

        print("\n=== State Diff (Current -> Desired) ===")
        if not creates and not updates and not deletes:
            print("No changes. Infrastructure matches desired state.")
            return

        for action in creates:
            print(f"  + create {action['object_type']}.{action['identity']}")

        for action in updates:
            fields = action.get("fields") or []
            if fields:
                changed = ", ".join(fields)
                print(f"  ~ update {action['object_type']}.{action['identity']} ({changed})")
            else:
                print(f"  ~ update {action['object_type']}.{action['identity']}")

        for action in deletes:
            print(f"  - delete {action['object_type']}.{action['identity']}")

        label = "Plan" if self.is_plan else "Apply"
        print(
            f"\n{label}: {len(creates)} to add, {len(updates)} to change, {len(deletes)} to destroy."
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

    @staticmethod
    def _endpoint_cache_key(endpoint: Any) -> str | None:
        endpoint_url = getattr(endpoint, "url", None)
        if not isinstance(endpoint_url, str) or not endpoint_url:
            return None

        parsed = urlparse(endpoint_url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 3:
            return None
        if parts[0] != "api":
            return None

        app = parts[1]
        model = parts[2].replace("-", "_")
        return f"{app}.{model}"

    def _build_endpoint_paths(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        if self.nb is None:
            return mapping

        endpoint_paths = [
            "dcim.location_types",
            "dcim.locations",
            "extras.roles",
            "dcim.manufacturers",
            "dcim.device_types",
            "dcim.platforms",
            "ipam.namespaces",
            "ipam.prefixes",
            "ipam.vlans",
            "extras.config_contexts",
            "extras.custom_fields",
            "extras.secrets",
            "extras.secrets_groups",
            "extras.secrets_groups_associations",
            "dcim.devices",
            "dcim.interfaces",
            "ipam.ip_addresses",
            "ipam.ip_address_to_interface",
            "dcim.cables",
        ]

        for endpoint_path in endpoint_paths:
            node = self.nb
            try:
                for part in endpoint_path.split("."):
                    node = getattr(node, part)
                key = self._endpoint_cache_key(node)
                if key:
                    mapping[key] = endpoint_path
                mapping[endpoint_path] = endpoint_path
            except Exception:
                continue

        return mapping

    @staticmethod
    def _dict_matches_filter(obj_data: dict[str, Any], filter_kwargs: dict[str, Any]) -> bool:
        for key, expected in filter_kwargs.items():
            if key.endswith("_id"):
                rel_key = key[:-3]
                rel_value = obj_data.get(rel_key)
                if isinstance(rel_value, dict):
                    actual = rel_value.get("id")
                else:
                    actual = rel_value
            else:
                actual = obj_data.get(key)

            if not NautobotDataLoader._values_equal(actual, expected):
                return False

        return True

    def _record_cache_stat(self, endpoint_path: str | None, *, hit: bool) -> None:
        key = endpoint_path or "<unknown>"
        bucket = self.cache_stats["by_endpoint"].setdefault(key, {"hits": 0, "misses": 0})
        if hit:
            self.cache_stats["hits"] += 1
            bucket["hits"] += 1
        else:
            self.cache_stats["misses"] += 1
            bucket["misses"] += 1

    def _snapshot_all(self, endpoint: Any) -> list[Any] | None:
        if not (self.is_plan and self.cache_mode != "off" and self.cache_payload):
            return None

        endpoint_key = self._endpoint_cache_key(endpoint)
        endpoint_path = self._endpoint_paths.get(endpoint_key or "")
        if not endpoint_path:
            self._record_cache_stat(endpoint_key, hit=False)
            return None

        section = self.cache_payload.get("endpoints", {}).get(endpoint_path)
        if not section:
            self._record_cache_stat(endpoint_path, hit=False)
            return None

        objects = section.get("objects")
        if not isinstance(objects, list):
            self._record_cache_stat(endpoint_path, hit=False)
            return None

        self._record_cache_stat(endpoint_path, hit=True)
        planned: list[Any] = []
        for idx, obj_data in enumerate(objects):
            if not isinstance(obj_data, dict):
                continue
            synthetic_id = str(obj_data.get("id") or f"cached-{endpoint_path}-{idx}")
            planned.append(self._build_planned_object(obj_data, synthetic_id))
        return planned

    def _snapshot_filter(self, endpoint: Any, filter_kwargs: dict[str, Any]) -> list[Any] | None:
        if not (self.is_plan and self.cache_mode != "off" and self.cache_payload):
            return None

        endpoint_key = self._endpoint_cache_key(endpoint)
        endpoint_path = self._endpoint_paths.get(endpoint_key or "")
        if not endpoint_path:
            self._record_cache_stat(endpoint_key, hit=False)
            return None

        section = self.cache_payload.get("endpoints", {}).get(endpoint_path)
        if not section:
            self._record_cache_stat(endpoint_path, hit=False)
            return None

        objects = section.get("objects")
        if not isinstance(objects, list):
            self._record_cache_stat(endpoint_path, hit=False)
            return None

        filter_key = tuple(sorted((str(k), str(self._normalize_for_compare(v))) for k, v in filter_kwargs.items()))
        cache_key = (endpoint_path, filter_key)
        if cache_key in self._snapshot_query_cache:
            self._record_cache_stat(endpoint_path, hit=True)
            return self._snapshot_query_cache[cache_key]

        matched: list[Any] = []
        for idx, obj_data in enumerate(objects):
            if not isinstance(obj_data, dict):
                continue
            if not self._dict_matches_filter(obj_data, filter_kwargs):
                continue

            synthetic_id = str(obj_data.get("id") or f"cached-{endpoint_path}-{idx}")
            matched.append(self._build_planned_object(obj_data, synthetic_id))

        self._snapshot_query_cache[cache_key] = matched
        self._record_cache_stat(endpoint_path, hit=True)
        return matched

    def _snapshot_lookup_by_identity(self, endpoint: Any, object_type: str, identity: str) -> Any | None:
        if not (self.is_plan and self.cache_mode != "off" and self.cache_payload):
            return None

        endpoint_key = self._endpoint_cache_key(endpoint)
        endpoint_path = self._endpoint_paths.get(endpoint_key or "")
        if not endpoint_path:
            self._record_cache_stat(endpoint_key, hit=False)
            return None

        section = self.cache_payload.get("endpoints", {}).get(endpoint_path)
        if not section:
            self._record_cache_stat(endpoint_path, hit=False)
            return None

        objects = section.get("objects")
        indexes = section.get("indexes")
        if not isinstance(objects, list) or not isinstance(indexes, dict):
            self._record_cache_stat(endpoint_path, hit=False)
            return None

        key_by_object_type = {
            "LocationType": "name",
            "Location": "name",
            "Role": "name",
            "Manufacturer": "name",
            "DeviceType": "model",
            "Platform": "name",
            "Namespace": "name",
            "Prefix": "prefix",
            "VLAN": "vid",
            "ConfigContext": "name",
            "Secret": "name",
            "SecretsGroup": "name",
            "Device": "name",
        }

        lookup_key = key_by_object_type.get(object_type)
        if not lookup_key:
            return None

        positions = indexes.get(lookup_key, {}).get(str(identity), [])
        for pos in positions:
            if not isinstance(pos, int) or pos < 0 or pos >= len(objects):
                continue
            obj_data = objects[pos]
            if not isinstance(obj_data, dict):
                continue
            synthetic_id = str(obj_data.get("id") or f"cached-{endpoint_path}-{pos}")
            self._record_cache_stat(endpoint_path, hit=True)
            return self._build_planned_object(obj_data, synthetic_id)

        # Fallback for object families where cache field naming may vary.
        if object_type == "CustomField":
            for pos, obj_data in enumerate(objects):
                if not isinstance(obj_data, dict):
                    continue
                if str(obj_data.get("name", "")) == identity or str(obj_data.get("key", "")) == identity:
                    synthetic_id = str(obj_data.get("id") or f"cached-{endpoint_path}-{pos}")
                    self._record_cache_stat(endpoint_path, hit=True)
                    return self._build_planned_object(obj_data, synthetic_id)

        self._record_cache_stat(endpoint_path, hit=False)
        return None

    def cache_stats_summary(self) -> str:
        hits = int(self.cache_stats.get("hits", 0))
        misses = int(self.cache_stats.get("misses", 0))
        total = hits + misses
        if total <= 0:
            return "Cache stats: no snapshot lookups"
        ratio = (hits / total) * 100.0
        return f"Cache stats: hits={hits} misses={misses} hit_ratio={ratio:.1f}%"

    def _needs_live_verify_for_compare(
        self,
        obj: Any,
        payload: dict[str, Any],
        compare_keys: list[str] | None,
    ) -> bool:
        if self.cache_mode != "read-through":
            return False
        if not self.is_plan:
            return False
        if not hasattr(obj, "serialize"):
            return False

        try:
            snapshot = obj.serialize() or {}
        except Exception:
            return False

        keys = compare_keys or list(payload.keys())
        for key in keys:
            desired = payload.get(key)
            current = snapshot.get(key)

            if key == "status" and isinstance(desired, str):
                if isinstance(current, str):
                    continue
                if isinstance(current, dict):
                    if any(
                        isinstance(current.get(name_key), str)
                        for name_key in ("value", "name", "label", "slug")
                    ):
                        continue
                return True

        return False

    def _get_prefetched_objects(self, endpoint: Any) -> list[Any] | None:
        if not self.prefetch_enabled:
            return None

        endpoint_key = id(endpoint)
        if endpoint_key in self._prefetch_cache:
            return self._prefetch_cache[endpoint_key]

        try:
            # Use endpoint.all() so pagination is handled by pynautobot and we don't
            # accidentally miss existing objects that are outside a single page.
            objs = list(endpoint.all())
        except Exception:
            try:
                objs = list(endpoint.filter(limit=1000))
            except Exception:
                self._prefetch_cache[endpoint_key] = None
                return None

        self._prefetch_cache[endpoint_key] = objs
        return objs

    def _cached_filter(self, endpoint: Any, filter_kwargs: dict[str, Any]) -> list[Any] | None:
        objs = self._get_prefetched_objects(endpoint)
        if objs is None:
            return None
        return [obj for obj in objs if self._object_matches_filter(obj, filter_kwargs)]

    @staticmethod
    def _filter_cache_key(endpoint: Any, filter_kwargs: dict[str, Any]) -> tuple[int, tuple[tuple[str, str], ...]]:
        normalized = tuple(sorted((str(k), str(NautobotDataLoader._normalize_for_compare(v))) for k, v in filter_kwargs.items()))
        return (id(endpoint), normalized)

    def _cached_live_filter(self, endpoint: Any, filter_kwargs: dict[str, Any]) -> list[Any]:
        key = self._filter_cache_key(endpoint, filter_kwargs)
        if key in self._filter_cache:
            return self._filter_cache[key]

        results = [obj for obj in endpoint.filter(**filter_kwargs) if self._object_matches_filter(obj, filter_kwargs)]
        self._filter_cache[key] = results
        return results

    def _clear_filter_cache_for_endpoint(self, endpoint: Any) -> None:
        endpoint_key = id(endpoint)
        self._filter_cache = {
            key: value for key, value in self._filter_cache.items() if key[0] != endpoint_key
        }

    def _list_cables(self) -> list[Any]:
        if self._cables_snapshot is None:
            self._cables_snapshot = list(self.nb.dcim.cables.filter(limit=1000))
        return self._cables_snapshot

    def _upsert_cable_snapshot(self, cable_obj: Any) -> None:
        cables = self._list_cables()
        cable_id = getattr(cable_obj, "id", None)
        if cable_id is None:
            cables.append(cable_obj)
            return
        for idx, existing in enumerate(cables):
            if getattr(existing, "id", None) == cable_id:
                cables[idx] = cable_obj
                return
        cables.append(cable_obj)

    def _remove_cable_snapshot(self, cable_obj: Any) -> None:
        cables = self._list_cables()
        cable_id = getattr(cable_obj, "id", None)
        if cable_id is None:
            return
        self._cables_snapshot = [c for c in cables if getattr(c, "id", None) != cable_id]

    def _upsert_prefetch_object(self, endpoint: Any, obj: Any) -> None:
        objs = self._get_prefetched_objects(endpoint)
        if objs is None:
            self._clear_filter_cache_for_endpoint(endpoint)
            return
        obj_id = getattr(obj, "id", None)
        if obj_id is None:
            objs.append(obj)
            self._clear_filter_cache_for_endpoint(endpoint)
            return
        for i, existing in enumerate(objs):
            if getattr(existing, "id", None) == obj_id:
                objs[i] = obj
                self._clear_filter_cache_for_endpoint(endpoint)
                return
        objs.append(obj)
        self._clear_filter_cache_for_endpoint(endpoint)

    def _remove_prefetch_object(self, endpoint: Any, obj: Any) -> None:
        objs = self._get_prefetched_objects(endpoint)
        if objs is None:
            self._clear_filter_cache_for_endpoint(endpoint)
            return
        obj_id = getattr(obj, "id", None)
        if obj_id is None:
            return
        self._prefetch_cache[id(endpoint)] = [existing for existing in objs if getattr(existing, "id", None) != obj_id]
        self._clear_filter_cache_for_endpoint(endpoint)

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
            if not self._values_equal(current, desired):
                update_payload[key] = desired

        if not update_payload:
            self._record_action("noop", object_type, identity, object_id=getattr(obj, "id", None))
            return

        if not self.is_plan:
            obj.update(update_payload)
        self._record_action("update", object_type, identity, sorted(update_payload.keys()), getattr(obj, "id", None))

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
        existing: list[Any] = []
        snapshot_hit = False
        if isinstance(object_id, str):
            by_identity = self._snapshot_lookup_by_identity(endpoint, object_type, object_id)
            if by_identity is not None:
                existing = [by_identity]
                snapshot_hit = True

        snapshot_existing = self._snapshot_filter(endpoint, filter_kwargs)
        used_snapshot = snapshot_hit or snapshot_existing is not None
        if snapshot_existing is not None and not existing:
            existing = snapshot_existing

        if not existing and (not used_snapshot or self.cache_mode == "read-through"):
            cached_existing = self._cached_filter(endpoint, filter_kwargs)
            if cached_existing is not None:
                existing = cached_existing
                if not existing:
                    existing = self._cached_live_filter(endpoint, filter_kwargs)
            else:
                existing = self._cached_live_filter(endpoint, filter_kwargs)

            if not existing and hasattr(endpoint, "get"):
                try:
                    maybe_obj = endpoint.get(**filter_kwargs)
                    if maybe_obj:
                        existing = [maybe_obj]
                except Exception:
                    pass

        if existing and used_snapshot and update_existing and self.cache_mode == "read-through" and self.nb is not None:
            # Keep read-through mode correctness-first: use snapshot hits to avoid broad
            # discovery scans, but rebind to live objects before field drift comparison.
            existing = self._cached_live_filter(endpoint, filter_kwargs)
        elif existing and used_snapshot and self._needs_live_verify_for_compare(existing[0], data, compare_keys):
            cached_existing = self._cached_filter(endpoint, filter_kwargs)
            if cached_existing is not None:
                existing = cached_existing
            else:
                existing = self._cached_live_filter(endpoint, filter_kwargs)

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
        self._upsert_prefetch_object(endpoint, obj)
        self._record_action("create", object_type, object_id, sorted(data.keys()), getattr(obj, "id", None))
        return obj

    def get_by_name(self, endpoint: Any, name: str, label: str) -> Any:
        cached = self._planned_named_objects.get((id(endpoint), name))
        if cached:
            return cached

        snapshot = self._snapshot_filter(endpoint, {"name": name})
        if snapshot:
            obj = snapshot[0]
            self._planned_named_objects[(id(endpoint), name)] = obj
            return obj
        if snapshot == [] and self.cache_mode == "strict" and self.is_plan:
            raise RuntimeError(f"{label} '{name}' not found in snapshot cache")

        prefetched = self._cached_filter(endpoint, {"name": name})
        if prefetched:
            obj = prefetched[0]
            self._planned_named_objects[(id(endpoint), name)] = obj
            return obj

        obj = endpoint.get(name=name)
        if not obj:
            raise RuntimeError(f"{label} '{name}' not found")
        self._planned_named_objects[(id(endpoint), name)] = obj
        return obj

    def get_device_type_by_model(self, model: str) -> Any:
        cached = self._planned_device_types_by_model.get(model)
        if cached:
            return cached

        snapshot = self._snapshot_filter(self.nb.dcim.device_types, {"model": model})
        if snapshot:
            self._planned_device_types_by_model[model] = snapshot[0]
            return snapshot[0]
        if snapshot == [] and self.cache_mode == "strict" and self.is_plan:
            raise RuntimeError(f"DeviceType model '{model}' not found in snapshot cache")

        prefetched = self._cached_filter(self.nb.dcim.device_types, {"model": model})
        if prefetched:
            self._planned_device_types_by_model[model] = prefetched[0]
            return prefetched[0]

        matches = self.nb.dcim.device_types.filter(model=model)
        if not matches:
            raise RuntimeError(f"DeviceType model '{model}' not found")
        self._planned_device_types_by_model[model] = matches[0]
        return matches[0]

    def get_interface(self, device_id: str, interface_name: str) -> Any:
        snapshot = self._snapshot_filter(self.nb.dcim.interfaces, {"device_id": device_id, "name": interface_name})
        if snapshot is not None:
            matches = snapshot
            if matches:
                return matches[0]
            if self.cache_mode == "strict" and self.is_plan:
                raise RuntimeError(f"Interface '{interface_name}' not found on device id '{device_id}' in snapshot cache")

        cached = self._cached_filter(self.nb.dcim.interfaces, {"device_id": device_id, "name": interface_name})
        if cached is not None:
            matches = cached
        else:
            matches = self._cached_live_filter(self.nb.dcim.interfaces, {"device_id": device_id, "name": interface_name})
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
                "status": ip_item.get("status", "Active"),
            }
            compare_keys = ["status"]
            if self._supports_ip_namespace():
                payload["namespace"] = self.namespace.id
                compare_keys.append("namespace")
            if "role" in ip_item:
                payload["role"] = ip_item["role"]
                compare_keys.append("role")
            if "dns_name" in ip_item:
                payload["dns_name"] = ip_item["dns_name"]
                compare_keys.append("dns_name")

            ip_obj = self.create_or_get(
                self.nb.ipam.ip_addresses,
                {"address": address},
                payload,
                compare_keys=compare_keys,
            )

        self.create_or_get(
            self.nb.ipam.ip_address_to_interface,
            {"ip_address": ip_obj.id, "interface": interface.id},
            {"ip_address": ip_obj.id, "interface": interface.id},
        )
        return ip_obj

    def _supports_config_context_relations(self) -> bool:
        if self._config_context_relations_supported is not None:
            return self._config_context_relations_supported

        sample = None
        for ctx in self.nb.extras.config_contexts.filter(limit=1):
            sample = ctx
            break

        if not sample:
            self._config_context_relations_supported = True
            return True

        data = sample.serialize()
        self._config_context_relations_supported = (
            data.get("roles") is not None or data.get("platforms") is not None
        )
        return self._config_context_relations_supported

    def _supports_ip_namespace(self) -> bool:
        if self._ip_namespace_supported is not None:
            return self._ip_namespace_supported

        sample = None
        for ip in self.nb.ipam.ip_addresses.filter(limit=1):
            sample = ip
            break

        if not sample:
            self._ip_namespace_supported = True
            return True

        data = sample.serialize()
        self._ip_namespace_supported = data.get("namespace") is not None
        return self._ip_namespace_supported

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
        supports_relations = self._supports_config_context_relations()
        for item in self.data.get("config_contexts", []):
            payload = {
                "name": item["name"],
                "weight": item.get("weight", 100),
                "is_active": bool(item.get("is_active", True)),
                "description": item.get("description", ""),
                "data": item.get("data", {}),
            }
            compare_keys = ["weight", "is_active", "description", "data"]
            if supports_relations and item.get("roles"):
                payload["roles"] = [self.get_by_name(self.nb.extras.roles, n, "Role").id for n in item["roles"]]
                compare_keys.append("roles")
            if supports_relations and item.get("platforms"):
                payload["platforms"] = [self.get_by_name(self.nb.dcim.platforms, n, "Platform").id for n in item["platforms"]]
                compare_keys.append("platforms")
            self.create_or_get(
                self.nb.extras.config_contexts,
                {"name": item["name"]},
                payload,
                object_type="ConfigContext",
                identity=item["name"],
                compare_keys=compare_keys,
            )
            print(f"  ConfigContext: {item['name']}")

    def ensure_custom_fields(self) -> None:
        for item in self.data.get("custom_fields", []):
            # Custom field filter keys differ across Nautobot versions; match by attributes from full list.
            obj = None
            existing_custom_fields = self._get_prefetched_objects(self.nb.extras.custom_fields)
            if existing_custom_fields is None:
                existing_custom_fields = list(self.nb.extras.custom_fields.filter(limit=1000))
            for existing in existing_custom_fields:
                if getattr(existing, "key", None) == item["name"]:
                    obj = existing
                    break
                if getattr(existing, "name", None) == item["name"]:
                    obj = existing
                    break
                if item.get("label") and getattr(existing, "label", None) == item["label"]:
                    obj = existing
                    break

            if obj:
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
            for cable in self._list_cables():
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
                cable_obj = self.nb.dcim.cables.create(payload)
                self._upsert_cable_snapshot(cable_obj)
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
            try:
                dev = self.get_by_name(self.nb.dcim.devices, item["name"], "Device")
            except RuntimeError:
                dev = None
            if dev:
                managed[item["name"]] = dev
        return managed

    @staticmethod
    def _related_id(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, dict):
            rel = value.get("id", value)
            return str(rel) if rel is not None else None
        if hasattr(value, "id"):
            rel = getattr(value, "id", None)
            return str(rel) if rel is not None else None
        return str(value)

    @staticmethod
    def _object_field(obj: Any, field: str) -> Any:
        value = getattr(obj, field, None)
        if value is not None:
            return value
        if hasattr(obj, "serialize"):
            try:
                data = obj.serialize() or {}
                return data.get(field)
            except Exception:
                return None
        return None

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

        ctx_rows = self._snapshot_all(self.nb.extras.config_contexts)
        if ctx_rows is None:
            ctx_rows = list(self.nb.extras.config_contexts.filter(limit=1000))
        for ctx in ctx_rows:
            name = getattr(ctx, "name", None)
            if not name or name in desired_ctx_names:
                continue
            if not self.is_plan:
                ctx.delete()
                self._remove_prefetch_object(self.nb.extras.config_contexts, ctx)
            self._record_action("delete", "ConfigContext", name, object_id=getattr(ctx, "id", None))

        vlan_rows = self._snapshot_all(self.nb.ipam.vlans)
        if vlan_rows is None:
            vlan_rows = list(self.nb.ipam.vlans.filter(limit=1000))
        for vlan in vlan_rows:
            vid = int(getattr(vlan, "vid", 0))
            if vid in desired_vlan_vids:
                continue
            if not self.is_plan:
                vlan.delete()
                self._remove_prefetch_object(self.nb.ipam.vlans, vlan)
            self._record_action("delete", "VLAN", str(vid), object_id=getattr(vlan, "id", None))

        prefix_rows = self._snapshot_all(self.nb.ipam.prefixes)
        using_snapshot_prefixes = prefix_rows is not None
        if prefix_rows is None:
            prefix_rows = list(self.nb.ipam.prefixes.filter(namespace=self.namespace.id, limit=1000))
        for prefix in prefix_rows:
            if using_snapshot_prefixes and not self._values_equal(getattr(prefix, "namespace", None), self.namespace.id):
                continue
            value = getattr(prefix, "prefix", None)
            if not value or value in desired_prefixes:
                continue
            if not self.is_plan:
                prefix.delete()
                self._remove_prefetch_object(self.nb.ipam.prefixes, prefix)
            self._record_action("delete", "Prefix", value, object_id=getattr(prefix, "id", None))

        role_rows = self._snapshot_all(self.nb.extras.roles)
        if role_rows is None:
            role_rows = list(self.nb.extras.roles.filter(limit=1000))
        for role in role_rows:
            name = getattr(role, "name", None)
            if not name or name in desired_roles:
                continue
            if not self.is_plan:
                if not self._safe_delete(role):
                    continue
                self._remove_prefetch_object(self.nb.extras.roles, role)
            self._record_action("delete", "Role", name, object_id=getattr(role, "id", None))

        platform_rows = self._snapshot_all(self.nb.dcim.platforms)
        if platform_rows is None:
            platform_rows = list(self.nb.dcim.platforms.filter(limit=1000))
        for platform in platform_rows:
            name = getattr(platform, "name", None)
            if not name or name in desired_platforms:
                continue
            if not self.is_plan:
                if not self._safe_delete(platform):
                    continue
                self._remove_prefetch_object(self.nb.dcim.platforms, platform)
            self._record_action("delete", "Platform", name, object_id=getattr(platform, "id", None))

        dtype_rows = self._snapshot_all(self.nb.dcim.device_types)
        if dtype_rows is None:
            dtype_rows = list(self.nb.dcim.device_types.filter(limit=1000))
        for dtype in dtype_rows:
            model = getattr(dtype, "model", None)
            if not model or model in desired_device_types:
                continue
            if not self.is_plan:
                if not self._safe_delete(dtype):
                    continue
                self._remove_prefetch_object(self.nb.dcim.device_types, dtype)
            self._record_action("delete", "DeviceType", model, object_id=getattr(dtype, "id", None))

        manufacturer_rows = self._snapshot_all(self.nb.dcim.manufacturers)
        if manufacturer_rows is None:
            manufacturer_rows = list(self.nb.dcim.manufacturers.filter(limit=1000))
        for manufacturer in manufacturer_rows:
            name = getattr(manufacturer, "name", None)
            if not name or name in desired_manufacturers:
                continue
            if not self.is_plan:
                if not self._safe_delete(manufacturer):
                    continue
                self._remove_prefetch_object(self.nb.dcim.manufacturers, manufacturer)
            self._record_action("delete", "Manufacturer", name, object_id=getattr(manufacturer, "id", None))

        location_rows = self._snapshot_all(self.nb.dcim.locations)
        if location_rows is None:
            location_rows = list(self.nb.dcim.locations.filter(limit=1000))
        for location in location_rows:
            name = getattr(location, "name", None)
            if not name or name in desired_locations:
                continue
            if not self.is_plan:
                if not self._safe_delete(location):
                    continue
                self._remove_prefetch_object(self.nb.dcim.locations, location)
            self._record_action("delete", "Location", name, object_id=getattr(location, "id", None))

        location_type_rows = self._snapshot_all(self.nb.dcim.location_types)
        if location_type_rows is None:
            location_type_rows = list(self.nb.dcim.location_types.filter(limit=1000))
        for location_type in location_type_rows:
            name = getattr(location_type, "name", None)
            if not name or name in desired_location_types:
                continue
            if not self.is_plan:
                if not self._safe_delete(location_type):
                    continue
                self._remove_prefetch_object(self.nb.dcim.location_types, location_type)
            self._record_action("delete", "LocationType", name, object_id=getattr(location_type, "id", None))

    def prune_managed_network_state(self) -> None:
        managed_devices = self._managed_devices()
        desired_interfaces, desired_ips = self._desired_interface_sets()
        desired_cables = self._desired_cable_pairs()

        managed_iface_id_to_label: dict[str, str] = {}
        managed_iface_label_to_obj: dict[str, Any] = {}
        managed_device_ids = {str(getattr(dev_obj, "id", "")) for dev_obj in managed_devices.values()}

        iface_rows = self._snapshot_all(self.nb.dcim.interfaces)
        if iface_rows is None:
            for dev_name, dev_obj in managed_devices.items():
                for iface in self.nb.dcim.interfaces.filter(device_id=dev_obj.id):
                    label = f"{dev_name}:{iface.name}"
                    managed_iface_id_to_label[str(iface.id)] = label
                    managed_iface_label_to_obj[label] = iface
        else:
            device_id_to_name = {str(getattr(dev_obj, "id", "")): dev_name for dev_name, dev_obj in managed_devices.items()}
            for iface in iface_rows:
                iface_dev_id = self._related_id(self._object_field(iface, "device"))
                if not iface_dev_id or iface_dev_id not in managed_device_ids:
                    continue
                iface_name = getattr(iface, "name", None)
                iface_id = self._related_id(getattr(iface, "id", None))
                if not iface_name or not iface_id:
                    continue
                dev_name = device_id_to_name.get(iface_dev_id)
                if not dev_name:
                    continue
                label = f"{dev_name}:{iface_name}"
                managed_iface_id_to_label[iface_id] = label
                managed_iface_label_to_obj[label] = iface

        # Delete stale cables first, otherwise interface deletion may fail due to in-use links.
        cable_rows = self._snapshot_all(self.nb.dcim.cables)
        if cable_rows is None:
            cable_rows = self._list_cables()
        for cable in cable_rows:
            a_id = self._related_id(self._object_field(cable, "termination_a"))
            b_id = self._related_id(self._object_field(cable, "termination_b"))
            a_label = managed_iface_id_to_label.get(str(a_id)) if a_id else None
            b_label = managed_iface_id_to_label.get(str(b_id)) if b_id else None
            if not a_label or not b_label:
                continue

            key = frozenset([a_label, b_label])
            if key not in desired_cables:
                if not self.is_plan:
                    cable.delete()
                    self._remove_prefetch_object(self.nb.dcim.cables, cable)
                    self._remove_cable_snapshot(cable)
                self._record_action(
                    "delete",
                    "Cable",
                    " <-> ".join(sorted(key)),
                    object_id=getattr(cable, "id", None),
                )

        # Remove stale interface IP assignments (and orphaned IP objects when no bindings remain).
        assignment_rows = self._snapshot_all(self.nb.ipam.ip_address_to_interface)
        ip_rows = self._snapshot_all(self.nb.ipam.ip_addresses)

        assignments_by_interface: dict[str, list[Any]] = {}
        assignments_by_ip: dict[str, int] = {}
        ip_by_id: dict[str, Any] = {}

        using_snapshot_ip_assignment = assignment_rows is not None and ip_rows is not None
        if using_snapshot_ip_assignment:
            for ip_obj in ip_rows or []:
                ip_id = self._related_id(getattr(ip_obj, "id", None))
                if ip_id:
                    ip_by_id[ip_id] = ip_obj

            for assignment in assignment_rows or []:
                iface_id = self._related_id(self._object_field(assignment, "interface"))
                ip_id = self._related_id(self._object_field(assignment, "ip_address"))
                if iface_id:
                    assignments_by_interface.setdefault(iface_id, []).append(assignment)
                if ip_id:
                    assignments_by_ip[ip_id] = assignments_by_ip.get(ip_id, 0) + 1

        for (dev_name, iface_name), wanted_addrs in desired_ips.items():
            iface = managed_iface_label_to_obj.get(f"{dev_name}:{iface_name}")
            if not iface:
                continue

            iface_id = self._related_id(getattr(iface, "id", None))
            if not iface_id:
                continue

            if using_snapshot_ip_assignment:
                iface_assignments = assignments_by_interface.get(iface_id, [])
            else:
                iface_assignments = list(self.nb.ipam.ip_address_to_interface.filter(interface=iface.id))

            for assignment in iface_assignments:
                ip_id = self._related_id(self._object_field(assignment, "ip_address"))
                if not ip_id:
                    continue

                if using_snapshot_ip_assignment:
                    ip_obj = ip_by_id.get(ip_id)
                else:
                    ip_obj = self.nb.ipam.ip_addresses.get(id=ip_id)
                if not ip_obj:
                    continue

                ip_address = getattr(ip_obj, "address", None)
                if not ip_address:
                    continue

                if ip_address not in wanted_addrs:
                    if not self.is_plan:
                        assignment.delete()
                        self._remove_prefetch_object(self.nb.ipam.ip_address_to_interface, assignment)
                    self._record_action(
                        "delete",
                        "IPAddressAssignment",
                        f"{dev_name}:{iface_name}:{ip_address}",
                        object_id=getattr(assignment, "id", None),
                    )

                    if using_snapshot_ip_assignment:
                        remaining = list(range(assignments_by_ip.get(ip_id, 0)))
                    else:
                        remaining = self._cached_live_filter(self.nb.ipam.ip_address_to_interface, {"ip_address": ip_address})
                    if not remaining:
                        if not self.is_plan:
                            ip_obj.delete()
                            self._remove_prefetch_object(self.nb.ipam.ip_addresses, ip_obj)
                        self._record_action("delete", "IPAddress", ip_address, object_id=getattr(ip_obj, "id", None))

        # Remove stale interfaces on managed devices.
        for dev_name, dev_obj in managed_devices.items():
            wanted_ifaces = desired_interfaces.get(dev_name, set())
            if iface_rows is None:
                dev_ifaces = list(self.nb.dcim.interfaces.filter(device_id=dev_obj.id))
            else:
                dev_ifaces = [
                    iface
                    for label, iface in managed_iface_label_to_obj.items()
                    if label.startswith(f"{dev_name}:")
                ]

            for iface in dev_ifaces:
                iface_name = getattr(iface, "name", None)
                if not iface_name or iface_name in wanted_ifaces:
                    continue
                if not self.is_plan:
                    iface.delete()
                    self._remove_prefetch_object(self.nb.dcim.interfaces, iface)
                self._record_action("delete", "Interface", f"{dev_name}:{iface_name}", object_id=getattr(iface, "id", None))

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
        if self.mode in ("apply", "prune", "plan"):
            self.prune_managed_network_state()
            self.prune_managed_global_state()

        self.print_state_diff()

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
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="Path to local data loader state JSON file")
    parser.add_argument(
        "--cache-file",
        default=str(DEFAULT_CACHE_FILE),
        help="Path to local data loader cache JSON file",
    )
    parser.add_argument(
        "--cache-mode",
        choices=["off", "read-through", "strict"],
        default=os.getenv("DATA_LOADER_CACHE_MODE", "off"),
        help="Cache mode: off (default), read-through, strict",
    )
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=int(os.getenv("DATA_LOADER_CACHE_TTL_SECONDS", "300")),
        help="Maximum cache age in seconds before marked stale",
    )
    parser.add_argument(
        "--cache-refresh",
        choices=["none", "auto", "full"],
        default=os.getenv("DATA_LOADER_CACHE_REFRESH", "auto"),
        help="Cache refresh policy: none, auto (refresh when stale), or full",
    )
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

    api_calls = {"count": 0}
    api_calls_by_endpoint: dict[str, int] = {}
    original_request = getattr(getattr(nb, "http_session", None), "request", None)
    if callable(original_request):
        def counted_request(*request_args, **request_kwargs):
            api_calls["count"] += 1
            method = str(request_args[0]).upper() if len(request_args) > 0 else str(request_kwargs.get("method", "GET")).upper()
            raw_url = request_args[1] if len(request_args) > 1 else request_kwargs.get("url", "")
            parsed = urlparse(str(raw_url))
            endpoint_key = f"{method} {parsed.path}"
            api_calls_by_endpoint[endpoint_key] = api_calls_by_endpoint.get(endpoint_key, 0) + 1
            return original_request(*request_args, **request_kwargs)

        nb.http_session.request = counted_request

    print("=== Nautobot data loader starting ===")
    print(f"  Data file: {args.data_file}")
    print(f"  State file: {args.state_file}")
    print(f"  Cache file: {args.cache_file}")
    print(f"  Cache mode: {args.cache_mode}")
    print(f"  Cache refresh: {args.cache_refresh}")
    print(f"  Mode: {args.mode}")

    if args.mode == "prune":
        print("  WARN: --mode prune is deprecated and will be removed in a future release. Use --mode apply.")

    state_store = StateStore(Path(args.state_file))
    previous_state = state_store.load()

    cache_store = CacheStore(Path(args.cache_file))
    cache_payload: dict[str, Any] | None = None
    cache_stale: bool | None = None
    effective_cache_mode = args.cache_mode
    if args.cache_mode != "off":
        cache_payload = cache_store.load()
        cache_stale = cache_store.is_stale(cache_payload, args.cache_ttl_seconds)
        print(f"  Cache stale: {'yes' if cache_stale else 'no'}")

        refresh_requested = args.cache_refresh == "full"
        refresh_auto = args.cache_refresh == "auto" and cache_stale
        should_refresh = refresh_requested or refresh_auto

        if should_refresh:
            refresh_stats = refresh_cache_snapshot(nb, cache_payload)
            cache_store.touch_refresh(
                cache_payload,
                source="full-snapshot",
                nautobot_url=nautobot_url,
            )
            cache_store.save(cache_payload)
            print(
                "  Cache refreshed: "
                f"{refresh_stats['endpoints']} endpoints, {refresh_stats['objects']} objects"
            )
            cache_stale = cache_store.is_stale(cache_payload, args.cache_ttl_seconds)

        if args.cache_mode == "strict" and cache_stale:
            raise RuntimeError(
                "Cache is stale in strict mode. Run with --cache-refresh full or increase --cache-ttl-seconds."
            )

        if args.cache_mode == "read-through" and cache_stale and args.cache_refresh == "none":
            # Safety fallback: stale snapshots can miss drift/deletes and produce incorrect plans.
            # In read-through mode with refresh disabled, bypass cache for this run.
            effective_cache_mode = "off"
            print("  WARN: cache is stale and refresh is disabled; bypassing cache for this run.")

    loader = NautobotDataLoader(
        nb,
        data,
        mode=args.mode,
        cache_mode=effective_cache_mode,
        cache_payload=cache_payload,
    )
    try:
        state_store.acquire_lock()
        loader.run()
        new_state = loader.build_state_snapshot(previous_state, str(args.data_file))
        state_store.save(new_state)
        if args.cache_mode != "off" and cache_payload is not None:
            cache_store.touch_runtime(
                cache_payload,
                run_mode=args.mode,
                source="loader-runtime",
                nautobot_url=nautobot_url,
            )
            cache_store.save(cache_payload)
    finally:
        state_store.release_lock()

    print("\n=== Nautobot data loader complete ===")
    if args.cache_mode != "off":
        print(f"=== {loader.cache_stats_summary()} ===")
    print(f"=== API calls made: {api_calls['count']} ===")
    if api_calls_by_endpoint:
        print("=== API call hotspots (top 10) ===")
        for endpoint_key, count in sorted(api_calls_by_endpoint.items(), key=lambda item: item[1], reverse=True)[:10]:
            print(f"  {count:>4}  {endpoint_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
