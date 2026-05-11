#!/usr/bin/env python3
"""Bulk DCPI lite-scoring v3 — slug normalization + iso/state population +
   verdict matrix + collision avoidance with curated full-scored slugs."""
import os, sys, re

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr); sys.exit(2)
try: import psycopg2
except ImportError: print("ERROR: psycopg2 missing", file=sys.stderr); sys.exit(2)


US_STATE_ISO = {
    "AL":"SERC","AK":"AK","AR":"MISO","AZ":"WECC","CA":"CAISO","CO":"WECC",
    "CT":"ISONE","DE":"PJM","FL":"FRCC","GA":"SERC","HI":"HECO","IA":"MISO",
    "ID":"WECC","IL":"PJM","IN":"PJM","KS":"SPP","KY":"PJM","LA":"MISO",
    "MA":"ISONE","MD":"PJM","ME":"ISONE","MI":"MISO","MN":"MISO","MO":"SPP",
    "MS":"MISO","MT":"WECC","NC":"SERC","ND":"MISO","NE":"SPP","NH":"ISONE",
    "NJ":"PJM","NM":"WECC","NV":"WECC","NY":"NYISO","OH":"PJM","OK":"SPP",
    "OR":"WECC","PA":"PJM","RI":"ISONE","SC":"SERC","SD":"SPP","TN":"TVA",
    "TX":"ERCOT","UT":"WECC","VA":"PJM","VT":"ISONE","WA":"WECC","WI":"MISO",
    "WV":"PJM","WY":"WECC","DC":"PJM",
}


def normalize_slug(name, state):
    """City name + state → canonical slug like 'st-louis-mo'."""
    s = (name or "").lower().strip()
    s = s.replace("st.", "st").replace("saint ", "st-").replace("st ", "st-")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if state and not s.endswith(f"-{state.lower()}"):
        s = f"{s}-{state.lower()}"
    return s


def compute_verdict(constraint, excess):
    """Phase 229 verdict matrix."""
    c, e = float(constraint or 0), float(excess or 0)
    if c == 0 and e == 0: return "NODATA"
    if e >= 60 and c <= 40: return "BUILD"
    if e >= 50 and c <= 50: return "BUILD"
    if c >= 70 and e <= 40: return "AVOID"
    if c >= 60 and e <= 30: return "AVOID"
    return "CAUTION"


