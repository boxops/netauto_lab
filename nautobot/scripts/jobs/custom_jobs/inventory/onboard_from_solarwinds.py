"""
Purpose:
Query all SNMP-managed nodes from SolarWinds Orion (Orion.Nodes joined with
NodesCustomProperties) and onboard them to Nautobot as Device records.

All nodes are onboarded, including infrastructure devices (UPS, servers, etc.)
that have no SSH management capability – those are created without a platform.
Devices with an unknown/unmapped vendor are also onboarded without a platform.

Enable full_sync to treat SolarWinds as the Source of Truth: Nautobot devices
whose names do not appear in the query results will be deleted.

Run with dry_run=True (the default) first — it logs every planned change
without writing anything to the Nautobot inventory.
"""

from django.conf import settings
from django.core.exceptions import ValidationError

from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import connection as db_connection

from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, ObjectVar, StringVar, register_jobs
from nautobot.dcim.choices import InterfaceTypeChoices
from nautobot.dcim.models import Device, DeviceType, Interface, Location, Manufacturer, Platform
from nautobot.extras.models import Role, Status
from nautobot.extras.models.secrets import (
    SecretsGroup,
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.ipam.models import IPAddress, Namespace, Prefix

from custom_jobs.monitoring.swisclient import SwisClient

name = "Inventory"

# ---------------------------------------------------------------------------
# Vendors with no SSH management interface – onboarded without a platform;
# used for informational logging only (no longer causes a skip).
# ---------------------------------------------------------------------------
SKIP_VENDORS = frozenset({
    "merawex sp. z o. o.",              # UPS / power monitoring, SNMP-only
    "net-snmp",                         # generic Linux SNMP agent (servers)
    "linux",                            # Linux hosts
    "windows",                          # Windows hosts
    "powec as",                         # DC power monitors
    "phoenixtec power co., ltd.",       # UPS
    "american power conversion corp.",  # APC UPS
    "eaton corporation",                # UPS
    "cyber power system inc.",          # UPS
    "morningstar corporation",          # solar power monitors
    "qnap systems, inc",                # NAS
    "broadcom corporation",             # Broadcom Linux stack
    "tp-link technology co.,ltd",       # consumer-grade, no SSH
    "netgear",                          # consumer-grade
    "alcatel data networks",            # DSLAM, no SSH management
})


# ---------------------------------------------------------------------------
# Vendor substring → network_driver  (evaluated in order; first match wins)
# ---------------------------------------------------------------------------
VENDOR_PLATFORM_MAP = [
    ("mikrotik",   "mikrotik_routeros"),
    ("dasan",      "keymile_nos"),
    ("ceragon",    "ceragon_os"),
    ("netonix",    "netonix_os"),
    ("motorola",   "cambium_cnmatrix"),  # Motorola SPS (Canopy/Cambium)
    ("cambium",    "cambium_cnmatrix"),
    ("ubiquiti",   "ubiquiti_airos"),
    ("frogfoot",   "ubiquiti_airos"),    # Frogfoot Networks run Ubiquiti AirOS
    ("fs.com",     "fiberstore_fsos"),
    ("cisco",      "cisco_ios"),         # refined below from MachineType
    ("siklu",      "siklu_os"),
    ("fortinet",   "fortinet"),
    ("juniper",    "juniper_junos"),
    ("arista",     "arista_eos"),
]

# Further refine "cisco_ios" when MachineType/Description reveals otherwise
CISCO_REFINEMENTS = [
    ("IOS XR", "cisco_xr"),
    ("NX-OS",  "cisco_nxos"),
    ("IOS-XE", "cisco_xe"),
    ("S300",   "cisco_s300"),
]

# Manufacturer name normalisation (raw Vendor value → clean display name)
VENDOR_NAME_OVERRIDES = {
    "dasan co.,ltd.":              "DASAN",
    "motorola sps":                "Cambium Networks",
    "frogfoot networks":           "Ubiquiti",
    "ubiquiti networks, inc.":     "Ubiquiti",
    "fortinet, inc.":              "Fortinet",
    "siklu communication ltd":     "Siklu",
    "fs.com inc.":                 "FS.COM",
    "juniper networks, inc.":      "Juniper",
    "giganet ltd":                 "Giganet",
    "piping hot networks limited": "PipingHot Networks",
}

# Name used for the auto-created management interface on each device
MGMT_INTERFACE_NAME = "Management"


# ---------------------------------------------------------------------------
# SWQL query – single round-trip, includes CustomProperties via LEFT JOIN
# ---------------------------------------------------------------------------
def _build_swql(only_up: bool) -> str:
    where = [
        "n.ObjectSubType = 'SNMP'",
        "n.IPAddress IS NOT NULL",
    ]
    if only_up:
        where.append("n.Status = 1")
    return f"""
SELECT
    n.NodeID,
    n.Caption,
    n.IPAddress,
    n.Vendor,
    n.MachineType,
    n.Description,
    n.Community,
    cp.Area        AS cp_area,
    cp.Device_Type AS cp_device_type
FROM Orion.Nodes n
LEFT JOIN Orion.NodesCustomProperties cp ON n.NodeID = cp.NodeID
WHERE {" AND ".join(where)}
ORDER BY n.Caption
"""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _swis_client(credential: SecretsGroup) -> SwisClient:
    server = credential.get_secret_value(
        access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
        secret_type=SecretsGroupSecretTypeChoices.TYPE_KEY,
    )
    username = credential.get_secret_value(
        access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
        secret_type=SecretsGroupSecretTypeChoices.TYPE_USERNAME,
    )
    password = credential.get_secret_value(
        access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
        secret_type=SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
    )
    return SwisClient(server, username, password)


def _map_platform(vendor: str, machine_type: str, description: str):
    """Return the Nautobot network_driver string, or None if not mappable."""
    vl = (vendor or "").lower()
    for substring, driver in VENDOR_PLATFORM_MAP:
        if substring in vl:
            if driver == "cisco_ios":
                combined = f"{machine_type} {description}".upper()
                for keyword, refined in CISCO_REFINEMENTS:
                    if keyword.upper() in combined:
                        return refined
            return driver
    return None


def _extract_model(machine_type: str) -> str:
    """
    Extract hardware model from a MachineType string.

    RouterOS example:
        "RouterOS 6.49.3 (stable) on CCR1009-7G-1C-1S+" → "CCR1009-7G-1C-1S+"
    All others: the raw value, truncated to 50 characters.
    """
    mt = (machine_type or "").strip()
    if " on " in mt:
        return mt.split(" on ")[-1].strip()[:50]
    return mt[:50] or "Unknown"


def _canonical_vendor(vendor: str) -> str:
    return VENDOR_NAME_OVERRIDES.get((vendor or "").lower(), vendor or "Unknown")


def _get_default_sw_credential():
    try:
        return SecretsGroup.objects.get(name="SOLARWINDS_NPM_API")
    except SecretsGroup.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

class OnboardFromSolarWinds(Job):
    """Fetch all SNMP nodes from SolarWinds Orion and upsert them into Nautobot."""

    class Meta:
        name = "Onboard Devices from SolarWinds"
        description = (
            "Queries Orion.Nodes + NodesCustomProperties and creates/updates "
            "Device, DeviceType, Manufacturer, and IPAddress records in Nautobot. "
            "All nodes are onboarded regardless of SSH capability. "
            "Always run with dry_run=True first to preview planned changes."
        )
        has_sensitive_variables = False
        soft_time_limit = 3600   # 1 hour
        time_limit = 4800        # 80 minutes
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    sw_credential = ObjectVar(
        model=SecretsGroup,
        label="SolarWinds Credentials",
        description="SecretsGroup with SolarWinds NPM server (key), username, and password",
        default=_get_default_sw_credential(),
    )
    default_location = ObjectVar(
        model=Location,
        label="Default Location",
        description="Used when a node's SolarWinds Area does not match any Nautobot Location",
    )
    default_role = ObjectVar(
        model=Role,
        label="Default Role",
        description="Used when a node's SolarWinds Device_Type does not match any Nautobot Role",
    )
    only_up_nodes = BooleanVar(
        label="Only Up Nodes",
        description="Restrict to nodes currently reporting Status=Up in SolarWinds",
        default=True,
    )
    vendor_filter = StringVar(
        label="Vendor Filter",
        description=(
            "Optional comma-separated vendor substrings to limit scope, "
            "e.g. 'mikrotik,cisco'. Leave blank to process all mapped vendors."
        ),
        required=False,
        default="",
    )
    dry_run = BooleanVar(
        label="Dry Run",
        description=(
            "Log every planned create/update without writing anything to Nautobot. "
            "Always run this first."
        ),
        default=True,
    )
    full_sync = BooleanVar(
        label="Full CRUD Sync",
        description=(
            "Treat SolarWinds as the Source of Truth: any Nautobot device whose name "
            "does not appear in the current SolarWinds query will be deleted. "
            "In dry-run mode the count is only logged, nothing is deleted. "
            "Use with caution – ensure your vendor/node filters are broad enough."
        ),
        default=False,
    )
    max_workers = IntegerVar(
        label="Max Worker Threads",
        description=(
            "Number of parallel threads used to process SolarWinds nodes. "
            "Higher values speed up large imports but increase database connection usage. "
            "Recommended: 5–20."
        ),
        default=10,
        min_value=1,
        max_value=50,
    )

    def run(
        self,
        sw_credential,
        default_location,
        default_role,
        only_up_nodes,
        vendor_filter,
        dry_run,
        full_sync,
        max_workers,
    ):
        tag = "[DRY RUN] " if dry_run else ""

        # Pre-load shared DB objects once
        status_active = Status.objects.get(name="Active")
        global_ns = Namespace.objects.get(name="Global")

        # ------------------------------------------------------------------
        # Fetch nodes from SolarWinds (single SWQL query)
        # ------------------------------------------------------------------
        self.logger.info("Connecting to SolarWinds Orion...")
        swis = _swis_client(sw_credential)
        nodes = swis.query(_build_swql(only_up_nodes)).get("results", [])
        self.logger.info(
            f"Fetched {len(nodes)} SNMP nodes from SolarWinds"
            f"{' (Up only)' if only_up_nodes else ''}."
        )

        # Optional client-side vendor filter
        if vendor_filter:
            filters = [f.strip().lower() for f in vendor_filter.split(",") if f.strip()]
            before = len(nodes)
            nodes = [
                n for n in nodes
                if any(f in (n.get("Vendor") or "").lower() for f in filters)
            ]
            self.logger.info(
                f"Vendor filter {filters!r} applied: {before} → {len(nodes)} nodes."
            )

        # ------------------------------------------------------------------
        # Per-node processing
        # ------------------------------------------------------------------
        counts = {
            "no_ssh_vendor":  0,   # informational: non-SSH devices still onboarded
            "no_platform":    0,   # informational: devices onboarded without a platform
            "skipped_error":  0,
            "created":        0,
            "updated":        0,
            "deleted":        0,
            "would_delete":   0,
        }

        seen_captions: set[str] = set()
        for node in nodes:
            cap = (node.get("Caption") or "").strip()
            if cap:
                seen_captions.add(cap)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_node = {
                executor.submit(
                    self._process_node_safe,
                    node=node,
                    status_active=status_active,
                    global_ns=global_ns,
                    default_location=default_location,
                    default_role=default_role,
                    dry_run=dry_run,
                    tag=tag,
                ): node
                for node in nodes
            }

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                try:
                    result, pending_logs = future.result()
                    for level, msg in pending_logs:
                        getattr(self.logger, level)(msg)
                    for k, v in result.items():
                        counts[k] += v
                except Exception as exc:
                    counts["skipped_error"] += 1
                    self.logger.error(
                        f"[NodeID={node.get('NodeID')}] {node.get('Caption')} "
                        f"– unexpected error: {exc}"
                    )

        # ------------------------------------------------------------------
        # Full CRUD sync – remove Nautobot devices absent from SolarWinds
        # ------------------------------------------------------------------
        if full_sync:
            stale_qs = Device.objects.exclude(name__in=seen_captions)
            if dry_run:
                counts["would_delete"] = stale_qs.count()
                self.logger.info(
                    f"[DRY RUN] FULL SYNC – Would delete {counts['would_delete']} "
                    f"Nautobot device(s) not found in SolarWinds query results."
                )
            else:
                for dev in stale_qs:
                    self.logger.info(
                        f"FULL SYNC – Deleting device '{dev.name}' "
                        f"(absent from SolarWinds)"
                    )
                    dev.delete()
                    counts["deleted"] += 1

        # ------------------------------------------------------------------
        # Final summary
        # ------------------------------------------------------------------
        total = len(nodes)
        action_word = "Would create" if dry_run else "Created"
        update_word = "Would update" if dry_run else "Updated"
        summary = (
            f"\n{'=' * 60}\n"
            f"{'DRY RUN ' if dry_run else ''}SUMMARY  ({total} nodes evaluated)\n"
            f"{'=' * 60}\n"
            f"  Non-SSH / infrastructure devices:            {counts['no_ssh_vendor']}\n"
            f"  Onboarded without platform mapping:          {counts['no_platform']}\n"
            f"  Skipped – errors:                            {counts['skipped_error']}\n"
            f"  {action_word}:                               {counts['created']}\n"
            f"  {update_word}:                               {counts['updated']}\n"
        )
        if full_sync:
            delete_key = "would_delete" if dry_run else "deleted"
            delete_word = "[DRY RUN] Would delete" if dry_run else "Deleted (CRUD sync)"
            summary += f"  {delete_word}:                       {counts[delete_key]}\n"
        self.logger.info(summary)

    # ------------------------------------------------------------------
    # Per-node logic
    # ------------------------------------------------------------------

    def _process_node_safe(
        self,
        node: dict,
        status_active,
        global_ns,
        default_location,
        default_role,
        dry_run: bool,
        tag: str,
    ) -> tuple[dict, list]:
        """Thread wrapper: calls _process_node and closes the thread-local DB connection."""
        try:
            return self._process_node(
                node=node,
                status_active=status_active,
                global_ns=global_ns,
                default_location=default_location,
                default_role=default_role,
                dry_run=dry_run,
                tag=tag,
            )
        finally:
            db_connection.close()

    def _process_node(
        self,
        node: dict,
        status_active,
        global_ns,
        default_location,
        default_role,
        dry_run: bool,
        tag: str,
    ) -> tuple[dict, list]:
        local = {"no_ssh_vendor": 0, "no_platform": 0, "created": 0, "updated": 0}
        logs: list[tuple[str, str]] = []

        node_id   = node.get("NodeID")
        caption   = (node.get("Caption")    or "").strip()
        ip_addr   = (node.get("IPAddress")  or "").strip()
        vendor    = (node.get("Vendor")     or "").strip()
        machine   = (node.get("MachineType") or "").strip()
        desc      = (node.get("Description") or "").strip()
        community = (node.get("Community")  or "").strip()
        cp_area   = (node.get("cp_area")    or "").strip()
        cp_dtype  = (node.get("cp_device_type") or "").strip()

        if not caption or not ip_addr:
            return local, logs

        log_id = f"[NodeID={node_id}] {caption} ({ip_addr})"

        # 1. Note non-SSH / infrastructure vendors – still onboard them
        if vendor.lower() in SKIP_VENDORS:
            local["no_ssh_vendor"] += 1
            logs.append(("debug",
                f"{log_id} – Note: '{vendor}' is a non-SSH device; onboarding without platform"
            ))

        # 2. Map vendor → network_driver
        driver = _map_platform(vendor, machine, desc)
        if driver is None:
            local["no_platform"] += 1
            logs.append(("debug",
                f"{log_id} – vendor '{vendor}' not mapped to any platform; onboarding without platform"
            ))
            platform_obj = None
        else:
            # 3. Verify platform exists in Nautobot
            platform_obj = Platform.objects.filter(network_driver=driver).first()
            if platform_obj is None:
                local["no_platform"] += 1
                logs.append(("debug",
                    f"{log_id} – platform '{driver}' not found in Nautobot; onboarding without platform"
                ))

        # 4. Resolve Location (Area → Nautobot Location, else default)
        location_obj = (
            Location.objects.filter(name=cp_area).first() if cp_area else None
        ) or default_location

        # 5. Resolve Role (Device_Type → Nautobot Role, else default)
        role_obj = (
            Role.objects.filter(name=cp_dtype).first() if cp_dtype else None
        ) or default_role

        # 6. Derive Manufacturer name and DeviceType model
        mfr_name = _canonical_vendor(vendor)
        model    = _extract_model(machine)

        # ------------------------------------------------------------------
        # DRY RUN – log and count, no writes
        # ------------------------------------------------------------------
        if dry_run:
            platform_label = (
                f"{driver}  ({platform_obj.name})" if platform_obj else (driver or "None")
            )
            existing = Device.objects.filter(name=caption).first()
            if existing:
                local["updated"] += 1
                logs.append(("debug",
                    f"{tag}UPDATE device '{caption}'\n"
                    f"         IP:           {ip_addr}\n"
                    f"         Platform:     {platform_label}\n"
                    f"         Manufacturer: {mfr_name}\n"
                    f"         Model:        {model}\n"
                    f"         Location:     {location_obj}\n"
                    f"         Role:         {role_obj}\n"
                    f"         SNMP comm:    {'<set>' if community else '<empty>'}"
                ))
            else:
                local["created"] += 1
                logs.append(("debug",
                    f"{tag}CREATE device '{caption}'\n"
                    f"         IP:           {ip_addr}\n"
                    f"         Platform:     {platform_label}\n"
                    f"         Manufacturer: {mfr_name}\n"
                    f"         Model:        {model}\n"
                    f"         Location:     {location_obj}\n"
                    f"         Role:         {role_obj}\n"
                    f"         SNMP comm:    {'<set>' if community else '<empty>'}"
                ))
            return local, logs

        # ------------------------------------------------------------------
        # LIVE WRITES
        # ------------------------------------------------------------------

        # Manufacturer
        mfr_obj, mfr_created = Manufacturer.objects.get_or_create(name=mfr_name)
        if mfr_created:
            logs.append(("info", f"{log_id} Created Manufacturer: {mfr_name}"))

        # DeviceType
        dt_obj, dt_created = DeviceType.objects.get_or_create(
            manufacturer=mfr_obj,
            model=model,
        )
        if dt_created:
            logs.append(("info", f"{log_id} Created DeviceType: {mfr_name} / {model}"))

        # Device (update_or_create so repeated runs stay idempotent)
        device_obj, dev_created = Device.objects.update_or_create(
            name=caption,
            defaults={
                "device_type": dt_obj,
                "location":    location_obj,
                "status":      status_active,
                "role":        role_obj,
                "platform":    platform_obj,
            },
        )
        if dev_created:
            local["created"] += 1
            logs.append(("info", f"{log_id} Created device"))
        else:
            local["updated"] += 1
            logs.append(("info", f"{log_id} Updated device"))

        # Validate platform assignment immediately; fall back to None on manufacturer mismatch
        if platform_obj is not None:
            try:
                device_obj.validated_save()
            except ValidationError as ve:
                if "platform" in str(ve).lower():
                    logs.append(("warning",
                        f"{log_id} Platform '{driver}' rejected by Nautobot "
                        f"(manufacturer mismatch); onboarding without platform."
                    ))
                    device_obj.platform = None
                    device_obj.save()
                else:
                    raise

        # SNMP community custom field
        if community:
            device_obj.custom_field_data["snmp_community"] = community
            device_obj.validated_save()

        # Management interface (idempotent)
        iface_obj, _ = Interface.objects.get_or_create(
            name=MGMT_INTERFACE_NAME,
            device=device_obj,
            defaults={
                "status": status_active,
                "type":   InterfaceTypeChoices.TYPE_VIRTUAL,
            },
        )

        # Prefix (needed by Nautobot IPAM; /24 covering the management IP)
        prefix_str = ".".join(ip_addr.split(".")[:3]) + ".0/24"
        Prefix.objects.get_or_create(
            prefix=prefix_str,
            namespace=global_ns,
            defaults={"status": status_active},
        )

        # IP address (re-use if already exists for this host)
        ip_obj = IPAddress.objects.filter(host=ip_addr, status=status_active).first()
        if ip_obj is None:
            ip_obj = IPAddress.objects.create(
                host=ip_addr,
                type="host",
                mask_length=32,
                status=status_active,
            )
            logs.append(("info", f"{log_id} Created IP {ip_addr}/32"))

        ip_obj.interfaces.set([iface_obj])
        ip_obj.validated_save()

        device_obj.primary_ip4 = ip_obj
        device_obj.validated_save()
        logs.append(("info", f"{log_id} Done – primary IP {ip_addr}"))
        return local, logs


register_jobs(OnboardFromSolarWinds)
