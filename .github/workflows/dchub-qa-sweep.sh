#!/usr/bin/env bash
# dchub-qa-sweep.sh — Single-command QA sweep across all 4 priorities.
# Outputs a prioritized report + diagnostics to stdout and /tmp/dchub-qa-<timestamp>/
#
# Usage:
#   CANARY_SECRET="..." ./dchub-qa-sweep.sh
#
# Covers:
#   P1 — Tool descriptions & 0 tool_call_attempts diagnosis
#   P2 — dchubapiproxy build pipeline + KV binding health
#   P3 — Latency delta (railway-a vs replit) + write-path failover gap
#   P4 — Rotation secret + workflow action versions

set -u
DOMAIN="https://dchub.cloud"
OUT="/tmp/dchub-qa-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

line() { printf '%.0s─' {1..72}; echo; }
hdr()  { echo; line; echo "  $*"; line; }

hdr "DC Hub QA sweep — $(date -u '+%Y-%m-%d %H:%MZ')"
echo "Output dir: $OUT"
echo "Domain:     $DOMAIN"
echo "Canary:     ${CANARY_SECRET:+CONFIGURED}"

# ─────────────────────────────────────────────────────────────────────
# P1: Tool descriptions & 0 tool_call_attempts
# ─────────────────────────────────────────────────────────────────────
hdr "P1  Tool descriptions & handshake-to-call conversion"

# 1a. Pull live tools/list from MCP server (authoritative list)
curl -sS -X POST "$DOMAIN/mcp" \
  -H 'content-type: application/json' \
  -H 'accept: application/json' \
  --max-time 15 \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  > "$OUT/tools-list-live.json" 2>&1

LIVE_COUNT=$(python3 -c "import json,sys; d=json.load(open('$OUT/tools-list-live.json')); print(len(d.get('result',{}).get('tools',[])))" 2>/dev/null || echo "ERR")
echo "Live tools/list count:     $LIVE_COUNT"
echo "  (full dump: $OUT/tools-list-live.json)"

# 1b. Score each description against rubric
python3 <<PYEOF
import json, sys
try:
    d = json.load(open("$OUT/tools-list-live.json"))
    tools = d.get("result", {}).get("tools", [])
except Exception as e:
    print(f"  [skip] could not parse tools list: {e}")
    sys.exit(0)

def score(desc):
    issues = []
    if not desc: issues.append("EMPTY")
    else:
        if len(desc) < 40:   issues.append("too-short(<40ch)")
        if len(desc) > 600:  issues.append("too-long(>600ch)")
        low = desc.lower()
        if "use when" not in low and "use this" not in low and "call this" not in low:
            issues.append("no-when-to-use")
        if not any(x in low for x in ["example", "e.g.", "for instance", "such as"]):
            issues.append("no-example")
        if any(v in low for v in ["get info", "returns data", "query the"]):
            issues.append("vague-verb")
    return issues

print(f"{'Tool':<32} {'Len':>4}  Issues")
print("─" * 72)
priority_rewrites = []
for t in tools:
    name = t.get("name","?")
    desc = t.get("description","") or ""
    issues = score(desc)
    issue_str = ",".join(issues) if issues else "ok"
    print(f"{name:<32} {len(desc):>4}  {issue_str}")
    if len(issues) >= 2:
        priority_rewrites.append((name, len(issues), desc))

print()
print(f"Priority rewrites (2+ issues): {len(priority_rewrites)}")
for n, i, d in sorted(priority_rewrites, key=lambda x: -x[1])[:10]:
    print(f"  • {n} ({i} issues)")
PYEOF

echo
echo "Next step for P1:"
echo "  • Review /tmp/…/tools-list-live.json for full current descriptions"
echo "  • Apply proposed rewrites from tool-descriptions-v2.json (generated alongside this script)"
echo "  • Redeploy backend, then rerun eval_runner.py to quantify lift"

# ─────────────────────────────────────────────────────────────────────
# P2: Build pipeline + KV binding
# ─────────────────────────────────────────────────────────────────────
hdr "P2  Infrastructure hygiene"

# 2a. Version + backends + cache state from dchubapiproxy
echo "dchubapiproxy /api/version:"
curl -sS "$DOMAIN/api/version" | python3 -m json.tool 2>/dev/null | head -20 | sed 's/^/  /'

# 2b. Probe a KV-cached endpoint to confirm KV is wired
echo
echo "KV cache signals on /api/ai/mcp-health:"
curl -sSI "$DOMAIN/api/ai/mcp-health" 2>/dev/null \
  | grep -iE 'x-cache|x-backend-used|x-dc-worker-version|x-dc-hub' \
  | sed 's/^/  /'

