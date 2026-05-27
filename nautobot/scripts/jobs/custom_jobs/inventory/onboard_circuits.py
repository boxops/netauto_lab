"""Purpose: Discover circuits from interface descriptions and sync to Nautobot.

Interface descriptions encode circuit info in the format:
  <device>_<interface>_<provider>_<circuit-id>

Example:
  AB-MFD-OF011-1-SW01_eth-0-24_Openreach_ONEA70104313

For each matched interface the job will:
  - get_or_create Provider
  - get_or_create CircuitType (default "Transit")
  - update_or_create Circuit keyed on (cid, provider)
  - update_or_create CircuitTermination A-side with device location
  - update_or_create CircuitTermination Z-side with a ProviderNetwork
  - Cable the A-side CircuitTermination to the device Interface
    (skipped if the interface already has a cable)
"""

import re

from django.conf import settings

from nautobot.circuits.models import Circuit, CircuitTermination, CircuitType, Provider, ProviderNetwork
from nautobot.dcim.models import Cable, Interface
from nautobot.extras.models import Status
from nautobot.apps.jobs import Job, register_jobs, BooleanVar, IntegerVar, StringVar

from custom_jobs.framework import FrameworkJobMixin
from custom_jobs.modules.tools import (
    apply_device_filters,
    DeviceFormEntry,
    JobLogBuffer,
    JobProxy,
    parallel_execution,
)

name = "Inventory"

# Regex: device_interface_provider_cid
# Provider and CID may themselves contain underscores, so we split on _ with a
# maximum of 3 splits to keep device and interface as the first two tokens.
# Minimum 4 tokens required; any additional underscores are joined back into the
# provider name (tokens 2..-2) and cid is always the last token.
_DESC_MIN_PARTS = 4


def parse_circuit_description(description: str) -> tuple[str, str] | None:
    """Return (provider, cid) if description matches the circuit pattern, else None.

    Pattern: <device>_<interface>_<provider>_<cid>
    The device and interface tokens are ignored (self-referential on the interface
    where the description lives).  Provider may contain underscores.
    CID is always the final token and must be non-empty.
    """
    parts = description.split("_")
    if len(parts) < _DESC_MIN_PARTS:
        return None
    # parts[0] = device, parts[1] = interface (may also be multi-part like eth-0-24)
    # parts[-1] = cid, parts[2:-1] = provider (join back)
    cid = parts[-1].strip()
    provider = "_".join(parts[2:-1]).strip()
    if not cid or not provider:
        return None
    return provider, cid


class CircuitDiscovery:
    """Scan Nautobot interface descriptions for circuit info and sync Circuit objects.

    Reads descriptions from *already-saved* Nautobot Interface records — no SSH
    required.  Call after CaptureDeviceData has run so descriptions are current.
    """

    DEFAULT_CIRCUIT_TYPE = "Transit"

    def __init__(self, job, device, dry_run: bool = False):
        self.job = job
        self.device = device
        self.dry_run = dry_run

    def run(self):
        interfaces = Interface.objects.filter(device=self.device).exclude(description="")
        matched = 0
        for iface in interfaces:
            parsed = parse_circuit_description(iface.description)
            if parsed is None:
                continue
            provider_name, cid = parsed
            self.job.logger.info(
                f"{self.device} Found circuit in description of {iface.name}: "
                f"provider='{provider_name}' cid='{cid}'"
            )
            matched += 1
            if not self.dry_run:
                try:
                    self._sync_circuit(iface, provider_name, cid)
                except Exception as exc:
                    self.job.logger.error(
                        f"{self.device} Failed to sync circuit '{cid}' on {iface.name}: {exc}"
                    )

        if matched == 0:
            self.job.logger.info(f"{self.device} No circuit descriptions found.")
        else:
            self.job.logger.info(
                f"{self.device} {'Would process' if self.dry_run else 'Processed'} "
                f"{matched} circuit description(s)."
            )
        return {"matched_descriptions": matched}

    def _sync_circuit(self, iface: Interface, provider_name: str, cid: str):
        status_active = Status.objects.get(name="Active")

        # ── Provider ──────────────────────────────────────────────────────
        provider, prov_created = Provider.objects.get_or_create(name=provider_name)
        if prov_created:
            self.job.logger.info(f"{self.device} Created provider '{provider_name}'.")

        # ── CircuitType ───────────────────────────────────────────────────
        circuit_type, _ = CircuitType.objects.get_or_create(name=self.DEFAULT_CIRCUIT_TYPE)

        # ── Circuit ───────────────────────────────────────────────────────
        circuit, circ_created = Circuit.objects.update_or_create(
            cid=cid,
            provider=provider,
            defaults={
                "circuit_type": circuit_type,
                "status": status_active,
                "description": iface.description,
            },
        )
        action = "Created" if circ_created else "Updated"
        self.job.logger.info(f"{self.device} {action} circuit '{cid}' (provider: {provider_name}).")

        # ── A-side termination (device side) ─────────────────────────────
        term_a, ta_created = CircuitTermination.objects.update_or_create(
            circuit=circuit,
            term_side="A",
            defaults={"location": self.device.location},
        )
        if ta_created:
            self.job.logger.info(f"{self.device} Created A-side termination for '{cid}'.")

        # ── Z-side termination (provider network) ─────────────────────────
        pn_name = f"{provider_name} Network"
        provider_network, pn_created = ProviderNetwork.objects.get_or_create(
            name=pn_name,
            provider=provider,
        )
        if pn_created:
            self.job.logger.info(f"{self.device} Created provider network '{pn_name}'.")

        term_z, tz_created = CircuitTermination.objects.update_or_create(
            circuit=circuit,
            term_side="Z",
            defaults={"provider_network": provider_network},
        )
        if tz_created:
            self.job.logger.info(f"{self.device} Created Z-side termination for '{cid}'.")

        # ── Cable: interface → A-side termination ─────────────────────────
        # Skip if the interface already has any cable (LLDP may have wired it).
        iface_already_cabled = (
            Cable.objects.filter(termination_a_id=iface.pk).exists()
            or Cable.objects.filter(termination_b_id=iface.pk).exists()
        )
        if iface_already_cabled:
            self.job.logger.info(
                f"{self.device} Interface {iface.name} already cabled — skipping circuit cable."
            )
            return

        # Skip if the termination already has a cable.
        if term_a.cable is not None:
            self.job.logger.info(
                f"{self.device} A-side termination for '{cid}' already cabled — skipping."
            )
            return

        cable = Cable(
            termination_a=iface,
            termination_b=term_a,
            status=Status.objects.get_for_model(Cable).get(name="Connected"),
        )
        cable.validated_save()
        self.job.logger.info(
            f"{self.device} Cabled {iface.name} → circuit '{cid}' A-side termination."
        )


