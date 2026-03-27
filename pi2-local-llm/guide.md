# Raspberry Pi 5 — Local LLM Node

Private AI inference on your own hardware, accessible exclusively through your Tailscale mesh. No data leaves your network. No API keys, no usage limits, no cloud dependency.

**Result:** A self-hosted LLM server running Ollama and Open WebUI, accessible from iPhone, iPad, and Mac via Tailscale. Models run entirely on the Pi 5 16GB. Documents stored in Nextcloud on Pi 1 are available to the LLM via WebDAV for RAG workflows.

---

## Placeholders

This guide uses the following placeholders. Replace them with your actual values before running any command.

| Placeholder | What it is | How to find it |
|---|---|---|
| `YOUR-PI2-HOSTNAME` | Pi 2's hostname (set during OS flash) | `hostname` on the Pi |
| `YOUR-PI1-HOSTNAME` | Pi 1's hostname | `hostname` on Pi 1 |
| `your-tailnet` | Your Tailscale network name | Tailscale Admin → DNS tab — the part before `.ts.net` in your machine FQDNs |
| `YOUR_PI2_TAILSCALE_IP` | Pi 2's Tailscale IP | `tailscale ip` on Pi 2, or Tailscale Admin → Machines |
| `YOUR_PI1_TAILSCALE_IP` | Pi 1's Tailscale IP | `tailscale ip` on Pi 1 |
| `YOUR_PI1_TAILSCALE_HOSTNAME` | Pi 1's full Tailscale FQDN | `tailscale status --self --json \| grep DNSName` on Pi 1 |
| `YOUR_NC_USERNAME` | Your Nextcloud username | Nextcloud Settings → Personal info |
| `YOUR_NC_PASSWORD` | Your Nextcloud password (or app password) | Nextcloud → Settings → Security → App passwords |
| `YOUR_USERNAME` | Your Pi OS username | Set during Raspberry Pi Imager setup |
| `100.x.x.x` | Any Tailscale device IP | `tailscale ip` on the relevant device |

**Getting your full Tailscale hostname (needed for TLS certs):**
```bash
tailscale status --self --json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))"
# e.g. → vpi5-llm.your-tailnet.ts.net
```

---

## Hardware

| Component | Model |
|---|---|
| Board | Raspberry Pi 5 (16GB RAM) |
| Storage | PNY CS1030 250GB NVMe SSD (PCIe Gen3, M.2 2280) |
| HAT | Official Raspberry Pi M.2 HAT+ (Model A) |
| Cooling | Active Cooler (temperature-controlled fan) |
| Case | Metal enclosure |
| Power | Official 27W USB-C Power Supply |
| SD Card | 64GB microSD (initial setup only) |
| Network | Ethernet cable + Orange Livebox 5 (fiber) |

**Why 16GB RAM?** LLMs load their full model weights into memory at runtime. A 7B parameter model in 4-bit quantization requires ~4–5GB of RAM. 16GB gives comfortable headroom to run a 7B model alongside the OS and Open WebUI without swapping to disk, which would make inference unusably slow on NVMe.

---

## Key Concepts

