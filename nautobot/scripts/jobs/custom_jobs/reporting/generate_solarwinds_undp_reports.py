"""
Purpose:
Check device serial numbers under a Nautobot location.
"""

import io
import csv
import hashlib
import json
import os
from typing import Any, Dict, List, Optional
import requests

from nautobot.apps.jobs import Job, register_jobs, ChoiceVar, ObjectVar, BooleanVar
from nautobot.extras.models.secrets import SecretsGroup
from nautobot.extras.models.secrets import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)

# from custom_jobs.modules.tools import load_csv, compare_csv_files
from deepdiff import DeepDiff

CHOICES = [
    ("keymile", "Keymile"),
    ("cambium", "Cambium"),
]

name = "Reporting"


def get_default_credential():
    try:
        return SecretsGroup.objects.get(name="SOLARWINDS_NPM_API")
    except SecretsGroup.DoesNotExist:
        return None


class SolarWindsUNDPReport(Job):

    vendor = ChoiceVar(
        description="Select a vendor",
        choices=CHOICES,
        required=True,
    )
    credential = ObjectVar(
        model=SecretsGroup,
        description="SolarWinds API SecretsGroup",
        required=True,
        default=get_default_credential(),
    )
    compare_to_last = BooleanVar(
        description="Compare current report to the last report",
        required=False,
    )

    class Meta:
        name = "Generate Subscriber Reports from SolarWinds"
        description = "Connect to SolarWinds and generate connected subscriber reports from Universal Device Poller metrics."
        has_sensitive_variables = False

    def run(self, vendor, credential, compare_to_last):
        ORION_SERVER = credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_KEY,
        )
        ORION_USERNAME = credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_USERNAME,
        )
        ORION_PASSWORD = credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
        )
        BASE_URL = (
            f"https://{ORION_SERVER}:17774/SolarWinds/InformationService/v3/Json/Query"
        )

        if vendor == "keymile":
            VENDOR = "dasan"
            HEADERS = [
                "ONUID on",
                "ONUAUTHSTATUS on",
                "ONUDEACTREASON on",
                "ONUUPTIME on",
                "ONUINACTIVETIME on",
                "ONUNAME on",
                "ONUMODEL on",
                "ONUPROFILE on",
                "ONURX on",
                "ONUSN on",
                "ONUSTATUS on",
            ]
            LATEST_DATA_QUERY = """
            SELECT
                n.NodeID,
                n.DisplayName,
                n.IPAddress,
                cpt.CompressedRowID, 
                cpt.Status, 
                cpt.AssignmentName,
                cpt.DateTime
            FROM Orion.NPM.CustomPollerStatusOnNodeTabular AS cpt
            LEFT JOIN Orion.Nodes AS n
                ON cpt.NodeID = n.NodeID
            WHERE n.Vendor LIKE '%dasan%'
            """
        elif vendor == "cambium":
            VENDOR = "cambium"
            HEADERS = [
                "ManagementIP on",
                "PhysAddress on",
                "SessionState on",
                "LUID on",
                "RegCount on",
                "ReRegCount on",
                "SessionCount on",
                "SiteName on",
                "SMSession on",
                "SoftwareVersion on",
                "ProductType on",
                "AdaptRate on",
                "LastPowerLevel on",
                "SNRHorizontal on",
                "SNRVertical on",
                "SSR on",
            ]
            LATEST_DATA_QUERY = """
            SELECT
                n.NodeID,
                n.DisplayName,
                n.IPAddress,
                cpt.CompressedRowID, 
                cpt.Status, 
                cpt.AssignmentName,
                cpt.DateTime
            FROM Orion.NPM.CustomPollerStatusOnNodeTabular AS cpt
            LEFT JOIN Orion.Nodes AS n
                ON cpt.NodeID = n.NodeID
            WHERE n.Vendor LIKE '%motorola%'
            """
        else:
            return

        report = SolarWindsReport(
            ORION_USERNAME, ORION_PASSWORD, BASE_URL, job=self, vendor=VENDOR
        )

        latest_nodes = report.get_sw_nodes(LATEST_DATA_QUERY)
        if latest_nodes:
            filtered_data = report.filter_table(latest_nodes, HEADERS)
            result = report.pivot_table(filtered_data)
            # Save the results to a file on the Nautobot server
            self.logger.info(f"Saving report for {VENDOR}")
            report.save_report(result)
            if compare_to_last:
                report.diff_latest_and_previous()

            # Create an in-memory string buffer
            csv_buffer = io.StringIO()
            fieldnames = [
                "NodeID",
                "DisplayName",
                "IPAddress",
                "CompressedRowID",
            ] + list(
                {
                    k
                    for d in result
                    for k in d.keys()
                    if k
                    not in [
                        "NodeID",
                        "DisplayName",
                        "IPAddress",
                        "CompressedRowID",
                    ]
                }
            )
            writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(result)

            # Get the CSV content as a string
            csv_content = csv_buffer.getvalue()
            csv_buffer.close()

            # Save the results to a file
            self.create_file(f"{VENDOR}_latest_solarwinds_report.csv", csv_content)


