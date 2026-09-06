#!/bin/bash
# Push a systemd unit failure to ntfy.
# Invoked via OnFailure=ntfy-alert@%N.service  (%N, not %n — see README)
# Deploy to /usr/local/bin/ntfy-alert.sh (mode 0755, root:root).
#
# No `-e`: a partial alert beats an aborted one. Every path logs.
set -uo pipefail

UNIT="${1:?usage: ntfy-alert.sh <unit>}"
# shellcheck disable=SC1091
source /etc/ntfy-alert.env

STATE=$(systemctl show "$UNIT" -p Result --value 2>/dev/null || echo unknown)
# ntfy caps the message body at 4096 bytes; leave room for title and headers.
DETAIL=$(journalctl -u "$UNIT" -n 15 --no-pager -o cat 2>/dev/null | tail -c 2500)

# curl without --fail exits 0 on 401/404, so check the status code explicitly
# rather than relying on `|| logger` — a rejected push must not look like success.
CODE=$(curl -sS --max-time 20 ${NTFY_CURL_OPTS} \
  -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${NTFY_TOKEN}" \
  -H "Title: $(hostname): ${UNIT} failed" \
  -H "Priority: high" \
  -H "Tags: rotating_light" \
  -d "result=${STATE}

${DETAIL}" \
  "${NTFY_ALERT_URL}" 2>/dev/null) || CODE="curl-error"

if [ "$CODE" = "200" ]; then
    logger -t ntfy-alert "alert pushed for ${UNIT} (result=${STATE})"
else
    logger -t ntfy-alert "FAILED to push alert for ${UNIT}: HTTP ${CODE}"
fi
