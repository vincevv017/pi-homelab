# <img src="https://cdn.simpleicons.org/snowflake/29B5E8" height="22" align="center" /> Snowflake SQL Fixer — Local LLM Fallback for Cortex AI

See [guide_snowflake_sql_fixer.md](./guide_snowflake_sql_fixer.md) for the complete setup guide.

**What this does:**
- Calls a Snowflake stored procedure with a broken SQL statement and its error message
- Snowflake reaches out over an External Access Integration to a tightly-scoped endpoint on Pi 2
- A local model (`qwen2.5-coder:7b` via Ollama) repairs the SQL and returns it straight into the worksheet
- Acts as a fallback SQL doctor when Cortex AI credits are exhausted — running entirely on hardware you own

**Services:** Snowflake External Access Integration · Stored Procedure (Python) · FastAPI · nginx (PROXY protocol) · Tailscale Funnel · Ollama

This is the **synchronous** path: Snowflake calls out to the Pi directly rather than the Pi polling Snowflake. That choice deliberately opens a narrow, authenticated, IP-filtered public surface — the tradeoff that makes the design interesting, and the reason the [security model](#security-model) below is worth reading before you build it.

---

## Architecture

```
┌──────────────────────── Snowflake (enterprise account) ──────────────────────┐
│                                                                              │
│  Worksheet:  CALL FIX_SQL_LOCAL('SELECT * FORM t', 'syntax error ... FORM'); │
│       │                                                                      │
│       ▼                                                                      │
│  Stored procedure FIX_SQL_LOCAL (Python)                                     │
│    ├─ reads bearer token from SECRET                                         │
│    ├─ EXTERNAL_ACCESS_INTEGRATION → allows egress to one host:port           │
│    └─ requests.post(https://<PI2_FUNNEL_FQDN>/fix-sql, {sql, error})         │
│                                  │                                           │
│  egress leaves via Snowflake's stable egress IP ranges  ─────────────┐       │
└──────────────────────────────────────────────────────────────────────┼───────┘
                                                                       │ public
                                                                       ▼ internet
┌──────────────────── Tailscale edge (public relay) ───────────────────────────┐
│  Funnel :8443  --proxy-protocol=2 --tls-terminated-tcp → localhost:9443      │
│    ├─ terminates TLS (node's Let's Encrypt cert)                             │
│    └─ prepends PROXY protocol v2 header (carries Snowflake's source IP)      │
│    (public :443 is left to the existing Open WebUI Docker nginx)             │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │ loopback only
                                       ▼
┌──────────────────────────── Pi 2 (vpi5-llm) ─────────────────────────────────┐
│  host nginx  127.0.0.1:9443  (proxy_protocol)                                │
│    ├─ real_ip_header proxy_protocol   → $remote_addr = Snowflake egress IP   │
│    ├─ include snowflake-egress-allow.conf ; deny all   (IP allowlist)        │
│    ├─ location = /fix-sql  → proxy_pass 127.0.0.1:8000                       │
│    └─ location /           → return 444   (everything else dropped)          │
│                                        │                                     │
│                                        ▼                                     │
│  FastAPI  127.0.0.1:8000  (localhost-only, hardened systemd unit)            │
│    ├─ Bearer token check (constant-time)                                     │
│    ├─ body validation + size caps                                            │
│    └─ POST 127.0.0.1:11434  Ollama  qwen2.5-coder:7b  (format=json)          │
│                                                                              │
│  refresh_egress_allowlist.py  (weekly timer)                                 │
│    └─ Pi 2 → Snowflake (outbound) → SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES    │
│         → rewrite snowflake-egress-allow.conf → nginx -s reload              │
└──────────────────────────────────────────────────────────────────────────────┘
```

Nothing here touches Mullvad, the notifier, Nextcloud, Open WebUI, or Pi 1. The Funnel surface lives only on Pi 2 and answers on exactly one path.

---

## Security model

Tailscale Funnel terminates at Tailscale's public relay and gives the node a real, internet-reachable `*.ts.net` name. That's not "no public exposure" — it's a **token-gated, IP-filtered, single-path** public endpoint, and the design is entirely about keeping that opening as small and as conditional as possible.

**Defense in depth, outermost to innermost:**

| Layer | Control | A leaked token alone is not enough because… |
|---|---|---|
| 1 | Funnel exposes one TCP port → nginx only. Ollama (`:11434`) and the FastAPI app (`:8000`) bind loopback and are never funneled. | The model runtime is unreachable from outside. |
| 2 | nginx `return 444` on every path except `= /fix-sql`. | Probing any other URL gets a silently-dropped connection. |
| 3 | Source IP (from PROXY protocol) must match Snowflake's published egress ranges; `deny all` otherwise. | The caller must originate from Snowflake's own infrastructure. |
| 4 | Bearer token (Snowflake `SECRET`), constant-time compared in the app. | The IP allowlist alone doesn't authorise; you still need the secret. |
| 5 | Body schema + size caps (≤ 64 KB), rate limit (10 req/min). | Abuse and oversized payloads are bounded. |

Layers 3 and 4 are independent — an attacker needs *both* a valid token *and* an IP inside Snowflake's egress ranges.

**Residual risk:** the TCP connection still reaches Tailscale's relay and the Pi before nginx evaluates the allowlist (the filtering is app-layer, not edge-layer). Funnel itself can't be IP-filtered at the Tailscale edge — this trusts Tailscale's relay and the local nginx config.

The egress-IP allowlist (layer 3) currently requires an **AWS Commercial** Snowflake deployment (`SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()` is AWS-only at time of writing). On Azure/GCP accounts, drop layer 3 and rely on the bearer token, rate limiting, and the single-path nginx rule — see the guide's [token-only fallback](./guide_snowflake_sql_fixer.md#fallback-token-only-non-aws-accounts).

See the [full guide](./guide_snowflake_sql_fixer.md) for setup, the validation gallery (four real repair cases), maintenance procedures, and troubleshooting.