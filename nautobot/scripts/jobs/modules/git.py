from datetime import datetime

from django.utils.timezone import make_aware

# from nautobot.core.celery import register_jobs
# from nautobot.dcim.models import (
#     Device,
#     DeviceType,
#     Location,
#     Manufacturer,
#     Platform,
#     Rack,
#     RackGroup,
# )
from nautobot.extras.datasources.git import (
    ensure_git_repository,
    get_repo_from_url_to_path_and_from_branch,
)

# from nautobot.extras.jobs import (
#     BooleanVar,
#     ChoiceVar,
#     Job,
#     JobButtonReceiver,
#     MultiObjectVar,
#     ObjectVar,
#     StringVar,
#     TextVar,
# )
from nautobot.extras.models import DynamicGroup

# from nautobot.tenancy.models import Tenant, TenantGroup
# from nautobot_plugin_nornir.plugins.inventory.nautobot_orm import NautobotORMInventory
# from nornir.core.plugins.inventory import InventoryPluginRegister
from nornir_nautobot.exceptions import NornirNautobotException

# from nautobot_golden_config.choices import ConfigPlanTypeChoice
# from nautobot_golden_config.exceptions import (
#     BackupFailure,
#     ComplianceFailure,
#     IntendedGenerationFailure,
# )

# from nautobot_golden_config.models import ComplianceFeature, ConfigPlan, GoldenConfig
# from nautobot_golden_config.nornir_plays.config_backup import config_backup
# from nautobot_golden_config.nornir_plays.config_compliance import config_compliance
# from nautobot_golden_config.nornir_plays.config_deployment import config_deployment
# from nautobot_golden_config.nornir_plays.config_intended import config_intended
from nautobot_golden_config.utilities import constant

# from nautobot_golden_config.utilities.config_plan import (
#     config_plan_default_status,
#     generate_config_set_from_compliance_feature,
#     generate_config_set_from_manual,
# )
from nautobot_golden_config.utilities.git import GitRepo
from nautobot_golden_config.utilities.helper import (
    get_device_to_settings_map,
    get_job_filter,
    # update_dynamic_groups_cache,
)


def get_repo_types_for_job(job_name):
    """Logic to determine which repo_types are needed based on job + plugin settings."""
    repo_types = []
    if "Backup" in job_name and constant.ENABLE_BACKUP:
        repo_types.extend(["backup_repository"])
    if "Intended" in job_name and constant.ENABLE_INTENDED:
        repo_types.extend(["jinja_repository", "intended_repository"])
    if "Compliance" in job_name and constant.ENABLE_COMPLIANCE:
        repo_types.extend(["intended_repository", "backup_repository"])
    if "All" in job_name:
        repo_types.extend(
            ["backup_repository", "jinja_repository", "intended_repository"]
        )
    return list(set(repo_types))


def get_refreshed_repos(job_obj, repo_types, data=None):
    """Small wrapper to pull latest branch, and return a GitRepo app specific object."""
    dynamic_groups = DynamicGroup.objects.exclude(golden_config_setting__isnull=True)
    repository_records = set()
    for group in dynamic_groups:
        # Make sure the data(device qs) device exist in the dg first.
        if data.filter(group.generate_query()).exists():
            for repo_type in repo_types:
                repo = getattr(group.golden_config_setting, repo_type, None)
                if repo:
                    repository_records.add(repo)

    repositories = {}
    for repository_record in repository_records:
        ensure_git_repository(repository_record, job_obj.logger)
        # TODO: Should this not point to non-nautobot.core import
        # We should ask in nautobot core for the `from_url` constructor to be it's own function
        git_info = get_repo_from_url_to_path_and_from_branch(repository_record)
        git_repo = GitRepo(
            repository_record.filesystem_path,
            git_info.from_url,
            clone_initially=False,
            base_url=repository_record.remote_url,
            nautobot_repo_obj=repository_record,
        )
        commit = True

        # if constant.ENABLE_INTENDED in git_repo.nautobot_repo_obj.provided_contents:
        #     commit = True
        # if constant.ENABLE_BACKUP in git_repo.nautobot_repo_obj.provided_contents:
        #     commit = True
        repositories[str(git_repo.nautobot_repo_obj.id)] = {
            "repo_obj": git_repo,
            "to_commit": commit,
        }
        job_obj.logger.debug(repositories)
    return repositories


def gc_repo_prep(job, data):
    """Prepare Golden Config git repos for work.

    Args:
        job (Job): Nautobot Job object with logger and other vars.
        data (dict): Data being passed from Job.

    Returns:
        List[GitRepo]: List of GitRepos to be used with Job(s).
    """
    job.logger.debug(
        "Compiling device data for GC job.", extra={"grouping": "Get Job Filter"}
    )
    job.qs = get_job_filter(data)
    job.logger.debug(
        f"In scope device count for this job: {job.qs.count()}",
        extra={"grouping": "Get Job Filter"},
    )
    job.logger.debug(
        "Mapping device(s) to GC Settings.",
        extra={"grouping": "Device to Settings Map"},
    )
    job.device_to_settings_map = get_device_to_settings_map(queryset=job.qs)
    gitrepo_types = list(set(get_repo_types_for_job(job.class_path)))
    job.logger.debug(
        f"Repository types to sync: {', '.join(sorted(gitrepo_types))}",
        extra={"grouping": "GC Repo Syncs"},
    )
    current_repos = get_refreshed_repos(
        job_obj=job, repo_types=gitrepo_types, data=job.qs
    )
    return current_repos


def gc_repo_push(job, current_repos, commit_message=""):
    """Push any work from worker to git repos in Job.

    Args:
        job (Job): Nautobot Job with logger and other attributes.
        current_repos (List[GitRepo]): List of GitRepos to be used with Job(s).
    """
    now = make_aware(datetime.now())
    job.logger.debug(
        f"Finished the {job.Meta.name} job execution.",
        extra={"grouping": "GC After Run"},
    )
    if current_repos:
        for _, repo in current_repos.items():
            if repo["to_commit"]:
                job.logger.debug(
                    f"Pushing {job.Meta.name} results to repo {repo['repo_obj'].base_url}.",
                    extra={"grouping": "GC Repo Commit and Push"},
                )
                if not commit_message:
                    commit_message = f"{job.Meta.name.upper()} JOB {now}"
                repo["repo_obj"].commit_with_added(commit_message)
                repo["repo_obj"].push()
                job.logger.info(
                    f'{repo["repo_obj"].nautobot_repo_obj.name}: the new Git repository hash is "{repo["repo_obj"].head}"',
                    extra={
                        "grouping": "GC Repo Commit and Push",
                        "object": repo["repo_obj"].nautobot_repo_obj,
                    },
                )


def gc_repos(func):
    """Decorator used for handle repo syncing, commiting, and pushing."""

    def gc_repo_wrapper(self, *args, **kwargs):
        """Decorator used for handle repo syncing, commiting, and pushing."""
        current_repos = gc_repo_prep(job=self, data=kwargs)
        # This is where the specific jobs run method runs via this decorator.
        try:
            func(self, *args, **kwargs)
        except Exception as error:  # pylint: disable=broad-exception-caught
            error_msg = f"`E3001:` General Exception handler, original error message ```{error}```"
            # Raise error only if the job kwarg (checkbox) is selected to do so on the job execution form.
            if kwargs.get("fail_job_on_task_failure"):
                raise NornirNautobotException(error_msg) from error
        finally:
            gc_repo_push(
                job=self,
                current_repos=current_repos,
                commit_message=kwargs.get("commit_message"),
            )

    return gc_repo_wrapper