class SolarWindsReport:

    def __init__(
        self, username: str, password: str, base_url: str, job: Job, vendor: str
    ):
        self.username = username
        self.password = password
        self.base_url = base_url
        self.job = job
        self.vendor = vendor

    def get_sw_nodes(self, query: str) -> Optional[List[Dict[str, Any]]]:
        try:
            response = requests.get(
                self.base_url,
                params={"query": query},
                auth=(self.username, self.password),
                verify=False,
                timeout=10,
            )
            response.raise_for_status()
            nodes = response.json().get("results", [])
            self.job.logger.info(f"Retrieved {len(nodes)} rows from SolarWinds")
            return nodes
        except requests.RequestException as e:
            self.job.logger.error(f"Request failed: {e}")
            return None

    def filter_table(
        self,
        data: List[Dict[str, Any]],
        headers: List[str],
    ) -> List[Dict[str, Any]]:
        filtered_data = [
            row
            for row in data
            if row["AssignmentName"] is not None
            and any(header in row["AssignmentName"] for header in headers)
        ]
        self.job.logger.info(f"Filtered {len(filtered_data)} rows")
        return filtered_data

    def pivot_table(self, data: List[Dict[str, Any]]):
        pivot_data = {}
        for row in data:
            key = (
                row["NodeID"],
                row["DisplayName"],
                row["IPAddress"],
                row["CompressedRowID"],
            )
            if key not in pivot_data:
                pivot_data[key] = {
                    "NodeID": row["NodeID"],
                    "DisplayName": row["DisplayName"],
                    "IPAddress": row["IPAddress"],
                    "CompressedRowID": row["CompressedRowID"],
                }
            assignment_name = row["AssignmentName"].split()[0]
            pivot_data[key][assignment_name] = row["Status"]

        pivot_list = list(pivot_data.values())
        self.job.logger.info(f"Pivoted {len(pivot_list)} rows")
        return pivot_list

    def diff_latest_and_previous(self):
        PERSISTENT_DIR = "/opt/nautobot/media/reports"
        latest = f"{self.vendor}_latest_solarwinds_report.csv"
        previous = f"{self.vendor}_previous_solarwinds_report.csv"
        # diff = DeepDiff(latest, previous, ignore_order=True)
        # self.job.logger.info(f"Diff: {diff}")
        # self.job.create_file(
        #     f"{self.vendor}_diff_solarwinds_report.json", json.dumps(diff)
        # )
        latest_file = os.path.join(PERSISTENT_DIR, latest)
        previous_file = os.path.join(PERSISTENT_DIR, previous)
        if os.path.exists(latest_file) and os.path.exists(previous_file):
            with open(latest_file, "r") as f:
                latest_data = list(csv.DictReader(f))
            with open(previous_file, "r") as f:
                previous_data = list(csv.DictReader(f))
            diff = DeepDiff(latest_data, previous_data, ignore_order=True)
            # self.job.logger.info(f"Diff: {diff}")
            self.job.create_file(
                f"{self.vendor}_diff_solarwinds_report.json", json.dumps(diff)
            )

    def save_report(self, data: List[Dict[str, Any]]):
        latest = f"{self.vendor}_latest_solarwinds_report.csv"
        previous = f"{self.vendor}_previous_solarwinds_report.csv"
        # Save the results to a file on the Nautobot server
        PERSISTENT_DIR = "/opt/nautobot/media/reports"
        os.makedirs(PERSISTENT_DIR, exist_ok=True)
        filepath = os.path.join(PERSISTENT_DIR, latest)
        # Rename the previous report to the current report
        if os.path.exists(filepath):
            os.rename(filepath, os.path.join(PERSISTENT_DIR, previous))
        with open(filepath, "w") as f:
            writer = csv.DictWriter(f, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
        self.job.logger.info(f"Report saved to {filepath}")


register_jobs(SolarWindsUNDPReport)
