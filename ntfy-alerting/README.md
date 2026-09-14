# ntfy Failure Alerting

A shared systemd `OnFailure=` handler that pushes unit failures to ntfy. Deployed identically on both Pi nodes; only the ntfy URL differs.

## Why

Both nodes run unattended `oneshot` units on timers. Before this existed, a failed run was silent: the June 2026 certificate lapse only surfaced because the Snowflake notifier stopped delivering, and the August 2026 renewal landed roughly an hour before expiry with nobody watching. A failing unit should reach a phone, not wait to be discovered.

## What it covers

| Node | Units wired to the alert |
|---|---|
| Pi 1 | `tailscale-cert-renew.service`, `exit-node-routes.service` |
| Pi 2 | `tailscale-cert-renew.service`, `snowflake-notifier.service` |

**What it does not cover:** a unit that never runs at all. A disabled timer, or a powered-off node, produces no failure and therefore no alert. That gap needs an external check.

## Files

| File | Deploy to | Mode |
|---|---|---|
| `ntfy-alert.sh` | `/usr/local/bin/ntfy-alert.sh` | `0755` root:root |
| `ntfy-alert@.service` | `/etc/systemd/system/ntfy-alert@.service` | `0644` |
| `ntfy-alert.env.example` | `/etc/ntfy-alert.env` | `0600` root:root |
| `onfailure.conf.example` | `/etc/systemd/system/<unit>.service.d/onfailure.conf` | `0644` |

## Setup

### 1. Dedicated ntfy token and topic

Alerts use their own topic (`homelab_alerts`) and their own token, separate from any application topic. A compromise of one credential does not reach the other.

```bash
# on the node running ntfy
docker exec <ntfy-container> ntfy token add admin
```

ntfy topics are implicit — no creation step. Subscribe to `homelab_alerts` in the ntfy client **before** relying on it; with `NTFY_AUTH_DEFAULT_ACCESS: deny-all`, an unauthenticated or missing subscription receives nothing and reports no error.

### 2. Credentials file

Copy `ntfy-alert.env.example` to `/etc/ntfy-alert.env`, fill in the token, and `chmod 600`.

**The node hosting ntfy** should reach it over loopback with certificate validation disabled:

```bash
NTFY_ALERT_URL="https://127.0.0.1:8443/homelab_alerts"
NTFY_CURL_OPTS="-k"
```

This is the point of the whole design. The alert path must not depend on the certificate or the tailnet, because those are exactly the things whose failure needs reporting.

**Other nodes** reach it over the tailnet normally:

```bash
NTFY_ALERT_URL="https://<hostname>.<tailnet>.ts.net/ntfy/homelab_alerts"
NTFY_CURL_OPTS=""
```

### 3. Script and template unit

```bash
sudo install -m 755 ntfy-alert.sh /usr/local/bin/ntfy-alert.sh
sudo install -m 644 ntfy-alert@.service /etc/systemd/system/ntfy-alert@.service
sudo systemctl daemon-reload
sudo bash -n /usr/local/bin/ntfy-alert.sh && echo "syntax OK"
```

### 4. Wire units via drop-ins

```bash
for u in tailscale-cert-renew exit-node-routes; do
  sudo mkdir -p /etc/systemd/system/$u.service.d
  printf '[Unit]\nOnFailure=ntfy-alert@%%N.service\n' \
    | sudo tee /etc/systemd/system/$u.service.d/onfailure.conf > /dev/null
done
sudo systemctl daemon-reload
systemctl show tailscale-cert-renew.service -p OnFailure
```

Expect `OnFailure=ntfy-alert@tailscale-cert-renew.service`.

Use `%N`, not `%n`. `%n` includes the `.service` suffix, producing the malformed instance `ntfy-alert@tailscale-cert-renew.service.service`, which systemd rejects at start time rather than at `daemon-reload`. `%N` is the same name without the suffix.

Drop-ins rather than editing units directly: the original stays untouched, reverting is `rm` plus `daemon-reload`, and a later rewrite of the unit will not silently drop the setting.

### 5. Test

`systemd-run --property=OnFailure=...` does **not** work — specifiers are not expanded in transient properties and the call fails with `Invalid unit name`. Use a real unit:

```bash
sudo tee /etc/systemd/system/alert-test.service > /dev/null << 'EOF'
[Unit]
Description=Deliberate failure to test ntfy alerting
OnFailure=ntfy-alert@%N.service

[Service]
Type=oneshot
ExecStart=/bin/false
EOF

sudo systemctl daemon-reload
sudo systemctl start alert-test.service     # reports failure — that is the test
sleep 3
journalctl -t ntfy-alert --no-pager --since "2 min ago"
```

Expect `alert pushed for alert-test (result=exit-code)` and a high-priority push on the phone.

To confirm messages reached the server independently of client delivery:

```bash
sudo bash -c 'source /etc/ntfy-alert.env; curl -s ${NTFY_CURL_OPTS} \
  -H "Authorization: Bearer $NTFY_TOKEN" \
  "https://127.0.0.1:8443/homelab_alerts/json?poll=1"' | tail -3
```

