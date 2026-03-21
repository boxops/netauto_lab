#!/usr/bin/env bash
# ============================================================
# setup.sh – Automated setup for the Network Automation Stack
# ============================================================
# Usage: bash setup.sh [--skip-prereqs] [--no-pull]
# ============================================================
set -euo pipefail

NETAUTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${NETAUTO_DIR}/setup.log"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

SKIP_PREREQS=false
NO_PULL=false

for arg in "$@"; do
  case $arg in
    --skip-prereqs) SKIP_PREREQS=true ;;
    --no-pull)      NO_PULL=true ;;
  esac
done

log()     { echo -e "${GREEN}[$(date '+%H:%M:%S')] $*${NC}" | tee -a "${LOG_FILE}"; }
warn()    { echo -e "${YELLOW}[$(date '+%H:%M:%S')] WARNING: $*${NC}" | tee -a "${LOG_FILE}"; }
error()   { echo -e "${RED}[$(date '+%H:%M:%S')] ERROR: $*${NC}" | tee -a "${LOG_FILE}"; exit 1; }
header()  { echo -e "\n${BLUE}══════════════════════════════════════════${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${BLUE}══════════════════════════════════════════${NC}"; }

cd "${NETAUTO_DIR}"

header "Network Automation Stack Setup"
log "Working directory: ${NETAUTO_DIR}"
log "Log file: ${LOG_FILE}"

# ── Step 1: Check prerequisites ───────────────────────────────────────────────
if [[ "${SKIP_PREREQS}" == "false" ]]; then
  header "Step 1/9 – Checking prerequisites"

  command -v docker >/dev/null 2>&1 || error "Docker is not installed. Install from https://docs.docker.com/get-docker/"
  log "✓ Docker found: $(docker --version)"

  docker compose version >/dev/null 2>&1 || error "Docker Compose v2 is not installed. Run: sudo apt install docker-compose-plugin"
  log "✓ Docker Compose found: $(docker compose version)"

  command -v python3 >/dev/null 2>&1 || warn "Python3 not found. Some scripts may not work."
  command -v git >/dev/null 2>&1 || warn "Git not found."

  # Check Docker is running
  docker info >/dev/null 2>&1 || error "Docker daemon is not running. Start with: sudo systemctl start docker"
  log "✓ Docker daemon is running"

  # Check available resources
  MEMORY_GB=$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
  if [[ "${MEMORY_GB}" -lt 8 ]]; then
    warn "Only ${MEMORY_GB}GB RAM available. Recommended minimum is 32GB."
  else
    log "✓ Available memory: ${MEMORY_GB}GB"
  fi

  DISK_AVAIL=$(df -BG "${NETAUTO_DIR}" | awk 'NR==2 {print $4}' | tr -d 'G')
  if [[ "${DISK_AVAIL}" -lt 20 ]]; then
    warn "Only ${DISK_AVAIL}GB disk space available. Recommended minimum is 100GB."
  else
    log "✓ Available disk: ${DISK_AVAIL}GB"
  fi
fi

# ── Step 2: Create .env from template ────────────────────────────────────────
header "Step 2/9 – Environment configuration"
if [[ -f ".env" ]]; then
  warn ".env file already exists. Skipping creation."
else
  cp .env.example .env
  log "Created .env from .env.example"

  # Generate secrets
  log "Generating random secrets..."

  SECRET_KEY=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(50)))")
  NAUTOBOT_DB_PASS=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))")
  REDIS_PASS=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))")
  NAUTOBOT_ADMIN_PASS=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20)))")
  GF_PASS=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20)))")
  GITEA_PASS=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20)))")
  GITEA_SECRET=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40)))")
  GITEA_DB_PASS=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))")
  API_TOKEN=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40)))")

  sed -i "s|CHANGE_ME_USE_STRONG_RANDOM_KEY_50CHARS|${SECRET_KEY}|g" .env
  sed -i "s|CHANGE_ME_nautobot_db_password|${NAUTOBOT_DB_PASS}|g" .env
  sed -i "s|CHANGE_ME_redis_password|${REDIS_PASS}|g" .env
  sed -i "s|CHANGE_ME_nautobot_admin_password|${NAUTOBOT_ADMIN_PASS}|g" .env
  sed -i "s|CHANGE_ME_grafana_admin_password|${GF_PASS}|g" .env
  sed -i "s|CHANGE_ME_gitea_admin_password|${GITEA_PASS}|g" .env
  sed -i "s|CHANGE_ME_gitea_secret_key|${GITEA_SECRET}|g" .env
  sed -i "s|CHANGE_ME_gitea_db_password|${GITEA_DB_PASS}|g" .env
  sed -i "s|CHANGE_ME_nautobot_api_token_40chars|${API_TOKEN}|g" .env

  log "✓ Secrets generated and saved to .env"
fi

# Source the .env for use in this script
set -a; source .env; set +a

