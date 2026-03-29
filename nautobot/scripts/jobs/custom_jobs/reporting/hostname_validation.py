from nautobot.apps.jobs import Job, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models.locations import Location
from nautobot.dcim.models.devices import Device

import re

name = "Reporting"


class ValidateHostname(Job):

    location_to_check = ObjectVar(
        model=Location,
    )
    regex_pattern = StringVar(default=r"^AB-[A-Z]{3}-\w+(-\d)?-\w+\d{2}$")

    class Meta:
        name = "Validate Device Hostnames"
        description = "Validate device hostnames under a Nautobot location."
        has_sensitive_variables = False

    def run(self, location_to_check, regex_pattern):
        device_query = Device.objects.filter(location=location_to_check)

        report = "Hostname,Valid\n"

        # Find any invalid hostnames that do not match AB-[A-Z]{3}-\w+-\d-\w+\d{2}
        # regex_pattern = r"^AB-[A-Z]{3}-\w+(-\d)?-\w+\d{2}$"
        self.logger.info(f"Regex pattern used: {regex_pattern}")
        pattern = re.compile(regex_pattern)
        for device in device_query:
            if not pattern.match(device.name):
                self.logger.error(
                    f"Invalid hostname: {device.name}",
                    extra={"object": device},
                )
                report = report + f"{device},False\n"
            else:
                self.logger.info(
                    f"Valid hostname: {device.name}",
                    extra={"object": device},
                )
                report = report + f"{device},True\n"

        self.create_file("hostname_validation.csv", report)


register_jobs(ValidateHostname)
