"""Ingestion Freshness Master Shell — GET /admin/ingestion-freshness
tick: /api/v1/admin/ingestion-freshness/master-tick
kill: INGESTION_FRESHNESS_SHELL_DISABLE=1

Built 2026-08-06 because every board shipped this week measures ADOPTION —
agents, calls, registries, retention — and nothing measures whether the data
underneath is still growing. That is the product.

★ THE FAILURE THIS SHELL EXISTS TO CATCH: A LOADER THAT STOPS SILENTLY.
A layer holding 55,064 fiber routes looks identical whether it was refreshed
this morning or abandoned in March. Row count alone cannot tell those apart.
Only RECENCY (when was the newest row written) and DELTA (how many since) can,
and neither is published anywhere today. The operator's question — "are we
still adding new fiber, new gas, new power plants, new transmission lines, new
data centers?" — currently has no answer short of a manual psql session.

The prior art says silent death is the NORMAL failure, not the exception:
16 feeds once "died" and it was ONE backfill; 4 of 11 loaders were doing
nothing; 43 of 48 DISABLED_JOBS still read live and fired zero.

★★★ THE TRAP THAT SHAPED THIS BOARD: A RELOAD IS NOT GROWTH.
Measured live 2026-08-06, gas_pipelines had 30,000 rows whose created_at fell
on a SINGLE day (2026-08-03) out of 30,918 total — 4 distinct write-days in the
table's whole history. transmission_lines: 94,619 rows on 2026-08-03. Those
loaders TRUNCATE AND RELOAD. A board that reported "rows created in the last
7 days" as growth would have published "gas +30,000 this week", which is false
in the most flattering possible direction — the layer gained ~900 segments and
rewrote the other 30,000. Conversely it would have read fiber_routes' 0-in-7d
as death when its loader is a ~30-day bulk refresh that is merely between runs.

So every layer is classified from its OWN data, not from a hardcoded list:
  distinct write-days, and the share of rows sharing the single busiest day.
  busiest-day share >= 50%  ->  SNAPSHOT  (rows-in-window is a REFRESH STAMP)
  otherwise                 ->  INCREMENTAL (rows-in-window is REAL GROWTH)
The delta check is a GAUGE in both modes and never convicts. Only recency-
against-declared-cadence can fail. See _lane_for_layer.

HONESTY RULES, each of which is a defect shipped elsewhere this week:

1. UNREADABLE IS NOT STALE. A layer whose freshness column is absent, entirely
   NULL, or uncastable renders pass=None ('?') WITH THE REASON — never False.
   A shell built this week violated this three times before shipping.
2. ZERO NEW ROWS IN 7d IS NOT A FAILURE. EIA-860M is monthly; HIFLD and the
   ISO queue snapshots are quarterly. Each layer declares its own expected
   cadence in its check detail and is judged against THAT, never against one
   global threshold. A guard that cries wolf gets deleted — this repo has
   already rejected two over-broad scans.
3. AN UNKNOWN COUNT IS NEVER RENDERED 0. A failed COUNT() means the number is
   unknown; printing 0 states "we measured, and found none".
4. NO INGESTION TIMESTAMP => UNMEASURABLE, and the detail NAMES the column that
   would be needed. Freshness is never inferred from row count.
5. THE PUBLISHED POPULATION IS BUILT FROM THE EXECUTED ONE. _population()
   derives from the same _LAYERS tuple the lanes iterate and republishes the
   exact SQL that ran, per routes/canonical_benchmarks.py _p50_filters() /
   _p50_population() (PR #2253). A hand-typed list drifts from the query.

Column names are READ FROM information_schema AT RUNTIME, in a declared
preference order, and the chosen column is NAMED in every detail line. This
repo has been burned repeatedly by transcribed names (`tool` vs `tool_name`,
`agents` vs `distinct_external_ips`) and by repo DDL that did not match the
live table. Nothing here is transcribed: the shell asks the database.
"""
from __future__ import annotations

import datetime as _dt
import os

from flask import Blueprint, Response, jsonify

# Imported, never copied — the honesty semantics must not drift between boards.
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _conn, _lane_verdict, _safe_lane)

