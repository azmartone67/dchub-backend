#!/usr/bin/env bash
# dchub_disruption_audit.sh
# One shell entry-point that runs all 5 disruption probes against dchub.cloud
# and benchmarks against DC Hawk / DCBytes / datacenters.com.
#
# Usage:
#   DCHUB_API_KEY=dchub_live_… ./scripts/dchub_disruption_audit.sh
#   DCHUB_API_KEY=… ./scripts/dchub_disruption_audit.sh --json > audit.json
#
# Read-only. Hits no destructive endpoints (no /heal/master-cycle, no writes).

set -euo pipefail
CURL=/usr/bin/curl
KEY="${DCHUB_API_KEY:-}"
BASE="${DCHUB_BASE:-https://dchub.cloud}"
OUT_DIR="${OUT_DIR:-/tmp/dchub-audit-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT_DIR"
JSON_MODE=0
[[ "${1:-}" == "--json" ]] && JSON_MODE=1

if [[ -z "$KEY" ]]; then
  echo "ERROR: set DCHUB_API_KEY (enterprise key recommended)" >&2; exit 2
fi

H=(-H "X-API-Key: $KEY" -H "Accept: application/json" --max-time 20)

banner() { printf "\n\033[1;36m━━━ %s ━━━\033[0m\n" "$1"; }
sub()    { printf "\033[1;33m• %s\033[0m\n" "$1"; }

fetch() { # fetch <path> <out_basename>
  local p="$1" out="$OUT_DIR/$2"
  local code http_code
  http_code=$("$CURL" -sL "${H[@]}" -o "$out" -w "%{http_code}" "$BASE$p" || echo "000")
  printf "  %-44s HTTP %s  bytes=%s\n" "$p" "$http_code" "$(wc -c <"$out" | tr -d ' ')"
}

# ─────────────────────────────────────────────────────────────────────────
banner "Probe 0 — auth + plan"
"$CURL" -s "${H[@]}" "$BASE/api/v1/me" | tee "$OUT_DIR/me.json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); u=d.get('user',{}); print(f'  user={u.get(\"email\",\"?\")}  plan={u.get(\"plan\",\"?\")}  active={u.get(\"is_active\")}')" \
  || echo "  (parse failed; see $OUT_DIR/me.json)"

# ─────────────────────────────────────────────────────────────────────────
banner "Probe 1 — MCP funnel reality check"
sub "live funnel snapshot"
fetch "/api/v1/mcp/funnel"            funnel.json
fetch "/api/v1/mcp/conversion-funnel" conversion_funnel.json
fetch "/api/v1/mcp/power-users"       power_users.json
python3 - "$OUT_DIR/funnel.json" <<'PY' || true
import json,sys,os
p=sys.argv[1]
if not os.path.exists(p): sys.exit(0)
try: d=json.load(open(p))
except Exception as e: print(f"  parse fail: {e}"); sys.exit(0)
calls=d.get("tool_calls_7d") or d.get("calls_7d") or d.get("totals",{}).get("tool_calls_7d","?")
sigs =d.get("upgrade_signals_7d") or d.get("signals_7d") or d.get("totals",{}).get("upgrade_signals_7d","?")
conv =d.get("conversions_30d") or d.get("totals",{}).get("conversions_30d","?")
keys =d.get("active_keys") or d.get("totals",{}).get("active_keys","?")
print(f"  calls(7d)={calls}  signals(7d)={sigs}  conv(30d)={conv}  active_keys={keys}")
try:
    r=int(sigs)/max(int(calls),1)*100
    print(f"  paywall hit rate: {r:.1f}%")
    if isinstance(conv,int) and conv>=0:
        denom=int(sigs) if sigs!='?' else 1
        print(f"  signal→paid (rolling): 1/{denom:,}  ({100*int(conv)/max(denom,1):.4f}%)")
except: pass
PY

# ─────────────────────────────────────────────────────────────────────────
banner "Probe 2 — Self-heal proof (is the data actually fresh?)"
sub "deep health + heal findings"
fetch "/api/v1/heartbeat/page"        heartbeat.json
fetch "/api/v1/qa/dashboard"          qa_dashboard.json
fetch "/api/v1/heal/findings"         heal_findings.json
fetch "/audit/"                       audit_page.html
fetch "/health"                       health.json
fetch "/health/deep"                  health_deep.json
python3 - "$OUT_DIR/heal_findings.json" "$OUT_DIR/heartbeat.json" <<'PY' || true
import json,sys,os
for tag,p in [("heal_findings",sys.argv[1]),("heartbeat",sys.argv[2])]:
    if not os.path.exists(p): continue
    try: d=json.load(open(p))
    except: print(f"  {tag}: not JSON"); continue
    if isinstance(d,dict):
        keys=list(d.keys())[:10]
        print(f"  {tag} keys: {keys}")
        # surface freshness-like fields
        for k in ("last_run","last_heal_at","ran_at","last_refresh","stale_count","fresh_count","actions","verdict"):
            if k in d: print(f"    {k} = {str(d[k])[:160]}")
PY

