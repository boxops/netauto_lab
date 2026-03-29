import os
import subprocess
from netmiko import ConnectHandler
from django.conf import settings

from nautobot.apps.jobs import register_jobs, Job, TextVar

name = "Operations"


class PasswordProber(Job):
    """Job to probe passwords of devices in the network."""

    ip_addresses = TextVar(
        description="List of IP addresses to probe, separated by newlines",
        required=True,
    )
    usernames = TextVar(
        description="List of usernames to probe, separated by newlines",
        required=True,
    )
    passwords = TextVar(
        description="List of passwords to probe, separated by newlines",
        required=True,
    )

    class Meta:
        name = "Password Prober"
        description = "Probe credentials of devices using SSH"
        has_sensitive_variables = True
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400  # 40 minutes
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
            "bulk",
        ]

    def run(self, ip_addresses, usernames, passwords):
        """Probe passwords of devices in the network."""
        credentials = ["IP,Username,Password,Result\n"]

        ip_list = [ip.strip() for ip in ip_addresses.split("\n") if ip.strip()]
        username_list = [
            username.strip() for username in usernames.split("\n") if username.strip()
        ]
        password_list = [
            password.strip() for password in passwords.split("\n") if password.strip()
        ]

        for ip in ip_list:
            try:
                result = subprocess.run(
                    ["ping", "-c", "1", ip], capture_output=True, text=True
                )
                if result.returncode != 0:
                    self.logger.error(f"Failed to ping {ip}")
                    credentials.append(f"{ip},,,Failed to ping\n")
                    continue
            except Exception as e:
                self.logger.error(f"Error pinging {ip}: {e}")
                credentials.append(f"{ip},,,Error pinging\n")
                continue

            login_successful = False
            for username in username_list:
                for password in password_list:
                    try:
                        device_info = {
                            "device_type": "generic",
                            "ip": ip,
                            "username": username,
                            "password": password,
                            "global_delay_factor": 2,
                        }
                        with ConnectHandler(**device_info) as session:
                            session.find_prompt()
                            self.logger.info(f"Successfully logged in to {ip}")
                            credentials.append(f"{ip},{username},{password},Success\n")
                            login_successful = True
                            break
                    except Exception as e:
                        self.logger.error(
                            f"Failed to login to {ip} with {username}/{password}: {e}"
                        )
                if login_successful:
                    break

            if not login_successful:
                credentials.append(f"{ip},,,Failed to login with all credentials\n")

        credentials_str = "".join(credentials)
        self.create_file("credentials.csv", credentials_str)


register_jobs(PasswordProber)
