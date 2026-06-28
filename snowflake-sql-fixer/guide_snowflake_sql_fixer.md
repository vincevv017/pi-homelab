# Snowflake → Local LLM SQL Fixer — Complete Setup Guide

**Version:** 1.8
**Date:** 2026-06-20
**Status:** WORKING end-to-end and validated. Four-case test gallery captured (syntax / cross-dialect / semantic GROUP BY / window OVER), all passing from Snowflake; off-tailnet denied with 403. Maintenance & update procedures documented.
**Covers:** External Access Integration · stored procedure · FastAPI repair endpoint · nginx PROXY-protocol front · Snowflake egress-IP allowlist · Tailscale Funnel (TLS-terminated TCP) · hardened systemd units

This guide builds a **synchronous** path from Snowflake to a local LLM on Pi 2. You call a stored procedure with a broken query and its error; Snowflake reaches out over an External Access Integration to a tightly-scoped endpoint on Pi 2; `qwen2.5-coder:7b` repairs the SQL locally; the fix comes straight back into your worksheet. The use case: a fallback SQL doctor when Cortex AI credits are exhausted, running entirely on hardware you own.

This is the synchronous, "feels native" path — Snowflake calls out to the Pi directly rather than the Pi polling Snowflake. It deliberately accepts a *narrow, authenticated, IP-filtered* public surface. That tradeoff is stated plainly in the [Security model](#security-model) section and is the most important thing to understand before you build.

---

## What this builds

```
┌──────────────────────── Snowflake (personal account) ────────────────────────┐
│                                                                               │
│  Worksheet:  CALL FIX_SQL_LOCAL('SELECT * FORM t', 'syntax error ... FORM');  │
│       │                                                                       │
│       ▼                                                                       │
│  Stored procedure FIX_SQL_LOCAL (Python)                                      │
│    ├─ reads bearer token from SECRET                                          │
│    ├─ EXTERNAL_ACCESS_INTEGRATION → allows egress to one host:port            │
│    └─ requests.post(https://<PI2_FUNNEL_FQDN>/fix-sql, {sql, error})          │
│                                  │                                            │
│  egress leaves via Snowflake's stable egress IP ranges  ─────────────┐        │
└──────────────────────────────────────────────────────────────────────┼───────┘
                                                                        │ public
                                                                        ▼ internet
┌──────────────────── Tailscale edge (public relay) ───────────────────────────┐
│  Funnel :8443  --proxy-protocol=2 --tls-terminated-tcp → localhost:9443       │
│    ├─ terminates TLS (node's Let's Encrypt cert)                              │
│    └─ prepends PROXY protocol v2 header (carries Snowflake's source IP)       │
│    (public :443 is left to the existing Open WebUI Docker nginx)              │
└──────────────────────────────────────┬────────────────────────────────────────┘
                                        │ loopback only
                                        ▼
┌──────────────────────────── Pi 2 (vpi5-llm) ─────────────────────────────────┐
│  host nginx  127.0.0.1:9443  (proxy_protocol)                                 │
│    ├─ real_ip_header proxy_protocol   → $remote_addr = Snowflake egress IP    │
│    ├─ include snowflake-egress-allow.conf ; deny all   (IP allowlist)         │
│    ├─ location = /fix-sql  → proxy_pass 127.0.0.1:8000                         │
│    └─ location /           → return 444   (everything else dropped)           │
│                                        │                                       │
│                                        ▼                                       │
│  FastAPI  127.0.0.1:8000  (localhost-only, hardened systemd unit)             │
│    ├─ Bearer token check (constant-time)                                      │
│    ├─ body validation + size caps                                             │
│    └─ POST 127.0.0.1:11434  Ollama  qwen2.5-coder:7b  (format=json)           │
│                                                                               │
│  refresh_egress_allowlist.py  (weekly timer)                                  │
│    └─ Pi 2 → Snowflake (outbound) → SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES     │
│         → rewrite snowflake-egress-allow.conf → nginx -s reload               │
└───────────────────────────────────────────────────────────────────────────────┘
```

Nothing here touches Mullvad, the notifier, Nextcloud, Open WebUI, or Pi 1. The Funnel surface lives only on Pi 2 and answers on exactly one path.

---

## Security model

Read this before building. This design opens a public endpoint; the entire point is making that opening as small and as conditional as possible.

**The honest framing:** Tailscale Funnel terminates at Tailscale's public relay and gives you a real, internet-reachable `*.ts.net` name. This is *not* "no public exposure." It is a **token-gated, IP-filtered, single-path** public endpoint. The article should say exactly that — the engineering value is in the constraints, not in pretending the hole isn't there.

**Why TCP+PROXY and not HTTP Funnel:** Funnel in HTTP mode strips the client source IP — the backend never learns who called. The `--tls-terminated-tcp` forwarder instead prepends a PROXY protocol header carrying the original source IP. That single fact is what makes the egress-IP allowlist possible. Without it, the bearer token would be your *only* gate.

**Defense in depth, outermost to innermost:**

| Layer | Control | A leaked token alone is not enough because… |
|---|---|---|
| 1 | Funnel exposes one TCP port → nginx only. Ollama (`:11434`) and the FastAPI app (`:8000`) bind loopback and are never funneled. | The model runtime is unreachable from outside. |
| 2 | nginx `return 444` on every path except `= /fix-sql`. | Probing any other URL gets a silently-dropped connection. |
| 3 | Source IP (from PROXY protocol) must match Snowflake's published egress ranges; `deny all` otherwise. | The caller must originate from Snowflake's own infrastructure. |
| 4 | Bearer token (Snowflake `SECRET`), constant-time compared in the app. | The IP allowlist alone doesn't authorise; you still need the secret. |
| 5 | Body schema + size caps (≤ 64 KB), rate limit (10 req/min). | Abuse and oversized payloads are bounded. |

Layers 3 and 4 are independent: an attacker needs *both* a valid token *and* an IP inside Snowflake's egress ranges. That AND is the whole point.

**Residual risk to disclose in the article:** the TCP connection still reaches Tailscale's relay and your Pi before nginx evaluates the allowlist (the filtering is app-layer, not edge-layer). Funnel itself can't be IP-filtered at the Tailscale edge. You are trusting Tailscale's relay and your own nginx config. State this; don't hide it.

---

## Placeholders

Fill these in before running any command. You will discover them at the steps indicated.

| Placeholder | What it is | Where to find it |
|---|---|---|
| `<PI2_FUNNEL_FQDN>` | Pi 2's public Funnel hostname (e.g. `vpi5-llm.<tailnet>.ts.net`) | `tailscale status --self --json \| grep DNSName` on Pi 2 |
| `<PI2_USERNAME>` | Pi 2 OS username | `whoami` on Pi 2 |
| `<TAILNET>` | Your tailnet name | Tailscale Admin → DNS tab |
| `<PI_TOKEN>` | Bearer token for `/fix-sql` | Phase A, Step A.2 (`openssl rand -hex 32`) |
| `<SF_ACCOUNT>` | Snowflake account identifier (`orgname-accountname`) | Snowsight → account menu, or `SELECT CURRENT_ORGANIZATION_NAME() \|\| '-' \|\| CURRENT_ACCOUNT_NAME();` |
| `<SF_WAREHOUSE>` | Warehouse the refresh job uses (XS is plenty) | your account |

The refresh job authenticates as a **dedicated `TYPE=SERVICE` user `PI_FIXER_SVC`** (role `PI_FIXER_ROLE`), created in Phase D.1 — not a personal user. Its private key lives only at `/etc/sql-fixer/sf_key.p8` on Pi 2.

**Pi 2 fixed port map (not placeholders — these are the chosen values):** Funnel public `:8443` → host nginx `127.0.0.1:9443` (proxy_protocol) → FastAPI `127.0.0.1:8000` → Ollama `127.0.0.1:11434`. Public `:443` is reserved for the existing Open WebUI Docker nginx and is never touched.

> Per repo convention: no real hostnames, IPs, tokens, or tailnet names in anything you commit. The values above live only in `chmod 600` env files on the Pi and in Snowflake objects — never in git.

---

## Phase 0 — Feasibility gate (do this first)

This design depends on Snowflake's **stable egress IP ranges**, which at time of writing are available on **AWS Commercial deployments only**. If your personal account is on Azure or GCP, the allowlist cannot be built and you must fall back to token-only (see [Fallback](#fallback-token-only-non-aws-accounts)).

### 0.1 — Confirm the egress function returns data

In a Snowsight worksheet:

```sql
SELECT SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES();
```

**Expected (AWS):** a JSON array of objects with `ipv4_prefix`, `effective`, `expires`.
**If empty / errors:** your region doesn't support stable egress IPs → use the [Fallback](#fallback-token-only-non-aws-accounts).

Flatten it to eyeball the CIDRs you'll be allowlisting:

```sql
SELECT value:"ipv4_prefix"::STRING  AS cidr,
       value:"expires"::TIMESTAMP   AS expires
FROM TABLE(FLATTEN(
  INPUT => PARSE_JSON(SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES())
));
```

### 0.2 — Confirm you can create account-level objects

External Access Integrations require `ACCOUNTADMIN` (or a role with `CREATE INTEGRATION`). On a personal account you have this; on the trial it requires a support case — which is why a real account matters here.

```sql
SELECT CURRENT_AVAILABLE_ROLES();   -- ACCOUNTADMIN should be listed
```

### 0.3 — Confirm Funnel is permitted in your tailnet

Funnel needs HTTPS Certificates on (DNS tab) **and** a `funnel` node attribute in the ACL. Note: `tailscale funnel status` returning **`No serve config` is normal here** — it means nothing is configured yet, not that Funnel is blocked. Don't treat it as a failure. Full enablement (including tag-scoping to the LLM node only) is handled in [Phase E.1](#e1--enable-funnel-on-the-tailnet-acl--https); at this stage just confirm HTTPS certs:

```bash
tailscale dns status | grep -i https
# If Pi 1's `tailscale cert` renewal already works, HTTPS certs are on tailnet-wide.
```

### 0.4 — Confirm the coder model is present on Pi 2

```bash
ollama list | grep qwen2.5-coder
# If missing:  ollama pull qwen2.5-coder:7b
```

Only proceed past here once 0.1 returns data and 0.3 reports Funnel is available.

---

## Phase A — Pi 2: the repair endpoint

All steps in this phase run on **Pi 2** via SSH.

### A.1 — Layout

```bash
mkdir -p ~/sql-fixer/app
mkdir -p ~/.config/sql-fixer
```

### A.2 — Generate the bearer token

```bash
openssl rand -hex 32
```

Record the output as `<PI_TOKEN>`. It goes into the app's env file (below) **and** into a Snowflake `SECRET` (Phase F). It is never committed.

### A.3 — App env file

```bash
nano ~/.config/sql-fixer/env
```

```bash
SQL_FIXER_TOKEN="<PI_TOKEN>"
OLLAMA_URL="http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL="qwen2.5-coder:7b"
OLLAMA_TIMEOUT="150"
MAX_BODY_BYTES="65536"
```

```bash
chmod 600 ~/.config/sql-fixer/env
```

### A.4 — The FastAPI app

```bash
nano ~/sql-fixer/app/main.py
```

```python
#!/usr/bin/env python3
"""Local SQL repair endpoint. Binds loopback only; fronted by nginx + Funnel."""
import hmac
import json
import os
import time

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

TOKEN = os.environ["SQL_FIXER_TOKEN"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "150"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "65536"))

SYSTEM_PROMPT = (
    "You are a Snowflake SQL repair assistant. You receive a broken SQL "
    "statement and the exact Snowflake error message it produced. Return ONLY "
    "a JSON object with two keys: 'fixed_sql' (the corrected statement) and "
    "'explanation' (one or two sentences on what was wrong and what you "
    "changed). Preserve the original intent, table names, and column names. "
    "Do not invent tables or columns. Target the Snowflake SQL dialect. "
    "Do not wrap the SQL in markdown or backticks."
)

app = FastAPI(title="sql-fixer", docs_url=None, redoc_url=None, openapi_url=None)


class FixRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    error: str = Field(default="", max_length=8000)
    dialect: str = Field(default="snowflake", max_length=32)


def _authorised(header_value: str | None) -> bool:
    if not header_value or not header_value.startswith("Bearer "):
        return False
    presented = header_value.split(" ", 1)[1]
    return hmac.compare_digest(presented, TOKEN)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/fix-sql")
async def fix_sql(
    req: Request,
    body: FixRequest,
    authorization: str | None = Header(default=None),
):
    if not _authorised(authorization):
        raise HTTPException(status_code=401, detail="unauthorised")

    raw = await req.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    user_msg = f"Broken SQL:\n{body.sql}\n\nSnowflake error:\n{body.error or '(none provided)'}"

    t0 = time.monotonic()
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.1},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ollama unreachable: {exc}")

    latency_ms = round((time.monotonic() - t0) * 1000)
    content = r.json().get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
        fixed_sql = parsed.get("fixed_sql", "")
        explanation = parsed.get("explanation", "")
    except json.JSONDecodeError:
        fixed_sql = content.strip()
        explanation = "Model did not return valid JSON; raw output passed through."

    return {
        "fixed_sql": fixed_sql,
        "explanation": explanation,
        "model": OLLAMA_MODEL,
        "latency_ms": latency_ms,
    }
```

### A.5 — Dependencies

```bash
sudo apt install python3-fastapi python3-pydantic python3-requests -y 2>/dev/null \
  || pip3 install --user fastapi uvicorn pydantic requests

# uvicorn for serving:
sudo apt install uvicorn -y 2>/dev/null || pip3 install --user uvicorn
```

### A.6 — Smoke test (manual, before systemd/nginx)

```bash
set -a; source ~/.config/sql-fixer/env; set +a
uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir ~/sql-fixer &
sleep 2

curl -s -X POST http://127.0.0.1:8000/fix-sql \
  -H "Authorization: Bearer $SQL_FIXER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT * FORM customers","error":"syntax error line 1 at position 9 unexpected FORM"}' \
  | python3 -m json.tool

kill %1
```

**Expected:** a JSON object with `fixed_sql` containing `SELECT * FROM customers`, an `explanation`, and a `latency_ms`. **Measured as-built (Pi 5, qwen2.5-coder:7b): ~59s cold (first call, model load) → ~18s warm.** Both sit well under every timeout in the chain (FastAPI 150s, proc 160s, nginx 180s). This is a credit-exhaustion fallback, not an interactive hot path — frame the latency accordingly.

**Negative auth test (must return 401) — do not skip; this is a core security layer:**

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir ~/sql-fixer &
sleep 2
curl -s -o /dev/null -w "bad token -> %{http_code}\n" -X POST http://127.0.0.1:8000/fix-sql \
  -H "Authorization: Bearer WRONG" -H "Content-Type: application/json" \
  -d '{"sql":"x","error":"y"}'
curl -s -o /dev/null -w "no token  -> %{http_code}\n" -X POST http://127.0.0.1:8000/fix-sql \
  -H "Content-Type: application/json" -d '{"sql":"x","error":"y"}'
kill %1
# Expected: both -> 401
```

---

## Phase B — Pi 2: hardened systemd service

### B.1 — Service unit

```bash
sudo nano /etc/systemd/system/sql-fixer.service
```

```ini
[Unit]
Description=Local SQL repair endpoint (FastAPI, loopback only)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=<PI2_USERNAME>
EnvironmentFile=/home/<PI2_USERNAME>/.config/sql-fixer/env
WorkingDirectory=/home/<PI2_USERNAME>/sql-fixer
ExecStart=/usr/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM

[Install]
WantedBy=multi-user.target
```

> `ProtectHome=read-only` lets the unit read the env file but not write your home. If `uvicorn` lives under `~/.local/bin`, use the full path in `ExecStart`.

### B.2 — Enable

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sql-fixer.service
systemctl status sql-fixer.service --no-pager
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

---

## Phase C — Pi 2: nginx PROXY-protocol front + IP allowlist

The app is loopback-only. A **host** nginx reads the PROXY protocol source IP, enforces the egress-IP allowlist, and exposes exactly one path.

> **Two-nginx note (as built on Pi 2).** Pi 2 already runs a *containerized* nginx (`nginx:alpine` in `~/openwebui/docker-compose.yml`) that owns host **`:443`** for Open WebUI + the digest. That container is **not** suitable for this job — a container's loopback isn't the host's, so it can't receive Funnel's `localhost` forward or reach FastAPI on host `127.0.0.1:8000`. So the SQL-fixer uses a separate **host** nginx (installed via `apt`). The two coexist cleanly because they never share a port: Docker nginx stays on `:443`; the host nginx listens only on `127.0.0.1:9443`. This is also why the Funnel runs on public **`:8443`** (Phase E) — `:443` belongs to Open WebUI.

### C.1 — Install the host nginx and remove its default site

```bash
# `which nginx` returns empty even if a *containerized* nginx exists — that's expected.
which nginx || sudo apt install nginx -y

# The apt package ships a default :80 site we don't want competing with anything.
sudo rm -f /etc/nginx/sites-enabled/default

# Confirm the port map: Docker nginx on :443, nothing on :9443 yet, FastAPI on :8000.
sudo ss -ltnp | grep -E ':80|:443|:8443|:8000|:9443' || true
```

### C.2 — Rate-limit zone (http context)

```bash
sudo nano /etc/nginx/conf.d/sql-fixer-limits.conf
```

```nginx
limit_req_zone $binary_remote_addr zone=fixsql:1m rate=10r/m;
```

### C.3 — Seed an empty allowlist (refresh job fills it in Phase D)

```bash
echo "# Snowflake egress ranges — generated by refresh_egress_allowlist.py" \
  | sudo tee /etc/nginx/snowflake-egress-allow.conf
echo "# (empty until first refresh — all traffic denied until then)" \
  | sudo tee -a /etc/nginx/snowflake-egress-allow.conf
```

### C.4 — Server block

```bash
sudo nano /etc/nginx/sites-available/sql-fixer
```

```nginx
server {
    # Funnel (--tls-terminated-tcp=8443) connects here over loopback,
    # speaking PROXY protocol then plaintext HTTP.
    listen 127.0.0.1:9443 proxy_protocol;

    # Trust PROXY protocol only from the local Tailscale daemon.
    set_real_ip_from 127.0.0.1;
    real_ip_header   proxy_protocol;

    # $remote_addr is now the Snowflake egress IP. Allowlist, then deny.
    include /etc/nginx/snowflake-egress-allow.conf;
    deny all;

    client_max_body_size 64k;

    location = /fix-sql {
        limit_req zone=fixsql burst=3 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host        $host;
        proxy_set_header X-Real-IP   $remote_addr;
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }

    # Everything else is silently dropped.
    location / { return 444; }
}
```

```bash
sudo ln -sf /etc/nginx/sites-available/sql-fixer /etc/nginx/sites-enabled/sql-fixer
sudo nginx -t
sudo systemctl reload nginx
```

> Until Phase D populates the allowlist, the `deny all` blocks everything — that is intentional fail-closed behaviour.

---

## Phase D — Pi 2: egress allowlist refresh (outbound, pull-based)

This is the piece that keeps the design consistent with your repo's outbound-only principle: **Pi 2 calls Snowflake**, never the reverse, to learn the current egress ranges and rewrite the nginx allowlist. Stable egress IPs expire (~90 days), so a weekly refresh keeps a comfortable margin.

### D.1 — Dedicated Snowflake service identity (least privilege)

Do **not** reuse a personal Snowflake user or copy a private key from another machine. The Pi gets its own `TYPE=SERVICE` user (passwordless, key-pair only) and generates its own key locally; only the public key goes to Snowflake.

**Privilege note:** `SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()` is effectively ACCOUNTADMIN-gated (every official example calls it as ACCOUNTADMIN). To avoid granting the Pi ACCOUNTADMIN, wrap it in an **owner's-rights** procedure that a locked-down role can be granted `USAGE` on.

Generate the Pi key (root-owned, never leaves the Pi):

```bash
sudo mkdir -p /etc/sql-fixer
sudo sh -c 'openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -nocrypt -out /etc/sql-fixer/sf_key.p8'
sudo openssl rsa -in /etc/sql-fixer/sf_key.p8 -pubout -out /etc/sql-fixer/sf_key.pub
sudo chmod 600 /etc/sql-fixer/sf_key.p8
sudo grep -v "PUBLIC KEY" /etc/sql-fixer/sf_key.pub | tr -d '\n'; echo   # body to paste below
```

Snowflake objects (run as ACCOUNTADMIN):

```sql
USE ROLE ACCOUNTADMIN;

CREATE DATABASE IF NOT EXISTS PI_FIXER;
CREATE SCHEMA  IF NOT EXISTS PI_FIXER.UTIL;

CREATE ROLE IF NOT EXISTS PI_FIXER_ROLE;
GRANT USAGE ON WAREHOUSE <SF_WAREHOUSE> TO ROLE PI_FIXER_ROLE;
GRANT USAGE ON DATABASE  PI_FIXER       TO ROLE PI_FIXER_ROLE;
GRANT USAGE ON SCHEMA    PI_FIXER.UTIL  TO ROLE PI_FIXER_ROLE;

CREATE OR REPLACE PROCEDURE PI_FIXER.UTIL.GET_EGRESS_RANGES()
  RETURNS STRING
  LANGUAGE SQL
  EXECUTE AS OWNER
AS
$$
BEGIN
  RETURN (SELECT SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES());
END;
$$;
GRANT USAGE ON PROCEDURE PI_FIXER.UTIL.GET_EGRESS_RANGES() TO ROLE PI_FIXER_ROLE;

CREATE USER IF NOT EXISTS PI_FIXER_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = PI_FIXER_ROLE
  DEFAULT_WAREHOUSE = <SF_WAREHOUSE>
  RSA_PUBLIC_KEY = '<PASTE_PI_PUBLIC_KEY_BODY>';
GRANT ROLE PI_FIXER_ROLE TO USER PI_FIXER_SVC;
```

Verify under the locked-down role before wiring the Pi:

```sql
USE ROLE PI_FIXER_ROLE;
USE WAREHOUSE <SF_WAREHOUSE>;
CALL PI_FIXER.UTIL.GET_EGRESS_RANGES();   -- expect the JSON with your CIDRs
```

Install the connector in a dedicated venv (Trixie is externally-managed; `snowflake-connector-python` isn't an apt package):

```bash
sudo apt install -y python3-venv
sudo mkdir -p /opt/sql-fixer
sudo python3 -m venv /opt/sql-fixer/.venv
sudo /opt/sql-fixer/.venv/bin/pip install snowflake-connector-python cryptography
```

### D.2 — Refresh env (root-owned)

```bash
sudo nano /etc/sql-fixer/refresh-env
```

```bash
SF_ACCOUNT="<SF_ACCOUNT>"
SF_USER="PI_FIXER_SVC"
SF_ROLE="PI_FIXER_ROLE"
SF_WAREHOUSE="<SF_WAREHOUSE>"
SF_PRIVATE_KEY="/etc/sql-fixer/sf_key.p8"
ALLOWLIST_PATH="/etc/nginx/snowflake-egress-allow.conf"
```

```bash
sudo chmod 600 /etc/sql-fixer/refresh-env
```

### D.3 — Refresh script

```bash
sudo nano /usr/local/bin/refresh_egress_allowlist.py
```

```python
#!/opt/sql-fixer/.venv/bin/python3
"""Pull Snowflake's stable egress IP ranges (via owner's-rights wrapper) and
rewrite the nginx allowlist. Outbound only: Pi -> Snowflake."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

ACCOUNT = os.environ["SF_ACCOUNT"]
USER = os.environ["SF_USER"]
ROLE = os.environ["SF_ROLE"]
WAREHOUSE = os.environ["SF_WAREHOUSE"]
KEY_PATH = os.environ["SF_PRIVATE_KEY"]
OUT = Path(os.environ["ALLOWLIST_PATH"])


def load_key() -> bytes:
    with open(KEY_PATH, "rb") as fh:
        p = serialization.load_pem_private_key(fh.read(), password=None)
    return p.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def main() -> int:
    conn = snowflake.connector.connect(
        account=ACCOUNT, user=USER, role=ROLE, warehouse=WAREHOUSE,
        private_key=load_key(),
    )
    try:
        cur = conn.cursor()
        # Owner's-rights wrapper, so the low-priv role never needs ACCOUNTADMIN.
        cur.execute("CALL PI_FIXER.UTIL.GET_EGRESS_RANGES()")
        raw = cur.fetchone()[0]
    finally:
        conn.close()

    data = json.loads(raw)
    cidrs = sorted({e["ipv4_prefix"] for e in data if e.get("ipv4_prefix")})
    if not cidrs:
        print("ERROR: no egress ranges returned — refusing to write empty allowlist",
              file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [f"# generated {stamp} — {len(cidrs)} ranges"]
    lines += [f"allow {c};" for c in cidrs]
    body = "\n".join(lines) + "\n"

    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(body)
    tmp.replace(OUT)

    subprocess.run(["nginx", "-t"], check=True)
    subprocess.run(["nginx", "-s", "reload"], check=True)
    print(f"allowlist updated: {len(cidrs)} ranges, nginx reloaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

```bash
sudo chmod 755 /usr/local/bin/refresh_egress_allowlist.py
```

> The script writes `/etc/nginx/...` and reloads nginx, so it runs as root via the systemd unit below. The venv shebang (`/opt/sql-fixer/.venv/bin/python3`) means no PATH assumptions. If you'd rather not run as root, move the nginx-touching calls behind a narrow `sudoers` entry for `nginx -t` / `nginx -s reload` and run the unit as `vincepi`.

### D.4 — First manual run

```bash
sudo bash -c 'set -a; source /etc/sql-fixer/refresh-env; set +a; /usr/local/bin/refresh_egress_allowlist.py'

cat /etc/nginx/snowflake-egress-allow.conf
# Expect:  allow 153.45.21.0/24;  and  allow 153.45.97.0/24;
```

### D.5 — Timer

```bash
sudo nano /etc/systemd/system/sql-fixer-allowlist.service
```

```ini
[Unit]
Description=Refresh Snowflake egress IP allowlist for sql-fixer
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/sql-fixer/refresh-env
ExecStart=/usr/local/bin/refresh_egress_allowlist.py
```

```bash
sudo nano /etc/systemd/system/sql-fixer-allowlist.timer
```

```ini
[Unit]
Description=Weekly Snowflake egress allowlist refresh

[Timer]
OnCalendar=Mon 07:30
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sql-fixer-allowlist.timer
systemctl list-timers sql-fixer-allowlist --no-pager
```


---

## Phase E — Pi 2: Tailscale Funnel (TLS-terminated TCP)

This is the only step that creates the public surface. It forwards public **`:8443`** to the host nginx on loopback **`:9443`**, terminating TLS at the Tailscale daemon and prepending the PROXY protocol header. **Public `:443` is deliberately left alone — it belongs to the existing Open WebUI Docker nginx.** Funnel only permits public ports 443, 8443, and 10000; we take 8443.

### E.1 — Enable Funnel on the tailnet (ACL + HTTPS) — *as built*

> Status: completed 2026-06-20. Both gates passed; Pi 2 (`vpi5-llm`) carries `tag:funnel`. The notes below record exactly what was done and the two gotchas hit, so the steps are reproducible.

Funnel has two prerequisites. Don't be misled by `tailscale funnel status` printing **`No serve config`** — that only means nothing is configured *yet*, **not** that Funnel is unavailable. The real gates are:

**(a) HTTPS Certificates — confirmed already on.** Funnel auto-provisions a Let's Encrypt cert for the node's `*.ts.net` name, which requires HTTPS Certificates enabled under Admin Console → **DNS**. Pi 1's `tailscale cert` renewal already works, so this was on tailnet-wide (same setting). Note `tailscale dns status` does **not** surface this toggle — checking for "https" there returns nothing, which is expected, not a failure. Two reliable checks:

```bash
# Visual: Admin Console → DNS → HTTPS Certificates  (shows a "Disable" button when on)
# Productive test on Pi 2 (also provisions the cert Funnel will use):
mkdir -p /tmp/certcheck && cd /tmp/certcheck
sudo tailscale cert "$(tailscale status --self --json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")"
# Success → writes <fqdn>.crt + <fqdn>.key  → HTTPS is on.
cd /tmp && rm -rf /tmp/certcheck   # diagnostic only; Funnel provisions its own cert
```

> Article hygiene: the `tailscale cert` output prints your real tailnet name (the `<tailnet>` you placeholder everywhere). Mask that line in any screenshot.

**(b) The `funnel` node attribute — the missing piece.** `tailscale serve` / `tailscale cert` do **not** need this attribute, so a tailnet set up for certs (like this one) lacks it. Scope it to the LLM node only, never the whole tailnet.

In Admin Console → **Access Controls**, switch from the **visual editor to the JSON editor** (toggle at the top of the page). The visual "access rule" form (source/destination/port/protocol) builds *grants* and cannot create `tagOwners` or `nodeAttrs` — those are separate top-level sections of the HuJSON file. Add both as siblings of `grants`:

```hujson
"tagOwners": {
	"tag:funnel": ["autogroup:admin"],
},

"nodeAttrs": [
	{"target": ["tag:funnel"], "attr": ["funnel"]},
],
```

Save (the editor validates HuJSON; trailing commas and `//` comments are allowed).

Then apply the tag to **Pi 2 only**.

> ⚠️ **Tag the LLM node only — never the exit node (Pi 1).** The funnel runs on Pi 2; Pi 1 has no role in it. Running `tailscale up` on Pi 1 is dangerous: its non-default flag set is load-bearing for Mullvad coexistence —
> `--accept-routes --accept-dns=false --advertise-exit-node --advertise-routes=192.168.1.0/24 --netfilter-mode=off`.
> In particular `--netfilter-mode=off` is what keeps Tailscale out of nftables so Mullvad's rules survive. A `--reset` or an omitted flag on Pi 1 tears down Mullvad coexistence, DNS, and the exit node at once. Do not tag Pi 1.

**Gotcha — `tailscale up` demands all non-default flags.** `tailscale up` is declarative: any flag you don't mention resets to default. On the first attempt it errors and prints the full command including current non-default flags. **Use that printed command; never `--reset`** (which would drop `--accept-routes` and with it the LAN route the `lan-route-fix.service` depends on). On Pi 2 the only non-default flag is `--accept-routes`:

```bash
sudo tailscale up --advertise-tags=tag:funnel --accept-routes
```

Verify the tag landed and routing survived the re-auth:

```bash
tailscale status --self --json | python3 -c "import sys,json; print('tags:', json.load(sys.stdin)['Self'].get('Tags'))"
# Expected: tags: ['tag:funnel']
tailscale status | grep -i "offers exit node\|192.168"   # Pi 1 still advertising exit node / route
ip rule | grep "192.168.1.0/24"                          # lan-route-fix rule still present
# If the ip rule is gone:  sudo systemctl restart lan-route-fix.service
```

> Side effect: a tagged node is owned by the tag, not your user account, so **its node key no longer expires**. Desirable for an always-on service node, but worth knowing it changed.

Alternatively, the interactive CLI flow auto-creates an `autogroup:member` funnel attr and walks you through a web consent page — broader than the tag approach, so prefer the tag scoping above for a public-facing endpoint.

### E.2 — Discover the Funnel FQDN

```bash
tailscale status --self --json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))"
# e.g. vpi5-llm.<tailnet>.ts.net  → this is <PI2_FUNNEL_FQDN>
```

### E.3 — Start the forwarder

Bring up the host nginx + the app (Phases A–C) first so something is listening on `127.0.0.1:9443`. Then run **in the foreground once** so any unmet requirement surfaces as a consent URL. **The `--proxy-protocol=2` flag is mandatory** — without it the funnel terminates TLS but does *not* prepend the PROXY header, and nginx (listening with `proxy_protocol`) drops every connection with an empty reply.

```bash
sudo tailscale funnel --proxy-protocol=2 --tls-terminated-tcp=8443 tcp://localhost:9443
# If it prints a consent link, approve it in a browser, then Ctrl-C.
```

Once it starts clean, background it:

```bash
sudo tailscale funnel --bg --proxy-protocol=2 --tls-terminated-tcp=8443 tcp://localhost:9443
tailscale funnel status
```

**Expected:** the status line must read `(TLS terminated, PROXY protocol v2)` — if it only says `(TLS terminated)`, the `--proxy-protocol=2` flag didn't take and nginx will reject everything. It should forward `https://<PI2_FUNNEL_FQDN>:8443` → `tcp://localhost:9443`, with your existing `:443` Open WebUI entry still listed and untouched. If it says `No serve config` after `--bg`, the command errored silently — re-run in the foreground and read the message.

### E.4 — Verify PROXY protocol is reaching nginx

From a device **outside** Snowflake (your Mac, off-tailnet, or via cellular) the call should be **denied** (your IP isn't in the Snowflake allowlist), which proves the allowlist is live:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<PI2_FUNNEL_FQDN>:8443/fix-sql -X POST \
  -H "Content-Type: application/json" -d '{"sql":"x","error":"y"}'
# Expected: 403  (denied by nginx allowlist) — NOT 401, NOT 200
```

If you get a connection reset / empty reply instead of 403, nginx isn't receiving the PROXY protocol correctly — see [Troubleshooting](#nginx-returns-connection-reset-or-400-on-funnel-traffic).

---

## Phase F — Snowflake: integration + stored procedure

Run these in Snowsight with `ACCOUNTADMIN` (or a role with the right grants).

### F.1 — Network rule (egress to the one host)

```sql
CREATE OR REPLACE NETWORK RULE pi_sql_fixer_rule
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('<PI2_FUNNEL_FQDN>:8443');
```

### F.2 — Secret (the bearer token from A.2)

```sql
CREATE OR REPLACE SECRET pi_sql_fixer_token
  TYPE = GENERIC_STRING
  SECRET_STRING = '<PI_TOKEN>';
```

### F.3 — External access integration

```sql
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION pi_sql_fixer_integration
  ALLOWED_NETWORK_RULES = (pi_sql_fixer_rule)
  ALLOWED_AUTHENTICATION_SECRETS = (pi_sql_fixer_token)
  ENABLED = TRUE;
```

### F.4 — Stored procedure

```sql
CREATE OR REPLACE PROCEDURE FIX_SQL_LOCAL(BROKEN_SQL STRING, ERROR_MESSAGE STRING)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = 3.11
  HANDLER = 'fix_sql'
  EXTERNAL_ACCESS_INTEGRATIONS = (pi_sql_fixer_integration)
  PACKAGES = ('snowflake-snowpark-python', 'requests')
  SECRETS = ('pi_token' = pi_sql_fixer_token)
AS
$$
import _snowflake
import requests

PI_ENDPOINT = "https://<PI2_FUNNEL_FQDN>:8443/fix-sql"

def fix_sql(session, broken_sql, error_message):
    token = _snowflake.get_generic_secret_string('pi_token')
    resp = requests.post(
        PI_ENDPOINT,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"sql": broken_sql,
              "error": error_message or "",
              "dialect": "snowflake"},
        timeout=160,
    )
    resp.raise_for_status()
    return resp.json()
$$;
```

### F.5 — Grant usage (optional, if not running as ACCOUNTADMIN day-to-day)

```sql
GRANT USAGE ON PROCEDURE FIX_SQL_LOCAL(STRING, STRING) TO ROLE <your_working_role>;
```

---

## Phase G — End-to-end test

### G.1 — The headline call

```sql
CALL FIX_SQL_LOCAL(
  'SELECT custmer_id, SUM(amount) FORM orders GROUP BY custmer_id',
  'SQL compilation error: syntax error line 1 at position 38 unexpected ''FORM'''
);
```

**Expected:** a VARIANT with `fixed_sql` (corrected `FROM`, intent preserved — note it keeps a typo'd *identifier* like `custmer_id` untouched, fixing only the actual error), `explanation`, `model`, and `latency_ms`. As-built: ~56s cold (model load), ~18s warm. The model preserving identifiers while fixing only the flagged error is the desired behavior.

### G.2 — Pull just the fixed SQL out

`FIX_SQL_LOCAL` is a **stored procedure**, so it's invoked with `CALL` — `SELECT FIX_SQL_LOCAL(...)` fails with "Unknown function" (that syntax is for UDFs). To extract a single field from a proc's VARIANT result, `RESULT_SCAN` the call:

```sql
CALL FIX_SQL_LOCAL(
  'SELECT * FORM customers WHERE created_at > current_date - 7',
  'syntax error ... unexpected FORM'
);
SELECT $1:fixed_sql::STRING AS fixed_sql
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
```

> Want the inline `SELECT FIX_SQL_LOCAL_UDF(sql, err)` experience instead? Create a scalar Python **UDF** with the same `EXTERNAL_ACCESS_INTEGRATIONS` + `SECRETS`, returning `STRING`. It reads nicer in a worksheet but Snowflake may invoke a UDF per-row with tighter timeouts, so the proc remains the safer primary for a ~20–60s Pi call.

### G.3 — Confirm the allowlist is doing its job

While the Snowflake call succeeds (Snowflake's egress IP is allowed), the same endpoint from any non-Snowflake IP returns `403` (Phase E.4). That contrast — works from Snowflake, denied from everywhere else — is the demo, and the screenshot pair worth keeping for the article.

### G.4 — Validation gallery (as-built results)

Four representative repairs, run as `CALL FIX_SQL_LOCAL(broken, error)` against the live deployment. These double as a regression suite (re-run after any change) and as article evidence — note how the *error message* is what lets the model fix semantic problems a linter couldn't. Warm latencies landed ~30–33s; first call after idle ~59s.

**1. Syntax — keyword typo**

```sql
CALL FIX_SQL_LOCAL(
  'SELECT custmer_id, SUM(amount) FORM orders GROUP BY custmer_id',
  'SQL compilation error: syntax error line 1 at position 38 unexpected ''FORM'''
);
```
→ `fixed_sql`: `SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id`
→ *"The keyword 'FORM' was misspelled as 'FROM'. I corrected the spelling..."*  (~59s cold)

**2. Cross-dialect — SQL Server functions in Snowflake**

```sql
CALL FIX_SQL_LOCAL(
  'SELECT GETDATE() AS ts, ISNULL(email, ''none'') AS email FROM customers',
  'SQL compilation error: Unknown function GETDATE'
);
```
→ `fixed_sql`: `SELECT CURRENT_TIMESTAMP AS ts, COALESCE(email, 'none') AS email FROM customers`
→ *"GETDATE is not a valid Snowflake function. Replaced with CURRENT_TIMESTAMP and ISNULL with COALESCE."*  (~31s)

**3. Semantic — missing GROUP BY column** *(the case for the synchronous design: only solvable because the error is sent)*

```sql
CALL FIX_SQL_LOCAL(
  'SELECT region, channel, SUM(revenue) AS rev FROM sales GROUP BY region',
  'SQL compilation error: ''CHANNEL'' is not a valid group by expression'
);
```
→ `fixed_sql`: `SELECT region, channel, SUM(revenue) AS rev FROM sales GROUP BY region, channel`
→ *"'CHANNEL' was not included in the GROUP BY clause. I added it to match the SELECT statement."*  (~31s)

**4. Snowflake-specific — window function needs OVER**

```sql
CALL FIX_SQL_LOCAL(
  'SELECT order_id, amount, ROW_NUMBER() AS rn FROM orders',
  'SQL compilation error: Window function [ROW_NUMBER] requires an OVER clause'
);
```
→ `fixed_sql`: `SELECT order_id, amount, ROW_NUMBER() OVER () AS rn FROM orders`
→ *"...ROW_NUMBER() window function was missing its required OVER clause. I added the OVER () clause..."*  (~33s)

> Determinism note: at `temperature 0.1` the model is near-deterministic on the *fix* but can vary on incidental choices — e.g. test 1 sometimes preserves a typo'd identifier (`custmer_id`) and sometimes normalizes it (`customer_id`). Both are valid repairs of the flagged error; don't treat minor identifier normalization as a regression.

---

## Fallback: token-only (non-AWS accounts)

If Phase 0.1 returned nothing, you can't build the IP allowlist. Keep every other layer and drop layer 3:

- Skip Phase D entirely.
- In `sites-available/sql-fixer`, remove the `include .../snowflake-egress-allow.conf;` and `deny all;` lines. Keep `proxy_protocol` (still useful for logging/rate-limit keys) and everything else.
- The bearer token + single-path + rate limit + body caps remain.

State clearly in the article that this variant rests on the token alone, and that the egress-IP layer requires an AWS-region Snowflake account.

---

## Troubleshooting

### External call returns `000` / `curl: (52) Empty reply from server` (TLS succeeds)
The funnel is terminating TLS but not sending the PROXY protocol header, so nginx (listening with `proxy_protocol`) drops the connection before any HTTP response. Check `tailscale funnel status`: if the line reads `(TLS terminated)` without `PROXY protocol v2`, the `--proxy-protocol=2` flag is missing. Restart the funnel with it: `sudo tailscale funnel --tls-terminated-tcp=8443 off` then `sudo tailscale funnel --bg --proxy-protocol=2 --tls-terminated-tcp=8443 tcp://localhost:9443`. The status must then show `(TLS terminated, PROXY protocol v2)`.

### Open WebUI became unreachable after starting the funnel
You ran Funnel on `:443` instead of `:8443`. `:443` belongs to the Open WebUI Docker nginx; Funnel on `:443` shadows it. Stop the funnel (`sudo tailscale funnel --tls-terminated-tcp=443 off` or reset the serve config) and restart it on `:8443` per Phase E.3. Confirm both entries coexist in `tailscale funnel status` / `tailscale serve status`.

### `CALL` fails with an integration / egress error
The network rule host must exactly match the Funnel FQDN and port (`<PI2_FUNNEL_FQDN>:8443`). Re-check `tailscale funnel status`. Confirm the integration is `ENABLED = TRUE` and the procedure references it.

### `CALL` returns 401 from the Pi
Token mismatch between the Snowflake `SECRET` and `~/.config/sql-fixer/env`. Regenerate once, set both sides, `systemctl restart sql-fixer`, and `CREATE OR REPLACE SECRET` again.

### nginx returns connection reset or 400 on Funnel traffic
nginx expects the PROXY protocol but isn't getting it (or vice-versa). Confirm Funnel is in `--tls-terminated-tcp` mode (not plain `--https`/HTTP serve), and that the nginx `listen` line has `proxy_protocol`. The two must agree.

### Snowflake call works but external `curl` also returns 200 (not 403)
The allowlist isn't being enforced. Likely `real_ip_header proxy_protocol;` isn't resolving — check `set_real_ip_from 127.0.0.1;` is present and that `snowflake-egress-allow.conf` actually contains `allow` lines (Phase D.4). If `$remote_addr` is staying `127.0.0.1`, every request would be allowed — verify with `proxy_protocol` in an `access_log` format temporarily.

### `403` from Snowflake too (the call you want to succeed)
Snowflake's egress IPs rotated and the allowlist is stale. Run the refresh manually (D.4). If it's chronic, shorten the timer interval.

### Ollama slow / 502 from the app
Cold load on Pi 5 is ~60s; `OLLAMA_TIMEOUT=150` covers it. If persistent, `ollama ps` for memory pressure — `qwen2.5-coder:7b` needs ~5 GB; on the 16 GB Pi 2 this is fine alongside the OS, but not if Open WebUI is also holding a model resident.

### Funnel keeps turning off after reboot
`--bg` persists, but verify with `tailscale funnel status` after a reboot. If it drops, add a small oneshot unit that re-asserts the funnel command `After=tailscaled.service`.

---

## Maintenance & updates

### Component inventory (what lives where, on Pi 2)

| Thing | Location |
|---|---|
| FastAPI app | `~/sql-fixer/app/main.py` |
| App env (bearer token, model) | `~/.config/sql-fixer/env` (chmod 600) |
| App service | `sql-fixer.service` |
| host nginx server block | `/etc/nginx/sites-available/sql-fixer` (+ symlink in `sites-enabled/`) |
| nginx rate-limit zone | `/etc/nginx/conf.d/sql-fixer-limits.conf` |
| egress allowlist (generated) | `/etc/nginx/snowflake-egress-allow.conf` |
| refresh script | `/usr/local/bin/refresh_egress_allowlist.py` (venv shebang) |
| refresh venv | `/opt/sql-fixer/.venv` |
| refresh creds (SF key + env) | `/etc/sql-fixer/` (chmod 600) |
| refresh timer | `sql-fixer-allowlist.timer` / `.service` |
| Funnel | `tailscale funnel` config (`--bg`, persisted by tailscaled) |

Snowflake side: `PI_FIXER` DB (role `PI_FIXER_ROLE`, user `PI_FIXER_SVC`, wrapper `PI_FIXER.UTIL.GET_EGRESS_RANGES`), plus `pi_sql_fixer_rule` / `pi_sql_fixer_token` / `pi_sql_fixer_integration` / `FIX_SQL_LOCAL`.

### Services reference — what each one does, ports, start/stop

Five things have to be up for a `CALL` to succeed end to end. Four are systemd units on Pi 2; the Funnel is not — it's a tailscaled-level config, not a service you can `systemctl` against.

| Service | Type | Port | What it does | Start | Stop |
|---|---|---|---|---|---|
| `sql-fixer.service` | systemd | loopback `:8000` | The FastAPI app — checks the bearer token, calls Ollama, returns the fix | `sudo systemctl start sql-fixer.service` | `sudo systemctl stop sql-fixer.service` |
| `nginx` (host instance) | systemd (apt) | loopback `:9443` for this site (`:443` is the separate Docker nginx — untouched) | PROXY-protocol front; enforces the Snowflake egress-IP allowlist and rate limit; proxies `/fix-sql` to the app, drops everything else | `sudo systemctl start nginx` | `sudo systemctl stop nginx` |
| `sql-fixer-allowlist.timer` + `.service` | systemd timer (oneshot) | none — outbound only | Pulls Snowflake's current egress IP ranges weekly and rewrites `snowflake-egress-allow.conf` | `sudo systemctl start sql-fixer-allowlist.service` (forces an immediate run) | `sudo systemctl stop sql-fixer-allowlist.timer` (cancels future runs only — doesn't touch the current allowlist file) |
| Ollama | systemd (installed by the Ollama installer; not part of this repo) | loopback `:11434` | Runs `qwen2.5-coder:7b` and serves the repair request | `sudo systemctl start ollama` | `sudo systemctl stop ollama` |
| Tailscale Funnel | tailscaled config (`--bg`), no unit file | public `:8443` → `localhost:9443` | The **only** public listener. Everything above is unreachable from outside the tailnet without this | `sudo tailscale funnel --bg --proxy-protocol=2 --tls-terminated-tcp=8443 tcp://localhost:9443` | `sudo tailscale funnel --tls-terminated-tcp=8443 off` |

Check any of them with `systemctl status <name>` (or `tailscale funnel status` for the Funnel).

**Stopping the app or nginx does not close the public opening.** They sit *behind* the Funnel, not in front of it — the Funnel's registration at Tailscale's relay is independent and persists (`--bg`) regardless of whether anything downstream is running.

| If you stop… | You lose | The Funnel hostname:8443 is still… |
|---|---|---|
| `sql-fixer.service` | The repair logic | Reachable — nginx still answers, now returns `502` |
| `nginx` | Proxying + the IP allowlist | Reachable — Funnel still forwards, now to a closed port |
| Tailscale Funnel (`off`, above) | The public listener itself | **Gone** — this is the only one that actually closes it |

For routine maintenance (restarting Ollama, redeploying the app) stopping `sql-fixer.service` is fine — the token and IP allowlist are enforced by nginx regardless, so nothing is exposed beyond a `502`. Reach for the Funnel `off` command specifically when you want the public surface itself gone — e.g. before an extended absence, or on suspected token exposure. Confirm with `tailscale funnel status`: nothing listed for `:8443` (your `:443` Open WebUI entry should still be there, untouched).

### Update the app or the prompt

```bash
nano ~/sql-fixer/app/main.py            # edit handler / SYSTEM_PROMPT
sudo systemctl restart sql-fixer.service
curl -s http://127.0.0.1:8000/healthz   # {"status":"ok"}
# then re-run the G.4 gallery as a regression check
```

### Update / change the model

```bash
ollama pull qwen2.5-coder:7b            # refresh weights
# or switch models:
nano ~/.config/sql-fixer/env            # set OLLAMA_MODEL=...
sudo systemctl restart sql-fixer.service
```

### Rotate the bearer token

Token lives in **two** places that must stay in sync — the Pi env and the Snowflake secret:

```bash
NEW=$(openssl rand -hex 32)
sed -i "s|^SQL_FIXER_TOKEN=.*|SQL_FIXER_TOKEN=\"$NEW\"|" ~/.config/sql-fixer/env
sudo systemctl restart sql-fixer.service
echo "$NEW"     # paste into the SQL below, then clear your scrollback
```
```sql
CREATE OR REPLACE SECRET pi_sql_fixer_token TYPE = GENERIC_STRING SECRET_STRING = '<NEW>';
```
Verify with a `CALL`. Rotate on any suspected exposure and on a routine cadence.

### Rotate the Snowflake service-user key

Snowflake supports two concurrent public keys for zero-downtime rotation:

```bash
sudo sh -c 'openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -nocrypt -out /etc/sql-fixer/sf_key_new.p8'
sudo openssl rsa -in /etc/sql-fixer/sf_key_new.p8 -pubout -out /etc/sql-fixer/sf_key_new.pub
sudo grep -v "PUBLIC KEY" /etc/sql-fixer/sf_key_new.pub | tr -d '\n'; echo
```
```sql
ALTER USER PI_FIXER_SVC SET RSA_PUBLIC_KEY_2 = '<NEW_BODY>';   -- add as second key
```
Point the env at the new key, confirm a refresh run works, then retire the old:
```bash
sudo mv /etc/sql-fixer/sf_key_new.p8 /etc/sql-fixer/sf_key.p8
sudo chmod 600 /etc/sql-fixer/sf_key.p8
sudo bash -c 'set -a; source /etc/sql-fixer/refresh-env; set +a; /usr/local/bin/refresh_egress_allowlist.py'
```
```sql
ALTER USER PI_FIXER_SVC SET RSA_PUBLIC_KEY = '<NEW_BODY>';   -- promote
ALTER USER PI_FIXER_SVC UNSET RSA_PUBLIC_KEY_2;              -- drop old
```

### Egress allowlist

Refreshes automatically every Monday (`sql-fixer-allowlist.timer`). Snowflake's ranges expire ~90 days, so the weekly cadence has margin. Manual refresh / inspect:

```bash
sudo systemctl start sql-fixer-allowlist.service     # force a refresh now
cat /etc/nginx/snowflake-egress-allow.conf           # current allowed CIDRs
systemctl list-timers sql-fixer-allowlist --no-pager # next/last run
journalctl -u sql-fixer-allowlist -n 20              # last run output
```
If a legitimate `CALL` ever returns `403`, the ranges likely rotated and the timer hadn't run yet — force a refresh.

### Cert & Funnel dependency

The Funnel's TLS termination needs a valid **node** cert (managed by tailscaled, separate from the Open WebUI file cert). After any node-cert event or a reboot, confirm the Funnel is up **and** still advertising PROXY v2:

```bash
tailscale funnel status     # must show: (TLS terminated, PROXY protocol v2)
```
If it dropped or lost the PROXY label, re-assert:
```bash
sudo tailscale funnel --bg --proxy-protocol=2 --tls-terminated-tcp=8443 tcp://localhost:9443
```

### Post-reboot checklist

Most things self-start (`sql-fixer.service` and `nginx` are enabled; the timer is scheduled; `--bg` Funnel persists). After a reboot, verify the chain in order:

```bash
systemctl is-active sql-fixer.service nginx                       # active / active
sudo ss -ltnp | grep -E ':443|:8443|:8000|:9443'                  # docker :443, nginx :9443, uvicorn :8000
tailscale funnel status                                            # PROXY protocol v2
curl -s http://127.0.0.1:8000/healthz                             # {"status":"ok"}
# then one CALL from Snowflake as the true end-to-end check
```
If the Funnel doesn't survive reboots reliably, add a oneshot unit `After=tailscaled.service` that re-runs the funnel command (see Troubleshooting).

### Health at a glance

```bash
systemctl --failed | grep sql-fixer            # nothing = good
journalctl -u sql-fixer -n 30 --no-pager       # app log
sudo tail -20 /var/log/nginx/error.log         # PROXY/allowlist errors surface here
```

---

## Suggested repo structure

A new top-level directory, mirroring how `snowflake-notifier/` is organised:

```
pi-homelab/
├── snowflake-notifier/                  (existing)
└── snowflake-sql-fixer/                 (new)
    ├── README.md                        # what it is, security model summary, the one diagram
    ├── guide_snowflake_sql_fixer.md     # this file
    ├── app/
    │   └── main.py                      # FastAPI repair endpoint
    ├── refresh_egress_allowlist.py      # Pi -> Snowflake egress-range puller
    ├── nginx/
    │   ├── sql-fixer.conf               # server block (sanitised, placeholders)
    │   └── sql-fixer-limits.conf        # rate-limit zone
    ├── systemd/
    │   ├── sql-fixer.service
    │   ├── sql-fixer-allowlist.service
    │   └── sql-fixer-allowlist.timer
    └── snowflake/
        ├── 01_network_rule.sql
        ├── 02_secret.sql
        ├── 03_external_access_integration.sql
        ├── 04_procedure.sql
        └── 05_test_calls.sql
```
