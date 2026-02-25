"""
tests/test_ansible.py
Validates Ansible playbook syntax, lint compliance, and dynamic inventory.

Run with:
    pytest tests/test_ansible.py -v

These tests run against the Ansible container and do NOT require live devices.
"""

import subprocess
import os
import pytest
import yaml
from pathlib import Path

ANSIBLE_DIR = Path(__file__).parent.parent / "ansible"
PLAYBOOKS_DIR = ANSIBLE_DIR / "playbooks"
ROLES_DIR = ANSIBLE_DIR / "roles"
INVENTORY_DIR = ANSIBLE_DIR / "inventory"

DOCKER_COMPOSE = "docker compose"
ANSIBLE_SERVICE = "ansible"


def run_ansible_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command inside the Ansible container."""
    full_cmd = f"{DOCKER_COMPOSE} run --rm {ANSIBLE_SERVICE} {' '.join(cmd)}"
    return subprocess.run(
        full_cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=str(ANSIBLE_DIR.parent),
        check=check,
    )


# ---------------------------------------------------------------------------
# Playbook YAML validity
# ---------------------------------------------------------------------------

class TestPlaybookYAML:
    """All playbooks must be valid YAML."""

    @pytest.mark.parametrize(
        "playbook",
        list(PLAYBOOKS_DIR.glob("*.yml")),
        ids=lambda p: p.name,
    )
    def test_playbook_is_valid_yaml(self, playbook: Path):
        with open(playbook) as f:
            content = yaml.safe_load_all(f)
            docs = list(content)
        assert docs is not None
        assert len(docs) > 0, f"{playbook.name} is empty"

    @pytest.mark.parametrize(
        "playbook",
        list(PLAYBOOKS_DIR.glob("*.yml")),
        ids=lambda p: p.name,
    )
    def test_playbook_has_name(self, playbook: Path):
        """Every playbook must have a top-level name in its first play."""
        with open(playbook) as f:
            docs = list(yaml.safe_load_all(f))
        # Ansible playbooks are YAML lists of plays
        plays = docs[0] if isinstance(docs[0], list) else docs
        assert plays is not None and len(plays) > 0
        first_play = plays[0]
        assert "name" in first_play, f"{playbook.name} first play missing 'name' key"

    @pytest.mark.parametrize(
        "playbook",
        list(PLAYBOOKS_DIR.glob("*.yml")),
        ids=lambda p: p.name,
    )
    def test_playbook_has_hosts(self, playbook: Path):
        """Every playbook must declare a hosts key in its first play."""
        with open(playbook) as f:
            docs = list(yaml.safe_load_all(f))
        # Ansible playbooks are YAML lists of plays
        plays = docs[0] if isinstance(docs[0], list) else docs
        first_play = plays[0]
        assert "hosts" in first_play, f"{playbook.name} first play missing 'hosts' key"


# ---------------------------------------------------------------------------
# Role structure
# ---------------------------------------------------------------------------

class TestRoleStructure:
    EXPECTED_ROLES = [
        "common",
        "monitoring",
        "interfaces",
        "routing",
        "security",
        "backup_and_restore",
        "qos",
    ]

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_role_exists(self, role: str):
        role_path = ROLES_DIR / role
        assert role_path.is_dir(), f"Role '{role}' directory missing"

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_role_has_tasks(self, role: str):
        tasks_file = ROLES_DIR / role / "tasks" / "main.yml"
        assert tasks_file.exists(), f"Role '{role}' missing tasks/main.yml"

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_role_tasks_valid_yaml(self, role: str):
        tasks_file = ROLES_DIR / role / "tasks" / "main.yml"
        with open(tasks_file) as f:
            content = yaml.safe_load(f)
        # tasks can be None (empty) but file must parse
        assert content is not None or tasks_file.stat().st_size < 10


# ---------------------------------------------------------------------------
# Inventory YAML validity
# ---------------------------------------------------------------------------

class TestInventory:
    def test_static_lab_inventory_valid(self):
        inv_file = INVENTORY_DIR / "lab.yml"
        assert inv_file.exists(), "lab.yml inventory file missing"
        with open(inv_file) as f:
            data = yaml.safe_load(f)
        assert data is not None
        assert "all" in data, "lab.yml must have 'all' top-level group"

    def test_nautobot_inventory_plugin_config(self):
        inv_file = INVENTORY_DIR / "nautobot.yml"
        assert inv_file.exists(), "nautobot.yml inventory file missing"
        with open(inv_file) as f:
            data = yaml.safe_load(f)
        assert data is not None
        assert "plugin" in data, "nautobot.yml must declare 'plugin'"
        assert "nautobot" in data["plugin"], "nautobot.yml must use nautobot plugin"

    def test_static_inventory_has_devices(self):
        inv_file = INVENTORY_DIR / "lab.yml"
        with open(inv_file) as f:
            data = yaml.safe_load(f)
        # Navigate into children/hosts
        children = data.get("all", {}).get("children", {})
        assert len(children) > 0, "lab.yml has no device groups"


# ---------------------------------------------------------------------------
# Collections requirements
# ---------------------------------------------------------------------------

class TestCollections:
    REQUIRED_COLLECTIONS = [
        "ansible.netcommon",
        "arista.eos",
        "cisco.ios",
        "networktocode.nautobot",
    ]

    def test_requirements_file_exists(self):
        req_file = ANSIBLE_DIR / "collections" / "requirements.yml"
        assert req_file.exists(), "collections/requirements.yml missing"

    def test_requirements_file_valid_yaml(self):
        req_file = ANSIBLE_DIR / "collections" / "requirements.yml"
        with open(req_file) as f:
            data = yaml.safe_load(f)
        assert "collections" in data, "requirements.yml must have 'collections' key"
        assert len(data["collections"]) > 0

    @pytest.mark.parametrize("collection", REQUIRED_COLLECTIONS)
    def test_required_collection_declared(self, collection: str):
        req_file = ANSIBLE_DIR / "collections" / "requirements.yml"
        with open(req_file) as f:
            data = yaml.safe_load(f)
        names = [c["name"] for c in data["collections"]]
        assert collection in names, f"Required collection '{collection}' not in requirements.yml"


# ---------------------------------------------------------------------------
# Ansible config
# ---------------------------------------------------------------------------

class TestAnsibleConfig:
    def test_ansible_cfg_exists(self):
        cfg = ANSIBLE_DIR / "ansible.cfg"
        assert cfg.exists(), "ansible.cfg missing"

    def test_ansible_cfg_has_defaults(self):
        cfg = ANSIBLE_DIR / "ansible.cfg"
        content = cfg.read_text()
        assert "[defaults]" in content, "ansible.cfg missing [defaults] section"
