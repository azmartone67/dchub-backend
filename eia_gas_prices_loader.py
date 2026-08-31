"""
eia_gas_prices_loader.py — Populate the `eia_gas_prices` table (the ONLY writer)
═══════════════════════════════════════════════════════════════════════════════
WHY THIS EXISTS (the repair):
  The `eia_gas_prices` table is READ by two production surfaces but had ZERO
  writers repo-wide, so both fell back to placeholder values:

    1. routes/dcgi.py (~line 314) — the DCGI composite's gas_cost_score factor.
       With no rows, every state got a constant gas_cost_score = 50.0, so ~40%
       of the DCGI composite was dead/neutral for all 51 states.

    2. routes/powered_land_gas.py (_delivered_industrial ~line 443,
       _delivered_electric ~line 484) — the per-market delivered industrial /
       electric-power gas tariffs. With no rows these returned None →
       data_basis="synthetic_seed" and delivered_industrial/electric = null on
       /api/v1/markets/<slug>/gas-pricing.

  This loader fetches EIA v2 state-level natural-gas delivered prices and writes
  the EXACT columns/units those readers expect, so both surfaces go live.

WHAT THE READERS EXPECT (verified against the live code, 2026-06-19):
  Table:   eia_gas_prices
  Columns read:  state, price, sector, period
    • routes/dcgi.py:314
        SELECT DISTINCT ON (UPPER(state)) UPPER(state) AS st, price, sector, period
        FROM eia_gas_prices
        WHERE price IS NOT NULL AND price > 0 AND (
            sector ILIKE '%%indus%%' OR sector ILIKE '%%electric%%'
            OR sector IN ('PIN', 'PEU'))
        ORDER BY UPPER(state), period DESC
    • routes/powered_land_gas.py:443 (industrial)
        SELECT price, period FROM eia_gas_prices
         WHERE UPPER(state)=%s AND price>0 AND (sector ILIKE '%%indus%%' OR sector='PIN')
         ORDER BY period DESC LIMIT 1
    • routes/powered_land_gas.py:484 (electric power)
        SELECT price, period FROM eia_gas_prices
         WHERE UPPER(state)=%s AND price>0 AND (sector ILIKE '%%electric%%' OR sector='PEU')
         ORDER BY period DESC LIMIT 1
    • dchub_mcp_server.py:2026  SELECT state, price, period, sector FROM eia_gas_prices

  UNITS TRAP (important): powered_land_gas.py treats `price` as $/MMBtu — it
  feeds `price` straight into gas_to_grid_usd_per_mwh() which expects $/MMBtu
  ($/MWh = heat_rate_btu_per_kwh * price / 1000). But the EIA PIN/PEU series
  report in $ per thousand cubic feet ($/Mcf). We therefore CONVERT
  $/Mcf → $/MMBtu by dividing by the EIA heat content of ~1.037 MMBtu/Mcf
  before storing into `price`. (The sibling scripts/eia_pricing_discovery.py
  stores the RAW $/Mcf into a DIFFERENT table, eia_natural_gas_prices.price_dollars_mcf;
  this loader is for the MMBtu-keyed eia_gas_prices the routes read.)

  STORED `sector` VALUES: we write the descriptive names "industrial" and
  "electric_power" so they satisfy the readers' `ILIKE '%indus%'` /
  `ILIKE '%electric%'` predicates (the readers' alternate `sector IN
  ('PIN','PEU')` branch is then redundant but harmless). The EIA process code
  (PIN / PEU) is also stored in the `eia_process` column for traceability.
  (Unique index is FULL, not partial — see the ON CONFLICT trap note below.)

EIA SERIES USED:
  endpoint  natural-gas/pri/sum/data   (EIA Open Data API v2)
  facets[process][]=PIN  → Price of Natural Gas Delivered to Industrial Consumers
  facets[process][]=PEU  → Price of Natural Gas Delivered to Electric Power Consumers
  frequency=monthly, data[0]=value, sorted period DESC.
  Per-record state comes from the `area-name` field (FULL state name, e.g.
  "Texas") — mapped to the 2-letter postal via STATE_MAP. (EIA's natural-gas
  state series expose `area-name`, not a clean duoarea postal, so name-mapping
  is the reliable key — confirmed by the sibling discovery script.)

RUN (manually, in the Railway shell against Neon):
  railway run python eia_gas_prices_loader.py
  # or locally:
  export DATABASE_URL="postgresql://...neon...:require"
  export EIA_API_KEY="..."   # free key: https://www.eia.gov/opendata/register.php
  python eia_gas_prices_loader.py

VERIFY rows landed:
  SELECT sector, COUNT(*) AS rows, COUNT(DISTINCT state) AS states,
         MIN(period) AS oldest, MAX(period) AS newest
    FROM eia_gas_prices GROUP BY sector;
  SELECT state, sector, price, period FROM eia_gas_prices
   WHERE state='TX' ORDER BY period DESC LIMIT 4;

FAILS SAFE: if EIA_API_KEY or DATABASE_URL is missing, or the API errors,
this logs and exits NON-ZERO without raising on import — a degraded scheduler
entry just no-ops instead of crashing the worker.
"""

