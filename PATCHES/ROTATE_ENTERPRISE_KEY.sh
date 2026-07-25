#!/usr/bin/env bash
# Rotate a leaked DC Hub API key.
# ==============================================================================
# RUN LOCALLY. Needs: DATABASE_URL (or NEON_DATABASE_URL) in env.
#
#   ./PATCHES/ROTATE_ENTERPRISE_KEY.sh mint   <leaked_key>
#   ./PATCHES/ROTATE_ENTERPRISE_KEY.sh revoke <leaked_key>
#
# The leaked key is passed as an ARGUMENT — never hardcoded, never read from an
# ambient env var. The previous version did `LEAKED_KEY="${DCHUB_API_KEY}"`, and
# DCHUB_API_KEY holds a DIFFERENT key that was never leaked: running it would
# have revoked the clean key and left the leaked one live. Pass the target.
#
# ------------------------------------------------------------------------------
# WHY THIS DOESN'T USE gen_dev_key.py  (verified live 2026-07-25)
# ------------------------------------------------------------------------------
# `api_keys` is the ONLY table that actually authenticates a request.
# util/tier_gate.resolve_tier step 1a queries
#     SELECT tier,email,user_id FROM mcp_dev_keys WHERE key_hash=%s
# but the live mcp_dev_keys table has neither `key_hash` nor `user_id`. That
# query always raises UndefinedColumn, a bare `except Exception: pass` swallows
# it, and every key falls through to step 1b:
#     api_keys WHERE key_hash IN (sha256(key), rawkey) AND is_active = 1
#
# Therefore:
#   * gen_dev_key.py mint   -> writes mcp_dev_keys only, with a `dch_live_`
#                              prefix. The resulting key NEVER authenticates.
#   * gen_dev_key.py revoke -> sets mcp_dev_keys.status='revoked' and does NOT
#                              touch api_keys.is_active. The key stays FULLY LIVE.
#
# So: mint into api_keys, revoke via api_keys.is_active. mcp_dev_keys is mirrored
# only for bookkeeping, and only when the old key had a row there.
# ==============================================================================
set -euo pipefail

CMD="${1:-}"
TARGET="${2:-}"
if [[ -z "$CMD" || -z "$TARGET" ]]; then
    sed -n '2,10p' "$0" >&2
    exit 2
fi
: "${DATABASE_URL:=${NEON_DATABASE_URL:-}}"
[[ -n "$DATABASE_URL" ]] || { echo "ERROR: set DATABASE_URL or NEON_DATABASE_URL" >&2; exit 2; }
export DATABASE_URL TARGET

API_BASE="${DCHUB_API_BASE:-https://api.dchub.cloud}"

# Prove a key is a live drop-in: an admin001 pro key sees 10 of 132 markets;
# anonymous sees 5. Anything else means the key is not resolving as expected.
probe() {
    local key="$1" label="$2"
    curl -s -H "X-API-Key: $key" -H "User-Agent: key-rotation/2.0" \
         "$API_BASE/api/v1/markets/list" --max-time 30 \
      | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"  [$label] tier={d.get('tier')} count={d.get('count')} total={d.get('total')}\")"
}

case "$CMD" in
mint)
    echo "=== pre-rotation probe (target key) ==="
    probe "$TARGET" "target"

    echo "=== minting replacement (mirrors the target's tier exactly) ==="
    python3 <<'PY'
import hashlib, json, os, secrets, sys
import psycopg2