Clean up:

```bash
sudo rm /etc/systemd/system/alert-test.service && sudo systemctl daemon-reload
```

## Design notes

**`set -uo pipefail`, deliberately without `-e`.** An alert script that aborts partway is worse than one that pushes an incomplete message. Every path still logs.

**Explicit HTTP code check.** `curl` without `--fail` exits 0 on a 401 or 404, so `cmd || logger` never fires and a rejected push looks identical to a delivered one. The script captures `%{http_code}` and logs the outcome either way. This is the same silent-failure pattern that caused the original certificate lapse.

**`tail -c 2500` on the journal excerpt.** ntfy enforces a 4096-byte message body limit; the title, result line and headers consume the rest.

**iOS delivery.** Self-hosted ntfy cannot use Apple's push service directly. `NTFY_UPSTREAM_BASE_URL: "https://ntfy.sh"` forwards a wake-up poll so the app fetches the real message from the local server. The bridge is per-topic and only engages once the client is subscribed, so messages published before subscribing may never appear.

---

## Cross-Node Watchdog

`OnFailure=` only fires when a unit *runs and fails*. It cannot see a unit that never runs: a disabled timer, a crashed scheduler, a node that is simply off. Nothing fails, so nothing alerts. The September 2026 node-key expiry on Pi 2 was exactly this shape — eleven days from taking out the Funnel, Open WebUI, the notifier's ntfy path and `tailscale cert` itself, with no failing unit anywhere.

Each node checks the **other** daily and warns below `WARN_DAYS` (default 21).

| Check | Source | Verdict |
|---|---|---|
| Peer present in the tailnet | `tailscale status --json` | Problem if absent |
| Node key expiry | Peer's `KeyExpiry` | Problem below threshold |
| HTTPS cert expiry | TLS handshake on peer `:443` | Problem below threshold |
| Round-trip time | `tailscale ping <ip>` | Diagnostic only, never a verdict |

### Liveness comes from the TLS handshake, not from ping

The first version used `tailscale ping "$PEER_FQDN"` for reachability. It returns `no matching peer` and exits 1 for a **full MagicDNS FQDN**, while accepting the short hostname or the tailnet IP:

```
$ tailscale ping <peer>.<tailnet>.ts.net   →  no matching peer   (exit 1)
$ tailscale ping <peer>                    →  pong ... in 1ms    (exit 0)
$ tailscale ping 100.x.y.z                 →  pong ... in 1ms    (exit 0)
```

On one node this produced `UNREACHABLE` in the same run that went on to complete a TLS handshake with that peer and read its certificate — two contradictory results, and a false-positive alert on day one. A monitoring check that cries wolf trains you to ignore the topic, which is worse than having no check.

The fix was structural rather than a tuning change. The script already resolves the peer's tailnet IP and opens `:443`; a completed handshake is **stronger** evidence of liveness than a disco ping, and costs no extra round trip. Ping survives only as a logged diagnostic that cannot contribute to the verdict.

### Exit codes

Exit **0** when a peer problem was found *and the alert was delivered* — reporting is the script doing its job, and a non-zero exit there would make `OnFailure=` fire a second, duplicate alert. Exit **1** only when the alert itself could not be sent. `OnFailure=` therefore covers precisely the cases the script cannot report on its own: a crash, a missing env file, a rejected push.

### Known limitation

Pi 1 hosts ntfy. If Pi 1 dies completely, Pi 2 detects it but cannot tell you, because the alert path runs through Pi 1. Degraded states and any Pi 2 failure are covered; total loss of Pi 1 needs an external check. This is named rather than papered over.

### Files

| File | Deploy to | Mode |
|---|---|---|
| `homelab-watchdog.sh` | `/usr/local/bin/homelab-watchdog.sh` | `0755` root:root |
| `homelab-watchdog.service` | `/etc/systemd/system/` | `0644` |
| `homelab-watchdog.timer` | `/etc/systemd/system/` | `0644` |
| `homelab-watchdog.env.example` | `/etc/homelab-watchdog.env` | `0600` root:root |

Each node's env file points at the **other** node. Credentials are reused from `/etc/ntfy-alert.env`.

### Verify the alert path

A watchdog that has never alerted is untested. Force a warning by raising the threshold above the real remaining days, then put it back:

```bash
sudo sed -i 's/^WARN_DAYS=.*/WARN_DAYS=200/' /etc/homelab-watchdog.env
sudo systemctl start homelab-watchdog.service
journalctl -t homelab-watchdog --no-pager --since "1 min ago" | tail -2
sudo sed -i 's/^WARN_DAYS=.*/WARN_DAYS=21/' /etc/homelab-watchdog.env
```

Expect `N problem(s), alert pushed` and a push on the phone. Then enable:

```bash
sudo systemctl enable --now homelab-watchdog.timer
systemctl list-timers | grep watchdog
```
