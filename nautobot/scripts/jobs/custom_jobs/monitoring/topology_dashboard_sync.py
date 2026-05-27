"""Build topology dashboard artifacts from Nautobot inventory and cabling."""

from __future__ import annotations

import json
from pathlib import Path

from nautobot.dcim.models import Cable, Device


DEFAULT_TOPOLOGY_OUTPUT_DIR = "/opt/nautobot/monitoring/topology_dashboard"


def generate_topology_artifacts(output_dir: Path, dry_run: bool = False):
    """Generate a minimal topology payload and optionally write it to disk."""

    output_dir.mkdir(parents=True, exist_ok=True)

    nodes = []
    for device in Device.objects.select_related("role", "platform", "location", "device_type"):
        nodes.append(
            {
                "name": device.name,
                "primary_ip": str(getattr(getattr(device, "primary_ip4", None), "address", None)) if getattr(device, "primary_ip4", None) else None,
                "role": device.role.name if device.role else None,
                "platform": device.platform.network_driver if device.platform else None,
                "location": device.location.name if device.location else None,
                "device_type": device.device_type.model if device.device_type else None,
            }
        )

    edges = []
    for cable in Cable.objects.all():
        term_a = getattr(cable, "termination_a", None)
        term_b = getattr(cable, "termination_b", None)
        if not term_a or not term_b:
            continue
        edges.append(
            {
                "a_device": getattr(getattr(term_a, "device", None), "name", None),
                "a_interface": getattr(term_a, "name", None),
                "b_device": getattr(getattr(term_b, "device", None), "name", None),
                "b_interface": getattr(term_b, "name", None),
            }
        )

    locations = sorted({device["location"] for device in nodes if device["location"]})
    payload = {"all": {"nodes": nodes, "edges": edges, "locations": locations}}

    files = []
    if not dry_run:
        out_file = output_dir / "topology-data.json"
        out_file.write_text(json.dumps(payload["all"], indent=2, sort_keys=True), encoding="utf-8")
        files.append(str(out_file))

    return payload, files
