"""
Check the state of the device backups and report any issues.
- Was the device reachable?
- Was the backup successful?
- Did it grab the full backup and not just a partial one?
"""

from datetime import datetime, timedelta
from django.utils import timezone
from nautobot.apps.jobs import Job, register_jobs, IntegerVar, BooleanVar
from nautobot_golden_config.models import GoldenConfig
from custom_jobs.modules.tools import apply_device_filters, DeviceFormEntry, ping_device
import os
from nautobot.extras.models.groups import DynamicGroup
from nautobot.core.utils.data import render_jinja2

name = "Custom Reporting"


class BackupStateChecker(Job, DeviceFormEntry):
    """Job to check the state of device backups and report any issues."""

    days_threshold = IntegerVar(
        description="Number of days to look back for backup issues",
        default=7,
        min_value=1,
        max_value=365,
        required=False,
    )

    generate_csv_report = BooleanVar(
        description="Generate a CSV report of backup states",
        default=True,
        required=False,
    )

    debug_completeness = BooleanVar(
        description="Enable debug logging for backup completeness checks",
        default=False,
        required=False,
    )

    class Meta:
        name = "Check Device Backup States"
        description = "Check the state of device backups and report any issues including connectivity, backup success, and backup completeness."
        has_sensitive_variables = False

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
        days_threshold=7,
        generate_csv_report=True,
        debug_completeness=False,
    ):
        """Check backup states for devices and report issues."""

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

        if not all_devices:
            self.logger.warning("No devices found matching the criteria.")
            return

        # Set the time threshold for recent backups
        threshold_date = timezone.now() - timedelta(days=days_threshold)

        # Initialize counters and report data
        total_devices = 0
        reachable_devices = 0
        unreachable_devices = 0
        successful_backups = 0
        failed_backups = 0
        partial_backups = 0
        no_backup_attempts = 0
        outdated_backups = 0

        report_data = []

        self.logger.info(f"Checking backup states for {len(all_devices)} devices...")
        self.logger.info(f"Looking for backups within the last {days_threshold} days")

        for device in all_devices:
            total_devices += 1
            device_report = self._check_device_backup_state(
                device, threshold_date, debug_completeness
            )

            # Update counters
            if device_report["reachable"]:
                reachable_devices += 1
            else:
                unreachable_devices += 1

            if device_report["backup_status"] == "successful":
                successful_backups += 1
            elif device_report["backup_status"] == "failed":
                failed_backups += 1
            elif device_report["backup_status"] == "partial":
                partial_backups += 1
            elif device_report["backup_status"] == "no_attempt":
                no_backup_attempts += 1
            elif device_report["backup_status"] == "outdated":
                outdated_backups += 1

            report_data.append(device_report)

            # Log issues
            if not device_report["reachable"]:
                self.logger.error(
                    f"Device {device.name} is not reachable", extra={"object": device}
                )

            if device_report["backup_status"] in ["failed", "no_attempt", "outdated"]:
                self.logger.error(
                    f"Device {device.name} backup issue: {device_report['backup_status']} - {device_report['issue_description']}",
                    extra={"object": device},
                )
            elif device_report["backup_status"] == "partial":
                self.logger.warning(
                    f"Device {device.name} has a partial backup: {device_report['issue_description']}",
                    extra={"object": device},
                )
            else:
                self.logger.debug(
                    f"Device {device.name} backup is healthy", extra={"object": device}
                )

        # Generate summary report
        self._generate_summary_report(
            total_devices,
            reachable_devices,
            unreachable_devices,
            successful_backups,
            failed_backups,
            partial_backups,
            no_backup_attempts,
            outdated_backups,
            days_threshold,
        )

        # Generate CSV report if requested
        if generate_csv_report:
            self._generate_csv_report(report_data)

    def _check_device_backup_state(
        self, device, threshold_date, debug_completeness=False
    ):
        """Check the backup state of a single device."""
        device_report = {
            "device_name": device.name,
            "location": str(device.location) if device.location else "N/A",
            "platform": str(device.platform) if device.platform else "N/A",
            "primary_ip": str(device.primary_ip4.host) if device.primary_ip4 else "N/A",
            "reachable": False,
            "backup_status": "unknown",
            "last_attempt": "Never",
            "last_success": "Never",
            "backup_size": 0,
            "backup_file_exists": False,
            "issue_description": "",
        }

        # Check device reachability
        if device.primary_ip4:
            try:
                device_report["reachable"] = ping_device(device.primary_ip4.host)
            except Exception as e:
                self.logger.debug(f"Ping test failed for {device.name}: {e}")
                device_report["reachable"] = False

        # Also check the can_connect custom field if it exists
        if hasattr(device, "cf") and device.cf.get("can_connect") is not None:
            device_report["reachable"] = device.cf.get("can_connect", False)

        # Check backup status from GoldenConfig
        try:
            golden_config = GoldenConfig.objects.get(device=device)

            # Check last attempt date
            if golden_config.backup_last_attempt_date:
                device_report["last_attempt"] = (
                    golden_config.backup_last_attempt_date.strftime("%Y-%m-%d %H:%M")
                )

            # Check last success date
            if golden_config.backup_last_success_date:
                device_report["last_success"] = (
                    golden_config.backup_last_success_date.strftime("%Y-%m-%d %H:%M")
                )

                # Determine backup status
                if golden_config.backup_last_success_date >= threshold_date:
                    # Recent successful backup
                    backup_content = golden_config.backup_config
                    if backup_content:
                        device_report["backup_size"] = len(backup_content)

                        # Check if backup seems complete (basic heuristics)
                        completeness_result = self._is_backup_complete(
                            backup_content,
                            device.platform.network_driver if device.platform else "",
                        )

                        if debug_completeness:
                            self.logger.debug(
                                f"Device {device.name}: Backup completeness check - "
                                f"Size: {len(backup_content)} chars, "
                                f"Lines: {len(backup_content.split(chr(10)))}, "
                                f"Platform: {device.platform.network_driver if device.platform else 'unknown'}, "
                                f"Complete: {completeness_result}",
                                extra={"object": device},
                            )

                        if completeness_result:
                            device_report["backup_status"] = "successful"
                        else:
                            device_report["backup_status"] = "partial"
                            # Get more detailed reason for partial backup
                            partial_reason = self._get_partial_backup_reason(
                                backup_content,
                                (
                                    device.platform.network_driver
                                    if device.platform
                                    else ""
                                ),
                            )
                            device_report["issue_description"] = (
                                f"Backup appears incomplete: {partial_reason} (size: {device_report['backup_size']} chars)"
                            )

                            if debug_completeness:
                                self.logger.debug(
                                    f"Device {device.name}: Partial backup reason: {partial_reason}",
                                    extra={"object": device},
                                )
                    else:
                        device_report["backup_status"] = "failed"
                        device_report["issue_description"] = (
                            "Backup timestamp exists but no content stored"
                        )
                else:
                    device_report["backup_status"] = "outdated"
                    device_report["issue_description"] = (
                        f"Last successful backup was {golden_config.backup_last_success_date.strftime('%Y-%m-%d')}"
                    )
            else:
                # No successful backup
                if golden_config.backup_last_attempt_date:
                    if golden_config.backup_last_attempt_date >= threshold_date:
                        device_report["backup_status"] = "failed"
                        device_report["issue_description"] = (
                            "Recent backup attempts failed"
                        )
                    else:
                        device_report["backup_status"] = "outdated"
                        device_report["issue_description"] = (
                            f"Last backup attempt was {golden_config.backup_last_attempt_date.strftime('%Y-%m-%d')}"
                        )
                else:
                    device_report["backup_status"] = "no_attempt"
                    device_report["issue_description"] = "No backup attempts recorded"

            # Check if backup file exists on disk
            device_report["backup_file_exists"] = self._check_backup_file_exists(device)

        except GoldenConfig.DoesNotExist:
            device_report["backup_status"] = "no_attempt"
            device_report["issue_description"] = (
                "No GoldenConfig record found for device"
            )

        return device_report

    def _is_backup_complete(self, backup_content, platform):
        """Basic heuristics to determine if a backup appears complete."""
        if not backup_content:
            return False

        # Minimum size check (adjust as needed)
        if len(backup_content) < 100:
            return False

        # Platform-specific completeness checks with more comprehensive requirements
        platform_checks = {
            "cisco_ios": {
                "required": ["version", "end"],  # Must have these
                "optional": [
                    "interface",
                    "hostname",
                    "ip route",
                    "access-list",
                ],  # Should have some of these
                "min_lines": 20,
                "typical_size": 1000,
            },
            "cisco_xr": {
                "required": ["version", "commit"],
                "optional": ["interface", "hostname", "router", "prefix-set"],
                "min_lines": 15,
                "typical_size": 800,
            },
            "cisco_nxos": {
                "required": ["version", "end"],
                "optional": ["interface", "hostname", "ip route", "vlan"],
                "min_lines": 20,
                "typical_size": 1000,
            },
            "cisco_xe": {
                "required": ["version", "end"],
                "optional": ["interface", "hostname", "ip route", "access-list"],
                "min_lines": 20,
                "typical_size": 1000,
            },
            "mikrotik_routeros": {
                "required": ["# software id =", "# model ="],
                "optional": [
                    "/interface",
                    "/ip address",
                    "/routing",
                    "/system identity",
                ],
                "min_lines": 10,
                "typical_size": 500,
            },
            "fiberstore_fsos": {
                "required": ["version"],
                "optional": ["interface", "hostname", "ip route"],
                "min_lines": 10,
                "typical_size": 500,
            },
            "keymile_nos": {
                "required": ["version"],
                "optional": ["interface", "hostname", "ip route"],
                "min_lines": 10,
                "typical_size": 500,
            },
        }

        # Default checks for unknown platforms
        default_checks = {
            "required": ["version"],
            "optional": ["interface", "hostname"],
            "min_lines": 5,
            "typical_size": 200,
        }

        checks = platform_checks.get(platform, default_checks)
        backup_lower = backup_content.lower()
        backup_lines = backup_content.split("\n")

        # Check line count
        if len(backup_lines) < checks["min_lines"]:
            return False

        # Check size relative to typical expectations
        if (
            len(backup_content) < checks["typical_size"] * 0.3
        ):  # Less than 30% of typical size
            return False

        # Check required keywords - ALL must be present
        required_found = sum(
            1 for keyword in checks["required"] if keyword.lower() in backup_lower
        )
        if required_found < len(checks["required"]):
            return False

        # Check optional keywords - at least half should be present
        optional_found = sum(
            1 for keyword in checks["optional"] if keyword.lower() in backup_lower
        )
        min_optional_required = max(1, len(checks["optional"]) // 2)
        if optional_found < min_optional_required:
            return False

        # Additional heuristics for common incomplete backup patterns
        # Check for common error messages or truncation indicators
        error_indicators = [
            "connection timed out",
            "connection lost",
            "command failed",
            "error:",
            "% invalid",
            "% incomplete",
            "more--",  # Paging indicator
            "press any key",
            "% ambiguous command",
        ]

        for indicator in error_indicators:
            if indicator.lower() in backup_lower:
                return False

        # Check if backup ends abruptly (platform-specific end markers)
        end_markers = {
            "cisco_ios": ["end"],
            "cisco_xe": ["end"],
            "cisco_nxos": ["end"],
            "cisco_xr": ["commit", "end"],
            "mikrotik_routeros": ["# configuration", "/system identity set"],
        }

        expected_endings = end_markers.get(platform, [])
        if expected_endings:
            # Check if backup ends with one of the expected markers
            last_lines = " ".join(backup_lines[-5:]).lower()  # Check last 5 lines
            has_proper_ending = any(
                marker.lower() in last_lines for marker in expected_endings
            )
            if not has_proper_ending:
                return False

        return True

    def _get_partial_backup_reason(self, backup_content, platform):
        """Determine the specific reason why a backup is considered partial."""
        if not backup_content:
            return "No content"

        backup_lower = backup_content.lower()
        backup_lines = backup_content.split("\n")

        # Get platform expectations
        platform_checks = {
            "cisco_ios": {
                "required": ["version", "end"],
                "optional": ["interface", "hostname", "ip route", "access-list"],
                "min_lines": 20,
                "typical_size": 1000,
                "end_markers": ["end"],
            },
            "cisco_xr": {
                "required": ["version", "commit"],
                "optional": ["interface", "hostname", "router", "prefix-set"],
                "min_lines": 15,
                "typical_size": 800,
                "end_markers": ["commit", "end"],
            },
            "cisco_nxos": {
                "required": ["version", "end"],
                "optional": ["interface", "hostname", "ip route", "vlan"],
                "min_lines": 20,
                "typical_size": 1000,
                "end_markers": ["end"],
            },
            "cisco_xe": {
                "required": ["version", "end"],
                "optional": ["interface", "hostname", "ip route", "access-list"],
                "min_lines": 20,
                "typical_size": 1000,
                "end_markers": ["end"],
            },
            "mikrotik_routeros": {
                "required": ["# software id =", "# model ="],
                "optional": [
                    "/interface",
                    "/ip address",
                    "/routing",
                    "/system identity",
                ],
                "min_lines": 10,
                "typical_size": 500,
                "end_markers": ["# configuration", "/system identity set"],
            },
        }

        default_checks = {
            "required": ["version"],
            "optional": ["interface", "hostname"],
            "min_lines": 5,
            "typical_size": 200,
            "end_markers": [],
        }

        checks = platform_checks.get(platform, default_checks)
        reasons = []

        # Check size
        if len(backup_content) < checks["typical_size"] * 0.3:
            reasons.append(
                f"content too small ({len(backup_content)} < {int(checks['typical_size'] * 0.3)} expected)"
            )

        # Check line count
        if len(backup_lines) < checks["min_lines"]:
            reasons.append(
                f"too few lines ({len(backup_lines)} < {checks['min_lines']} expected)"
            )

        # Check required keywords
        missing_required = []
        for keyword in checks["required"]:
            if keyword.lower() not in backup_lower:
                missing_required.append(keyword)
        if missing_required:
            reasons.append(f"missing required keywords: {', '.join(missing_required)}")

        # Check optional keywords
        optional_found = sum(
            1 for keyword in checks["optional"] if keyword.lower() in backup_lower
        )
        min_optional_required = max(1, len(checks["optional"]) // 2)
        if optional_found < min_optional_required:
            missing_optional = [
                kw for kw in checks["optional"] if kw.lower() not in backup_lower
            ]
            reasons.append(
                f"insufficient optional content (found {optional_found}/{len(checks['optional'])})"
            )

        # Check for error indicators
        error_indicators = [
            "connection timed out",
            "connection lost",
            "command failed",
            "error:",
            "% invalid",
            "% incomplete",
            "more--",
            "press any key",
            "% ambiguous command",
        ]
        found_errors = [err for err in error_indicators if err.lower() in backup_lower]
        if found_errors:
            reasons.append(
                f"error indicators found: {', '.join(found_errors[:2])}"
            )  # Limit to first 2

        # Check for proper ending
        if checks["end_markers"]:
            last_lines = " ".join(backup_lines[-5:]).lower()
            has_proper_ending = any(
                marker.lower() in last_lines for marker in checks["end_markers"]
            )
            if not has_proper_ending:
                reasons.append("missing proper ending markers")

        # Check for abrupt truncation patterns
        last_line = backup_lines[-1].strip() if backup_lines else ""
        if last_line and not last_line.endswith(("end", "!", "}", ">", "#")):
            reasons.append("appears truncated")

        return "; ".join(reasons) if reasons else "unknown reason"

    def _check_backup_file_exists(self, device):
        """Check if the backup file exists on disk."""
        try:
            # Get backup directory and file path
            backup_dynamic_groups = DynamicGroup.objects.exclude(
                golden_config_setting__isnull=True
            )
            if not backup_dynamic_groups.exists():
                return False

            backup_dynamic_group = backup_dynamic_groups.first()
            backup_directory = (
                backup_dynamic_group.golden_config_setting.backup_repository.filesystem_path
            )
            backup_path_template_obj = render_jinja2(
                template_code=backup_dynamic_group.golden_config_setting.backup_path_template,
                context={"obj": device},
            )
            backup_file = os.path.join(backup_directory, backup_path_template_obj)

            return os.path.exists(backup_file)
        except Exception as e:
            self.logger.debug(
                f"Could not check backup file existence for {device.name}: {e}"
            )
            return False

    def _generate_summary_report(
        self,
        total_devices,
        reachable_devices,
        unreachable_devices,
        successful_backups,
        failed_backups,
        partial_backups,
        no_backup_attempts,
        outdated_backups,
        days_threshold,
    ):
        """Generate a summary report of backup states."""

        self.logger.info("=" * 60)
        self.logger.info("BACKUP STATE SUMMARY REPORT")
        self.logger.info("=" * 60)

        self.logger.info(f"Total devices checked: {total_devices}")
        self.logger.info(f"Threshold period: {days_threshold} days")
        self.logger.info("")

        self.logger.info("CONNECTIVITY STATUS:")
        self.logger.info(
            f"  Reachable devices: {reachable_devices} ({reachable_devices/total_devices*100:.1f}%)"
        )
        self.logger.info(
            f"  Unreachable devices: {unreachable_devices} ({unreachable_devices/total_devices*100:.1f}%)"
        )
        self.logger.info("")

        self.logger.info("BACKUP STATUS:")
        self.logger.info(
            f"  Successful backups: {successful_backups} ({successful_backups/total_devices*100:.1f}%)"
        )
        self.logger.info(
            f"  Failed backups: {failed_backups} ({failed_backups/total_devices*100:.1f}%)"
        )
        self.logger.info(
            f"  Partial backups: {partial_backups} ({partial_backups/total_devices*100:.1f}%)"
        )
        self.logger.info(
            f"  No backup attempts: {no_backup_attempts} ({no_backup_attempts/total_devices*100:.1f}%)"
        )
        self.logger.info(
            f"  Outdated backups: {outdated_backups} ({outdated_backups/total_devices*100:.1f}%)"
        )
        self.logger.info("")

        # Calculate health score
        healthy_devices = successful_backups
        health_score = (
            (healthy_devices / total_devices * 100) if total_devices > 0 else 0
        )

        self.logger.info(f"OVERALL BACKUP HEALTH SCORE: {health_score:.1f}%")

        if health_score < 70:
            self.logger.error(
                "Backup health score is below 70% - immediate attention required!"
            )
        elif health_score < 90:
            self.logger.warning(
                "Backup health score is below 90% - monitoring recommended"
            )
        else:
            self.logger.info("Backup health score is good")

        self.logger.info("=" * 60)

    def _generate_csv_report(self, report_data):
        """Generate a CSV report of all device backup states."""

        csv_content = "Device Name,Location,Platform,Primary IP,Reachable,Backup Status,Last Attempt,Last Success,Backup Size,File Exists,Issue Description\n"

        for device_data in report_data:
            csv_content += f"{device_data['device_name']},{device_data['location']},{device_data['platform']},"
            csv_content += f"{device_data['primary_ip']},{device_data['reachable']},{device_data['backup_status']},"
            csv_content += f"{device_data['last_attempt']},{device_data['last_success']},{device_data['backup_size']},"
            csv_content += f"{device_data['backup_file_exists']},\"{device_data['issue_description']}\"\n"

        # Create the file with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"backup_state_report_{timestamp}.csv"

        self.create_file(filename, csv_content)
        self.logger.info(f"CSV report generated: {filename}")


register_jobs(BackupStateChecker)