# ── Step 3: Create Docker networks ────────────────────────────────────────────
header "Step 3/9 – Creating Docker networks"
for network in netauto_mgmt-network netauto_monitoring-network netauto_syslog-network netauto_clab; do
  if docker network inspect "${network}" >/dev/null 2>&1; then
    log "✓ Network ${network} already exists"
  else
    log "Creating network ${network}..."
  fi
done
log "Docker networks will be created by docker compose..."

# ── Step 4: Pull Docker images ────────────────────────────────────────────────
if [[ "${NO_PULL}" == "false" ]]; then
  header "Step 4/9 – Pulling Docker images"
  log "This may take several minutes..."
  docker compose pull --quiet 2>&1 | tee -a "${LOG_FILE}" || warn "Some images failed to pull. Will try to continue."
fi

# ── Step 5: Build custom images ───────────────────────────────────────────────
header "Step 5/9 – Building custom images"
docker compose build --quiet 2>&1 | tee -a "${LOG_FILE}" || error "Image build failed. Check ${LOG_FILE} for details."
log "✓ Images built successfully"

# ── Step 6: Start core services ───────────────────────────────────────────────
header "Step 6/9 – Starting core services"
docker compose up -d nautobot-postgres redis 2>&1 | tee -a "${LOG_FILE}"
log "Waiting for databases to be healthy..."
sleep 10

docker compose up -d nautobot 2>&1 | tee -a "${LOG_FILE}"
log "Waiting for Nautobot to be healthy (up to 3 minutes)..."

TIMEOUT=180
ELAPSED=0
until docker compose exec -T nautobot curl -sf http://localhost:8080/health/ >/dev/null 2>&1; do
  if [[ ${ELAPSED} -ge ${TIMEOUT} ]]; then
    error "Nautobot did not become healthy within ${TIMEOUT} seconds. Check logs: docker compose logs nautobot"
  fi
  echo -n "."
  sleep 5
  ELAPSED=$((ELAPSED + 5))
done
echo ""
log "✓ Nautobot is healthy"

# ── Step 7: Initialize Nautobot ───────────────────────────────────────────────
header "Step 7/9 – Initializing Nautobot"
log "Running database migrations..."
docker compose exec -T nautobot nautobot-server migrate --no-input 2>&1 | tee -a "${LOG_FILE}"

log "Creating superuser..."
cat > /tmp/nautobot_superuser.py << 'SUPERUSER_SCRIPT'
import os, sys
from django.contrib.auth import get_user_model
from nautobot.users.models import Token
User = get_user_model()
username = os.environ.get('NAUTOBOT_SUPERUSER_NAME', 'admin')
email    = os.environ.get('NAUTOBOT_SUPERUSER_EMAIL', 'admin@example.com')
password  = os.environ.get('NAUTOBOT_SUPERUSER_PASSWORD', '')
token_key = os.environ.get('NAUTOBOT_SUPERUSER_API_TOKEN', '')
if not password:
    print('ERROR: NAUTOBOT_SUPERUSER_PASSWORD is not set')
    sys.exit(1)
u, created = User.objects.get_or_create(username=username, defaults={'email': email, 'is_superuser': True, 'is_staff': True})
u.set_password(password)
u.is_superuser = True
u.is_staff = True
u.save()
if token_key:
    t, _ = Token.objects.get_or_create(user=u, defaults={'key': token_key})
    if t.key != token_key:
        t.key = token_key
        t.save()
print(f"Superuser {'created' if created else 'updated'}: {username}")
SUPERUSER_SCRIPT
docker compose exec -T nautobot nautobot-server shell < /tmp/nautobot_superuser.py 2>&1 | tee -a "${LOG_FILE}"

log "Collecting static files..."
docker compose exec -T nautobot nautobot-server collectstatic --no-input 2>&1 | tee -a "${LOG_FILE}"

log "Loading initial data..."
docker compose exec -T nautobot pip install pynautobot --quiet 2>&1 | tee -a "${LOG_FILE}" || true

# Give Nautobot a moment to fully start
sleep 5

docker compose exec -T nautobot python /opt/nautobot/initializers/load_initial_data.py 2>&1 \
  | tee -a "${LOG_FILE}" || warn "Initial data load failed. You can run it manually later."

log "✓ Nautobot initialized"

# ── Step 8: Start all remaining services ─────────────────────────────────────
header "Step 8/9 – Starting all services"
# Create the clab management network if it doesn't exist yet.
# ContainerLab will use this network when a topology is later deployed.
if ! docker network inspect clab >/dev/null 2>&1; then
  log "Creating clab management network..."
  docker network create --driver bridge \
    --subnet 172.20.20.0/24 \
    --gateway 172.20.20.1 \
    clab 2>&1 | tee -a "${LOG_FILE}"
else
  log "✓ clab network already exists"
fi
docker compose up -d 2>&1 | tee -a "${LOG_FILE}"
log "Waiting for services to stabilize (60 seconds)..."
sleep 60

