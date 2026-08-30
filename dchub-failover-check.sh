#!/usr/bin/env bash
# dchub-failover-check.sh v3.0 — current-contract failover canary (2026-07).
#
# WHY THIS WAS RED FOR 92 DAYS: the Replit secondary was retired for Render, and
# the backend header `x-backend-used` was replaced by `x-dc-hub-served-by`. The
# old drill forced a failover and asserted `x-backend-used: replit`, so it failed
# every run (expected=replit got=MISSING). This version validates the LIVE contract:
#   - dchub.cloud serves 200 through the failover-aware edge worker
#     (x-dc-hub-served-by present; primary = railway-primary)
#
# v3.1 (2026-07-30) — LC3: the deep forced-failover drill, now that both v3.0 TODO
# unknowns are confirmed live.
#
#   force mechanism : X-DCHUB-Force-Backend: render|railway  (_worker.js:1981)
#                     per-request QA escape hatch, unauthenticated, never mutates
#                     global routing.
#   Render's tag    : render-PRIMARY, not render-secondary. _tagBackend()
#                     (_worker.js:1906) emits `${backend}-${mode}`, and the force
#                     header makes render the CHOSEN primary. Asserting
#                     "render-secondary" would have failed 100% of runs on a
#                     perfectly healthy system — the SAME bug class as the 92-day
#                     `x-backend-used: replit` red this script was written to end.
#
# ★★ TWO WAYS THIS DRILL CAN LIE, both now closed:
#
#  1. v3.0's step-3 "forced" probe was a DEAD NO-OP. It sent
#     `X-Dchub-Canary: $CANARY_SECRET`, a header _worker.js reads NOWHERE (verified
#     on frontend main: zero non-comment hits). So `[api forced] INFO` has been
#     printing the PRIMARY's own values under a "forced" label — reassuring, and
#     evidence of nothing.
#
#  2. CLOUDFLARE CACHE. A forced GET whose response is already cached returns the
#     cached PRIMARY body, force header and all. Measured live: without a
#     cache-buster the forced probe returned railway-primary/is_failover=false;
#     with one, the same request returned render-primary/is_failover=true. Every
#     probe below therefore carries a unique `?_=<ts>`.
#
# The drill asserts is_failover=true on the payload SPECIFICALLY so that a silently
# unforced probe (either failure above) is FATAL rather than flattering.
#
# Ships with FAILOVER_DEEP_DRILL_ENFORCE=0: the deep drill prints WOULD-FAIL and
# does not set the exit code, so it cannot flip a green feed red on day one.
# ★ Arming it to =1 after one clean week is a REQUIRED follow-up.
set -u

MCP_URL="https://dchub.cloud/mcp"
API_URL="https://dchub.cloud/api/ai/mcp-health"
FRESH_URL="https://dchub.cloud/api/v1/ops/origin-freshness"
# ★2026-08-30 — the ARTIFACT a third party actually receives, not a proxy for it.
# FRESH_URL reports DATA age (data_age_hours/stale) and both origins share the
# Neon DB, so it reads stale:false on Render even while Render serves a
# month-old BUILD. This file is baked into the build, so it is the thing that
# goes stale. Measured through the forced-Render path on 2026-08-30:
#   render  generated_at 2026-07-30T08:26:39Z  facilities 15,300+  deals 1,600+
#   railway generated_at 2026-08-30T09:08:39Z  facilities 19,500+  deals 2,000+
ARTIFACT_URL="https://dchub.cloud/.well-known/mcp_facts.json"
# The exporter runs daily (mcp-facts-export.yml, 05:17 UTC) and an autoDeploy
# mirror rebuilds on every merge, so 48h is generous for a healthy mirror and
# still catches the real break (31 days when this assertion was written).
ARTIFACT_MAX_AGE_H="${FAILOVER_ARTIFACT_MAX_AGE_H:-48}"
UA="dchub-failover-canary/3.1"          # a User-Agent avoids CF bot-403 on this zone
EXPECT_PRIMARY="railway-primary"
EXPECT_FORCED="render-primary"
ENFORCE="${FAILOVER_DEEP_DRILL_ENFORCE:-0}"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"canary","version":"1"}}}'
FAIL=0

