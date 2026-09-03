"""dominant_source.py — how far a table's LARGEST source lags its newest row.

★★★ MAX(ts) OVER A MULTI-SOURCE TABLE ANSWERS THE WEAKEST QUESTION THERE IS.
Extracted 2026-09-03 from routes/data_freshness_radar.py (#3655) because a
SECOND production monitoring surface — routes/infra_growth.py, the growth board
— computes freshness the same bare way and is masked by the same lanes. One
implementation, two consumers: a copy is how the two boards would drift into
disagreeing about which lane is dominant.

Measured on the growth board's own 16 layers, 2026-09-03, using each layer's
OWN declared freshness column (not the radar's):

    layer                 rows     board_age  threshold   dominant source        lag
    substations         127,271          0d        10d    HIFLD (79,788)         19d   MASKED
    metro_fiber_routes   64,836          7d        75d    zayo (19,241)          68d   boundary
    gas_pipelines        33,771          3d       130d    eia_geodot (32,851)     0d
    transmission_lines   95,566          2d       120d    eia-arcgis (94,619)     0d
    data_centers         27,935          0d        14d    openstreetmap (7,924)   0d

Only substations is masked today, and the mask stays SILENT on the four healthy
layers — a lag of 0 says the dominant source IS the freshness signal. That is
the property that keeps this from manufacturing reds for slow federal data.

The measure is RELATIVE — dominant source against THIS TABLE'S own newest row,
never against a clock — so it needs no cadence model and cannot fire on a table
whose sources are all equally quiet.
"""
import re

# Defense-in-depth on identifiers interpolated into SQL below. Table and column
# names reach this helper from a hand-maintained registry, never from a request,
# but the f-strings are real and this is the only thing standing in front of them.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# ★★★ MASK THRESHOLD. Two boards measured it independently, on DIFFERENT
# columns, and both found the same empty middle:
#
#   data_freshness_radar (updated_at): nine unmasked tables all measured a lag
#     of exactly 0 days; the two masked ones measured 19 and 74.
#   infra_growth (each layer's own _FRESH_COL, mostly created_at): the four
#     multi-source layers that are healthy all measured 0; substations measured
#     19 and metro_fiber_routes 68.
#
# Nothing real lands between 0 and 19 on either board, so 7 fires on the failure
# and not on the noise. Deliberately NOT env-overridable — land_power_crawler.py
# already records why: "a guard with an env escape hatch is a guard that gets
# flipped at 3am to make a red dashboard go green."
MASK_LAG_DAYS = 7


def tables_with_a_source_column(cur, tables):
    """The subset of `tables` that actually has a `source` column, in ONE query.

    A caller measuring many tables per request should use this and pass the
    result to `dominant_source_lag(..., has_source=...)`. Returns None if the
    probe itself fails, which callers must treat as "unknown" and fall back to
    per-table probing — never as "none of them have one", which would silently
    disable the whole check.
    """
    names = [t for t in (tables or []) if t and _IDENT_RE.match(t)]
    if not names:
        return set()
    try:
        cur.execute("""SELECT table_name FROM information_schema.columns
                        WHERE table_schema='public' AND column_name='source'
                          AND table_name = ANY(%s)""", (names,))
        return {r[0] for r in (cur.fetchall() or [])}
    except Exception:
        return None


def dominant_source_lag(cur, table, ts_col, has_source=None):
    """How far the table's LARGEST source lags the table's own newest row.

    ★★★ MAX(ts) OVER A MULTI-SOURCE TABLE ANSWERS THE WEAKEST QUESTION THERE IS.
    It reports "is ANY lane alive", so it is pinned green by the most trivial
    writer on the table and says nothing about the lane that supplies the rows.
    Measured against production 2026-09-03:

      · substations   127,271 rows. The canonical HIFLD lane is 63% of them and
        last wrote 2026-08-14 (it fetches 75,328 and upserts 0, 500ing nightly).
        `auto_discovery` — 708 rows, 0.56% of the table — writes 1-8 rows EVERY
        DAY and has not missed one in 21 days, so MAX(updated_at) is always a
        few hours old. Neither registry's threshold could ever fire: not
        infra_growth's 10 days, not this module's 60.

      · fiber_routes   64,836 rows. Every real carrier source last moved
        2026-06-20 — 74 days. The 20 hardcoded routes in jobs_api.MAJOR_ROUTES
        are re-upserted daily, and this radar's published last_record_at was
        byte-identical to that write: 2026-09-03T01:20:50.603151.

    Both lanes were already known to be broken. Both read `fresh`. That is the
    same failure this module exists to catch, happening to this module.

    ★ THE MEASURE IS RELATIVE, WHICH IS WHY IT NEEDS NO CADENCE MODEL. It
    compares the dominant source against THIS TABLE'S OWN newest row, never
    against a clock. A genuinely quiet table whose sources are all equally quiet
    has a lag of 0 and is left entirely to the existing SLA — so this cannot
    manufacture a red for slow federal data. A large lag means one specific
    thing: something other than the main source is carrying the freshness
    signal.

    `has_source` lets a caller that already knows skip the per-table
    information_schema probe. Measured on the growth board's 16 layers: sixteen
    separate probes cost 1,691 ms, one batched probe 79 ms — and that endpoint
    sits behind a 15s edge timeout, so the difference is not cosmetic. Left as
    None (the default) the helper probes for itself exactly as before.

    Returns (lag_days, dominant_source, dominant_rows). A table with no `source`
    column returns (None, None, None) — not a failure, just a table this check
    has nothing to say about. Never raises: it runs inside the scan and must not
    be able to break it.
    """
    if not table or not ts_col:
        return None, None, None
    if not _IDENT_RE.match(table) or not _IDENT_RE.match(ts_col):
        return None, None, None
    if has_source is False:
        return None, None, None
    if has_source is None:
        try:
            cur.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s
                     AND column_name='source' LIMIT 1""", (table,))
            if not cur.fetchone():
                return None, None, None
        except Exception:
            return None, None, None

    try:
        cur.execute(
            f"""SELECT COALESCE(NULLIF(source,''),'(blank)') AS s,
                       COUNT(*) AS n,
                       MAX({ts_col}::timestamptz) AS mx
                FROM {table} GROUP BY 1""")
        rows = [r for r in (cur.fetchall() or []) if r and r[2] is not None]
    except Exception:
        return None, None, None

    # One source cannot mask itself, and with nothing to compare against the
    # existing SLA is already the whole truth.
    if len(rows) < 2:
        return None, None, None

    global_max = max(r[2] for r in rows)
    dom = max(rows, key=lambda r: r[1])
    dom_src, dom_rows, dom_max = dom[0], int(dom[1]), dom[2]
    lag_days = int((global_max - dom_max).total_seconds() // 86400)
    return lag_days, dom_src, dom_rows

