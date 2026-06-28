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