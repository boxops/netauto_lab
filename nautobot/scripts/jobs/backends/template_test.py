import os
import textfsm
from io import StringIO
from netmiko import ConnectHandler
from pprint import pprint
from dotenv import load_dotenv
import json

load_dotenv()


def parse_command_output_from_file(command_output: str, template_file: str):
    with open(f"{template_file}") as file:
        template = textfsm.TextFSM(file)
        parsed_output = template.ParseText(command_output)
    headers = template.header
    return [dict(zip(headers, row)) for row in parsed_output]


def parse_command_output_from_string(command_output: str, template_string: str):
    template = textfsm.TextFSM(StringIO(template_string))
    parsed_output = template.ParseText(command_output)
    headers = template.header
    return [dict(zip(headers, row)) for row in parsed_output]


show_license_status_output = """root>platform software show versions
Downloaded version:   12.7.0.0.0.274                
Installed version:    12.7.0.0.0.274                
root>
"""

show_license_status_template = """Value VERSION (\S+)
Value DOWNLOADED_VERSION (\S+)
 
Start
  ^Downloaded\ version:\s+${VERSION}
  ^Installed\ version:\s+${DOWNLOADED_VERSION} -> Record
  ^nou\s+${VERSION}\s+${DOWNLOADED_VERSION} -> Record
"""

parsed_output = parse_command_output_from_string(
    show_license_status_output, show_license_status_template
)
# show_license_status_template = "example.textfsm"
# parsed_output = parse_command_output(
#     show_license_status_output, show_license_status_template
# )
pprint(parsed_output)

# device = {
#     "device_type": "cisco_xr",
#     "host": "10.8.255.5",
#     "username": os.environ["SSH_USERNAME"],
#     "password": os.environ["SSH_PASSWORD"],
#     "secret": os.environ["SSH_SECRET"],
#     "session_log": "netmiko.log",
#     "verbose": True,
#     "global_delay_factor": 2,
# }

# with ConnectHandler(**device) as session:
#     session.enable()

#     show_license_status_template = """
#     Value REG_STATUS (REGISTERED|UNREGISTERED)

#     Start
#     ^Registration:
#     ^\s+Status: ${REG_STATUS}
#     """

# platform_commands = {
#     "cisco_xr": (
#         "show interfaces",
#         "cisco_xr_show_interfaces.textfsm",
#     ),
# }
# command, parser_file = platform_commands[device["device_type"]]
# output = session.send_command(command)
# parsed_output = parse_command_output(output, parser_file)
# pprint(parsed_output)

# with open("template_test_result.json", "w") as file:
#     json.dump(parsed_output, file, indent=4)

# command = "platform software show versions"
# output = session.send_command(command)
# parsed_output = parse_command_output(output, "ceragon_os_show_versions.textfsm")
# pprint(parsed_output)

# parsed_version = parsed_output[0]["SN"]
# print(parsed_version)
