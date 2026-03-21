#!/usr/bin/env python3
"""
Nautobot initial data loader – Nautobot 3.x compatible.
Run once after `nautobot-server migrate` to seed the database
with locations, roles, platforms, and common config.

Key Nautobot 3.x changes vs 2.x:
  - Region + Site  →  LocationType + Location
  - dcim.device_roles  →  extras.roles
  - slug fields removed from most models; filter by name instead
  - napalm_driver moved off Platform (use napalm plugin if needed)
"""

import os
import sys

import pynautobot

NAUTOBOT_URL = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
NAUTOBOT_TOKEN = os.getenv("NAUTOBOT_SUPERUSER_API_TOKEN", "")

nb = pynautobot.api(NAUTOBOT_URL, token=NAUTOBOT_TOKEN)


def create_or_get(endpoint, filter_kwargs: dict, data: dict):
    """Return the first matching object or create it."""
    existing = endpoint.filter(**filter_kwargs)
    if existing:
        return existing[0]
    return endpoint.create(data)


def main():
    print("=== Nautobot initializer starting ===")

    # ── Location Types ────────────────────────────────────────────────────────
    # Nautobot 3.x replaces Region/Site with a flexible LocationType hierarchy.
    region_type = create_or_get(
        nb.dcim.location_types, {"name": "Region"},
        {"name": "Region", "nestable": True},
    )
    print("  LocationType: Region")

    site_type = create_or_get(
        nb.dcim.location_types, {"name": "Site"},
        {"name": "Site", "nestable": False, "parent": region_type.id,
         "content_types": ["dcim.device"]},
    )
    print("  LocationType: Site")

    # ── Regions (top-level Locations) ─────────────────────────────────────────
    for name in ("North America", "Europe"):
        create_or_get(
            nb.dcim.locations, {"name": name, "location_type": region_type.id},
            {"name": name, "location_type": region_type.id, "status": "Active"},
        )
        print(f"  Region: {name}")

    na_region = nb.dcim.locations.get(name="North America", location_type=region_type.id)

    # ── Sites (child Locations) ───────────────────────────────────────────────
    for site_name in ("site-nyc", "site-sfo", "site-lab"):
        create_or_get(
            nb.dcim.locations, {"name": site_name, "location_type": site_type.id},
            {"name": site_name, "location_type": site_type.id,
             "status": "Active", "parent": na_region.id},
        )
        print(f"  Site: {site_name}")

    # ── Roles (replaces dcim.device_roles in Nautobot 3.x) ───────────────────
    roles = [
        {"name": "Spine",       "color": "aa1409", "content_types": ["dcim.device"]},
        {"name": "Leaf",        "color": "4caf50", "content_types": ["dcim.device"]},
        {"name": "Border-Leaf", "color": "2196f3", "content_types": ["dcim.device"]},
        {"name": "Router",      "color": "ff9800", "content_types": ["dcim.device"]},
        {"name": "Firewall",    "color": "9c27b0", "content_types": ["dcim.device"]},
        {"name": "Server",      "color": "607d8b", "content_types": ["dcim.device"]},
    ]
    for role in roles:
        create_or_get(nb.extras.roles, {"name": role["name"]}, role)
        print(f"  Role: {role['name']}")

    # ── Manufacturers ─────────────────────────────────────────────────────────
    for mfr_name in ("Arista Networks", "Cisco Systems", "Juniper Networks", "Nokia"):
        create_or_get(nb.dcim.manufacturers, {"name": mfr_name}, {"name": mfr_name})
        print(f"  Manufacturer: {mfr_name}")

    arista  = nb.dcim.manufacturers.get(name="Arista Networks")
    cisco   = nb.dcim.manufacturers.get(name="Cisco Systems")
    juniper = nb.dcim.manufacturers.get(name="Juniper Networks")
    nokia   = nb.dcim.manufacturers.get(name="Nokia")

    # ── Device Types ─────────────────────────────────────────────────────────
    device_types = [
        {"model": "cEOS",     "manufacturer": arista.id,  "u_height": 1},
        {"model": "cEOS-lab", "manufacturer": cisco.id,   "u_height": 1},
        {"model": "vMX",      "manufacturer": juniper.id, "u_height": 1},
        {"model": "SR Linux", "manufacturer": nokia.id,   "u_height": 1},
    ]
    for dt in device_types:
        create_or_get(
            nb.dcim.device_types,
            {"model": dt["model"], "manufacturer": dt["manufacturer"]},
            dt,
        )
        print(f"  Device Type: {dt['model']}")

    # ── Platforms ─────────────────────────────────────────────────────────────
    # napalm_driver was removed from the base Platform model in Nautobot 3.x.
    # network_driver maps to the Netmiko device_type used by onboard_device job.
    platforms = [
        {"name": "Arista EOS",     "manufacturer": arista.id,  "network_driver": "arista_eos"},
        {"name": "Cisco IOS",      "manufacturer": cisco.id,   "network_driver": "cisco_ios"},
        {"name": "Cisco IOS-XE",   "manufacturer": cisco.id,   "network_driver": "cisco_xe"},
        {"name": "Cisco IOS-XR",   "manufacturer": cisco.id,   "network_driver": "cisco_xr"},
        {"name": "Cisco NX-OS",    "manufacturer": cisco.id,   "network_driver": "cisco_nxos"},
        {"name": "Juniper JunOS",  "manufacturer": juniper.id, "network_driver": "juniper_junos"},
        {"name": "Nokia SR Linux", "manufacturer": nokia.id,   "network_driver": "nokia_srl"},
    ]
    for p in platforms:
        create_or_get(nb.dcim.platforms, {"name": p["name"]}, p)
        print(f"  Platform: {p['name']}")

    # ── Prefixes ──────────────────────────────────────────────────────────────
    prefixes = [
        {"prefix": "10.0.0.0/8",    "status": "Active", "description": "Lab supernet"},
        {"prefix": "10.10.0.0/16",  "status": "Active", "description": "Lab management"},
        {"prefix": "172.16.0.0/12", "status": "Reserved", "description": "RFC1918 block"},
        {"prefix": "192.168.0.0/16","status": "Reserved", "description": "RFC1918 block"},
    ]
    for pfx in prefixes:
        if not nb.ipam.prefixes.filter(prefix=pfx["prefix"]):
            nb.ipam.prefixes.create(pfx)
        print(f"  Prefix: {pfx['prefix']}")

    # ── VLANs ─────────────────────────────────────────────────────────────────
    vlans = [
        {"vid": 1,   "name": "default",    "status": "Active"},
        {"vid": 100, "name": "management", "status": "Active"},
        {"vid": 200, "name": "servers",    "status": "Active"},
        {"vid": 300, "name": "transit",    "status": "Active"},
    ]
    for vlan in vlans:
        if not nb.ipam.vlans.filter(vid=vlan["vid"]):
            nb.ipam.vlans.create(vlan)
        print(f"  VLAN: {vlan['vid']} - {vlan['name']}")

    # ── Custom Fields ─────────────────────────────────────────────────────────
    for cf in [
        {"name": "snmp_community", "label": "SNMP Community", "type": "text",
         "content_types": ["dcim.device"], "default": "public"},
        {"name": "oob_ip",         "label": "OOB Management IP", "type": "text",
         "content_types": ["dcim.device"]},
    ]:
        try:
            nb.extras.custom_fields.create(cf)
        except Exception as e:
            if "already exists" not in str(e) and "duplicate" not in str(e).lower():
                raise
        print(f"  Custom Field: {cf['name']}")

    # ── Secrets (SSH credentials sourced from environment variables) ──────────
    secrets_config = [
        {"name": "lab-admin-username", "variable": "ADMIN_USERNAME",
         "description": "SSH username for lab devices"},
        {"name": "lab-admin-password", "variable": "ADMIN_PASSWORD",
         "description": "SSH password for lab devices"},
        {"name": "lab-admin-secret",   "variable": "ADMIN_SECRET",
         "description": "SSH enable/secret password for lab devices"},
    ]
    secret_objs = {}
    for sc in secrets_config:
        s = create_or_get(
            nb.extras.secrets,
            {"name": sc["name"]},
            {"name": sc["name"], "provider": "environment-variable",
             "parameters": {"variable": sc["variable"]},
             "description": sc["description"]},
        )
        secret_objs[sc["name"]] = s
        print(f"  Secret: {sc['name']}")

    # ── SecretsGroup ──────────────────────────────────────────────────────────
    sg = create_or_get(
        nb.extras.secrets_groups,
        {"name": "lab-ssh-creds"},
        {"name": "lab-ssh-creds",
         "description": "Generic SSH credentials for lab devices (admin/admin)"},
    )
    print("  SecretsGroup: lab-ssh-creds")

    # ── SecretsGroupAssociations ──────────────────────────────────────────────
    associations = [
        ("lab-admin-username", "Generic", "username"),
        ("lab-admin-password", "Generic", "password"),
        ("lab-admin-secret",   "Generic", "secret"),
    ]
    for secret_name, access_type, secret_type in associations:
        create_or_get(
            nb.extras.secrets_groups_associations,
            {"secrets_group": sg.id, "access_type": access_type, "secret_type": secret_type},
            {"secrets_group": sg.id, "access_type": access_type, "secret_type": secret_type,
             "secret": secret_objs[secret_name].id},
        )
        print(f"  SecretsGroupAssociation: {access_type}/{secret_type} → {secret_name}")

    print("\n=== Nautobot initializer complete ===")


if __name__ == "__main__":
    main()
