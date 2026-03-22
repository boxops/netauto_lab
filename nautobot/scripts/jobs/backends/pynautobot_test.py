"""
Add devices to sites on Nautobot
Device: AB-ABB-NF016-1-OLT01
Site: NF016 Cab 1
Where NF016 is Device[2] and Cab 1 is Device[3]
"""

import pynautobot
import requests
import urllib3
from pprint import pprint
import json

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
requests.packages.urllib3.disable_warnings()


class NautobotBackend:
    """Get Nautobot data."""

    def __init__(self):
        self.nautobot = pynautobot.api(
            url="https://nautobot.air-band.local",
            token="3878cf86b9881ec8fbce8c2261ae41047ab0cacc",
            verify=False,
            api_version="2.1",
        )
        # Use requests
        self.nautobot_url = "https://nautobot.air-band.local"
        self.nautobot_token = "3878cf86b9881ec8fbce8c2261ae41047ab0cacc"
        self.headers = {
            "Authorization": f"Token {self.nautobot_token}",
            "Accept": "application/json",
        }
        self.all_devices = None
        self.device = None
        self.device_name = None
        self.calculated_location = None
        self.location = None

    def get_location(self):
        """Get the location."""
        try:
            self.location = self.nautobot.dcim.locations.get(
                name=self.calculated_location
            )
            if self.location:
                print(f"Location {self.calculated_location} found.")
        except Exception as e:
            print(f"Location {self.calculated_location} not found.")
        pprint(self.location)

    def get_device(self):
        """Get the device."""
        self.device = self.nautobot.dcim.devices.get(name=self.device_name)
        pprint(self.device)

    def requests_get_all_devices(self):
        """Get all devices using requests."""
        url = f"{self.nautobot_url}/api/dcim/devices/"
        response = requests.get(url, headers=self.headers, verify=False)
        self.all_devices = response.json()["results"]

    def save_all_devices_to_file(self):
        """Save all devices to a JSON file."""
        with open("all_devices.json", "w") as f:
            json.dump(self.all_devices, f, indent=4)

    def load_all_devices_from_file(self):
        """Load all devices from a JSON file."""
        with open("all_devices.json", "r") as f:
            self.all_devices = json.load(f)

    def requests_graphql_get_all_devices(self):
        """Get all devices using GraphQL."""
        query = """
        query {
        devices {
            name
            location {
            name
            }
        }
        }
        """
        url = f"{self.nautobot_url}/api/graphql/"
        response = requests.post(
            url, headers=self.headers, verify=False, json={"query": query}
        )
        self.all_devices = response.json()["data"]["devices"]

    def get_all_devices(self):
        """Get all devices."""
        self.all_devices = self.nautobot.dcim.devices.all()
        pprint(self.all_devices)

    def get_all_device_pages(self):
        """Get all device pages."""
        self.all_devices = self.nautobot.dcim.devices.all()
        with open("all_devices.json", "w") as f:
            json.dump(self.all_devices, f, indent=4)
        # pprint(self.all_devices)

    def calculate_location_name_with_cab(self):
        """Calculate the location from the device name."""
        location = self.device_name.split("-")[2]
        cab = self.device_name.split("-")[3]
        self.calculated_location = f"{location} Cab {cab}"
        print(self.calculated_location)

    def calculate_location_name_without_cab(self):
        """Calculate the location from the device name."""
        self.calculated_location = self.device_name.split("-")[2]
        print(self.calculated_location)

    def update_device_location(self):
        """Update the device location."""
        if self.device.location.id == self.location.id:
            return
        self.device.location = self.location
        self.device.save()
        print(f"Device {self.device_name} added to site {self.location.name}")

    def execute(self):
        """Execute the process."""
        for device in self.all_devices:
            try:
                if device["location"]["name"] == "Unknown":
                    self.device_name = device["name"]
                    self.get_device()
                    self.calculate_location_name_with_cab()
                    self.get_location()
                    if self.location:
                        self.update_device_location()
                    else:
                        self.calculate_location_name_without_cab()
                        self.get_location()
                        self.update_device_location()
            except Exception as e:
                print(f"Error processing device {self.device_name}: {e}")


if __name__ == "__main__":
    nautobot = NautobotBackend()
    # nautobot.requests_graphql_get_all_devices()
    # nautobot.save_all_devices_to_file()

    nautobot.load_all_devices_from_file()
    nautobot.execute()