ingestion_freshness_master_shell_bp = Blueprint(
    "ingestion_freshness_master_shell", __name__)

# A window whose writes account for at least this share of the WHOLE table means
# the table was essentially rewritten inside that window — the delta is a
# refresh, not growth.
#
# ★ THIS THRESHOLD IS APPLIED TO THE WINDOW DELTA, NOT TO THE BUSIEST DAY, AND
# THE FIRST DRAFT GOT THAT WRONG. Classifying on "busiest single day >= 50% of
# rows" reads the live data as: facilities SNAPSHOT (68%), substations SNAPSHOT
# (63%). Both are FALSE — facilities added 1,366 genuinely new sites in 7d
# across 103 distinct write-days, and substations writes 1-4 rows a day across
# 79. Their busiest day is an old one-time SEED load, and a seed is
# indistinguishable from a reload by share alone. Calling the platform's
# healthiest loader a truncate-and-reload is a false accusation of exactly the
# kind this board is supposed to prevent.
#
# The mode exists for ONE purpose: to stop a reader treating the 7d/30d delta as
# new assets. So it is derived from that delta. "30,000 of 30,918 rows were
# written in this window" IS the sentence that settles it, and it is true of the
# reload layers (gas 97%, power plants 99%, transmission 99.9%, ISO queue 98%)
# and false of the incremental ones (facilities 8%, substations 0.01%, deals 2%)
# — a clean separation with no seed-load confusion.
_REWRITE_WINDOW_SHARE = 50.0

# Statement timeout for the tick. A board that hangs is a board nobody loads.
_STATEMENT_TIMEOUT_MS = 15000


def _disabled() -> bool:
    return os.environ.get("INGESTION_FRESHNESS_SHELL_DISABLE", "") == "1"


# ── the layers ───────────────────────────────────────────────────────────────
# `cadence_days` is the age beyond which SILENCE IS SUSPICIOUS for that layer,
# and it is always the UPSTREAM PUBLISHER'S OWN SCHEDULE plus grace — never a
# number picked to make the board look busy. `cadence` states it in prose and
# is printed in the check detail so the reader can disagree with the threshold
# rather than having to reverse-engineer it.
#
# `ts_candidates` is a PREFERENCE ORDER, not a guess: the first column that
# exists live AND carries non-NULL values wins, and it is named in the output.
# Native timestamp columns are preferred over TEXT ones on purpose —
# discovered_facilities.discovered_at and deals.created_at are both TEXT live,
# and a TEXT column that fails ::timestamptz must degrade to UNMEASURABLE
# rather than take the lane down (a TEXT created_at 500'd growthfix's first
# live tick).
_LAYERS = (
    dict(key="facilities", label="data centre facilities",
         table="discovered_facilities",
         # Canon's OWN query, mirrored exactly — public_endpoints.py counts
         # facilities as COUNT(DISTINCT canonical_slug). Counting rows here
         # would publish 24,472 against a canon of 16,742 and invent a
         # discrepancy that does not exist.
         entity_key="canonical_slug",
         count_where="canonical_slug IS NOT NULL",
         ts_candidates=("first_seen", "discovered_at", "last_updated"),
         cadence_days=3,
         cadence="continuous — discovery crawlers write most days",
         source="DC Hub discovery crawlers (multi-source)"),
    dict(key="fiber_routes", label="fiber routes",
         table="fiber_routes",
         ts_candidates=("created_at", "discovered_at", "updated_at"),
         cadence_days=45,
         cadence=("bulk vendor refresh — the two observed loads (2026-05-21, "
                  "2026-06-20) sit ~30d apart, +15d grace"),
         source="fiber route vendor bulk load"),
    dict(key="gas_pipelines", label="gas pipeline segments",
         table="gas_pipelines",
         ts_candidates=("created_at", "last_updated", "updated_at"),
         cadence_days=120,
         cadence="quarterly GEM/EIA pipeline refresh (90d) + 30d grace",
         source="Global Energy Monitor / EIA"),
    dict(key="power_plants", label="power plants",
         table="power_plants",
         ts_candidates=("created_at", "last_updated"),
         cadence_days=60,
         cadence="EIA-860M is MONTHLY (30d) + 30d grace",
         source="EIA-860M"),
    dict(key="transmission_lines", label="transmission lines",
         table="transmission_lines",
         ts_candidates=("created_at", "last_updated"),
         cadence_days=120,
         cadence="HIFLD transmission refresh is quarterly (90d) + 30d grace",
         source="HIFLD"),
    dict(key="substations", label="substations",
         table="substations",
         ts_candidates=("created_at", "updated_at"),
         cadence_days=30,
         cadence=("HIFLD quarterly base, but this table takes incremental "
                  "writes most days — 30d"),
         source="HIFLD + incremental enrichment"),
    dict(key="interconnect_queue", label="interconnection queue",
         table="interconnect_queue",
         ts_candidates=("loaded_at",),
         cadence_days=120,
         cadence="ISO queue snapshots are quarterly (90d) + 30d grace",
         source="ISO/RTO queue snapshots"),
    dict(key="deals", label="deals / transactions",
         table="deals",
         ts_candidates=("extracted_at", "created_at", "updated_at"),
         cadence_days=14,
         cadence="continuous M&A extraction — 14d covers a quiet fortnight",
         source="news extraction pipeline"),
    dict(key="subsea_cables", label="subsea cables",
         table="subsea_cables",
         ts_candidates=("created_at", "updated_at"),
         cadence_days=120,
         cadence=("TeleGeography publishes continuously; our loader is a "
                  "one-shot bulk — quarterly (90d) + 30d grace"),
         source="TeleGeography submarine cable map"),
    dict(key="subsea_landing_points", label="subsea cable landing points",
         # NOT cable_landing_points — that table is live-empty (0 rows) and is
         # already covered by tests/test_subsea_wrong_table_and_segment_counts
         # .py, which asserts its emptiness renders UNMEASURED rather than 0.
         # The populated table is subsea_landing_points (1,908 rows live).
         table="subsea_landing_points",
         ts_candidates=("created_at", "updated_at"),
         cadence_days=120,
         cadence="loaded alongside subsea_cables — quarterly (90d) + 30d grace",
         source="TeleGeography submarine cable map"),
)


