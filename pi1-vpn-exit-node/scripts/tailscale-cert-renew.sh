#!/bin/bash
# Weekly Tailscale/Let's Encrypt certificate refresh for the Pi 1 exit node.
# Deploy to /usr/local/bin/tailscale-cert-renew.sh (mode 0755, root:root).
set -euo pipefail

# --- config ----------------------------------------------------------------
HOSTNAME="<hostname>.<tailnet>.ts.net"
CERT_DIR="/etc/tailscale/certs"
COMPOSE="/home/YOUR_USERNAME/nextcloud/docker-compose.yml"
NGINX_SVC="nginx"          # single container fronting :443 (Nextcloud) and :8443 (ntfy)
PORTS=(443 8443)           # both terminate with the same file cert
# ---------------------------------------------------------------------------

CRT="${CERT_DIR}/${HOSTNAME}.crt"
log() { logger -t tailscale-cert-renew "$1"; }

# Work in a temp dir so `tailscale cert` never writes to / (systemd CWD).
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
cd "$WORK_DIR"

# Self-throttling: real ACME renewal only inside the ~30-day window,
# cached copy returned instantly otherwise. Safe to call weekly.
tailscale cert "$HOSTNAME"

if ! diff -q "${HOSTNAME}.crt" "$CRT" > /dev/null 2>&1; then
    install -m 644 "${HOSTNAME}.crt" "$CRT"
    install -m 640 "${HOSTNAME}.key" "${CERT_DIR}/${HOSTNAME}.key"
    if docker compose -f "$COMPOSE" exec -T "$NGINX_SVC" nginx -s reload; then
        log "file cert renewed; ${NGINX_SVC} reloaded"
    else
        log "file cert renewed; reload FAILED -> restarting ${NGINX_SVC}"
        docker compose -f "$COMPOSE" restart "$NGINX_SVC"
    fi
else
    log "file cert unchanged; no reload"
fi

# Verify every consumer against the on-disk cert; warn on any that lags.
TS_IP=$(tailscale ip -4)
# An empty TS_IP makes the connect string ":443", which openssl resolves to
# loopback — reporting "unreachable" while the real cause is that tailscaled
# was not ready. Seen after the 2026-09 tailscale upgrade restarted the daemon.
if [ -z "$TS_IP" ]; then
    log "ERROR: tailscale ip -4 returned nothing - cannot verify served certs"
    exit 1
fi
file_end=$(openssl x509 -enddate -noout -in "$CRT" | cut -d= -f2)
log "file notAfter=${file_end}"

for p in "${PORTS[@]}"; do
    served=$(echo | openssl s_client -connect "${TS_IP}:${p}" \
             -servername "$HOSTNAME" 2>/dev/null \
             | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2 || true)
    log ":${p} served notAfter=${served:-unreachable}"
    if [ -n "${served:-}" ] \
       && [ "$(date -d "$served" +%s)" -lt "$(date -d "$file_end" +%s)" ]; then
        log "WARNING: :${p} serving an older cert than the file — reload did not take"
    fi
done
