"""
Purpose: Generate Oxidized inventory from Nautobot devices and restart the service.

This job will:
1. Query all active devices from Nautobot
2. Generate an Oxidized-compatible inventory in CSV format
3. Write the inventory to /home/oxidized/.config/oxidized/router.db
4. Restart the oxidized systemd service
"""

import csv
import os
import subprocess
from io import StringIO

from nautobot.apps.jobs import Job, register_jobs, BooleanVar
from nautobot.dcim.models import Device
from custom_jobs.modules.tools import get_device_connection_info

name = "Custom Operations"


class OxidizedInventoryGenerator(Job):
    restart_service = BooleanVar(
        description="Restart the oxidized systemd service after updating inventory",
        default=True,
    )
    
    dry_run = BooleanVar(
        description="Perform a dry run without writing to file or restarting service",
        default=False,
    )

    class Meta:
        name = "Generate Oxidized Inventory"
        description = "Generate Oxidized router.db inventory from Nautobot devices and restart the service"
        has_sensitive_variables = False
        soft_time_limit = 300  # 5 minutes
        time_limit = 600  # 10 minutes

    def run(self, restart_service=True, dry_run=False):
        """
        Main execution method for the job.
        
        Args:
            restart_service: Whether to restart the oxidized service
            dry_run: If True, only log what would be done without making changes
        """
        try:
            # Query active devices from Nautobot
            self.logger.info("Querying devices from Nautobot...")
            devices = self.get_devices()
            self.logger.info(f"Found {len(devices)} active devices")

            # Generate the inventory
            inventory_content = self.generate_inventory(devices)
            
            if dry_run:
                self.logger.info("DRY RUN MODE - No changes will be made")
                self.logger.info("Generated inventory preview:")
                # Show first 20 lines as preview
                all_lines = inventory_content.split('\n')
                preview_lines = all_lines[:20]
                for line in preview_lines:
                    self.logger.info(f"  {line}")
                if len(all_lines) > 20:
                    remaining = len(all_lines) - 20
                    self.logger.info(f"  ... ({remaining} more lines)")
                return

            # Write to file
            output_file = "/home/oxidized/.config/oxidized/router.db"
            self.write_inventory(inventory_content, output_file)
            self.logger.info(f"Successfully wrote inventory to {output_file}")

            # Restart the service if requested
            if restart_service:
                self.restart_oxidized_service()
                self.logger.info("Successfully restarted oxidized service")
            else:
                self.logger.info("Skipped restarting oxidized service")

            self.logger.info("Job completed successfully")

        except Exception as e:
            self.logger.error(f"Job failed with error: {str(e)}")
            raise

    def get_devices(self):
        """
        Query active devices from Nautobot that should be included in Oxidized inventory.
        
        Returns:
            QuerySet of Device objects
        """
        # Get all devices with Active status that have a primary IP, platform, and secrets group
        devices = Device.objects.filter(
            status__name="Active",
            primary_ip4__isnull=False,
            platform__isnull=False,
            secrets_group__isnull=False,
        ).select_related('platform', 'primary_ip4', 'location', 'role', 'secrets_group').order_by('name')
        
        return devices

    def generate_inventory(self, devices):
        """
        Generate Oxidized inventory in colon-separated format.
        
        Oxidized router.db format:
        ip:name:model:username:password:enable_password
        
        For Nautobot integration, we'll use:
        - ip: device.primary_ip4.address (host part)
        - name: device.name
        - model: device.platform.network_driver (mapped to Oxidized model)
        - username/password/enable: Extracted from device secrets_group
        
        Args:
            devices: QuerySet of Device objects
            
        Returns:
            String containing colon-separated inventory
        """
        lines = []
        
        for device in devices:
            try:
                # Get device details
                name = device.name
                ip = str(device.primary_ip4.host)
                
                # Map Nautobot platform to Oxidized model
                model = self.map_platform_to_oxidized_model(device.platform.network_driver)
                
                # Get credentials from the device using the helper function
                try:
                    device_info = get_device_connection_info(device)
                    username = device_info.get('username', '')
                    password = device_info.get('password', '')
                    enable_password = device_info.get('secret', password)  # Use secret if available, else password
                except Exception as e:
                    self.logger.warning(f"Error getting credentials for device {device.name}: {str(e)}")
                    continue
                
                # Format: ip:name:model:username:password:enable_password
                line = f"{ip}:{name}:{model}:{username}:{password}:{enable_password}"
                lines.append(line)
                
                self.logger.debug(f"Added device {name} ({ip}) with model {model}")
                
            except Exception as e:
                self.logger.warning(f"Error processing device {device.name}: {str(e)}")
                continue
        
        return '\n'.join(lines) + '\n' if lines else ''

    def map_platform_to_oxidized_model(self, network_driver):
        """
        Map Nautobot platform network_driver to Oxidized model name.
        
        Args:
            network_driver: String representing the Netmiko driver name
            
        Returns:
            String representing the Oxidized model name
        """
        # Mapping of common network drivers to Oxidized models
        driver_mapping = {
            'cisco_ios': 'ios',
            'cisco_xe': 'ios',
            'cisco_xr': 'iosxr',
            'cisco_nxos': 'nxos',
            'cisco_asa': 'asa',
            'cisco_s300': 'ios',
            'juniper_junos': 'junos',
            'arista_eos': 'eos',
            'mikrotik_routeros': 'routeros',
            'ubiquiti_edge': 'edgeswitch',
            'ubiquiti_edgeswitch': 'edgeswitch',
            'ubiquiti_airos': 'airos',
            'fortinet': 'fortios',
            'paloalto_panos': 'panos',
            'dell_force10': 'force10',
            'hp_comware': 'comware',
            'hp_procurve': 'procurve',
            'fiberstore_fsos': 'fsos',
            'keymile_nos': 'ios',  # Fallback to generic IOS-like
            'netonix_os': 'edgeswitch',  # Similar to EdgeSwitch
            'ceragon_os': 'ios',  # Fallback
            'siklu_os': 'ios',  # Fallback
            'cambium_cnmatrix': 'ios',  # Fallback
        }
        
        # Return mapped model or the original driver name as fallback
        model = driver_mapping.get(network_driver.lower(), network_driver)
        
        if model == network_driver:
            self.logger.debug(f"No mapping found for {network_driver}, using as-is")
        
        return model

    def write_inventory(self, content, output_file):
        """
        Write inventory content to the Oxidized router.db file.
        
        Args:
            content: String containing the inventory in CSV format
            output_file: Path to the output file
        """
        try:
            # Ensure the directory exists
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            
            # Write the file
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(content)
            
            # Set appropriate permissions (readable by oxidized user)
            # chmod 644 = rw-r--r--
            os.chmod(output_file, 0o644)
            
            self.logger.info(f"Wrote {len(content.splitlines())} lines to {output_file}")
            
        except PermissionError as e:
            self.logger.error(f"Permission denied writing to {output_file}: {str(e)}")
            self.logger.error("Ensure the Nautobot process has write access to /home/oxidized/.config/oxidized/")
            raise
        except Exception as e:
            self.logger.error(f"Error writing inventory file: {str(e)}")
            raise

    def restart_oxidized_service(self):
        """
        Restart the oxidized systemd service.
        """
        try:
            self.logger.info("Restarting oxidized systemd service...")
            
            # Use systemctl to restart the service
            result = subprocess.run(
                ['sudo', 'systemctl', 'restart', 'oxidized'],
                capture_output=True,
                text=True,
                timeout=30,
                check=True
            )
            
            self.logger.info("Oxidized service restarted successfully")
            
            # Check the status to confirm it's running
            status_result = subprocess.run(
                ['sudo', 'systemctl', 'is-active', 'oxidized'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if status_result.stdout.strip() == 'active':
                self.logger.info("Confirmed: oxidized service is active")
            else:
                self.logger.warning(f"Oxidized service status: {status_result.stdout.strip()}")
            
        except subprocess.TimeoutExpired:
            self.logger.error("Timeout while restarting oxidized service")
            raise
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error restarting oxidized service: {e.stderr}")
            self.logger.error("Ensure the Nautobot user has sudo permissions for 'systemctl restart oxidized'")
            self.logger.error("Add to /etc/sudoers.d/nautobot: 'nautobot ALL=(ALL) NOPASSWD: /bin/systemctl restart oxidized, /bin/systemctl is-active oxidized'")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error restarting service: {str(e)}")
            raise


register_jobs(OxidizedInventoryGenerator)
