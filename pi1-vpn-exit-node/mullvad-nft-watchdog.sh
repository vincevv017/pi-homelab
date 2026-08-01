#!/bin/bash
# Watchdog for exit node runtime state.
#
# Guards against two independent events, either of which silently breaks
# exit node traffic without restarting exit-node-routes.service:
#
#   1. Mullvad reconnect / location switch / package upgrade
#      → wipes all custom nft rules and can clear the split-tunnel list
#   2. tailscaled restart (package upgrade)
#      → recreates tailscale0, destroying the device-bound return route
#        and resetting the per-interface rp_filter
#
# nft and ip errors are sent to the journal rather than /dev/null: a failed
# re-apply must be distinguishable from a successful one.

MULLVAD_TABLE=1836018789   # Mullvad's routing table (created dynamically on connect)

while true; do
    # --- Check tailscale0 output rule ---
    if ! nft list chain inet mullvad output 2>/dev/null | grep -q 'tailscale0'; then
        # Rules were wiped — re-apply all four
        {
            nft add rule inet mullvad input iifname "tailscale0" accept
            nft insert rule inet mullvad output oifname "tailscale0" accept
            nft insert rule inet mullvad forward iifname "tailscale0" accept
            nft insert rule inet mullvad forward oifname "tailscale0" accept
        } 2>&1 | logger -t mullvad-nft-watchdog
        logger -t mullvad-nft-watchdog "re-applied tailscale0 nft rules"
    fi

    # --- Check split-tunnel mark rule ---
    # Mullvad's output chain only accepts 0x6d6f6c65-marked traffic for its own
    # WireGuard handshake. This rule ensures all split-tunnel traffic (including
    # tailscaled's TCP to login.tailscale.com) can exit via eth0.
    if ! nft list chain inet mullvad output 2>/dev/null | grep -q 'meta mark 0x6d6f6c65 accept'; then
        nft insert rule inet mullvad output meta mark 0x6d6f6c65 accept \
            2>&1 | logger -t mullvad-nft-watchdog
        logger -t mullvad-nft-watchdog "re-applied split-tunnel mark rule"
    fi

    # --- Check Tailscale return route and rp_filter ---
    # tailscaled restarts (package upgrades) recreate tailscale0. The kernel
    # deletes device-bound routes along with the old interface, and per-interface
    # sysctls reset to the 'default' value. Without the return route, replies
    # arriving on wg0-mullvad find only "default dev wg0-mullvad" in Mullvad's
    # table and loop back into the tunnel instead of reaching the client.
    # The ip link guard avoids error spam during the restart window.
    if ip link show tailscale0 >/dev/null 2>&1; then
        if ! ip route show table "$MULLVAD_TABLE" 2>/dev/null | grep -q '100.64.0.0/10'; then
            ip route add 100.64.0.0/10 dev tailscale0 table "$MULLVAD_TABLE" \
                2>&1 | logger -t mullvad-nft-watchdog
            logger -t mullvad-nft-watchdog "re-applied Tailscale return route"
        fi
        if [ "$(cat /proc/sys/net/ipv4/conf/tailscale0/rp_filter 2>/dev/null)" != "0" ]; then
            sysctl -qw net.ipv4.conf.tailscale0.rp_filter=0
            logger -t mullvad-nft-watchdog "reset tailscale0 rp_filter to 0"
        fi
    fi

    # --- Check split-tunnel PID ---
    # Mullvad updates or reconnects can silently clear the split-tunnel list,
    # and a tailscaled restart changes the PID. Either way, if tailscaled is not
    # excluded it can't reach the coordination server.
    TSPID=$(pgrep tailscaled || true)
    if [ -n "$TSPID" ]; then
        if ! mullvad split-tunnel list | grep -q "$TSPID"; then
            mullvad split-tunnel clear 2>/dev/null || true
            mullvad split-tunnel add "$TSPID" 2>/dev/null || true
            logger -t mullvad-nft-watchdog "re-applied split-tunnel for tailscaled PID $TSPID"
        fi
    fi

    sleep 5
done
