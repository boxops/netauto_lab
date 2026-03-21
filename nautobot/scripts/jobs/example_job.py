"""
example_job.py – Starter Nautobot Job for netauto_lab.

Place Job files in nautobot/scripts/jobs/ for instant auto-discovery
(they are bind-mounted into the container at JOBS_ROOT=/opt/nautobot/scripts/jobs).

After adding or editing a file here run:  make refresh-jobs
"""

from nautobot.apps.jobs import Job, register_jobs


class HelloNetworkJob(Job):
    """A minimal example Job that logs a greeting. Replace with real logic."""

    class Meta:
        name = "Hello Network"
        description = "Example Job – verifies the Jobs framework is working."
        has_sensitive_variables = False

    def run(self):
        self.logger.info("Hello from netauto_lab Jobs! Replace this with real automation.")


register_jobs(HelloNetworkJob)
