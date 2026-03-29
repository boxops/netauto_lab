"""
Purpose: Onboard serial numbers from actual devices to Nautobot.
"""

from django.conf import settings
import json

from nautobot.apps.jobs import register_jobs, Job, ObjectVar, BooleanVar
from nautobot.extras.models.secrets import SecretsGroup
from nautobot.extras.models.secrets import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)

from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry
from custom_jobs.monitoring.solarwinds import SolarWinds

name = "Monitoring"

SUPPORTED_PLATFORMS = [
    "keymile_nos",
    # "fiberstore_fsos",
    # "mikrotik_routeros",
    # "netonix_os",
    # "cisco_ios",
    "cisco_xr",
    # "cisco_xe",
    # "cisco_nxos",
    # "cisco_s300",
    # "ubiquiti_airos",
    # "ubiquiti_edge",
    # "ubiquiti_edgeswitch",
    # "ceragon_os",
    # "siklu_os",
    "certa_os",
    "arista_eos",
]


def get_default_credential():
    try:
        return SecretsGroup.objects.get(name="SOLARWINDS_NPM_API")
    except SecretsGroup.DoesNotExist:
        return None


class ProvisionNodesOnSolarWinds(Job, DeviceFormEntry):
    credential = ObjectVar(
        model=SecretsGroup,
        description="SolarWinds API SecretsGroup",
        required=True,
        default=get_default_credential(),
    )
    update_custom_properties = BooleanVar(
        description="Update custom properties to the node", required=False, default=True
    )
    update_pollers = BooleanVar(
        description="Update pollers on the node", required=False, default=True
    )
    update_undp = BooleanVar(
        description="Update Universal Device Pollers on the node",
        required=False,
        default=True,
    )
    update_interfaces = BooleanVar(
        description="Update interfaces to the node", required=False, default=True
    )

    class Meta:
        name = "Provision Nodes on SolarWinds"
        description = f"Supported platforms: {SUPPORTED_PLATFORMS}"
        has_sensitive_variables = False
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400  # 40 minutes
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

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
        credential=None,
        update_custom_properties=None,
        update_pollers=None,
        update_undp=None,
        update_interfaces=None,
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

        for device in all_devices:
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    continue
                self.logger.info(f"Processing device: {device}")
                task = ProvisionNode(
                    self,
                    device,
                    credential,
                    update_custom_properties,
                    update_pollers,
                    update_undp,
                    update_interfaces,
                )
                task.provision()
            except Exception as e:
                self.logger.error(f"Error processing device {device}: {e}")


