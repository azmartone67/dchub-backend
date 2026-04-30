#!/usr/bin/env bash
# run_v21.sh — one-shot deploy of MCP v2.1 from a Replit shell.
# Idempotent: rerun anytime — it skips steps already done.
#
# Run:  cd ~/workspace && bash dchub-mcp-v2.1/run_v21.sh
#
# Required env (set in Replit Secrets):
#   NEON_DATABASE_URL    — full Neon connection string with ?sslmode=require
#   DCHUB_INTERNAL_KEY   — same value Railway uses for server.mjs
#
# Optional:
#   TEST_EMAIL           — email to associate with the test key (default: you@dchub.cloud)

set -u

cd "${HOME}/workspace"
HERE="$(pwd)"
BUNDLE="${HERE}/dchub-mcp-v2.1"

# ── ANSI helpers ───────────────────────────────────────────────────────────
red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yel()   { printf "\033[33m%s\033[0m\n" "$*"; }
blue()  { printf "\033[34m%s\033[0m\n" "$*"; }
hr()    { printf "\033[90m────────────────────────────────────────\033[0m\n"; }

step() { hr; blue "▸ $*"; }

# ── 0. Preflight ───────────────────────────────────────────────────────────
step "0/8  Preflight — env vars and bundle files"

MISSING=""
[ -z "${NEON_DATABASE_URL:-}" ]  && MISSING="${MISSING} NEON_DATABASE_URL"
[ -z "${DCHUB_INTERNAL_KEY:-}" ] && MISSING="${MISSING} DCHUB_INTERNAL_KEY"
if [ -n "$MISSING" ]; then
  red "  Missing env vars:$MISSING"
  red "  Set them in Replit Secrets (lock icon in sidebar), then reopen the shell."
  exit 1
fi
green "  env vars set"

for f in migration_001_api_keys.sql gen_dev_key.py flask_mcp_endpoints.py server.mjs test_smoke.sh cf_worker_mcpjson_patch.js; do
  if [ ! -f "${BUNDLE}/${f}" ]; then
    red "  Missing: ${BUNDLE}/${f}"
    red "  Drag the bundle files into ~/workspace/dchub-mcp-v2.1/ via the file panel."
    exit 1
  fi
done
green "  bundle files in place"

# ── 1. Validate Neon connection ────────────────────────────────────────────
step "1/8  Validating Neon connection"

# Allow .env to override / supplement the Replit secret (common in this project).
if [ -f .env ]; then
  ENV_URL=$(grep -E "^NEON_DATABASE_URL=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^["'\'']//;s/["'\'']$//')
  if [ -n "$ENV_URL" ] && [ "$ENV_URL" != "$NEON_DATABASE_URL" ]; then
    yel "  .env has a different NEON_DATABASE_URL than your env. Using .env value."
    export NEON_DATABASE_URL="$ENV_URL"
  fi
fi

# Try the actual connection first; only diagnose length / encoding if it fails.
if PSQL_OUT=$(psql "$NEON_DATABASE_URL" -c "SELECT 1" 2>&1); then
  green "  Neon reachable"
else
  red "  Neon connection FAILED:"
  printf "%s\n" "$PSQL_OUT" | sed 's/^/    /'
  hr
  yel "  Diagnostics:"
  python3 - <<'PYDIAG'
import os, urllib.parse as u
url = os.environ.get('NEON_DATABASE_URL','')
print(f"    url length:  {len(url)}")
try:
    p = u.urlparse(url)
    print(f"    scheme:      {p.scheme}")
    print(f"    user:        {p.username}")
    print(f"    host:        {p.hostname}")
    print(f"    port:        {p.port or 5432}")
    print(f"    db:          {(p.path or '/').lstrip('/').split('?')[0]}")
    pw = p.password or ''
    print(f"    pw length:   {len(pw)}")
    print(f"    pw starts:   {pw[:4]}…")
    bad = [c for c in pw if c in '@/:?#% ']
    print(f"    pw has special chars needing url-encoding: {bool(bad)}")
    print(f"    sslmode in querystring: {'sslmode' in (p.query or '')}")
