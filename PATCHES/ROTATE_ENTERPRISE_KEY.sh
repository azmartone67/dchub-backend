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
# WHY THIS KEEPS ITS OWN SQL  (re-verified against origin/main 2026-09-01)
# ------------------------------------------------------------------------------
# ★★★ The block that used to sit here said gen_dev_key.py was BROKEN — that its
# `revoke` left the key "FULLY LIVE" and its `mint` produced a key that "NEVER
# authenticates". Both were true on 2026-07-25. Both are false now, and the
# premise under them is false too. Do not reinstate them.
#
# WHAT CHANGED, in the order it changed:
#
#  1. #2766 (5a6b8b1c7, 2026-08-16) — `gen_dev_key.py revoke` had only run
#     `UPDATE mcp_dev_keys SET status='revoked'`. It now ALSO disables the
#     api_keys row, matching both storage conventions (sha256(key) for customer
#     keys, the RAW string for partner/admin keys), and reports each row count
#     separately. It does not leave the key live.
#
#  2. #3288 (a9d98800c, 2026-08-28) — THE PREMISE ITSELF. The old block argued
#     mcp_dev_keys could be ignored because resolve_tier step 1a filtered on a
#     `key_hash` column that table has never had, so it always raised
#     UndefinedColumn into a bare except. That query is FIXED. Step 1a is now
#         SELECT tier,email,developer_id FROM mcp_dev_keys
#          WHERE api_key = %s AND COALESCE(status,'active') = 'active'
#     and it RETURNS BEFORE step 1b ever looks at api_keys.
#
# So there is no longer one authenticating table. There are two, and which one
# applies depends on where the key was MINTED:
#
#   * api_keys      dashboard / partner / paid keys. resolve_tier step 1b,
#                   key_hash IN (sha256(key), rawkey) AND is_active = 1.
#                   is_active is an INTEGER column — write 0, never FALSE.
#   * mcp_dev_keys  MCP-minted keys (claim_free_key, OAuth, pair-code). TWO
#                   live gates: resolve_tier step 1a above (REST), and
#                   flask_mcp_endpoints POST /api/v1/keys/validate — the hop the
#                   Node MCP server relays on every call — which reads
#                   `WHERE api_key = %s` and requires status='active'.
#   (dch_trial_ keys are a THIRD id space, auto_trial_keys. Not handled here.)
#
# ★ CONSEQUENCE FOR THIS SCRIPT: the `UPDATE mcp_dev_keys` in revoke below is
#   NOT bookkeeping any more — it is load-bearing. Dropping it would leave a
#   mirrored key authenticating through step 1a with api_keys already disabled.
#
# WHY WE STILL DON'T DELEGATE
#   * mint CANNOT delegate, and that is structural, not a defect. `gen_dev_key.py
#     mint` creates a NEW identity (fresh developer_id, `dch_live_` prefix) in
#     mcp_dev_keys only, and deliberately refuses to write the api_keys row
#     because that grants privilege and needs an explicit user_id + tier
#     decision. This script does the opposite job: it CLONES the leaked key's
#     existing api_keys row — same user_id, rate_limit_tier, plan, name — so the
#     replacement is a like-for-like swap. Delegating would hand the operator a
#     key with a different identity and no api_keys row at all.
#   * revoke COULD delegate now, and deliberately does not. gen_dev_key.py needs
#     psycopg3 (`psycopg`) while this script runs on psycopg2; the rotation also
#     needs the cache-busted /api/v1/me probe and the api_endpoint_log fallout
#     query below, neither of which that tool does; and its failure semantics
#     are being reworked right now (see below). One tool and one connection
#     story, for a runbook you run while a credential is leaking.
#
# ★ STATE OF gen_dev_key.py AS OF 2026-09-01 — re-check before relying on it:
#   - The 08-16 fix over-corrected: it made "api_keys matched 0 rows" the failure
#     condition, which is backwards for an MCP-minted key that has no api_keys
#     row and was fully revoked in mcp_dev_keys. **FIXED and LANDED** — #3515
#     (8036b0462). Its revoke now re-reads BOTH tables after the write and takes
#     the verdict from that, using the same two live-predicates this script uses
#     (`is_active IS NULL OR is_active = 1`, `COALESCE(status,'active')`). Its
#     field names differ from this script's — it reports `rows_found` and
#     `still_accepted_anywhere` — so read the tool's own output, do not assume
#     these keys. The two agree on semantics, which is the point.
#   - Its mint banner used to claim "AUTHENTICATES ON /mcp BUT NOT ON REST",
#     reasoning that step 1a "always raises" on a key_hash column mcp_dev_keys
#     does not have. #3288 killed that premise; the banner outlived it. FIXED —
#     it now reads the tier straight out of util.tier_gate._PLAN_TO_TIER and
#     prints what the key ACTUALLY resolves to per path. Worth knowing when you
#     mint one, because it is not uniform:
#         --tier free        /mcp AUTHENTICATES,  REST ANONYMOUS  (unprivileged)
#         --tier paid        /mcp AUTHENTICATES,  REST PRO        ★ live credential
#         --tier enterprise  /mcp AUTHENTICATES,  REST ENTERPRISE ★ live credential
#     So "an mcp_dev_keys-only key is harmless on REST" is true ONLY for free.
#     Above free it is a live REST credential with no api_keys row anywhere —
#     which is exactly the kind of key this script's revoke must not miss.
#   - revoke below WAS origin-blind in the same way and was fixed on 2026-09-01:
#     it used to warn "the key was NOT revoked" whenever n_api == 0, which is
#     exactly backwards for an MCP-minted key that has no api_keys row and was
#     fully revoked in mcp_dev_keys. It now censuses BOTH tables before and
#     after, and the verdict is:
#         no row in either        -> exit 1, "NOT REVOKED" (+ names auto_trial_
#                                    keys if the key is a dch_trial_ one)
#         still live after UPDATE -> exit 1, "REVOKE DID NOT TAKE"
#         a home, nothing live    -> exit 0, naming the home and its gate
#     ★ Read `still_live_in`, not the rowcounts. A rowcount is what the UPDATE
#       claims it touched; the post-commit re-read is what is actually true —
#       and it is the only thing that catches `is_active = FALSE` on an INTEGER
#       column, which reports a rowcount and changes nothing.
#     ★ The /api/v1/me probe is NOT a universal check. It joins api_keys only,
#       so for an mcp_dev_keys-homed key it was already returning DENIED before
#       the revoke. revoke says which probe applies; believe it over the banner.
#   - mint still aborts when the target has no api_keys row, and that stays.
#     It is not the same cry-wolf: mint CLONES an api_keys row, so without one
#     there is genuinely nothing to mirror. (Its "nothing would authenticate"
#     wording is now imprecise — an mcp_dev_keys row does authenticate — but
#     the abort itself is correct. Rotate that class with gen_dev_key.py.)
#   - dchub-mcp-v2.1/gen_dev_key.py WAS a separate, pre-#2766 copy whose revoke
#     only touched mcp_dev_keys — so revoking through the bundle path reported
#     success and left the credential live. #3518 LANDED 2026-09-01 and made it
#     a symlink (git mode 120000) to the root file, guarded by
#     tests/test_revoke_tool_has_one_implementation.py. There is now ONE
#     implementation; either path runs it. If that guard ever goes red, assume
#     a second copy is back and revoke from the ROOT file until it is fixed.
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

