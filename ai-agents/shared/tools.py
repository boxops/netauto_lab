"""
Shared tools available to all network AI agents.

Tools are organised into four tiers that map to a standard investigation workflow:
  1. Discovery   – Nautobot inventory (devices, interfaces, topology, VLANs, prefixes, IPs)
  2. Metrics     – Prometheus real-time state (reachability, interface counters, BGP)
  3. Logs        – Loki syslog events (interface flaps, BGP events, errors)
  4. Actions     – Ansible playbook execution (always check-mode by default)

See docs/agent-tools-framework.md for the full workflow guide.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from langchain.tools import tool

from shared.config import settings


# ── Nautobot helpers ──────────────────────────────────────────────────────────

def _nautobot_get(path: str, params: dict | None = None) -> dict:
    resp = httpx.get(
        f"{settings.nautobot_url}/api/{path.lstrip('/')}",
        headers={"Authorization": f"Token {settings.nautobot_token}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _summarize_device(d: dict) -> dict:
    return {
        "name": d.get("name"),
        "role": d.get("role", {}).get("name") if d.get("role") else None,
        "platform": d.get("platform", {}).get("name") if d.get("platform") else None,
        "location": d.get("location", {}).get("name") if d.get("location") else None,
        "status": d.get("status", {}).get("name") if d.get("status") else None,
        "primary_ip": d.get("primary_ip4", {}).get("address") if d.get("primary_ip4") else None,
    }


def _summarize_prefix(p: dict) -> dict:
    return {
        "prefix": p.get("prefix"),
        "status": p.get("status", {}).get("name") if p.get("status") else None,
        "description": p.get("description"),
    }


def _summarize_vlan(v: dict) -> dict:
    return {
        "vid": v.get("vid"),
        "name": v.get("name"),
        "status": v.get("status", {}).get("name") if v.get("status") else None,
        "description": v.get("description"),
    }


def _summarize_circuit(c: dict) -> dict:
    return {
        "cid": c.get("cid"),
        "provider": c.get("provider", {}).get("name") if c.get("provider") else None,
        "type": c.get("circuit_type", {}).get("name") if c.get("circuit_type") else None,
        "status": c.get("status", {}).get("name") if c.get("status") else None,
    }


def _device_name_from_slug(slug: str) -> str:
    """Extract device hostname from a Nautobot natural_slug.
    Format: device-name__location-slug__interface-name_shortid
    """
    return slug.split("__")[0] if slug else ""


def _parse_termination(term: dict | None) -> dict | None:
    """Extract {device, interface} from a cable termination object."""
    if not term:
        return None
    slug = term.get("natural_slug", "")
    device = _device_name_from_slug(slug)
    iface = term.get("display") or term.get("name")
    return {"device": device, "interface": iface} if device else None


# ── Tier 1 – Nautobot Discovery ───────────────────────────────────────────────

@tool
def get_all_devices() -> str:
    """
    List every network device in Nautobot with its role, platform, location,
    status, and primary IP address.

    Use this as the FIRST tool whenever a task involves multiple devices or when
    you do not yet know what devices exist. Do NOT use search_nautobot() for a
    broad device listing — this tool is faster and more complete.

    Returns:
        JSON with total_count and a devices list.
    """
    try:
        result = _nautobot_get("dcim/devices/", {"depth": 1, "limit": 200})
        devices = [_summarize_device(d) for d in result["results"]]
        return json.dumps({
            "total_count": result["count"],
            "returned": len(devices),
            "devices": devices,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_device_info(device_name: str) -> str:
    """
    Get detailed information about a single network device from Nautobot,
    including role, platform, location, primary IP, status, and interface count.

    Use this when you need full details for ONE known device.
    Use get_all_devices() first if you need to discover device names.

    Args:
        device_name: The exact hostname as it appears in Nautobot (e.g., 'leaf1', 'spine2').

    Returns:
        JSON with device details and interface count.
    """
    try:
        result = _nautobot_get("dcim/devices/", {"name": device_name, "depth": 1})
        if result["count"] == 0:
            available = _nautobot_get("dcim/devices/", {"depth": 1, "limit": 50})
            names = [d["name"] for d in available["results"]]
            return json.dumps({
                "error": f"Device '{device_name}' not found.",
                "available_devices": names,
            })
        device = result["results"][0]
        iface_count = _nautobot_get("dcim/interfaces/", {"device": device_name, "limit": 1})
        return json.dumps({
            "name": device["name"],
            "role": device.get("role", {}).get("name") if device.get("role") else None,
            "platform": device.get("platform", {}).get("name") if device.get("platform") else None,
            "location": device.get("location", {}).get("name") if device.get("location") else None,
            "primary_ip": device.get("primary_ip4", {}).get("address") if device.get("primary_ip4") else None,
            "status": device.get("status", {}).get("name") if device.get("status") else None,
            "serial": device.get("serial"),
            "interface_count": iface_count.get("count", 0),
            "nautobot_url": f"{settings.nautobot_url}/dcim/devices/{device['id']}/",
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_device_interfaces(device_name: str) -> str:
    """
    Get all interfaces for a specific device from Nautobot, including type,
    description, enabled status, connected neighbor (device + interface),
    and assigned IP addresses.

    Use this tool when you need interface-level information such as:
    - Generating interface descriptions or documentation
    - Finding what a device is connected to
    - Checking IP assignments per interface
    - Identifying uplinks vs. access ports

    Call get_all_devices() first if you do not know the exact device name.

    Args:
        device_name: Exact hostname as it appears in Nautobot (e.g., 'leaf1').

    Returns:
        JSON with device name, interface count, and a list of interface objects.
    """
    try:
        result = _nautobot_get("dcim/interfaces/", {
            "device": device_name,
            "depth": 1,
            "limit": 500,
        })
        if result["count"] == 0:
            return json.dumps({
                "error": f"No interfaces found for '{device_name}'. "
                         "Verify the device name with get_all_devices().",
            })

        interfaces = []
        for iface in result["results"]:
            entry: dict[str, Any] = {
                "name": iface.get("name"),
                "type": iface.get("type", {}).get("label") if isinstance(iface.get("type"), dict) else None,
                "enabled": iface.get("enabled"),
                "description": iface.get("description") or "",
                "mgmt_only": iface.get("mgmt_only", False),
                "mtu": iface.get("mtu"),
            }

            # Connected neighbor — extract device name from natural_slug
            endpoint = iface.get("connected_endpoint")
            if endpoint:
                slug = endpoint.get("natural_slug", "")
                connected_device = _device_name_from_slug(slug)
                connected_iface = endpoint.get("display") or endpoint.get("name")
                if connected_device:
                    entry["connected_to"] = {
                        "device": connected_device,
                        "interface": connected_iface,
                    }

            # IP addresses (query separately when count > 0)
            ip_count = iface.get("ip_address_count", 0)
            if ip_count > 0:
                try:
                    ips = _nautobot_get("ipam/ip-addresses/", {
                        "interface": iface["id"],
                        "limit": 10,
                    })
                    entry["ip_addresses"] = [
                        ip["address"] for ip in ips.get("results", [])
                    ]
                except Exception:
                    entry["ip_address_count"] = ip_count

            interfaces.append(entry)

        return json.dumps({
            "device": device_name,
            "interface_count": result["count"],
            "interfaces": interfaces,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_topology() -> str:
    """
    Get the complete physical topology — every cable connection in Nautobot,
    showing which device+interface connects to which device+interface.

    Use this tool for:
    - Understanding the full network topology at a glance
    - Blast radius analysis before a chaos experiment
    - Checking redundancy (how many uplinks does a device have?)
    - Generating topology documentation

    Returns:
        JSON with total cable count and a list of {side_a, side_b} connection pairs.
    """
    try:
        cables = _nautobot_get("dcim/cables/", {"depth": 1, "limit": 200})
        connections = []
        for cable in cables.get("results", []):
            side_a = _parse_termination(cable.get("termination_a"))
            side_b = _parse_termination(cable.get("termination_b"))
            if side_a and side_b:
                connections.append({"side_a": side_a, "side_b": side_b})
        return json.dumps({
            "cable_count": cables.get("count", 0),
            "connections": connections,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_connected_devices(device_name: str) -> str:
    """
    Get the list of devices directly connected to the specified device via cables.

    Use this for a quick neighbor lookup when you already know the device name.
    Use get_topology() for the full picture across all devices.

    Args:
        device_name: The hostname of the device to find neighbors for.

    Returns:
        JSON listing connected devices and the interfaces used on each side.
    """
    try:
        result = _nautobot_get("dcim/devices/", {"name": device_name})
        if result["count"] == 0:
            return json.dumps({"error": f"Device '{device_name}' not found."})
        device_id = result["results"][0]["id"]
        cables = _nautobot_get("dcim/cables/", {"device": device_name, "depth": 1})
        neighbors = []
        for cable in cables.get("results", []):
            for side_key in ("termination_a", "termination_b"):
                term = cable.get(side_key) or {}
                term_device = _device_name_from_slug(term.get("natural_slug", ""))
                if term_device and term_device != device_name:
                    neighbors.append({
                        "device": term_device,
                        "interface": term.get("display") or term.get("name"),
                    })
        return json.dumps({
            "device": device_name,
            "neighbor_count": len(neighbors),
            "neighbors": neighbors,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_vlans() -> str:
    """
    List all VLANs defined in Nautobot with their ID, name, status, and description.

    Use this when designing configurations, planning VLAN assignments, or
    documenting the network.

    Returns:
        JSON with total VLAN count and a list of VLAN objects.
    """
    try:
        result = _nautobot_get("ipam/vlans/", {"depth": 1, "limit": 500})
        vlans = [_summarize_vlan(v) for v in result["results"]]
        return json.dumps({
            "total_count": result["count"],
            "vlans": vlans,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_prefixes() -> str:
    """
    List all IP prefixes (subnets) in Nautobot with their status and description.

    Use this for IP planning, subnetting decisions, or understanding the
    addressing scheme before allocating new IPs.

    Returns:
        JSON with total prefix count and a list of prefix objects.
    """
    try:
        result = _nautobot_get("ipam/prefixes/", {"depth": 1, "limit": 500})
        prefixes = [_summarize_prefix(p) for p in result["results"]]
        return json.dumps({
            "total_count": result["count"],
            "prefixes": prefixes,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_ip_addresses(device_name: str = "", prefix: str = "") -> str:
    """
    Query IP addresses from Nautobot IPAM. Optionally filter by device or prefix.

    Use this to:
    - See all IPs assigned to a specific device
    - List all IPs within a given prefix/subnet
    - Check IP assignment status

    Leave both arguments empty to list all IP addresses.

    Args:
        device_name: Optional — filter to IPs assigned to this device hostname.
        prefix:      Optional — filter to IPs within this prefix (e.g., '10.10.0.0/16').

    Returns:
        JSON with IP addresses, their assignments, and status.
    """
    try:
        params: dict[str, Any] = {"depth": 1, "limit": 200}
        if device_name:
            params["device"] = device_name
        if prefix:
            params["parent"] = prefix
        result = _nautobot_get("ipam/ip-addresses/", params)
        addresses = []
        for ip in result["results"]:
            entry: dict[str, Any] = {
                "address": ip.get("address"),
                "status": ip.get("status", {}).get("name") if ip.get("status") else None,
                "dns_name": ip.get("dns_name") or None,
                "description": ip.get("description") or None,
            }
            obj = ip.get("assigned_object")
            obj_type = ip.get("assigned_object_type", "")
            if obj and obj_type:
                entry["assigned_to"] = {
                    "type": obj_type,
                    "display": obj.get("display"),
                }
            addresses.append(entry)
        return json.dumps({
            "total_count": result["count"],
            "returned": len(addresses),
            "ip_addresses": addresses,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_available_ips(prefix: str, count: int = 1) -> str:
    """
    Find available (unallocated) IP addresses within a given prefix from Nautobot IPAM.

    Use this during IP planning to reserve new addresses.
    Use get_prefixes() first to see available prefixes.

    Args:
        prefix: The prefix to search in (e.g., '10.10.0.0/16').
        count:  Number of available IPs to return (default 1).

    Returns:
        JSON list of available IP addresses.
    """
    try:
        result = _nautobot_get("ipam/prefixes/", {"prefix": prefix})
        if result["count"] == 0:
            return json.dumps({"error": f"Prefix '{prefix}' not found in Nautobot."})
        prefix_id = result["results"][0]["id"]
        available = _nautobot_get(
            f"ipam/prefixes/{prefix_id}/available-ips/", {"count": count}
        )
        return json.dumps(available, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def search_nautobot(query: str) -> str:
    """
    Search Nautobot across devices, prefixes, VLANs, and circuits using a keyword.

    Use this for flexible keyword lookups (e.g., searching by site name, description,
    or partial device name). For full listings use the dedicated tools instead:
    - get_all_devices() for all devices
    - get_vlans() for all VLANs
    - get_prefixes() for all prefixes

    Pass an empty string to list up to 50 objects of each type.

    Args:
        query: Search term. Use "" to list all.

    Returns:
        JSON with matching objects grouped by type.
    """
    endpoints = {
        "devices": ("dcim/devices/", {"q": query, "limit": 50, "depth": 1}, _summarize_device),
        "prefixes": ("ipam/prefixes/", {"q": query, "limit": 50, "depth": 1}, _summarize_prefix),
        "vlans": ("ipam/vlans/", {"q": query, "limit": 50, "depth": 1}, _summarize_vlan),
        "circuits": ("circuits/circuits/", {"q": query, "limit": 50, "depth": 1}, _summarize_circuit),
    }
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for label, (path, params, summarize) in endpoints.items():
        try:
            data = _nautobot_get(path, params)
            results[label] = {
                "count": data.get("count", 0),
                "results": [summarize(r) for r in data.get("results", [])],
            }
        except Exception as e:
            errors[label] = str(e)
    output: dict[str, Any] = {"query": query, "results": results}
    if errors:
        output["errors"] = errors
    return json.dumps(output, indent=2)


@tool
def get_devices_by_location(location_name: str) -> str:
    """
    List all devices at a given location (site) in Nautobot.

    Use this when a task is scoped to a specific site.
    Use get_all_devices() when you need the full inventory across all sites.

    Args:
        location_name: The location/site name (e.g., 'site-lab', 'site-nyc').

    Returns:
        JSON with device list for that location. Includes available locations on error.
    """
    def _available_locations() -> list[str]:
        try:
            locs = _nautobot_get("dcim/locations/", {"limit": 50})
            return [loc.get("name") for loc in locs.get("results", [])]
        except Exception:
            return []

    try:
        result = _nautobot_get(
            "dcim/devices/",
            {"location": location_name, "depth": 1, "limit": 100},
        )
        if result["count"] == 0:
            return json.dumps({
                "error": f"No devices found at location '{location_name}'.",
                "available_locations": _available_locations(),
            })
        return json.dumps({
            "location": location_name,
            "device_count": result["count"],
            "devices": [_summarize_device(d) for d in result["results"]],
        }, indent=2)
    except httpx.HTTPStatusError:
        return json.dumps({
            "error": f"Location '{location_name}' not recognised by Nautobot.",
            "available_locations": _available_locations(),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tier 2 – Prometheus Metrics ───────────────────────────────────────────────

def _prometheus_query(promql: str) -> list[dict]:
    """Run an instant PromQL query and return the result list."""
    resp = httpx.get(
        f"{settings.prometheus_url}/api/v1/query",
        params={"query": promql},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "success":
        return data["data"]["result"]
    return []


def _prometheus_range_query(promql: str, minutes: int = 60) -> list[dict]:
    """Run a PromQL range query and return summarised series."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    resp = httpx.get(
        f"{settings.prometheus_url}/api/v1/query_range",
        params={
            "query": promql,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "step": "60s",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "success":
        return data["data"]["result"]
    return []


@tool
def get_device_metrics(device_name: str) -> str:
    """
    Get real-time health metrics for a specific device from Prometheus.

    Reports:
    - ICMP reachability and round-trip latency (ping probe)
    - Packet loss percentage
    - Interface operational status (if SNMP metrics available)

    The device's primary IP is looked up from Nautobot automatically.
    Use get_active_alerts() alongside this for a full operational picture.

    Args:
        device_name: Exact hostname as it appears in Nautobot (e.g., 'leaf1').

    Returns:
        JSON with reachability status, latency, and available interface metrics.
    """
    try:
        # Resolve primary IP from Nautobot
        dev_result = _nautobot_get("dcim/devices/", {"name": device_name, "depth": 1})
        if dev_result["count"] == 0:
            return json.dumps({"error": f"Device '{device_name}' not found in Nautobot."})
        device = dev_result["results"][0]
        primary_ip_cidr = (
            device.get("primary_ip4", {}).get("address") if device.get("primary_ip4") else None
        )
        primary_ip = primary_ip_cidr.split("/")[0] if primary_ip_cidr else None

        metrics: dict[str, Any] = {"device": device_name, "primary_ip": primary_ip}

        # ICMP probe metrics
        if primary_ip:
            ping_filter = f'{{url="{primary_ip}"}}'
            ping_up = _prometheus_query(f"ping_result_code{ping_filter}")
            if ping_up:
                code = int(float(ping_up[0]["value"][1]))
                metrics["reachable"] = code == 0
                metrics["ping_result_code"] = code
            avg = _prometheus_query(f"ping_average_response_ms{ping_filter}")
            if avg:
                metrics["avg_rtt_ms"] = round(float(avg[0]["value"][1]), 2)
            loss = _prometheus_query(f"ping_percent_packet_loss{ping_filter}")
            if loss:
                metrics["packet_loss_pct"] = round(float(loss[0]["value"][1]), 2)
        else:
            metrics["reachable"] = None
            metrics["note"] = "No primary IP set in Nautobot — cannot query ping metrics."

        # Interface operational status (SNMP-based, may not be available)
        iface_status = _prometheus_query(
            f'interface_ifOperStatus{{agent_host="{primary_ip}"}}'
        )
        if not iface_status and primary_ip:
            # Try hostname-based label as fallback
            iface_status = _prometheus_query(
                f'interface_ifOperStatus{{host="{device_name}"}}'
            )
        if iface_status:
            metrics["interface_oper_status"] = [
                {
                    "interface": s["metric"].get("ifDescr", s["metric"].get("interface", "")),
                    "status": "up" if float(s["value"][1]) == 1 else "down",
                }
                for s in iface_status
            ]
        else:
            metrics["interface_oper_status"] = "not available (SNMP metrics not collected)"

        return json.dumps(metrics, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_interface_metrics(device_name: str, interface_name: str = "") -> str:
    """
    Get traffic and error counters for device interfaces from Prometheus.

    Reports per interface:
    - Inbound / outbound octets (bytes)
    - Inbound / outbound unicast packets
    - Inbound / outbound errors and discards
    - Interface speed

    Requires SNMP polling to be active for the device. Returns a clear message
    if metrics are not yet collected.

    Args:
        device_name:    Hostname of the device (e.g., 'spine1').
        interface_name: Optional — filter to a specific interface (e.g., 'Ethernet1').
                        Leave empty to return all interfaces.

    Returns:
        JSON with per-interface traffic counters.
    """
    try:
        dev_result = _nautobot_get("dcim/devices/", {"name": device_name, "depth": 1})
        primary_ip_cidr = None
        if dev_result["count"] > 0:
            dev = dev_result["results"][0]
            primary_ip_cidr = (
                dev.get("primary_ip4", {}).get("address") if dev.get("primary_ip4") else None
            )
        primary_ip = primary_ip_cidr.split("/")[0] if primary_ip_cidr else device_name

        host_filter = f'agent_host="{primary_ip}"'
        iface_filter = f',ifDescr="{interface_name}"' if interface_name else ""
        selector = "{" + host_filter + iface_filter + "}"

        metric_names = [
            "interface_ifHCInOctets",
            "interface_ifHCOutOctets",
            "interface_ifHCInUcastPkts",
            "interface_ifHCOutUcastPkts",
            "interface_ifInErrors",
            "interface_ifOutErrors",
            "interface_ifInDiscards",
            "interface_ifOutDiscards",
            "interface_ifHighSpeed",
            "interface_ifOperStatus",
        ]

        by_interface: dict[str, dict] = {}
        any_data = False
        for metric in metric_names:
            results = _prometheus_query(f"{metric}{selector}")
            for r in results:
                any_data = True
                iface = r["metric"].get("ifDescr", r["metric"].get("interface", "unknown"))
                if iface not in by_interface:
                    by_interface[iface] = {"interface": iface}
                key = metric.replace("interface_if", "").lower()
                val = float(r["value"][1])
                if metric == "interface_ifOperStatus":
                    by_interface[iface][key] = "up" if val == 1 else "down"
                else:
                    by_interface[iface][key] = int(val)

        if not any_data:
            return json.dumps({
                "device": device_name,
                "note": "No SNMP interface metrics found. "
                        "Metrics are collected when Telegraf SNMP polling is active for this device.",
                "interfaces": [],
            }, indent=2)

        return json.dumps({
            "device": device_name,
            "interfaces": list(by_interface.values()),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_active_alerts() -> str:
    """
    Get all currently firing alerts from Prometheus Alertmanager.

    Use this at the START of any incident investigation to identify what
    problems are currently active before querying metrics or logs.

    Returns:
        JSON with active alert details including name, severity, instance, and description.
    """
    try:
        resp = httpx.get(f"{settings.alertmanager_url}/api/v2/alerts", timeout=30)
        resp.raise_for_status()
        alerts = resp.json()
        result = [
            {
                "name": a.get("labels", {}).get("alertname"),
                "severity": a.get("labels", {}).get("severity"),
                "instance": a.get("labels", {}).get("instance"),
                "summary": a.get("annotations", {}).get("summary"),
                "description": a.get("annotations", {}).get("description"),
                "starts_at": a.get("startsAt"),
                "state": a.get("status", {}).get("state"),
            }
            for a in alerts
        ]
        return json.dumps({"active_alert_count": len(result), "alerts": result}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_recent_alert_events(limit: int = 20) -> str:
    """
    Get recently ingested alert events from the Alertmanager webhook receiver.

    Use this to see recent alert history (including resolved alerts), not just
    what is currently firing. Complements get_active_alerts().

    Args:
        limit: Number of most recent events to fetch (1–200).

    Returns:
        JSON with recent alert events and count.
    """
    limit = min(max(limit, 1), 200)
    try:
        resp = httpx.get(
            f"{settings.alert_event_receiver_url}/events",
            params={"limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def query_prometheus(promql_query: str, time_range_minutes: int = 60) -> str:
    """
    Run a raw PromQL query against Prometheus.

    Use this for custom or advanced metric queries when the dedicated tools
    (get_device_metrics, get_interface_metrics) are not sufficient.

    Available metric families:
    - ping_*             : ICMP probe results (labels: url)
    - interface_if*      : SNMP interface counters (labels: agent_host, ifDescr)
    - bgp_peer_bgpPeer*  : BGP session state via SNMP (labels: agent_host, bgpPeerRemoteAddr)
    - up                 : Scrape target availability

    Args:
        promql_query:       A valid PromQL expression.
        time_range_minutes: Look-back window in minutes for range queries.

    Returns:
        JSON with query results (up to 20 series).
    """
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=time_range_minutes)
        resp = httpx.get(
            f"{settings.prometheus_url}/api/v1/query_range",
            params={
                "query": promql_query,
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "step": "60s",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] == "success":
            results = data["data"]["result"]
            summary = [
                {
                    "metric": r["metric"],
                    "latest_value": r["values"][-1] if r["values"] else None,
                }
                for r in results[:20]
            ]
            return json.dumps({
                "status": "success",
                "series_count": len(results),
                "results": summary,
            }, indent=2)
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tier 3 – Loki Log Analysis ────────────────────────────────────────────────

def _loki_query(logql: str, minutes: int = 60, limit: int = 50) -> list[dict]:
    """Run a LogQL query against Loki and return sorted log entries."""
    end_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    start_ns = int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1e9)
    resp = httpx.get(
        f"{settings.loki_url}/loki/api/v1/query_range",
        params={"query": logql, "start": start_ns, "end": end_ns, "limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    lines = []
    for stream in data.get("data", {}).get("result", []):
        device = stream.get("stream", {}).get("device", "")
        for ts, line in stream.get("values", []):
            lines.append({"timestamp": ts, "device": device, "message": line})
    lines.sort(key=lambda x: x["timestamp"], reverse=True)
    return lines


@tool
def query_logs(device: str = "", log_pattern: str = "", time_range_minutes: int = 60) -> str:
    """
    Query Loki for device syslog messages matching a pattern.

    Use this for custom or advanced log searches. For common event types use
    the dedicated tools: get_interface_events(), get_bgp_events(), get_recent_errors().

    Args:
        device:             Optional — filter logs to a specific device hostname.
        log_pattern:        Optional — text pattern to search within log lines.
        time_range_minutes: How far back to search (default 60 minutes).

    Returns:
        JSON with matching log entries (newest first, up to 50).
    """
    try:
        logql = '{job="syslog"}'
        if device:
            logql = f'{{job="syslog", device="{device}"}}'
        if log_pattern:
            logql += f' |= "{log_pattern}"'
        lines = _loki_query(logql, minutes=time_range_minutes, limit=50)
        return json.dumps({"log_count": len(lines), "logs": lines}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_interface_events(device_name: str = "", time_range_minutes: int = 60) -> str:
    """
    Get interface state-change events (up/down flaps) from Loki syslog.

    Searches for log patterns indicating link state changes such as:
    "changed state to", "line protocol", "went down", "came up".

    Use this during incident investigation after checking get_active_alerts()
    and get_device_metrics() to understand interface-level event history.

    Args:
        device_name:        Optional — restrict to a specific device.
        time_range_minutes: How far back to search (default 60 minutes).

    Returns:
        JSON with interface event log entries (newest first).
    """
    try:
        base = f'{{job="syslog", device="{device_name}"}}' if device_name else '{job="syslog"}'
        patterns = ["changed state to", "line protocol", "went down", "came up",
                    "link down", "link up", "interface"]
        combined = base + ' |~ "(?i)(' + "|".join(patterns) + ')"'
        lines = _loki_query(combined, minutes=time_range_minutes, limit=100)
        return json.dumps({
            "device": device_name or "all",
            "time_range_minutes": time_range_minutes,
            "event_count": len(lines),
            "events": lines,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_bgp_events(device_name: str = "", time_range_minutes: int = 60) -> str:
    """
    Get BGP session state-change events from Loki syslog.

    Searches for log patterns indicating BGP state changes such as:
    "BGP", "neighbor", "Established", "Idle", "OpenSent", "session".

    Use this during BGP incident investigation after checking get_active_alerts()
    and get_device_metrics(). Combine with get_interface_events() for full context.

    Args:
        device_name:        Optional — restrict to a specific device.
        time_range_minutes: How far back to search (default 60 minutes).

    Returns:
        JSON with BGP event log entries (newest first).
    """
    try:
        base = f'{{job="syslog", device="{device_name}"}}' if device_name else '{job="syslog"}'
        combined = base + ' |~ "(?i)(bgp|neighbor|established|idle|opensent|active state)"'
        lines = _loki_query(combined, minutes=time_range_minutes, limit=100)
        return json.dumps({
            "device": device_name or "all",
            "time_range_minutes": time_range_minutes,
            "event_count": len(lines),
            "events": lines,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_recent_errors(device_name: str = "", time_range_minutes: int = 60) -> str:
    """
    Get recent ERROR and WARNING level log entries from Loki syslog.

    Use this for general health checks, initial triage, or when an alert does
    not clearly indicate which subsystem is failing.

    Args:
        device_name:        Optional — restrict to a specific device.
        time_range_minutes: How far back to search (default 60 minutes).

    Returns:
        JSON with error/warning log entries (newest first, up to 100).
    """
    try:
        base = f'{{job="syslog", device="{device_name}"}}' if device_name else '{job="syslog"}'
        combined = base + ' |~ "(?i)(error|warning|critical|fail|exception)"'
        lines = _loki_query(combined, minutes=time_range_minutes, limit=100)
        return json.dumps({
            "device": device_name or "all",
            "time_range_minutes": time_range_minutes,
            "entry_count": len(lines),
            "entries": lines,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tier 4 – Ansible Actions ──────────────────────────────────────────────────

@tool
def run_ansible_playbook(
    playbook: str,
    devices: list[str] | None = None,
    check_mode: bool = True,
    extra_vars: dict | None = None,
) -> str:
    """
    Execute an Ansible playbook against network devices.

    SAFETY: Always runs in check_mode=True (dry-run) by default.
    Only set check_mode=False when the user has explicitly approved execution
    with language like "approved", "execute", or "apply for real".

    Use get_all_devices() to confirm device names before running.

    Args:
        playbook:   Playbook filename from ansible/playbooks/ (with or without .yml).
        devices:    Optional list of device hostnames to limit execution scope.
        check_mode: Dry-run if True (default). Set False only with user approval.
        extra_vars: Additional variables to pass to the playbook.

    Returns:
        JSON with return code, stdout, stderr, and success flag.
    """
    if not playbook.endswith(".yml"):
        playbook += ".yml"

    cmd = [
        "docker", "exec", "netauto-ansible-1",
        "ansible-playbook",
        f"/ansible/playbooks/{playbook}",
        "-i", "/ansible/inventory/lab.yml",
    ]
    if check_mode:
        cmd.append("--check")
    if devices:
        cmd.extend(["--limit", ",".join(devices)])
    if extra_vars:
        cmd.extend(["-e", json.dumps(extra_vars)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return json.dumps({
            "playbook": playbook,
            "check_mode": check_mode,
            "return_code": result.returncode,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr,
            "success": result.returncode == 0,
        }, indent=2)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Playbook timed out after 120 seconds."})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool sets per agent ───────────────────────────────────────────────────────

# All Nautobot discovery tools
_NAUTOBOT_TOOLS = [
    get_all_devices,
    get_device_info,
    get_device_interfaces,
    get_topology,
    get_connected_devices,
    get_vlans,
    get_prefixes,
    get_ip_addresses,
    get_available_ips,
    search_nautobot,
    get_devices_by_location,
]

# All Prometheus metric tools
_PROMETHEUS_TOOLS = [
    get_device_metrics,
    get_interface_metrics,
    get_active_alerts,
    get_recent_alert_events,
    query_prometheus,
]

# All Loki log tools
_LOKI_TOOLS = [
    get_interface_events,
    get_bgp_events,
    get_recent_errors,
    query_logs,
]

OPS_TOOLS = _NAUTOBOT_TOOLS + _PROMETHEUS_TOOLS + _LOKI_TOOLS + [run_ansible_playbook]

ENG_TOOLS = _NAUTOBOT_TOOLS + [
    get_device_metrics,       # useful for validating current state
    get_interface_metrics,    # useful for bandwidth planning
    get_active_alerts,        # useful for checking impact before changes
    run_ansible_playbook,
]