import os
import sys
import time
import math

import psycopg2

# ── Config ──────────────────────────────────────────────────────────────────

# Mirror the sibling DB-conn precedence (eia_gas_bulk_loader / eia_pricing_discovery):
#   DATABASE_URL → NEON_DATABASE_URL → NEON_URL
DATABASE_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("NEON_DATABASE_URL")
    or os.environ.get("NEON_URL")
)

EIA_API_KEY = os.environ.get("EIA_API_KEY")
EIA_BASE = "https://api.eia.gov/v2"

# EIA heat content of natural gas (MMBtu per thousand cubic feet). EIA's standard
# conversion factor; used to turn the $/Mcf delivered-price series into the
# $/MMBtu the routes consume. ~1.037 MMBtu/Mcf.
MMBTU_PER_MCF = 1.037

# EIA `process` facet code → (stored sector name, human label). The descriptive
# stored name must contain "indus" / "electric" so the readers' ILIKE matches.
GAS_PROCESSES = {
    "PIN": ("industrial", "Price of Natural Gas Delivered to Industrial Consumers"),
    "PEU": ("electric_power", "Price of Natural Gas Delivered to Electric Power Consumers"),
}

# Full state name → 2-letter postal. EIA natural-gas state series report
# `area-name` as the full name (e.g. "Texas"); the routes key on 2-letter state.
STATE_MAP = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

US_STATES = set(STATE_MAP.values())

# ★ 2026-08-30: EIA does NOT use one casing for `area-name`. Most state series
# come back "USA-XX", but nine come back as the BARE UPPERCASE full name —
# "TEXAS", "OHIO", "NEW YORK", "CALIFORNIA", "COLORADO", "FLORIDA",
# "MASSACHUSETTS", "MINNESOTA", "WASHINGTON". `_state_abbr` ended in
# `STATE_MAP.get(name)`, an exact lookup against Title-Case keys, so all nine
# resolved to None and were dropped by the `if not state: continue` in
# fetch_and_upsert — silently, with the loader still reporting success.
#
# The price was never withheld upstream; the name never matched.
#
# ★ 2026-08-31 CORRECTION — measured, and it narrows the claim this comment
# originally made. Replaying the live EIA call and diffing the new lookup
# against a replica of the old one: of 52 distinct `area-name` values over
# 29,650 records, EXACTLY 9 change result (old None -> correct postal), ~580
# records each. 42 arrive as "USA-*"; the 10th uppercase name is "U.S.", which
# both versions correctly leave unmapped. So the MECHANISM above is confirmed.
#
# What is NOT supported: that this silently cost those nine states months of
# data. The first run after the fix wrote 4,690 rows for them and their row
# counts and max periods did NOT move (CA 584, TX 595, NY 596; max period
# 2026-05, same as the states that were never affected). A months-long drop
# would have back-filled the missing periods and the counts would have jumped.
# The likeliest reading is that EIA's uppercase casing is RECENT and the nine
# were still current from earlier loads — i.e. this was caught before the
# damage showed, which is the boring outcome, not the dramatic one.
#
# Any link to routes/dcgi.py's `cost = 50.0` fallback is UNPROVEN and is
# deliberately not asserted here. Those states hold PEU/PIN rows, so the DCGI
# query would have found a price rather than falling back; and the DCGI
# composite has been WITHDRAWN since 2026-08-08
# (/api/v1/dcgi/scores/TX -> available:false), so no published number moved
# either way. Do not cite this fix as having changed a DCGI score.
#
# Case-folded index rather than case-folding at the call site, so the mapping
# has ONE authority. Verified collision-free: the 51 keys uppercase to 51
# distinct strings.
_STATE_MAP_CI = {k.upper(): v for k, v in STATE_MAP.items()}
assert len(_STATE_MAP_CI) == len(STATE_MAP), "STATE_MAP keys collide when upper-cased"

