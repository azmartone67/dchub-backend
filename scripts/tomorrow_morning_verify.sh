#!/usr/bin/env bash
# tomorrow_morning_verify.sh — single-shot read of what r46/r47 actually moved.
# Run this ~24h after the r47.x deploys to see the funnel data.

set -u
echo "═══════════════════════════════════════════════════════════════════"
echo "  r46/r47 ACTIVATION READ — $(date -u +%Y-%m-%dT%H:%MZ)"
echo "═══════════════════════════════════════════════════════════════════"

# ─── 1. Top-line activation summary ───────────────────────────────────
echo
echo "─── Activation summary (top-line) ────────────────────────────────"
railway run python3 -c "
import os, psycopg, json
url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
    cur.execute('SELECT * FROM v_conversion_summary')
    cols = [c.name for c in cur.description]
    row = cur.fetchone()
    print(json.dumps(dict(zip(cols, [str(v) for v in row])), indent=2))
" 2>/dev/null | tail -8

# ─── 2. Daily trend ──────────────────────────────────────────────────
echo
echo "─── Daily activation trend (last 14 days) ────────────────────────"
railway run python3 -c "
import os, psycopg
url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
    cur.execute('SELECT * FROM v_daily_activation ORDER BY day DESC LIMIT 14')
    print(f'{\"day\":12} {\"issued\":>7} {\"activated\":>9} {\"paid_tool\":>9} {\"pct_act\":>7}')
    for r in cur.fetchall():
        print(f'{str(r[0]):12} {r[1] or 0:>7} {r[2] or 0:>9} {r[3] or 0:>9} {str(r[4]) if r[4] is not None else \"—\":>7}%')
" 2>/dev/null | tail -20

# ─── 3. Paywall attribution by source (UA bucketing) ──────────────────
echo
echo "─── Paywall hits by source (24h) ─────────────────────────────────"
railway run python3 -c "
import os, psycopg
url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
    cur.execute(\"\"\"SELECT source, sum(blocks)::int AS blocks, sum(unique_sessions)::int AS sessions
                    FROM v_paywall_attribution
                    WHERE day >= CURRENT_DATE - INTERVAL '1 day'
                    GROUP BY source ORDER BY 2 DESC LIMIT 10\"\"\")
    print(f'{\"source\":40} {\"blocks\":>7} {\"sessions\":>9}')
    for r in cur.fetchall(): print(f'{r[0]:40} {r[1]:>7} {r[2]:>9}')
" 2>/dev/null | tail -15

# ─── 4. Top first-paywall tools (where conversion attention lands) ────
echo
echo "─── Top first-paywall triggers (24h vs 7d) ───────────────────────"
railway run python3 -c "
import os, psycopg
url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
    cur.execute(\"\"\"SELECT first_paywall_tool,
                           count(*) FILTER (WHERE first_paywall_at >= NOW() - INTERVAL '1 day')::int AS d1,
                           count(*) FILTER (WHERE first_paywall_at >= NOW() - INTERVAL '7 days')::int AS d7
                    FROM v_first_paywall_tool
                    GROUP BY 1 ORDER BY 3 DESC LIMIT 10\"\"\")
    print(f'{\"tool\":35} {\"24h\":>5} {\"7d\":>6}')
    for r in cur.fetchall(): print(f'{r[0]:35} {r[1]:>5} {r[2]:>6}')
" 2>/dev/null | tail -15

# ─── 5. Demand signal (high-intent sessions) ──────────────────────────
echo
echo "─── High-intent sessions (3+ paywall hits) ───────────────────────"
railway run python3 -c "
import os, psycopg
url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
    cur.execute(\"\"\"SELECT count(*) AS total,
                           count(*) FILTER (WHERE api_key='anonymous')::int AS anon,
                           count(*) FILTER (WHERE api_key<>'anonymous')::int AS keyed
                    FROM v_demand_signal\"\"\")
    t, a, k = cur.fetchone()
    print(f'  Total high-intent sessions: {t}')
    print(f'  Anonymous: {a}    Keyed: {k}')
" 2>/dev/null | tail -5

# ─── 6. Smoke-check the key endpoints still work ──────────────────────
echo
echo "─── Endpoint smoke check ─────────────────────────────────────────"
for path in \
  "/api/v1/interconnection-queue/snapshot" \
  "/api/v1/spare-capacity/listings?_=cb$(date +%s)" \
  "/ai-capacity-index/today.json" \
  "/hyperscaler-deals.rss" \
  "/api/v1/onboard/health"
do
  code=$(curl -sLo /dev/null -w "%{http_code}" --max-time 12 "https://dchub.cloud$path")
  echo "  $code  $path"
done

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  Done. Compare against yesterday's baseline:"
echo "    keys_issued: 4 → ?"
echo "    pct_activated: 4.0% → ?"
echo "    funnel_leak_critical (paywall→click): 0.005% → ?"
echo "═══════════════════════════════════════════════════════════════════"
