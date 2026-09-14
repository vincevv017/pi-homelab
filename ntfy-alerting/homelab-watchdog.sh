#!/bin/bash
# Cross-node watchdog: assert the PEER node is alive and not about to expire.
#
# Each node watches the other. This catches the class of failure that
# OnFailure= cannot: a unit that never runs, a disabled timer, an expiring
# node key, or a node that is simply off. Nothing fails, so nothing alerts.
#
# Liveness is inferred from peer presence in `tailscale status` plus a real
# TLS handshake on :443 — NOT from `tailscale ping`, which rejects a full
# MagicDNS FQDN with "no matching peer" and produced a false UNREACHABLE.
# Ping is kept as a diagnostic line and never decides the verdict.
#
# Deploy to /usr/local/bin/homelab-watchdog.sh (mode 0755, root:root).
# No `-e`: every check must run even if an earlier one fails.
set -uo pipefail

# shellcheck disable=SC1091
source /etc/homelab-watchdog.env      # PEER_FQDN, PEER_NAME, WARN_DAYS
# shellcheck disable=SC1091
source /etc/ntfy-alert.env            # NTFY_ALERT_URL, NTFY_CURL_OPTS, NTFY_TOKEN

WARN_DAYS="${WARN_DAYS:-21}"
PROBLEMS=()
log() { logger -t homelab-watchdog "$1"; }

days_left() {   # $1 = "Nov 29 21:02:05 2026 GMT" or ISO8601
    local end_s now_s
    end_s=$(date -d "$1" +%s 2>/dev/null) || return 1
    now_s=$(date +%s)
    echo $(( (end_s - now_s) / 86400 ))
}

# --- resolve the peer once: IPv4 + key expiry in a single status call -------
STATUS=$(tailscale status --json 2>/dev/null)
read -r PEER_IP KEY_EXP <<< "$(printf '%s' "$STATUS" | python3 -c "
import sys, json
want = '${PEER_FQDN}'.rstrip('.')
try:
    peers = json.load(sys.stdin).get('Peer') or {}
except Exception:
    print(' '); sys.exit(0)
for p in peers.values():
    if p.get('DNSName','').rstrip('.') == want:
        ips = p.get('TailscaleIPs') or []
        print(next((i for i in ips if ':' not in i), '-'), p.get('KeyExpiry') or '-')
        break
else:
    print('- -')
" 2>/dev/null)"

if [ "${PEER_IP:--}" = "-" ] || [ -z "${PEER_IP:-}" ]; then
    PROBLEMS+=("peer not present in tailnet (node down, or logged out)")
    log "${PEER_NAME}: not in tailscale status"
    PEER_IP=""
else
    log "${PEER_NAME}: present at ${PEER_IP}"
fi

# --- node key expiry --------------------------------------------------------
if [ "${KEY_EXP:--}" = "-" ] || [ -z "${KEY_EXP:-}" ]; then
    log "${PEER_NAME}: node key expiry disabled"
else
    KD=$(days_left "$KEY_EXP") || KD=""
    if [ -n "$KD" ] && [ "$KD" -lt "$WARN_DAYS" ]; then
        PROBLEMS+=("node key expires in ${KD}d (${KEY_EXP})")
    fi
    log "${PEER_NAME}: node key ${KD:-unknown}d left"
fi

# --- :443 TLS handshake = liveness AND cert expiry in one check -------------
if [ -n "$PEER_IP" ]; then
    CERT_END=$(echo | openssl s_client -connect "${PEER_IP}:443" \
               -servername "$PEER_FQDN" 2>/dev/null \
               | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
    if [ -z "${CERT_END:-}" ]; then
        PROBLEMS+=(":443 not serving TLS (nginx down, or node unreachable)")
        log "${PEER_NAME}: :443 handshake failed"
    else
        CD=$(days_left "$CERT_END") || CD=""
        if [ -n "$CD" ] && [ "$CD" -lt "$WARN_DAYS" ]; then
            PROBLEMS+=("cert expires in ${CD}d (${CERT_END})")
        fi
        log "${PEER_NAME}: cert ${CD:-unknown}d left (${CERT_END})"
    fi

    # Diagnostic only — never contributes to PROBLEMS. Uses the IP, since
    # `tailscale ping <fqdn>` fails with "no matching peer".
    if RTT=$(tailscale ping -c 1 --timeout 5s "$PEER_IP" 2>&1 | head -1); then
        log "${PEER_NAME}: ping ${RTT}"
    else
        log "${PEER_NAME}: ping unavailable (diagnostic only)"
    fi
fi

# --- alert ------------------------------------------------------------------
if [ ${#PROBLEMS[@]} -eq 0 ]; then
    log "${PEER_NAME}: all checks OK"
    exit 0
fi

BODY=$(printf '%s\n' "${PROBLEMS[@]}")
CODE=$(curl -sS --max-time 20 ${NTFY_CURL_OPTS} \
  -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${NTFY_TOKEN}" \
  -H "Title: $(hostname) watchdog: ${PEER_NAME}" \
  -H "Priority: high" \
  -H "Tags: warning" \
  -d "$BODY" \
  "${NTFY_ALERT_URL}" 2>/dev/null) || CODE="curl-error"

# Exit 0 when the alert was delivered: reporting a peer problem IS the script
# doing its job. Exit non-zero only when the alert itself could not be sent, so
# OnFailure= fires for exactly the cases this script cannot report on its own
# (a crash, a missing env file, a rejected push) and never duplicates an alert.
if [ "$CODE" = "200" ]; then
    log "${PEER_NAME}: ${#PROBLEMS[@]} problem(s), alert pushed"
    exit 0
fi
log "${PEER_NAME}: ${#PROBLEMS[@]} problem(s), ALERT PUSH FAILED HTTP ${CODE}"
exit 1