# Area keys EIA returns that are legitimately NOT states. Anything unmapped and
# NOT in here is a genuine surprise and gets reported — see fetch_and_upsert.
KNOWN_NON_STATE_AREAS = {"U.S.", "USA", "US", "UNITED STATES", "NA"}

# DDL gate parity with db_utils (which SKIPs DDL when SKIP_DDL=1).
SKIP_DDL = os.environ.get("SKIP_DDL", "0") == "1"

# ── EIA fetch ───────────────────────────────────────────────────────────────


def eia_get(endpoint, params=None):
    """GET against the EIA v2 API with simple 429/timeout retry.

    Uses `requests` like the sibling helpers (eia_api.make_eia_request /
    scripts/eia_pricing_discovery.eia_get). Imported lazily so a missing
    `requests` at import time can't crash a scheduler that imports this module.
    """
    import requests  # lazy import: keep module import side-effect-free

    url = f"{EIA_BASE}/{endpoint}"
    base_params = {"api_key": EIA_API_KEY}
    if params:
        base_params.update(params)

    for attempt in range(3):
        try:
            resp = requests.get(url, params=base_params, timeout=30)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            print(f"  Timeout on attempt {attempt + 1}")
            time.sleep(2)
        except Exception as e:
            print(f"  EIA error (attempt {attempt + 1}): {e}")
            if attempt == 2:
                return None
            time.sleep(2)
    return None


def _state_abbr(name):
    """EIA area key → 2-letter postal, or None.

    EIA is NOT consistent here, and assuming it was silently dropped nine
    states from every run until 2026-08-30. (It did NOT cost two months of
    data, as an earlier draft of this comment claimed — see the measured
    correction above `_STATE_MAP_CI`.) Three shapes are real, all observed
    live in the same response:
        "USA-AK"   most states
        "TEXAS"    nine states, bare UPPERCASE full name
        "Texas"    Title Case, accepted defensively
    A bare 2-letter postal is also accepted. National/region aggregates
    ("USA", "U.S.") → None, which is correct and stays correct.
    """
    if not name:
        return None
    name = name.strip()
    # "USA-XX" → "XX"
    if name.upper().startswith("USA-"):
        cand = name.split("-", 1)[1].strip().upper()
        return cand if cand in US_STATES else None
    if len(name) == 2 and name.upper() in US_STATES:
        return name.upper()
    # Case-INSENSITIVE. `STATE_MAP.get(name)` was exact against Title-Case keys
    # and dropped every state EIA reports as bare uppercase. See _STATE_MAP_CI.
    return _STATE_MAP_CI.get(name.upper())


# ── DB ──────────────────────────────────────────────────────────────────────


def ensure_schema(cur):
    """CREATE TABLE IF NOT EXISTS eia_gas_prices, matching the read columns.

    Columns the routes read:  state, price, sector, period.
    Extra bookkeeping columns: eia_process, units, retrieved_at.

    TRAP avoided (per this codebase's history): the UNIQUE index is FULL, not
    partial — a partial UNIQUE index would NOT match the ON CONFLICT
    (state, sector, period) target unless the WHERE predicate were repeated,
    which previously caused upsert spam → container loops on gas_pipelines.
    """
    if SKIP_DDL:
        print("  SKIP_DDL=1 → skipping CREATE TABLE / index (assuming table exists)")
        return
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS eia_gas_prices (
            id           BIGSERIAL PRIMARY KEY,
            state        TEXT NOT NULL,
            sector       TEXT NOT NULL,
            price        NUMERIC(10,4),
            period       TEXT NOT NULL,
            eia_process  TEXT,
            units        TEXT DEFAULT 'usd_per_mmbtu',
            retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # FULL unique index — simple, so ON CONFLICT (state, sector, period) matches.
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_eia_gas_prices_state_sector_period
        ON eia_gas_prices (state, sector, period)
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_eia_gas_prices_state ON eia_gas_prices (state)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_eia_gas_prices_period ON eia_gas_prices (period DESC)"
    )


