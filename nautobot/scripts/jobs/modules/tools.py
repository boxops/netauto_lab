"""A collection of tools for use in Nautobot Jobs."""

import os
import re
import subprocess
import difflib
import textfsm
from subprocess import Popen, PIPE
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import csv
import hashlib
import json
import logging
import queue
from django.db.models import Q
from logging.handlers import QueueHandler, QueueListener

# from dictdiffer import diff

from nautobot.extras.models.secrets import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.dcim.models import (
    Device,
    DeviceType,
    Location,
    Manufacturer,
    Platform,
    Rack,
    RackGroup,
)
from nautobot.extras.models import DynamicGroup, Role, Status, Tag
from nautobot.apps.jobs import MultiObjectVar
from nautobot.tenancy.models import Tenant, TenantGroup

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def ping_device(host):
    if os.system(f"ping -c 1 {host}") != 0:
        return False
    else:
        return True


def strip_namespace(tag):
    """Strip namespace from XML tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def xml_to_dict(xml_string, strip_namespaces=False):
    """Convert XML to a dictionary with optional namespace stripping.

    Example:
    xml_string = "<root>
        <child1>value1</child1>
        <child2>value2</child2>
    </root>"

    result = xml_to_dict(xml_string, strip_namespaces=True)
    print(result)
    """

    def _element_to_dict(element):
        node = {}
        for child in element:
            tag = strip_namespace(child.tag) if strip_namespaces else child.tag
            node[tag] = _element_to_dict(child)
        if element.text and element.text.strip():
            node["text"] = element.text.strip()
        return node

    root = ET.fromstring(xml_string)
    root_tag = strip_namespace(root.tag) if strip_namespaces else root.tag
    return {root_tag: _element_to_dict(root)}


# TODO: Add error handling
def get_device_connection_info(device):
    """Get device information for use in Netmiko connection."""
    device_info = {
        "device_type": device.platform.network_driver,
        "ip": device.primary_ip4.host,
        "username": device.secrets_group.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_USERNAME,
        ),
        "password": device.secrets_group.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
        ),
        "secret": device.secrets_group.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_SECRET,
        ),
        "port": 22,
        "global_delay_factor": 2,
        # SSH compatibility options for older devices
        "ssh_config_file": False,
        "allow_agent": False,
        # "look_for_keys": False,
        "disabled_algorithms": {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]},
        "ssh_strict": False,
        "session_log": f"/tmp/netmiko_session_{device.name}_{device.id}.log",
    }

    # Additional SSH parameters for very old or problematic devices
    # These help with devices that have outdated SSH implementations
    legacy_ssh_params = {
        "use_keys": False,
        # "key_policy": "paramiko.AutoAddPolicy()",
        "banner_timeout": 60,
        "blocking_timeout": 60,
        "timeout": 60,
        "session_timeout": 60,
        "auth_timeout": 60,
        "fast_cli": False,
    }
    device_info.update(legacy_ssh_params)
    if device.platform.network_driver in [
        "fiberstore_fsos",
        "netonix_os",
        "ubiquiti_airos",
        "ubiquiti_edge",
        "ubiquiti_edgeswitch",
        "ceragon_os",
        "siklu_os",
        "cambium_cnmatrix",
    ]:
        device_info["device_type"] = "generic"
    return device_info


# TODO: Add error handling
def get_ftp_server_credentials(credential):
    """Get the FTP server connection parameters."""
    return {
        "host": credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_KEY,
        ),
        "username": credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_USERNAME,
        ),
        "password": credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
        ),
    }


def send_bash_command(command, verbose=False):
    """
    Send a bash command to localhost.

    Parameters:
        command (str): Bash command to send, required
        verbose (bool): Print output, default False

    Example:
        send_bash_command('ls -l')

    Returns:
        None
    """
    if verbose:
        print(f"Sending bash command: {command}")
    process = Popen(command, shell=True, stdout=PIPE)
    out, err = process.communicate()
    if verbose:
        print(out)
        print(err)


def parse_command_output(command_output, template_file):
    """Parse command output using TextFSM template."""
    with open(f"{BASE_DIR}/templates/{template_file}") as file:
        template = textfsm.TextFSM(file)
        parsed_output = template.ParseText(command_output)
    headers = template.header
    return [dict(zip(headers, row)) for row in parsed_output]


def find_match(pattern, output):
    match = re.search(pattern, output)
    if match:
        found = (match.group(1)).strip(",")
        print(f"Found: {found}")
        return found
    else:
        raise Exception(
            f"Could not parse, pattern: {pattern} not found in the input string: {output}"
        )


def convert_flat_config_to_dict(config: str):
    """Convert a flat configuration to a dictionary.

    Example:
    cat /tmp/system.cfg
    aaa.1.devname=ath0
    """
    config_dict = {}
    for line in config.splitlines():
        if "=" in line:
            line = line.strip()
            if line:
                key, value = line.split("=", 1)
                config_dict[key] = value
    return config_dict


def apply_device_filters(
    all_devices=None,
    tenant_group=None,
    tenant=None,
    location=None,
    rack_group=None,
    rack=None,
    role=None,
    manufacturer=None,
    platform=None,
    device_type=None,
    tags=None,
    status=None,
):
    """Apply filters to a queryset of devices using AND logic."""
    # Check if any filters were provided
    filters_provided = any(
        [
            tenant_group,
            tenant,
            location,
            rack_group,
            rack,
            role,
            manufacturer,
            platform,
            device_type,
            tags,
            status,
        ]
    )

    # If no filters are provided, return the empty set as is
    # This prevents adding all devices when only the device parameter is used
    if not filters_provided:
        return all_devices

    # Start with all devices
    queryset = Device.objects.all()

    # Build Q objects for each filter
    q_objects = Q()

    if tenant_group:
        q_objects &= Q(tenant__group__in=tenant_group)
    if tenant:
        q_objects &= Q(tenant__in=tenant)
    if location:
        # use the descendants relationship to get all devices from a region and sites below
        # region = Location.objects.get(name="Oxfordshire")
        # child_locations = region.descendants()
        # devices = Device.objects.filter(location__in=child_locations)
        q_objects &= Q(location__in=location)
    if rack_group:
        q_objects &= Q(rack__group__in=rack_group)
    if rack:
        q_objects &= Q(rack__in=rack)
    if role:
        q_objects &= Q(role__in=role)
    if manufacturer:
        q_objects &= Q(manufacturer__in=manufacturer)
    if platform:
        q_objects &= Q(platform__in=platform)
    if device_type:
        q_objects &= Q(device_type__in=device_type)
    if status:
        q_objects &= Q(status__in=status)

    # Apply all the filters
    queryset = queryset.filter(q_objects)

    # Handle tags separately as they have a many-to-many relationship
    if tags:
        for t in tags:
            queryset = queryset.filter(tags=t)

    # Update the all_devices set
    all_devices.update(queryset)

    return all_devices


def parallel_execution(task_func, devices, max_workers, logger=None):
    """Execute tasks in parallel with memory management."""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for device in devices:
            futures.append(executor.submit(task_func, device))

        # Process results as they complete to prevent accumulation
        for future in as_completed(futures):
            try:
                future.result()  # This will raise any exceptions
            except Exception as e:
                if logger:
                    logger.error(f"Error in parallel task: {e}")
            finally:
                # Explicit cleanup
                del future


# def parallel_execution(function, devices, max_workers, *args):
#     """
#     Execute a function for each device in a list using a thread pool.

