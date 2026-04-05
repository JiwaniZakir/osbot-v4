#!/usr/bin/env bash
# =============================================================================
# osbot-v4 twice-daily performance audit — host-side wrapper
#
# Cron schedule (VPS local time = UTC):
#   30 10 * * *   /opt/osbot-v4/deploy/audit.sh   # 6:30 AM EDT (10:30 UTC)
#    0  1 * * *   /opt/osbot-v4/deploy/audit.sh   # 9:00 PM EDT (01:00 UTC)
#
# What this script does:
#   1. Verifies the osbot-v4 container is running; starts/restarts it if not
#   2. Waits for the container to become healthy (up to 60s)
#   3. Runs the Python audit script inside the container
#   4. Exits with the audit script's exit code so cron can alert on failures
#
# Output: all stdout/stderr flows to cron's mail or the cron log configured
# below. The audit script also writes to /opt/osbot/state/audit_report.txt
# (inside the container / on the state volume) for persistent history.
# =============================================================================

set -euo pipefail

CONTAINER="osbot-v4"
AUDIT_SCRIPT="/opt/osbot/deploy/audit.py"
COMPOSE_DIR="/opt/osbot-v4/deploy"
LOG_FILE="/var/log/osbot-audit.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

log()  { echo "[AUDIT-HOST] $(_ts) INFO  $*"; }
warn() { echo "[AUDIT-HOST] $(_ts) WARN  $*" >&2; }
err()  { echo "[AUDIT-HOST] $(_ts) ERROR $*" >&2; }

# ---------------------------------------------------------------------------
# Step 1: Check container status
# ---------------------------------------------------------------------------

log "=== osbot-v4 audit starting ==="

CONTAINER_STATUS=$(docker ps --filter "name=^${CONTAINER}$" --format "{{.Status}}" 2>/dev/null || true)

if [[ -z "$CONTAINER_STATUS" ]]; then
    warn "Container '$CONTAINER' not running — attempting to start via docker compose"
    if [[ -d "$COMPOSE_DIR" ]]; then
        (cd "$COMPOSE_DIR" && docker compose up -d) || true
    else
        docker start "$CONTAINER" || true
    fi

    # Wait up to 60s for container to come up
    for i in $(seq 1 12); do
        sleep 5
        CONTAINER_STATUS=$(docker ps --filter "name=^${CONTAINER}$" --format "{{.Status}}" 2>/dev/null || true)
        if [[ -n "$CONTAINER_STATUS" ]]; then
            log "Container started: $CONTAINER_STATUS"
            break
        fi
        if [[ $i -eq 12 ]]; then
            err "Container failed to start after 60s — aborting audit"
            exit 1
        fi
    done
elif echo "$CONTAINER_STATUS" | grep -qi "unhealthy"; then
    warn "Container '$CONTAINER' is unhealthy ($CONTAINER_STATUS) — restarting"
    docker restart "$CONTAINER" || true
    sleep 15
    CONTAINER_STATUS=$(docker ps --filter "name=^${CONTAINER}$" --format "{{.Status}}" 2>/dev/null || true)
    log "Post-restart status: ${CONTAINER_STATUS:-not running}"
else
    log "Container running: $CONTAINER_STATUS"
fi

# ---------------------------------------------------------------------------
# Step 2: Run the Python audit inside the container
# ---------------------------------------------------------------------------

log "Running audit script inside container..."

# Pass the container's env vars through so ceiling overrides work
AUDIT_EXIT=0
docker exec \
    --env OSBOT_STATE_DIR=/opt/osbot/state \
    "$CONTAINER" \
    python3 "$AUDIT_SCRIPT" \
    || AUDIT_EXIT=$?

log "Audit script exited with code $AUDIT_EXIT"

# ---------------------------------------------------------------------------
# Step 3: Interpret exit code
# ---------------------------------------------------------------------------

case $AUDIT_EXIT in
    0)  log "=== Audit complete — all systems healthy ===" ;;
    1)  err "=== Audit FAILED — DB unavailable (check container state volume) ===" ;;
    2)  warn "=== Audit WARNING — token utilization critical (bot may pause soon) ===" ;;
    *)  warn "=== Audit finished with unexpected exit code $AUDIT_EXIT ===" ;;
esac

exit $AUDIT_EXIT
