# Ansible Playbooks

## Running Playbooks

Use the Ansible container to run any playbook:

```bash
# Open an interactive Ansible shell
make ansible-shell

# Run a specific playbook interactively (prompts for name)
make run-playbook

# Or run directly
docker compose run --rm ansible \
  ansible-playbook playbooks/health_check.yml \
  -i inventory/nautobot.yml \
  --limit leaf1
```

## Playbook Reference

### health_check.yml

Collects facts and verifies basic connectivity across all managed devices.

```bash
ansible-playbook playbooks/health_check.yml -i inventory/nautobot.yml
```

**What it does:**
- Gathers device facts (OS version, uptime, serial number)
- Checks interface operational states
- Verifies BGP peer adjacencies (if configured)
- Outputs a summary report

---

### backup_config.yml

Backs up running configurations from all devices.

```bash
ansible-playbook playbooks/backup_config.yml -i inventory/nautobot.yml
```

**Variables:**
- `backup_dir` (default: `/backups`) — local path for config files
- `push_to_git` (default: `true`) — commit and push configs to Gitea

---

### deploy_config.yml

Pushes intended configurations from Nautobot Golden Config to devices.

```bash
# Always run with --check first
ansible-playbook playbooks/deploy_config.yml \
  -i inventory/nautobot.yml \
  --check --diff \
  --limit spine1
```

**Variables:**
- `config_source` — Nautobot Golden Config rendered template
- `rollback_on_failure` (default: `true`)

---

### compliance_check.yml

Runs Nautobot Golden Config compliance checks and reports deviations.

```bash
ansible-playbook playbooks/compliance_check.yml -i inventory/nautobot.yml
```

Results are pushed back to Nautobot Golden Config compliance endpoint.

---

### provision_device.yml

Zero-touch provisioning workflow for new devices.

```bash
ansible-playbook playbooks/provision_device.yml \
  -i inventory/nautobot.yml \
  -e "device_name=leaf4 mgmt_ip=172.20.20.24"
```

**Workflow:**
1. Creates device record in Nautobot
2. Assigns management IP from IPAM
3. Configures baseline config (common role)
4. Configures monitoring (SNMPv3, syslog)
5. Notifies via Slack webhook

## Inventory

### Nautobot Dynamic Inventory

Located at `ansible/inventory/nautobot.yml`. Pulls live device data from Nautobot.

```bash
# Test inventory output
docker compose run --rm ansible \
  ansible-inventory -i inventory/nautobot.yml --list | python3 -m json.tool
```

### Static Lab Inventory

Located at `ansible/inventory/lab.yml`. Hard-coded Containerlab management IPs for use when Nautobot is not available.

```yaml
# Example: target just spines
ansible-playbook playbooks/health_check.yml \
  -i inventory/lab.yml --limit spines
```

## Ansible Roles

| Role | Purpose |
|------|---------|
| `common` | Hostname, DNS, NTP, management ACL, logging baseline |
| `monitoring` | SNMPv3 credentials, syslog server config |
| `interfaces` | Interface descriptions, L3 IPs, trunk/access VLAN config |
| `routing` | BGP, OSPF, and static route configuration |
| `security` | SSH hardening, AAA (TACACS/RADIUS), control-plane policy |

## Linting

```bash
make lint   # runs ansible-lint on all playbooks
```
