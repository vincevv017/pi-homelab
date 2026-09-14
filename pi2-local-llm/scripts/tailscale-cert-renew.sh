#!/bin/bash
# Weekly Tailscale/Let's Encrypt certificate refresh for the Pi 2 LLM node.
# Two consumers: Docker nginx on :443 (file cert) and the Funnel on :8443
# (tailscaled internal store). One `tailscale cert` call refreshes both.
# Deploy to /usr/local/bin/tailscale-cert-renew.sh (mode 0755, root:root).
set -euo pipefail

# --- config ----------------------------------------------------------------
HOSTNAME="<hostname>.<tailnet>.ts.net"
CERT_DIR="/etc/tailscale/certs"
COMPOSE="/home/YOUR_USERNAME/openwebui/docker-compose.yml"
NGINX_SVC="nginx"          # docker compose service name
FUNNEL_PORT=8443           # SQL-fixer Funnel (tailscaled-managed cert); verify only
# ---------------------------------------------------------------------------

CRT="${CERT_DIR}/${HOSTNAME}.crt"
log() { logger -t tailscale-cert-renew "$1"; }

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
cd "$WORK_DIR"

# Self-throttling: real ACME renewal only inside the window, cached otherwise.
# Also refreshes tailscaled's internal store -> the funnel's :$FUNNEL_PORT cert.
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

# Verify both consumers; warn if the running funnel lags the file cert.
TS_IP=$(tailscale ip -4)
# An empty TS_IP makes the connect string ":443", which openssl resolves to
# loopback — reporting "unreachable" while the real cause is that tailscaled
# was not ready. Seen after the 2026-09 tailscale upgrade restarted the daemon.
if [ -z "$TS_IP" ]; then
    log "ERROR: tailscale ip -4 returned nothing - cannot verify served certs"
    exit 1
fi
file_end=$(openssl x509 -enddate -noout -in "$CRT" | cut -d= -f2)
fun_end=$(echo | openssl s_client -connect "${TS_IP}:${FUNNEL_PORT}" \
          -servername "$HOSTNAME" 2>/dev/null \
          | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2 || true)
log ":443 file notAfter=${file_end}"
log ":${FUNNEL_PORT} funnel notAfter=${fun_end:-unreachable}"

if [ -n "${fun_end:-}" ] \
   && [ "$(date -d "$fun_end" +%s)" -lt "$(date -d "$file_end" +%s)" ]; then
    log "WARNING: funnel cert older than file cert — bounce funnel on :${FUNNEL_PORT}"
fi
