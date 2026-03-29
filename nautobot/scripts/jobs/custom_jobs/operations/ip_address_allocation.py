"""Purpose: Reserve the next available IP from a Nautobot prefix and assign it to a device interface."""

from nautobot.apps.jobs import Job, register_jobs, ObjectVar, StringVar, BooleanVar
from nautobot.ipam.models import IPAddress, Prefix, Namespace
from nautobot.dcim.models import Interface
from nautobot.extras.models import Status
import ipaddress

name = "Operations"


class IPAddressAllocation(Job):
    """
    Allocate the next available IP address from a selected Nautobot prefix,
    create the IPAddress record with Active status, and optionally assign it
    to a device interface already tracked in Nautobot.
    """

    prefix = ObjectVar(
        model=Prefix,
        description="Prefix to allocate from",
        required=True,
    )
    interface = ObjectVar(
        model=Interface,
        description="Device interface to assign the IP to (optional)",
        required=False,
    )
    dns_name = StringVar(
        description="DNS name / FQDN for the new IP address (optional)",
        required=False,
    )
    description = StringVar(
        description="Description for the IP address record",
        required=False,
    )
    dry_run = BooleanVar(
        description="Preview the allocation without creating records",
        default=True,
        required=False,
    )

    class Meta:
        name = "IP Address Allocation"
        description = (
            "Find the next available IP in a Nautobot prefix, create an IPAddress record, "
            "and optionally assign it to a device interface."
        )
        has_sensitive_variables = False
        soft_time_limit = 300
        time_limit = 600
        task_queues = ["default", "priority"]

    def run(
        self,
        prefix=None,
        interface=None,
        dns_name="",
        description="",
        dry_run=True,
    ):
        if not prefix:
            self.logger.error("No prefix provided.")
            return

        network = ipaddress.ip_network(str(prefix.prefix), strict=False)

        # Cap search to avoid iterating millions of addresses in huge prefixes.
        # For /8 that would be 16M iterations; limit to first 65k hosts.
        MAX_SEARCH = 65_536
        if network.num_addresses > MAX_SEARCH:
            self.logger.warning(
                f"Prefix {prefix} is large ({network.num_addresses} addresses). "
                f"Only the first {MAX_SEARCH} addresses will be searched."
            )

        existing_hosts = set(
            ipaddress.ip_address(h)
            for h in IPAddress.objects.filter(parent=prefix).values_list("host", flat=True)
        )

        next_ip = None
        for i, host in enumerate(network.hosts()):
            if i >= MAX_SEARCH:
                break
            if host not in existing_hosts:
                next_ip = str(host)
                break

        if not next_ip:
            self.logger.error(f"No available IPs in prefix {prefix}.")
            return

        mask = network.prefixlen
        address_with_prefix = f"{next_ip}/{mask}"

        self.logger.info(
            f"Next available IP in {prefix}: {address_with_prefix}"
        )

        if dry_run:
            self.logger.info(
                f"DRY RUN: Would create IPAddress {address_with_prefix}"
                + (f" and assign to {interface}" if interface else "")
            )
            return

        try:
            active_status = Status.objects.get(name="Active")
            ip_obj = IPAddress(
                address=address_with_prefix,
                status=active_status,
                dns_name=dns_name or "",
                description=description or "",
            )
            ip_obj.validated_save()
            self.logger.info(f"Created IPAddress: {ip_obj}")

            if interface:
                interface.ip_addresses.add(ip_obj)
                interface.validated_save()
                self.logger.info(f"Assigned {ip_obj} to interface {interface} on {interface.device}.")

        except Exception as exc:
            self.logger.error(f"Failed to allocate IP: {exc}")


register_jobs(IPAddressAllocation)
