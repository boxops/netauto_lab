#!/usr/bin/env python3
"""
Nautobot initial data loader.
Run once after `nautobot-server migrate` to seed the database
with sites, device roles, platforms, and common config.
"""

import os
import sys

import pynautobot

NAUTOBOT_URL = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
NAUTOBOT_TOKEN = os.getenv("NAUTOBOT_SUPERUSER_API_TOKEN", "")

nb = pynautobot.api(NAUTOBOT_URL, token=NAUTOBOT_TOKEN)


def create_or_get(endpoint, unique_field: str, data: dict):
    """Create an object if it doesn't already exist."""
    existing = endpoint.filter(**{unique_field: data[unique_field]})
    if existing:
        return existing[0]
    return endpoint.create(data)


def main():
    print("=== Nautobot initializer starting ===")

    # ── Regions ──────────────────────────────────────────────────────────────
    regions = [
        {"name": "North America", "slug": "north-america"},
        {"name": "Europe", "slug": "europe"},
    ]
    for r in regions:
        create_or_get(nb.dcim.regions, "slug", r)
        print(f"  Region: {r['name']}")

    # ── Sites ────────────────────────────────────────────────────────────────
    na_region = nb.dcim.regions.get(slug="north-america")
    sites = [
        {"name": "site-nyc", "slug": "site-nyc", "status": "active", "region": na_region.id},
        {"name": "site-sfo", "slug": "site-sfo", "status": "active", "region": na_region.id},
        {"name": "site-lab", "slug": "site-lab", "status": "active", "region": na_region.id},
    ]
    for s in sites:
        create_or_get(nb.dcim.sites, "slug", s)
        print(f"  Site: {s['name']}")

    # ── Device Roles ──────────────────────────────────────────────────────────
    roles = [
        {"name": "Spine", "slug": "spine", "color": "aa1409"},
        {"name": "Leaf", "slug": "leaf", "color": "4caf50"},
        {"name": "Border-Leaf", "slug": "border-leaf", "color": "2196f3"},
        {"name": "Router", "slug": "router", "color": "ff9800"},
        {"name": "Firewall", "slug": "firewall", "color": "9c27b0"},
        {"name": "Server", "slug": "server", "color": "607d8b"},
    ]
    for role in roles:
        create_or_get(nb.dcim.device_roles, "slug", role)
        print(f"  Device Role: {role['name']}")

    # ── Manufacturers ─────────────────────────────────────────────────────────
    manufacturers = [
        {"name": "Arista Networks", "slug": "arista"},
        {"name": "Cisco Systems", "slug": "cisco"},
        {"name": "Juniper Networks", "slug": "juniper"},
        {"name": "Nokia", "slug": "nokia"},
    ]
    for m in manufacturers:
        create_or_get(nb.dcim.manufacturers, "slug", m)
        print(f"  Manufacturer: {m['name']}")

    arista = nb.dcim.manufacturers.get(slug="arista")
    cisco = nb.dcim.manufacturers.get(slug="cisco")
    juniper = nb.dcim.manufacturers.get(slug="juniper")
    nokia = nb.dcim.manufacturers.get(slug="nokia")

    # ── Device Types ─────────────────────────────────────────────────────────
    device_types = [
        {"model": "cEOS", "slug": "ceos", "manufacturer": arista.id, "u_height": 1},
        {"model": "cEOS-lab", "slug": "ceos-lab", "manufacturer": cisco.id, "u_height": 1},
        {"model": "vMX", "slug": "vmx", "manufacturer": juniper.id, "u_height": 1},
        {"model": "SR Linux", "slug": "sr-linux", "manufacturer": nokia.id, "u_height": 1},
    ]
    for dt in device_types:
        create_or_get(nb.dcim.device_types, "slug", dt)
        print(f"  Device Type: {dt['model']}")

    # ── Platforms ─────────────────────────────────────────────────────────────
    platforms = [
        {"name": "Arista EOS", "slug": "arista_eos", "manufacturer": arista.id, "napalm_driver": "eos"},
        {"name": "Cisco IOS", "slug": "cisco_ios", "manufacturer": cisco.id, "napalm_driver": "ios"},
        {"name": "Cisco IOS-XR", "slug": "cisco_iosxr", "manufacturer": cisco.id, "napalm_driver": "iosxr"},
        {"name": "Cisco NX-OS", "slug": "cisco_nxos", "manufacturer": cisco.id, "napalm_driver": "nxos"},
        {"name": "Juniper JunOS", "slug": "juniper_junos", "manufacturer": juniper.id, "napalm_driver": "junos"},
        {"name": "Nokia SR Linux", "slug": "nokia_srlinux", "manufacturer": nokia.id, "napalm_driver": "srlinux"},
    ]
    for p in platforms:
        create_or_get(nb.dcim.platforms, "slug", p)
        print(f"  Platform: {p['name']}")

    # ── Prefixes ──────────────────────────────────────────────────────────────
    lab_site = nb.dcim.sites.get(slug="site-lab")
    prefixes = [
        {"prefix": "10.0.0.0/8", "status": "container", "description": "Lab supernet"},
        {"prefix": "10.10.0.0/16", "status": "active", "site": lab_site.id, "description": "Lab management"},
        {"prefix": "172.16.0.0/12", "status": "container", "description": "RFC1918 block"},
        {"prefix": "192.168.0.0/16", "status": "container", "description": "RFC1918 block"},
    ]
    for pfx in prefixes:
        existing = nb.ipam.prefixes.filter(prefix=pfx["prefix"])
        if not existing:
            nb.ipam.prefixes.create(pfx)
        print(f"  Prefix: {pfx['prefix']}")

    # ── VLANs ─────────────────────────────────────────────────────────────────
    vlans = [
        {"vid": 1, "name": "default", "status": "active", "site": lab_site.id},
        {"vid": 100, "name": "management", "status": "active", "site": lab_site.id},
        {"vid": 200, "name": "servers", "status": "active", "site": lab_site.id},
        {"vid": 300, "name": "transit", "status": "active", "site": lab_site.id},
    ]
    for vlan in vlans:
        existing = nb.ipam.vlans.filter(vid=vlan["vid"], site=lab_site.id)
        if not existing:
            nb.ipam.vlans.create(vlan)
        print(f"  VLAN: {vlan['vid']} - {vlan['name']}")

    # ── Custom Fields ─────────────────────────────────────────────────────────
    cf_snmp = {
        "name": "snmp_community",
        "label": "SNMP Community",
        "type": "text",
        "content_types": ["dcim.device"],
        "default": "public",
    }
    existing = nb.extras.custom_fields.filter(name="snmp_community")
    if not existing:
        nb.extras.custom_fields.create(cf_snmp)
    print("  Custom Field: snmp_community")

    cf_mgmt_ip = {
        "name": "oob_ip",
        "label": "OOB Management IP",
        "type": "text",
        "content_types": ["dcim.device"],
    }
    existing = nb.extras.custom_fields.filter(name="oob_ip")
    if not existing:
        nb.extras.custom_fields.create(cf_mgmt_ip)
    print("  Custom Field: oob_ip")

    print("\n=== Nautobot initializer complete ===")


if __name__ == "__main__":
    main()
