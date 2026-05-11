#!/usr/bin/env python3
"""Bulk DCPI lite-scoring v2 — calibrated formulas + proper rounding + state field."""
import os, sys

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr); sys.exit(2)
try: import psycopg2
except ImportError: print("ERROR: psycopg2 missing", file=sys.stderr); sys.exit(2)


def main():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    print("[bulk-score v2] connecting...")

    with conn.cursor() as cur:
        # Get all city stats
        cur.execute("""
            SELECT LOWER(city), city, state,
                   COUNT(*) AS fac,
                   COALESCE(SUM(power_mw), 0) AS op_mw,
                   COALESCE(SUM(power_mw) FILTER (WHERE status IN
                       ('construction','planned','permitting','Under Construction','Planned')), 0) AS pipe_mw
            FROM discovered_facilities
            WHERE city IS NOT NULL AND city != ''
              AND state IS NOT NULL AND LENGTH(state) = 2 AND state ~ '^[A-Z]{2}$'
              AND (country = 'US' OR country = 'USA')
            GROUP BY LOWER(city), city, state
            HAVING COUNT(*) >= 3
            ORDER BY fac DESC LIMIT 350;
        """)
        rows = cur.fetchall()
        print(f"[bulk-score v2] {len(rows)} candidate markets")

        # Get full-scored slugs (these we won't touch)
        cur.execute("""
            SELECT DISTINCT market_slug FROM market_power_scores
            WHERE tier_required IS NULL OR tier_required != 'lite-pro';
        """)
        full_slugs = {r[0] for r in cur.fetchall()}
        print(f"[bulk-score v2] {len(full_slugs)} markets have FULL scoring — preserved")

        # Get existing lite-pro slugs (these we'll UPDATE)
        cur.execute("SELECT DISTINCT market_slug FROM market_power_scores WHERE tier_required = 'lite-pro';")
        lite_slugs = {r[0] for r in cur.fetchall()}
        print(f"[bulk-score v2] {len(lite_slugs)} markets have LITE scoring — will update")

        scored = 0
        errors = 0
        for r in rows:
            try:
                slug_l, name, state, fac, op_mw, pipe_mw = r
                slug = slug_l.replace(" ", "-").replace(",", "").replace("/", "-")

                # Don't overwrite full-scored markets
                if slug in full_slugs: continue

                # $/kWh from state
                cur.execute("""
                    SELECT AVG(price_cents_kwh)/100.0 FROM eia_electricity_rates
                    WHERE state=%s AND sector='ALL' AND retrieved_at > NOW() - INTERVAL '365 days';
                """, (state,))
                kr = cur.fetchone()
                kwh = float(kr[0]) if kr and kr[0] else None

                # ===== CALIBRATED FORMULAS =====
                op_mw_f = float(op_mw or 0)
                pipe_mw_f = float(pipe_mw or 0)
                fac_n = int(fac or 0)

                # Constraint: pipe pressure on existing capacity
                # 0.5 ratio = 30, 1.0 = 60, 1.5 = 90, 2.0+ = 100
                pipe_ratio = (pipe_mw_f / op_mw_f) if op_mw_f > 0 else 0
                constraint = min(100.0, pipe_ratio * 60)
                # Additional constraint from absolute pipe magnitude
                if pipe_mw_f > 500: constraint += 10
                if pipe_mw_f > 1000: constraint += 10
                constraint = min(100.0, constraint)

                # Excess power: cheap energy + underbuilt = opportunity
                excess = 0.0
                if kwh is not None:
                    # $0.08/kWh = 73 points, $0.12 = 60, $0.20 = 33, $0.30 = 0
                    excess = max(0.0, min(100.0, (0.30 - kwh) * 333))
                # Underbuilt market bonus
                if pipe_mw_f < 100 and op_mw_f > 200:
                    excess = max(excess, 65.0)
                # Ecosystem maturity bonus
                if fac_n >= 30: excess = max(excess, 50.0)
                elif fac_n >= 15: excess = max(excess, 35.0)

                # Round to 1 decimal
                constraint = round(constraint, 1)
                excess = round(excess, 1)

                # Verdict
                if excess > 50 and constraint < 60: verdict = "BUILD"
                elif constraint > 75: verdict = "AVOID"
                else: verdict = "CAUTION"

                # INSERT (history-preserving)
                cur.execute("""
                    INSERT INTO market_power_scores
                    (market_slug, market_name, latitude, longitude,
                     constraint_score, excess_power_score,
                     verdict, tier_required, computed_at)
                    VALUES (%s, %s, NULL, NULL, %s, %s, %s, 'lite-pro', NOW());
                """, (slug, name, constraint, excess, verdict))

                # Also store state in market_name for now (no separate field)
                # Set a market_state column if it exists
                cur.execute("""
                    UPDATE market_power_scores
                    SET market_name = %s
                    WHERE market_slug = %s AND tier_required = 'lite-pro'
                      AND computed_at = (SELECT MAX(computed_at) FROM market_power_scores
                                         WHERE market_slug = %s AND tier_required = 'lite-pro');
                """, (f"{name}, {state}", slug, slug))

                scored += 1
                if scored % 30 == 0:
                    conn.commit()
                    print(f"[bulk-score v2] {scored} (re)scored...")
            except Exception as e:
                errors += 1
                conn.rollback()
                if errors <= 3:
                    print(f"[bulk-score v2] err {r[1]}: {str(e)[:120]}")

        conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT market_slug) FROM market_power_scores;")
        total = cur.fetchone()[0]
    conn.close()

    print(f"\n[bulk-score v2] complete:")
    print(f"  re-scored: {scored} · errors: {errors} · total unique slugs: {total}")


if __name__ == "__main__":
    main()
