#!/usr/bin/env bash
# Overnight cleanup - safe operations only. Diagnostic report at the end.
set -u
REPORT=~/workspace/MORNING_REPORT.txt
: > "$REPORT"
echo "Sleep cleanup run: $(date)" | tee -a "$REPORT"
echo "=========================================" | tee -a "$REPORT"

# ---------------------------------------------------------------
# #4: Remove orphan functions/ dir from dchub-backend (wrong repo)
# ---------------------------------------------------------------
echo "" | tee -a "$REPORT"
echo "[#4] Orphan functions/ in dchub-backend" | tee -a "$REPORT"
if [ -d ~/workspace/functions ] && [ -f ~/workspace/functions/press-release.js ]; then
  cd ~/workspace
  git rm -f functions/press-release.js functions/press.js 2>&1 | tee -a "$REPORT"
  # rmdir only if dir is now empty
  rmdir functions 2>/dev/null && echo "  (functions/ dir removed)" | tee -a "$REPORT"
  git commit -m "cleanup: remove orphan functions/ - CF Pages uses dchub-frontend repo" 2>&1 | tail -3 | tee -a "$REPORT"
  git push origin main 2>&1 | tail -3 | tee -a "$REPORT"
else
  echo "  SKIP: no orphan functions/ found in dchub-backend" | tee -a "$REPORT"
fi

# ---------------------------------------------------------------
# #6: Fix _redirects ordering in dchub-frontend
# ---------------------------------------------------------------
echo "" | tee -a "$REPORT"
echo "[#6] _redirects ordering in dchub-frontend" | tee -a "$REPORT"
cd ~/dchub-frontend
git pull --rebase origin main >/dev/null 2>&1

python3 <<'PY' 2>&1 | tee -a ~/workspace/MORNING_REPORT.txt
import pathlib, re
p = pathlib.Path('_redirects')
lines = p.read_text().splitlines()
# Move exact-match rules flagged by CF above any splat/placeholder lines.
priority = {'/markets/ashburn', '/ai.txt', '/shell', '/shell-rates',
            '/lease-rates', '/powered-shell-rates',
            '/markets/dallas-fort-worth', '/markets/new-york-tristate'}
preserved, moved = [], []
for ln in lines:
    first = ln.split(None, 1)[0] if ln.strip() and not ln.startswith('#') else ''
    if first in priority:
        moved.append(ln)
    else:
        preserved.append(ln)
if moved:
    # insert moved rules right after any leading comments/blanks
    out, hdr = [], True
    inserted = False
    for ln in preserved:
        if hdr and (ln.startswith('#') or not ln.strip()):
            out.append(ln); continue
        if not inserted:
            out.extend(moved); out.append(''); inserted = True
        hdr = False
        out.append(ln)
    if not inserted:
        out = moved + [''] + preserved
    p.write_text('\n'.join(out) + '\n')
    print(f"  moved {len(moved)} rules to top")
else:
    print("  no matching rules found (maybe already fixed)")
PY

if ! git diff --quiet _redirects 2>/dev/null; then
  git add _redirects
  git commit -m "perf: move exact-match redirects above splat rules (CF hint)" 2>&1 | tail -3 | tee -a "$REPORT"
  git push origin main 2>&1 | tail -3 | tee -a "$REPORT"
else
  echo "  _redirects unchanged (nothing to commit)" | tee -a "$REPORT"
fi

# ---------------------------------------------------------------
# #1: LinkedIn double-registration — DIAGNOSE ONLY
# ---------------------------------------------------------------
echo "" | tee -a "$REPORT"
echo "[#1] LinkedIn/AI-Digest double-registration (diagnostic)" | tee -a "$REPORT"
cd ~/workspace
echo "  --- Blueprint registrations in main.py ---" | tee -a "$REPORT"
grep -nE "register_blueprint|linkedin|weekly_digest|ai_weekly|scheduler\.add_job" main.py 2>/dev/null | \
  grep -iE "linkedin|weekly|digest|poster" | tee -a "$REPORT"
echo "  --- Files that import linkedin/digest modules ---" | tee -a "$REPORT"
grep -rlnE "from.*linkedin|from.*weekly_digest|import.*LinkedIn" --include="*.py" \
  ~/workspace 2>/dev/null | head -10 | tee -a "$REPORT"
echo "  --- APScheduler job definitions ---" | tee -a "$REPORT"
grep -nE "add_job|cron|interval" main.py 2>/dev/null | head -20 | tee -a "$REPORT"

# ---------------------------------------------------------------
# #5: Cron freshness check
# ---------------------------------------------------------------
echo "" | tee -a "$REPORT"
echo "[#5] press_releases cron freshness" | tee -a "$REPORT"
curl -s https://dchub-backend-production.up.railway.app/api/press-releases/list 2>/dev/null | \
  python3 -c "
import sys, json
from datetime import datetime, timezone
d = json.load(sys.stdin)
print(f'  total={d.get(\"count\")}, most recent dates:')
for r in (d.get('releases') or [])[:5]:
    print(f'    {r.get(\"date\"):12} | {r.get(\"slug\")}')
" 2>&1 | tee -a "$REPORT"

echo "" | tee -a "$REPORT"
echo "=========================================" | tee -a "$REPORT"
echo "Morning report saved: $REPORT" | tee -a "$REPORT"
echo "" | tee -a "$REPORT"
echo "Items still needing your hands tomorrow:" | tee -a "$REPORT"
echo "  #2 MCP proxy worker: zip upload via CF dashboard" | tee -a "$REPORT"
echo "  #3 INTERNAL_SYNC_SECRET: set CF worker env var in dashboard" | tee -a "$REPORT"
