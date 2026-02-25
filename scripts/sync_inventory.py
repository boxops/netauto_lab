#!/usr/bin/env python3
"""
sync_inventory.py – Sync Containerlab topology devices to Nautobot.

After running `containerlab deploy`, this script reads the Containerlab
inventory and registers/updates all devices in Nautobot.

Usage:
    python3 scripts/sync_inventory.py [--topology containerlab/topologies/spine-leaf.yml]
    python3 scripts/sync_inventory.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
    import pynautobot
except ImportError:
    print("Missing dependencies. Run: pip install pynautobot pyyaml")
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────────────────────
NAUTOBOT_URL = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
NAUTOBOT_TOKEN = os.getenv("NAUTOBOT_SUPERUSER_API_TOKEN", "")
DEFAULT_TOPOLOGY = "containerlab/topologies/spine-leaf.yml"
CLAB_PREFIX = "clab-spine-leaf"  # Containerlab container name prefix

# Role-to-slug mapping
ROLE_SLUG_MAP = {
    "spine": "spine",
    "leaf": "leaf",
    "border-leaf": "border-leaf",
    "client": "server",
}

PLATFORM_SLUG_MAP = {
    "ceos": "arista_eos",
    "linux": "server",
    "srlinux": "nokia_srlinux",
    "vr-vmx": "juniper_junos",
}


def get_clab_inspect(topology: str) -> dict[str, Any]:
    """Run containerlab inspect to get current state."""
    try:
        result = subprocess.run(
            ["sudo", "containerlab", "inspect", "--topology", topology, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"WARNING: containerlab inspect failed: {result.stderr}")
            return {}
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"WARNING: Could not run containerlab inspect: {e}")
        return {}


def load_topology(topology_file: str) -> dict:
    """Load and parse the Containerlab topology YAML file."""
    with open(topology_file) as f:
        return yaml.safe_load(f)


def sync_to_nautobot(
    topology_file: str,
    dry_run: bool = False,
    site_slug: str = "site-lab",
) -> None:
    """Sync topology devices to Nautobot."""
    nb = pynautobot.api(NAUTOBOT_URL, token=NAUTOBOT_TOKEN)

    # Load topology
    print(f"Loading topology: {topology_file}")
    topology = load_topology(topology_file)

    # Get containerlab state
    clab_state = get_clab_inspect(topology_file)

    nodes = topology.get("topology", {}).get("nodes", {})
    lab_name = topology.get("name", "spine-leaf")

    site = nb.dcim.sites.get(slug=site_slug)
    if not site:
        print(f"ERROR: Site '{site_slug}' not found in Nautobot. Run the initializer first.")
        sys.exit(1)

    print(f"Syncing {len(nodes)} nodes to Nautobot site: {site.name}")
    print("=" * 60)

    for node_name, node_config in nodes.items():
        kind = node_config.get("kind", "ceos")
        mgmt_ip = node_config.get("mgmt-ipv4", "")
        labels = node_config.get("labels", {})
        role_name = labels.get("role", "leaf")
        bgp_as = labels.get("bgp-as", "")

        role_slug = ROLE_SLUG_MAP.get(role_name, "leaf")
        platform_slug = PLATFORM_SLUG_MAP.get(kind, "arista_eos")

        role = nb.dcim.device_roles.get(slug=role_slug)
        platform = nb.dcim.platforms.get(slug=platform_slug)

        if not role:
            print(f"  SKIP {node_name}: role '{role_slug}' not found")
            continue

        # Pick appropriate device type
        dt_slug = "ceos" if kind == "ceos" else "sr-linux"
        device_type = nb.dcim.device_types.get(slug=dt_slug)
        if not device_type:
            dt_slug = "ceos"
            device_type = nb.dcim.device_types.get(slug=dt_slug)
            if not device_type:
                print(f"  SKIP {node_name}: no suitable device type found")
                continue

        device_data = {
            "name": node_name,
            "device_type": device_type.id,
            "role": role.id,
            "site": site.id,
            "status": "active",
        }
        if platform:
            device_data["platform"] = platform.id

        # Check if device already exists
        existing = nb.dcim.devices.get(name=node_name)

        if dry_run:
            if existing:
                print(f"  UPDATE (dry-run) {node_name}: role={role_name}, platform={platform_slug}, mgmt_ip={mgmt_ip}")
            else:
                print(f"  CREATE (dry-run) {node_name}: role={role_name}, platform={platform_slug}, mgmt_ip={mgmt_ip}")
            continue

        if existing:
            existing.update(device_data)
            device = existing
            print(f"  UPDATED {node_name}")
        else:
            device = nb.dcim.devices.create(device_data)
            print(f"  CREATED {node_name} (id={device.id})")

        # Set management IP
        if mgmt_ip:
            ip_str = f"{mgmt_ip}/24"
            existing_ip = nb.ipam.ip_addresses.get(address=ip_str)
            if not existing_ip:
                try:
                    ip_obj = nb.ipam.ip_addresses.create({
                        "address": ip_str,
                        "status": "active",
                        "description": f"Management IP for {node_name}",
                    })
                    device.update({"primary_ip4": ip_obj.id})
                    print(f"    Assigned mgmt IP: {ip_str}")
                except Exception as e:
                    print(f"    WARNING: Could not assign IP {ip_str}: {e}")

        # Set custom fields (BGP AS, SNMP)
        if bgp_as:
            try:
                device.update({"custom_fields": {"bgp_as": bgp_as}})
            except Exception:
                pass

    print("=" * 60)
    if dry_run:
        print("Dry run complete. No changes made.")
    else:
        print(f"Sync complete. {len(nodes)} devices processed.")


def main():
    parser = argparse.ArgumentParser(description="Sync Containerlab topology to Nautobot")
    parser.add_argument("--topology", default=DEFAULT_TOPOLOGY, help="Path to Containerlab topology YAML")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying Nautobot")
    parser.add_argument("--site", default="site-lab", help="Nautobot site slug to assign devices to")
    args = parser.parse_args()

    if not NAUTOBOT_TOKEN:
        print("ERROR: NAUTOBOT_SUPERUSER_API_TOKEN is not set.")
        sys.exit(1)

    if not Path(args.topology).exists():
        print(f"ERROR: Topology file not found: {args.topology}")
        sys.exit(1)

    sync_to_nautobot(args.topology, dry_run=args.dry_run, site_slug=args.site)


if __name__ == "__main__":
    main()
