# Snowflake Release Notifier — Complete Setup Guide

**Version:** 1.0  
**Date:** 2026-05-30  
**Status:** Production-ready  
**Covers:** ntfy setup · notifier script · digest page · ntfy web UI fix · selector diagnostics

This guide is a single, linear walkthrough. Follow it top to bottom on a fresh Pi homelab setup.

---

## What this builds

```
┌──────────────── Pi 2 (vpi5-llm) ─────────────────────────┐
│                                                           │
│  08:00 — systemd timer fires                              │
│    ├─ fetch_html  →  docs.snowflake.com  (release notes)  │
│    ├─ fetch_rss   →  medium.com/snowflake (blog)          │
│    │                                                      │
│    ├─ filter:  html_scrape — dedupe only (no time gate)   │
│    │           rss         — 24h gate + dedupe            │
│    │                                                      │
│    ├─ score against keywords.json tiers                   │
│    ├─ top 5 → Ollama llama3.2:3b → 1-2 sentence summaries│
│    ├─ render  ~/openwebui/digest/YYYY-MM-DD.html          │
│    └─ POST → ntfy (Pi 1) → iPhone / macOS push           │
│                                                           │
│  nginx :443  /digest/  →  ~/openwebui/digest/            │
└───────────────────────────┬───────────────────────────────┘
                            │ Tailscale
                            ▼
┌──────────────── Pi 1 (vpi5) ──────────────────────────────┐
│  ntfy container                                           │
│    ├─ nginx :443  /ntfy/  → ntfy:80  (API + iOS app)      │
│    └─ nginx :8443 /       → ntfy:80  (web UI)             │
└──────────────────────────┬────────────────────────────────┘
                           │ APNS bridge (topic name only —
                           │ notification content stays on Pi 1)
                           ▼
                    iPhone / macOS
```

Nothing in this guide touches Mullvad VPN, UFW, fail2ban, Tailscale cert renewal timers, `nc-knowledge-sync`, Nextcloud, or Open WebUI.

---

## ⚙️ Customization: choose your domain

> This is the first thing to read, even before looking at the phases. The keyword tiers in `keywords.json` are the **only** thing you need to change to track a different ecosystem. Everything else — the pipeline, scoring, deduplication, Ollama summaries, digest rendering, ntfy delivery — is domain-agnostic.

### How keywords work

Items are scored against three weighted tiers. An item's score is the sum of weights for every keyword matched in its title and body text. Items with score 0 are silently dropped before summarization.

| Tier | Weight | Intent |
|---|---|---|
| `tier_1_critical` | 3 | Must-read: core platform moves, breaking changes, GA of flagship features |
| `tier_2_important` | 2 | High-signal: governance, tooling, partner integrations you actively use |
| `tier_3_adjacent` | 1 | Ecosystem noise: adjacent tech, infrastructure, connectivity |

An item matching two tier-1 keywords scores 6. An item matching one tier-1 and one tier-3 keyword scores 4. The top 5 by score get Ollama summaries; items 6–15 appear in the digest unsummarised.

### Example A — Snowflake (what ships in this repo)

Tuned for a Solution Architect focused on Cortex AI, semantic modelling, and data governance:

```json
{
  "tier_1_critical": {
    "keywords": [
      "semantic view", "cortex analyst", "cortex agent",
      "cortex search", "snowflake intelligence", "ontology", "ai/bi"
    ],
    "weight": 3,
    "description": "Core agentic BI platform"
  },
  "tier_2_important": {
    "keywords": [
      "lineage", "data governance", "aggregate awareness",
      "metric composition", "dbt semantic", "streamlit",
      "data catalog", "data visualization"
    ],
    "weight": 2,
    "description": "Semantic layer & governance"
  },
  "tier_3_adjacent": {
    "keywords": [
      "snowpark", "ai/ml", "cortex llm", "vectors",
      "dynamic tables", "iceberg"
    ],
    "weight": 1,
    "description": "Ecosystem & adjacent"
  }
}
```

### Example B — dbt / Modern Data Stack

Same infrastructure, same script, different `keywords.json`. Add the dbt blog RSS feed and dbt-core GitHub Atom release feed to `sources.json`:

```json
{
  "tier_1_critical": {
    "keywords": [
      "semantic layer", "metricflow", "saved queries",
      "dbt cloud", "dbt core", "semantic model"
    ],
    "weight": 3,
    "description": "dbt semantic layer core"
  },
  "tier_2_important": {
    "keywords": [
      "data contracts", "unit tests", "model governance",
      "dbt mesh", "dbt explorer", "exposures", "state:modified"
    ],
    "weight": 2,
    "description": "dbt governance & collaboration"
  },
  "tier_3_adjacent": {
    "keywords": [
      "snowflake", "bigquery", "databricks",
      "python models", "incremental", "jinja"
    ],
    "weight": 1,
    "description": "Adapters & ecosystem"
  }
}
```

And the matching source entries to add to `sources.json`:

```json
{
  "name": "dbt_blog",
  "url": "https://www.getdbt.com/blog/rss.xml",
  "type": "rss",
  "category": "Official",
  "emoji": "🟠",
  "enabled": true
},
{
  "name": "dbt_core_releases",
  "url": "https://github.com/dbt-labs/dbt-core/releases.atom",
  "type": "rss",
  "category": "Official",
  "emoji": "📦",
  "enabled": true
}
```

### To adapt this for any other domain

1. Replace `keywords.json` with tiers that reflect what you actually care about.
2. Add RSS feeds or HTML-scrape targets for that domain to `sources.json`.
3. Leave the script, systemd units, and digest template unchanged — they are keyword-agnostic.

The digest page header says "Snowflake Pulse" — if you fork for a different domain, change the `wordmark` string in `style.css` and the `build_body` title in the script.

---

## Placeholders

Fill these in before running any command. You will discover them at the steps indicated.

| Placeholder | What it is | Where to find it |
|---|---|---|
| `YOUR_PI1_TAILSCALE_HOSTNAME` | Pi 1 full Tailscale FQDN (e.g. `REDACTED_TAILSCALE_HOST`) | `tailscale status --self --json \| grep DNSName` on Pi 1 |
| `YOUR_PI2_TAILSCALE_HOSTNAME` | Pi 2 full Tailscale FQDN | same command on Pi 2 |
| `YOUR_PI2_USERNAME` | Pi 2 OS username | `whoami` on Pi 2 |
| `YOUR_NTFY_TOKEN` | Bearer token for posting to ntfy | Phase A, Step A.5 |
| `YOUR_GITHUB_USERNAME` | GitHub handle | https://github.com/settings/profile |

---

## Phase A — Pi 1: ntfy server setup

All steps in this phase run on **Pi 1** via SSH.

### A.1 — Confirm Tailscale hostname

```bash
tailscale status --self --json | grep DNSName
```

Record the FQDN. This is `YOUR_PI1_TAILSCALE_HOSTNAME` throughout.

