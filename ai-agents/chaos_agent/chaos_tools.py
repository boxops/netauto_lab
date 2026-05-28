"""
Dedicated chaos engineering tools for controlled network disruption.

Each tool is paired with a restore counterpart and defaults to check_mode=True.
Configuration changes are submitted via the Nautobot 'Deploy Device Configurations' job.
Read/verification commands are submitted via the Nautobot 'Commands Runner' job.
"""
from __future__ import annotations

import json

from langchain_core.tools import tool

from shared.tools import run_show_commands as _show
from shared.tools import run_config_commands as _config

_show_fn = _show.func
_config_fn = _config.func


@tool
def shutdown_interface(
    device: str,
    interface: str,
    check_mode: bool = True,
) -> str:
    """
    Admin-shut a network interface to simulate a link failure.

    check_mode=True (default): SIMULATION — shows the current interface state
    and describes what would happen, but does NOT touch the device.

    check_mode=False: Applies "interface <name> / shutdown" via the Nautobot
    'Deploy Device Configurations' job. Requires explicit user approval.

    Args:
        device:     Device hostname as it appears in Nautobot (e.g., 'leaf1').
        interface:  Interface name (e.g., 'Ethernet1').
        check_mode: True = dry-run simulation (default). False = execute.

    Returns:
        JSON with simulation notice or job result.
    """
    if check_mode:
        # Show current interface state so the agent can describe the blast radius.
        current_state = _show_fn(
            device_name=device,
            commands=f"show interfaces {interface} status",
        )
        sim = json.loads(
            _config_fn(
                device_name=device,
                config_lines=f"interface {interface}\n shutdown",
                check_mode=True,
            )
        )
        sim["pre_change_state"] = json.loads(current_state)
        sim["chaos_action"] = "shutdown_interface"
        sim["device"] = device
        sim["interface"] = interface
        return json.dumps(sim, indent=2)

    result = json.loads(
        _config_fn(
            device_name=device,
            config_lines=f"interface {interface}\n shutdown",
            check_mode=False,
        )
    )
    result["chaos_action"] = "shutdown_interface"
    result["device"] = device
    result["interface"] = interface
    return json.dumps(result, indent=2)


@tool
def restore_interface(
    device: str,
    interface: str,
    check_mode: bool = True,
) -> str:
    """
    No-shut a previously admin-shut interface to restore normal operation.

    check_mode=True (default): SIMULATION — describes what would happen without
    touching the device.

    check_mode=False: Applies "interface <name> / no shutdown" via the Nautobot
    'Deploy Device Configurations' job. Requires explicit user approval.

    Args:
        device:     Device hostname as it appears in Nautobot (e.g., 'leaf1').
        interface:  Interface name (e.g., 'Ethernet1').
        check_mode: True = dry-run simulation (default). False = execute.

    Returns:
        JSON with simulation notice or job result.
    """
    result = json.loads(
        _config_fn(
            device_name=device,
            config_lines=f"interface {interface}\n no shutdown",
            check_mode=check_mode,
        )
    )
    result["chaos_action"] = "restore_interface"
    result["device"] = device
    result["interface"] = interface
    return json.dumps(result, indent=2)


@tool
def flap_bgp_neighbor(
    device: str,
    neighbor_ip: str,
    method: str = "soft",
    check_mode: bool = True,
) -> str:
    """
    Clear a BGP session to simulate a BGP peer flap.

    check_mode=True (default): SIMULATION — shows the current BGP neighbor state
    and describes what would happen, but does NOT clear the session.

    check_mode=False: Issues a "clear ip bgp" command via the Nautobot 'Commands Runner'
    job (with is_config=True). Requires explicit user approval.

    Args:
        device:      Device hostname where the BGP session lives (e.g., 'spine1').
        neighbor_ip: IP address of the BGP peer to clear.
        method:      'soft' (default — graceful route-refresh) or 'hard' (full reset).
        check_mode:  True = dry-run simulation (default). False = execute.

    Returns:
        JSON with simulation notice or job result.
    """
    if method not in ("soft", "hard"):
        return json.dumps({"error": "method must be 'soft' or 'hard'."})

    clear_cmd = (
        f"clear ip bgp {neighbor_ip} soft"
        if method == "soft"
        else f"clear ip bgp {neighbor_ip}"
    )

    if check_mode:
        # Show current BGP session state before describing the action.
        current_state = _show_fn(
            device_name=device,
            commands=f"show ip bgp neighbors {neighbor_ip} | include BGP state",
        )
        result = {
            "simulation": True,
            "check_mode": True,
            "chaos_action": "flap_bgp_neighbor",
            "device": device,
            "neighbor_ip": neighbor_ip,
            "method": method,
            "command_would_run": clear_cmd,
            "pre_change_state": json.loads(current_state),
            "message": (
                f"SIMULATION — BGP session NOT cleared. "
                f"Would run: '{clear_cmd}' on {device}. "
                "Re-run with check_mode=False and explicit user approval to execute."
            ),
        }
        return json.dumps(result, indent=2)

    # BGP clear is a one-shot operational command — use Commands Runner with is_config=True
    # so it sends as a config-context command (enable → send command).
    from shared.tools import _get_device_id, _resolve_job_id, _submit_job, _poll_job, _fetch_job_logs, _format_job_output, settings
    try:
        device_id = _get_device_id(device)
        job_id = _resolve_job_id("Commands Runner")
        result_id = _submit_job(job_id, {
            "device": [device_id],
            "commands": clear_cmd,
            "is_config": True,
        })
        job_result = _poll_job(result_id)
        status = job_result.get("status", {}).get("value", "UNKNOWN")
        logs = _fetch_job_logs(result_id)
        output = _format_job_output(logs, status)
        output["chaos_action"] = "flap_bgp_neighbor"
        output["device"] = device
        output["neighbor_ip"] = neighbor_ip
        output["method"] = method
        output["command_run"] = clear_cmd
        output["job_result_url"] = f"{settings.nautobot_url}/extras/job-results/{result_id}/"
        return json.dumps(output, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def verify_bgp_state(
    device: str,
    neighbor_ip: str,
) -> str:
    """
    Check whether a BGP neighbor session is Established on a device.

    Use this before a chaos experiment to document the baseline, and after
    a restore to confirm the session has recovered.

    Args:
        device:      Device hostname (e.g., 'spine1').
        neighbor_ip: IP address of the BGP peer to verify.

    Returns:
        JSON with BGP session state output from the device.
    """
    result = json.loads(
        _show_fn(
            device_name=device,
            commands=(
                f"show ip bgp neighbors {neighbor_ip} | include BGP state\n"
                f"show ip bgp summary | include {neighbor_ip}"
            ),
        )
    )
    result["chaos_action"] = "verify_bgp_state"
    result["device"] = device
    result["neighbor_ip"] = neighbor_ip
    return json.dumps(result, indent=2)


CHAOS_TOOLS = [
    shutdown_interface,
    restore_interface,
    flap_bgp_neighbor,
    verify_bgp_state,
]