# 2c. Manual steps the script can't do
cat <<'EOF'

Manual follow-ups (need Cloudflare dashboard or API token):
  • Build pipeline: https://dash.cloudflare.com → dchubapiproxy → Settings → Build
    Click "View build" on the failed build → read log.
    Fix: disconnect git build (dashboard → Build → Disconnect) OR push valid
    code to dchub-frontend. Leaving broken build connected is a landmine.
  • KV binding: https://dash.cloudflare.com → dchubapiproxy → Settings → Bindings
    Confirm DCHUB_API_KEYS and DCHUB_CACHE are both present and point to
    the right KV namespace. If DCHUB_API_KEYS is missing, tier enforcement
    silently allows everything — P0 for a paid product.
EOF

# ─────────────────────────────────────────────────────────────────────
# P3: Latency + write-path failover gap
# ─────────────────────────────────────────────────────────────────────
hdr "P3  Latency delta & write-path failover gap"

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"qa","version":"0"}}}'

bench() {
  local label="$1" ; local extra="$2"
  local total=0 min=999 max=0 n=5
  for i in $(seq 1 $n); do
    local t
    t=$(curl -sS -o /dev/null -w '%{time_total}' -X POST "$DOMAIN/mcp" \
      -H 'content-type: application/json' \
      -H 'accept: application/json' \
      $extra --max-time 15 -d "$INIT")
    total=$(python3 -c "print($total + $t)")
    min=$(python3 -c "print(min($min, $t))")
    max=$(python3 -c "print(max($max, $t))")
  done
  local avg
  avg=$(python3 -c "print(round($total / $n, 3))")
  printf "  %-30s avg=%.3fs min=%.3fs max=%.3fs  (n=%d)\n" "$label" "$avg" "$min" "$max" "$n"
}

echo "/mcp handshake latency benchmark (5x each):"
bench "railway-a (primary)" ""
if [[ -n "${CANARY_SECRET:-}" ]]; then
  bench "replit (via canary)" "-H \"X-Dchub-Canary: $CANARY_SECRET\""
else
  echo "  (skipping replit benchmark: CANARY_SECRET not set)"
fi

cat <<'EOF'

Replit cold-start note: if replit avg > 2x railway-a, expected — Replit boots
on demand. Acceptable for failover (emergency path). Not acceptable if ever
promoted to primary. Keep Railway hot.

Write-path failover gap:
  /publish/all (cron daily digest), /api/press-releases/{slug} (news pages),
  and cron cache seed still hit RAILWAY_BACKEND only — no failover. Low
  urgency (write-only or with 404 fallback) but asymmetric with the rest of
  the system. See dchubapiproxy-v4.5.11-notes.md in outputs for the patch.
EOF

# ─────────────────────────────────────────────────────────────────────
# P4: Rotation + action versions
# ─────────────────────────────────────────────────────────────────────
hdr "P4  Operational discipline"

NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "Fresh CANARY_SECRET candidate (for next 90-day rotation):"
echo "  $NEW_SECRET"
echo
cat <<EOF
Rotation procedure when you're ready (NOT required today):
  1. Bind new value as CANARY_SECRET on mcp-proxy Worker
  2. Bind same value on dchubapiproxy Worker
  3. Update GitHub Actions repo secret: dchub-backend → Settings → Actions
  4. Local verify: CANARY_SECRET="$NEW_SECRET" ./dchub-failover-check.sh drill
  5. GitHub verify: Actions → Failover canary → Run workflow
  6. Once both pass, previous secret is dead; no rollback needed.

actions/checkout version:
  Currently on @v4 (Node 20). GitHub forces Node 24 on Jun 2, 2026.
  @v5 will ship with Node 24 support — update workflow when released.
  No action needed until then; warning is cosmetic.
EOF

# ─────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────
hdr "Action summary"
cat <<EOF
Do now (high leverage):
  1. P1 — merge tool-descriptions-v2.json into backend, redeploy, rerun evals
  2. P2 — click "View build" on dchubapiproxy failed build, fix or disconnect
  3. P2 — verify DCHUB_API_KEYS KV binding exists on dchubapiproxy

Do soon (operational hygiene):
  4. P3 — apply v4.5.11 patch to add write-path failover (patch notes in outputs)
  5. P4 — set 90-day calendar reminder for CANARY_SECRET rotation

Do on deadline (months away):
  6. P4 — update actions/checkout@v4 → @v5 before June 2026

All artifacts saved to: $OUT
EOF
