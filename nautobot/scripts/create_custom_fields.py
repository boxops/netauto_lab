"""
Create all custom fields required by custom Nautobot Jobs.

Usage (run once during initial setup, or after adding new jobs):

    docker exec -i netauto-nautobot-1 nautobot-server shell \
        < nautobot/scripts/create_custom_fields.py

The script is fully idempotent — re-running it is safe. Existing fields are
only updated if their label or default value has drifted from what is defined
here; content-type assignments are always added (never removed).

After running, verify in the Nautobot UI under:
    Extensibility → Custom Fields
"""

from django.contrib.contenttypes.models import ContentType
from nautobot.dcim.models import Device, Interface
from nautobot.extras.choices import CustomFieldTypeChoices
from nautobot.extras.models import CustomField
from nautobot.ipam.models import IPAddress

# ── Content-type helpers ───────────────────────────────────────────────────────

ct_device    = ContentType.objects.get_for_model(Device)
ct_interface = ContentType.objects.get_for_model(Interface)
ct_ipaddress = ContentType.objects.get_for_model(IPAddress)

# ── Field definitions ──────────────────────────────────────────────────────────
#
# Each entry in CUSTOM_FIELDS maps directly to a CustomField row.
#
# Required keys:
#   key           — unique slug; must match the string used in job code exactly
#   label         — human-readable name shown in the Nautobot UI
#   type          — CustomFieldTypeChoices constant (see list below)
#   content_types — list of Django ContentType objects to assign this field to
#
# Optional keys:
#   default       — default value stored in Nautobot (can be None)
#   description   — help text shown next to the field in the UI
#   required      — bool; defaults to False
#   weight        — sort order in the UI; lower = higher up
#
# CustomFieldTypeChoices values:
#   TYPE_TEXT         — free-form text string
#   TYPE_INTEGER      — integer number
#   TYPE_BOOLEAN      — True / False
#   TYPE_DATE         — ISO date string (YYYY-MM-DD)
#   TYPE_URL          — URL string
#   TYPE_SELECT       — single value from a predefined choice set
#   TYPE_MULTISELECT  — multiple values from a predefined choice set
#   TYPE_JSON         — arbitrary JSON value

