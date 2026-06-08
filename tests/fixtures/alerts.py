"""
Canonical Alertmanager webhook payloads for pipeline tests.

Each factory returns the dict that would arrive at POST /webhook/alert.
All fingerprints are stable so tests can assert on them.
"""
from __future__ import annotations


def interface_down_payload(
    device: str = "spine2",
    interface: str = "Ethernet1",
    fingerprint: str = "fp-iface-down-001",
    status: str = "firing",
) -> dict:
    return {
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": fingerprint,
                "labels": {
                    "alertname": "InterfaceDown",
                    "severity": "critical",
                    "sysName": device,
                    "ifDescr": interface,
                    "instance": "telegraf:9273",
                },
                "annotations": {
                    "summary": f"{device} {interface} is operationally down",
                    "description": f"Interface {interface} on {device} has ifOperStatus=2",
                },
            }
        ],
    }


def bgp_peer_down_payload(
    device: str = "spine1",
    neighbor_ip: str = "10.0.0.2",
    fingerprint: str = "fp-bgp-down-001",
    status: str = "firing",
) -> dict:
    return {
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": fingerprint,
                "labels": {
                    "alertname": "BGPPeerDown",
                    "severity": "critical",
                    "sysName": device,
                    "neighbor": neighbor_ip,
                    "instance": "telegraf:9273",
                },
                "annotations": {
                    "summary": f"BGP peer {neighbor_ip} down on {device}",
                    "description": f"BGP session to {neighbor_ip} is not Established on {device}",
                },
            }
        ],
    }


def admin_down_payload(
    device: str = "spine2",
    interface: str = "Ethernet1",
    fingerprint: str = "fp-admin-down-001",
    status: str = "firing",
) -> dict:
    return {
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": fingerprint,
                "labels": {
                    "alertname": "InterfaceAdminDown",
                    "severity": "warning",
                    "sysName": device,
                    "ifDescr": interface,
                    "instance": "telegraf:9273",
                },
                "annotations": {
                    "summary": f"{device} {interface} is admin-shutdown",
                    "description": f"Interface {interface} on {device} has ifAdminStatus=2",
                },
            }
        ],
    }


def alert_storm_payload(
    device: str = "spine2",
    fingerprint_prefix: str = "fp-storm",
) -> dict:
    """Three alerts for the same device — simulates a link-down alert storm."""
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": f"{fingerprint_prefix}-iface",
                "labels": {
                    "alertname": "InterfaceDown",
                    "severity": "critical",
                    "sysName": device,
                    "ifDescr": "Ethernet1",
                    "instance": "telegraf:9273",
                },
                "annotations": {"summary": f"{device} Ethernet1 is down"},
            },
            {
                "status": "firing",
                "fingerprint": f"{fingerprint_prefix}-bgp",
                "labels": {
                    "alertname": "BGPPeerDown",
                    "severity": "critical",
                    "sysName": device,
                    "neighbor": "10.0.0.2",
                    "instance": "telegraf:9273",
                },
                "annotations": {"summary": f"BGP peer down on {device}"},
            },
            {
                "status": "firing",
                "fingerprint": f"{fingerprint_prefix}-admin",
                "labels": {
                    "alertname": "InterfaceAdminDown",
                    "severity": "warning",
                    "sysName": device,
                    "ifDescr": "Ethernet1",
                    "instance": "telegraf:9273",
                },
                "annotations": {"summary": f"{device} Ethernet1 admin-down"},
            },
        ],
    }