# ─────────────────────────────────────────────────────────────────────────
banner "Probe 3 — DCPI as a citable artifact"
sub "live count + quality + landing-page surface"
fetch "/api/v1/dcpi/live-count"  dcpi_count.json
fetch "/api/v1/dcpi/quality"     dcpi_quality.json
fetch "/dcpi"                    dcpi_page.html
fetch "/dcpi/leaderboard"        dcpi_leaderboard.html
fetch "/api/v1/dcpi/leaderboard" dcpi_leaderboard.json
sub "is DCPI machine-readable / citable?"
python3 - "$OUT_DIR/dcpi_page.html" <<'PY' || true
import re,sys,os
p=sys.argv[1]
if not os.path.exists(p): sys.exit(0)
h=open(p,encoding='utf-8',errors='ignore').read()
def has(pat,label):
    n=len(re.findall(pat,h,re.I))
    print(f"  {label}: {n}")
has(r'application/ld\+json',     "JSON-LD blocks (schema.org)")
has(r'"@type"\s*:\s*"Dataset"',  "schema.org Dataset typing")
has(r'<meta\s+property="og:',    "OpenGraph tags")
has(r'<meta\s+name="twitter:',   "Twitter card tags")
has(r'canonical',                "canonical link rel")
has(r'<link[^>]+oembed',         "OEmbed link")
has(r'cite this|how to cite|citation', "citation guidance")
has(r'methodology',              "methodology mention")
has(r'iframe|embed',             "embed/iframe pattern")
PY

# ─────────────────────────────────────────────────────────────────────────
banner "Probe 4 — AI-citation pipeline (are LLMs *supposed* to pick you up?)"
fetch "/ai.txt"      ai.txt
fetch "/llms.txt"    llms.txt
fetch "/llms-full.txt" llms_full.txt
fetch "/robots.txt"  robots.txt
fetch "/sitemap.xml" sitemap.xml
fetch "/"            home.html
sub "homepage AI signals"
python3 - "$OUT_DIR/home.html" <<'PY' || true
import re,sys,os
p=sys.argv[1]
if not os.path.exists(p): sys.exit(0)
h=open(p,encoding='utf-8',errors='ignore').read()
def has(pat,label):
    n=len(re.findall(pat,h,re.I)); print(f"  {label}: {n}")
has(r'application/ld\+json',     "JSON-LD blocks")
has(r'"@type"\s*:\s*"(Dataset|Organization|WebSite|Article)"', "schema.org core types")
has(r'<meta\s+property="og:',    "OpenGraph")
has(r'<meta\s+name="twitter:',   "Twitter card")
has(r'<link[^>]+rel="canonical', "canonical link")
has(r'aria-[a-z]+=',             "aria attrs (a11y)")
has(r'<main\b',                  "<main> landmark")
PY
sub "llms.txt contents (first 400 chars)"
head -c 400 "$OUT_DIR/llms.txt" 2>/dev/null || echo "  (no /llms.txt)"; echo

# ─────────────────────────────────────────────────────────────────────────
banner "Probe 5 — MCP funnel friction (anonymous → paid result)"
sub "round-trip count via curl, no key"
NOKEY=(-H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" --max-time 15)
i=0
step() { i=$((i+1)); printf "  step %s: %s\n" "$i" "$1"; }
step "initialize"
"$CURL" -s "${NOKEY[@]}" -X POST "$BASE/mcp" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"audit","version":"1"}}}' \
  -o "$OUT_DIR/mcp_init.txt" -w "    -> HTTP %{http_code} time=%{time_total}s\n"
step "list tools (anonymous)"
"$CURL" -s "${NOKEY[@]}" -X POST "$BASE/mcp" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  -o "$OUT_DIR/mcp_tools.txt" -w "    -> HTTP %{http_code} time=%{time_total}s\n"
step "call a paid tool anonymously (expect paid_only nudge)"
"$CURL" -s "${NOKEY[@]}" -X POST "$BASE/mcp" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"analyze_site","arguments":{"site_url":"dchub.cloud"}}}' \
  -o "$OUT_DIR/mcp_paid_anon.txt" -w "    -> HTTP %{http_code} time=%{time_total}s\n"
step "follow redeem link surface"
"$CURL" -sL -o "$OUT_DIR/redeem.html" -w "    -> HTTP %{http_code} time=%{time_total}s\n" \
  "$BASE/signup?from=mcp&tool=analyze_site&tier=free"
echo "  total steps to *attempt* conversion: $i  (3 MCP + 1 web)"

# ─────────────────────────────────────────────────────────────────────────
banner "Competitive scan — DC Hawk / DCBytes / datacenters.com"
for host in datacenterhawk.com dcbyte.com www.datacenters.com; do
  sub "$host"
  for path in / /ai.txt /llms.txt /llms-full.txt /robots.txt /mcp /api /sitemap.xml; do
    code=$("$CURL" -s -o /dev/null -w "%{http_code}" -L --max-time 10 "https://$host$path" || echo "000")
    printf "    %-22s %s\n" "$path" "$code"
  done
done

# ─────────────────────────────────────────────────────────────────────────
banner "Done"
echo "raw responses: $OUT_DIR"
