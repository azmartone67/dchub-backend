#!/usr/bin/env bash
# Beat the gate liveness ledger. Called by a gate as its own FINAL step.
#
#   scripts/gate_beat.sh <gate> <verdict> [checked] [selftest] [note]
#
#   gate      workflow:job          e.g. api-response-contract:contract
#   verdict   pass|fail|unmeasured|no_scope
#             ★ `fail` means the gate REFUSED something. It did its job and the
#               ledger is HEALTHY. It is not an alarm — see routes/gate_runs.py.
#   checked   how many items this run examined. 0 with verdict=pass is a
#             VACUOUS pass and alarms; use verdict=no_scope when zero is
#             legitimately expected (a delta gate on a PR touching nothing).
#   selftest  pass|fail|absent — did this gate's must-fail control run and pass?
#
# ★ NEVER fails the calling job. A beat is telemetry; the gate's own work
#   matters more than its heartbeat. The failure direction is safe by
#   construction: a dropped beat leaves the gate reading `never-run`, so the
#   board over-reports risk rather than under-reporting it. There is no path
#   here that can make a dead gate look healthy.
#
# ★ SINGLE WRITER. Only the gate itself may call this. Nothing beats on another
#   gate's behalf — tools/deadman/watch.py learned that three times, each time
#   by masking an honest failure with a bare success.
set -uo pipefail

GATE="${1:?gate required (workflow:job)}"
VERDICT="${2:-pass}"
CHECKED="${3:-}"
SELFTEST="${4:-absent}"
NOTE="${5:-}"
API="${DCHUB_API_BASE:-https://dchub.cloud}"

if [ -z "${DCHUB_ADMIN_KEY:-}" ]; then
  echo "::warning::gate_beat: DCHUB_ADMIN_KEY absent — beat SKIPPED for ${GATE}."
  echo "::warning::${GATE} will read 'never-run' on /api/v1/ops/gates until a beat lands."
  exit 0
fi

BODY=$(GATE="$GATE" VERDICT="$VERDICT" CHECKED="$CHECKED" SELFTEST="$SELFTEST" NOTE="$NOTE" \
  python3 -c '
import json, os
b = {"gate": os.environ["GATE"], "verdict": os.environ["VERDICT"],
     "selftest": os.environ["SELFTEST"], "repo": "dchub-backend"}
c = os.environ.get("CHECKED", "")
if c.strip():
    try: b["checked"] = int(c)
    except ValueError: pass
n = os.environ.get("NOTE", "")
if n.strip(): b["note"] = n[:280]
print(json.dumps(b))')

CODE=$(curl -sS -o /tmp/gate_beat_resp.json -w '%{http_code}' --max-time 20 \
  -X POST "${API}/api/v1/admin/gates/beat" \
  -H 'Content-Type: application/json' \
  -H "X-Admin-Key: ${DCHUB_ADMIN_KEY}" \
  -A 'dchub-gate-beat/1.0 (+https://dchub.cloud/api/v1/ops/gates)' \
  --data "$BODY" 2>/dev/null) || CODE=000

if [ "$CODE" = "200" ]; then
  echo "gate_beat: ${GATE} verdict=${VERDICT} checked=${CHECKED:-n/a} selftest=${SELFTEST} -> 200"
else
  # ERROR level so it is greppable, but exit 0 — see the header.
  echo "::warning::gate_beat DROPPED ${GATE} (HTTP ${CODE}) — it will read stale/never-run on the board."
  head -c 300 /tmp/gate_beat_resp.json 2>/dev/null || true
fi
exit 0
