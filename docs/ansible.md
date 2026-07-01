# Ansible Playbooks

## Running Playbooks

```bash
make ansible-shell        # interactive Ansible shell
make run-playbook         # prompts for playbook name

# Or directly:
docker compose run --rm ansible \
  ansible-playbook playbooks/health_check.yml \
  -i inventory/nautobot.yml --limit leaf1
```

## Playbook Reference

| Playbook | Purpose |
|---|---|
| `health_check.yml` | Gather facts, check interface states, verify BGP adjacencies, output summary |
| `backup_config.yml` | Backup running configs (`backup_dir=/backups`, `push_to_git=true`) |
| `deploy_config.yml` | Push Golden Config intended configs — **always run with `--check --diff` first** |
| `compliance_check.yml` | Run Golden Config compliance checks; push results to Nautobot |
| `provision_device.yml` | ZTP workflow: create Nautobot record → assign IPAM → baseline config → SNMP/syslog → Slack notify |

## Inventory

**Nautobot dynamic inventory** (`ansible/inventory/nautobot.yml`) — pulls live device data from Nautobot.

```bash
# Test inventory output
docker compose run --rm ansible \
  ansible-inventory -i inventory/nautobot.yml --list | python3 -m json.tool
```

**Static lab inventory** (`ansible/inventory/lab.yml`) — hard-coded Containerlab management IPs for use when Nautobot is unavailable.

## Roles

| Role | Purpose |
|---|---|
| `common` | Hostname, DNS, NTP, management ACL, logging baseline |
| `monitoring` | SNMPv3 credentials, syslog server config |
| `interfaces` | Interface descriptions, L3 IPs, trunk/access VLAN config |
| `routing` | BGP, OSPF, and static route configuration |
| `security` | SSH hardening, AAA (TACACS/RADIUS), control-plane policy |

## Linting

```bash
make lint   # ansible-lint on all playbooks
```