#     Parameters:
#         function (function): Function to execute
#         devices (list): List of devices to process
#         *args: Additional arguments to pass to the function

#     Example:
#         execute_function_for_devices(devices, function, *args, logger=logger)

#     Returns:
#         None
#     """
#     with ThreadPoolExecutor(max_workers=max_workers) as executor:
#         futures = [executor.submit(function, device, *args) for device in devices]
#         for future in as_completed(futures):
#             future.result()

#     # # Set up a queue for logging
#     # log_queue = queue.Queue()
#     # queue_handler = QueueHandler(log_queue)
#     # listener = QueueListener(log_queue, *logging.getLogger().handlers)
#     # listener.start()

#     # # Add the queue handler to the root logger
#     # logging.getLogger().addHandler(queue_handler)

#     # with ThreadPoolExecutor(max_workers=max_workers) as executor:
#     #     futures = [executor.submit(function, device, *args) for device in devices]
#     #     for future in as_completed(futures):
#     #         future.result()

#     # # Remember to stop the listener when done
#     # listener.stop()


class DeviceFormEntry:
    """Class definition to use as Mixin for form definitions."""

    tenant_group = MultiObjectVar(model=TenantGroup, required=False)
    tenant = MultiObjectVar(model=Tenant, required=False)
    location = MultiObjectVar(model=Location, required=False)
    rack_group = MultiObjectVar(model=RackGroup, required=False)
    rack = MultiObjectVar(model=Rack, required=False)
    role = MultiObjectVar(model=Role, required=False)
    manufacturer = MultiObjectVar(model=Manufacturer, required=False)
    platform = MultiObjectVar(model=Platform, required=False)
    device_type = MultiObjectVar(
        model=DeviceType, required=False, display_field="display"
    )
    device = MultiObjectVar(model=Device, required=False)
    # dynamic_group = MultiObjectVar(model=DynamicGroup, required=False)
    tags = MultiObjectVar(
        model=Tag,
        required=False,
        display_field="name",
        query_params={"content_types": "dcim.device"},
    )
    status = MultiObjectVar(
        model=Status,
        required=False,
        query_params={"content_types": Device._meta.label_lower},
        display_field="label",
        label="Device Status",
    )


def _open_file_config(cfg_path: str) -> str:
    """Open config file from local disk."""
    # This might fail, raising an IOError
    with open(cfg_path, encoding="utf-8") as filehandler:
        device_cfg = filehandler.read()
    return device_cfg.strip()


def diff_files(backup_file, intended_file):
    """Utility function to provide `Unix Diff` between two files."""
    with open(backup_file, encoding="utf-8") as file:
        backup = file.readlines()
    with open(intended_file, encoding="utf-8") as file:
        intended = file.readlines()

    yield from difflib.unified_diff(backup, intended, lineterm="")