# ── Initialize Gitea ──────────────────────────────────────────────────────────────────
log "Initializing Gitea admin user..."
GITEA_USER="${GITEA_ADMIN_USER:-gitadmin}"
GITEA_PASS_VAL="${GITEA_ADMIN_PASSWORD}"
GITEA_EMAIL="${GITEA_ADMIN_EMAIL:-gitadmin@example.com}"
docker compose exec -T -u git gitea gitea admin user create \
  --username "${GITEA_USER}" \
  --password "${GITEA_PASS_VAL}" \
  --email "${GITEA_EMAIL}" \
  --admin \
  --must-change-password=false 2>&1 | tee -a "${LOG_FILE}" || \
  log "Gitea admin user already exists or creation skipped."

log "Creating netauto-jobs repository in Gitea..."
GITEA_PORT_VAL="${GITEA_PORT:-3001}"
if curl -sf -u "${GITEA_USER}:${GITEA_PASS_VAL}" \
     -X POST "http://localhost:${GITEA_PORT_VAL}/api/v1/user/repos" \
     -H "Content-Type: application/json" \
     -d '{"name":"netauto-jobs","description":"Nautobot Jobs repository","private":false,"auto_init":true,"default_branch":"main"}' \
     -o /dev/null 2>&1; then
  log "✓ netauto-jobs repository created"
else
  warn "netauto-jobs repository may already exist or Gitea is not ready yet."
fi

log "Registering netauto-jobs Git repository in Nautobot..."
python3 - << GITREPO_SCRIPT 2>&1 | tee -a "${LOG_FILE}" || warn "Git repo registration failed. Register manually in Nautobot."
import os, requests
token = os.environ.get('NAUTOBOT_SUPERUSER_API_TOKEN', '')
port  = os.environ.get('NAUTOBOT_PORT', '8080')
gitea_port = os.environ.get('GITEA_PORT', '3001')
gitea_user = os.environ.get('GITEA_ADMIN_USER', 'gitadmin')
gitea_pass = os.environ.get('GITEA_ADMIN_PASSWORD', '')
base = f'http://localhost:{port}/api'
H = {'Authorization': f'Token {token}', 'Content-Type': 'application/json'}
existing = requests.get(f'{base}/extras/git-repositories/?name=netauto-jobs', headers=H).json()
if existing.get('count', 0) > 0:
    print('Git repository netauto-jobs already registered in Nautobot.')
else:
    r = requests.post(f'{base}/extras/git-repositories/', headers=H, json={
        'name': 'netauto-jobs',
        'remote_url': f'http://{gitea_user}:{gitea_pass}@gitea:3000/{gitea_user}/netauto-jobs.git',
        'branch': 'main',
        'provided_contents': ['extras.job'],
    })
    if r.ok:
        print(f'Registered netauto-jobs Git repository (id={r.json()["id"]}).')
    else:
        print(f'WARNING: {r.status_code} {r.text[:200]}')
GITREPO_SCRIPT

# ── Step 9: Final health check ────────────────────────────────────────────────
header "Step 9/9 – Health check"
bash "${NETAUTO_DIR}/scripts/health_check.sh" 2>&1 | tee -a "${LOG_FILE}" || warn "Some services may not be healthy yet."

# ── Print access information ──────────────────────────────────────────────────
NAUTOBOT_PORT="${NAUTOBOT_PORT:-8080}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
GITEA_PORT="${GITEA_PORT:-3001}"
AGENT_UI_PORT="${AGENT_UI_PORT:-7860}"

echo -e "
${GREEN}╔══════════════════════════════════════════════════════════╗
║          Network Automation Stack – Ready!                ║
╚══════════════════════════════════════════════════════════╝${NC}

${CYAN}Service URLs:${NC}
  Nautobot      : http://localhost:${NAUTOBOT_PORT}
  Grafana       : http://localhost:${GRAFANA_PORT}
  Prometheus    : http://localhost:${PROMETHEUS_PORT}
  Gitea         : http://localhost:${GITEA_PORT}
  AI Agents UI  : http://localhost:${AGENT_UI_PORT}

${CYAN}Default credentials:${NC}
  Nautobot  : ${NAUTOBOT_SUPERUSER_NAME:-admin} / (see .env)
  Grafana   : admin / (see .env)
  Gitea     : ${GITEA_ADMIN_USER:-gitadmin} / (see .env)

${CYAN}Useful commands:${NC}
  make status          - Check service health
  make logs            - Tail all logs
  make ansible-shell   - Open Ansible container shell
  make agent-chat      - CLI chat with AI agent
  make deploy-lab      - Deploy Containerlab topology

${YELLOW}Next steps:${NC}
  1. Open Nautobot and verify initial data was loaded
  2. Configure Nautobot Golden Config with your Git repo
  3. Deploy Containerlab: make deploy-lab
  4. Sync inventory: make sync-inventory
  5. Open Grafana and verify dashboards
"