served_by() { printf '%s' "$1" | awk -F': *' 'tolower($1)=="x-dc-hub-served-by"{print tolower($2)}' | tr -d '\r\n'; }
http_status() { printf '%s' "$1" | head -1 | awk '{print $2}'; }

MODE="${1:-canary}"
[[ "$MODE" == "drill" ]] && echo "=== dchub failover canary (current contract) ==="

# 1) MCP edge accepts an initialize (200)
mcp_hdrs=$(curl -sS -o /dev/null -D - -X POST "$MCP_URL" -A "$UA" \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  --max-time 20 --data "$INIT")
mcp_status=$(http_status "$mcp_hdrs")
if [[ "$mcp_status" != "200" ]]; then
  echo "[mcp primary] FAIL status=${mcp_status:-none}" >&2; FAIL=1
else
  echo "[mcp primary] OK status=200 served-by=$(served_by "$mcp_hdrs")"
fi

# 2) API serves 200 through the failover-aware worker, from the primary origin
api_hdrs=$(curl -sS -o /dev/null -D - "$API_URL" -A "$UA" --max-time 20)
api_status=$(http_status "$api_hdrs")
api_served=$(served_by "$api_hdrs")
if [[ "$api_status" != "200" ]]; then
  echo "[api primary] FAIL status=${api_status:-none}" >&2; FAIL=1
elif [[ -z "$api_served" ]]; then
  echo "[api primary] FAIL x-dc-hub-served-by MISSING (failover worker not in request path)" >&2; FAIL=1
elif [[ "$api_served" == "$EXPECT_PRIMARY" ]]; then
  echo "[api primary] OK served-by=$api_served"
else
  # a non-primary served-by is a healthy failover in progress, not a canary failure
  echo "[api primary] WARN served-by=$api_served (not $EXPECT_PRIMARY — failover may be active)"
fi

# 3) DEEP DRILL — force the edge onto Render and prove the mirror actually serves.
#    Replaces v3.0's dead X-Dchub-Canary probe. CANARY_SECRET is no longer used:
#    the real force mechanism needs no secret.
deep_fail=0
note() {   # respect ENFORCE: loud either way, only fatal when armed
  if [[ "$ENFORCE" == "1" ]]; then echo "[api forced] FAIL $1" >&2; deep_fail=1
  else echo "[api forced] WOULD-FAIL $1 (ENFORCE=0, not failing the run)"; fi
}

cb="$(date +%s)$RANDOM"     # ★ cache-buster: without it CF serves the cached PRIMARY
f_hdrs=$(curl -sS -o /dev/null -D - "$FRESH_URL?_=$cb" -A "$UA" --max-time 25 \
  -H "X-DCHUB-Force-Backend: render")
f_body=$(curl -sS "$FRESH_URL?_=${cb}b" -A "$UA" --max-time 25 \
  -H "X-DCHUB-Force-Backend: render")
f_status=$(http_status "$f_hdrs")
f_served=$(served_by "$f_hdrs")
f_isfo=$(printf '%s' "$f_body" | tr -d ' ' | grep -o '"is_failover":[a-z]*' | cut -d: -f2)
f_commit=$(printf '%s' "$f_body" | grep -o '"commit":"[^"]*"' | cut -d'"' -f4)

if [[ "$f_status" != "200" ]]; then
  note "forced probe status=${f_status:-none}"
elif [[ "$f_isfo" != "true" ]]; then
  # THE important assertion. If the force silently did not apply — dead header, CF
  # cache, worker rollback — we are reading the PRIMARY and would otherwise report
  # a passing "failover drill" having never touched Render.
  note "is_failover=${f_isfo:-missing} — the force did NOT apply; this probe read the PRIMARY, not Render"
elif [[ "$f_served" != "$EXPECT_FORCED" ]]; then
  note "served-by=$f_served expected=$EXPECT_FORCED"
