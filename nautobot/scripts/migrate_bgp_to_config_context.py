"""
Migrate bgp_asn custom field values → device local config context.

For each Arista EOS device that has a non-null bgp_asn custom field value
the script:
  1. Reads device.cf["bgp_asn"].
  2. Deep-merges {"bgp": {"asn": <value>}} into the device's
     local_config_context_data (creating or extending the dict as needed).
  3. Saves the device with validated_save().
  4. Clears the custom field value on the device (sets it to None) so the
     deprecated field is no longer authoritative.

After all devices have been migrated you can safely delete the bgp_asn
CustomField from Extensibility → Custom Fields in the Nautobot UI.

Usage (run once inside the Nautobot container):

    docker exec -i netauto-nautobot-1 nautobot-server shell \\
        < nautobot/scripts/migrate_bgp_to_config_context.py

The script is idempotent: re-running it is safe because it only writes
bgp.asn when the custom field value is non-null, and it skips devices
that already have bgp.asn set in their local config context to the same
value.
"""

from nautobot.dcim.models import Device

migrated = 0
skipped_already_migrated = 0
skipped_no_cf = 0

for device in Device.objects.all().order_by("name"):
    cf_asn = device.cf.get("bgp_asn")

    if cf_asn is None:
        skipped_no_cf += 1
        continue

    # Ensure local_config_context_data is a mutable dict.
    lcc = device.local_config_context_data or {}
    if not isinstance(lcc, dict):
        lcc = {}

    existing_asn = lcc.get("bgp", {}).get("asn")

    if existing_asn == cf_asn:
        # Already migrated; clear the CF value so it is no longer authoritative.
        device.cf["bgp_asn"] = None
        device.validated_save()
        skipped_already_migrated += 1
        print(f"  [already-migrated]  {device.name}  bgp.asn={cf_asn}")
        continue

    # Deep-merge: preserve any keys already in local_config_context_data.
    bgp_block = lcc.get("bgp", {})
    if not isinstance(bgp_block, dict):
        bgp_block = {}
    bgp_block["asn"] = cf_asn
    lcc["bgp"] = bgp_block
    device.local_config_context_data = lcc

    # Clear the deprecated custom field value.
    device.cf["bgp_asn"] = None

    device.validated_save()
    migrated += 1
    print(f"  [migrated]           {device.name}  bgp.asn={cf_asn}")

print()
print(
    f"Done.  migrated={migrated}  "
    f"already_migrated={skipped_already_migrated}  "
    f"no_cf_value={skipped_no_cf}"
)
print()
print("Next steps:")
print("  1. Verify each device's Local Config Context in the Nautobot UI")
print("     (Device → Advanced → Local Config Context Data).")
print("  2. Confirm the merged context (Device → Config Context tab) shows")
print("     bgp.asn correctly.")
print("  3. Once verified, delete the 'bgp_asn (deprecated)' Custom Field from")
print("     Extensibility → Custom Fields.")