class OnboardCircuits(FrameworkJobMixin, Job, DeviceFormEntry):
    """Discover and sync circuits from interface descriptions into Nautobot.

    Scans interface descriptions for the pattern:
      <device>_<interface>_<provider>_<circuit-id>

    For each match, creates/updates: Provider, CircuitType, Circuit,
    CircuitTermination (A + Z sides), ProviderNetwork, and a Cable linking
    the interface to the A-side termination.
    """

    dry_run = BooleanVar(
        description="Preview discovered circuits without writing to Nautobot",
        default=False,
        required=False,
    )
    circuit_type = StringVar(
        description="CircuitType name to assign to discovered circuits",
        default="Transit",
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of parallel workers (when parallel is enabled)",
        default=10,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Onboard Circuits from Interface Descriptions"
        description = (
            "Scan interface descriptions for circuit info "
            "(<device>_<interface>_<provider>_<cid>) and create matching "
            "Provider, Circuit, CircuitTermination, ProviderNetwork, and Cable objects."
        )
        has_sensitive_variables = False
        hidden = True
        soft_time_limit = 900
        time_limit = 1200
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(self, **kwargs):
        dry_run = kwargs.pop("dry_run", False)
        circuit_type = kwargs.pop("circuit_type", "Transit")
        parallel_task = kwargs.pop("parallel_task", False)
        max_workers = kwargs.pop("max_workers", 10)

        self.begin_framework_run(
            inputs={
                "dry_run": dry_run,
                "circuit_type": circuit_type,
                "parallel_task": parallel_task,
                "max_workers": max_workers,
            }
        )
        all_devices = apply_device_filters(set(), **kwargs)

        if not all_devices:
            self.logger.warning("No devices matched the selected filters.")
            self.record_skipped(
                target="all-devices",
                message="No devices matched selected filters",
                details={},
            )
            self.finalize_framework_run(filename_prefix="onboard_circuits_report")
            return

        if dry_run:
            self.logger.info("Dry-run mode — no changes will be written to Nautobot.")

        def process_device(dev):
            buf = JobLogBuffer()
            proxy = JobProxy(buf)
            discovery = CircuitDiscovery(proxy, dev, dry_run=dry_run)
            discovery.DEFAULT_CIRCUIT_TYPE = circuit_type or CircuitDiscovery.DEFAULT_CIRCUIT_TYPE
            try:
                summary = discovery.run()
                matched = summary.get("matched_descriptions", 0)
                if matched > 0:
                    self.record_success(
                        target=str(dev),
                        message=f"{matched} circuit description(s) discovered",
                        details={
                            "matched_descriptions": matched,
                            "dry_run": dry_run,
                            "circuit_type": circuit_type or CircuitDiscovery.DEFAULT_CIRCUIT_TYPE,
                        },
                    )
                else:
                    self.record_skipped(
                        target=str(dev),
                        message="No matching circuit descriptions found",
                        details={"matched_descriptions": 0},
                    )
            except Exception as exc:
                buf.error(f"{dev} Unexpected error during circuit discovery: {exc}")
                self.record_failure(
                    target=str(dev),
                    message=f"Unexpected error during circuit discovery: {exc}",
                )
            return buf

        if parallel_task:
            parallel_execution(process_device, all_devices, max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                process_device(dev).drain_to(self.logger)

        self.finalize_framework_run(filename_prefix="onboard_circuits_report")


register_jobs(OnboardCircuits)
