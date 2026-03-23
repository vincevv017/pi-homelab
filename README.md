<div align="center">

# <img src="https://cdn.simpleicons.org/raspberrypi/A22846" height="28" align="center" /> Pi Homelab

### Private Cloud & Local AI on Raspberry Pi 5

*Your data. Your hardware. Your rules.*

<br/>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](./LICENSE)
[![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi%205-c51a4a?style=flat-square&logo=raspberrypi&logoColor=white)](https://www.raspberrypi.com/)
[![Tailscale](https://img.shields.io/badge/network-Tailscale-232f3e?style=flat-square)](https://tailscale.com/)
[![Mullvad VPN](https://img.shields.io/badge/VPN-Mullvad-44ad8e?style=flat-square)](https://mullvad.net/)
[![Nextcloud](https://img.shields.io/badge/cloud-Nextcloud-0082C9?style=flat-square&logo=nextcloud&logoColor=white)](https://nextcloud.com/)
[![Ollama](https://img.shields.io/badge/LLM-Ollama-black?style=flat-square)](https://ollama.com/)
[![Open WebUI](https://img.shields.io/badge/UI-Open%20WebUI-grey?style=flat-square)](https://openwebui.com/)
[![Docker](https://img.shields.io/badge/containers-Docker-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docker.com/)

<br/>

<!-- ═══════════════════════════════════════════════════════════
     HERO IMAGE — replace with a photo of your hardware setup
     Both Pis side by side in their metal cases works well
     Ideal dimensions: 1200×600px
     ═══════════════════════════════════════════════════════════ -->
<!-- ![Pi Homelab hardware](./assets/hero.jpg) -->

</div>

---

## Why

Cloud AI and cloud storage are convenient. They're also a continuous decision on your behalf: where your data lives, who can access it, what happens to it, and how much it costs next quarter.

**API pricing shifts. Data residency regulations evolve. Vendor lock-in compounds.** These aren't hypothetical concerns — they're active risks, especially for anyone operating in a regulated industry or across jurisdictions.

This stack addresses them directly: LLM inference runs on your hardware, files are stored on your own server, and all access goes through an encrypted tunnel you control. No data leaves your network. No usage limits. No third-party dependency on the inference path.

---

## Architecture

```
┌─────────────────────────────┐
│   iPhone  /  iPad  /  Mac   │
│   • Tailscale client        │
│   • Nextcloud iOS app       │
│   • Open WebUI (browser)    │
└──────────┬──────────────────┘
           │  WireGuard (Tailscale mesh)
           │
     ┌─────┴──────────────────────────────────────────┐
     │              Raspberry Pi 1                    │
     │         VPN Exit Node & Personal Cloud         │
     │         LAN: 192.168.1.50 / NVMe              │
     │                                               │
     │  ┌──────────────┐   ┌────────────────────┐   │
     │  │  Mullvad VPN │   │     Nextcloud       │   │
     │  │  Swiss exit  │   │  Nginx · MariaDB    │   │
     │  │  nft watchdog│   │  Docker · TLS       │   │
     │  └──────────────┘   └────────────────────┘   │
     └─────────────────────────┬──────────────────────┘
                               │  WebDAV over Tailscale
                               │
     ┌─────────────────────────┴──────────────────────┐
     │              Raspberry Pi 2                    │
     │             Local LLM Node                     │
     │         LAN: 192.168.1.52 / NVMe              │
     │                                               │
     │  ┌──────────────┐   ┌────────────────────┐   │
     │  │    Ollama    │   │    Open WebUI       │   │
     │  │  llama3.2:3b │   │  Nginx · Docker    │   │
     │  │  mistral:7b  │   │  TLS · RAG KB      │   │
     │  │  qwen2.5:7b  │   └────────────────────┘   │
     │  └──────────────┘                             │
     │  /mnt/nextcloud ←── davfs2 + nc-sync.py       │
     └────────────────────────────────────────────────┘
```

**Exit node flow:** `Device → Tailscale → Pi 1 → Mullvad WireGuard → Internet (Swiss IP)`

**LLM flow:** `Device → Tailscale → Pi 2 → Open WebUI → Ollama`

**RAG flow:** `Pi 1 Nextcloud → WebDAV → Pi 2 /mnt/nextcloud → nc-knowledge-sync.py → Open WebUI KB`

---

## Nodes

### <img src="https://cdn.simpleicons.org/raspberrypi/A22846" height="18" align="center" /> Pi 1 — VPN Exit Node & Personal Cloud

→ [Full setup guide](./pi1-vpn-exit-node/guide.md)

<!-- ═══════════════════════════════════════════════════════════
     PI 1 IMAGE — photo of Pi 1 in its metal case
     Suggested: overhead or 3/4 angle, good lighting
     Ideal dimensions: 800×450px
     ═══════════════════════════════════════════════════════════ -->
<!-- ![Pi 1 hardware](./assets/pi1-hardware.jpg) -->

| Service | Role |
|---|---|
| **Mullvad VPN** | WireGuard tunnel — all client traffic exits via Swiss IP |
| **Tailscale** | Encrypted mesh, exit node, subnet router (`192.168.1.0/24`) |
| **Nextcloud** | Private file sync — replaces iCloud / OneDrive / Google Drive |
| **Nginx** | Reverse proxy + TLS termination |
| **MariaDB** | Nextcloud database |
| **nftables watchdog** | Keeps `tailscale0` rules intact across Mullvad reconnects |

| Component | Model |
|---|---|
| Board | Raspberry Pi 5 (4 GB) |
| Storage | EDILOCA EN605 256 GB NVMe (PCIe Gen3 x4, M.2 2280) |
| HAT | Official Raspberry Pi M.2 HAT+ (Model A) |
| Cooling | Active Cooler (temperature-controlled) |
| Case | Metal enclosure |
| Power | Official 27 W USB-C PSU |

---

### <img src="https://cdn.simpleicons.org/raspberrypi/A22846" height="18" align="center" /> Pi 2 — Local LLM Node

→ [Full setup guide](./pi2-local-llm/guide.md)

<!-- ═══════════════════════════════════════════════════════════
     PI 2 IMAGE — photo of Pi 2 in its metal case
     Same style as Pi 1 photo for visual consistency
     Ideal dimensions: 800×450px
     ═══════════════════════════════════════════════════════════ -->
<!-- ![Pi 2 hardware](./assets/pi2-hardware.jpg) -->

| Service | Role |
|---|---|
| **Ollama** | Local model runtime — downloads, manages, and serves LLMs |
| **Open WebUI** | Private ChatGPT-like interface with RAG and conversation history |
| **Nginx** | Reverse proxy + TLS termination |
| **davfs2** | Mounts Nextcloud (Pi 1) via WebDAV for document access |
| **nc-knowledge-sync** | Indexes Nextcloud files into Open WebUI Knowledge Base |

| Component | Model |
|---|---|
| Board | Raspberry Pi 5 **(16 GB)** — required for 7B models |
| Storage | PNY CS1030 250 GB NVMe (PCIe Gen3, M.2 2280) |
| HAT | Official Raspberry Pi M.2 HAT+ (Model A) |
| Cooling | Active Cooler (temperature-controlled) |
| Case | Metal enclosure |
| Power | Official 27 W USB-C PSU |

**Why 16 GB?** A 7B parameter model at 4-bit quantization needs ~4–5 GB of RAM at runtime. 16 GB gives headroom to run a model alongside the OS and Open WebUI without swapping to disk — which would make inference unusably slow on NVMe.

#### Models

| Model | Size | Best for |
|---|---|---|
| `llama3.2:3b` | ~2 GB | Fast responses, lightweight tasks |
| `mistral:7b` | ~4.5 GB | General purpose — best quality/speed balance |
| `qwen2.5-coder:7b` | ~4.7 GB | Code generation, review, debugging |

---

## Open WebUI

<!-- ═══════════════════════════════════════════════════════════
     OPEN WEBUI SCREENSHOT — browser capture of the interface
     Show the chat UI with a model responding, dark theme
     Ideal dimensions: 1200×700px
     ═══════════════════════════════════════════════════════════ -->
<!-- ![Open WebUI interface](./assets/openwebui-screenshot.png) -->

Accessible from any Tailscale-connected device at `https://YOUR_PI2_TAILSCALE_IP`. Supports model switching, conversation history, file uploads, and RAG queries against your Nextcloud Knowledge Base.

---

## Key Technical Solutions

Three non-obvious problems that required custom solutions:

**1 — Mullvad + Tailscale nftables conflict**
Mullvad regenerates its entire nftables ruleset on every reconnect and VPN location switch, silently wiping custom `tailscale0` rules. Without them, all regular TCP connections to Pi services time out (Tailscale SSH still works because it bypasses the kernel network stack). A watchdog service polls every 5 seconds and re-applies four nft rules across the input, output, and forward chains, plus the `0x6d6f6c65` split-tunnel mark rule that keeps the Tailscale coordination server reachable.

**2 — Tailscale subnet route conflict on Pi 2**
Pi 1 advertises `192.168.1.0/24` as a Tailscale subnet route. Pi 2 (running default netfilter mode) injects this into routing table 52 at priority 5270 — evaluated before the main table. LAN SSH breaks because SYN-ACK responses are sent via `tailscale0` instead of `eth0`. Fixed with a policy routing rule at priority 100 that forces LAN-destined traffic through the main table first, made persistent via a systemd oneshot service.

**3 — Open WebUI RAG from Nextcloud**
Open WebUI's "Sync Directory" feature opens a client-side file picker — it cannot reference server-side paths like `/mnt/nextcloud`. [`nc-knowledge-sync.py`](./pi2-local-llm/scripts/nc-knowledge-sync.py) uses the REST API directly: it hashes local files, uploads new or changed ones, waits for async processing, removes deleted files from the KB, and reindexes — tracking state in a local manifest so only changes are processed on each run.

---

## Repository Structure

```
pi-homelab/
│
├── README.md                              ← you are here
├── LICENSE                                ← MIT
├── .gitignore
│
├── assets/                                ← photos and screenshots
│   ├── hero.jpg                           ← both Pis side by side (hero image)
│   ├── pi1-hardware.jpg                   ← Pi 1 hardware photo
│   ├── pi2-hardware.jpg                   ← Pi 2 hardware photo
│   └── openwebui-screenshot.png           ← Open WebUI interface
│
├── pi1-vpn-exit-node/
│   ├── README.md                          ← node summary
│   └── guide.md                           ← complete setup guide (Phases 1–10)
│                                            OS flash → NVMe boot → hardening →
│                                            Mullvad → Tailscale → routing →
│                                            Nextcloud → cert renewal
│
└── pi2-local-llm/
    ├── README.md                          ← node summary + script docs
    ├── guide.md                           ← complete setup guide (Phases 4–8)
    │                                        Tailscale → Ollama → Open WebUI →
    │                                        WebDAV mount → RAG sync → cert renewal
    └── scripts/
        └── nc-knowledge-sync.py           ← Nextcloud → Open WebUI KB sync
```

---

## Security

Both nodes are accessible **exclusively through Tailscale** — no ports are forwarded on the router, no services are exposed to the public internet.

- 🔑 SSH key-only authentication (no passwords)
- 🛡️ UFW deny-all inbound (SSH · Tailscale · HTTPS only)
- 🚫 Fail2ban on SSH
- 🔒 HTTPS via Tailscale-provisioned Let's Encrypt certificates (auto-renewed monthly)
- 📁 Nextcloud WebDAV mounted read-only on Pi 2
- 🗝️ LLM API key stored `chmod 600`
- 🌐 No public IP exposure on either node

---

## Cost

| Item | Cost |
|---|---|
| Mullvad VPN | €5 / month — one account covers all devices |
| Tailscale | Free (personal, up to 100 devices) |
| Nextcloud | Free (self-hosted, open source) |
| Ollama + Open WebUI | Free (open source) |
| Electricity | ~€3 / month (two Pi 5 nodes at ~5 W idle each) |
| **Total recurring** | **~€8 / month** |

No cloud storage fees. No per-token API costs. No data leaving the network.

---

## License

[MIT](./LICENSE)
