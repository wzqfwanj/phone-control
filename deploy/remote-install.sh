#!/usr/bin/env bash
# deploy/remote-install.sh - runs on the dev host (as root), invoked by deploy/push.ps1.
#
# Installs/updates phone-control under $REMOTE_ROOT, keeps an existing .env untouched,
# creates .venv + dependencies, installs the systemd unit, restarts the service and
# self-checks http://127.0.0.1:8090/healthz. Idempotent: safe to run on every deploy.
#
# Inputs (env vars, all optional):
#   STAGE        staging dir holding server.tar, web.tar, phone-control.service, env.example
#                (default: /tmp/phone-control-push)
#   REMOTE_ROOT  install dir                     (default: /opt/phone-control)
#   SERVICE_NAME systemd unit name               (default: phone-control)
#
# Exit code 0 = deployed and healthy; non-zero = clear error on stderr.
set -euo pipefail

STAGE="${STAGE:-/tmp/phone-control-push}"
ROOT="${REMOTE_ROOT:-/opt/phone-control}"
SERVICE="${SERVICE_NAME:-phone-control}"
PORT="8090"
HEALTH_URL="http://127.0.0.1:${PORT}/healthz"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
[ "$(id -u)" = "0" ] || die "must run as root on the dev host"
for f in server.tar web.tar phone-control.service env.example; do
    [ -f "$STAGE/$f" ] || die "missing staging file: $STAGE/$f"
done
command -v python3 >/dev/null 2>&1 || die "python3 not found on dev host"
command -v openssl >/dev/null 2>&1 || die "openssl not found on dev host (needed for DEVICE_KEY)"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found on dev host"

log "install root: $ROOT (systemd unit: $SERVICE)"
mkdir -p "$ROOT/server" "$ROOT/web"

# ---------------------------------------------------------------- 1. server code
# Stage into *.new and rsync --delete, so files removed from the repo disappear too.
log "install server/ code"
rm -rf "$ROOT/server.new"
mkdir -p "$ROOT/server.new"
tar -xf "$STAGE/server.tar" -C "$ROOT/server.new"
find "$ROOT/server.new" -type f -exec chmod 644 {} +
find "$ROOT/server.new" -type d -exec chmod 755 {} +
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$ROOT/server.new/" "$ROOT/server/"
    rm -rf "$ROOT/server.new"
else
    rm -rf "$ROOT/server"
    mv "$ROOT/server.new" "$ROOT/server"
fi

# ---------------------------------------------------------------- 2. web static assets
log "install web/dist/ assets"
rm -rf "$ROOT/web/dist.new"
mkdir -p "$ROOT/web/dist.new"
tar -xf "$STAGE/web.tar" -C "$ROOT/web/dist.new"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$ROOT/web/dist.new/" "$ROOT/web/dist/"
    rm -rf "$ROOT/web/dist.new"
else
    rm -rf "$ROOT/web/dist"
    mv "$ROOT/web/dist.new" "$ROOT/web/dist"
fi
# normalize permissions: tar from Windows stores 0777/0666, static assets need 0755/0644
find "$ROOT/web/dist" -type d -exec chmod 755 {} +
find "$ROOT/web/dist" -type f -exec chmod 644 {} +

# ---------------------------------------------------------------- 3. .env (preserve!)
log "ensure $ROOT/.env"
if [ ! -f "$ROOT/.env" ]; then
    DEVICE_KEY="$(openssl rand -hex 16)"
    sed "s|^CONTROL_DEVICE_KEY=.*|CONTROL_DEVICE_KEY=${DEVICE_KEY}|" "$STAGE/env.example" > "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    log "created .env with a new random CONTROL_DEVICE_KEY (read it with: grep ^CONTROL_DEVICE_KEY= $ROOT/.env)"
else
    log "kept existing .env (DEVICE_KEY and other custom values unchanged)"
fi

# Fill in defaults only when a required key is missing; never overwrite custom values.
ensure_env() {
    local key="$1" value="$2"
    if ! grep -q "^${key}=" "$ROOT/.env"; then
        printf '%s=%s\n' "$key" "$value" >> "$ROOT/.env"
        log "added missing ${key}=${value} to .env"
    fi
}
ensure_env CONTROL_HOST 127.0.0.1
ensure_env CONTROL_PORT "$PORT"
ensure_env CONTROL_STATIC_DIR web/dist

# ---------------------------------------------------------------- 4. venv + deps
log "python venv + dependencies"
VENV="$ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || die "python3 -m venv failed (install python3-venv on the dev host?)"
fi
REQ_FILE="$ROOT/server/requirements.txt"
[ -f "$REQ_FILE" ] || die "missing $REQ_FILE in uploaded server code"
REQ_SUM="$(sha256sum "$REQ_FILE" | awk '{print $1}')"
STAMP="$VENV/.requirements.sha256"
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$REQ_SUM" ]; then
    "$VENV/bin/python" -m pip install --disable-pip-version-check -q -r "$REQ_FILE" \
        || die "pip install failed (dev host needs network access to PyPI)"
    printf '%s\n' "$REQ_SUM" > "$STAMP"
else
    log "dependencies already up to date (requirements.txt unchanged)"
fi

# ---------------------------------------------------------------- 5. systemd unit
log "install systemd unit and (re)start $SERVICE"
install -m 644 "$STAGE/phone-control.service" "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"
sleep 1
if ! systemctl is-active --quiet "$SERVICE"; then
    printf 'ERROR: %s failed to start\n' "$SERVICE" >&2
    systemctl status "$SERVICE" --no-pager -l >&2 || true
    journalctl -u "$SERVICE" -n 80 --no-pager >&2 || true
    exit 1
fi

# ---------------------------------------------------------------- 6. health self-check
log "health check $HEALTH_URL"
ok=0
body=""
for _ in $(seq 1 20); do
    if body="$(curl -fsS --max-time 2 "$HEALTH_URL" 2>/dev/null)"; then
        if printf '%s' "$body" | grep -q '"ok"'; then
            ok=1
            break
        fi
    fi
    sleep 0.5
done
if [ "$ok" -ne 1 ]; then
    printf 'ERROR: health check failed: %s\n' "$HEALTH_URL" >&2
    systemctl status "$SERVICE" --no-pager -l >&2 || true
    journalctl -u "$SERVICE" -n 100 --no-pager >&2 || true
    exit 1
fi
log "healthz: $body"

# ---------------------------------------------------------------- 7. loopback sanity
listen_line="$(ss -ltnp 2>/dev/null | grep -E ":${PORT}[[:space:]]" || true)"
if [ -z "$listen_line" ]; then
    die "nothing is listening on port $PORT"
fi
log "listen: $listen_line"
case "$listen_line" in
    *"127.0.0.1:${PORT}"*) : ;;
    *) die "service is not bound to loopback 127.0.0.1:${PORT} only (check CONTROL_HOST in $ROOT/.env)" ;;
esac

log "phone-control installed and running"
