"""
Generate documentation for a site based on the device settings that are part of that site.

Produces a Markdown report covering:
  1. Site Overview       – location metadata, tenant, status, contact
  2. Device Inventory    – all devices with type, serial, status, primary IP, rack position,
                           platform, and software version
  3. Device Interfaces   – per-device interface list with type, mode, IPs, MTU, and description
  4. Connectivity        – cables between site devices, VLANs, IP prefixes
  5. Rack Layouts        – per-rack device placement (U position)
  6. Additional Notes    – custom site description / comments
"""

from datetime import date

from nautobot.apps.jobs import Job, ObjectVar, BooleanVar, register_jobs
from nautobot.dcim.models import (
    Cable,
    Device,
    Location,
    Rack,
)
from nautobot.ipam.models import VLAN, Prefix

name = "Documentation"

# ── helpers ───────────────────────────────────────────────────────────────────


def _md_table(headers: list, rows: list) -> str:
    """Return a Markdown table string."""
    sep = " | "
    header_row = sep.join(str(h) for h in headers)
    divider = sep.join(["---"] * len(headers))
    body_rows = "\n".join(
        "| " + sep.join(str(c) for c in row) + " |" for row in rows
    )
    return f"| {header_row} |\n| {divider} |\n{body_rows}"


def _rack_position(device) -> str:
    if device.rack:
        pos = f"Rack {device.rack.name}"
        if device.position is not None:
            u = int(device.position)
            pos += f", U{u}"
            if device.device_type and device.device_type.u_height:
                top = u + int(device.device_type.u_height) - 1
                if top != u:
                    pos += f"-U{top}"
        return pos
    return "—"


def _primary_ip(device) -> str:
    if device.primary_ip:
        return str(device.primary_ip.address)
    return "—"


def _software_version(device) -> str:
    try:
        sv = device.software_version
        if sv:
            return str(sv)
    except AttributeError:
        pass
    return "—"


# ── Job ───────────────────────────────────────────────────────────────────────


