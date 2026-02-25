"""
Shared tools available to all network AI agents.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
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
        result = _nautobot_get("dcim/devices/", {"name": device_name})
        if result["count"] == 0:
            return json.dumps({"error": f"Device '{device_name}' not found in Nautobot."})
        device = result["results"][0]
        return json.dumps({
            "name": device["name"],
            "site": device.get("site", {}).get("name"),
            "role": device.get("role", {}).get("name"),
            "platform": device.get("platform", {}).get("name"),
            "primary_ip": device.get("primary_ip4", {}).get("address") if device.get("primary_ip4") else None,
            "status": device.get("status", {}).get("value"),
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


@tool
def search_nautobot(query: str) -> str:
    """
    Search Nautobot for devices, prefixes, VLANs, circuits matching a query.

    Args:
        query: Search term (e.g., device name, IP address, site name).

    Returns:
        JSON string with matching objects.
    """
    try:
        result = _nautobot_get("extras/search/", {"q": query})
        return json.dumps(result, indent=2)
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
        end = datetime.utcnow()
        start = end - timedelta(minutes=time_range_minutes)
        resp = httpx.get(
            f"{settings.prometheus_url}/api/v1/query_range",
            params={
                "query": promql_query,
                "start": start.isoformat() + "Z",
                "end": end.isoformat() + "Z",
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
            f"{settings.prometheus_url.replace('9090', '9093')}/api/v2/alerts",
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
        end_ns = int(datetime.utcnow().timestamp() * 1e9)
        start_ns = int((datetime.utcnow() - timedelta(minutes=time_range_minutes)).timestamp() * 1e9)

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
    query_prometheus,
    get_active_alerts,
    query_logs,
    run_ansible_playbook,
    search_nautobot,
]

ENG_TOOLS = [
    get_device_info,
    get_connected_devices,
    search_nautobot,
    get_available_ips,
    query_prometheus,
    run_ansible_playbook,
]
