"""Purpose: Run commands on devices then save each output to a file."""

import time
from deepdiff import DeepDiff
from netmiko import ConnectHandler

from nautobot.apps.jobs import Job, register_jobs, TextVar, BooleanVar, IntegerVar

from custom_jobs.modules.tools import get_device_connection_info
from custom_jobs.modules.tools import apply_device_filters
from custom_jobs.modules.tools import DeviceFormEntry

# from custom_jobs.modules.tools import parallel_execution

import logging
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import QueueHandler, QueueListener

# Set up a queue for logging
log_queue = queue.Queue()
queue_handler = QueueHandler(log_queue)
listener = QueueListener(log_queue, *logging.getLogger().handlers)
listener.start()

# Add the queue handler to the root logger
logging.getLogger().addHandler(queue_handler)


def parallel_execution(function, devices, max_workers=10, *args):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(function, device, *args) for device in devices]
        for future in as_completed(futures):
            future.result()


name = "Operations"

SUPPORTED_PLATFORMS = [
    "keymile_nos",
    "fiberstore_fsos",
    "mikrotik_routeros",
    "netonix_os",
    "cisco_ios",
    "cisco_xr",
    "cisco_xe",
    "cisco_nxos",
    "cisco_s300",
    "ubiquiti_airos",
    "arista_eos",
]