class GenerateSiteDocs(Job):
    """Generate a Markdown site documentation report for a selected location."""

    site = ObjectVar(
        model=Location,
        description="Site (Location) to generate documentation for",
        required=True,
    )
    include_child_locations = BooleanVar(
        description="Include devices from child locations (sub-sites)",
        default=True,
        required=False,
    )

    class Meta:
        name = "Generate Site Documentation"
        description = (
            "Produce a Markdown report for a selected site covering device inventory "
            "(including platform and software version), device interfaces, cabling, "
            "rack layouts, VLANs, and IP prefixes."
        )
        has_sensitive_variables = False

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self, site, include_child_locations=True):
        self.logger.info(
            f"Generating documentation for site: {site.name}",
            extra={"object": site},
        )

        devices = self._get_devices(site, include_child_locations)
        racks = Rack.objects.filter(location=site)
        cables = self._get_cables(devices)
        vlans = VLAN.objects.filter(location=site)
        prefixes = Prefix.objects.filter(location=site)

        sections = [
            self._section_overview(site),
            self._section_device_inventory(devices),
            self._section_device_interfaces(devices),
            self._section_connectivity(cables, vlans, prefixes),
            self._section_rack_layouts(racks, devices),
            self._section_notes(site),
        ]

        report = "\n\n---\n\n".join(sections)
        filename = f"site_docs_{site.name.replace(' ', '_')}_{date.today()}.md"

        self.logger.info(f"Report complete. Attaching as: {filename}")
        self.create_file(filename=filename, content=report)

    # ── Queries ───────────────────────────────────────────────────────────────

    def _get_devices(self, site, include_children: bool):
        select = (
            "device_type__manufacturer",
            "role",
            "platform",
            "rack",
            "primary_ip4",
            "primary_ip6",
            "status",
            "software_version",
        )
        if include_children:
            location_ids = [site.pk] + list(
                site.descendants().values_list("pk", flat=True)
            )
            return Device.objects.filter(
                location__pk__in=location_ids
            ).select_related(*select)
        return Device.objects.filter(location=site).select_related(*select)

    def _get_cables(self, devices):
        """Return cables where at least one end terminates on a site device."""
        seen = set()
        cables = []
        for device in devices:
            for iface in device.interfaces.select_related("cable"):
                cable = iface.cable
                if cable is None or cable.pk in seen:
                    continue
                seen.add(cable.pk)
                cables.append(cable)
        return cables

    # ── Section builders ──────────────────────────────────────────────────────

    def _section_overview(self, site) -> str:
        lines = [f"# Site Documentation – {site.name}", "", "## 1. Site Overview", ""]

        fields = [
            ("Location Type", getattr(site.location_type, "name", "—")),
            ("Status", getattr(site.status, "name", "—") if site.status else "—"),
            ("Tenant", site.tenant.name if site.tenant else "—"),
            ("Parent Location", site.parent.name if site.parent else "—"),
            ("Date of Report", str(date.today())),
        ]
        for attr, label in [
            ("physical_address", "Physical Address"),
            ("latitude", "Latitude"),
            ("longitude", "Longitude"),
            ("contact_name", "Site Contact"),
            ("contact_email", "Contact Email"),
            ("contact_phone", "Contact Phone"),
        ]:
            val = getattr(site, attr, None)
            if val:
                fields.append((label, str(val)))

        for key, val in fields:
            lines.append(f"- **{key}:** {val}")

        if site.description:
            lines += ["", f"> {site.description}"]

        return "\n".join(lines)

    def _section_device_inventory(self, devices) -> str:
        lines = ["## 2. Device Inventory", ""]
        device_list = list(devices)
        if not device_list:
            lines.append("_No devices found at this site._")
            return "\n".join(lines)

        headers = [
            "Device Name", "Role", "Manufacturer", "Model",
            "Serial Number", "Status", "Primary IP", "Rack / Position",
            "Platform", "Software Version",
        ]
        rows = [
            [
                d.name,
                d.role.name if d.role else "—",
                d.device_type.manufacturer.name if d.device_type else "—",
                d.device_type.model if d.device_type else "—",
                d.serial or "—",
                d.status.name if d.status else "—",
                _primary_ip(d),
                _rack_position(d),
                d.platform.name if d.platform else "—",
                _software_version(d),
            ]
            for d in sorted(device_list, key=lambda x: x.name)
        ]
        lines.append(_md_table(headers, rows))
        lines.append(f"\n_Total: {len(rows)} device(s)._")
        return "\n".join(lines)

    def _section_device_interfaces(self, devices) -> str:
        lines = ["## 3. Device Interfaces", ""]
        device_list = sorted(devices, key=lambda d: d.name)
        if not device_list:
            lines.append("_No devices found at this site._")
            return "\n".join(lines)

        headers = [
            "Device", "Interface", "Type", "Enabled", "Mode",
            "IP Addresses", "MAC Address", "MTU", "Description",
        ]
        rows = []
        for device in device_list:
            ifaces = device.interfaces.prefetch_related(
                "ip_addresses", "tagged_vlans", "untagged_vlan"
            ).select_related("lag").order_by("name")
            for iface in ifaces:
                ip_list = ", ".join(
                    str(ip.address) for ip in iface.ip_addresses.all()
                ) or "—"
                rows.append([
                    device.name,
                    iface.name,
                    iface.get_type_display() if hasattr(iface, "get_type_display") else (iface.type or "—"),
                    "Yes" if iface.enabled else "No",
                    iface.get_mode_display() if hasattr(iface, "get_mode_display") and iface.mode else "—",
                    ip_list,
                    str(iface.mac_address) if iface.mac_address else "—",
                    str(iface.mtu) if iface.mtu else "—",
                    iface.description or "—",
                ])

        if rows:
            lines.append(_md_table(headers, rows))
            lines.append(f"\n_Total: {len(rows)} interface(s) across {len(device_list)} device(s)._")
        else:
            lines.append("_No interfaces found for devices at this site._")
        return "\n".join(lines)

    def _section_connectivity(self, cables, vlans, prefixes) -> str:
        lines = ["## 4. Connectivity & Network Topology", ""]

        # Physical Cabling
        lines += ["### 4.1 Physical Cabling", ""]
        if cables:
            headers = [
                "Cable ID", "Side A Device", "Side A Interface",
                "Side B Device", "Side B Interface", "Cable Type",
            ]
            rows = []
            for cable in cables:
                a_dev = a_iface = b_dev = b_iface = "—"
                if cable.termination_a:
                    a_iface = getattr(cable.termination_a, "name", "—")
                    dev = getattr(cable.termination_a, "device", None)
                    a_dev = dev.name if dev else "—"
                if cable.termination_b:
                    b_iface = getattr(cable.termination_b, "name", "—")
                    dev = getattr(cable.termination_b, "device", None)
                    b_dev = dev.name if dev else "—"
                rows.append([
                    str(cable.pk)[:8],
                    a_dev, a_iface,
                    b_dev, b_iface,
                    cable.type or "—",
                ])
            lines.append(_md_table(headers, rows))
            lines.append(f"\n_Total: {len(rows)} cable(s)._")
        else:
            lines.append("_No cables recorded for this site._")

        # VLANs
        lines += ["", "### 4.2 VLANs", ""]
        vlans_list = list(vlans.order_by("vid"))
        if vlans_list:
            headers = ["VLAN ID", "Name", "Status"]
            rows = [
                [
                    v.vid,
                    v.name,
                    v.status.name if v.status else "—",
                ]
                for v in vlans_list
            ]
            lines.append(_md_table(headers, rows))
        else:
            lines.append("_No VLANs assigned to this site._")

        # IP Prefixes
        lines += ["", "### 4.3 IP Prefixes", ""]
        prefix_list = list(prefixes.order_by("network", "prefix_length"))
        if prefix_list:
            headers = ["Prefix", "Status", "Role", "Description"]
            rows = [
                [
                    str(p.prefix),
                    p.status.name if p.status else "—",
                    p.role.name if p.role else "—",
                    p.description or "—",
                ]
                for p in prefix_list
            ]
            lines.append(_md_table(headers, rows))
        else:
            lines.append("_No IP prefixes assigned to this site._")

        return "\n".join(lines)

    def _section_rack_layouts(self, racks, devices) -> str:
        lines = ["## 5. Rack Layouts", ""]
        rack_list = list(racks.order_by("name"))
        if not rack_list:
            lines.append("_No racks defined at this site._")
            return "\n".join(lines)

        # Index devices by rack pk
        rack_devices: dict = {}
        for d in devices:
            if d.rack:
                rack_devices.setdefault(d.rack.pk, []).append(d)

        for rack in rack_list:
            lines.append(f"### Rack: {rack.name}")
            lines.append("")
            if rack.u_height:
                lines.append(f"- **Height:** {rack.u_height}U")
            if rack.facility_id:
                lines.append(f"- **Facility ID:** {rack.facility_id}")
            if rack.tenant:
                lines.append(f"- **Tenant:** {rack.tenant.name}")
            lines.append("")

            rack_devs = rack_devices.get(rack.pk, [])
            if rack_devs:
                headers = ["U Position", "Device Name", "Model", "Role", "Status"]
                rows = [
                    [
                        d.position if d.position is not None else "—",
                        d.name,
                        d.device_type.model if d.device_type else "—",
                        d.role.name if d.role else "—",
                        d.status.name if d.status else "—",
                    ]
                    for d in rack_devs
                ]
                rows.sort(key=lambda r: (r[0] == "—", r[0] if r[0] != "—" else 0))
                lines.append(_md_table(headers, rows))
            else:
                lines.append("_No devices mounted in this rack._")
            lines.append("")

        return "\n".join(lines)

    def _section_notes(self, site) -> str:
        lines = ["## 6. Additional Notes", ""]
        if site.description:
            lines.append(f"**Site Description:** {site.description}")
        else:
            lines.append("_No additional notes._")
        return "\n".join(lines)


register_jobs(GenerateSiteDocs)
