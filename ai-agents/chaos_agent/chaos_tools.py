"""
Dedicated chaos engineering tools for controlled network disruption.

Each tool is paired with a restore counterpart and defaults to check_mode=True.
All tools call Ansible playbooks via the shared run_ansible_playbook runner.
"""
from __future__ import annotations

import json

from langchain_core.tools import tool

from shared.tools import run_ansible_playbook as _ansible

_run = _ansible.func


@tool
def shutdown_interface(
    device: str,
    interface: str,
    check_mode: bool = True,
) -> str:
    """
    Admin-shut a network interface on a device to simulate a link failure.

    Args:
        device: Device hostname (e.g. leaf1, spine2).
        interface: Interface name (e.g. Ethernet1, Management0).
        check_mode: If True, performs a dry-run only (default: True).

    Returns:
        JSON result with playbook output and success flag.
    """
    result = _run(
        playbook="chaos_shutdown_interface",
        devices=[device],
        check_mode=check_mode,
        extra_vars={"target_interface": interface, "target_device": device},
    )
    data = json.loads(result)
    data["chaos_action"] = "shutdown_interface"
    data["device"] = device
    data["interface"] = interface
    return json.dumps(data, indent=2)


@tool
def restore_interface(
    device: str,
    interface: str,
    check_mode: bool = True,
) -> str:
    """
    No-shutdown a previously admin-shut interface to restore normal operation.

    Args:
        device: Device hostname (e.g. leaf1, spine2).
        interface: Interface name (e.g. Ethernet1, Management0).
        check_mode: If True, performs a dry-run only (default: True).

    Returns:
        JSON result with playbook output and success flag.
    """
    result = _run(
        playbook="chaos_restore_interface",
        devices=[device],
        check_mode=check_mode,
        extra_vars={"target_interface": interface, "target_device": device},
    )
    data = json.loads(result)
    data["chaos_action"] = "restore_interface"
    data["device"] = device
    data["interface"] = interface
    return json.dumps(data, indent=2)


@tool
def flap_bgp_neighbor(
    device: str,
    neighbor_ip: str,
    method: str = "soft",
    check_mode: bool = True,
) -> str:
    """
    Clear a BGP session to simulate a BGP peer flap.

    Args:
        device: Device hostname where the BGP session lives.
        neighbor_ip: IP address of the BGP peer to clear.
        method: 'soft' (default, graceful route-refresh) or 'hard' (full reset).
        check_mode: If True, performs a dry-run only (default: True).

    Returns:
        JSON result with playbook output and success flag.
    """
    result = _run(
        playbook="clear_bgp_session",
        devices=[device],
        check_mode=check_mode,
        extra_vars={
            "bgp_peer": neighbor_ip,
            "target_device": device,
            "bgp_clear_method": method,
        },
    )
    data = json.loads(result)
    data["chaos_action"] = "flap_bgp_neighbor"
    data["device"] = device
    data["neighbor_ip"] = neighbor_ip
    data["method"] = method
    return json.dumps(data, indent=2)


@tool
def verify_bgp_state(
    device: str,
    neighbor_ip: str,
) -> str:
    """
    Check whether a BGP neighbor session is Established on a device.
    Use this after a flap to confirm recovery or to establish a baseline.

    Args:
        device: Device hostname.
        neighbor_ip: IP address of the BGP peer to verify.

    Returns:
        JSON result with BGP state output.
    """
    result = _run(
        playbook="validate_bgp",
        devices=[device],
        check_mode=False,
        extra_vars={"bgp_peer": neighbor_ip, "target_device": device},
    )
    data = json.loads(result)
    data["chaos_action"] = "verify_bgp_state"
    data["device"] = device
    data["neighbor_ip"] = neighbor_ip
    return json.dumps(data, indent=2)


CHAOS_TOOLS = [
    shutdown_interface,
    restore_interface,
    flap_bgp_neighbor,
    verify_bgp_state,
]
