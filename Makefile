# ============================================================
# Makefile – Network Automation Stack
# ============================================================
# Usage: make <target>
# Run 'make help' to see all available targets.
# ============================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash
.ONESHELL:

-include .env
export

COMPOSE     := docker compose
PROJECT_DIR := $(shell pwd)
DATE        := $(shell date '+%Y%m%d_%H%M%S')

# Colors
GREEN  := \033[0;32m
YELLOW := \033[1;33m
CYAN   := \033[0;36m
NC     := \033[0m

.PHONY: help init start stop restart logs status clean \
        backup-data restore-data \
        deploy-lab destroy-lab \
        ansible-shell run-playbook sync-inventory \
        agent-chat update health-check \
        lint test

## ── Setup & lifecycle ─────────────────────────────────────────────────────────

help:  ## Show this help message
	@echo ""
	@echo "  Network Automation Stack – Available targets"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-22s$(NC) %s\n", $$1, $$2}'
	@echo ""

init:  ## Initialize environment (first-time setup)
	@echo -e "$(GREEN)Initializing Network Automation Stack...$(NC)"
	@bash setup.sh

init-service:  ## Install and enable netauto-lab.service (auto-start on VM boot)
	@echo -e "$(GREEN)Installing netauto-lab systemd service...$(NC)"
	@sudo cp $(PROJECT_DIR)/netauto-lab.service /etc/systemd/system/netauto-lab.service
	@sudo systemctl daemon-reload
	@sudo systemctl enable netauto-lab.service
	@echo -e "$(GREEN)Service installed and enabled. The stack will start automatically on next boot.$(NC)"
	@echo -e "$(CYAN)Manage with: sudo systemctl {start|stop|status} netauto-lab$(NC)"

remove-service:  ## Disable and remove netauto-lab.service
	@echo -e "$(YELLOW)Removing netauto-lab systemd service...$(NC)"
	@sudo systemctl disable --now netauto-lab.service || true
	@sudo rm -f /etc/systemd/system/netauto-lab.service
	@sudo systemctl daemon-reload
	@echo -e "$(GREEN)Service removed.$(NC)"

start:  ## Start all services
	@echo -e "$(GREEN)Starting all services...$(NC)"
	@docker network inspect clab >/dev/null 2>&1 \
	  || docker network create clab --subnet 172.20.20.0/24 >/dev/null
	@$(COMPOSE) rm -f 2>/dev/null || true
	$(COMPOSE) up -d
	@echo -e "$(GREEN)Services started. Run 'make status' to verify.$(NC)"

stop:  ## Stop all services
	@echo -e "$(YELLOW)Stopping all services...$(NC)"
	$(COMPOSE) stop

restart:  ## Restart all services (or specific: make restart SVC=grafana)
	@if [ -n "$(SVC)" ]; then \
	  echo -e "$(YELLOW)Restarting $(SVC)...$(NC)"; \
	  $(COMPOSE) restart $(SVC); \
	else \
	  echo -e "$(YELLOW)Restarting all services...$(NC)"; \
	  $(COMPOSE) restart; \
	fi

clab-inspect:
	@echo -e "$(CYAN)Inspecting Containerlab topology...$(NC)"
	@sudo containerlab inspect --topo containerlab/topologies/spine-leaf.clab.yml

logs:  ## Tail logs for all services (or specific: make logs SVC=nautobot)
	@if [ -n "$(SVC)" ]; then \
	  $(COMPOSE) logs -f $(SVC); \
	else \
	  $(COMPOSE) logs -f; \
	fi

status:  ## Show service status
	@echo -e "$(CYAN)Service status:$(NC)"
	$(COMPOSE) ps

health-check:  ## Run comprehensive health check
	@bash scripts/health_check.sh

update:  ## Pull latest images and restart
	@echo -e "$(YELLOW)Pulling latest images...$(NC)"
	$(COMPOSE) pull
	$(COMPOSE) build --pull
	$(COMPOSE) up -d
	@echo -e "$(GREEN)Update complete.$(NC)"

## ── Data management ───────────────────────────────────────────────────────────

backup-data:  ## Backup all persistent data
	@echo -e "$(GREEN)Starting backup...$(NC)"
	@bash scripts/backup.sh
	@echo -e "$(GREEN)Backup complete.$(NC)"

restore-data:  ## Restore from backup (interactive)
	@echo -e "$(YELLOW)Available backups:$(NC)"
	@ls -la backups/ 2>/dev/null || echo "No backups found."
	@echo ""
	@read -p "Enter backup timestamp to restore (YYYYMMDD_HHMMSS): " BACKUP_TS; \
	BACKUP_PATH="backups/$${BACKUP_TS}"; \
	if [ ! -d "$${BACKUP_PATH}" ]; then \
	  echo "Backup not found: $${BACKUP_PATH}"; exit 1; \
	fi; \
	echo "Restoring from $${BACKUP_PATH}..."; \
	$(COMPOSE) exec -T nautobot-postgres sh -c \
	  "gunzip -c /backups/$${BACKUP_TS}/nautobot_postgres_$${BACKUP_TS}.sql.gz | psql -U $${NAUTOBOT_DB_USER:-nautobot} $${NAUTOBOT_DB_NAME:-nautobot}" || true; \
	echo "Restore complete."

clean:  ## Remove all containers and data (DESTRUCTIVE – prompts for confirmation)
	@echo -e "$(RED)WARNING: This will destroy ALL data in the stack!$(NC)"
	@read -p "Type 'yes' to confirm: " CONFIRM; \
	if [ "$${CONFIRM}" = "yes" ]; then \
	  $(COMPOSE) down -v --remove-orphans; \
	  echo "All containers and volumes removed."; \
	else \
	  echo "Cancelled."; \
	fi

## ── Containerlab ──────────────────────────────────────────────────────────────

deploy-lab:  ## Deploy Containerlab spine-leaf topology
	@echo -e "$(GREEN)Deploying Containerlab topology...$(NC)"
	@sudo containerlab deploy --topo containerlab/topologies/spine-leaf.clab.yml 2>&1 \
	  || { echo -e "$(YELLOW)Containerlab deploy failed. See docs.$(NC)"; exit 1; }
	@echo -e "$(GREEN)Lab deployed. Run 'make sync-inventory' to register in Nautobot.$(NC)"

destroy-lab:  ## Destroy Containerlab topology
	@echo -e "$(YELLOW)Destroying Containerlab topology...$(NC)"
	@sudo containerlab destroy --topo containerlab/topologies/spine-leaf.clab.yml --cleanup 2>&1 || true

redeploy-lab:  ## Redeploy Containerlab topology (destroy + deploy)
	@make destroy-lab
	@make deploy-lab

sync-inventory:  ## Sync Containerlab devices to Nautobot
	@echo -e "$(GREEN)Syncing inventory to Nautobot...$(NC)"
	@NAUTOBOT_URL="http://localhost:$(NAUTOBOT_PORT)" \
	  NAUTOBOT_SUPERUSER_API_TOKEN="$(NAUTOBOT_SUPERUSER_API_TOKEN)" \
	  python3 scripts/sync_inventory.py
	@echo -e "$(GREEN)Inventory sync complete.$(NC)"

sync-inventory-dry:  ## Preview inventory sync (dry run)
	@NAUTOBOT_URL="http://localhost:$(NAUTOBOT_PORT)" \
	  NAUTOBOT_SUPERUSER_API_TOKEN="$(NAUTOBOT_SUPERUSER_API_TOKEN)" \
	  python3 scripts/sync_inventory.py --dry-run

## ── Ansible ───────────────────────────────────────────────────────────────────

ansible-shell:  ## Open an interactive Ansible container shell
	@echo -e "$(CYAN)Opening Ansible shell. Type 'exit' to leave.$(NC)"
	$(COMPOSE) exec ansible bash

run-playbook:  ## Run an Ansible playbook (interactive)
	@echo -e "$(CYAN)Available playbooks:$(NC)"
	@ls ansible/playbooks/*.yml | xargs -I{} basename {}
	@echo ""
	@read -p "Playbook name (without .yml): " PB; \
	read -p "Target hosts (leave blank for all): " HOSTS; \
	read -p "Check mode? [Y/n]: " CHECK; \
	CMD="ansible-playbook /ansible/playbooks/$${PB}.yml -i /ansible/inventory/lab.yml"; \
	[ -n "$$HOSTS" ] && CMD="$$CMD --limit $$HOSTS"; \
	[ "$${CHECK:-Y}" != "n" ] && CMD="$$CMD --check --diff"; \
	$(COMPOSE) exec ansible $$CMD

lint:  ## Lint Ansible playbooks and configs
	@echo -e "$(GREEN)Linting Ansible playbooks...$(NC)"
	$(COMPOSE) exec ansible ansible-lint /ansible/playbooks/ || true
	@echo -e "$(GREEN)Validating YAML configs...$(NC)"
	@find prometheus loki promtail grafana telegraf -name '*.yml' -o -name '*.yaml' 2>/dev/null \
	  | xargs python3 -c "import sys, yaml; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" 2>&1 \
	  && echo "YAML validation: OK" || echo "YAML validation: check errors above"

## ── Nautobot Jobs ──────────────────────────────────────────────────────

refresh-jobs:  ## Re-scan JOBS_ROOT and register any new/changed Job classes
	@echo -e "$(CYAN)Refreshing Nautobot Jobs from JOBS_ROOT...$(NC)"
	@echo "\
from nautobot.extras.jobs import get_jobs; \
from nautobot.extras.models import Job as JobModel, JobQueue; \
dq = JobQueue.objects.get(name='default'); \
jobs = get_jobs(reload=True); \
new = [JobModel.objects.get_or_create(module_name=jc.__module__, job_class_name=jc.__name__, defaults={'name': getattr(jc.Meta,'name',jc.__name__), 'default_job_queue': dq}) for jc in jobs.values()]; \
created = sum(1 for _, c in new if c); \
print(f'Jobs in registry: {len(jobs)} | New DB records: {created}')" \
	  | $(COMPOSE) exec -T nautobot nautobot-server shell
	@echo -e "$(GREEN)Jobs refreshed.$(NC)"

sync-jobs:  ## Pull the latest commits from the netauto-jobs Git repo into Nautobot
	@echo -e "$(CYAN)Syncing netauto-jobs Git repository in Nautobot...$(NC)"
	@python3 scripts/sync_nautobot_jobs.py
	@echo -e "$(GREEN)Sync complete.$(NC)"

## ── AI Agents ─────────────────────────────────────────────────────────────────

define AGENT_CHAT_PY
import sys, httpx, json
OPS_URL = 'http://localhost:8000'
print('Connecting to Ops Agent...')
session_id = ''
while True:
    try:
        msg = input('\n[Ops Agent] > ').strip()
        if msg.lower() in ('exit', 'quit', 'q'):
            break
        if not msg:
            continue
        resp = httpx.post(f'{OPS_URL}/chat',
            json={'message': msg, 'session_id': session_id},
            timeout=120)
        resp.raise_for_status()
        data = resp.json()
        session_id = data.get('session_id', session_id)
        print(f'\n{data["response"]}')
    except KeyboardInterrupt:
        break
    except httpx.ConnectError:
        print('Agent service is not available. Start with: make start')
        sys.exit(1)
    except Exception as e:
        print(f'Error: {e}')
endef
export AGENT_CHAT_PY

agent-chat:  ## Start an interactive CLI chat with the Ops Agent
	@echo -e "$(CYAN)Network AI Agents CLI – type 'exit' to quit$(NC)"
	@echo ""
	@which python3 >/dev/null 2>&1 || (echo "python3 required"; exit 1)
	@python3 -c "$$AGENT_CHAT_PY"

## ── Testing ───────────────────────────────────────────────────────────────────

test:  ## Run all tests
	@echo -e "$(GREEN)Running tests...$(NC)"
	@python3 -m pytest tests/ -v --tb=short 2>&1 || echo "Tests failed or pytest not installed."
