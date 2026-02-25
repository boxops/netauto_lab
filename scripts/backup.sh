#!/usr/bin/env bash
# ============================================================
# backup.sh – Backup all persistent data from the stack
# ============================================================
# Usage: bash scripts/backup.sh [--dest /path/to/backups]
# ============================================================
set -euo pipefail

NETAUTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DEST="${NETAUTO_DIR}/backups"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_PATH="${BACKUP_DEST}/${TIMESTAMP}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

for arg in "$@"; do
  case $arg in
    --dest) BACKUP_DEST="$2"; shift 2 ;;
  esac
done

log()   { echo -e "${GREEN}[$(date '+%H:%M:%S')] $*${NC}"; }
warn()  { echo -e "${YELLOW}[$(date '+%H:%M:%S')] WARNING: $*${NC}"; }
error() { echo -e "${RED}[$(date '+%H:%M:%S')] ERROR: $*${NC}"; exit 1; }

cd "${NETAUTO_DIR}"
source .env 2>/dev/null || true

mkdir -p "${BACKUP_PATH}"
log "Backup path: ${BACKUP_PATH}"

# ── Nautobot database ────────────────────────────────────────────────────────
log "Backing up Nautobot database..."
docker compose exec -T nautobot-postgres pg_dump \
  -U "${NAUTOBOT_DB_USER:-nautobot}" \
  -d "${NAUTOBOT_DB_NAME:-nautobot}" \
  --no-password 2>/dev/null \
  | gzip > "${BACKUP_PATH}/nautobot_postgres_${TIMESTAMP}.sql.gz"
log "✓ Nautobot database backed up"

# ── Gitea database ────────────────────────────────────────────────────────────
log "Backing up Gitea database..."
docker compose exec -T gitea-postgres pg_dump \
  -U "${GITEA_DB_USER:-gitea}" \
  -d "${GITEA_DB_NAME:-gitea}" \
  --no-password 2>/dev/null \
  | gzip > "${BACKUP_PATH}/gitea_postgres_${TIMESTAMP}.sql.gz" || warn "Gitea DB backup failed"
log "✓ Gitea database backed up"

# ── Configuration files ───────────────────────────────────────────────────────
log "Backing up configuration files..."
tar -czf "${BACKUP_PATH}/configs_${TIMESTAMP}.tar.gz" \
  .env \
  prometheus/ \
  grafana/provisioning/ \
  telegraf/ \
  loki/ \
  promtail/ \
  nautobot/configuration/ \
  ansible/ansible.cfg \
  ansible/inventory/ \
  ansible/collections/ \
  containerlab/ \
  2>/dev/null || warn "Some config files could not be backed up"
log "✓ Configuration files backed up"

# ── Prometheus data snapshot ─────────────────────────────────────────────────
log "Creating Prometheus snapshot..."
SNAPSHOT_RESP=$(curl -s -X POST "http://localhost:${PROMETHEUS_PORT:-9090}/api/v1/admin/tsdb/snapshot" 2>/dev/null || echo '{}')
SNAPSHOT_NAME=$(echo "${SNAPSHOT_RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data', {}).get('name', ''))" 2>/dev/null || echo "")
if [[ -n "${SNAPSHOT_NAME}" ]]; then
  docker compose cp "prometheus:/prometheus/snapshots/${SNAPSHOT_NAME}" "${BACKUP_PATH}/prometheus_snapshot_${TIMESTAMP}" 2>/dev/null \
    && log "✓ Prometheus snapshot: ${SNAPSHOT_NAME}" \
    || warn "Failed to copy Prometheus snapshot"
else
  warn "Failed to create Prometheus snapshot (API may not be accessible)"
fi

# ── Clean old backups (keep last 30 days) ────────────────────────────────────
log "Cleaning backups older than 30 days..."
find "${BACKUP_DEST}" -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>/dev/null || true

# ── Summary ───────────────────────────────────────────────────────────────────
BACKUP_SIZE=$(du -sh "${BACKUP_PATH}" 2>/dev/null | cut -f1)
log "Backup complete. Size: ${BACKUP_SIZE}. Location: ${BACKUP_PATH}"

ls -lh "${BACKUP_PATH}/"