def main():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    print("[bulk-score v3] connecting...")

    with conn.cursor() as cur:
        # Ensure iso/state columns exist
        cur.execute("""
            DO $$ BEGIN
                BEGIN ALTER TABLE market_power_scores ADD COLUMN iso TEXT;
                EXCEPTION WHEN duplicate_column THEN END;
                BEGIN ALTER TABLE market_power_scores ADD COLUMN state TEXT;
                EXCEPTION WHEN duplicate_column THEN END;
            END $$;
        """)
        conn.commit()

        # Get all curated full-scored slug roots so we don't collide
        cur.execute("""
            SELECT DISTINCT market_slug FROM market_power_scores
            WHERE tier_required IS NULL OR tier_required != 'lite-pro';
        """)
        full_slugs = {r[0] for r in cur.fetchall()}
        full_roots = set()
        for slug in full_slugs:
            root = re.sub(r"-[a-z]{2}$", "", slug)  # strip state suffix for root match
            full_roots.add(slug)
            full_roots.add(root)
        print(f"[bulk-score v3] {len(full_slugs)} curated slugs — will skip collisions")

        # Pull candidates
        cur.execute("""
            SELECT city, state,
                   COUNT(*) AS fac,
                   COALESCE(SUM(power_mw), 0) AS op_mw,
                   COALESCE(SUM(power_mw) FILTER (WHERE status IN
                       ('construction','planned','permitting','Under Construction','Planned')), 0) AS pipe_mw
            FROM discovered_facilities
            WHERE city IS NOT NULL AND city != ''
              AND state IS NOT NULL AND LENGTH(state) = 2 AND state ~ '^[A-Z]{2}$'
              AND (country = 'US' OR country = 'USA')
            GROUP BY city, state
            HAVING COUNT(*) >= 3
            ORDER BY fac DESC LIMIT 400;
        """)
        rows = cur.fetchall()
        print(f"[bulk-score v3] {len(rows)} candidate markets")

        # Delete existing lite-pro rows to start clean
        cur.execute("DELETE FROM market_power_scores WHERE tier_required = 'lite-pro';")
        deleted = cur.rowcount
        conn.commit()
        print(f"[bulk-score v3] cleared {deleted} stale lite-pro rows")

        scored = skipped_collision = errors = 0
        for r in rows:
            try:
                name, state, fac, op_mw, pipe_mw = r
                slug = normalize_slug(name, state)
                root = re.sub(r"-[a-z]{2}$", "", slug)

                # Skip if this collides with a curated slug
                if slug in full_roots or root in full_roots:
                    skipped_collision += 1
                    continue

                iso = US_STATE_ISO.get(state, "UNK")

                # $/kWh from state
                cur.execute("""
                    SELECT AVG(price_cents_kwh)/100.0 FROM eia_electricity_rates
                    WHERE state=%s AND sector='ALL' AND retrieved_at > NOW() - INTERVAL '365 days';
                """, (state,))
                kr = cur.fetchone()
                kwh = float(kr[0]) if kr and kr[0] else None

                op_mw_f, pipe_mw_f, fac_n = float(op_mw or 0), float(pipe_mw or 0), int(fac or 0)

                # Constraint
                pipe_ratio = (pipe_mw_f / op_mw_f) if op_mw_f > 0 else 0
                constraint = min(100.0, pipe_ratio * 60)
                if pipe_mw_f > 500: constraint += 10
                if pipe_mw_f > 1000: constraint += 10
                constraint = round(min(100.0, constraint), 1)

                # Excess
                excess = 0.0
                if kwh is not None:
                    excess = max(0.0, min(100.0, (0.30 - kwh) * 333))
                if pipe_mw_f < 100 and op_mw_f > 200:
                    excess = max(excess, 65.0)
                if fac_n >= 30: excess = max(excess, 50.0)
                elif fac_n >= 15: excess = max(excess, 35.0)
                excess = round(excess, 1)

                verdict = compute_verdict(constraint, excess)

                cur.execute("""
                    INSERT INTO market_power_scores
                    (market_slug, market_name, latitude, longitude,
                     constraint_score, excess_power_score,
                     verdict, tier_required, computed_at,
                     iso, state)
                    VALUES (%s, %s, NULL, NULL, %s, %s, %s, 'lite-pro', NOW(), %s, %s)
                    ON CONFLICT (market_slug) DO UPDATE SET
                        market_name = EXCLUDED.market_name,
                        constraint_score = EXCLUDED.constraint_score,
                        excess_power_score = EXCLUDED.excess_power_score,
                        verdict = EXCLUDED.verdict,
                        computed_at = NOW(),
                        iso = EXCLUDED.iso,
                        state = EXCLUDED.state;
                """, (slug, f"{name}, {state}", constraint, excess, verdict, iso, state))

                scored += 1
                if scored % 50 == 0:
                    conn.commit()
                    print(f"[bulk-score v3] {scored} scored, {skipped_collision} collision-skipped...")
            except Exception as e:
                errors += 1
                conn.rollback()
                if errors <= 3:
                    print(f"[bulk-score v3] err {r[0]}: {str(e)[:120]}")

        conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT market_slug) FROM market_power_scores;")
        total = cur.fetchone()[0]
        cur.execute("""
            SELECT verdict, COUNT(*) FROM market_power_scores
            WHERE tier_required = 'lite-pro'
            GROUP BY verdict ORDER BY COUNT(*) DESC;
        """)
        dist = cur.fetchall()
    conn.close()

    print(f"\n[bulk-score v3] complete:")
    print(f"  scored: {scored} · skipped (collision): {skipped_collision} · errors: {errors}")
    print(f"  total unique slugs: {total}")
    print(f"  verdict distribution (lite-pro): {dist}")


if __name__ == "__main__":
    main()
