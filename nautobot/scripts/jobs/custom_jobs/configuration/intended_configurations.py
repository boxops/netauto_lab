"""Purpose: Generate intended device configurations with Nautobot."""

from datetime import datetime
from django.conf import settings
import ipaddress
import os

from nautobot.apps.jobs import register_jobs, Job, IntegerVar, BooleanVar
from nautobot_golden_config.models import GoldenConfig
from nautobot.extras.models.groups import DynamicGroup
from nautobot.core.utils.data import render_jinja2
from nautobot_golden_config.utilities.graphql import graph_ql_query

from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.modules.tools import parallel_execution
from custom_jobs.modules.tools import JobLogBuffer
from custom_jobs.modules.tools import JobProxy

name = "Configuration"

# Fallback directory for rendered intended configs when no Git repo is configured.
DEFAULT_INTENDED_ROOT = getattr(settings, "INTENDED_ROOT", "/opt/nautobot/intended")

# Fallback directory for Jinja templates (scripts volume is mounted here).
DEFAULT_JINJA_ROOT = getattr(settings, "JINJA_ROOT", "/opt/nautobot/scripts/jinja_templates")

SUPPORTED_PLATFORMS = [
    "keymile_nos",
    "fiberstore_fsos",
    "mikrotik_routeros",
    # "netonix_os",
    "cisco_ios",
    "cisco_xr",
    # "cisco_xe",
    # "cisco_nxos",
    # "cisco_s300",
    # "ubiquiti_airos",
    # "siklu_os",
    "arista_eos",
]


