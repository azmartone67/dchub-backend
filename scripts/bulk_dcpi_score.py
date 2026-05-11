#!/usr/bin/env python3
"""
Bulk DCPI lite-scoring — direct Postgres, no HTTP.
Scores ALL US cities with ≥3 facilities into market_power_scores.

Usage:
    railway run python3 scripts/bulk_dcpi_score.py
OR locally:
    DATABASE_URL=$(railway variables get DATABASE_URL) python3 scripts/bulk_dcpi_score.py
"""
import os
import sys

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr)
    print("Run via: railway run python3 scripts/bulk_dcpi_score.py", file=sys.stderr)
    sys.exit(2)

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed", file=sys.stderr)
    sys.exit(2)


def main():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    scored = 0
    errors = 0
    print(f"[bulk-score] connecting to DB...")

    with conn.cursor() as cur:
        # Step 1: ensure unique constraint exists
        try:
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'market_power_scores_slug_key'
                    ) THEN
                        ALTER TABLE market_power_scores
                            ADD CONSTRAINT market_power_scores_slug_key UNIQUE (market_slug);
                    END IF;
                END $$;
            """)
            conn.commit()
            print("[bulk-score] unique constraint ensured")
        except Exception as e:
            print(f"[bulk-score] constraint setup err: {e}")
            conn.rollback()

        # Step 2: pull all US cities with ≥3 facilities + their state
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
            ORDER BY fac DESC
            LIMIT 250;
        """)
        rows = cur.fetchall()
        print(f"[bulk-score] {len(rows)} candidate markets")

        # Step 3: score each + upsert
        for r in rows:
            try:
                slug_l, name, state, fac, op_mw, pipe_mw = r
                slug = slug_l.replace(" ", "-").replace(",", "").replace("/", "-")

                # $/kWh lookup
                cur.execute("""
                    SELECT AVG(price_cents_kwh)/100.0
                    FROM eia_electricity_rates
                    WHERE state = %s AND sector = 'ALL'
                      AND retrieved_at > NOW() - INTERVAL '365 days';
                """, (state,))
                kr = cur.fetchone()
                kwh = float(kr[0]) if kr and kr[0] else None

                # Lite scoring
                pipe_ratio = (float(pipe_mw) / float(op_mw)) if op_mw and float(op_mw) > 0 else 0
                constraint_score = min(100.0, pipe_ratio * 150)
                excess_score = 0.0
                if kwh is not None:
                    excess_score = max(0.0, min(100.0, (0.30 - kwh) * 333))
                if float(pipe_mw) < 50 and float(op_mw) > 100:
                    excess_score = max(excess_score, 60.0)
                if float(fac) >= 20:
                    excess_score = max(excess_score, 50.0)  # ecosystem maturity bonus

                if excess_score > 50 and constraint_score < 60:
                    verdict = "BUILD"
                elif constraint_score > 75:
                    verdict = "AVOID"
                else:
                    verdict = "CAUTION"

                cur.execute("""
                    INSERT INTO market_power_scores
                    (market_slug, market_name, constraint_score, excess_power_score, verdict,
                     tier_required, computed_at)
                    VALUES (%s, %s, %s, %s, %s, 'lite-pro', NOW())
                    ON CONFLICT (market_slug) DO UPDATE SET
                      constraint_score = EXCLUDED.constraint_score,
                      excess_power_score = EXCLUDED.excess_power_score,
                      verdict = EXCLUDED.verdict,
                      computed_at = NOW();
                """, (slug, name, constraint_score, excess_score, verdict))
                scored += 1
                if scored % 25 == 0:
                    conn.commit()
                    print(f"[bulk-score] {scored} scored...")
            except Exception as e:
                errors += 1
                print(f"[bulk-score] err on {r[1]}: {str(e)[:120]}")
                conn.rollback()

        conn.commit()

    # Final count
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM market_power_scores;")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM market_power_scores WHERE tier_required = 'lite-pro';")
        lite = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM market_power_scores WHERE tier_required != 'lite-pro' OR tier_required IS NULL;")
        full = cur.fetchone()[0]

    conn.close()
    print()
    print(f"[bulk-score] complete:")
    print(f"  scored this run: {scored}")
    print(f"  errors:          {errors}")
    print(f"  total in DB:     {total}")
    print(f"  full scoring:    {full}")
    print(f"  lite scoring:    {lite}")


if __name__ == "__main__":
    main()