# Prove a key is valid. Two traps make the obvious probes lie:
#
#  1. Cloudflare caches these GETs and the cache key IGNORES X-API-Key, so a
#     plain curl happily returns another key's cached response — during the
#     2026-07-25 rotation this made a REVOKED key look alive and made a brand
#     new key echo the old key's key_issued_at. Always send a unique ?cb= and
#     a unique User-Agent, and confirm cf-cache-status is BYPASS/MISS.
#  2. /api/v1/markets/list soft-gates on key PRESENCE, not validity — a bogus
#     key gets tier=free (10 of 132 markets) exactly like a real one. It can
#     never prove a key works. /api/v1/me is the authoritative check: it
#     joins api_keys with is_active=1 and 401s "invalid_or_revoked_key".
probe() {
    local key="$1" label="$2"
    local cb="$$-$(od -An -N4 -tu4 </dev/urandom | tr -d ' ')"
    curl -s -H "X-API-Key: $key" -H "Cache-Control: no-cache" \
         -H "User-Agent: key-rotation/2.0-$cb" \
         "$API_BASE/api/v1/me?cb=$cb" --max-time 30 \
      | python3 -c "
import json,sys
raw=sys.stdin.read()
try: d=json.loads(raw)
except Exception: print('  [$label] unparseable:', raw[:120]); raise SystemExit
if d.get('success'):
    u=d.get('user',{})
    print(f\"  [$label] VALID  id={u.get('id')} plan={u.get('plan')} issued={str(u.get('key_issued_at'))[:19]}\")
else:
    print(f\"  [$label] DENIED error={d.get('error')}\")"
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
    echo "=== revoking (ORIGIN-AWARE: api_keys AND mcp_dev_keys are both gates) ==="
    rc=0
    python3 <<'PY' || rc=$?
import hashlib, json, os, sys
import psycopg2

old = os.environ["TARGET"]
kh  = hashlib.sha256(old.encode()).hexdigest()

# The two "is it live?" predicates below mirror the GATES exactly. Narrowing
# either one would report a still-authenticating key as dead:
#   * resolve_tier 1b accepts `is_active IS NULL` as live, not just = 1.
#   * resolve_tier 1a accepts a NULL status as active (COALESCE). /api/v1/keys/
#     validate is stricter (status == 'active'), so COALESCE is the fail-safe
#     reading — if the two ever disagree, we call the key live.
ANY_API  = "SELECT COUNT(*) FROM api_keys WHERE key_hash IN (%s,%s)"
LIVE_API = ("SELECT COUNT(*) FROM api_keys WHERE key_hash IN (%s,%s) "
            "AND (is_active IS NULL OR is_active = 1)")
ANY_MCP  = "SELECT COUNT(*) FROM mcp_dev_keys WHERE api_key = %s"
LIVE_MCP = ("SELECT COUNT(*) FROM mcp_dev_keys WHERE api_key = %s "
            "AND COALESCE(status,'active') = 'active'")

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur  = conn.cursor()

def census():
    cur.execute(ANY_API,  (kh, old)); a_any  = cur.fetchone()[0]
    cur.execute(LIVE_API, (kh, old)); a_live = cur.fetchone()[0]
    cur.execute(ANY_MCP,  (old,));    m_any  = cur.fetchone()[0]
    cur.execute(LIVE_MCP, (old,));    m_live = cur.fetchone()[0]
    return {"api_keys": (a_any, a_live), "mcp_dev_keys": (m_any, m_live)}

try:
    before = census()
    cur.execute("UPDATE api_keys SET is_active=0 WHERE key_hash IN (%s,%s)",
                (kh, old))
    n_api = cur.rowcount
    cur.execute("UPDATE mcp_dev_keys SET status='revoked' WHERE api_key=%s", (old,))
    n_mcp = cur.rowcount
    conn.commit()
except Exception:
    conn.rollback()
    raise

# POST-STATE, read AFTER the commit. A rowcount is a claim about what the UPDATE
# said it touched; this is a measurement of what is actually persisted. It is
# also what catches `is_active = FALSE` on an INTEGER column — that reports a
# rowcount and changes nothing.
after = census()

found_in      = [t for t, (present, _) in before.items() if present]
still_live_in = [t for t, (_, live)    in after.items()  if live]
was_live_in   = [t for t, (_, live)    in before.items() if live]
revoked       = bool(found_in) and not still_live_in

print(json.dumps({
    "api_keys_rows_deactivated": n_api,
    "mcp_dev_keys_rows_revoked": n_mcp,
    "found_in":        found_in,       # where this key LIVES — its origin
    "still_live_in":   still_live_in,  # ← read THIS, not the rowcounts
    "already_revoked": bool(found_in) and not was_live_in,
    "revoked":         revoked,
}, indent=2))

GATE = {
    "api_keys":     "util/tier_gate.resolve_tier step 1b (REST)",
    "mcp_dev_keys": ("resolve_tier step 1a (REST, since #3288) + "
                     "POST /api/v1/keys/validate (the /mcp hop)"),
}

if not found_in:
    # ★ NOT the same as "the revoke failed to take" — this key has no row in
    #   either table we manage. Do not call it revoked; do not call it live.
    trial = None
    try:
        cur.execute("SELECT COUNT(*) FROM auto_trial_keys WHERE api_key=%s", (old,))
        trial = cur.fetchone()[0]
    except Exception:
        conn.rollback()          # a failed stmt poisons the tx until rollback
    sys.stderr.write(
        "\nNOT REVOKED — no row in api_keys OR mcp_dev_keys.\n"
        "Nothing was disabled because there was nothing here to disable.\n"
        + ("★ This key IS present in auto_trial_keys (dch_trial_ class), which\n"
           "  this script does not manage. Revoke it there.\n" if trial else
           "Check the key value and which database DATABASE_URL points at.\n"
           "dch_trial_ keys live in a THIRD table, auto_trial_keys.\n"))
    sys.exit(1)

if still_live_in:
    sys.stderr.write(
        "\n★★★ REVOKE DID NOT TAKE — the key is STILL LIVE in: "
        + ", ".join(still_live_in) + "\n"
        + "".join("    %s → %s\n" % (t, GATE[t]) for t in still_live_in)
        + "The UPDATE reported rows but the post-read still sees an active row.\n"
          "Do NOT retire the old credential. Treat it as leaking.\n")
    sys.exit(1)

sys.stderr.write(
    "\nREVOKED. This key's home was: " + ", ".join(found_in) + "\n"
    + "".join("    %s → %s\n" % (t, GATE[t]) for t in found_in))
if was_live_in == []:
    sys.stderr.write(
        "★ It was ALREADY revoked before this run — nothing was live to disable.\n"
        "  Success (the credential is not live), but confirm you targeted the\n"
        "  key you meant to.\n")
# ★ Tell the operator how to read the probe that runs next. /api/v1/me joins
#   api_keys ONLY, so for a key whose home is mcp_dev_keys it was ALREADY
#   returning DENIED before this revoke — there it proves nothing.
if "api_keys" in found_in:
    sys.stderr.write(
        "\nThe /api/v1/me probe below IS authoritative for this key.\n")
else:
    sys.stderr.write(
        "\n★ The /api/v1/me probe below is NOT authoritative for this key: it\n"
        "  joins api_keys, which this key never had a row in, so it was DENIED\n"
        "  before the revoke too. Prove it on the path that gated it — a\n"
        "  tools/call on https://dchub.cloud/mcp with (a) the key, (b) a\n"
        "  same-shape bogus key, (c) no key: dead ⇒ all three identical.\n")
PY

    # Corroboration only — the verdict above came from the DB census, and it is
    # authoritative. `|| true` so a network blip on the probe cannot swallow the
    # revoke's exit code (probe pipes curl into python3, and pipefail would
    # otherwise abort the script here under `set -e`).
    echo "=== post-revoke probe (see the note above for how to read this) ==="
    probe "$TARGET" "revoked" || true
    cat <<'EOF'

Watch for fallout (NB: api_endpoint_log.api_key_prefix is 24 chars, not 16 —
querying with the wrong width silently returns zero rows and looks like "unused"):
  SELECT status, count(*) FROM api_endpoint_log
   WHERE api_key_prefix = left('<leaked_key>', 24)
     AND called_at > now() - interval '15 minutes'
   GROUP BY 1;
EOF
    exit "$rc"
    ;;

*)
    echo "unknown command: $CMD (expected 'mint' or 'revoke')" >&2
    exit 2
    ;;
esac