class CustomDeviceIntended(Job, DeviceFormEntry):
    """Job to generate intended device configurations with Nautobot."""

    parallel_task = BooleanVar(
        description="Execute intended tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of workers to use for parallel execution",
        default=20,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Generate Intended Device Configurations"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400  # 40 minutes
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    # @gc_repos  # Uncomment to re-enable Git repository sync once repos are configured.
    def run(
        self,
        tenant_group=None,
        tenant=None,
        location=None,
        rack_group=None,
        rack=None,
        role=None,
        manufacturer=None,
        platform=None,
        device_type=None,
        device=None,
        tags=None,
        status=None,
        parallel_task=True,
        max_workers=None,
    ):
        all_devices = set()

        all_devices = apply_device_filters(
            all_devices,
            tenant_group=tenant_group,
            tenant=tenant,
            location=location,
            rack_group=rack_group,
            rack=rack,
            role=role,
            manufacturer=manufacturer,
            platform=platform,
            device_type=device_type,
            tags=tags,
            status=status,
        )
        if device:
            all_devices.update(device)

        def intended_config(dev):
            buf = JobLogBuffer()
            try:
                if dev.platform.network_driver not in SUPPORTED_PLATFORMS:
                    buf.info(
                        f"{dev} Platform {dev.platform.network_driver} is not supported. Skipping..."
                    )
                    return buf
                buf.info(f"{dev} Processing device...")
                task = DeviceIntent(job=JobProxy(buf), device=dev)
                task.generate_config()
            except Exception as e:
                buf.error(f"{dev} Error processing device: {e}")
            return buf

        if parallel_task:
            parallel_execution(intended_config, all_devices, max_workers=max_workers, job_logger=self.logger)
        else:
            for dev in all_devices:
                intended_config(dev).drain_to(self.logger)


class DeviceIntent:
    def __init__(self, job, device):
        self.job = job
        self.device = device

    def _resolve_intended_path(self, setting):
        """Return the full path for the intended config file.

        Uses the Golden Config setting's intended repository when configured,
        otherwise falls back to DEFAULT_INTENDED_ROOT/<hostname>.txt.
        """
        if setting is not None and setting.intended_repository is not None and setting.intended_path_template:
            directory = setting.intended_repository.filesystem_path
            relative_path = render_jinja2(
                template_code=setting.intended_path_template,
                context={"obj": self.device},
            )
            return os.path.join(directory, relative_path)

        self.job.logger.warning(
            f"{self.device} No Golden Config intended repository configured. "
            f"Writing to fallback directory: {DEFAULT_INTENDED_ROOT}"
        )
        return os.path.join(DEFAULT_INTENDED_ROOT, f"{self.device.name}.txt")

    def _resolve_jinja_path(self, setting):
        """Return the path to the Jinja template for this device.

        Uses the Golden Config setting's jinja repository when configured,
        otherwise falls back to DEFAULT_JINJA_ROOT/<platform>.j2.
        """
        if setting is not None and setting.jinja_repository is not None and setting.jinja_path_template:
            directory = setting.jinja_repository.filesystem_path
            relative_path = render_jinja2(
                template_code=setting.jinja_path_template,
                context={"obj": self.device},
            )
            return os.path.join(directory, relative_path)

        driver = (self.device.platform.network_driver if self.device.platform else "unknown")
        fallback = os.path.join(DEFAULT_JINJA_ROOT, f"{driver}.j2")
        self.job.logger.warning(
            f"{self.device} No Golden Config jinja repository configured. "
            f"Using local template: {fallback}"
        )
        return fallback

    def _build_device_context(self, setting):
        """Build the Jinja2 rendering context.

        Uses the SOT aggregation GraphQL query when configured,
        otherwise returns a minimal context built from the ORM.
        """
        if setting is not None and setting.sot_agg_query is not None:
            self.job.request.user = self.job.user
            _, device_data = graph_ql_query(
                self.job.request, self.device, setting.sot_agg_query.query
            )
            return device_data

        return self._build_enriched_context()

    def _build_enriched_context(self):
        """Build a rich Jinja2 context from Nautobot ORM — no Git repo required.

        Shared infrastructure config (NTP, SNMP, logging, BGP max-paths) is sourced
        from Nautobot config contexts so it can be managed centrally per platform/role.
        Per-device unique values (BGP ASN, router-ID, neighbors) are derived from the ORM.
        """
        from nautobot.ipam.models import VLAN

        device = self.device
        # Merged config context (platform + role + location hierarchy)
        cc = device.get_config_context()

        context = {"obj": device, "cc": cc}

        # Role (lower-cased name, e.g. 'leaf' or 'spine')
        context["role"] = device.role.name.lower() if device.role else ""

        # ── VLANs ────────────────────────────────────────────────────────────
        # Derive from Vlan-prefixed interfaces present on the device.
        vlan_vids = []
        for iface in device.interfaces.filter(name__startswith="Vlan"):
            try:
                vlan_vids.append(int(iface.name[4:]))
            except ValueError:
                pass
        device_vlans = []
        for v in VLAN.objects.filter(vid__in=vlan_vids).order_by("vid"):
            device_vlans.append({"vid": v.vid, "name": v.name})
        context["device_vlans"] = device_vlans

        # ── BGP ──────────────────────────────────────────────────────────────
        # BGP ASN is per-device — stored in the device's local config context
        # under bgp.asn (set by migrate_bgp_to_config_context.py).  The merged
        # config context (cc) already includes local_config_context_data so a
        # single lookup is sufficient.
        bgp_asn = cc.get("bgp", {}).get("asn")
        context["bgp_asn"] = bgp_asn

        # Router-ID from Loopback0
        lo_iface = device.interfaces.filter(name="Loopback0").first()
        lo_ip_obj = lo_iface.ip_addresses.first() if lo_iface else None
        lo_ip = str(lo_ip_obj.address).split("/")[0] if lo_ip_obj else None
        context["bgp_router_id"] = lo_ip

        # max-paths from config context (set per role: leaf=2, spine=4)
        context["bgp_max_paths"] = cc.get("bgp", {}).get("max_paths", 2)

        # Neighbors: derive from /31 Ethernet IPs by finding the peer in the IP table
        bgp_neighbors = []
        for eth in device.interfaces.filter(name__startswith="Ethernet"):
            for ip_obj in eth.ip_addresses.all():
                try:
                    net = ipaddress.ip_interface(str(ip_obj.address)).network
                except ValueError:
                    continue
                if net.prefixlen != 31:
                    continue
                my_ip = ipaddress.ip_interface(str(ip_obj.address)).ip
                hosts = list(net.hosts())
                peer_ip = str(hosts[0] if my_ip == hosts[1] else hosts[1])
                # Resolve peer's ASN via host/mask_length lookup on IPAddress
                peer_asn = None
                try:
                    from nautobot.ipam.models import IPAddress as NBIPAddress
                    peer_ip_obj = NBIPAddress.objects.filter(
                        host=peer_ip, mask_length=net.prefixlen
                    ).first()
                    if peer_ip_obj:
                        peer_iface = peer_ip_obj.interfaces.first()
                        if peer_iface:
                            peer_asn = peer_iface.device.get_config_context().get("bgp", {}).get("asn")
                except Exception:
                    pass
                bgp_neighbors.append({"ip": peer_ip, "remote_as": peer_asn})
        # Sort by neighbor IP for deterministic output
        bgp_neighbors.sort(key=lambda n: ipaddress.ip_address(n["ip"]))
        context["bgp_neighbors"] = bgp_neighbors

        # BGP networks to advertise
        bgp_networks = []
        for iface in device.interfaces.filter(name__startswith="Vlan"):
            for ip_obj in iface.ip_addresses.all():
                try:
                    net = ipaddress.ip_interface(str(ip_obj.address)).network
                    bgp_networks.append(str(net))
                except ValueError:
                    pass
        # Spines: no SVIs — advertise the loopback /24 summary
        if not bgp_networks and lo_ip:
            try:
                lo_net = ipaddress.ip_network(lo_ip)
                bgp_networks.append(str(lo_net.supernet(new_prefix=24)))
            except Exception:
                pass
        bgp_networks.sort()
        context["bgp_networks"] = bgp_networks

        return context

    def generate_config(self):
        """Generate intended configuration for the device."""
        intended_obj, _ = GoldenConfig.objects.get_or_create(device=self.device)
        intended_obj.intended_last_attempt_date = datetime.now()
        intended_obj.save()

        groups = DynamicGroup.objects.exclude(golden_config_setting__isnull=True)
        setting = groups[0].golden_config_setting if groups.exists() else None

        try:
            intended_file = self._resolve_intended_path(setting)
        except Exception as e:
            self.job.logger.error(f"{self.device} Could not resolve intended path: {e}")
            return

        try:
            jinja_file = self._resolve_jinja_path(setting)
        except Exception as e:
            self.job.logger.error(f"{self.device} Could not resolve jinja path: {e}")
            return

        os.makedirs(os.path.dirname(intended_file), exist_ok=True)
        self.job.logger.info(f"{self.device} Intent file: {intended_file}")

        device_data = self._build_device_context(setting)

        try:
            with open(jinja_file) as f:
                template_code = f.read()
            rendered_config = render_jinja2(
                template_code=template_code,
                context=device_data,
            )
        except FileNotFoundError:
            self.job.logger.error(
                f"{self.device} Jinja template not found: {jinja_file}. "
                f"Create the template at {DEFAULT_JINJA_ROOT}/<platform>.j2"
            )
            return
        except Exception as e:
            self.job.logger.error(f"{self.device} Failed to render Jinja template: {e}")
            return

        with open(intended_file, "w") as f:
            f.write(rendered_config)

        intended_obj.intended_last_success_date = datetime.now()
        intended_obj.intended_config = rendered_config
        intended_obj.save()

        self.job.logger.info(
            f"{self.device} Successfully generated intended configuration → {intended_file}"
        )


register_jobs(CustomDeviceIntended)
