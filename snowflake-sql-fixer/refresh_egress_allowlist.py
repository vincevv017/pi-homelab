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