except Exception as e:
    print(f"    parse error: {e}")
PYDIAG
  hr
  yel "  Common fixes:"
  echo "    • Did Replit pick up the secret update? Restart the workspace (not just the shell tab)."
  echo "    • Is .env shadowing the secret? grep -iE '^NEON|^DATABASE' .env"
  echo "    • Was the password rotated? In Neon: Roles → neondb_owner → Reset password."
  echo "    • If pw has special chars, percent-encode or use the PG* env vars."
  exit 2
fi

# ── 2. Run migration ───────────────────────────────────────────────────────
step "2/8  Running migration_001_api_keys.sql"

psql "$NEON_DATABASE_URL" -v ON_ERROR_STOP=1 -f "${BUNDLE}/migration_001_api_keys.sql"
green "  migration applied"
psql "$NEON_DATABASE_URL" -c "\dt api_keys mcp_call_log"

# ── 3. Install psycopg ─────────────────────────────────────────────────────
step "3/8  Installing psycopg[binary]>=3.2"

if python3 -c "import psycopg" 2>/dev/null; then
  green "  psycopg already installed"
else
  pip install --quiet --break-system-packages 'psycopg[binary]>=3.2' 2>/dev/null \
    || pip install --quiet 'psycopg[binary]>=3.2'
  green "  installed"
fi

# ── 4. Mint a paid test key (or reuse) ─────────────────────────────────────
step "4/8  Minting / reusing test API key"

TEST_EMAIL="${TEST_EMAIL:-you@dchub.cloud}"
KEY_FILE="${HERE}/.test_api_key"

if [ -f "$KEY_FILE" ]; then
  TEST_API_KEY=$(cat "$KEY_FILE")
  yel "  reusing key from ${KEY_FILE}: ${TEST_API_KEY:0:20}…"
