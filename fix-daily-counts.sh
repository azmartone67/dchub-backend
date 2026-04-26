#!/usr/bin/env bash
# fix-daily-counts.sh
# ----------------------------------------------------------------------
# After PR #7 merged, /daily switched from "bundled seed" to "DB snapshot".
# The DB snapshot has /stats-scaled UC and ANN counts (158 + 143 nationally),
# which crushes per-state UC + ANN ~95% vs the Aterio bundled seed.
#
# This script:
#   1. Verifies REFRESH_SECRET is set (or asks for it)
#   2. Hits POST /refresh on the daily service to repopulate today's DB row
#   3. Verifies /snapshot returns the right shape
#
# IMPORTANT: For the script to produce HIGH UC + ANN counts, you must FIRST
# set DRY_RUN=1 on the daily Railway service (one-click in Railway UI), and
# wait for it to redeploy (~1-2 min). DRY_RUN=1 forces fetch_snapshot() to
# return the bundled Aterio seed instead of /stats-scaled values.
#
# How to set DRY_RUN=1:
#   1. Open https://railway.com/project/8b33570c-80fa-4869-8de6-dd62899a0eb2
#   2. Click on the daily service (the one hosting f7dd subdomain)
#   3. Variables tab → + New Variable
#   4. Name: DRY_RUN  Value: 1
#   5. Save → service auto-redeploys
#   6. Wait for redeploy to finish, then run THIS script
# ----------------------------------------------------------------------

set -euo pipefail

# Most likely the daily service URL — confirm by hitting /health
SVC="${SVC:-https://dchub-backend-production-f7dd.up.railway.app}"

: "${REFRESH_SECRET:?ERROR: export REFRESH_SECRET=... first (Railway daily service > Variables)}"

echo "→ Confirming service is up..."
curl -s "$SVC/health" | python3 -m json.tool

echo
echo "→ Checking current /snapshot (before refresh)..."
BEFORE_AS_OF=$(curl -s "$SVC/snapshot" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("as_of","?"))')
BEFORE_TX_OP=$(curl -s "$SVC/snapshot" | python3 -c 'import sys,json; d=json.load(sys.stdin); tx=[s for s in d["states"] if s["name"]=="TEXAS"][0]; print(f"op={tx[\"op\"]} uc={tx[\"uc\"]} ann={tx[\"ann\"]}")')
BEFORE_SOURCE=$(curl -s "$SVC/snapshot" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("source","?")[:80])')
echo "  as_of: $BEFORE_AS_OF"
echo "  TX:    $BEFORE_TX_OP"
echo "  source: $BEFORE_SOURCE"

echo
echo "→ Triggering POST /refresh (auth via REFRESH_SECRET)..."
RESP=$(curl -sS -X POST -H "Authorization: Bearer $REFRESH_SECRET" "$SVC/refresh")
echo "$RESP" | python3 -m json.tool 2>/dev/null | head -30 || echo "$RESP"

echo
echo "→ Checking /snapshot AFTER refresh..."
sleep 3
AFTER_AS_OF=$(curl -s "$SVC/snapshot" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("as_of","?"))')
AFTER_TX=$(curl -s "$SVC/snapshot" | python3 -c 'import sys,json; d=json.load(sys.stdin); tx=[s for s in d["states"] if s["name"]=="TEXAS"][0]; print(f"op={tx[\"op\"]} uc={tx[\"uc\"]} ann={tx[\"ann\"]}")')
AFTER_SOURCE=$(curl -s "$SVC/snapshot" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("source","?")[:80])')
echo "  as_of: $AFTER_AS_OF"
echo "  TX:    $AFTER_TX"
echo "  source: $AFTER_SOURCE"

echo
echo "Reading the result:"
echo "  - source contains 'seed (no API key)'  → DRY_RUN=1 worked, bundled seed used (HIGH counts)"
echo "  - source contains 'DC Hub /stats'      → DRY_RUN not set; /stats scaling still active (LOW counts)"
echo "  - TX uc + ann low (e.g. 31 + 28)       → /stats scaling, set DRY_RUN=1 in Railway and re-run"
echo "  - TX uc + ann high (e.g. 140 + 610)    → bundled seed; refresh dchub.cloud/daily to verify"