class ProvisionNode:

    def __init__(
        self,
        job,
        device,
        credential,
        update_custom_properties,
        update_pollers,
        update_undp,
        update_interfaces,
    ):
        self.job = job
        self.device = device
        self.credential = credential
        self.update_custom_properties = update_custom_properties
        self.update_pollers = update_pollers
        self.update_undp = update_undp
        self.update_interfaces = update_interfaces

        npm_server = credential.get_secret_value(
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
        self.sw = SolarWinds(
            npm_server=npm_server,
            username=username,
            password=password,
            logger=self.job.logger,
        )

        self.gathered_data = None

    def get_config_context(self):
        context = self.device.get_config_context()
        # {
        #     "Pollers": {
        #         "N.Routing.SNMP.Ipv4RoutingTable": false,
        #         "N.Routing.SNMP.Ipv6RoutingTable": false,
        #         "N.RoutingNeighbor.SNMP.BGP": false,
        #         "N.Topology_LLDP.SNMP.lldpRemoteSystemsData": true,
        #         "N.Topology_Layer2.SNMP.Dot1qTpFdbNoVLANs": true,
        #         "N.Topology_Layer3.SNMP.ipNetToMedia": false,
        #         "N.Topology_Layer3_IpRouting.SNMP.ipForwardRouter": false,
        #         "N.Topology_Layer3_IpRouting.SNMP.rolesRouter": false,
        #         "N.Topology_PortsMap.SNMP.Dot1qVlanEgressPorts": true,
        #         "N.Topology_Vlans.SNMP.Dot1q": true
        #     },
        #     "UniversalDevicePollers": [
        #         "ONUDEACTREASON",
        #         "ONUSN",
        #         "ONUName",
        #         "ONUINACTIVETIME",
        #         "RXTR",
        #         "ONUSTATUS",
        #         "ONUAUTHSTATUS",
        #         "ONUPROFILE"
        #     ]
        # }

        # # Enrich the context with device specific data
        # custom_properties_update = {
        #     "Area": self.device.location.name,
        #     "Device_Type": self.device.role.name,
        # }

        # if "CustomProperties" in context:
        #     context["CustomProperties"].update(custom_properties_update)
        # else:
        #     context["CustomProperties"] = custom_properties_update

        context.update(
            {
                "Caption": self.device.name,
                "IPAddress": self.device.primary_ip.host,
            }
        )

        self.gathered_data = context

    def get_custom_fields(self):
        custom_fields = self.device.custom_field_data
        # "custom_fields": {
        #     "area": null,
        #     "can_connect": true,
        #     "customer_type": "Internal",
        #     "in_service": true,
        #     "is_mgmt_up": true,
        #     "last_network_data_sync": null,
        #     "monitoredinterfaces": [
        #         "Port23",
        #         "Port24",
        #         "Port25"
        #     ],
        #     "network_layer": "None",
        #     "snmpcommunity": "Airband-F3g&1h"
        # },

        # Enrich the context with device specific data
        custom_properties_update = {
            "Area": custom_fields.get("area", "Unknown"),
            "Customer_Type": custom_fields.get("customer_type", "Internal"),
            "Device_Type": self.device.role.name,
            "In_Service": custom_fields.get("in_service", True),
            "Is_Mgmt_Up": custom_fields.get("is_mgmt_up", True),
            "Network_Layer": custom_fields.get("network_layer", "None"),
        }

        if "CustomProperties" in self.gathered_data:
            self.gathered_data["CustomProperties"].update(custom_properties_update)
        else:
            self.gathered_data["CustomProperties"] = custom_properties_update

        self.gathered_data["MonitoredInterfaces"] = custom_fields.get(
            "monitoredinterfaces"
        )
        self.gathered_data["SNMPCommunity"] = custom_fields.get(
            "snmpcommunity", "public"
        )

    def get_or_create_node(self):
        self.sw.add_node_using_snmp_v2(
            node_name=self.gathered_data["Caption"],
            ip_address=self.gathered_data["IPAddress"],
            snmp_community=self.gathered_data["SNMPCommunity"],
        )

    def func_update_custom_properties(self):
        for key, value in self.gathered_data["CustomProperties"].items():
            self.sw.set_node_custom_property(
                node_name=self.gathered_data["Caption"],
                custom_property_name=key,
                custom_property_value=value,
            )
        self.sw.get_node_custom_properties(node_name=self.gathered_data["Caption"])

    def func_update_pollers(self):
        # All available pollers
        # N.Cpu.Agent.Linux
        # N.Cpu.SNMP.CiscoGen3
        # N.Cpu.SNMP.F5BigIpSystemHost
        # N.Cpu.SNMP.Fortigate
        # N.Cpu.SNMP.HrProcessorLoad
        # N.Cpu.SNMP.NetSnmpSystemStats
        # N.Cpu.WMI.Windows
        # N.Details.Agent.Linux
        # N.Details.SNMP.F5
        # N.Details.SNMP.Generic
        # N.Details.WMI.Generic
        # N.Details.WMI.Vista
        # N.EnergyWise.SNMP.Cisco
        # N.IPAddresses.Agent.Linux
        # N.LoadAverage.Agent.Linux
        # N.LoadAverage.SNMP.Linux
        # N.Memory.Agent.Linux
        # N.Memory.SNMP.CiscoAsr
        # N.Memory.SNMP.CiscoGen3
        # N.Memory.SNMP.F5BigIpDashboard
        # N.Memory.SNMP.F5BigIpSystemHost
        # N.Memory.SNMP.Fortigate
        # N.Memory.SNMP.HrStorage
        # N.Memory.SNMP.JuniperJunOS
        # N.Memory.SNMP.NetSnmpReal
        # N.Memory.WMI.Windows
        # N.Nexus.SNMP.DeviceContext
        # N.ResponseTime.Agent.Native
        # N.ResponseTime.ICMP.Native
        # N.ResponseTime.SNMP.Native
        # N.Routing.SNMP.Ipv4CidrRoutingTable
        # N.Routing.SNMP.Ipv4RoutingTable
        # N.Routing.SNMP.Ipv6RoutingTable
        # N.RoutingNeighbor.SNMP.BGP
        # N.RoutingNeighbor.SNMP.OSPF
        # N.RoutingNeighbor.SNMP.OSPFv3
        # N.Status.Agent.Native
        # N.Status.ICMP.Native
        # N.Status.SNMP.Native
        # N.SwitchStack.SNMP.Cisco
        # N.Topology_CDP.SNMP.cdpCacheTable
        # N.Topology_Layer2.SNMP.Dot1dTpFdb
        # N.Topology_Layer2.SNMP.Dot1dTpFdbNoVLANs
        # N.Topology_Layer2.SNMP.Dot1qTpFdb
        # N.Topology_Layer2.SNMP.Dot1qTpFdbNoVLANs
        # N.Topology_Layer3.SNMP.ipNetToMedia
        # N.Topology_Layer3.SNMP.ipNetToPhysical
        # N.Topology_Layer3_IpRouting.SNMP.ipCidrRouter
        # N.Topology_Layer3_IpRouting.SNMP.ipForwardRouter
        # N.Topology_Layer3_IpRouting.SNMP.rolesRouter
        # N.Topology_LLDP.SNMP.lldpRemoteSystemsData
        # N.Topology_PortsMap.SNMP.Dot1dBase
        # N.Topology_PortsMap.SNMP.Dot1dBaseNoVLANs
        # N.Topology_PortsMap.SNMP.Dot1qVlanEgressPorts
        # N.Topology_STP.SNMP.Dot1dStp
        # N.Topology_Vlans.SNMP.Dot1q
        # N.Topology_Vlans.SNMP.VtpVlan
        # N.Uptime.Agent.Linux
        # N.Uptime.SNMP.Generic
        # N.Uptime.WMI.Generic
        # N.Uptime.WMI.XP
        # N.VirtualPortChannels.CLI.CiscoNexus
        # N.VRFRouting.SNMP.CiscoVrfMib
        # N.VRFRouting.SNMP.MPLSVPNStandard
        # N.WirelessAP.SNMP.Generic
        for poller, enabled in self.gathered_data["Pollers"].items():
            self.sw.attach_poller_to_node(
                node_name=self.gathered_data["Caption"],
                poller_name=poller,
                enabled=enabled,
            )

    def func_update_undp(self):
        list_of_undps = self.sw.get_list_of_custom_pollers_for_node(
            node_name=self.gathered_data["Caption"]
        )
        # [{'CustomPollerName': 'ONURX'},
        # {'CustomPollerName': 'ONUUPTIME'},
        # {'CustomPollerName': 'ONUName'},
        # {'CustomPollerName': 'ONUUptimeTransformed'},
        # {'CustomPollerName': 'ONUDEACTREASON'},
        # {'CustomPollerName': 'ONUSN'},
        # {'CustomPollerName': 'ONUSTATUS'},
        # {'CustomPollerName': 'ONUAUTHSTATUS'},
        # {'CustomPollerName': 'ONUPROFILE'},
        # {'CustomPollerName': 'ONUINACTIVETIME'},
        # {'CustomPollerName': 'GPONPORT'},
        # {'CustomPollerName': 'RXTR'}]

        # Add any pollers that are in the template but not in the list of UNDPs
        for undp in self.gathered_data["UniversalDevicePollers"]:
            if undp not in [poller["CustomPollerName"] for poller in list_of_undps]:
                self.sw.add_custom_poller_by_name(
                    node_name=self.gathered_data["Caption"], poller_name=undp
                )

        # Remove any pollers that are not in the template
        for poller in list_of_undps:
            if (
                poller["CustomPollerName"]
                not in self.gathered_data["UniversalDevicePollers"]
            ):
                self.sw.remove_custom_poller_by_name(
                    node_name=self.gathered_data["Caption"],
                    poller_name=poller["CustomPollerName"],
                )

    def func_update_interfaces(self):
        discovered_interfaces = self.sw.get_discovered_interfaces(
            node_name=self.gathered_data["Caption"]
        )
        # pprint(discovered_interfaces)
        # [{'Caption': 'br20 · Management',
        # 'InterfaceID': 0,
        # 'Manageable': True,
        # 'ifAdminStatus': 1,
        # 'ifIndex': 1003,
        # 'ifOperStatus': 1,
        # 'ifSpeed': 0.0,
        # 'ifSubType': 0,
        # 'ifType': 6}]

        current_interfaces = self.sw.get_list_of_interfaces(
            node_name=self.gathered_data["Caption"]
        )
        self.job.logger.info(f"Current interfaces: {current_interfaces}")

        # add anything that is in MonitoredInterfaces
        for interface in discovered_interfaces:
            for port in self.gathered_data["MonitoredInterfaces"]:
                if port in interface["Caption"]:
                    self.sw.add_interface(
                        node_name=self.gathered_data["Caption"],
                        interface_name=interface["Caption"],
                    )

        # remove anything that is not in MonitoredInterfaces
        for interface in current_interfaces:
            if not any(
                port in interface["Name"]
                for port in self.gathered_data["MonitoredInterfaces"]
            ):
                self.sw.remove_interface(
                    node_name=self.gathered_data["Caption"],
                    interface_name=interface["Name"],
                )

        updated_interfaces = self.sw.get_list_of_interfaces(
            node_name=self.gathered_data["Caption"]
        )
        self.job.logger.info(f"Updated interfaces: {updated_interfaces}")

    def unmanage(self):
        # self.sw.unmanage_node(node_name=self.gathered_data["Caption"])
        pass

    def remanage(self):
        # self.sw.remanage_node(node_name=self.gathered_data["Caption"])
        pass

    def delete(self):
        self.sw.delete_node(node_name=self.gathered_data["Caption"])

    def func_poll_now(self):
        self.sw.poll_now(node_name=self.gathered_data["Caption"])

    def provision(self):
        self.get_config_context()
        self.get_custom_fields()
        self.job.logger.info(
            f"Gathered data: {json.dumps(self.gathered_data, indent=4)}"
        )
        self.get_or_create_node()
        if self.device.status.name == "Active":
            self.remanage()
        elif self.device.status.name == "Unmanaged":
            self.unmanage()
        else:
            self.delete()
            return
        if self.update_custom_properties:
            self.func_update_custom_properties()
        if self.update_pollers:
            self.func_update_pollers()
        if self.update_undp:
            self.func_update_undp()
        if self.update_interfaces:
            self.func_update_interfaces()
        self.func_poll_now()


register_jobs(ProvisionNodesOnSolarWinds)