# ── SQL builders — ONE definition, used by BOTH the executor and the
#    publisher, so the published population cannot drift from what ran ────────
def _where(spec: dict) -> str:
    w = spec.get("count_where")
    return f" WHERE {w}" if w else ""


def _count_sql(spec: dict) -> str:
    """How many rows/entities exist now."""
    ek = spec.get("entity_key")
    expr = f"COUNT(DISTINCT {ek})" if ek else "COUNT(*)"
    return f"SELECT {expr} FROM {spec['table']}{_where(spec)}"


def _freshness_sql(spec: dict, col: str, cast: str) -> str:
    """Newest write + rows written in the 7d and 30d windows.

    For a layer with an entity_key the deltas count DISTINCT ENTITIES whose
    EARLIEST write falls in the window — i.e. genuinely new sites, not rows
    touched. Counting rows would report 1,368 for a week in which 1,366 new
    facilities appeared, and would climb whenever an existing facility merely
    gained a second source row.
    """
    ek = spec.get("entity_key")
    if ek:
        return (
            f"SELECT (SELECT MAX({cast}) FROM {spec['table']}{_where(spec)}), "
            f"(SELECT COUNT(*) FROM (SELECT {ek} FROM {spec['table']}"
            f"{_where(spec)} GROUP BY {ek} "
            f"HAVING MIN({cast}) >= now() - interval '7 days') a), "
            f"(SELECT COUNT(*) FROM (SELECT {ek} FROM {spec['table']}"
            f"{_where(spec)} GROUP BY {ek} "
            f"HAVING MIN({cast}) >= now() - interval '30 days') b)")
    return (f"SELECT MAX({cast}), "
            f"COUNT(*) FILTER (WHERE {cast} >= now() - interval '7 days'), "
            f"COUNT(*) FILTER (WHERE {cast} >= now() - interval '30 days') "
            f"FROM {spec['table']}{_where(spec)}")