old = os.environ["TARGET"]
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
try:
    cur.execute(
        "SELECT id,user_id,rate_limit_tier,plan,name FROM api_keys "
        "WHERE key_hash IN (%s,%s)",
        (hashlib.sha256(old.encode()).hexdigest(), old))
    row = cur.fetchone()
    if not row:
        sys.exit("target key has no api_keys row — nothing would authenticate; abort")

    new = "dchub_live_" + secrets.token_hex(24)
    h = hashlib.sha256(new.encode()).hexdigest()
    cur.execute("SELECT 1 FROM api_keys WHERE key_hash IN (%s,%s)", (h, new))
    if cur.fetchone():
        sys.exit("collision — rerun")

    cur.execute(
        """INSERT INTO api_keys (user_id,key_hash,key_prefix,name,permissions,
                                 rate_limit_tier,is_active,created_at,plan,
                                 usage_count,calls_today,calls_total)
           VALUES (%s,%s,%s,%s,'[]',%s,1,NOW(),%s,0,0,0) RETURNING id""",
        (row[1], h, new[:16], f"{row[4]} (rotated)", row[2], row[3]))
    new_id = cur.fetchone()[0]

    # Mirror into mcp_dev_keys only if the old key had a row there.
    cur.execute("SELECT tier,email,developer_id FROM mcp_dev_keys WHERE api_key=%s", (old,))
    m = cur.fetchone()
    if m:
        cur.execute(
            """INSERT INTO mcp_dev_keys (api_key,developer_id,email,tier,status,metadata)
               VALUES (%s,%s,%s,%s,'active','{}'::jsonb)""",
            (new, m[2], m[1], m[0]))
    conn.commit()
except Exception:
    conn.rollback()
    raise

print(json.dumps({"old_api_keys_id": row[0], "new_api_keys_id": new_id,
                  "mirrored_to_mcp_dev_keys": bool(m),
                  "new_prefix": new[:16] + "…"}, indent=2))
with open("/tmp/dchub_new_key.txt", "w") as fh:
    fh.write(new)
print("\nNew key written to /tmp/dchub_new_key.txt (shred it after use).")
PY

    NEW_KEY="$(cat /tmp/dchub_new_key.txt)"
    echo "=== post-mint probe (new key must match the target exactly) ==="
    probe "$NEW_KEY" "new"

    cat <<'EOF'

NEXT — update every consumer BEFORE revoking:
  Railway (both services share these; check which vars hold the target):
    for SVC in dchub-backend dchub-worker; do
      tr -d '\n' < /tmp/dchub_new_key.txt \
        | railway variable set --stdin <VAR_NAME> --service "$SVC"
    done
  Local MCP config: ~/.claude.json  (X-API-Key under mcpServers.dchub — back it up first)
  GitHub Actions secrets are write-only; update by name from the GitHub UI/CLI.

Confirm nothing still holds the old value:
    railway variables --service dchub-backend --json | grep -c "<leaked_key>"

Then, and only then:  ROTATE_ENTERPRISE_KEY.sh revoke <leaked_key>
EOF
    ;;

revoke)
    echo "=== revoking (api_keys.is_active=0 — the authenticating table) ==="
    python3 <<'PY'
import hashlib, json, os
import psycopg2

old = os.environ["TARGET"]
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
try:
    cur.execute("UPDATE api_keys SET is_active=0 WHERE key_hash IN (%s,%s)",
                (hashlib.sha256(old.encode()).hexdigest(), old))
    n_api = cur.rowcount
    cur.execute("UPDATE mcp_dev_keys SET status='revoked' WHERE api_key=%s", (old,))
    n_mcp = cur.rowcount
    conn.commit()
except Exception:
    conn.rollback()
    raise
print(json.dumps({"api_keys_rows_deactivated": n_api,
                  "mcp_dev_keys_rows_revoked": n_mcp}, indent=2))
if n_api == 0:
    print("WARNING: no api_keys row matched — the key was NOT revoked.")
PY

    echo "=== post-revoke probe (expect tier=anonymous count=5) ==="
    probe "$TARGET" "revoked"
    cat <<'EOF'

Watch for fallout (NB: api_endpoint_log.api_key_prefix is 24 chars, not 16 —
querying with the wrong width silently returns zero rows and looks like "unused"):
  SELECT status, count(*) FROM api_endpoint_log
   WHERE api_key_prefix = left('<leaked_key>', 24)
     AND called_at > now() - interval '15 minutes'
   GROUP BY 1;
EOF
    ;;

*)
    echo "unknown command: $CMD (expected 'mint' or 'revoke')" >&2
    exit 2
    ;;
esac
