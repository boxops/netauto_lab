#!/usr/bin/env bash
# ============================================================
# health_check.sh – Verify all stack services are healthy
# ============================================================
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

NETAUTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${NETAUTO_DIR}"

source .env 2>/dev/null || true

NAUTOBOT_PORT="${NAUTOBOT_PORT:-8080}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
ALERTMANAGER_PORT="${ALERTMANAGER_PORT:-9093}"
GITEA_PORT="${GITEA_PORT:-3001}"
AGENT_UI_PORT="${AGENT_UI_PORT:-7860}"
LOKI_PORT="${LOKI_PORT:-3100}"

PASS=0
FAIL=0
WARN=0
RESULTS=()

check() {
  local name="$1"
  local url="$2"
  local expected_code="${3:-200}"

  http_code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "${url}" 2>/dev/null || echo "000")

  if [[ "${http_code}" == "${expected_code}" ]]; then
    RESULTS+=("${GREEN}  ✓ ${name}${NC} (HTTP ${http_code})")
    PASS=$((PASS + 1))
  elif [[ "${http_code}" == "000" ]]; then
    RESULTS+=("${RED}  ✗ ${name}${NC} (Connection refused / timeout)")
    FAIL=$((FAIL + 1))
  else
    RESULTS+=("${YELLOW}  ⚠ ${name}${NC} (HTTP ${http_code}, expected ${expected_code})")
    WARN=$((WARN + 1))
  fi
}

check_container() {
  local name="$1"
  local container_pattern="$2"

  container=$(docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        if '${container_pattern}' in d.get('Service', ''):
            print(d.get('State', 'unknown'))
    except: pass
" 2>/dev/null | head -1)

  if [[ "${container}" == "running" ]]; then
    RESULTS+=("${GREEN}  ✓ ${name}${NC} container running")
    PASS=$((PASS + 1))
  elif [[ -z "${container}" ]]; then
    RESULTS+=("${YELLOW}  ⚠ ${name}${NC} container not found")
    WARN=$((WARN + 1))
  else
    RESULTS+=("${RED}  ✗ ${name}${NC} container: ${container}")
    FAIL=$((FAIL + 1))
  fi
}

echo -e "\n${BLUE}══════════════════════════════════════════"
echo -e "  Network Automation Stack – Health Check"
echo -e "══════════════════════════════════════════${NC}\n"

echo "Checking HTTP endpoints..."
check "Nautobot"         "http://localhost:${NAUTOBOT_PORT}/health/"
check "Grafana"          "http://localhost:${GRAFANA_PORT}/api/health"
check "Prometheus"       "http://localhost:${PROMETHEUS_PORT}/-/healthy"
check "Alertmanager"     "http://localhost:${ALERTMANAGER_PORT}/-/healthy"
check "Loki"             "http://localhost:${LOKI_PORT}/ready"
check "Gitea"            "http://localhost:${GITEA_PORT}/api/healthz"
check "Agent UI"         "http://localhost:${AGENT_UI_PORT}/" 200

echo ""
echo "Checking containers..."
check_container "nautobot"           "nautobot"
check_container "nautobot-worker"    "nautobot-worker"
check_container "prometheus"         "prometheus"
check_container "grafana"            "grafana"
check_container "telegraf"           "telegraf"
check_container "loki"               "loki"
check_container "promtail"           "promtail"
check_container "gitea"              "gitea"
check_container "rabbitmq"           "rabbitmq"
check_container "ai-ops-agent"       "ai-ops-agent"
check_container "ai-eng-agent"       "ai-eng-agent"

echo ""
echo "Checking disk space..."
DISK_AVAIL=$(df -BG "${NETAUTO_DIR}" | awk 'NR==2 {print $4}' | tr -d 'G')
if [[ "${DISK_AVAIL}" -lt 5 ]]; then
  RESULTS+=("${RED}  ✗ Disk space${NC} CRITICAL: only ${DISK_AVAIL}GB available")
  FAIL=$((FAIL + 1))
elif [[ "${DISK_AVAIL}" -lt 20 ]]; then
  RESULTS+=("${YELLOW}  ⚠ Disk space${NC} WARNING: only ${DISK_AVAIL}GB available")
  WARN=$((WARN + 1))
else
  RESULTS+=("${GREEN}  ✓ Disk space${NC} ${DISK_AVAIL}GB available")
  PASS=$((PASS + 1))
fi

echo ""
echo "Results:"
for r in "${RESULTS[@]}"; do
  echo -e "${r}"
done

echo ""
echo -e "${BLUE}══════════════════════════════════════════${NC}"
echo -e "  Summary: ${GREEN}${PASS} passed${NC}  ${YELLOW}${WARN} warnings${NC}  ${RED}${FAIL} failed${NC}"
echo -e "${BLUE}══════════════════════════════════════════${NC}\n"

if [[ ${FAIL} -gt 0 ]]; then
  echo -e "${YELLOW}Troubleshooting tips:"
  echo "  - View logs:  docker compose logs <service>"
  echo "  - Restart:    docker compose restart <service>"
  echo "  - Full setup: bash setup.sh${NC}"
  exit 1
fi
