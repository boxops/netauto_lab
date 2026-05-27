"""
Shared tools available to all network AI agents.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from langchain.tools import tool

from shared.config import settings


# ── Nautobot tools ────────────────────────────────────────────────────────────

def _nautobot_get(path: str, params: dict | None = None) -> dict:
    """Make an authenticated GET request to Nautobot."""
    resp = httpx.get(
        f"{settings.nautobot_url}/api/{path.lstrip('/')}",
        headers={"Authorization": f"Token {settings.nautobot_token}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


@tool
def get_device_info(device_name: str) -> str:
    """
    Get details about a network device from Nautobot Source of Truth.

    Args:
        device_name: The hostname or name of the device.

    Returns:
        JSON string with device details including site, role, platform, IP addresses.
    """
    try:
        result = _nautobot_get("dcim/devices/", {"name": device_name, "depth": 1})
        if result["count"] == 0:
            return json.dumps({"error": f"Device '{device_name}' not found in Nautobot."})
        device = result["results"][0]
        return json.dumps({
            "name": device["name"],
            "location": device.get("location", {}).get("name") if device.get("location") else None,
            "role": device.get("role", {}).get("name") if device.get("role") else None,
            "platform": device.get("platform", {}).get("name") if device.get("platform") else None,
            "primary_ip": device.get("primary_ip4", {}).get("address") if device.get("primary_ip4") else None,
            "status": device.get("status", {}).get("name") if device.get("status") else None,
            "serial": device.get("serial"),
            "nautobot_url": f"{settings.nautobot_url}/dcim/devices/{device['id']}/",
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_connected_devices(device_name: str) -> str:
    """
    Get the list of devices directly connected to the specified device.

    Args:
        device_name: The hostname of the device to explore neighbors for.

    Returns:
        JSON string listing connected devices and the interfaces used.
    """
    try:
        result = _nautobot_get("dcim/devices/", {"name": device_name})
        if result["count"] == 0:
            return json.dumps({"error": f"Device '{device_name}' not found."})
        device_id = result["results"][0]["id"]
        cables = _nautobot_get("dcim/cables/", {"device": device_name})
        neighbors = []
        for cable in cables.get("results", []):
            for endpoint in cable.get("a_terminations", []) + cable.get("b_terminations", []):
                if endpoint.get("object", {}).get("device", {}).get("id") != device_id:
                    neighbors.append({
                        "device": endpoint.get("object", {}).get("device", {}).get("name"),
                        "interface": endpoint.get("object", {}).get("name"),
                    })
        return json.dumps({"device": device_name, "neighbors": neighbors}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _summarize_device(d: dict) -> dict:
    return {
        "name": d.get("name"),
        "location": d.get("location", {}).get("name") if d.get("location") else None,
        "role": d.get("role", {}).get("name") if d.get("role") else None,
        "platform": d.get("platform", {}).get("name") if d.get("platform") else None,
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
    }


def _summarize_circuit(c: dict) -> dict:
    return {
        "cid": c.get("cid"),
        "provider": c.get("provider", {}).get("name") if c.get("provider") else None,
        "type": c.get("circuit_type", {}).get("name") if c.get("circuit_type") else None,
        "status": c.get("status", {}).get("name") if c.get("status") else None,
    }


@tool
def search_nautobot(query: str) -> str:
    """
    Search Nautobot for devices, prefixes, VLANs, and circuits matching a query.
    Pass an empty string to list all objects of each type.

    Args:
        query: Search term (e.g., device name, IP address, site name). Use "" to list all.

    Returns:
        JSON string with matching objects grouped by type.
    """
    endpoints = {
        "devices": ("dcim/devices/", {"q": query, "limit": 10, "depth": 1}, _summarize_device),
        "prefixes": ("ipam/prefixes/", {"q": query, "limit": 10, "depth": 1}, _summarize_prefix),
        "vlans": ("ipam/vlans/", {"q": query, "limit": 10, "depth": 1}, _summarize_vlan),
        "circuits": ("circuits/circuits/", {"q": query, "limit": 10, "depth": 1}, _summarize_circuit),
    }
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for label, (path, params, summarize) in endpoints.items():
        try:
            data = _nautobot_get(path, params)
            results[label] = {
                "count": data.get("count", 0),
                "results": [summarize(r) for r in data.get("results", [])[:10]],
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
    List all devices at a given location (site) from Nautobot.

    Args:
        location_name: The location/site name (e.g., 'site-lab', 'site-nyc').

    Returns:
        JSON string with device list including name, role, platform, IP, and status.
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
        devices = [_summarize_device(d) for d in result["results"]]
        return json.dumps({
            "location": location_name,
            "device_count": result["count"],
            "devices": devices,
        }, indent=2)
    except httpx.HTTPStatusError:
        return json.dumps({
            "error": f"Location '{location_name}' not recognised by Nautobot.",
            "available_locations": _available_locations(),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_available_ips(prefix: str, count: int = 1) -> str:
    """
    Find available IP addresses in a given prefix from Nautobot IPAM.

    Args:
        prefix: The prefix to search in (e.g., '10.10.0.0/16').
        count: Number of available IPs to return.

    Returns:
        JSON list of available IP addresses.
    """
    try:
        result = _nautobot_get("ipam/prefixes/", {"prefix": prefix})
        if result["count"] == 0:
            return json.dumps({"error": f"Prefix {prefix} not found."})
        prefix_id = result["results"][0]["id"]
        available = _nautobot_get(f"ipam/prefixes/{prefix_id}/available-ips/", {"count": count})
        return json.dumps(available, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Prometheus tools ──────────────────────────────────────────────────────────

@tool
def query_prometheus(promql_query: str, time_range_minutes: int = 60) -> str:
    """
    Run a PromQL query against Prometheus to retrieve metrics.

    Args:
        promql_query: A valid PromQL expression (e.g., 'up{job="icmp-probes"}').
        time_range_minutes: Look-back window in minutes for range queries.

    Returns:
        JSON string with the query results.
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
        # Summarize to avoid overly large responses
        if data["status"] == "success":
            results = data["data"]["result"]
            summary = []
            for r in results[:20]:  # Limit to 20 series
                series = {"metric": r["metric"], "latest_value": r["values"][-1] if r["values"] else None}
                summary.append(series)
            return json.dumps({"status": "success", "series_count": len(results), "results": summary}, indent=2)
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_active_alerts() -> str:
    """
    Get all currently firing alerts from Prometheus Alertmanager.

    Returns:
        JSON string with active alert details.
    """
    try:
        resp = httpx.get(
            f"{settings.alertmanager_url}/api/v2/alerts",
            timeout=30,
        )
        resp.raise_for_status()
        alerts = resp.json()
        result = []
        for alert in alerts:
            result.append({
                "name": alert.get("labels", {}).get("alertname"),
                "severity": alert.get("labels", {}).get("severity"),
                "instance": alert.get("labels", {}).get("instance"),
                "summary": alert.get("annotations", {}).get("summary"),
                "description": alert.get("annotations", {}).get("description"),
                "starts_at": alert.get("startsAt"),
                "state": alert.get("status", {}).get("state"),
            })
        return json.dumps({"active_alerts": result, "count": len(result)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def get_recent_alert_events(limit: int = 20) -> str:
    """
    Get recently ingested alert events from the Alertmanager webhook receiver.

    Args:
        limit: Number of most recent events to fetch (1-200).

    Returns:
        JSON string with recent alert events and count.
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


# ── Loki tools ────────────────────────────────────────────────────────────────

@tool
def query_logs(device: str = "", log_pattern: str = "", time_range_minutes: int = 60) -> str:
    """
    Query Loki for device syslog messages matching a pattern.

    Args:
        device: Device hostname to filter logs for (optional).
        log_pattern: Text pattern to search for in logs (optional).
        time_range_minutes: How far back to search in minutes.

    Returns:
        JSON string with matching log entries.
    """
    try:
        end_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
        start_ns = int((datetime.now(timezone.utc) - timedelta(minutes=time_range_minutes)).timestamp() * 1e9)

        logql = '{job="syslog"}'
        if device:
            logql = '{job="syslog", device="' + device + '"}'
        if log_pattern:
            logql += f' |= "{log_pattern}"'

        resp = httpx.get(
            f"{settings.loki_url}/loki/api/v1/query_range",
            params={"query": logql, "start": start_ns, "end": end_ns, "limit": 50},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        lines = []
        for stream in data.get("data", {}).get("result", []):
            for ts, line in stream.get("values", []):
                lines.append({"timestamp": ts, "message": line})
        lines.sort(key=lambda x: x["timestamp"], reverse=True)
        return json.dumps({"log_count": len(lines), "logs": lines[:50]}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ansible tools ─────────────────────────────────────────────────────────────

@tool
def run_ansible_playbook(
    playbook: str,
    devices: list[str] | None = None,
    check_mode: bool = True,
    extra_vars: dict | None = None,
) -> str:
    """
    Execute an Ansible playbook. Runs in check mode by default for safety.

    IMPORTANT: Always use check_mode=True unless the user has explicitly approved execution.

    Args:
        playbook: Playbook filename from the ansible/playbooks directory.
        devices: List of device names to limit execution to.
        check_mode: If True, performs a dry-run only (default: True).
        extra_vars: Additional variables to pass to the playbook.

    Returns:
        JSON string with execution result or dry-run output.
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return json.dumps({
            "playbook": playbook,
            "check_mode": check_mode,
            "return_code": result.returncode,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr,
            "success": result.returncode == 0,
        }, indent=2)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Playbook execution timed out after 120 seconds."})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Common tool sets ──────────────────────────────────────────────────────────

OPS_TOOLS = [
    get_device_info,
    get_connected_devices,
    get_devices_by_location,
    query_prometheus,
    get_active_alerts,
    get_recent_alert_events,
    query_logs,
    run_ansible_playbook,
    search_nautobot,
]

ENG_TOOLS = [
    get_device_info,
    get_connected_devices,
    get_devices_by_location,
    search_nautobot,
    get_available_ips,
    query_prometheus,
    run_ansible_playbook,
]