**What is Ollama?** Ollama is a local model runtime — it downloads, manages, and serves LLM models on your hardware. It exposes a REST API (compatible with OpenAI's format) that Open WebUI talks to. Think of it as the engine room: no user interface, just inference.

**What is Open WebUI?** Open WebUI is a self-hosted chat interface that connects to Ollama. It gives you a ChatGPT-like experience — conversation history, model switching, file uploads, and RAG — running entirely on your Pi.

**What is RAG?** Retrieval-Augmented Generation. Instead of asking the model to answer from its training data alone, RAG lets it search a document collection first and base its answer on the retrieved content. In this setup, your Nextcloud documents (stored on Pi 1) are mounted on Pi 2 via WebDAV and made available to Open WebUI as a knowledge base.

**Why no Mullvad on Pi 2?** Pi 2 is a compute node, not a network exit point. It doesn't need to anonymise traffic — it never connects to the internet directly. All access is through Tailscale, which provides end-to-end encryption. Adding Mullvad would introduce nftables complexity with no privacy benefit for this use case.

---

## Prerequisites

Follow the **Pi 1 guide (Phases 1–3.5)** before continuing here. This covers:

- Phase 1: OS flash, first boot, SSH access
- **1.5**: Assign static IP `192.168.1.52` on the Livebox (Pi 1 uses `192.168.1.50`)
- Phase 2: NVMe boot configuration
- Phase 3: Security hardening (SSH keys, UFW, fail2ban, system updates)

**Phase 3.1 note:** Skip `ssh-keygen` — your key already exists from Pi 1 setup. Just run:
```bash
ssh-copy-id username@192.168.1.52
```

After completing Phase 3.5, return here.

---

## Phase 4: Tailscale Installation

Pi 2 joins the same Tailscale network as Pi 1 but as a regular node — not an exit node. It doesn't route anyone's traffic; it just needs to be reachable from your other devices.

### 4.1 Install Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

### 4.2 Configure Tailscale

```bash
sudo tailscale up \
  --accept-routes \
  --accept-dns=false
```

Follow the authentication URL printed in the terminal to log in with the same Tailscale account used on Pi 1.

**Key flags:**

| Flag | Purpose |
|---|---|
| `--accept-routes` | Accepts subnet routes advertised by Pi 1 |
| `--accept-dns=false` | Pi keeps its own DNS (not managed by Tailscale) |

Note the absence of `--advertise-exit-node` and `--netfilter-mode=off` — Pi 2 is not doing any traffic routing, so neither is needed.

### 4.3 Verify

```bash
sudo tailscale status
# Should show both Pis and your other devices on the tailnet

sudo tailscale ip
# Note this Pi's Tailscale IP — you'll use it to access Open WebUI
```

### 4.4 Update UFW for Tailscale

```bash
sudo ufw allow 41641/udp comment 'Tailscale'
sudo ufw status verbose
```

### 4.5 Fix LAN Routing (Tailscale Subnet Route Conflict)

Pi 1 advertises `192.168.1.0/24` as a Tailscale subnet route (`--advertise-routes=192.168.1.0/24`). Because Pi 2 uses Tailscale's default netfilter mode, Tailscale injects this route into its own routing table (table 52) as `192.168.1.0/24 dev tailscale0`. Tailscale's `ip rule` at priority 5270 is evaluated before the main table (priority 32766), so all LAN-destined packets — including SYN-ACK responses to SSH connections — get routed through the Tailscale tunnel instead of `eth0`.

**Symptom:** Pi 2 is unreachable via LAN SSH (SYN packets arrive on `eth0` but SYN-ACK is sent via `tailscale0`), while Tailscale SSH works fine.

**Verify the problem exists:**

```bash
ip route show table 52
# If you see "192.168.1.0/24 dev tailscale0" — the fix is needed
```

**Fix:** Add a policy routing rule that forces LAN traffic through the main table before Tailscale's rules:

```bash
sudo ip rule add to 192.168.1.0/24 lookup main priority 100
```

Verify:

```bash
ip route get 192.168.1.20 from 192.168.1.52
# Should show: dev eth0 (not dev tailscale0)
```

**Make it persistent:**

```bash
sudo nano /etc/systemd/system/lan-route-fix.service
```

Paste:

```ini
[Unit]
Description=Force LAN traffic via eth0 (bypass Tailscale routing table)
After=tailscaled.service network-online.target
Wants=tailscaled.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip rule add to 192.168.1.0/24 lookup main priority 100
ExecStop=/sbin/ip rule del to 192.168.1.0/24 lookup main priority 100

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable lan-route-fix.service
sudo systemctl start lan-route-fix.service
```

After a reboot, confirm both paths work:

```bash
# LAN SSH (from Mac on the same network)
ssh username@192.168.1.52

# Tailscale SSH (from anywhere)
ssh username@100.x.x.x
```

**Why Pi 1 doesn't have this problem:** Pi 1 runs Tailscale with `--netfilter-mode=off`, which disables Tailscale's automatic routing table and `ip rule` injection. All routing on Pi 1 is managed manually via the exit-node-setup script. Pi 2 uses the default netfilter mode (appropriate since it's not routing traffic), but this means Tailscale manages table 52 automatically — and injects the subnet route that conflicts with direct LAN access.

---

## Phase 5: Ollama

### 5.1 Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

This installs Ollama as a systemd service that starts automatically on boot.

### 5.2 Bind Ollama to Tailscale IP Only

By default Ollama listens on `127.0.0.1:11434` (localhost only). You need it to also accept connections from Open WebUI running in Docker — but not from the public internet or the LAN. Bind it to the Tailscale IP:

```bash
sudo nano /etc/systemd/system/ollama.service.d/override.conf
```

Create the directory first if needed:
```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo nano /etc/systemd/system/ollama.service.d/override.conf
```

Paste (replace `100.x.x.x` with this Pi's actual Tailscale IP):

```ini
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
```

Paste:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

`host.docker.internal` resolves to the host's Docker bridge IP, not `127.0.0.1` — so binding Ollama to localhost only causes a connection timeout from inside the container. Binding to `0.0.0.0` allows Docker to reach it. UFW already blocks port 11434 from the LAN and internet, so this is safe.

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Verify Ollama is listening on all interfaces:

```bash
ss -tlnp | grep 11434
# Should show: *:11434
```

### 5.3 Verify Ollama

```bash
sudo systemctl status ollama --no-pager
curl http://localhost:11434/api/tags
# Should return: {"models":[]}
```

### 5.4 Pull Initial Models

```bash
# Lightweight, fast — good for testing and quick queries (~2GB)
ollama pull llama3.2:3b

# General purpose 7B — best quality/performance balance (~4.5GB)
ollama pull mistral:7b

# Code-focused 7B — for programming tasks (~4.7GB)
# qwen2.5-coder outperforms codellama on modern benchmarks
ollama pull qwen2.5-coder:7b
```

Each pull downloads the quantized model weights. Expect 5–15 minutes per model depending on your connection.

Verify models are loaded:
```bash
ollama list
# Should show all three models with their sizes
```

Test inference:
```bash
ollama run llama3.2:3b "Respond in one sentence: what is a Raspberry Pi?"
# Should return a response in a few seconds
```

---

## Phase 6: Open WebUI

### 6.1 Install Docker

```bash
curl -sSL https://get.docker.com | sh
sudo usermod -aG docker $(whoami)

# Log out and back in for group change to take effect
exit
```

SSH back in, then verify:

```bash
docker --version
docker compose version
```

### 6.2 Create Project Directory

```bash
mkdir -p ~/openwebui
```

### 6.3 Docker Compose

First, find the Docker bridge gateway IP — this is the host IP reachable from inside containers:

```bash
ip route | grep docker
# Look for: 172.18.0.0/16 dev docker0 ... src 172.18.0.1
# The gateway is the src address, e.g. 172.18.0.1
```

Allow Ollama connections from the Docker network through UFW:

```bash
sudo ufw allow from 172.18.0.0/16 to any port 11434 comment 'Ollama from Docker'
sudo ufw reload
```

Then create the compose file:

```bash
nano ~/openwebui/docker-compose.yml
```

Paste (this is the complete file — skip the partial version shown in 6.4):

```yaml
services:
  nginx:
    image: nginx:alpine
    restart: always
    ports:
      - "443:443"
    volumes:
      - ./nginx/openwebui.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/tailscale/certs:/etc/tailscale/certs:ro
    depends_on:
      - open-webui

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    restart: always
    expose:
      - "8080"
    volumes:
      - open-webui:/app/backend/data
      - /mnt/nextcloud:/mnt/nextcloud:ro

volumes:
  open-webui:
```

**Key settings:**

| Setting | Purpose |
|---|---|
| `expose: 8080` | Internal port only — Nginx proxies it |
| `/mnt/nextcloud:ro` | Mounts the Nextcloud WebDAV share read-only for RAG access |

**Why no `extra_hosts`?** `host.docker.internal:host-gateway` maps to the `docker0` interface (`172.17.0.1`), but compose creates its own bridge network (`172.18.0.0/16`) with a different gateway. Since `docker0` is linkdown from the container's perspective, connections to `host.docker.internal:11434` silently fail. The solution is to use the compose network gateway IP directly in Open WebUI's settings (step 6.7).

### 6.4 Nginx Reverse Proxy

Open WebUI will be served over HTTPS on your Tailscale IP, same pattern as Nextcloud on Pi 1.

First, provision a Tailscale certificate for this Pi:

```bash
# Find this Pi's full Tailscale hostname
tailscale status --self --json | grep -i dnsname
# Look for your Pi's entry, e.g: "DNSName": "vpi5-llm.your-tailnet.ts.net."
# (ignore the trailing dot — that's standard DNS notation)

# Provision certificate (replace with your actual hostname)
sudo tailscale cert vpi5-llm.your-tailnet.ts.net
# Wrote public cert to vpi5-llm.your-tailnet.ts.net.crt
# Wrote private key to vpi5-llm.your-tailnet.ts.net.key

# Move certificates to /etc/tailscale/certs/ (where Nginx expects them)
sudo mkdir -p /etc/tailscale/certs
sudo mv vpi5-llm.your-tailnet.ts.net.crt /etc/tailscale/certs/
sudo mv vpi5-llm.your-tailnet.ts.net.key /etc/tailscale/certs/
sudo chmod 640 /etc/tailscale/certs/vpi5-llm.your-tailnet.ts.net.key
```

Create the Nginx config directory and config:

```bash
mkdir -p ~/openwebui/nginx
nano ~/openwebui/nginx/openwebui.conf
```

Paste (replace `vpi5-2.your-tailnet.ts.net` and the Tailscale IP with your actual values):

```nginx
server {
    listen 443 ssl;
    server_name vpi5-2.your-tailnet.ts.net 100.x.x.x;

    ssl_certificate /etc/tailscale/certs/vpi5-2.your-tailnet.ts.net.crt;
    ssl_certificate_key /etc/tailscale/certs/vpi5-2.your-tailnet.ts.net.key;

    client_max_body_size 100M;

    location / {
        proxy_pass http://open-webui:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support (required for streaming responses)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Required for Open WebUI streaming responses
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

Add the Nginx service to `docker-compose.yml`:

```yaml
services:
  nginx:
    image: nginx:alpine
    restart: always
    ports:
      - "443:443"
    volumes:
      - ./nginx/openwebui.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/tailscale/certs:/etc/tailscale/certs:ro
    depends_on:
      - open-webui

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    restart: always
    expose:
      - "8080"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - open-webui:/app/backend/data
      - /mnt/nextcloud:/mnt/nextcloud:ro

volumes:
  open-webui:
```

### 6.5 Open Port 443

```bash
sudo ufw allow 443/tcp comment 'Open WebUI HTTPS'
```

### 6.6 Launch

```bash
cd ~/openwebui
docker compose up -d
```

Wait 30 seconds for the container to initialise:

```bash
docker ps
# Should show both nginx and open-webui containers as "Up"
```

### 6.7 Initial Setup

From a Tailscale-connected device, open: `https://YOUR_PI2_TAILSCALE_IP`

1. Create an admin account on first visit
2. Go to **Settings** → **Connections**
3. Disable **OpenAI API** — not needed and causes noise in the model selector
4. Under **Ollama API**, set URL to your Docker bridge gateway IP:
   `http://172.18.0.1:11434`
   (verify your gateway with `ip route | grep docker | awk '{print $9}'` if unsure)
5. Click **Verify** — should show a green checkmark and list your models

**Health check:** select `llama3.2:3b` in the model dropdown and send a test message. First response takes 30–60 seconds on CPU-only hardware — this is normal.

### 6.8 Customisation

Keep it minimal — the interface should feel like a private tool, not a product demo:

1. **Settings** → **General**:
   - Name: `Pi LLM` (or your preference)
   - Logo: upload a custom icon if desired
2. **Appearance**: set to Dark theme
3. Leave everything else at defaults

---

## Phase 7: Nextcloud WebDAV Mount (RAG)

This mounts your Nextcloud files from Pi 1 onto Pi 2's filesystem, making them available to Open WebUI as a document collection for RAG queries.

### 7.1 Install davfs2

```bash
sudo apt install davfs2 -y
```

When prompted about SUID, select **Yes**.

### 7.2 Create Mount Point

```bash
sudo mkdir -p /mnt/nextcloud
sudo chown $(whoami):$(whoami) /mnt/nextcloud
```

### 7.3 Configure davfs2 Credentials

```bash
sudo nano /etc/davfs2/secrets
```

Add (replace with your actual Nextcloud username and password):

```
https://YOUR_PI1_TAILSCALE_IP/remote.php/dav/files/YOUR_NC_USERNAME/ YOUR_NC_USERNAME YOUR_NC_PASSWORD
```

```bash
sudo chmod 600 /etc/davfs2/secrets
```

### 7.4 Add fstab Entry

```bash
sudo nano /etc/fstab
```

Add at the bottom:

```
https://YOUR_PI1_TAILSCALE_IP/remote.php/dav/files/YOUR_NC_USERNAME/ /mnt/nextcloud davfs _netdev,auto,uid=1000,gid=1000,ro 0 0
```

**Key options:**

| Option | Purpose |
|---|---|
| `_netdev` | Wait for network before mounting (important on boot) |
| `auto` | Mount automatically at boot |
| `ro` | Read-only — Pi 2 reads documents, never writes |
| `uid=1000,gid=1000` | Mount as your Pi user, not root |

### 7.5 Trust the Nextcloud TLS Certificate

The fstab entry uses Pi 1's Tailscale IP, but the TLS certificate is issued for its hostname (`vpi5.your-tailnet.ts.net`). Without pinning the cert, davfs2 prompts interactively on every mount — which blocks auto-mount on boot.

Pin the certificate so davfs2 trusts it silently:

```bash
sudo mkdir -p /etc/davfs2/certs
echo | openssl s_client -connect YOUR_PI1_TAILSCALE_IP:443 \
  -servername YOUR_PI1_TAILSCALE_HOSTNAME 2>/dev/null | \
  openssl x509 > /tmp/nextcloud.pem
sudo mv /tmp/nextcloud.pem /etc/davfs2/certs/nextcloud.pem
```

Tell davfs2 to use it:

```bash
sudo nano /etc/davfs2/davfs2.conf
```

Add at the bottom:

```
trust_server_cert /etc/davfs2/certs/nextcloud.pem
```

**Note:** When the Tailscale certificate renews (every 90 days, handled by the timer on Pi 1), you'll need to re-export this cert. The `warning: the server does not support locks` message is normal for Nextcloud WebDAV and can be ignored.

### 7.6 Mount and Verify

```bash
sudo mount /mnt/nextcloud
ls /mnt/nextcloud
# Should list your Nextcloud files and folders
```

### 7.7 Sync Documents to Open WebUI Knowledge Base

Open WebUI's Knowledge → Sync Directory renders as a browser file picker — it opens Finder or Files on the client, not the Pi's filesystem. There is no way to type a server-side path like `/mnt/nextcloud` through the UI. The workaround is a Python script that uses Open WebUI's REST API to sync files from the WebDAV mount into a Knowledge Base directly.

**How it works:** The sync script reads files from `/mnt/nextcloud`, computes SHA-256 hashes, uploads new or changed files via the API, removes files that were deleted from Nextcloud, and reindexes the Knowledge Base. A local manifest file tracks sync state so only changes are processed on each run. A systemd timer runs it automatically every 6 hours.

#### 7.7.1 Install Dependencies

```bash
sudo apt install python3-requests -y
```

#### 7.7.2 Deploy the Sync Script

The `nc-knowledge-sync.py` script is in the GitHub repo alongside this guide. Copy it to Pi 2:

```bash
sudo cp nc-knowledge-sync.py /usr/local/bin/nc-knowledge-sync.py
sudo chmod +x /usr/local/bin/nc-knowledge-sync.py
```

#### 7.7.3 Generate an API Key

First, enable API key creation (admin-only, one-time setup):

1. **Admin Panel** → **Settings** → **General** → toggle **Enable API Keys** on
2. Scroll to **Default Permissions** → under **Features**, enable **API Keys**

Then create your key:

3. **Settings** → **Account** → **API Keys**
4. Click **Create new secret key**
5. Copy the key immediately (starts with `sk-`) — it won't be shown again

Store it in a config file on the Pi:

```bash
mkdir -p ~/.config/nc-sync
nano ~/.config/nc-sync/env
```

Paste (replace values):

```
OWUI_URL="https://YOUR_PI2_TAILSCALE_IP"
OWUI_TOKEN="sk-your-api-key-here"
OWUI_KB_NAME="Nextcloud Documents"
OWUI_FOLDER="/mnt/nextcloud/Documents"
```

```bash
chmod 600 ~/.config/nc-sync/env
```

#### 7.7.4 Test with Dry Run

```bash
source ~/.config/nc-sync/env

python3 /usr/local/bin/nc-knowledge-sync.py \
  --url "$OWUI_URL" \
  --token "$OWUI_TOKEN" \
  --knowledge "$OWUI_KB_NAME" \
  --folder "$OWUI_FOLDER" \
  --dry-run
```

This shows what would be synced without making changes. Verify the file list looks correct.

#### 7.7.5 First Sync

```bash
python3 /usr/local/bin/nc-knowledge-sync.py \
  --url "$OWUI_URL" \
  --token "$OWUI_TOKEN" \
  --knowledge "$OWUI_KB_NAME" \
  --folder "$OWUI_FOLDER"
```

Each file is uploaded, processed (text extraction + embedding), then added to the KB. Expect 5–30 seconds per file. After completion, open Open WebUI → **Workspace** → **Knowledge** — you should see the "Nextcloud Documents" knowledge base with all your files.

#### 7.7.6 Automate with systemd Timer

Create a wrapper script:

```bash
sudo nano /usr/local/bin/nc-knowledge-sync-run.sh
```

Paste (replace `YOUR_USERNAME`):

```bash
#!/bin/bash
set -e

ENV_FILE="/home/YOUR_USERNAME/.config/nc-sync/env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: Config not found at $ENV_FILE"
    exit 1
fi
source "$ENV_FILE"

# Skip if WebDAV mount is down
if [ ! -d "$OWUI_FOLDER" ] || [ -z "$(ls -A "$OWUI_FOLDER" 2>/dev/null)" ]; then
    echo "WARNING: $OWUI_FOLDER is empty or not mounted — skipping sync"
    exit 0
fi

exec python3 /usr/local/bin/nc-knowledge-sync.py \
    --url "$OWUI_URL" \
    --token "$OWUI_TOKEN" \
    --knowledge "$OWUI_KB_NAME" \
    --folder "$OWUI_FOLDER"
```

```bash
sudo chmod +x /usr/local/bin/nc-knowledge-sync-run.sh
```

Create the service:

```bash
sudo nano /etc/systemd/system/nc-knowledge-sync.service
```

```ini
[Unit]
Description=Sync Nextcloud documents to Open WebUI Knowledge Base
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nc-knowledge-sync-run.sh
User=YOUR_USERNAME
StandardOutput=journal
StandardError=journal
```

Create the timer:

```bash
sudo nano /etc/systemd/system/nc-knowledge-sync.timer
```

```ini
[Unit]
Description=Run Nextcloud → Open WebUI sync every 6 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable nc-knowledge-sync.timer
sudo systemctl start nc-knowledge-sync.timer
```

#### 7.7.7 Use in Chat

In any Open WebUI chat, type `#` and select **Nextcloud Documents** from the dropdown. The model searches the Knowledge Base and uses relevant documents as context.

To make it automatic, attach the KB to a custom model:

1. **Workspace** → **Models** → **+ Add New Model**
2. Name: `Pi LLM + Docs`
3. Base model: `mistral:7b`
4. Knowledge: select **Nextcloud Documents**
5. Save

Every chat with that model now has access to your Nextcloud documents without typing `#`.

**Ad-hoc uploads still work:** For one-off documents not in Nextcloud, click **+** in any chat, upload a file, and ask your question. The model uses it as context immediately.

#### 7.7.8 Troubleshooting Sync

**"Folder not found" error:** WebDAV mount disconnected. Remount: `sudo mount /mnt/nextcloud`

**"Authentication failed" error:** API key expired or revoked. Generate a new one in Open WebUI → Settings → Account → API Keys (requires Enable API Keys + Default Permissions to be on in Admin Panel), then update `~/.config/nc-sync/env`.

**Files in KB but RAG returns nothing:** Check Admin → Settings → Documents — ensure the embedding model is configured. Also verify file processing completed (check the manifest for `processing_failed` entries).

**Force full re-sync:** Delete the manifest and run again:

```bash
rm ~/nc-sync-manifest-Nextcloud_Documents.json
sudo systemctl start nc-knowledge-sync.service
```

**Check sync logs:**

```bash
journalctl -u nc-knowledge-sync.service --no-pager --since "1 hour ago"
```

---

## Phase 8: Tailscale Certificate Renewal

Tailscale HTTPS certificates are issued via Let's Encrypt and expire every **90 days**. Rather than renewing manually each quarter, a systemd timer handles it automatically.

### 8.1 Create the Renewal Script

```bash
sudo nano /usr/local/bin/tailscale-cert-renew.sh
```

Paste (replace `vpi5-llm.your-tailnet.ts.net` with your actual Pi 2 Tailscale hostname, and `YOUR_USERNAME` with your Pi username):

```bash
#!/bin/bash
HOSTNAME="vpi5-llm.your-tailnet.ts.net"
CERT_DIR="/etc/tailscale/certs"

tailscale cert "$HOSTNAME"

# Only copy + reload if the cert has actually changed
if ! diff -q "${HOSTNAME}.crt" "${CERT_DIR}/${HOSTNAME}.crt" > /dev/null 2>&1; then
    cp "${HOSTNAME}.crt" "${CERT_DIR}/"
    cp "${HOSTNAME}.key" "${CERT_DIR}/"
    chmod 640 "${CERT_DIR}/${HOSTNAME}.key"
    docker compose -f /home/YOUR_USERNAME/openwebui/docker-compose.yml exec nginx nginx -s reload
    logger "tailscale-cert-renew: cert renewed and nginx reloaded"
fi

rm -f "${HOSTNAME}.crt" "${HOSTNAME}.key"
```

```bash
sudo chmod +x /usr/local/bin/tailscale-cert-renew.sh
```

### 8.2 Create the Systemd Service

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

### 8.3 Create the Systemd Timer

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

### 8.4 Enable and Start

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

## Architecture

```
┌──────────┐    Tailscale     ┌──────────────────────────────────┐
│  iPhone  │  ── encrypted ──→│         Raspberry Pi 2           │
│  iPad    │                  │         (192.168.1.52)           │
│  Mac     │  ←────────────── │                                  │
└──────────┘                  │  Docker:                         │
                              │  ├─ Nginx (443)                  │
                              │  └─ Open WebUI (8080)            │
                              │       ↑                          │
                              │       │ REST API (upload + index)│
                              │       │                          │
                              │  Host:                           │
                              │  ├─ Ollama (11434)               │
                              │  └─ nc-knowledge-sync.py         │
                              │       ↑ reads files              │
                              │       │                          │
                              │  /mnt/nextcloud (WebDAV mount)   │
                              └──────────────────────────────────┘
                                          │
                                   WebDAV over Tailscale
                                          │
                              ┌──────────────────────────────────┐
                              │         Raspberry Pi 1           │
                              │  └─ Nextcloud                    │
                              └──────────────────────────────────┘
```

---

## Verification

After a clean reboot:

```bash
sudo reboot
```

Reconnect and check:

```bash
# Tailscale running?
sudo tailscale status

# LAN route fix active?
ip rule show | grep "priority 100"
# Should show: 100: from all to 192.168.1.0/24 lookup main
sudo systemctl status lan-route-fix --no-pager

# Ollama running?
sudo systemctl status ollama --no-pager
ollama list

# Docker containers up?
docker ps

# Nextcloud mounted?
ls /mnt/nextcloud

# Knowledge sync timer active?
systemctl list-timers | grep nc-knowledge
# Should show nc-knowledge-sync.timer with next/last run times

# Cert renewal timer active?
systemctl list-timers | grep tailscale-cert
# Should show tailscale-cert-renew.timer with next scheduled run

# Accessible from Tailscale?
curl -sk https://localhost | grep -i "open webui"
```

From a client device: open `https://YOUR_PI2_TAILSCALE_IP` — the Open WebUI login should appear.

From the LAN: `ssh username@192.168.1.52` should also connect.

---

## Maintenance

**Updating models:**

```bash
# Pull updated version of a model
ollama pull mistral:7b

# Remove old or unused models
ollama rm modelname
ollama list
```

**Updating Open WebUI:**

> `apt full-upgrade` does not update Open WebUI — it only updates OS packages. To update Open WebUI, use `docker compose pull` below.

```bash
cd ~/openwebui
docker compose pull
docker compose up -d
docker image prune -f
```

**Monitoring Ollama activity:**

```bash
# What models are currently loaded in memory and their status
ollama ps
# Shows: model name, size, processor (CPU%), context window, unload timer

# Live Ollama logs — see inference requests, load times, errors
journalctl -u ollama -n 20 --no-pager

# Follow logs in real time while a request is processing
journalctl -u ollama -f

# Unload a specific model from memory immediately
ollama stop mistral:7b

# List all downloaded models with sizes
ollama list
```

Key things to look for in logs:
- `llama runner started in Xs` — model load time
- `200 | Xs | POST /api/chat` — completed request with duration
- `loaded runners count=N` — how many models are in memory simultaneously (keep at 1 for best performance)

**Checking inference performance:**

```bash
# Run a timed test
time ollama run mistral:7b "Summarise the concept of data sovereignty in two sentences."

# Monitor RAM usage during inference
watch -n 1 free -h
```

Expected: a 7B model response in 30–90 seconds depending on prompt length. RAM usage during inference should stay under 12GB, leaving headroom for the OS and Open WebUI.

**Temperature monitoring:**

```bash
vcgencmd measure_temp
# Expected: 40–55°C under inference load with active cooling
```

**Tailscale certificate renewal:**

Handled automatically by the monthly systemd timer set up in Phase 8. To check status or trigger manually:

```bash
# Check timer
systemctl list-timers | grep tailscale-cert

# Manual trigger
sudo /usr/local/bin/tailscale-cert-renew.sh
journalctl -u tailscale-cert-renew --no-pager --since "5 min ago"
```

**Knowledge Base sync:**

The sync runs automatically every 6 hours via systemd timer. To trigger a manual sync:

```bash
sudo systemctl start nc-knowledge-sync.service
journalctl -u nc-knowledge-sync.service --no-pager --since "5 min ago"
```

To force a full re-sync (re-uploads everything):

```bash
rm ~/nc-sync-manifest-Nextcloud_Documents.json
sudo systemctl start nc-knowledge-sync.service
```

---

## Security Checklist

- ✅ SSH key-only authentication (shared key from Pi 1 setup)
- ✅ UFW firewall (deny all incoming except SSH, Tailscale, HTTPS)
- ✅ Fail2ban (auto-ban brute force attempts)
- ✅ Tailscale (WireGuard-based mesh, no port forwarding needed)
- ✅ LAN routing fix (policy rule forces LAN traffic via eth0, bypassing Tailscale table 52)
- ✅ Open WebUI behind Tailscale only (no public internet exposure)
- ✅ Open WebUI HTTPS via Tailscale certificates (auto-renewed monthly via systemd timer)
- ✅ Ollama bound to localhost (not exposed directly on network)
- ✅ Nextcloud WebDAV mounted read-only
- ✅ Knowledge sync API key stored with restricted permissions (chmod 600)
- ✅ No Mullvad (not needed — no traffic routing, no internet exit)
- ✅ NVMe boot (reliable, fast storage)
- ✅ Active cooling (temperature-controlled)

---

## Models Reference

| Model | Size | Best For |
|---|---|---|
| `llama3.2:3b` | ~2GB | Fast responses, quick lookups, lightweight tasks |
| `mistral:7b` | ~4.5GB | General purpose — best quality/speed balance |
| `qwen2.5-coder:7b` | ~4.7GB | Code generation, review, debugging (recommended over codellama) |
| `llama3.1:8b` | ~5GB | Stronger reasoning, longer context |

Run only one large model at a time on 16GB RAM. Ollama unloads models from memory after a period of inactivity (default 5 minutes), so switching between models is safe.

---

## Credits & Resources

- [Ollama](https://ollama.com)
- [Open WebUI Documentation](https://docs.openwebui.com)
- [Tailscale Documentation](https://tailscale.com/kb/)
- [davfs2 Manual](https://savannah.nongnu.org/projects/davfs2)

---

**Last Updated:** March 2026 (replaced manual RAG workaround with automated API-based Knowledge Base sync from Nextcloud; added automated Tailscale certificate renewal via systemd timer)
**Tested On:** Raspberry Pi 5 (16GB), Raspberry Pi OS Lite Bookworm (64-bit), Ollama, Open WebUI, Docker, Nginx