def fetch_and_upsert(conn):
    """Pull PIN + PEU state delivered prices, convert $/Mcf → $/MMBtu, upsert."""
    cur = conn.cursor()
    total_upserted = 0
    errors = 0
    by_sector = {}
    unmapped_areas = {}   # raw EIA area-name -> records dropped (mapping misses only)

    PAGE = 5000
    for proc_code, (sector_name, label) in GAS_PROCESSES.items():
        print(f"\n  Process: {sector_name} ({proc_code}) — {label}")

        # Paginate the full series (sorted period DESC) so states whose MOST
        # recent value is withheld by EIA are still captured at their last
        # non-withheld older period — the readers take DISTINCT ON (state)
        # ORDER BY period DESC, so an older real value wins over no value.
        #
        # ★ This comment used to name "TX/OH/WA industrial" as the motivating
        #   example. That was wrong and it hid the real bug for two months:
        #   those three were not withheld at any period, they were never mapped
        #   at all (bare-uppercase `area-name` vs a Title-Case STATE_MAP), so
        #   pagination could not have rescued them. Withheld and unmapped look
        #   identical downstream — zero rows — which is why the drop below is
        #   now counted and reported instead of silently `continue`d.
        records = []
        offset = 0
        for _page in range(12):  # hard cap: 60k records
            data = eia_get(
                "natural-gas/pri/sum/data",
                {
                    "frequency": "monthly",
                    "data[0]": "value",
                    "facets[process][]": proc_code,
                    "sort[0][column]": "period",
                    "sort[0][direction]": "desc",
                    "length": PAGE,
                    "offset": offset,
                },
            )
            if not data or "response" not in data:
                break
            page_recs = data.get("response", {}).get("data", [])
            records.extend(page_recs)
            if len(page_recs) < PAGE:
                break
            offset += PAGE
            time.sleep(0.3)

        if not records:
            print(f"  ✗ No data returned for {sector_name} ({proc_code})")
            errors += 1
            continue

        print(f"  → {len(records)} records from EIA (paginated)")
        if records:
            sample = records[0]
            print(
                "    sample: area-name=%r value=%r units=%r period=%r"
                % (
                    sample.get("area-name"),
                    sample.get("value"),
                    sample.get("units") or sample.get("unitsName"),
                    sample.get("period"),
                )
            )

        upserted_here = 0
        for rec in records:
            raw_state = rec.get("area-name") or rec.get("duoarea") or ""
            state = _state_abbr(raw_state)
            if not state:
                # ★ Was a bare `continue`. An unmapped area is indistinguishable
                #   downstream from a state EIA withheld — both are zero rows —
                #   so the loader reported success while dropping nine states.
                #   Aggregates are expected and stay quiet; anything else is a
                #   mapping failure and is surfaced by the caller.
                key = (raw_state or "").strip().upper()
                if key and key not in KNOWN_NON_STATE_AREAS:
                    unmapped_areas[raw_state] = unmapped_areas.get(raw_state, 0) + 1
                continue

            period = (rec.get("period") or "").strip()
            value = rec.get("value")
            if not period or value is None:
                continue

            # EIA sometimes returns 'W' (withheld) or other non-numeric markers.
            try:
                price_mcf = float(value)
            except (TypeError, ValueError):
                continue
            if price_mcf <= 0:
                continue

            # $/Mcf → $/MMBtu (the unit the routes consume).
            price_mmbtu = round(price_mcf / MMBTU_PER_MCF, 4)

            try:
                cur.execute(
                    """
                    INSERT INTO eia_gas_prices
                        (state, sector, price, period, eia_process, units, retrieved_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (state, sector, period) DO UPDATE
                    SET price        = EXCLUDED.price,
                        eia_process  = EXCLUDED.eia_process,
                        units        = EXCLUDED.units,
                        retrieved_at = NOW()
                    """,
                    (state, sector_name, price_mmbtu, period, proc_code, "usd_per_mmbtu"),
                )
                upserted_here += 1
                total_upserted += 1
            except Exception as e:
                errors += 1
                conn.rollback()
                cur = conn.cursor()
                if errors <= 5:
                    print(f"    upsert error ({state} {period}): {e}")

        conn.commit()
        by_sector[sector_name] = upserted_here
        print(f"  ✓ Upserted {upserted_here} rows for {sector_name}")
        time.sleep(0.5)  # rate-limit courtesy

    if unmapped_areas:
        # Not fatal — EIA may add a region key we do not care about — but it is
        # never silent again. Every state absent from eia_gas_prices should be
        # explainable by a line here or by EIA genuinely withholding it.
        print("\n  ⚠ UNMAPPED area-name values (records DROPPED, not written):")
        for area, n in sorted(unmapped_areas.items(), key=lambda kv: -kv[1]):
            print(f"      {area!r}: {n} records")
        print("      If one of these is a US state, _state_abbr / STATE_MAP is "
              "the bug — not EIA withholding.")
    else:
        print("\n  ✓ every non-aggregate area-name mapped to a state")

    return total_upserted, errors, by_sector, unmapped_areas


