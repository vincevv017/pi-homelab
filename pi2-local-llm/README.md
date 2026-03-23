# <img src="https://cdn.simpleicons.org/raspberrypi/A22846" height="22" align="center" /> Pi 2 — Local LLM Node

See [guide.md](./guide.md) for the complete setup guide.

**What this node does:**
- Runs LLM inference locally via Ollama (Llama 3.2, Mistral 7B, Qwen 2.5 Coder)
- Serves Open WebUI — a private ChatGPT-like interface accessible over Tailscale
- Mounts Nextcloud documents from Pi 1 via WebDAV and indexes them for RAG

**Services:** Ollama · Open WebUI · Nginx · Docker · davfs2

## Scripts

### [`scripts/nc-knowledge-sync.py`](./scripts/nc-knowledge-sync.py)

Syncs files from a local directory (e.g. `/mnt/nextcloud` mounted via WebDAV) into an Open WebUI Knowledge Base via the REST API.

**Features:**
- Creates the knowledge base if it doesn't exist
- Incremental sync — only uploads new or changed files (SHA-256 hashing)
- Removes deleted files from the KB
- Waits for async file processing before adding to KB
- Dry-run mode for safe testing
- Designed to run as a systemd timer (every 6 hours)

**Usage:**
```bash
# Dry run — see what would change
python3 scripts/nc-knowledge-sync.py \
  --url https://YOUR_PI2_TAILSCALE_IP \
  --token sk-your-api-key \
  --knowledge "Nextcloud Documents" \
  --folder /mnt/nextcloud/Documents \
  --dry-run

# Full sync
python3 scripts/nc-knowledge-sync.py \
  --url https://YOUR_PI2_TAILSCALE_IP \
  --token sk-your-api-key \
  --knowledge "Nextcloud Documents" \
  --folder /mnt/nextcloud/Documents
```

See [guide.md § Phase 7](./guide.md) for full setup including systemd timer configuration.