else
  MINT_OUT=$(python3 "${BUNDLE}/gen_dev_key.py" mint --email "$TEST_EMAIL" --tier paid --note "v2.1 smoke test")
  echo "$MINT_OUT"
  TEST_API_KEY=$(echo "$MINT_OUT" | python3 -c "
import sys, json, re
m = re.search(r'\{.*\}', sys.stdin.read(), re.DOTALL)
print(json.loads(m.group())['api_key']) if m else print('')
")
  if [ -z "$TEST_API_KEY" ]; then
    red "  Failed to parse api_key from gen_dev_key output"
    exit 3
  fi
  echo "$TEST_API_KEY" > "$KEY_FILE"
  green "  minted: ${TEST_API_KEY:0:20}…"
fi
export TEST_API_KEY

# ── 5. Verify key against Neon directly ────────────────────────────────────
step "5/8  Verifying key in Neon"

VERIFY=$(psql "$NEON_DATABASE_URL" -At -c "SELECT tier FROM api_keys WHERE api_key='${TEST_API_KEY}' AND status='active';")
[ "$VERIFY" = "paid" ] && green "  key is active and tier=paid" || { red "  key not found / not active"; exit 4; }

# ── 6. Wire blueprint into main.py (Flask app) ─────────────────────────────
step "6/8  Wiring flask_mcp_endpoints into main.py"

# Make sure flask_mcp_endpoints.py is in the workspace root (Flask import path)
if [ ! -f flask_mcp_endpoints.py ] || ! cmp -s flask_mcp_endpoints.py "${BUNDLE}/flask_mcp_endpoints.py"; then
  cp "${BUNDLE}/flask_mcp_endpoints.py" ./flask_mcp_endpoints.py
  green "  copied flask_mcp_endpoints.py to workspace root"
else
  green "  flask_mcp_endpoints.py already current"
fi

# Add psycopg to requirements.txt if absent
if ! grep -q "^psycopg" requirements.txt 2>/dev/null; then
  echo "psycopg[binary,pool]>=3.2" >> requirements.txt
  green "  added psycopg to requirements.txt"
else
  green "  requirements.txt already has psycopg"
fi

# Wire blueprint into main.py
if grep -q "from flask_mcp_endpoints import mcp_bp" main.py 2>/dev/null; then
  green "  blueprint already registered in main.py"
else
  cp main.py "main.py.bak.v21.$(date +%s)"
  python3 << 'PYEOF'
import re, sys
with open('main.py', 'r') as f:
    src = f.read()

# Find an `app = Flask(...)` line and insert just after it.
m = re.search(r'^(app\s*=\s*Flask\([^)]*\)\s*)$', src, re.MULTILINE)
if not m:
    m = re.search(r'^(app\s*=\s*Flask\([^)]*\))', src, re.MULTILINE)
if not m:
    sys.stderr.write("WARN: couldn't find 'app = Flask(...)'. Add the two import lines manually:\n")
    sys.stderr.write("    from flask_mcp_endpoints import mcp_bp\n")
    sys.stderr.write("    app.register_blueprint(mcp_bp)\n")
    sys.exit(0)

# Find the end of that line
end = src.find('\n', m.end())
end = end if end >= 0 else len(src)

snippet = (
    "\n\n# ── MCP v2.1 telemetry + key validation ──────────────────────────\n"
    "try:\n"
    "    from flask_mcp_endpoints import mcp_bp\n"
    "    app.register_blueprint(mcp_bp)\n"
    "    print('[mcp v2.1] blueprint registered: /api/v1/keys/validate, /api/v1/mcp/track, /api/v1/mcp/stats')\n"
    "except Exception as _mcp_err:\n"
    "    print(f'[mcp v2.1] blueprint registration failed: {_mcp_err}')\n"
)
new_src = src[:end+1] + snippet + src[end+1:]
with open('main.py', 'w') as f:
    f.write(new_src)
print("OK — blueprint registration snippet added")
PYEOF
  green "  blueprint inserted (backup at main.py.bak.v21.*)"
fi

# ── 7. Apply Cloudflare Worker patch (mcp.json fix) ────────────────────────
step "7/8  Patching local worker.js with the real 20-tool mcp.json"

if [ ! -f worker.js ]; then
  yel "  no ~/workspace/worker.js found — skip this step and patch the Worker via Cloudflare dashboard."
else
  python3 "${BUNDLE}/apply_worker_patch.py" --worker worker.js \
    && green "  worker.js patched (backup saved)" \
    || red   "  worker.js patch failed — see message above"

  if command -v wrangler >/dev/null 2>&1; then
    yel "  to deploy the patched Worker, run:"
    echo "      wrangler deploy worker.js --name dchubapiproxy"
  else
    yel "  wrangler not on PATH. Either install (npm i -g wrangler) or apply via Cloudflare dashboard."
  fi
fi

# ── 8. Print next-step deploys + smoke test command ────────────────────────
step "8/8  Done. Next deploys (manual)"

cat <<EOF

  ${TEST_API_KEY:0:20}… is your test key (full value in ${KEY_FILE})

  Remaining manual steps:
  ────────────────────────

  A. Restart your Replit Flask app (so the new blueprint loads).
     Click the Stop button, then Run. Look for:
       [mcp v2.1] blueprint registered: /api/v1/keys/validate, /api/v1/mcp/track, /api/v1/mcp/stats

  B. Push the patched server.mjs to Railway.
     The new file is at ${BUNDLE}/server.mjs.
     Wherever your dchub-mcp-server repo lives (Replit clone or Railway-linked):
        cp ${BUNDLE}/server.mjs <repo>/server.mjs
        cd <repo>
        git add server.mjs && git commit -m "MCP v2.1: telemetry + key validation + tier gates"
        git push origin main

  C. Deploy the patched Cloudflare Worker (if step 7 patched it):
        wrangler deploy worker.js --name dchubapiproxy

  D. Smoke test end-to-end:
        export TEST_API_KEY=\$(cat ${KEY_FILE})
        MCP_URL=https://dchub.cloud/mcp \\
          API_KEY="\$TEST_API_KEY" \\
          DCHUB_INTERNAL_KEY="\$DCHUB_INTERNAL_KEY" \\
          bash ${BUNDLE}/test_smoke.sh

EOF
green "Local Replit work complete."