### A.2 — Add ntfy to the existing Docker Compose file

```bash
nano ~/nextcloud/docker-compose.yml
```

In the `services:` block, add after the last existing service (before the `volumes:` key):

```yaml
  ntfy:
    image: binwiederhier/ntfy:latest
    restart: always
    command: serve
    environment:
      NTFY_BASE_URL: "https://YOUR_PI1_TAILSCALE_HOSTNAME"
      NTFY_LISTEN_HTTP: ":80"
      NTFY_BEHIND_PROXY: "true"
      NTFY_AUTH_FILE: "/var/lib/ntfy/user.db"
      NTFY_AUTH_DEFAULT_ACCESS: "deny-all"
      NTFY_CACHE_FILE: "/var/lib/ntfy/cache.db"
      NTFY_ATTACHMENT_CACHE_DIR: "/var/lib/ntfy/attachments"
      NTFY_UPSTREAM_BASE_URL: "https://ntfy.sh"
    volumes:
      - ntfy:/var/lib/ntfy
    expose:
      - "80"
```

In the `volumes:` block at the bottom, add `ntfy:`:

```yaml
volumes:
  db:
  nextcloud:
  ntfy:
```

### A.3 — Add the `/ntfy/` nginx server block (API path)

```bash
nano ~/nextcloud/nginx/default.conf
```

Inside the existing `server { listen 443 ssl; ... }` block, add before `location /`:

```nginx
    # ntfy — subpath for API and iOS app
    location /ntfy/ {
        proxy_pass         http://ntfy:80/;
        proxy_http_version 1.1;
        proxy_set_header   Host            $host;
        proxy_set_header   X-Real-IP       $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        # SSE support
        proxy_set_header   Connection      "";
        chunked_transfer_encoding on;
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
    }
```

### A.4 — Add a dedicated server block on port 8443 (ntfy web UI)