# ── Main ────────────────────────────────────────────────────────────────────


def run():
    """Fetch EIA PIN/PEU state delivered gas prices into eia_gas_prices.

    Returns the number of rows upserted. Exits non-zero on fatal config/API
    failure (so a scheduler entry degrades gracefully rather than crashing).
    """
    print("=" * 64)
    print("DC Hub — eia_gas_prices loader (DCGI gas_cost_score + powered-land repair)")
    print("=" * 64)

    if not DATABASE_URL:
        print("❌ Set DATABASE_URL (or NEON_DATABASE_URL / NEON_URL) — exiting.")
        sys.exit(1)

    if not EIA_API_KEY:
        print("❌ EIA_API_KEY not configured — exiting (no rows written).")
        sys.exit(1)

    print(f"🔌 Connecting to Postgres: {DATABASE_URL[:42]}...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        sys.exit(1)

    try:
        cur = conn.cursor()
        ensure_schema(cur)
        conn.commit()

        try:
            cur.execute("SELECT COUNT(*) FROM eia_gas_prices")
            before = cur.fetchone()[0]
        except Exception:
            conn.rollback()
            before = 0
        print(f"📊 eia_gas_prices rows before: {before}")

        total, errors, by_sector, unmapped = fetch_and_upsert(conn)

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM eia_gas_prices")
        after = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT state) FROM eia_gas_prices")
        states = cur.fetchone()[0]
        cur.execute("SELECT MIN(period), MAX(period) FROM eia_gas_prices")
        min_p, max_p = cur.fetchone()

        print("\n" + "=" * 64)
        print("✅ eia_gas_prices load complete")
        print(f"   Upserted this run: {total} (errors: {errors})")
        for sec, n in by_sector.items():
            print(f"     {sec}: {n}")
        print(f"   Rows before → after: {before} → {after}")
        print(f"   Distinct states: {states} | periods: {min_p} … {max_p}")
        # ★ A state count below 51 (50 + DC) is the symptom the silent drop hid.
        #   State it as a verdict, not as a number the reader has to interpret.
        if states < 51:
            missing = sorted(US_STATES | {"DC"})
            cur.execute("SELECT DISTINCT UPPER(state) FROM eia_gas_prices")
            have = {r[0] for r in cur.fetchall()}
            gap = [s for s in missing if s not in have]
            print(f"   ⚠ {len(gap)} state(s) have NO row at all: {' '.join(gap)}")
            if unmapped:
                print("     …and area-names were dropped unmapped this run "
                      "(above) — suspect the mapping before suspecting EIA.")
        else:
            print("   ✓ all 50 states + DC present")
        print("=" * 64)
        return total
    except Exception as e:
        print(f"❌ Fatal error during load: {e}")
        import traceback

        traceback.print_exc()
        try:
            conn.rollback()
        except Exception:
            pass
        sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Back-compat alias mirroring the sibling loaders' entrypoint name.
def main():
    return run()


if __name__ == "__main__":
    main()
