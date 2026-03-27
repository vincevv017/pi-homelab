# Raspberry Pi 5 — Secure VPN Exit Node & Personal Cloud

Complete guide for building a self-hosted VPN exit node and personal cloud storage on a Raspberry Pi 5 with NVMe boot, routing all your devices through Mullvad VPN via a Tailscale mesh network, and running Nextcloud for file sync and storage.

**Result:** All your iOS/macOS devices route internet traffic through a controlled exit point on infrastructure you own, with encrypted DNS and zero port forwarding required. Plus a private Nextcloud instance accessible from all your devices via Tailscale — your own alternative to OneDrive, iCloud, or Google Drive, with data that never leaves your network.

## Key Concepts

> **Placeholders used in this guide**
>
> | Placeholder | What it is | How to find it |
> |---|---|---|
> | `YOUR-PI1-HOSTNAME` | Pi 1's hostname (set during OS flash) | `hostname` on the Pi |
> | `your-tailnet` | Your Tailscale network name | Tailscale Admin → DNS tab — the part before `.ts.net` in your machine FQDNs |
> | `100.x.x.x` | Pi 1's Tailscale IP | `tailscale ip` on the Pi, or Tailscale Admin → Machines |
> | `100.y.y.y` | A client device Tailscale IP (e.g. iPhone) | `tailscale ip` on that device |
> | `YOUR_USERNAME` | Your Pi OS username | Set during Raspberry Pi Imager setup |
> | `YOUR_ACCOUNT_NUMBER` | Your Mullvad account number | [account.mullvad.net](https://account.mullvad.net) |
>
> **Getting your full Tailscale hostname (needed for TLS certs):**
> ```bash
> tailscale status --self --json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))"
> # e.g. → vpi5.your-tailnet.ts.net
> ```

**What is WireGuard?** WireGuard is the VPN protocol underlying both Tailscale and Mullvad. It establishes encrypted tunnels between endpoints using modern cryptography. Compared to older protocols like OpenVPN or IPSec, WireGuard is significantly faster, simpler to audit, and has a minimal codebase that reduces attack surface.

**What is Tailscale?** Tailscale is a mesh networking tool built on WireGuard. It creates a persistent private network between your devices — phone, laptop, Pi — that works transparently across any network: home, office, or remote. Each device receives a stable IP in the `100.x.x.x` range and can reach any other device on your Tailscale network with end-to-end encryption, without requiring port forwarding or changes to your router configuration.

**What is an exit node?** An exit node is a Tailscale device through which other devices can route their outbound internet traffic. When a client device selects the Pi as its exit node, all internet-bound traffic is tunnelled to the Pi first, then forwarded onward. This gives you a consistent, controlled egress point regardless of where the client device is physically located — useful for maintaining predictable network behaviour across locations and enforcing a uniform outbound path for all your devices.

**What is Mullvad VPN?** Mullvad is a VPN provider based in Sweden operating on the WireGuard protocol. In this setup, Mullvad handles the outbound leg from the Pi to the internet: traffic exits via Mullvad's infrastructure rather than directly through your home ISP, giving you a stable exit IP, an encrypted last-mile connection, and a clean separation between your home network and your outbound traffic. Mullvad operates on an account-number model with no personally identifying information required.

**Why combine Tailscale + Mullvad?** Tailscale alone gives you secure, encrypted access to your Pi from anywhere, but your internet traffic still exits via your home ISP. Mullvad alone works on one device at a time and requires per-device configuration. Combined: a single Mullvad subscription on the Pi covers all your Tailscale-connected devices simultaneously, traffic management is handled centrally at the infrastructure level rather than on each client, and the entire stack runs on hardware you control.

**What is DNS and why does it matter?** DNS (Domain Name System) translates domain names like `example.com` into IP addresses. DNS queries travel separately from page content and, without explicit routing, can bypass the VPN tunnel — revealing query patterns to whichever resolver receives them. This setup routes all DNS through Mullvad's resolver (`10.64.0.1`), keeping DNS traffic inside the encrypted path alongside the rest of your traffic.

---

## Hardware

| Component | Model |
|---|---|
| Board | Raspberry Pi 5 (4GB RAM) |
| Storage | EDILOCA EN605 256GB NVMe SSD (PCIe Gen3 x4, M.2 2280) |
| HAT | Official Raspberry Pi HAT+ (Model A) with PCIe M.2 connector |
| Cooling | Active Cooler (temperature-controlled fan) |
| Case | Metal enclosure |
| Power | Official 27W USB-C Power Supply |
| SD Card | 64GB microSD (initial setup only) |
| Network | Ethernet cable + Internet router (fiber) |

**Why NVMe over SD?** 10x faster read/write, far more reliable under constant workloads, 256GB capacity, and better suited for running services like VPN routing and Docker.

---

## Phase 1: Initial SD Card Setup

### 1.1 Flash Raspberry Pi OS

On your Mac/PC:

1. Download **Raspberry Pi Imager**: https://www.raspberrypi.com/software/
2. Insert microSD card
3. In Imager:
   - **OS**: Raspberry Pi OS Lite (64-bit) — Bookworm
   - **Storage**: Your microSD card
   - **Settings** (gear icon):
     - Hostname: `vpi5`
     - Username: your choice (e.g. `vincepi`)
     - Password: set a strong password
     - WiFi: configure if needed (Ethernet recommended)
     - Enable SSH: yes, use password authentication
     - Locale: set your timezone and keyboard layout
4. Click **Write** and wait for completion

### 1.2 First Boot

1. Remove SD from computer
2. Insert SD into Pi
3. **Do NOT connect NVMe HAT yet** — SD boot only for now
4. Connect Ethernet cable to Pi and router
5. Power on Pi
6. Wait 2–3 minutes for first boot

### 1.3 Find and Connect via SSH

```bash
# Try hostname first
ssh username@vpi5.local

# Or scan network for the Pi's IP
nmap -sn 192.168.1.0/24

# Connect via IP
ssh username@192.168.1.XX
```

Accept the host key fingerprint (type `yes`), then enter your password.

### 1.4 Initial Updates

```bash
sudo apt update
sudo apt full-upgrade -y

# Update EEPROM firmware (critical for NVMe boot)
sudo rpi-eeprom-update -a
sudo reboot
```

Reconnect after reboot: `ssh username@vpi5.local`

### 1.5 Assign a Static IP on Your Router

Before going further, assign the Pi a fixed IP on your LAN. Without this, the router can hand it a different IP after a reboot or DHCP lease expiry — breaking SSH connections, firewall rules, and any service that references `192.168.1.50` directly.

This is done via a DHCP reservation on the router: the Pi keeps using DHCP, but the router always hands it the same address based on its MAC address.

**Step 1 — Get the Pi's MAC address:**

```bash
ip link show eth0
# Look for the line: link/ether xx:xx:xx:xx:xx:xx
# Example: link/ether dc:a6:32:xx:xx:xx
```

**Step 2 — Log into your router's admin interface:**

From your Mac or iPhone browser:
1. Go to `http://192.168.1.1` (typical default — check your router's label if different)
2. Log in with your admin credentials (usually printed on the router's label)

**Step 3 — Create the DHCP reservation:**

The exact UI varies by router model. Look for a section labelled **DHCP**, **LAN**, or **Network** in your router's admin panel, then find **Static Leases**, **Address Reservation**, or similar. Create a new entry with:
   - **MAC address**: the `dc:a6:32:xx:xx:xx` value from Step 1
   - **IP address**: `192.168.1.50`
   - **Name / Label** (optional): `vpi5`

Save and apply.

**Step 4 — Verify:**

```bash
sudo reboot
```

After reconnecting:

```bash
ip addr show eth0 | grep "inet "
# Should show: inet 192.168.1.50/24
```

From this point all documentation and firewall rules reference `192.168.1.50` as the Pi's permanent LAN address.

---

## Phase 2: NVMe Boot Configuration

### 2.1 Configure Boot Order (Before Connecting HAT)

```bash
sudo -E rpi-eeprom-config --edit
```

Set:

```
BOOT_ORDER=0xf41
```

Boot order is read **right to left**: `1` = SD card first, `4` = USB second, `f` = restart loop. This ensures the Pi boots reliably from SD while we set up the NVMe.

Save (Ctrl+X, Y, Enter), then:

```bash
sudo reboot
```

### 2.2 Connect NVMe HAT

After reboot and SSH reconnection:

```bash
sudo poweroff
```

Physical installation:

1. Unplug power completely
2. Insert M.2 NVMe into HAT at 30° angle, press down flat, secure with screw
3. Connect HAT to Pi via ribbon cable/PCIe connector (both ends firmly seated)
4. Power on, wait 2 minutes, SSH in

### 2.3 Verify NVMe Detection

```bash
lspci
# Should show: "Non-Volatile memory controller"

lsblk
# Should show: nvme0n1 (238.5G or similar)
```

### 2.4 Clone SD Card to NVMe

```bash
# Wipe NVMe
sudo wipefs -a /dev/nvme0n1

# Clone SD to NVMe (15–20 minutes)
sudo dd if=/dev/mmcblk0 of=/dev/nvme0n1 bs=4M status=progress conv=fsync
```

### 2.5 Expand NVMe Partition

```bash
sudo parted /dev/nvme0n1 resizepart 2 100%
sudo e2fsck -f /dev/nvme0n1p2
sudo resize2fs /dev/nvme0n1p2

# Verify
lsblk
# nvme0n1p2 should show ~238G
```

### 2.6 Update Boot Order to NVMe-First

```bash
sudo -E rpi-eeprom-config --edit
```

Change to:

```
BOOT_ORDER=0xf416
```

Read right to left: `6` = NVMe first, `1` = SD card fallback, `4` = USB fallback, `f` = restart loop.

```bash
sudo reboot
```

### 2.7 Verify NVMe Boot

```bash
ssh username@vpi5.local
df -h
```

You should see `/dev/nvme0n1p2` mounted as `/` with ~235GB total. The SD card can now be removed and kept as an emergency backup.

---

## Phase 3: Security Hardening

### 3.1 SSH Key Authentication

On your Mac/PC:

```bash
# Generate key if you don't have one (skip if already done for a previous Pi)
ssh-keygen -t ed25519 -C "pi-access"

# Copy key to this Pi
ssh-copy-id username@vpi5.local
```

Test: `exit` then `ssh username@vpi5.local` — should connect without a password.

**Setting up a second Pi?** Skip `ssh-keygen` — your key pair already exists at `~/.ssh/id_ed25519`. Just copy the same public key to the new Pi using its static IP:

```bash
# Pi 2 example
ssh-copy-id username@192.168.1.51
```

One key pair covers all your Pis. The private key stays on your Mac; the public key gets added to each Pi's `~/.ssh/authorized_keys`.

### 3.2 Disable Password Authentication

On the Pi:

```bash
sudo nano /etc/ssh/sshd_config
```

Set:

```
PasswordAuthentication no
PubkeyAuthentication yes
```

```bash
sudo systemctl restart sshd
```

**⚠️ Test in a NEW terminal before closing your current session!**

### 3.3 UFW Firewall

```bash
sudo apt install ufw -y
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp comment 'SSH'
sudo ufw allow 41641/udp comment 'Tailscale'
sudo ufw enable
sudo ufw status verbose
```

### 3.4 Fail2ban

```bash
sudo apt install fail2ban -y
```

Create a jail override for SSH (do **not** copy `jail.conf` to `jail.local` — the defaults work, and copying the full file risks duplicate section errors that crash fail2ban):

```bash
sudo nano /etc/fail2ban/jail.d/sshd.conf
```

Paste:

```
[sshd]
enabled = true
port = ssh
maxretry = 3
bantime = 3600
findtime = 600
```

**Note:** No `logpath` is needed — Bookworm uses journald by default, and fail2ban detects this automatically. Adding `logpath = /var/log/auth.log` would break on systems where rsyslog isn't installed.

**⚠️ If you previously copied `jail.conf` to `jail.local`:** Check for duplicate `[sshd]` sections, which crash fail2ban with `section 'sshd' already exists`. Either delete `jail.local` entirely (the defaults plus your `jail.d/sshd.conf` override are sufficient), or ensure only one `[sshd]` section exists in the file:

```bash
# Check for duplicates
grep -n "\[sshd\]" /etc/fail2ban/jail.local
# If two lines appear, remove the duplicate block
```

```bash
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
sudo fail2ban-client status sshd
```

### 3.5 System Updates

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt autoremove -y
```

---

## Phase 4: Mullvad VPN Installation

### 4.1 Install Mullvad

```bash
# Download Mullvad for ARM64
curl -fsSL https://mullvad.net/en/download/app/deb/latest -o mullvad.deb

# Install
sudo apt install -y ./mullvad.deb
rm mullvad.deb
```

### 4.2 Disable NetworkManager DNS Management

Mullvad needs to own `/etc/resolv.conf` — it writes `nameserver 10.64.0.1` every time it connects, ensuring the Pi uses its private DNS and nothing else. By default, NetworkManager also manages this file and will overwrite it with the router's IP (`192.168.1.1`) on reconnects. Mullvad's nftables firewall blocks DNS to anything except `10.64.0.1`, so if NetworkManager wins the race, all hostname resolution on the Pi breaks — including `apt update`.

The fix is to tell NetworkManager to leave the file alone:

```bash
sudo nano /etc/NetworkManager/NetworkManager.conf
```

Add `dns=none` under `[main]`:

```ini
[main]
plugins=ifupdown,keyfile
dns=none
```

Apply:

```bash
sudo systemctl restart NetworkManager
```

Verify NetworkManager is no longer managing DNS (the file should still show the router IP for now — Mullvad will overwrite it when it connects in the next step):

```bash
cat /etc/NetworkManager/NetworkManager.conf | grep dns
# Should show: dns=none
```

**⚠️ Never use `chattr +i` to make `/etc/resolv.conf` immutable.** Locking the file prevents Mullvad from writing its DNS on connect, causing it to enter "Blocked: Failed to set system DNS server" state at boot — which breaks DNS for the Pi and causes `exit-node-routes.service` to fail. `dns=none` is the correct solution: it stops NetworkManager from overwriting the file while leaving Mullvad free to write it.

### 4.3 Configure Mullvad

```bash
# Log in with your Mullvad account number
mullvad account login YOUR_ACCOUNT_NUMBER

# Set server to Switzerland (Zurich)
mullvad relay set location ch zrh

# Enable LAN sharing (critical — without this you lose SSH access)
mullvad lan set allow

# Enable auto-connect on boot
mullvad auto-connect set on

# Connect
mullvad connect

# Verify
mullvad status
# Should show: Connected, Switzerland, Zurich
curl -s ifconfig.me
# Should show Swiss IP

# Confirm Mullvad now owns resolv.conf
cat /etc/resolv.conf
# Should show: nameserver 10.64.0.1
```

**⚠️ CRITICAL: Always enable LAN sharing BEFORE connecting.** Without it, Mullvad blocks all LAN traffic including SSH, locking you out of the Pi.

---

## Phase 5: Tailscale Installation

### 5.1 Install Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

### 5.2 Configure Tailscale

```bash
# Start Tailscale with exit node and manual firewall mode
sudo tailscale up \
  --advertise-exit-node \
  --accept-routes \
  --advertise-routes=192.168.1.0/24 \
  --accept-dns=false \
  --netfilter-mode=off
```

Follow the authentication URL printed in the terminal to log in.

**Key flags explained:**

| Flag | Purpose |
|---|---|
| `--advertise-exit-node` | Makes this Pi available as a VPN exit point |
| `--accept-routes` | Accepts routes from other Tailscale nodes |
| `--advertise-routes=192.168.1.0/24` | Shares LAN access through Tailscale |
| `--accept-dns=false` | Pi keeps its own DNS (doesn't use Tailscale's) |
| `--netfilter-mode=off` | We manage iptables ourselves for Mullvad compatibility |

### 5.3 Approve Exit Node in Admin Panel

1. Go to https://login.tailscale.com/admin/machines
2. Find your Pi (`vpi5`)
3. Click the `...` menu → **Edit route settings**
4. Enable **"Use as exit node"**

### 5.4 Split-Tunnel Tailscale from Mullvad

Tailscale's daemon needs to reach the Tailscale coordination server, which it can't do through Mullvad. Exclude it:

```bash
# Find tailscaled PID
pgrep tailscaled

# Exclude from Mullvad
mullvad split-tunnel add $(pgrep tailscaled)
```

**Note:** The PID changes on reboot, and Mullvad updates or reconnects can silently clear the split-tunnel list entirely. The boot script (Phase 6) re-applies it at startup, and the watchdog (Phase 6.4) re-applies it at runtime if it's ever cleared.

---

## Phase 6: Routing Configuration

This is the critical phase that ties everything together. Traffic must flow:

```
iPhone → Tailscale → Pi (tailscale0) → Mullvad (wg0-mullvad) → Internet (Swiss IP)
          ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← (return path)
```

### 6.1 IP Forwarding

```bash
sudo nano /etc/sysctl.conf
```

Add (or uncomment):

```
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
```

Apply:

```bash
sudo sysctl -p
```

### 6.2 The Setup Script

Create the routing script that configures everything:

```bash
sudo nano /usr/local/bin/exit-node-setup.sh
```

Paste:

```bash
#!/bin/bash
set -e

# ============================================================
# Exit Node Setup: Tailscale traffic → Mullvad VPN
# ============================================================

# --- IP Forwarding (enforce even if sysctl.conf is loaded) ---
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv6.conf.all.forwarding=1
sysctl -w net.ipv4.conf.tailscale0.rp_filter=0
sysctl -w net.ipv4.conf.all.rp_filter=0

# --- Tailscale flags ---
tailscale up \
  --advertise-exit-node \
  --accept-routes \
  --advertise-routes=192.168.1.0/24 \
  --accept-dns=false \
  --netfilter-mode=off

# --- Split-tunnel: exclude tailscaled from Mullvad ---
TSPID=$(pgrep tailscaled || true)
if [ -n "$TSPID" ]; then
  mullvad split-tunnel add $TSPID 2>/dev/null || true
fi

# --- Return route: Tailscale IPs go back via tailscale0 ---
# Without this, Mullvad's routing table swallows return packets
# (table 1836018789 has "default dev wg0-mullvad" only)
ip route add 100.64.0.0/10 dev tailscale0 table 1836018789 2>/dev/null || true

# --- FORWARD rules: allow Tailscale traffic through ---
iptables -I FORWARD 1 -o tailscale0 -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -I FORWARD 2 -i tailscale0 -j ACCEPT

# --- Mangle: mark tailscale0 traffic for Mullvad routing ---
iptables -t mangle -A PREROUTING -i tailscale0 -j MARK --set-mark 0x100

# --- NAT: masquerade outgoing traffic on Mullvad tunnel ---
iptables -t nat -A POSTROUTING -o wg0-mullvad -j MASQUERADE

# --- DNS DNAT: redirect all client DNS to Mullvad's DNS ---
# Mullvad's nftables firewall rejects DNS to anything except 10.64.0.1
# This redirects iPhone's DNS (e.g. 8.8.8.8) to Mullvad's server
iptables -t nat -I PREROUTING 1 -i tailscale0 -p udp --dport 53 -j DNAT --to-destination 10.64.0.1
iptables -t nat -I PREROUTING 2 -i tailscale0 -p tcp --dport 53 -j DNAT --to-destination 10.64.0.1

# --- Mullvad nftables: allow Tailscale traffic ---
# Mullvad regenerates its nftables rules on every reconnect with policy drop
# on input, output, and forward chains. Without these rules, Mullvad blocks:
# - Incoming connections from Tailscale peers (input chain)
# - Responses to Tailscale peers (output chain drops CGNAT 100.64.0.0/10)
# - Docker container responses to Tailscale peers (forward chain)
nft add rule inet mullvad input iifname "tailscale0" accept
nft insert rule inet mullvad output oifname "tailscale0" accept
nft insert rule inet mullvad forward iifname "tailscale0" accept
nft insert rule inet mullvad forward oifname "tailscale0" accept

# --- Mullvad nftables: allow split-tunnel traffic to exit via eth0 ---
# Mullvad marks split-tunneled processes with 0x6d6f6c65 ("mole") and its
# default output chain only accepts that mark for its own WireGuard handshake
# packet (one specific UDP rule). All other 0x6d6f6c65-marked traffic —
# including tailscaled's TCP connections to login.tailscale.com — hits
# policy drop. This rule accepts all split-tunnel-marked outbound packets,
# allowing tailscaled to reach the coordination server via eth0 directly.
nft insert rule inet mullvad output meta mark 0x6d6f6c65 accept

echo "Exit node routes configured successfully"
```

```bash
sudo chmod +x /usr/local/bin/exit-node-setup.sh
```

### 6.3 The Systemd Service

Create a service that waits for Mullvad and Tailscale before applying routes:

```bash
sudo nano /etc/systemd/system/exit-node-routes.service
```

Paste:

```ini
[Unit]
Description=Exit node routing: Tailscale via Mullvad VPN
After=mullvad-daemon.service tailscaled.service network-online.target
Wants=mullvad-daemon.service tailscaled.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/bin/bash -c 'for i in $(seq 1 30); do mullvad status | grep -q "Connected" && exit 0; sleep 2; done; exit 1'
ExecStartPre=/bin/bash -c 'for i in $(seq 1 15); do ip link show tailscale0 >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1'
ExecStart=/usr/local/bin/exit-node-setup.sh

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable exit-node-routes.service
```

### 6.4 Mullvad nftables Watchdog

The boot script (6.2) applies the nftables rules once at startup. However, Mullvad regenerates its nftables rules on every reconnect — including when you switch VPN locations. This wipes out the tailscale0 rules, breaking NextCloud and exit node traffic.

This watchdog service checks every 5 seconds and re-applies the rules if Mullvad has wiped them:

```bash
sudo nano /usr/local/bin/mullvad-nft-watchdog.sh
```

Paste:

```bash
#!/bin/bash
# Watches for Mullvad nftables regeneration and re-applies tailscale0 rules
# Also monitors split-tunnel PID drift and the mark rule for coordination server access
# Mullvad wipes all custom nft rules on every reconnect/location switch

while true; do
    # --- Check tailscale0 output rule ---
    if ! nft list chain inet mullvad output 2>/dev/null | grep -q 'tailscale0'; then
        # Rules were wiped — re-apply all four
        nft add rule inet mullvad input iifname "tailscale0" accept 2>/dev/null
        nft insert rule inet mullvad output oifname "tailscale0" accept 2>/dev/null
        nft insert rule inet mullvad forward iifname "tailscale0" accept 2>/dev/null
        nft insert rule inet mullvad forward oifname "tailscale0" accept 2>/dev/null
        logger "mullvad-nft-watchdog: re-applied tailscale0 nft rules"
    fi

    # --- Check split-tunnel mark rule ---
    # Mullvad's output chain only accepts 0x6d6f6c65-marked traffic for its own
    # WireGuard handshake. This rule ensures all split-tunnel traffic (including
    # tailscaled's TCP to login.tailscale.com) can exit via eth0.
    if ! nft list chain inet mullvad output 2>/dev/null | grep -q 'meta mark 0x6d6f6c65 accept'; then
        nft insert rule inet mullvad output meta mark 0x6d6f6c65 accept 2>/dev/null
        logger "mullvad-nft-watchdog: re-applied split-tunnel mark rule"
    fi

    # --- Check split-tunnel PID ---
    # Mullvad updates or reconnects can silently clear the split-tunnel list.
    # If tailscaled is not excluded, it can't reach the coordination server.
    TSPID=$(pgrep tailscaled || true)
    if [ -n "$TSPID" ]; then
        if ! mullvad split-tunnel list | grep -q "$TSPID"; then
            mullvad split-tunnel clear 2>/dev/null || true
            mullvad split-tunnel add "$TSPID" 2>/dev/null || true
            logger "mullvad-nft-watchdog: re-applied split-tunnel for tailscaled PID $TSPID"
        fi
    fi

    sleep 5
done
```

```bash
sudo chmod +x /usr/local/bin/mullvad-nft-watchdog.sh
```

Create the service:

```bash
sudo nano /etc/systemd/system/mullvad-nft-watchdog.service
```

Paste:

```ini
[Unit]
Description=Watchdog: re-apply Tailscale nft rules after Mullvad reconnects
After=mullvad-daemon.service tailscaled.service

[Service]
Type=simple
ExecStart=/usr/local/bin/mullvad-nft-watchdog.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable mullvad-nft-watchdog.service
sudo systemctl start mullvad-nft-watchdog.service
```

**Verify:** Switch Mullvad location, wait 10 seconds, then check the rules were re-applied:

```bash
mullvad relay set location nl ams && mullvad reconnect
sleep 10

# tailscale0 interface rules
sudo nft list chain inet mullvad output | grep tailscale0
# Should show: oifname "tailscale0" accept

# Split-tunnel mark rule
sudo nft list chain inet mullvad output | grep "0x6d6f6c65 accept"
# Should show: meta mark 0x6d6f6c65 accept

# Split-tunnel PID
mullvad split-tunnel list
# Should show the tailscaled PID

journalctl -u mullvad-nft-watchdog --no-pager --since "1 min ago"
# Should show re-applied messages for each rule that was wiped
```

---

## Phase 7: Client Configuration

### 7.1 iPhone / iPad

1. Install **Tailscale** from the App Store
2. Sign in with the same account used on the Pi
3. In Tailscale settings:
   - **Exit Node**: select `vpi5`
   - **Allow LAN Access**: ON
   - **DNS**: set to "Auto" (the Pi redirects all DNS to Mullvad automatically)
4. Browse to https://whatismyip.com — you should see the Swiss IP

### 7.2 macOS

1. Install Tailscale from https://tailscale.com/download or the Mac App Store
2. Sign in with the same account
3. Click the Tailscale menu bar icon → **Exit Node** → select `vpi5`
4. Verify at https://whatismyip.com

### 7.3 SSH from iOS (Terminus or other SSH apps)

With Tailscale running on your iPhone, you can SSH into the Pi from anywhere using an app like **Terminus**:

1. Install **Terminus** from the App Store
2. Transfer your SSH private key to the app (e.g. via AirDrop, iCloud, or paste into Terminus)
3. Create a new host:
   - Hostname: `100.x.x.x` (your Pi's Tailscale IP)
   - Username: your Pi username
   - Authentication: select your imported private key
   - Passphrase: enter your key's passphrase if you set one during `ssh-keygen`
4. Connect — full terminal access to your Pi from your pocket

**Note:** Since password authentication is disabled on the Pi (Phase 3.2), you must import your private key into the SSH app. The key's passphrase is not your Pi's login password — it's the passphrase you chose when generating the key pair with `ssh-keygen`.

---

## Phase 8: Verification & Testing

### 8.1 After Reboot Verification

```bash
sudo reboot
```

Wait 2 minutes, then SSH in and check:

```bash
# Service started successfully?
sudo systemctl status exit-node-routes.service --no-pager

# Mullvad connected to Zurich?
mullvad status

# Tailscale running with exit node?
sudo tailscale status

# Pi's own exit IP is Swiss?
curl -s ifconfig.me

# Routing is correct?
sudo ip route get 8.8.8.8 from 100.y.y.y iif tailscale0
# Should show: dev wg0-mullvad table 1836018789
```

### 8.2 On Your iPhone

1. Open Safari → https://whatismyip.com
2. Should show the Swiss IP
3. Try browsing various websites — everything should work
4. Check DNS leak: https://dnsleaktest.com — should show Mullvad DNS only

---

## Architecture Overview

```
┌──────────┐     Tailscale      ┌──────────────────────────┐     WireGuard     ┌──────────┐
│  iPhone   │ ──── encrypted ──→ │     Raspberry Pi 5       │ ──── encrypted ──→│ Mullvad  │
│  iPad     │     tunnel         │                          │     tunnel        │ Server   │
│  Mac      │ ←──────────────── │  tailscale0 → wg0-mullvad│ ←──────────────── │ Zurich   │
└──────────┘                    │                          │                    └──────────┘
     │                          │  Docker:                 │
     │   NextCloud access       │  ├─ Nginx (443)         │
     └──── via Tailscale ──────→│  ├─ NextCloud (8080)    │
                                │  └─ MariaDB             │
                                └──────────────────────────┘
                                         │
                                    192.168.1.50
                                    (LAN / SSH)
```

### Packet Flow (Outbound)

1. iPhone sends packet (e.g. HTTPS to google.com)
2. Tailscale encrypts → sends to Pi's `tailscale0` interface
3. Mangle PREROUTING marks packet with `0x100`
4. Routing rule matches mark → uses Mullvad's routing table
5. DNS packets get DNAT'd to `10.64.0.1` (Mullvad DNS)
6. FORWARD chain accepts the packet
7. NAT POSTROUTING masquerades source to Mullvad's `wg0-mullvad` IP
8. Packet exits through Mullvad tunnel with Swiss IP

### Packet Flow (Return)

1. Response arrives on `wg0-mullvad`
2. Conntrack reverse-NATs destination back to iPhone's Tailscale IP
3. Route `100.64.0.0/10 dev tailscale0` in Mullvad's table directs it to Tailscale
4. Tailscale encrypts → delivers to iPhone

### Key Technical Details

| Component | Detail |
|---|---|
| Mullvad routing table | `1836018789` — created dynamically when Mullvad connects |
| Tailscale CGNAT range | `100.64.0.0/10` — all Tailscale device IPs |
| Mullvad internal DNS | `10.64.0.1` — only DNS allowed by Mullvad's nftables firewall |
| Fwmark `0x100` | Custom mark to override Tailscale's default routing |
| Fwmark `0x6d6f6c65` | Mullvad's split-tunnel exclusion mark (ASCII for "mole") — traffic from excluded processes is routed via `eth0`, not `wg0-mullvad`. The nft output chain requires a broad `meta mark 0x6d6f6c65 accept` rule or this traffic is dropped by `policy drop`. |

### Mullvad nftables and Tailscale Compatibility

Mullvad creates its own nftables firewall (`table inet mullvad`) with `policy drop` on the input, output, and forward chains. It regenerates these rules on every reconnect. By default, Mullvad allows traffic to/from private IP ranges (10.x, 172.16.x, 192.168.x) and through its own `wg0-mullvad` interface — but it does **not** allow traffic on `tailscale0`, and Tailscale's CGNAT range (100.64.0.0/10) is not recognized as a private range.

This means:

- **Input chain:** The Pi can't accept incoming connections from Tailscale peers (they have 100.x.x.x addresses, not in Mullvad's allowed ranges)
- **Output chain:** The Pi can't send SYN-ACK responses to Tailscale peers
- **Forward chain:** Docker containers can't respond to Tailscale peers (packets traverse FORWARD, not INPUT)

The fix is four `nft` rules added by the boot script, which run after Mullvad connects:

```
nft add rule inet mullvad input iifname "tailscale0" accept
nft insert rule inet mullvad output oifname "tailscale0" accept
nft insert rule inet mullvad forward iifname "tailscale0" accept
nft insert rule inet mullvad forward oifname "tailscale0" accept
```

Because Mullvad regenerates its rules on every reconnect (including VPN location switches), a watchdog service (Phase 6.4) monitors the nftables rules every 5 seconds and re-applies them if Mullvad has wiped them.

Without these rules, only Tailscale SSH works (because Tailscale SSH is handled internally by the Tailscale daemon and never touches the kernel's network stack), while all regular TCP connections to services on the Pi time out silently.

---

## Changing VPN Location

To switch from Zurich to another location:

```bash
# List available countries
mullvad relay list

# Switch to, for example, Tokyo
mullvad relay set location jp tyo

# Or Amsterdam
mullvad relay set location nl ams

# Reconnect
mullvad reconnect

# Verify
mullvad status
curl -s ifconfig.me
```

The change takes effect immediately on all connected devices.

---

## Troubleshooting

### All Tailscale Devices Show Offline / Coordination Server Unreachable

**Symptom:** `sudo tailscale status` shows all devices as "offline" and includes a health check warning: `Tailscale hasn't received a network map from the coordination server`. The Mac shows "exit node offline, internet traffic is blocked". iPhone may still work temporarily on cached state.

**Cause:** Two things must be true simultaneously for `tailscaled` to reach `login.tailscale.com`: (1) it must be in Mullvad's split-tunnel exclusion list so its traffic bypasses the WireGuard tunnel and goes via `eth0` directly, and (2) Mullvad's nftables output chain must have a rule accepting all `0x6d6f6c65`-marked traffic. Mullvad assigns this mark to split-tunneled processes. The default output chain only accepts this mark for its own WireGuard handshake (one narrow UDP rule) — all other `0x6d6f6c65`-marked traffic, including `tailscaled`'s TCP connections, hits `policy drop`. Either condition being missing kills coordination server access.

**Diagnose:**

```bash
# Is the split-tunnel set?
mullvad split-tunnel list
# Should show tailscaled's PID. If empty — that's the problem.

# Is the mark rule present?
sudo nft list chain inet mullvad output | grep "0x6d6f6c65 accept"
# Should show: meta mark 0x6d6f6c65 accept (a broad rule, not just the UDP one)
```

**Fix:**

```bash
# Re-apply split-tunnel
mullvad split-tunnel clear
mullvad split-tunnel add $(pgrep tailscaled)
mullvad split-tunnel list

# Re-apply the mark rule if missing
sudo nft insert rule inet mullvad output meta mark 0x6d6f6c65 accept

# Wait 30 seconds
sleep 30
sudo tailscale status
# Health check warnings should clear
```

**Prevention:** The updated watchdog (Phase 6.4) monitors both conditions every 5 seconds and self-heals if either is cleared by a Mullvad reconnect or update. Ensure the watchdog is running:

```bash
sudo systemctl status mullvad-nft-watchdog --no-pager
journalctl -u mullvad-nft-watchdog --no-pager --since "5 min ago"
```



**Check 1:** Is Mullvad connected?
```bash
mullvad status
```
If disconnected: `mullvad connect`

**Check 2:** Is the exit-node-routes service running?
```bash
sudo systemctl status exit-node-routes.service
```
If failed: `sudo /usr/local/bin/exit-node-setup.sh`

**Check 3:** Is the return route in place?
```bash
ip route show table 1836018789
```
Should include `100.64.0.0/10 dev tailscale0`. If missing:
```bash
sudo ip route add 100.64.0.0/10 dev tailscale0 table 1836018789
```

**Check 4:** Is IP forwarding enabled?
```bash
sysctl net.ipv4.ip_forward
```
Should be `1`. If not: `sudo sysctl -w net.ipv4.ip_forward=1`

### Pi's Own DNS Broken — `apt update` Fails with "Temporary failure resolving"

**Symptom:** `sudo apt update` fails for all repos with DNS resolution errors. `curl -s https://ifconfig.me` returns nothing. `mullvad status` may show "Blocked: Failed to set system DNS server".

**Cause:** `dns=none` is missing from NetworkManager.conf (NetworkManager overwrote `/etc/resolv.conf` with the router's IP), or `/etc/resolv.conf` was made immutable with `chattr +i` (preventing Mullvad from writing `10.64.0.1` on connect). Either way, Mullvad's nftables blocks all DNS except `10.64.0.1`, so the Pi can't resolve any hostname. If Mullvad is in "Blocked" state, `exit-node-routes.service` also fails because its Connected pre-check times out.

**Diagnose:**

```bash
cat /etc/resolv.conf          # Should be: nameserver 10.64.0.1
mullvad status                # Should be: Connected
lsattr /etc/resolv.conf       # Should NOT show 'i' flag
cat /etc/NetworkManager/NetworkManager.conf | grep dns  # Should show: dns=none
```

**Fix:**

```bash
# Remove immutable flag if set
sudo chattr -i /etc/resolv.conf

# Ensure NetworkManager won't overwrite the file
sudo nano /etc/NetworkManager/NetworkManager.conf
# Add under [main]: dns=none
sudo systemctl restart NetworkManager

# Reconnect Mullvad so it writes 10.64.0.1
mullvad reconnect
sleep 5
cat /etc/resolv.conf          # Should now show: nameserver 10.64.0.1
curl -s https://ifconfig.me   # Should return your VPN exit IP

# Restart the exit-node service
sudo systemctl restart exit-node-routes.service
sudo systemctl status exit-node-routes.service --no-pager | grep Active
# Should show: active (exited)
```

**Prevention:** Phase 4.2 covers this during initial setup. If you're seeing this on an existing install, apply the `dns=none` fix above and it won't recur.

### DNS Not Resolving (Pages Hang Forever)

Mullvad's nftables firewall blocks DNS to all servers except `10.64.0.1`. Verify the DNAT rule exists:

```bash
sudo iptables -t nat -L PREROUTING -v -n
```

Should show DNAT rules redirecting port 53 to `10.64.0.1`. If missing:
```bash
sudo iptables -t nat -I PREROUTING 1 -i tailscale0 -p udp --dport 53 -j DNAT --to-destination 10.64.0.1
sudo iptables -t nat -I PREROUTING 2 -i tailscale0 -p tcp --dport 53 -j DNAT --to-destination 10.64.0.1
```

### Packets Go Out But Replies Never Come Back

Check the return route:
```bash
ip route get 100.y.y.y
```

If it shows `dev wg0-mullvad` instead of `dev tailscale0`, the return path is broken:
```bash
sudo ip route add 100.64.0.0/10 dev tailscale0 table 1836018789
```

### SSH Locked Out After Mullvad Connection

If you enabled Mullvad without LAN sharing:
1. Connect a USB keyboard and HDMI monitor
2. Log in locally
3. Run: `mullvad lan set allow`
4. Or: `mullvad disconnect`

### NVMe Not Detected

1. Power off completely
2. Reseat M.2 drive in HAT (30° angle, press down, secure with screw)
3. Ensure ribbon cable is firmly seated on both ends
4. Look for blue LED on HAT when powered on
5. After boot: `lspci` should show "Non-Volatile memory controller"

### Pi Won't Boot After EEPROM Update

1. Remove NVMe HAT completely
2. Boot from SD card only
3. Update EEPROM: `sudo rpi-eeprom-update -a && sudo reboot`
4. Set boot order to SD-first: `BOOT_ORDER=0xf41`
5. Reconnect HAT after successful boot

### Services Reachable via Tailscale SSH But Not TCP (Port 443, etc.)

**Symptom:** Tailscale SSH works, but `nc -zv 100.x.x.x 443` times out. tcpdump on the Pi shows SYN packets arriving but no SYN-ACK is sent.

**Cause:** Mullvad's nftables firewall has `policy drop` on the output and forward chains. Tailscale's CGNAT range (100.64.0.0/10) is not in Mullvad's list of allowed private IP ranges, so response packets are silently dropped. Tailscale SSH appears to work because it's handled internally by the Tailscale daemon, bypassing the kernel's network stack entirely.

**Fix:** Ensure the four nft rules are in the boot script and currently active:

```bash
# Check if the rules are in place
sudo nft list chain inet mullvad input | grep tailscale0
sudo nft list chain inet mullvad output | head -5
sudo nft list chain inet mullvad forward | head -5
# All should show tailscale0 accept rules

# If missing, re-apply:
sudo nft add rule inet mullvad input iifname "tailscale0" accept
sudo nft insert rule inet mullvad output oifname "tailscale0" accept
sudo nft insert rule inet mullvad forward iifname "tailscale0" accept
sudo nft insert rule inet mullvad forward oifname "tailscale0" accept
```

**Note:** Mullvad regenerates its nftables rules on every reconnect, so these must be in the boot script (`exit-node-setup.sh`). The watchdog service (Phase 6.4) handles runtime reconnects automatically — check if it's running:

```bash
sudo systemctl status mullvad-nft-watchdog
journalctl -u mullvad-nft-watchdog --no-pager --since "5 min ago"
```

### NextCloud Unreachable From Tailscale Devices

**Check 1:** Are Docker containers running?
```bash
docker ps
# Should show nginx, nextcloud, and db containers all "Up"
```

**Check 2:** Is nginx responding locally?
```bash
curl -k https://127.0.0.1
# Should return NextCloud HTML or a redirect
```

**Check 3:** Are the nftables tailscale0 rules in place? (see above)

**Check 4:** Is the Mullvad output chain allowing tailscale0?
```bash
sudo nft list chain inet mullvad output | grep tailscale0
# Should show: oifname "tailscale0" accept
```

### NextCloud Shows Blank Page or Redirect Loop

This happens when NextCloud doesn't know it's behind an HTTPS proxy:

```bash
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:set \
  overwriteprotocol --value="https"
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:set \
  overwrite.cli.url --value="https://YOUR_PI_TAILSCALE_IP"
```

### NextCloud "Access Through Untrusted Domain" Error

Add the domain or IP you're using to access NextCloud:

```bash
# Check current trusted domains
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:get trusted_domains 0
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:get trusted_domains 1

# Add a new trusted domain (increment the number)
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:set \
  trusted_domains 2 --value="new.domain.or.ip"
```

---

## Diagnostic Commands Reference

```bash
# === System ===
vcgencmd measure_temp           # CPU temperature
free -h                         # Memory usage
df -h                           # Disk usage
uptime                          # System uptime

# === VPN Status ===
mullvad status                  # Mullvad connection state
sudo tailscale status           # Tailscale nodes and status
curl -s ifconfig.me             # External IP (should be VPN)

# === Routing ===
ip rule show                    # Policy routing rules
ip route show table 1836018789  # Mullvad's routing table
ip route show table 52          # Tailscale's routing table
sudo ip route get 8.8.8.8 from 100.y.y.y iif tailscale0  # Test packet path

# === Firewall ===
sudo iptables -L FORWARD -v -n            # FORWARD chain
sudo iptables -t nat -L PREROUTING -v -n  # DNAT rules
sudo iptables -t nat -L POSTROUTING -v -n # Masquerade rules
sudo iptables -t mangle -L PREROUTING -v -n  # Mangle marks
sudo nft list ruleset | head -100          # Mullvad's nftables

# === Packet Tracing ===
sudo tcpdump -i tailscale0 -n 'host 100.y.y.y' -c 20   # iPhone traffic
sudo tcpdump -i wg0-mullvad -n -c 10                        # Mullvad tunnel
sudo conntrack -L | grep 100.y.y.y | head -10           # Connection tracking

# === Services ===
sudo systemctl status mullvad-daemon --no-pager
sudo systemctl status tailscaled --no-pager
sudo systemctl status exit-node-routes --no-pager
sudo systemctl status mullvad-nft-watchdog --no-pager

# === Mullvad nftables ===
sudo nft list chain inet mullvad output | head -10  # Check tailscale0 rules
sudo nft list chain inet mullvad forward | head -10  # Check tailscale0 rules
sudo nft list chain inet mullvad input | head -20    # Input chain rules
journalctl -u mullvad-nft-watchdog --no-pager --since "1 hour ago"  # Watchdog activity

# === Docker / NextCloud ===
docker ps                                             # Running containers
docker logs nextcloud-nginx-1 --tail 20               # Nginx logs
docker logs nextcloud-nextcloud-1 --tail 20            # NextCloud logs
docker exec -u www-data nextcloud-nextcloud-1 php occ status  # NC status

# === Security ===
sudo ufw status verbose
sudo fail2ban-client status sshd
```

---

## Security Checklist

- ✅ Mullvad owns `/etc/resolv.conf` (dns=none in NetworkManager.conf — NM doesn't overwrite it, Mullvad writes 10.64.0.1 on connect)
- ✅ SSH key-only authentication (no password login)
- ✅ UFW firewall (deny all incoming except SSH, Tailscale & NextCloud HTTPS)
- ✅ Fail2ban (auto-ban brute force attempts)
- ✅ Mullvad VPN (WireGuard protocol, account-number based — no personal data required)
- ✅ Encrypted DNS via Mullvad (10.64.0.1) — no DNS leaks
- ✅ Tailscale (WireGuard-based mesh, no port forwarding needed)
- ✅ Mullvad nftables patched (tailscale0 allowed in input, output & forward chains)
- ✅ Mullvad nft watchdog (auto re-applies nft rules, split-tunnel PID, and mark rule on VPN location switch/reconnect/update)
- ✅ Split-tunnel for tailscaled (control plane bypasses VPN)
- ✅ LAN sharing enabled (prevents SSH lockout)
- ✅ NextCloud behind Tailscale only (no public internet exposure)
- ✅ NextCloud HTTPS via Tailscale certificates (auto-renewed monthly via systemd timer)
- ✅ NVMe boot (reliable, fast storage)
- ✅ Active cooling (temperature-controlled)

---

## Maintenance

**Tailscale certificate renewal:**

Handled automatically by the monthly systemd timer set up in Phase 10. To check status or trigger manually:

```bash
# Check timer
systemctl list-timers | grep tailscale-cert

# Manual trigger
sudo /usr/local/bin/tailscale-cert-renew.sh
journalctl -u tailscale-cert-renew --no-pager --since "5 min ago"
```

### Keeping the System Up to Date

Raspberry Pi OS receives regular security patches and package updates. Since this Pi is internet-facing (running VPN services), keeping it updated is important.

**Pre-flight check before every update:**

Before running `apt update`, confirm the Pi's DNS is resolving correctly. Mullvad must be connected and must own `/etc/resolv.conf` — if NetworkManager has overwritten it with the router's IP (`192.168.1.1`), `apt` will fail with "Temporary failure resolving" errors because Mullvad's nftables blocks DNS to anything except `10.64.0.1`.

```bash
# 1. Verify Mullvad is connected
mullvad status
# Expected: Connected

# 2. Verify resolv.conf points to Mullvad's DNS
cat /etc/resolv.conf
# Expected: nameserver 10.64.0.1

# 3. Verify external DNS is working
curl -s https://ifconfig.me
# Expected: your VPN exit IP
```

If `resolv.conf` shows `192.168.1.1` or any other address instead of `10.64.0.1`, reconnect Mullvad to let it rewrite the file:

```bash
mullvad reconnect
sleep 5
cat /etc/resolv.conf
# Should now show: nameserver 10.64.0.1
```

**⚠️ Never use `chattr +i` to make `/etc/resolv.conf` immutable.** Mullvad needs to write this file itself on every connect. Locking it causes Mullvad to enter "Blocked: Failed to set system DNS server" state at boot, which breaks DNS for the Pi and causes `exit-node-routes.service` to fail (its pre-check polls `mullvad status | grep Connected`, which never succeeds). The correct approach is `dns=none` in NetworkManager.conf (already configured) — this prevents NetworkManager from overwriting the file while still letting Mullvad write it.

**Manual update (recommended monthly or after security advisories):**

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt autoremove -y
```

**Check EEPROM firmware (quarterly):**

```bash
sudo rpi-eeprom-update
# If an update is available:
sudo rpi-eeprom-update -a
sudo reboot
```

**Automated unattended upgrades (security patches only):**

For hands-off security updates, install `unattended-upgrades`:

```bash
sudo apt install unattended-upgrades -y
sudo dpkg-reconfigure -plow unattended-upgrades
```

Select "Yes" when prompted. This automatically installs security patches daily without touching non-security packages (Mullvad, Tailscale, etc.), which avoids unexpected breaking changes.

To verify it's active:

```bash
sudo systemctl status unattended-upgrades
```

**Updating Mullvad and Tailscale:**

Both tools update through apt:

```bash
sudo apt update
sudo apt install --only-upgrade mullvad-vpn tailscale
```

After major Mullvad or Tailscale updates, reboot and verify the exit-node-routes service starts correctly:

```bash
sudo reboot
# After reboot:
sudo systemctl status exit-node-routes.service --no-pager
```

**Updating NextCloud and Docker containers:**

```bash
cd ~/nextcloud

# Pull latest images
docker compose pull

# Recreate containers with new images
docker compose up -d

# Clean up old images
docker image prune -f
```

After a NextCloud update (i.e. after `docker compose pull` — not after `apt full-upgrade`,
which only updates OS packages and does not touch Nextcloud), check for required database migrations:

```bash
docker exec -u www-data nextcloud-nextcloud-1 php occ upgrade
docker exec -u www-data nextcloud-nextcloud-1 php occ db:add-missing-indices
```

### Temperature Monitoring

The Raspberry Pi 5 with active cooling should run cool under VPN routing workloads.

**Check temperature:**

```bash
vcgencmd measure_temp
```

**Expected ranges:**

| State | Temperature | Notes |
|---|---|---|
| Idle | 35–45°C | Fan off, normal operation |
| Light load (VPN routing) | 40–50°C | Fan may spin occasionally |
| Heavy load | 50–65°C | Fan active, still healthy |
| Throttling | 80°C+ | CPU slows down to protect itself |
| Danger | 85°C+ | Sustained use risks hardware damage |

A reading of ~44°C at idle with VPN routing active is excellent — well within safe operating range. The active cooler's fan is temperature-controlled: it stays off below ~50°C and spins up progressively as needed.

**Check for throttling (undervoltage or thermal):**

```bash
vcgencmd get_throttled
# 0x0 = no issues (ideal)
# Other values indicate past or current throttling
```

If you see throttling, ensure the 27W power supply is connected (not a phone charger) and that the case has adequate airflow.

---

## Cost

| Item | Cost |
|---|---|
| Mullvad VPN | €5/month (1 account covers all devices) |
| Tailscale | Free (personal use, up to 100 devices) |
| NextCloud | Free (self-hosted, open source) |
| Docker | Free (open source) |
| Electricity | ~€2/month (Pi 5 draws ~5W idle) |
| **Total** | **~€7/month** |

Compared to running Mullvad on each device separately plus paying for cloud storage (OneDrive, iCloud, Google Drive), this setup provides VPN protection for all devices and private cloud storage with a single Mullvad subscription and no recurring storage fees.

---

## Phase 9: NextCloud — Personal Cloud Storage

NextCloud turns your Pi into a private alternative to OneDrive/iCloud/Google Drive. Combined with your Tailscale mesh, it's accessible securely from anywhere — no port forwarding, no public exposure.

**What you get:** File sync, photo backup from iOS, document editing, calendar, contacts — all stored on your own hardware.

### 9.1 Install Docker

```bash
curl -sSL https://get.docker.com | sh
sudo usermod -aG docker $(whoami)

# Log out and back in for group change to take effect
exit
# SSH back in
ssh username@vpi5.local
```

Verify:

```bash
docker --version
docker compose version
```

### 9.2 Create Project Directory

```bash
mkdir -p ~/nextcloud/nginx
```

### 9.3 Generate SSL Certificates

Tailscale can provision Let's Encrypt certificates for your Pi's Tailscale hostname:

```bash
# Enable HTTPS certificates in Tailscale admin:
# https://login.tailscale.com/admin/dns → Enable HTTPS

sudo tailscale cert vpi5.your-tailnet.ts.net
# Replace 'vpi5.your-tailnet.ts.net' with your actual Tailscale hostname
# Find it with: tailscale status --self
```

The certificates will be stored in `/etc/tailscale/certs/`.

### 9.4 Nginx Configuration

```bash
nano ~/nextcloud/nginx/nextcloud.conf
```

Paste (replace `vpi5.your-tailnet.ts.net` with your actual Tailscale hostname):

```nginx
server {
    listen 443 ssl;
    server_name vpi5.your-tailnet.ts.net 100.x.x.x;

    ssl_certificate /etc/tailscale/certs/vpi5.your-tailnet.ts.net.crt;
    ssl_certificate_key /etc/tailscale/certs/vpi5.your-tailnet.ts.net.key;

    client_max_body_size 10G;

    location / {
        proxy_pass http://nextcloud:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Key settings:**

| Setting | Purpose |
|---|---|
| `server_name` | Accepts both Tailscale hostname and IP address |
| `client_max_body_size 10G` | Allows uploading large files (default 1MB is too small) |
| `X-Forwarded-Proto` | Tells NextCloud it's behind HTTPS (prevents redirect loops) |

### 9.5 Docker Compose

```bash
nano ~/nextcloud/docker-compose.yml
```

Paste (change all passwords to strong, unique values):

```yaml
services:
  nginx:
    image: nginx:alpine
    restart: always
    ports:
      - "443:443"
    volumes:
      - ./nginx/nextcloud.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/tailscale/certs:/etc/tailscale/certs:ro
    depends_on:
      - nextcloud

  db:
    image: mariadb:11
    restart: always
    environment:
      MYSQL_ROOT_PASSWORD: CHANGE_ME_ROOT_PASSWORD
      MYSQL_DATABASE: nextcloud
      MYSQL_USER: nextcloud
      MYSQL_PASSWORD: CHANGE_ME_DB_PASSWORD
    volumes:
      - db:/var/lib/mysql

  nextcloud:
    image: nextcloud:latest
    restart: always
    ports:
      - "8080:80"
    depends_on:
      - db
    environment:
      MYSQL_HOST: db
      MYSQL_DATABASE: nextcloud
      MYSQL_USER: nextcloud
      MYSQL_PASSWORD: CHANGE_ME_DB_PASSWORD
    volumes:
      - nextcloud:/var/www/html
      - /mnt/data:/var/www/html/data

volumes:
  db:
  nextcloud:
```

**⚠️ CRITICAL:** The `MYSQL_PASSWORD` must be identical in both the `db` and `nextcloud` services.

### 9.6 Create Data Directory and Launch

```bash
# Create the data directory for NextCloud files
sudo mkdir -p /mnt/data
sudo chown -R www-data:www-data /mnt/data

# Allow port 443 through the firewall
sudo ufw allow 443/tcp comment 'NextCloud HTTPS'

# Launch all containers
cd ~/nextcloud
docker compose up -d
```

Wait about 30 seconds for all containers to start:

```bash
docker ps
# Should show 3 containers: nginx, nextcloud, db — all "Up"
```

### 9.7 Complete Setup Wizard

1. Open your browser (on a Tailscale-connected device): `https://YOUR_PI_TAILSCALE_IP`
2. Accept the certificate warning if accessing via IP
3. Create an admin account (choose a strong password)
4. Database configuration:
   - Select **MySQL/MariaDB**
   - Database user: `nextcloud`
   - Database password: (the password you set in docker-compose.yml)
   - Database name: `nextcloud`
   - Database host: `db`
5. Click **Install** and wait 1–2 minutes

### 9.8 Post-Installation Configuration

After the setup wizard completes, configure HTTPS and trusted domains:

```bash
# Tell NextCloud it's behind an HTTPS reverse proxy
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:set \
  overwrite.cli.url --value="https://YOUR_PI_TAILSCALE_IP"

docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:set \
  overwriteprotocol --value="https"

# Add Tailscale hostname as trusted domain
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:set \
  trusted_domains 1 --value="vpi5.your-tailnet.ts.net"

# Trust the Docker network as proxy
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:set \
  trusted_proxies 0 --value="172.18.0.0/16"
```

### 9.9 Connect Your Devices

**iPhone / iPad:**

1. Install **Nextcloud** from the App Store
2. Server address: `https://YOUR_PI_TAILSCALE_IP`
3. Log in, accept the certificate warning
4. Enable **Auto Upload** in settings for automatic photo backup

**macOS:**

1. Download the desktop client from https://nextcloud.com/install/#install-clients
2. Server address: `https://YOUR_PI_TAILSCALE_IP`
3. Log in and choose which folders to sync

**⚠️ Important:** Your devices must be connected to Tailscale to reach NextCloud. It is not exposed to the public internet — that's the security benefit.

### 9.10 Verify

```bash
# All containers running?
docker ps

# NextCloud status
docker exec -u www-data nextcloud-nextcloud-1 php occ status

# Check trusted domains
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:get trusted_domains 0
docker exec -u www-data nextcloud-nextcloud-1 php occ config:system:get trusted_domains 1
```

---

## Phase 10: Tailscale Certificate Renewal

Tailscale HTTPS certificates are issued via Let's Encrypt and expire every **90 days**. Rather than renewing manually each quarter, a systemd timer handles it automatically.

### 10.1 Create the Renewal Script

```bash
sudo nano /usr/local/bin/tailscale-cert-renew.sh
```

Paste (replace `vpi5.your-tailnet.ts.net` with your actual Pi 1 Tailscale hostname, and `YOUR_USERNAME` with your Pi username):

```bash
#!/bin/bash
HOSTNAME="vpi5.your-tailnet.ts.net"
CERT_DIR="/etc/tailscale/certs"

tailscale cert "$HOSTNAME"

# Only copy + reload if the cert has actually changed
if ! diff -q "${HOSTNAME}.crt" "${CERT_DIR}/${HOSTNAME}.crt" > /dev/null 2>&1; then
    cp "${HOSTNAME}.crt" "${CERT_DIR}/"
    cp "${HOSTNAME}.key" "${CERT_DIR}/"
    chmod 640 "${CERT_DIR}/${HOSTNAME}.key"
    docker compose -f /home/YOUR_USERNAME/nextcloud/docker-compose.yml exec nginx nginx -s reload
    logger "tailscale-cert-renew: cert renewed and nginx reloaded"
fi

rm -f "${HOSTNAME}.crt" "${HOSTNAME}.key"
```

```bash
sudo chmod +x /usr/local/bin/tailscale-cert-renew.sh
```

### 10.2 Create the Systemd Service

```bash
sudo nano /etc/systemd/system/tailscale-cert-renew.service
```

Paste:

```ini
[Unit]
Description=Renew Tailscale HTTPS certificate

[Service]
Type=oneshot
ExecStart=/usr/local/bin/tailscale-cert-renew.sh
```

### 10.3 Create the Systemd Timer

```bash
sudo nano /etc/systemd/system/tailscale-cert-renew.timer
```

Paste:

```ini
[Unit]
Description=Monthly Tailscale certificate renewal

[Timer]
OnCalendar=monthly
Persistent=true

[Install]
WantedBy=timers.target
```

### 10.4 Enable and Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable tailscale-cert-renew.timer
sudo systemctl start tailscale-cert-renew.timer
```

Verify the timer is scheduled:

```bash
systemctl list-timers | grep tailscale-cert
# Should show next run ~1 month out
```

**How it works:** `tailscale cert` requests a fresh certificate from Let's Encrypt via Tailscale's coordination server. The script compares the new cert against the one currently in `/etc/tailscale/certs/` — if they differ, it copies the new files and sends a reload signal to the Nginx container. If the cert hasn't changed (e.g. it was renewed recently), the script exits without touching anything. The `Persistent=true` flag means if the Pi was off when the timer was due, it runs at next boot.

**To trigger a manual renewal at any time:**

```bash
sudo /usr/local/bin/tailscale-cert-renew.sh
journalctl -u tailscale-cert-renew --no-pager --since "5 min ago"
```

---

## Next Steps

With this foundation in place, the Pi can also run:

- **Pi-hole** — network-wide ad blocking (Mullvad already provides built-in ad blocking, so this is optional)
- **Home Assistant** — smart home automation
- **Additional Docker services** — any containerized application

---

## Credits & Resources

- [Raspberry Pi Documentation](https://www.raspberrypi.com/documentation/)
- [Mullvad VPN](https://mullvad.net/)
- [Tailscale Documentation](https://tailscale.com/kb/)
- [WireGuard Protocol](https://www.wireguard.com/)
- [NextCloud Documentation](https://docs.nextcloud.com/)
- [Docker Documentation](https://docs.docker.com/)

---

**Last Updated:** March 2026 (added split-tunnel mark rule fix — Mullvad's nft output chain drops `0x6d6f6c65`-marked TCP traffic without an explicit accept rule, causing tailscaled to lose coordination server access; watchdog extended to self-heal split-tunnel PID and mark rule; added automated Tailscale certificate renewal via systemd timer)
**Tested On:** Raspberry Pi 5 (4GB), Raspberry Pi OS Lite Bookworm (64-bit), Mullvad VPN, Tailscale, NextCloud 33.0, Docker, Nginx