The ntfy web app uses root-relative asset paths and cannot be hosted under a subpath (upstream issue #1009). A dedicated server block on a separate port is the correct fix.

At the **bottom** of the nginx config file, outside any existing `server {}` block, add:

```nginx
# ntfy web UI — full root on port 8443
server {
    listen      8443 ssl;
    server_name YOUR_PI1_TAILSCALE_HOSTNAME;

    ssl_certificate     /etc/tailscale/certs/YOUR_PI1_TAILSCALE_HOSTNAME.crt;
    ssl_certificate_key /etc/tailscale/certs/YOUR_PI1_TAILSCALE_HOSTNAME.key;

    location / {
        proxy_pass         http://ntfy:80/;
        proxy_http_version 1.1;
        proxy_set_header   Host            $host;
        proxy_set_header   X-Real-IP       $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_set_header   Connection      "";
        chunked_transfer_encoding on;
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
    }
}
```

Replace both occurrences of `YOUR_PI1_TAILSCALE_HOSTNAME` with the FQDN from A.1.

Expose port 8443 in the nginx container's `ports:` block in `docker-compose.yml`:

```yaml
  nginx:
    ports:
      - "443:443"
      - "8443:8443"    # ntfy web UI
```

### A.5 — Start ntfy and create admin + token

```bash
cd ~/nextcloud
docker compose up -d
docker compose exec ntfy ntfy user add --role=admin admin
docker compose exec ntfy ntfy token add --user admin snowflake-notifier-write
```

The second command prints a token. Record it — this is `YOUR_NTFY_TOKEN`.

### A.6 — Verify

```bash
# ntfy API (subpath)
curl -sk -H "Authorization: Bearer YOUR_NTFY_TOKEN" \
  https://YOUR_PI1_TAILSCALE_HOSTNAME/ntfy/v1/health
# Expected: {"healthy":true}

# ntfy web UI (dedicated port)
curl -sk https://YOUR_PI1_TAILSCALE_HOSTNAME:8443/ | grep -i "<title>"
# Expected: <title>ntfy web</title>

# Test POST from Pi 1 → itself
curl -s -H "Authorization: Bearer YOUR_NTFY_TOKEN" \
  -d "ntfy is alive" \
  https://YOUR_PI1_TAILSCALE_HOSTNAME/ntfy/snowflake_releases
```

**Phase A complete when:** the health check returns `{"healthy":true}` and the test POST returns a JSON message object.

---

## Phase B — Pi 2: dependencies and configuration files

All steps in this phase run on **Pi 2** via SSH.

### B.1 — Install system packages

```bash
sudo apt install python3-requests python3-feedparser python3-bs4 \
  python3-dateutil python3-jinja2 -y

python3 -c "import requests, feedparser, bs4, dateutil, jinja2; print('ok')"
```

Expected: `ok`

### B.2 — Create directories

```bash
sudo mkdir -p /etc/snowflake-notifier/templates
sudo mkdir -p /etc/snowflake-notifier
mkdir -p ~/.config/snowflake-notifier
mkdir -p ~/.local/share/snowflake-notifier
```

### B.3 — Write the env file

```bash
nano ~/.config/snowflake-notifier/env
```

Paste (replace placeholders):

```bash
NTFY_URL="https://YOUR_PI1_TAILSCALE_HOSTNAME/ntfy/snowflake_releases"
NTFY_TOKEN="YOUR_NTFY_TOKEN"
DIGEST_DIR="/home/YOUR_PI2_USERNAME/openwebui/digest"
DIGEST_BASE_URL="https://YOUR_PI2_TAILSCALE_HOSTNAME/digest"
```

```bash
chmod 600 ~/.config/snowflake-notifier/env
```

### B.4 — Write `keywords.json`

```bash
sudo nano /etc/snowflake-notifier/keywords.json
```

Paste the keyword tier that matches your domain. The Snowflake-tuned example ships in this repo and is reproduced here:

```json
{
  "tier_1_critical": {
    "keywords": [
      "semantic view", "cortex analyst", "cortex agent",
      "cortex search", "snowflake intelligence", "ontology", "ai/bi"
    ],
    "weight": 3,
    "description": "Core agentic BI platform"
  },
  "tier_2_important": {
    "keywords": [
      "lineage", "data governance", "aggregate awareness",
      "metric composition", "dbt semantic", "streamlit",
      "data catalog", "data visualization"
    ],
    "weight": 2,
    "description": "Semantic layer & governance"
  },
  "tier_3_adjacent": {
    "keywords": [
      "snowpark", "ai/ml", "cortex llm", "vectors",
      "dynamic tables", "iceberg"
    ],
    "weight": 1,
    "description": "Ecosystem & adjacent"
  }
}
```

See the [Customization section](#️-customization-choose-your-domain) above for a dbt-focused alternative and guidance on writing your own.

### B.5 — Write `sources.json`

```bash
sudo nano /etc/snowflake-notifier/sources.json
```

Paste:

```json
{
  "feeds": [
    {
      "name": "snowflake_release_notes",
      "url": "https://docs.snowflake.com/en/release-notes/new-features",
      "type": "html_scrape",
      "category": "Official",
      "emoji": "🎉",
      "parse_rules": {
        "selector_item": "li.font-normal",
        "selector_title": "a",
        "date_from_title": true,
        "selector_content": null,
        "href_must_contain": "/release-notes/2"
      },
      "enabled": true
    },
    {
      "name": "snowflake_medium",
      "url": "https://medium.com/feed/snowflake",
      "type": "rss",
      "category": "Official",
      "emoji": "📢",
      "_comment": "Official Snowflake publication on Medium.",
      "enabled": true
    },
    {
      "name": "snowflake_community",
      "url": "https://community.snowflake.com/s/forum",
      "type": "html_scrape_js",
      "category": "Community",
      "emoji": "💬",
      "_comment": "Salesforce Experience Cloud — JS-rendered, needs Playwright. Out of scope for v1.",
      "enabled": false
    },
    {
      "name": "osi_releases",
      "url": "https://github.com/open-semantic-interchange/OSI/releases.atom",
      "type": "rss",
      "category": "Spec",
      "emoji": "📋",
      "_comment": "No releases yet. Flip enabled to true when the first release lands.",
      "enabled": false
    }
  ]
}
```

**Why these selectors for `snowflake_release_notes`:** Snowflake migrated from a Sphinx-based docs site to a Tailwind CSS framework. The old selectors (`li.toctree-l1` / `a.reference.internal`) no longer exist. The new structure uses `li.font-normal` with `a` throughout the left-side navigation panel. `href_must_contain: "/release-notes/2"` is a clean allow-list that keeps year-versioned content (`/release-notes/2026/...`, `/release-notes/2027/...`) and excludes navigation items, driver changelogs, and other non-release pages. See [Selector diagnostics](#selector-diagnostics) if the selector needs refreshing in future.

### B.6 — Verify `keywords.json` parses

```bash
python3 -c "
import json
from pathlib import Path
data = json.loads(Path('/etc/snowflake-notifier/keywords.json').read_text())
for name, tier in data.items():
    print(f\"{name}: weight={tier['weight']}, {len(tier['keywords'])} keywords\")
"
```

---

## Phase C — Digest page hosting on Pi 2

### C.1 — Create the digest directory

```bash
mkdir -p ~/openwebui/digest
chmod 755 ~/openwebui/digest
```

### C.2 — Mount it into the nginx container

```bash
nano ~/openwebui/docker-compose.yml
```

In the `nginx:` service `volumes:` block, add one line:

```yaml
  nginx:
    volumes:
      - ./nginx/openwebui.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/tailscale/certs:/etc/tailscale/certs:ro
      - ./digest:/var/www/digest:ro     # NEW
```

### C.3 — Add the `/digest/` nginx location

```bash
nano ~/openwebui/nginx/openwebui.conf
```

Inside `server { listen 443 ssl; ... }`, **before** `location /`, add:

```nginx
    location /digest/ {
        alias /var/www/digest/;
        autoindex off;
        index index.html;
        default_type text/html;
        add_header Cache-Control "public, max-age=300";
        try_files $uri $uri/ =404;
    }
```

### C.4 — Apply and verify

```bash
cd ~/openwebui
docker compose up -d
docker compose exec nginx nginx -t
docker compose exec nginx nginx -s reload

echo "<h1>digest hosting works</h1>" > ~/openwebui/digest/index.html
curl -sk https://YOUR_PI2_TAILSCALE_HOSTNAME/digest/
# Expected: <h1>digest hosting works</h1>

rm ~/openwebui/digest/index.html
```

### C.5 — Write the digest stylesheet

```bash
nano ~/openwebui/digest/style.css
```

Paste:

```css
/* Snowflake Pulse — digest stylesheet */

@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;1,9..144,400;1,9..144,500&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root {
    --bg: #faf8f2;          --ink: #14141c;        --ink-muted: #5b5b66;
    --rule: #d9d4c4;        --accent: #1a1a2e;     --highlight: #c2410c;
    --gold: #b8860b;        --card: #ffffff;       --pill: #efe9d9;
    --max-width: 760px;
}

* { box-sizing: border-box; }
html, body {
    margin: 0; padding: 0; background: var(--bg); color: var(--ink);
    font-family: 'IBM Plex Sans', -apple-system, system-ui, sans-serif;
    font-size: 17px; line-height: 1.55; -webkit-font-smoothing: antialiased;
}
.wrap { max-width: var(--max-width); margin: 0 auto; padding: 64px 28px 96px; }

/* Masthead */
.masthead { border-bottom: 1px solid var(--rule); padding-bottom: 28px; margin-bottom: 48px; }
.masthead .wordmark {
    font-family: 'Fraunces', Georgia, serif; font-style: italic; font-weight: 500;
    font-size: 42px; letter-spacing: -0.01em; color: var(--ink); margin: 0 0 6px; line-height: 1;
}
.masthead .meta {
    font-family: 'IBM Plex Mono', monospace; font-size: 12px;
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-muted);
}
.masthead .date { font-size: 15px; color: var(--ink-muted); margin-top: 8px; }

/* Section labels */
.section-label {
    font-family: 'IBM Plex Mono', monospace; font-size: 11px; letter-spacing: 0.16em;
    text-transform: uppercase; color: var(--ink-muted); margin: 56px 0 20px;
    padding-bottom: 8px; border-bottom: 1px solid var(--rule);
}
.section-label:first-of-type { margin-top: 0; }

/* Top item cards */
.item { display: grid; grid-template-columns: 56px 1fr; column-gap: 20px;
        padding: 24px 0; border-bottom: 1px solid var(--rule); }
.item:last-child { border-bottom: none; }
.item .num {
    font-family: 'Fraunces', Georgia, serif; font-style: italic; font-weight: 400;
    font-size: 36px; line-height: 1; color: var(--accent); padding-top: 4px;
}
.item .title {
    font-family: 'Fraunces', Georgia, serif; font-weight: 500; font-size: 22px;
    line-height: 1.25; color: var(--ink); margin: 0 0 10px;
    text-decoration: none; display: block;
}
.item .title:hover { color: var(--highlight); }
.item .summary { font-size: 16px; color: var(--ink); margin: 0 0 14px; }
.item .meta-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 10px; }
.item .stars { color: var(--gold); letter-spacing: 0.04em; font-size: 14px; }
.pill {
    font-family: 'IBM Plex Mono', monospace; font-size: 11px;
    background: var(--pill); padding: 3px 9px; border-radius: 12px; color: var(--ink-muted);
}
.source-link { font-size: 12px; color: var(--ink-muted); word-break: break-all; }

/* Also today */
.also { list-style: none; margin: 0; padding: 0; }
.also li { display: flex; gap: 16px; padding: 14px 0; border-bottom: 1px solid var(--rule); font-size: 15px; }
.also li:last-child { border-bottom: none; }
.also .src { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--ink-muted); display: block; margin-top: 4px; word-break: break-all; }

/* Archive list */
.archive-list { list-style: none; margin: 0; padding: 0; }
.archive-list li { display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid var(--rule); }
.archive-list li:last-child { border-bottom: none; }
.archive-list a { color: var(--ink); text-decoration: none; font-weight: 500; }
.archive-list a:hover { color: var(--highlight); }
.archive-list .count { font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--ink-muted); }

/* Degraded banner */
.degraded {
    background: #fef3c7; border: 1px solid #fbbf24; border-radius: 8px;
    padding: 12px 16px; font-size: 14px; margin-bottom: 32px;
}

/* Footer */
footer { margin-top: 64px; padding-top: 24px; border-top: 1px solid var(--rule);
         font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--ink-muted); }
footer a { color: var(--ink-muted); }
```

### C.6 — Write the digest HTML template

```bash
sudo nano /etc/snowflake-notifier/templates/digest.html.j2
```

Paste:

```jinja2
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snowflake Pulse — {{ date_long }}</title>
<link rel="stylesheet" href="/digest/style.css">
</head>
<body>
<div class="wrap">

    <header class="masthead">
        <h1 class="wordmark">Snowflake Pulse</h1>
        <div class="meta">Daily ecosystem digest · self-hosted</div>
        <div class="date">{{ date_long }}</div>
    </header>

    {% if degraded %}
    <div class="degraded">⚠ Summaries unavailable on this run (Ollama offline). Items listed with excerpts only.</div>
    {% endif %}

    {% if top %}
    <div class="section-label">Top today</div>
    {% for item in top %}
    <article class="item">
        <div class="num">{{ loop.index }}</div>
        <div>
            <a class="title" href="{{ item.url }}" target="_blank" rel="noopener">{{ item.emoji }} {{ item.title }}</a>
            <p class="summary">{{ item.summary }}</p>
            <div class="meta-row">
                {% if item.stars %}<span class="stars">{{ item.stars }}</span>{% endif %}
                {% for kw in item.matched %}<span class="pill">{{ kw }}</span>{% endfor %}
            </div>
            <a class="source-link" href="{{ item.url }}" target="_blank" rel="noopener">{{ item.url }}</a>
        </div>
    </article>
    {% endfor %}
    {% endif %}

    {% if additional %}
    <div class="section-label">Also today</div>
    <ol class="also">
    {% for item in additional %}
        <li>
            <div>
                <a href="{{ item.url }}" target="_blank" rel="noopener">{{ item.emoji }} {{ item.title }}</a>
                <span class="src">{{ item.url }}</span>
            </div>
        </li>
    {% endfor %}
    </ol>
    {% endif %}

    <footer>
        Generated {{ generated_at }} · Pi 2 · <a href="/digest/">archive</a>
    </footer>

</div>
</body>
</html>
```

### C.7 — Write the archive index template

```bash
sudo nano /etc/snowflake-notifier/templates/index.html.j2
```

Paste:

```jinja2
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snowflake Pulse — archive</title>
<link rel="stylesheet" href="/digest/style.css">
</head>
<body>
<div class="wrap">

    <header class="masthead">
        <h1 class="wordmark">Snowflake Pulse</h1>
        <div class="meta">Daily ecosystem digest · archive</div>
        <div class="date">{{ entries|length }} day{{ '' if entries|length == 1 else 's' }} on file</div>
    </header>

    <ul class="archive-list">
    {% for entry in entries %}
        <li>
            <a href="{{ entry.filename }}">{{ entry.date_long }}</a>
            <span class="count">{{ entry.item_count }} item{{ '' if entry.item_count == 1 else 's' }}</span>
        </li>
    {% endfor %}
    </ul>

    <footer>Self-hosted · Pi 2</footer>

</div>
</body>
</html>
```

---

## Phase D — Write the notifier script

All steps run on **Pi 2**.

### D.1 — Create the script

```bash
nano ~/notify_snowflake_releases.py
```

Paste the complete script below. This is v1.3 — it incorporates all patches from earlier versions: type-aware time filter, updated Snowflake docs selectors, href filtering, corrected date regex for the new Tailwind docs format, and dynamic base URL construction.

```python
#!/usr/bin/env python3
"""
Snowflake Release Notifier  v1.3
Spec: SNOWFLAKE_RELEASE_NOTIFIER_SPEC_20260505.md Rev 2.1

Changelog:
  v1.1 — Digest HTML page + ntfy Click/Actions headers
  v1.2 — Type-aware time filter: html_scrape bypasses 24h gate
  v1.3 — Fix stale Snowflake docs selectors (Sphinx → Tailwind migration):
          • New source URL + selectors in sources.json
          • extract_date_from_title: handles dash-separated and date-range formats
          • fetch_html: dynamic base_url, href_must_contain filter
          • WARNING log when html_scrape source returns 0 entries
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from jinja2 import Environment, FileSystemLoader, select_autoescape

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NTFY_URL        = os.environ["NTFY_URL"]
NTFY_TOKEN      = os.environ["NTFY_TOKEN"]
OLLAMA_URL      = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
KEYWORDS_FILE   = os.environ.get("KEYWORDS_FILE", "/etc/snowflake-notifier/keywords.json")
SOURCES_FILE    = os.environ.get("SOURCES_FILE", "/etc/snowflake-notifier/sources.json")
STATE_DIR       = Path(os.environ.get("STATE_DIR",
                   str(Path.home() / ".local/share/snowflake-notifier")))
DIGEST_DIR      = os.environ.get("DIGEST_DIR", "/home/vincepi/openwebui/digest")
DIGEST_BASE_URL = os.environ["DIGEST_BASE_URL"]
TEMPLATE_DIR    = "/etc/snowflake-notifier/templates"

SUMMARY_CACHE_FILE = STATE_DIR / "summary_cache.json"
NOTIFIED_URLS_FILE = STATE_DIR / "notified_urls.json"
RUN_STATE_FILE     = STATE_DIR / "run_state.json"

OLLAMA_TIMEOUT   = 240   # seconds — cold model load on Pi 2 ~60s after reboot
NTFY_RETRY_DELAYS = [2, 4, 8]

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        log.warning("Could not read %s: %s", path, e)
    return default

def save_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        log.error("Could not write %s: %s — attempting /tmp fallback", path, e)
        try:
            fallback = Path("/tmp") / path.name
            fallback.write_text(json.dumps(data, indent=2, default=str))
            log.warning("Saved to fallback: %s", fallback)
        except Exception as e2:
            log.error("Fallback write also failed: %s", e2)

def prune_dict_by_date(items: dict, max_days: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).date().isoformat()
    return {k: v for k, v in items.items() if str(v) >= cutoff}

# ---------------------------------------------------------------------------
# Keyword scoring
# ---------------------------------------------------------------------------

def load_keywords(path: str) -> list:
    """Returns list of (weight, [keywords]) tuples."""
    data = json.loads(Path(path).read_text())
    tiers = []
    for tier_data in data.values():
        tiers.append((tier_data["weight"], [kw.lower() for kw in tier_data["keywords"]]))
    return tiers

def score_item(title: str, text: str, tiers: list) -> tuple:
    combined = (title + " " + text).lower()
    score = 0
    matched = set()
    for weight, keywords in tiers:
        for kw in keywords:
            if kw in combined:
                score += weight
                matched.add(kw)
    return score, sorted(matched)

def stars_for_score(score: int) -> str:
    if score >= 3: return "⭐⭐⭐"
    if score == 2: return "⭐⭐"
    if score == 1: return "⭐"
    return ""

# ---------------------------------------------------------------------------
# Text sanitization
# ---------------------------------------------------------------------------

def sanitize_for_ollama(text: str, max_chars: int = 2000) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]

# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_rss(source: dict) -> list:
    url = source["url"]
    log.info("Fetching RSS: %s", url)
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        log.warning("RSS fetch failed for %s: %s", url, e)
        return []

    items = []
    for entry in feed.entries:
        title = getattr(entry, "title", "")
        link  = getattr(entry, "link", "")
        if not link:
            continue
        pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if pub is None:
            log.warning("No date for entry '%s' in %s — skipping", title, url)
            continue
        published_at = datetime(*pub[:6], tzinfo=timezone.utc)
        if hasattr(entry, "content") and entry.content:
            raw_text = entry.content[0].value
        elif hasattr(entry, "summary"):
            raw_text = entry.summary
        else:
            raw_text = ""
        items.append({
            "title":        title,
            "url":          link,
            "published_at": published_at,
            "raw_text":     raw_text,
            "source":       source["name"],
            "source_type":  "rss",
            "emoji":        source["emoji"],
        })

    log.info("  fetched %d entries from %s", len(items), source["name"])
    return items

def extract_date_from_title(title: str):
    """
    Handles four date formats found in Snowflake release notes:

    1. Feature update (current):   "May 28, 2026 - Gemini 3.5 Flash..."
    2. Weekly bundle (current):    "May 22-27, 2026 - 10.19 Release Notes"
    3. Announcement (legacy):      "May 5, 2026: Some feature (GA)"
    4. Bundle range (legacy):      "10.15 Release Notes: Apr 24, 2026-May 2, 2026"

    Formats 1 and 3 are unified into a single pattern (dash or colon after date).
    Format 2 uses a date range; returns the start date.
    """
    # Patterns 1 + 3: single date at start followed by " - " or ":"
    m = re.search(r'^(\w+ \d+,\s*\d{4})\s*[-:]', title)
    if m:
        return m.group(1)
    # Pattern 2: date range at start "Month DD-DD, YYYY - ..."
    m = re.search(r'^(\w+ \d+)-\d+,\s*(\d{4})\s+-', title)
    if m:
        return f"{m.group(1)}, {m.group(2)}"
    # Pattern 4 (legacy): single date at end after dash
    m = re.search(r'-\s*(\w+ \d+,\s*\d{4})\s*$', title)
    if m:
        return m.group(1)
    return None

def fetch_html(source: dict) -> list:
    url = source["url"]
    rules = source.get("parse_rules", {})
    log.info("Fetching HTML: %s", url)
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "snowflake-notifier/1.0"})
        resp.raise_for_status()
    except Exception as e:
        log.warning("HTML fetch failed for %s: %s", url, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    container_sel         = rules.get("selector_item", "")
    title_sel             = rules.get("selector_title", "")
    content_sel           = rules.get("selector_content", "")
    date_from_title       = rules.get("date_from_title", False)
    href_must_contain     = rules.get("href_must_contain", "")
    href_must_not_contain = rules.get("href_must_not_contain", "")

    if not container_sel or container_sel.startswith("TBD"):
        log.warning("parse_rules not filled in for %s — skipping HTML scrape", source["name"])
        return []

    # Derive base URL from the source URL so root-relative hrefs resolve correctly.
    # (Hardcoding a path prefix breaks when the docs site restructures its URLs.)
    parsed_source = urlparse(url)
    base_url = f"{parsed_source.scheme}://{parsed_source.netloc}"

    for container in soup.select(container_sel):
        title_el = container.select_one(title_sel) if title_sel else None
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        # Build absolute URL
        link = ""
        if title_el and title_el.name == "a" and title_el.get("href"):
            href = title_el["href"]
            link = href if href.startswith("http") else base_url + "/" + href.lstrip("/")
        if not link:
            link = url + "#" + re.sub(r"\W+", "-", title.lower())[:60]

        # Apply href allow-list / deny-list filters.
        # href_must_contain="/release-notes/2" captures versioned content across years
        # while excluding navigation items, driver changelogs, etc.
        if href_must_contain and href_must_contain not in link:
            continue
        if href_must_not_contain and href_must_not_contain in link:
            continue

        # Extract date
        if date_from_title:
            date_str = extract_date_from_title(title)
            if not date_str:
                log.warning("Could not extract date from title '%s' — skipping", title[:80])
                continue
        else:
            date_el = container.select_one(rules.get("selector_date", "")) \
                      if rules.get("selector_date") else None
            date_str = date_el.get_text(strip=True) if date_el else ""

        try:
            published_at = dateutil_parser.parse(date_str, fuzzy=True)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except Exception:
            log.warning("Could not parse date '%s' for '%s' — skipping", date_str, title[:60])
            continue

        content_el = container.select_one(content_sel) if content_sel else None
        raw_text = content_el.get_text(separator=" ", strip=True) if content_el else ""

        items.append({
            "title":        title,
            "url":          link,
            "published_at": published_at,
            "raw_text":     raw_text,
            "source":       source["name"],
            "source_type":  "html_scrape",
            "emoji":        source["emoji"],
        })

    log.info("  fetched %d entries from %s", len(items), source["name"])
    if len(items) == 0:
        log.warning(
            "  html_scrape source '%s' returned 0 entries — CSS selector may be stale. "
            "See 'Selector diagnostics' in the guide.",
            source["name"],
        )
    return items

# ---------------------------------------------------------------------------
# Filter pipeline
# ---------------------------------------------------------------------------

def filter_items(items: list, notified_urls: dict) -> list:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    # html_scrape sources bypass the time gate: the page lists the full index on
    # every scrape, so "new" means not yet in notified_urls, not "published today".
    # RSS sources keep the 24h gate (timestamps are accurate; prevents surfacing
    # weeks-old blog posts if the feed contains backfilled entries).
    after_time = []
    rss_skipped = 0
    for i in items:
        if i.get("source_type") == "html_scrape":
            after_time.append(i)
        elif i["published_at"] >= cutoff:
            after_time.append(i)
        else:
            rss_skipped += 1
    log.info(
        "After time filter: %d / %d items (%d RSS item(s) too old, html_scrape bypasses)",
        len(after_time), len(items), rss_skipped,
    )

    after_dedup = [i for i in after_time if i["url"] not in notified_urls.get("items", {})]
    log.info("After dedupe filter: %d / %d items", len(after_dedup), len(after_time))

    return after_dedup

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def ollama_healthy() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def ollama_summarize(text: str) -> str:
    sanitized = sanitize_for_ollama(text)
    prompt = (
        "You are summarizing a technical article for a daily Snowflake digest.\n"
        "Write 1-2 sentences in plain English. Keep technical terms "
        "(Cortex, semantic view, lineage, etc.) verbatim.\n"
        "Do not start with 'This article' or 'The post'. Do not add a preamble.\n\n"
        f"Article:\n{sanitized}\n\nSummary:"
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 100},
    }
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        raise RuntimeError(f"Ollama generate failed: {e}") from e

# ---------------------------------------------------------------------------
# Digest HTML rendering
# ---------------------------------------------------------------------------

def _stars_label(score: int) -> str:
    if score >= 3: return "★★★"
    if score == 2: return "★★"
    if score == 1: return "★"
    return ""

def render_digest(top_5: list, additional: list, degraded: bool, run_dt) -> str:
    """Render today's digest HTML, write it to DIGEST_DIR, rebuild the archive index."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"]),
    )
    items_top = [
        {
            "title":   it["title"],
            "summary": it.get("summary", ""),
            "url":     it["url"],
            "emoji":   it.get("emoji", ""),
            "matched": it.get("matched", []),
            "stars":   _stars_label(it.get("score", 0)),
        }
        for it in top_5
    ]
    items_add = [
        {"title": it["title"], "url": it["url"], "emoji": it.get("emoji", "")}
        for it in additional
    ]

    date_long = run_dt.strftime("%A, %-d %B %Y")
    date_iso  = run_dt.strftime("%Y-%m-%d")
    filename  = f"{date_iso}.html"

    html = env.get_template("digest.html.j2").render(
        date_long=date_long,
        top=items_top,
        additional=items_add,
        degraded=degraded,
        generated_at=run_dt.strftime("%Y-%m-%d %H:%M %Z"),
    )
    out_path = Path(DIGEST_DIR) / filename
    out_path.write_text(html, encoding="utf-8")
    log.info("Digest page written: %s", out_path)

    # Rebuild archive index from all YYYY-MM-DD.html files in the digest dir
    entries = []
    for p in sorted(Path(DIGEST_DIR).glob("????-??-??.html"), reverse=True):
        try:
            d = datetime.strptime(p.stem, "%Y-%m-%d")
        except ValueError:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        item_count = text.count('class="item"') + text.count("<li>")
        entries.append({
            "filename":   p.name,
            "date_long":  d.strftime("%A, %-d %B %Y"),
            "item_count": item_count,
        })

    index_html = env.get_template("index.html.j2").render(entries=entries)
    (Path(DIGEST_DIR) / "index.html").write_text(index_html, encoding="utf-8")
    log.info("Digest archive index rebuilt with %d entries", len(entries))

    return f"{DIGEST_BASE_URL.rstrip('/')}/{filename}"

# ---------------------------------------------------------------------------
# Notification format
# ---------------------------------------------------------------------------

def build_body(top_5: list, additional: list, degraded: bool) -> str:
    date_str = datetime.now().strftime("%B %-d, %Y")
    lines = [f"🎉 SNOWFLAKE PULSE — {date_str}", ""]

    if top_5:
        lines += ["━" * 39, "TOP TODAY", "━" * 39, ""]
        for i, item in enumerate(top_5, 1):
            stars = stars_for_score(item["score"])
            kws   = ", ".join(item["matched"])
            lines.append(f"{i}. {item['emoji']} {item['title']}")
            lines.append(item["summary"])
            lines.append(f"{stars} {kws}")
            lines.append(f"→ {item['url']}")
            lines.append("")

    if additional:
        lines += ["━" * 39, "ALSO TODAY", "━" * 39, ""]
        for i, item in enumerate(additional, len(top_5) + 1):
            lines.append(f"{i}. {item['emoji']} {item['title']}")
            lines.append(f"   → {item['url']}")
            lines.append("")

    if degraded:
        lines.append("⚠️ Summaries unavailable (Ollama offline)")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# ntfy POST with retry
# ---------------------------------------------------------------------------

def _build_tags(top_5: list) -> str:
    """
    Build a comma-separated ntfy tag string.
    'snowflake' always included (renders as ❄️ in the app).
    'fire' if any item scores tier-1 (≥3).
    'tada' if a release-notes item is in the top 5.
    """
    tags = ["snowflake"]
    if any(it.get("score", 0) >= 3 for it in top_5):
        tags.append("fire")
    if any(it.get("source") == "snowflake_release_notes" for it in top_5):
        tags.append("tada")
    if len(tags) < 4 and top_5:
        tags.append("bell")
    return ",".join(tags)

def post_ntfy(body: str, digest_url: str, top_5: list) -> bool:
    headers = {
        "Authorization": f"Bearer {NTFY_TOKEN}",
        "Title":         "Snowflake Pulse",
        "Priority":      "default",
        "Tags":          _build_tags(top_5),
        "Click":         digest_url,
        "Actions":       f"view, Open digest, {digest_url}, clear=true",
    }
    for attempt, delay in enumerate([0] + NTFY_RETRY_DELAYS, 1):
        if delay:
            log.warning("ntfy retry %d in %ds...", attempt, delay)
            time.sleep(delay)
        try:
            r = requests.post(NTFY_URL, data=body.encode(), headers=headers, timeout=15)
            if r.status_code in (200, 201):
                log.info("ntfy POST succeeded (attempt %d)", attempt)
                return True
            if r.status_code in (401, 403):
                log.error("ntfy auth error %d — token invalid, not retrying", r.status_code)
                return False
            log.warning("ntfy POST %d on attempt %d", r.status_code, attempt)
        except Exception as e:
            log.warning("ntfy POST exception on attempt %d: %s", attempt, e)
    log.error("ntfy POST failed after all retries")
    return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_start = time.time()
    log.info("=== snowflake-notifier run start ===")

    tiers       = load_keywords(KEYWORDS_FILE)
    sources_cfg = json.loads(Path(SOURCES_FILE).read_text())["feeds"]

    summary_cache = load_json(SUMMARY_CACHE_FILE, {"version": 1, "items": {}})
    notified_urls = load_json(NOTIFIED_URLS_FILE, {"version": 1, "items": {}})

    summary_cache["items"] = prune_dict_by_date(summary_cache.get("items", {}), 90)
    notified_urls["items"] = prune_dict_by_date(notified_urls.get("items", {}), 30)

    all_items = []
    for src in sources_cfg:
        if not src.get("enabled", False):
            log.info("Skipping disabled source: %s", src["name"])
            continue
        src_type = src.get("type", "")
        if src_type == "rss":
            all_items.extend(fetch_rss(src))
        elif src_type == "html_scrape":
            all_items.extend(fetch_html(src))
        else:
            log.info("Skipping unsupported source type '%s': %s", src_type, src["name"])

    fetched_count = len(all_items)
    log.info("Total items fetched: %d", fetched_count)

    filtered = filter_items(all_items, notified_urls)

    for item in filtered:
        item["score"], item["matched"] = score_item(
            item["title"], sanitize_for_ollama(item["raw_text"]), tiers
        )

    scored = [i for i in filtered if i["score"] > 0]
    log.info("After score-zero drop: %d / %d items", len(scored), len(filtered))
    scored.sort(key=lambda x: x["score"], reverse=True)

    top_5      = scored[:5]
    additional = scored[5:15]

    if not top_5 and not additional:
        log.info("No items survived filtering — skipping notification")
        save_run_state(run_start, "no_items", fetched_count, 0, 0, 0, 0, 0)
        sys.exit(0)

    ollama_ok  = ollama_healthy()
    degraded   = False
    ollama_calls = 0
    cache_hits   = 0

    if not ollama_ok:
        log.warning("Ollama unreachable — falling back to title + excerpt for top 5")
        degraded = True

    for item in top_5:
        url = item["url"]
        if url in summary_cache.get("items", {}):
            item["summary"] = summary_cache["items"][url]["summary"]
            cache_hits += 1
            log.info("Cache hit: %s", item["title"][:60])
        elif ollama_ok:
            try:
                item["summary"] = ollama_summarize(item["raw_text"])
                summary_cache.setdefault("items", {})[url] = {
                    "title":            item["title"],
                    "summary":          item["summary"],
                    "summary_date":     datetime.now(timezone.utc).isoformat(),
                    "source":           item["source"],
                    "keywords_matched": item["matched"],
                }
                ollama_calls += 1
                log.info("Summarized: %s", item["title"][:60])
            except RuntimeError as e:
                log.warning("Ollama failed for '%s': %s — using excerpt", item["title"][:60], e)
                item["summary"] = sanitize_for_ollama(item["raw_text"])[:200]
                degraded = True
        else:
            item["summary"] = sanitize_for_ollama(item["raw_text"])[:200]

    # Render the digest page before posting — the Click URL must resolve when tapped.
    run_dt = datetime.now()
    try:
        digest_url = render_digest(top_5, additional, degraded, run_dt)
    except Exception as e:
        log.error("Digest rendering failed: %s — falling back to archive URL", e)
        digest_url = f"{DIGEST_BASE_URL.rstrip('/')}/"

    body   = build_body(top_5, additional, degraded)
    posted = post_ntfy(body, digest_url, top_5)

    if posted:
        today = datetime.now(timezone.utc).date().isoformat()
        for item in top_5 + additional:
            notified_urls.setdefault("items", {})[item["url"]] = today
        save_json(SUMMARY_CACHE_FILE, summary_cache)
        save_json(NOTIFIED_URLS_FILE, notified_urls)
        log.info("State files updated")
    else:
        save_json(SUMMARY_CACHE_FILE, summary_cache)
        log.error("ntfy POST failed — notified_urls NOT updated (items remain eligible tomorrow)")

    status = "success" if posted else "ntfy_failed"
    save_run_state(
        run_start, status, fetched_count,
        len(scored), len(top_5), len(additional),
        ollama_calls, cache_hits,
    )
    log.info("=== run complete: %s | %.1fs ===", status, time.time() - run_start)
    sys.exit(0 if posted else 1)

def save_run_state(start, status, fetched, after_filter, top5, additional, calls, hits):
    duration = round(time.time() - start, 1)
    save_json(RUN_STATE_FILE, {
        "last_run_at":                 datetime.now(timezone.utc).isoformat(),
        "last_run_status":             status,
        "last_run_items_fetched":      fetched,
        "last_run_items_after_filter": after_filter,
        "last_run_top_5_count":        top5,
        "last_run_additional_count":   additional,
        "last_run_ollama_calls":       calls,
        "last_run_ollama_cache_hits":  hits,
        "last_run_duration_seconds":   duration,
    })

if __name__ == "__main__":
    main()
```

### D.2 — Place the script in the system bin

```bash
sudo cp ~/notify_snowflake_releases.py /usr/local/bin/notify_snowflake_releases.py
sudo chmod +x /usr/local/bin/notify_snowflake_releases.py
python3 -m ast.parse /usr/local/bin/notify_snowflake_releases.py && echo "syntax ok"
```

---

## Phase E — Systemd units

### E.1 — Create the wrapper script

```bash
sudo nano /usr/local/bin/snowflake-notifier-run.sh
```

Paste (replace `YOUR_PI2_USERNAME`):

```bash
#!/bin/bash
set -e

ENV_FILE="/home/YOUR_PI2_USERNAME/.config/snowflake-notifier/env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: Config not found at $ENV_FILE"
    exit 1
fi
set -a
source "$ENV_FILE"
set +a

exec python3 /usr/local/bin/notify_snowflake_releases.py
```

```bash
sudo chmod +x /usr/local/bin/snowflake-notifier-run.sh
```

### E.2 — Create the service unit

```bash
sudo nano /etc/systemd/system/snowflake-notifier.service
```

Paste (replace `YOUR_PI2_USERNAME`):

```ini
[Unit]
Description=Snowflake daily release / community notifier
After=network-online.target ollama.service
Wants=network-online.target ollama.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/snowflake-notifier-run.sh
User=YOUR_PI2_USERNAME
StandardOutput=journal
StandardError=journal
TimeoutStartSec=300
```

### E.3 — Create the timer unit

```bash
sudo nano /etc/systemd/system/snowflake-notifier.timer
```

Paste:

```ini
[Unit]
Description=Daily 08:00 trigger for Snowflake notifier

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` fires a missed run on next boot if Pi 2 was off at 08:00.

### E.4 — Enable the timer

```bash
sudo systemctl daemon-reload
sudo systemctl enable snowflake-notifier.timer
sudo systemctl start snowflake-notifier.timer
systemctl list-timers | grep snowflake-notifier
```

---

## Phase F — iOS app and first run

### F.1 — Install the ntfy iOS app

1. Install **ntfy** from the App Store (by Philipp Heckel).
2. Go to Settings → set **Default server** to `https://YOUR_PI1_TAILSCALE_HOSTNAME/ntfy/`.
3. Subscribe to topic `snowflake_releases` (note the trailing `s`).
4. In the topic settings, set the **Bearer token** to `YOUR_NTFY_TOKEN`.
5. Make sure Tailscale is active on your iPhone.

**APNS bridge note:** background push on iOS routes a wake-up signal (topic name + your server URL — not message content) through ntfy.sh's APNS bridge. Content fetches directly from your Pi 1 over Tailscale. If you want zero ntfy.sh involvement, open the app manually to receive via SSE.

### F.2 — Manual run

```bash
sudo systemctl start snowflake-notifier.service
journalctl -u snowflake-notifier -f
```

Expected log shape with v1.3:

```
2026-05-30 08:00:05 INFO    === snowflake-notifier run start ===
2026-05-30 08:00:05 INFO    Fetching HTML: https://docs.snowflake.com/en/release-notes/new-features
2026-05-30 08:00:07 INFO      fetched 34 entries from snowflake_release_notes
2026-05-30 08:00:07 INFO    Fetching RSS: https://medium.com/feed/snowflake
2026-05-30 08:00:09 INFO      fetched 10 entries from snowflake_medium
2026-05-30 08:00:09 INFO    Total items fetched: 44
2026-05-30 08:00:09 INFO    After time filter: 44 / 44 items (0 RSS item(s) too old, html_scrape bypasses)
2026-05-30 08:00:09 INFO    After dedupe filter: 8 / 44 items
2026-05-30 08:00:09 INFO    After score-zero drop: 3 / 8 items
2026-05-30 08:00:35 INFO    ntfy POST succeeded (attempt 1)
2026-05-30 08:00:35 INFO    Digest page written: /home/vincepi/openwebui/digest/2026-05-30.html
2026-05-30 08:00:35 INFO    === run complete: success | 30.4s ===
```

On the first run, most release notes entries will be new (not yet in `notified_urls.json`). They pass dedupe, are scored, and the zero-score ones are dropped. Those that match your keywords surface in the notification. All of them, scored or not, are added to `notified_urls.json` and won't reappear tomorrow.

### F.3 — Inspect state files

```bash
cat ~/.local/share/snowflake-notifier/run_state.json

cat ~/.local/share/snowflake-notifier/notified_urls.json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print('notified urls:', len(d.get('items',{})))"
```

### F.4 — Verify graceful degradation

```bash
sudo systemctl stop ollama
sudo systemctl start snowflake-notifier.service
journalctl -u snowflake-notifier --no-pager --since "2 min ago" | grep -i ollama
# Expected: WARNING  Ollama unreachable — falling back to title + excerpt for top 5
sudo systemctl start ollama
```

---

## Selector diagnostics

The Snowflake docs site has restructured at least once. When `snowflake_release_notes` returns 0 entries, the selector has drifted.

### Check current selectors

```bash
journalctl -u snowflake-notifier --since "today" | grep -E "fetched|WARNING"
```

If you see `WARNING ... 0 entries — CSS selector may be stale`, run the live diagnostic:

```bash
curl -s "https://docs.snowflake.com/en/release-notes/new-features" | \
python3 -c "
import sys
from bs4 import BeautifulSoup
soup = BeautifulSoup(sys.stdin.read(), 'html.parser')
seen = set()
for a in soup.find_all('a', href=True):
    if '/release-notes/' in a['href'] and a.get_text().strip():
        li = a.find_parent('li')
        if li and id(li) not in seen:
            seen.add(id(li))
            print(f'li={li.get(\"class\")} | a={a.get(\"class\")} | {a.get_text().strip()[:60]}')
" | head -20
```

This prints the CSS classes of every `<li>/<a>` pair that links to a release note page. Update `selector_item` and `selector_title` in `/etc/snowflake-notifier/sources.json` to match.

### Check that href filtering is still effective

After the diagnostic, confirm the `href_must_contain` filter still correctly excludes non-release items (navigation, driver changelogs) while keeping year-versioned content:

```bash
curl -s "https://docs.snowflake.com/en/release-notes/new-features" | \
python3 -c "
import sys
from bs4 import BeautifulSoup
soup = BeautifulSoup(sys.stdin.read(), 'html.parser')
kept, dropped = [], []
for a in soup.find_all('a', href=True):
    href = a['href']
    if '/release-notes/2' in href:
        kept.append(href)
    else:
        dropped.append(href)
kept = [h for h in kept if '/clients-drivers' not in h]
print(f'kept: {len(kept)} links')
for h in kept[:5]: print(' ', h)
print(f'dropped: {len(dropped)} links (nav, drivers, etc.)')
" 2>/dev/null
```

If the pattern breaks, adjust `href_must_contain` in `sources.json` to the new URL structure.

---

## Updating keywords

Keywords in `/etc/snowflake-notifier/keywords.json` take effect on the next run — no script restart needed. To test a change immediately:

```bash
sudo nano /etc/snowflake-notifier/keywords.json
# make changes

python3 -c "
import json
from pathlib import Path
data = json.loads(Path('/etc/snowflake-notifier/keywords.json').read_text())
for name, tier in data.items():
    print(f\"{name}: weight={tier['weight']} — {tier['keywords']}\")
"

sudo systemctl start snowflake-notifier.service
journalctl -u snowflake-notifier -f
```

---

## Troubleshooting

### `fetched 0 entries from snowflake_release_notes`

The CSS selector is stale. Run the selector diagnostic above. Update `sources.json`. Restart the service.

### Only Medium blog items appear in notifications

Two separate causes, both required to fix:

1. **Selector stale** — `fetch_html` returns 0 items, so only RSS items reach the filter. Fix: update selectors (Phase B.5).
2. **Time gate too strict** — even with working selectors, release notes entries that are 1–6 days old are dropped by the 24h filter. Fix: the type-aware `filter_items` in v1.2+ handles this; confirm v1.3 is in place.

With v1.3 installed and the correct selectors, both sources should appear every day.

### `Could not extract date from title '...' — skipping`

Title format doesn't match any of the four patterns in `extract_date_from_title`. Paste the actual title text and inspect. Common cause: a new docs page format without a leading date. The entry can be safely ignored if it's not a release note (e.g. a "What's new" overview page).

### ntfy push not arriving on iPhone

1. Confirm Tailscale is active on the iPhone.
2. Check the iOS ntfy app is set to the correct server and token.
3. Test POST manually from Pi 2: `curl -s -H "Authorization: Bearer YOUR_NTFY_TOKEN" -d "test" https://YOUR_PI1_TAILSCALE_HOSTNAME/ntfy/snowflake_releases`
4. If the POST succeeds but no notification appears, check APNS: the iOS app must be subscribed to the same topic name and have `NTFY_UPSTREAM_BASE_URL=https://ntfy.sh` set in the ntfy container env.

### Digest page returns 404

Check the nginx container has the volume mount (`./digest:/var/www/digest:ro`) and the `/digest/` location block is present in the config. Verify with `docker compose exec nginx nginx -t`.

### Ollama slow or timing out

Cold model load on Pi 2 takes ~60s after reboot. `OLLAMA_TIMEOUT=240` is intentionally generous. If it times out regularly, check `ollama ps` for memory pressure.