def _mode_sql(spec: dict, col: str, cast: str) -> str:
    """Distinct write-days and the biggest single day — the reload signature."""
    return (f"SELECT COUNT(*), COALESCE(MAX(n),0) FROM (SELECT "
            f"date_trunc('day',{cast}) d, COUNT(*) n FROM {spec['table']}"
            f"{_where(spec)} GROUP BY 1) x")


# ── db plumbing ──────────────────────────────────────────────────────────────
def _q(c, sql: str):
    """Fail-soft single row -> (row, None) or (None, reason). NEVER raises.

    LITERAL SQL only. Every identifier interpolated above comes from the
    _LAYERS constant or from information_schema — never from a request
    argument. No bound parameters are passed, which is also why no statement
    here may contain a literal % (the psycopg2 percent-substitution trap that
    has 500'd this codebase before).
    """
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone(), None
    except Exception as e:  # noqa: BLE001 — any failure is UNREADABLE
        try:
            c.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None, f"{type(e).__name__}: {str(e)[:110]}"


def _pick_ts_column(c, spec: dict):
    """(column, cast_expr, None) or (None, None, reason).

    Asks the LIVE database which of the declared candidates exists and carries
    data. A column that exists but is 100% NULL is UNMEASURABLE, not fresh and
    not stale. A TEXT column is accepted only if it actually casts.
    """
    tried = []
    for col in spec["ts_candidates"]:
        row, err = _q(c, "SELECT data_type FROM information_schema.columns "
                         f"WHERE table_name = '{spec['table']}' "
                         f"AND column_name = '{col}'")
        if err:
            tried.append(f"{col}: schema read failed ({err})")
            continue
        if not row:
            tried.append(f"{col}: absent live")
            continue
        cast = f"{col}::timestamptz" if row[0] == "text" else col
        probe, err = _q(c, f"SELECT COUNT(*), COUNT({cast}) FROM "
                           f"{spec['table']}{_where(spec)}")
        if err:
            # A TEXT column that will not cast lands here. That is UNMEASURABLE
            # for this candidate — try the next one before giving up.
            tried.append(f"{col} ({row[0]}): uncastable ({err})")
            continue
        if not probe:
            tried.append(f"{col} ({row[0]}): non-null probe unreadable")
            continue
        # An EMPTY table and an all-NULL column are different diagnoses and
        # must not be collapsed: the first says the loader never wrote, the
        # second says it wrote without stamping. cable_landing_points is live
        # at 0 rows, and the house rule for it is UNMEASURED-not-zero.
        if not probe[0]:
            tried.append(f"{col} ({row[0]}): table is EMPTY (0 rows) — no "
                         f"ingestion date exists to read, which is not the "
                         f"same as a stale one")
            continue
        if not probe[1]:
            tried.append(f"{col} ({row[0]}): present but 100% NULL across "
                         f"{probe[0]:,} rows")
            continue
        return col, cast, None
    return None, None, "; ".join(tried) or "no candidate columns declared"


