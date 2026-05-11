#!/usr/bin/env python3
"""Bulk DCPI lite-scoring — INSERT new rows only.
Existing 30 full-scored markets stay. We add lite scores for the other 102.
DCPI API returns latest-per-slug, so this just expands the unique-slug count.

Run: railway run python3 scripts/bulk_dcpi_score.py
"""
import os, sys

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr); sys.exit(2)
try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed", file=sys.stderr); sys.exit(2)


def main():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    print("[bulk-score] connecting to DB...")

    with conn.cursor() as cur:
        # Get existing slugs (so we only insert NEW ones)
        cur.execute("SELECT DISTINCT market_slug FROM market_power_scores;")
        existing_slugs = {r[0] for r in cur.fetchall()}
        print(f"[bulk-score] {len(existing_slugs)} slugs already in DB")

        # Find candidate markets from discovered_facilities (US, ≥3 facilities)
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
            LIMIT 300;
        """)
        rows = cur.fetchall()
        print(f"[bulk-score] {len(rows)} candidate markets from facility data")

        # Filter to NEW slugs only
        new_rows = []
        for r in rows:
            city_l = r[0]
            slug = city_l.replace(" ", "-").replace(",", "").replace("/", "-")
            if slug not in existing_slugs:
                new_rows.append((slug, *r[1:]))
        print(f"[bulk-score] {len(new_rows)} NEW markets to score")

        if not new_rows:
            print("[bulk-score] no new markets — exiting")
            return

        scored = 0
        errors = 0
        for r in new_rows:
            try:
                slug, name, state, fac, op_mw, pipe_mw = r
                # $/kWh
                cur.execute("""
                    SELECT AVG(price_cents_kwh)/100.0 FROM eia_electricity_rates
                    WHERE state=%s AND sector='ALL' AND retrieved_at > NOW() - INTERVAL '365 days';
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
                    excess_score = max(excess_score, 50.0)

                if excess_score > 50 and constraint_score < 60:
                    verdict = "BUILD"
                elif constraint_score > 75:
                    verdict = "AVOID"
                else:
                    verdict = "CAUTION"

                # Plain INSERT — no ON CONFLICT
                cur.execute("""
                    INSERT INTO market_power_scores
                    (market_slug, market_name, constraint_score, excess_power_score, verdict,
                     tier_required, computed_at)
                    VALUES (%s, %s, %s, %s, %s, 'lite-pro', NOW());
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
        cur.execute("SELECT COUNT(DISTINCT market_slug) FROM market_power_scores;")
        total = cur.fetchone()[0]
    conn.close()

    print(f"\n[bulk-score] complete:")
    print(f"  new markets inserted: {scored}")
    print(f"  errors:               {errors}")
    print(f"  total unique slugs:   {total}")


if __name__ == "__main__":
    main()