class CommandRunner(Job, DeviceFormEntry):

    commands = TextVar(
        description="List of commands to run, separated by newlines",
        required=True,
    )
    is_config = BooleanVar(
        description="Check if the commands are configuration commands",
        default=False,
        required=False,
    )
    re_run_after = IntegerVar(
        description="Re-run commands after the specified number of seconds, then compare the output",
        default=0,
        min_value=0,
        max_value=3600,
        required=False,
    )
    parallel_task = BooleanVar(
        description="Execute backup tasks in parallel",
        default=False,
        required=False,
    )
    max_workers = IntegerVar(
        description="Number of workers to use for parallel execution",
        default=20,
        min_value=1,
        max_value=20,
        required=False,
    )

    class Meta:
        name = "Commands Runner"
        description = "Run commands on devices and save each output to a file. Diff pre and post checks if re-run timer is specified."
        has_sensitive_variables = True
        soft_time_limit = 1800  # 30 minutes
        time_limit = 2400  # 40 minutes
        task_queues = [
            "default",
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
        commands=None,
        is_config=False,
        re_run_after=None,
        parallel_task=False,
        max_workers=None,
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

        def run_commands(device):
            try:
                if device.platform.network_driver not in SUPPORTED_PLATFORMS:
                    self.logger.info(
                        f"{device} Platform {device.platform.network_driver} is not supported. Skipping..."
                    )
                    return
                self.logger.info(f"{device} Processing device...")
                task = RunCommands(
                    job=self,
                    device=device,
                    commands=commands,
                    is_config=is_config,
                    re_run_after=re_run_after,
                )
                task.run()
            except Exception as e:
                self.logger.error(f"{device} Error processing device: {e}")

        if parallel_task:
            parallel_execution(run_commands, all_devices, max_workers=max_workers)
        else:
            for device in all_devices:
                run_commands(device)

        # Remove the queue handler after execution
        self.logger.removeHandler(queue_handler)

        # Remember to stop the listener when done
        listener.stop()


class RunCommands:

    def __init__(self, job, device, commands, is_config, re_run_after):
        self.job = job
        self.device = device
        self.commands = commands
        self.is_config = is_config
        self.re_run_after = re_run_after

    def diff_results(self, pre_check, post_check):
        diff = DeepDiff(pre_check, post_check, ignore_order=True)
        # {
        # "values_changed":{
        #     "root":{
        #         "new_value":" Port   vid     IpAddress          MAC Address       Lease(Sec)      Type\n------ ----- ------------------ ------------------- ------------ ------------\n 1      550   100.97.192.55      a8:fb:40:6f:61:a7   3118          snoop   \n 2      550   100.97.192.33      28:74:f5:eb:29:e5   3361          snoop   \n Total dhcp snoop binding entry: 2",
        #         "old_value":" Port   vid     IpAddress          MAC Address       Lease(Sec)      Type\n------ ----- ------------------ ------------------- ------------ ------------\n 1      550   100.97.192.55      a8:fb:40:6f:61:a7   3143          snoop   \n 2      550   100.97.192.33      28:74:f5:eb:29:e5   3386          snoop   \n Total dhcp snoop binding entry: 2",
        #         "diff":"--- \n+++ \n@@ -1,5 +1,5 @@\n  Port   vid     IpAddress          MAC Address       Lease(Sec)      Type\n ------ ----- ------------------ ------------------- ------------ ------------\n- 1      550   100.97.192.55      a8:fb:40:6f:61:a7   3143          snoop   \n- 2      550   100.97.192.33      28:74:f5:eb:29:e5   3386          snoop   \n+ 1      550   100.97.192.55      a8:fb:40:6f:61:a7   3118          snoop   \n+ 2      550   100.97.192.33      28:74:f5:eb:29:e5   3361          snoop   \n  Total dhcp snoop binding entry: 2"
        #     }
        # }
        # }
        if diff.get("values_changed"):
            return diff["values_changed"]["root"]["diff"]
        return False

    def get_device_session(self):
        device_info = get_device_connection_info(self.device)
        device_info["session_log"] = "netmiko.log"
        return ConnectHandler(**device_info)

    def send_commands(self, session, check_type):
        commands_output = ""
        if self.is_config:
            commands_list = [
                cmd.strip() for cmd in self.commands.split("\n") if cmd.strip()
            ]
            output = session.send_config_set(commands_list)
            commands_output += output
            self.job.logger.info(f"Config: {self.commands} Output: {output}")
        else:
            for command in self.commands.split("\n"):
                command = command.strip()
                output = session.send_command(command)
                self.save_command_output(command, output, check_type)
                self.job.logger.info(f"Command: {command} Output: {output}")
                commands_output += output
        return commands_output

    def save_command_output(self, command, output, check_type):
        if self.re_run_after > 0:
            suffix = f"-{check_type}-check.txt"
        else:
            suffix = ".txt"
        filename = f"{self.device.name}-{command.replace(' ', '_')}{suffix}"
        self.job.create_file(filename, output)

    def save_all_commands_output(self, commands_output, check_type):
        if self.re_run_after > 0:
            suffix = f"-all-commands-{check_type}-check.txt"
        else:
            suffix = "-all-commands.txt"
        filename = f"{self.device.name}{suffix}"
        self.job.create_file(filename, commands_output)

    def run_all_commands(self, check_type):
        try:
            with self.get_device_session() as session:
                session.enable()
                if self.device.platform.network_driver in [
                    "fiberstore_fsos",
                    "netonix_os",
                ]:
                    session.send_command_timing("terminal length 0")
                commands_output = self.send_commands(session, check_type)
                if commands_output:
                    self.save_all_commands_output(commands_output, check_type)
                return commands_output
        except Exception as e:
            self.job.logger.error(f"Error on device {self.device}: {e}")
            return False

    def run(self):
        pre_check = self.run_all_commands("pre")
        if self.re_run_after > 0:
            self.job.logger.info(
                f"Re-running commands after {self.re_run_after} seconds"
            )
            time.sleep(self.re_run_after)
            post_check = self.run_all_commands("post")
            diff = self.diff_results(pre_check, post_check)
            if diff:
                self.job.logger.info("Differences found between pre and post checks")
                self.job.logger.info(diff)
                self.job.create_file(f"{self.device.name}-all-commands-diff.txt", diff)
            else:
                self.job.logger.info("No differences found between pre and post checks")


register_jobs(CommandRunner)