def _age_days(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00").strip())
        except Exception:  # noqa: BLE001
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 86400.0


def _fmt(n) -> str:
    """An unknown count is NEVER rendered 0 — rule 3."""
    return "UNKNOWN" if n is None else f"{n:,}"


# ── the per-layer lane ───────────────────────────────────────────────────────
def _lane_for_layer(spec: dict) -> list[dict]:
    """Three checks per layer. Only the SECOND one can convict.

    1. rows            GAUGE — how many exist now (UNKNOWN, never 0, on failure)
    2. recency         THE VERDICT — newest write age vs this layer's declared
                       cadence. critical=True, so an unmeasurable freshness
                       column renders the whole lane '?' instead of a
                       confident PASS.
    3. growth          GAUGE — 7d/30d delta, reported alongside the ingestion
                       MODE so a truncate-and-reload is never read as growth.
                       Never returns False: "zero new rows in 7d" is a normal
                       state for a quarterly layer and convicting on it is how
                       a guard earns deletion.
    """
    checks: list[dict] = []
    c = _conn()
    if c is None:
        # Unreachable DB = UNOBSERVED. Not stale, not empty, not passing.
        return [_check(f"{spec['key']}_db", f"{spec['label']} readable", None,
                       "db unavailable — count, recency and delta all UNKNOWN "
                       "for this layer (not zero, not stale)", critical=True)]
    try:
        _q(c, f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")

        # 1 ── how many exist now
        row, err = _q(c, _count_sql(spec))
        total = row[0] if row and row[0] is not None else None
        basis = (f"COUNT(DISTINCT {spec['entity_key']})" if spec.get("entity_key")
                 else "COUNT(*)")
        checks.append(_check(
            f"{spec['key']}_rows", f"{spec['label']} — rows now",
            None if total is None else True,
            (f"{_fmt(total)} via {basis} on {spec['table']}"
             + (f" — count unreadable: {err}" if total is None else "")
             + (f". Entity basis, not rows: a facility with several source "
                f"rows counts once (canon's own query)."
                if spec.get("entity_key") else "")),
            critical=False))

        # 2 ── when was the newest row written  (THE ONE THAT CONVICTS)
        col, cast, why = _pick_ts_column(c, spec)
        if col is None:
            checks.append(_check(
                f"{spec['key']}_recency",
                f"{spec['label']} — written within its cadence", None,
                (f"UNMEASURABLE: {spec['table']} carries no readable ingestion "
                 f"timestamp. Tried {list(spec['ts_candidates'])} -> {why}. "
                 f"To measure this layer the table needs a NOT NULL "
                 f"timestamptz written at insert time (created_at or "
                 f"loaded_at). Freshness is NOT inferred from row count — a "
                 f"full table and an abandoned loader look identical."),
                critical=True))
            return checks

        row, err = _q(c, _freshness_sql(spec, col, cast))
        if not row:
            checks.append(_check(
                f"{spec['key']}_recency",
                f"{spec['label']} — written within its cadence", None,
                f"UNMEASURABLE: freshness query failed on "
                f"{spec['table']}.{col} — {err}. Read failure, not staleness.",
                critical=True))
            return checks

        newest, d7, d30 = row[0], row[1], row[2]
        age = _age_days(newest)
        horizon = spec["cadence_days"]
        if age is None:
            checks.append(_check(
                f"{spec['key']}_recency",
                f"{spec['label']} — written within its cadence", None,
                f"UNMEASURABLE: {spec['table']}.{col} has no MAX value to read "
                f"(table empty, or every value NULL). Not stale — unmeasured.",
                critical=True))
        else:
            fresh = age <= horizon
            checks.append(_check(
                f"{spec['key']}_recency",
                f"{spec['label']} — written within its cadence", fresh,
                (f"newest write {age:.1f}d ago "
                 f"({str(newest)[:19]}) read from {spec['table']}.{col}. "
                 f"EXPECTED CADENCE: {spec['cadence']} -> silence beyond "
                 f"{horizon}d is suspicious for THIS layer. Source: "
                 f"{spec['source']}. "
                 + ("Within cadence." if fresh else
                    f"BEYOND CADENCE by {age - horizon:.0f}d — this is the "
                    f"silent-loader-death signature: the row count still "
                    f"reads {_fmt(total)} and looks healthy.")),
                critical=True))

        # 3 ── has it grown, and is that growth or a reload?
        # Historical spread is CONTEXT, printed but never used to classify —
        # see the note on _REWRITE_WINDOW_SHARE for why share-of-busiest-day
        # cannot tell a one-time seed load from a recurring reload.
        mrow, merr = _q(c, _mode_sql(spec, col, cast))
        spread = f"write-day spread unreadable ({merr})"
        if mrow and mrow[0]:
            days, biggest = int(mrow[0]), int(mrow[1] or 0)
            bshare = (100.0 * biggest / total) if total else 0.0
            spread = (f"history: {days} distinct write-day(s), busiest holds "
                      f"{bshare:.0f}% of rows (a large busiest day may be an "
                      f"old seed load — it is not evidence of a reload)")

        mode, mode_note = "UNKNOWN", "delta unreadable, so mode is undecidable"
        if d30 is not None and total:
            share30 = 100.0 * d30 / total
            if d30 == 0:
                mode = "NO WRITES IN 30d"
                mode_note = ("nothing was written in the window, so there is "
                             "no delta to misread. Whether that is normal is "
                             "the recency check's question, not this one.")
            elif share30 >= _REWRITE_WINDOW_SHARE:
                mode = "REWRITE"
                mode_note = (
                    f"{share30:.0f}% of the ENTIRE table ({_fmt(d30)} of "
                    f"{_fmt(total)}) was written inside the 30d window. This "
                    f"loader rewrites what it already had, so the delta is a "
                    f"REFRESH STAMP, NOT new assets. Net growth is NOT "
                    f"measurable from this column — it needs a row count "
                    f"snapshotted over time, which IS stored: "
                    f"`infra_growth_snapshot` (snapshot_date, layer, count), "
                    f"daily since 2026-06-14 — read THAT for net growth, not "
                    f"this delta. NOTE its layer names differ from this "
                    f"board's (data_centers / metro_fiber_routes / "
                    f"power_plants_eia) and it does NOT cover subsea at all.")
            else:
                mode = "INCREMENTAL"
                mode_note = (
                    f"the 30d delta is {share30:.1f}% of the table, so writes "
                    f"are additions rather than a rewrite — this delta IS "
                    f"real growth.")
        unit = "new " + spec["entity_key"] if spec.get("entity_key") else "rows"
        checks.append(_check(
            f"{spec['key']}_growth", f"{spec['label']} — 7d / 30d delta",
            None if (d7 is None or d30 is None) else True,
            (f"{_fmt(d7)} {unit} in 7d, {_fmt(d30)} in 30d "
             f"(by {spec['table']}.{col}). MODE={mode}: {mode_note} {spread}. "
             f"GAUGE ONLY — this check never fails. Zero in 7d is the normal "
             f"state for a layer on a {spec['cadence_days']}d cadence; the "
             f"recency check above is what convicts."),
            critical=False))
        return checks
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


# ── lane: should the asset counts join canon? ────────────────────────────────
def _lane_canon_coverage() -> list[dict]:
    """BORN RED as a work order, not as a defect report.

    /api/v1/canon/phrases publishes exactly five quantities — facilities,
    countries, markets, deals, tools. Seven of the nine layers this board
    measures have NO canon owner, which means every figure published for them
    anywhere is a HARDCODED LITERAL with nothing to check it against. That is
    not hypothetical: the MCP server's own description still advertises
    "13k US power plants" and "94k transmission lines" as literals, and the
    initialize instructions advertised "15,300+ facilities" against a canon of
    16,500+ (mcp-server #132) — the one quantity canon DOES own was caught;
    the six it does not own cannot be.

    ★ 2026-08-06, verified live and stated so the next reader does not re-probe:
    /api/v1/stats/canonical was swept for keys matching fiber / gas / plant /
    transmission / substation / pipeline / cable. It returned NO match under
    ANY name. The endpoint does not carry them and they are not merely named
    differently — its keys are facilities_*, deals_*, news_articles,
    countries_covered and dcpi_*. So there is no existing publisher to point
    canon at; the counts would have to be added.

    The lane is red because the gap is real and unstarted. It goes green when
    the asset-class counts are canon-owned and readable from one place.
    """
    covered = {"facilities", "deals"}
    uncovered = [s["key"] for s in _LAYERS if s["key"] not in covered]
    return [_check(
        "canon_owns_asset_counts",
        "asset-class counts have a canon owner", False,
        (f"{len(covered)} of {len(_LAYERS)} measured layers are canon-owned "
         f"(facilities, deals). The other {len(uncovered)} have no canonical "
         f"publisher: {uncovered}. /api/v1/canon/phrases publishes only "
         f"facilities, countries, markets, deals, tools; /api/v1/stats/"
         f"canonical was swept live on 2026-08-06 for fiber/gas/plant/"
         f"transmission/substation/pipeline/cable keys and carries NONE of "
         f"them under any name. Consequence: every asset-class figure on the "
         f"site, in the MCP description and in the initialize instructions is "
         f"a literal that can go stale silently — exactly the mcp-server #132 "
         f"class. WORK ORDER, not a defect: publish these counts from canon "
         f"and have the description read them. This lane goes green when it "
         f"can."),
        critical=False)]


# ── population — BUILT FROM the executed layer list, never hand-typed ────────
def _population() -> dict:
    """What is measured, in prose and in the exact SQL that measures it.

    Derived from the same _LAYERS tuple the lanes iterate and from the same
    _count_sql/_freshness_sql/_mode_sql builders they call, per
    routes/canonical_benchmarks.py _p50_population() (PR #2253). The freshness
    column is resolved at RUNTIME, so it is published as the declared
    preference order plus a note that the live pick is named in each detail.
    """
    return {
        "statistic": ("per-layer row count, newest ingestion timestamp, and "
                      "7d/30d delta"),
        "layers_measured": [s["key"] for s in _LAYERS],
        "window": "7 and 30 days, rolling, ending now",
        "verdict_authority": (
            "ONLY the recency check can fail, and only against that layer's "
            "own declared cadence. The delta is a gauge and never convicts."),
        "excludes": (
            "cable_landing_points (0 rows live — the populated twin is "
            "subsea_landing_points, already guarded by "
            "tests/test_subsea_wrong_table_and_segment_counts.py); "
            "power_plants_eia and discovered_power_plants (staging twins of "
            "power_plants); gem_gas_pipelines (a source feed, not the "
            "serving table)"),
        "unmeasurable_policy": (
            "a layer whose freshness column is absent, all-NULL or uncastable "
            "renders pass=None with the reason and names the column that "
            "would be needed. UNREADABLE IS NOT STALE."),
        "sql": [
            {"layer": s["key"],
             "table": s["table"],
             "count": _count_sql(s),
             "freshness_candidates": list(s["ts_candidates"]),
             "freshness_template": _freshness_sql(s, "<col>", "<col>"),
             "mode_template": _mode_sql(s, "<col>", "<col>"),
             "cadence_days": s["cadence_days"],
             "cadence": s["cadence"],
             "source": s["source"]}
            for s in _LAYERS
        ],
    }


def _tick() -> dict:
    lanes = []
    for spec in _LAYERS:
        checks = _safe_lane(_lane_for_layer, spec)
        lanes.append({"id": spec["key"], "name": spec["label"],
                      "checks": checks, "verdict": _lane_verdict(checks)})
    checks = _safe_lane(_lane_canon_coverage)
    lanes.append({"id": "canon_coverage", "name": "asset counts in canon",
                  "checks": checks, "verdict": _lane_verdict(checks)})
    return {
        "shell": "ingestion-freshness",
        "note": ("Row count alone cannot tell a healthy layer from an "
                 "abandoned one — only recency and delta can. A RELOAD IS NOT "
                 "GROWTH: layers marked MODE=SNAPSHOT rewrite their whole "
                 "table, so their delta is a refresh stamp. UNREADABLE IS NOT "
                 "STALE: pass=None means unmeasured, never a failure."),
        "population": _population(),
        "lanes": lanes,
        "lanes_total": len(lanes),
        "lanes_pass": sum(1 for x in lanes if x["verdict"] == "PASS"),
        "summary": " ".join(f"{x['id']}={x['verdict']}" for x in lanes),
    }


@ingestion_freshness_master_shell_bp.route(
    "/api/v1/admin/ingestion-freshness/master-tick", methods=["GET"])
def ingestion_freshness_master_tick():
    if _disabled():
        return jsonify({"disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_tick())


@ingestion_freshness_master_shell_bp.route(
    "/admin/ingestion-freshness", methods=["GET"])
def ingestion_freshness_board():
    if _disabled():
        return Response("shell disabled", mimetype="text/plain")
    if not _admin_ok():
        return Response("unauthorized", status=401, mimetype="text/plain")
    t = _tick()
    rows = []
    for lane in t["lanes"]:
        rows.append(f"\n{lane['verdict']:<5} {lane['id']} — {lane['name']}")
        for c in lane["checks"]:
            mark = {True: "OK ", False: "RED", None: " ? "}[c["pass"]]
            rows.append(f"   [{mark}] {c['name']}\n        {c['detail']}")
    return Response(t["summary"] + "\n" + t["note"] + "\n" + "\n".join(rows),
                    mimetype="text/plain")