else
  echo "[api forced] OK served-by=$f_served is_failover=true commit=${f_commit:-?}"
  # Commit drift: assert ANCESTRY, never equality. Render trails main by design
  # (measured: 247 commits, still a true ancestor); equality would false-red on
  # every Railway deploy. Needs fetch-depth: 0 — a shallow checkout cannot answer
  # this, so an unknown object is reported as UNKNOWN, never as drift.
  if [[ -n "$f_commit" ]] && git cat-file -e "${f_commit}^{commit}" 2>/dev/null; then
    if git merge-base --is-ancestor "$f_commit" HEAD 2>/dev/null; then
      echo "[api forced] OK render commit $f_commit is an ancestor of HEAD ($(git rev-list --count "$f_commit"..HEAD 2>/dev/null) behind)"
    else
      note "render commit $f_commit is NOT an ancestor of HEAD — the mirror is on a divergent build"
    fi
  else
    echo "[api forced] UNKNOWN commit $f_commit not in this clone (need fetch-depth: 0) — not treated as drift"
  fi
fi

# 3b) SERVED-ARTIFACT FRESHNESS — the assertion the ancestry check cannot make.
#
# ★ Ancestry is deliberately not recency. The check above asserts Render's commit
# is an ANCESTOR of HEAD precisely so it will not false-red on every Railway
# deploy ("Render trails main by design"). A build 151 commits and 31 days behind
# is a perfect ancestor and passes. That is correct for divergence and blind to
# staleness, so staleness needs its own assertion — against the served bytes.
#
# ★ Why this is not paranoia: on 2026-08-30 a Railway deploy moved the edge onto
# Render for ~minutes and dchub.cloud published a 30-day-old mcp_facts.json —
# HTTP 200, cf-cache-status DYNAMIC, valid JSON, plausible numbers, facilities
# 21% understated. Nothing failed. Only x-dc-hub-served-by distinguished it.
a_cb="$(date +%s)$RANDOM"
a_body=$(curl -sS "$ARTIFACT_URL?_=$a_cb" -A "$UA" --max-time 25 \
  -H "X-DCHUB-Force-Backend: render" || true)
# The served copy is pretty-printed, so strip whitespace first — same shape as
# the is_failover/commit greps above. A tighter pattern silently matched
# nothing and reported "cannot judge freshness" instead of the real verdict.
a_gen=$(printf '%s' "$a_body" | tr -d ' \n' | grep -o '"generated_at":"[^"]*"' | head -1 | cut -d'"' -f4)
if [[ -z "$a_gen" ]]; then
  note "forced artifact $ARTIFACT_URL has no generated_at (body ${#a_body} bytes) — cannot judge mirror freshness"
else
  a_epoch=$(date -u -d "$a_gen" +%s 2>/dev/null || date -u -jf "%Y-%m-%dT%H:%M:%SZ" "$a_gen" +%s 2>/dev/null || echo "")
  if [[ -z "$a_epoch" ]]; then
    note "could not parse generated_at=$a_gen from the forced artifact"
  else
    a_age_h=$(( ( $(date -u +%s) - a_epoch ) / 3600 ))
    if (( a_age_h > ARTIFACT_MAX_AGE_H )); then
      note "the MIRROR serves a stale public surface: $ARTIFACT_URL generated_at=$a_gen is ${a_age_h}h old (max ${ARTIFACT_MAX_AGE_H}h). Every Railway deploy fails the edge onto this copy. FIX: set autoDeploy=yes on the Render service (srv-d86g7g6gvqtc73dlpojg), or add RENDER_DEPLOY_HOOK_URL and fire it on merge. Arming/lowering this threshold is NOT the fix."
    else
      echo "[api forced] OK mirror artifact generated_at=$a_gen (${a_age_h}h old, max ${ARTIFACT_MAX_AGE_H}h)"
    fi
  fi
fi

[[ "$deep_fail" -eq 1 ]] && FAIL=1

# NOT COVERED, stated so it is never mistaken for tested: the KV tier
# (x-cache-kv / x-cache-kv-age) is consulted ONLY after BOTH origins fail and has no
# per-request pin, so it cannot be drilled without a real outage. There is also no
# generic D1 serving tier — env.DCHUB_DB backs specific query routes, not serving.
echo "[coverage] KV tier NOT drilled (no per-request pin; needs a real dual-origin outage)"

[[ "$MODE" == "drill" && $FAIL -eq 0 ]] && echo "=== all checks passed ==="
exit $FAIL