CUSTOM_FIELDS = [
    # ──────────────────────────────────────────────────────────────────────────
    # IPAddress fields
    # Used by: inventory/arp_mac_sync.py
    # ──────────────────────────────────────────────────────────────────────────
    {
        "key":           "mac_address",
        "label":         "MAC Address",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_ipaddress],
        "default":       None,
        "description":   "MAC address learned from ARP table (populated by ARP/MAC Sync job).",
        "weight":        100,
    },
    {
        "key":           "arp_source_device",
        "label":         "ARP Source Device",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_ipaddress],
        "default":       None,
        "description":   "Name of the device whose ARP table provided this MAC mapping.",
        "weight":        110,
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Device fields
    # Used by: configuration/backup_configurations.py, reporting/backup_state_checker.py
    # ──────────────────────────────────────────────────────────────────────────
    {
        "key":           "can_connect",
        "label":         "Can Connect",
        "type":          CustomFieldTypeChoices.TYPE_BOOLEAN,
        "content_types": [ct_device],
        "default":       None,
        "description":   (
            "Set to True/False by the Backup job after each run. "
            "Read by Backup State Checker to determine whether connectivity failures "
            "are expected."
        ),
        "weight":        200,
    },
    {
        "key":           "last_network_data_sync",
        "label":         "Last Network Data Sync",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_device],
        "default":       None,
        "description":   "ISO-8601 timestamp of the last successful SSoT network data sync.",
        "weight":        210,
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Device fields — BGP
    # Used by: configuration/intended_configurations.py (migrate_bgp_to_config_context.py
    #          reads this field and copies its value into local_config_context_data;
    #          after migration the intended-config job reads from config context instead).
    # DEPRECATED: bgp_asn will be retired once all devices have been migrated to
    #             local config context (bgp.asn).  Do not add new references to
    #             device.cf["bgp_asn"] — use device.get_config_context()["bgp"]["asn"].
    # ──────────────────────────────────────────────────────────────────────────
    {
        "key":           "bgp_asn",
        "label":         "BGP ASN (deprecated)",
        "type":          CustomFieldTypeChoices.TYPE_INTEGER,
        "content_types": [ct_device],
        "default":       None,
        "description":   (
            "Per-device BGP Autonomous System Number. "
            "DEPRECATED: migrate values to the device local config context under "
            "bgp.asn using nautobot/scripts/migrate_bgp_to_config_context.py, "
            "then clear this field."
        ),
        "weight":        290,
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Device fields — SolarWinds integration
    # Used by: monitoring/provision_nodes_on_solarwinds.py
    # NOTE: snmpcommunity stores a plain-text string. For production environments
    # consider using Nautobot Secrets instead and removing this field.
    # ──────────────────────────────────────────────────────────────────────────
    {
        "key":           "area",
        "label":         "Area",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_device],
        "default":       None,
        "description":   "Geographic or logical area; used by the SolarWinds provisioning job.",
        "weight":        300,
    },
    {
        "key":           "customer_type",
        "label":         "Customer Type",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_device],
        "default":       "Internal",
        "description":   "Customer classification (e.g. Internal, External); used by SolarWinds job.",
        "weight":        310,
    },
    {
        "key":           "in_service",
        "label":         "In Service",
        "type":          CustomFieldTypeChoices.TYPE_BOOLEAN,
        "content_types": [ct_device],
        "default":       True,
        "description":   "Whether this device is actively in service; used by SolarWinds job.",
        "weight":        320,
    },
    {
        "key":           "is_mgmt_up",
        "label":         "Management Up",
        "type":          CustomFieldTypeChoices.TYPE_BOOLEAN,
        "content_types": [ct_device],
        "default":       True,
        "description":   "Whether the management plane is reachable; used by SolarWinds job.",
        "weight":        330,
    },
    {
        "key":           "network_layer",
        "label":         "Network Layer",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_device],
        "default":       None,
        "description":   "Network layer classification (e.g. Access, Distribution, Core); used by SolarWinds job.",
        "weight":        340,
    },
    {
        "key":           "monitoredinterfaces",
        "label":         "Monitored Interfaces",
        "type":          CustomFieldTypeChoices.TYPE_JSON,
        "content_types": [ct_device],
        "default":       None,
        "description":   "JSON list of interface names to monitor in SolarWinds (e.g. [\"GigabitEthernet0/1\"]).",
        "weight":        350,
    },
    {
        "key":           "snmpcommunity",
        "label":         "SNMP Community",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_device],
        "default":       "public",
        "description":   (
            "SNMP community string for this device. "
            "WARNING: stored in plain text — consider migrating to Nautobot Secrets for production."
        ),
        "weight":        360,
    },

    # ──────────────────────────────────────────────────────────────────────────
    # Interface fields
    # Used by: onboarding/capture_network_device_data.py (methods currently inactive)
    # Created here so the fields are available when the code is re-enabled.
    # ──────────────────────────────────────────────────────────────────────────
    {
        "key":           "speed",
        "label":         "Speed (Reported)",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_interface],
        "default":       None,
        "description":   "Interface speed as reported by the device (e.g. 1000Mb/s).",
        "weight":        400,
    },
    {
        "key":           "duplex",
        "label":         "Duplex",
        "type":          CustomFieldTypeChoices.TYPE_TEXT,
        "content_types": [ct_interface],
        "default":       None,
        "description":   "Interface duplex mode as reported by the device (e.g. Full, Half, Auto).",
        "weight":        410,
    },
]


# ── Create / update ────────────────────────────────────────────────────────────

created_count = 0
updated_count = 0
skipped_count = 0

for spec in CUSTOM_FIELDS:
    content_types = spec.pop("content_types")

    cf, created = CustomField.objects.get_or_create(
        key=spec["key"],
        defaults={
            "label":       spec["label"],
            "type":        spec["type"],
            "default":     spec.get("default"),
            "description": spec.get("description", ""),
            "required":    spec.get("required", False),
            "weight":      spec.get("weight", 100),
        },
    )

    if created:
        created_count += 1
        status = "CREATED"
    else:
        # Update mutable metadata if it has drifted — never change key or type.
        changed = False
        for attr in ("label", "description", "default", "required", "weight"):
            desired = spec.get(attr)
            if desired is not None and getattr(cf, attr) != desired:
                setattr(cf, attr, desired)
                changed = True
        if changed:
            cf.validated_save()
            updated_count += 1
            status = "UPDATED"
        else:
            skipped_count += 1
            status = "unchanged"

    # Assign to content types (additive — never removes existing assignments).
    for ct in content_types:
        cf.content_types.add(ct)

    print(f"  [{status:9s}]  {cf.key:30s}  ({cf.get_type_display()})")

print()
print(f"Done. created={created_count}  updated={updated_count}  unchanged={skipped_count}")
print()
print("Next steps:")
print("  1. Verify fields in Nautobot UI → Extensibility → Custom Fields")
print("  2. Re-run the ARP/MAC Sync job with dry_run=False to write MAC → IP mappings")
print("  3. Re-run Backup Configurations — 'can_connect' will now be set per device")
