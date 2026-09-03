"""Daily infra growth tracker — one count snapshot per layer per day.

Pure DB COUNT(*)s (no external egress) so it runs fine on Railway; a daily
cron POSTs the snapshot and surfaces FLATLINE warnings — a layer that
should be growing but hasn't changed in N days is an early signal that its
source quietly broke (exactly what happened to gas pipelines: frozen at
918 for weeks while the cron looked green).

Layers are tagged by expected cadence:
  daily    — should gain rows most days (substations, data centers)
  periodic — refreshes in bursts when the gov source republishes (gas,
             FCC fiber, gas compressors/processing)
  static   — annual federal data, no daily/weekly growth expected
The flatline check only fires when days-since-last-change exceeds the
layer's max_stale_days (None = never warn, for static layers).

★ A COUNT(*) DELTA CANNOT SEE A FULL-RELOAD LAYER. Several loaders here
replace their table wholesale (truncate/upsert every row) rather than
appending, so the row count is flat while every row is rewritten. Measured
2026-08-07: gas_pipelines rewrote 30,000 rows on 08-03, gas_compressors all
1,768 on 08-02, gem_power all 182,428 on 08-01 — and every one of them
reported delta_7d = 0, indistinguishable from an abandoned table. That is
why each layer also carries a freshness column (_FRESH_COL): last ingest
timestamp is the only signal that separates "refreshed in place" from
"dead". A layer with no such column is UNMEASURABLE for freshness and says
so — freshness is never inferred from a total.

Endpoints (admin-gated):
  POST /api/v1/admin/infra-growth/snapshot  → record today + return summary
  GET  /api/v1/admin/infra-growth           → summary from stored history
  GET  /api/v1/admin/infra-growth/history?layer=X&days=30 → raw series
"""
import os

import psycopg2
from flask import Blueprint, jsonify, request

from util.deals import DEALS_OK
from util.dominant_source import (MASK_LAG_DAYS, dominant_source_lag,
                                  tables_with_a_source_column)

infra_growth_bp = Blueprint("infra_growth", __name__)

# (label, source table, category, max_stale_days)  None = never flag stale.
_LAYERS = [
    ("substations",             "substations",              "daily",    10),
    ("data_centers",            "discovered_facilities",    "daily",    14),
    ("gas_pipelines",           "gas_pipelines",            "periodic", 130),
    ("fcc_fiber_hexes",         "fcc_fiber_hex",            "periodic", 230),
    ("metro_fiber_routes",      "fiber_routes",             "periodic", 75),
    ("gas_compressors",         "gas_compressor_stations",  "periodic", 200),
    ("gas_processing",          "gas_processing_plants",    "periodic", 200),
    # ★ Both repointed 2026-08-07 — each had been counting an abandoned twin.
    # transmission_lines counted `infrastructure_layers WHERE category='transmission'`,
    # which returns 0: that table's categories are infrastructure/fiber/
    # power_generation/substation and it has NO 'transmission' category at all.
    # The real table holds 95,560 rows (eia-arcgis-runner 94,619 + hifld 934),
    # last refreshed 2026-08-03 — the layer was rendering as absent/zero while
    # its loader was one of the healthiest on the board.
    ("transmission_lines",      "transmission_lines",       "periodic", 120),
    # power_plants_eia counted the `power_plants_eia` table: 13,446 rows, NO
    # timestamp column of any kind, and reporting_period/source_survey empty on
    # every row. The live twin `power_plants` is 100% source='eia-860'
    # (14,480 rows, refreshed 2026-07-31), so the label stays accurate.
    ("power_plants_eia",        "power_plants",             "periodic", 120),
    ("power_plants_discovered", "discovered_power_plants",   "static",   None),
    # ★ SUBSEA ADDED 2026-08-07 (audit SH52-059: "infra_growth_snapshot has no
    # subsea layer"). This is the (a) case the board could not distinguish: the
    # layer was not measured AT ALL, which on the page is indistinguishable from
    # a layer measured and found flat. Both tables went 133 days with no writes
    # (last 2026-03-27) because nothing drove the sync — its only registration
    # was dchub-scheduler.py's JOBS dict, and no start command in this repo
    # launches that file. #2330 put it in data-sync.yml, which runs 4x/day and
    # upserts every row (updated_at=NOW() on insert AND on conflict), so 10 days
    # of no movement means the driver died again, not that the source is quiet.
    ("subsea_cables",           "subsea_cables",            "periodic", 10),
    ("subsea_landings",         "subsea_landing_points",    "periodic", 10),
    # GEM worldwide inventory — gated quarterly refresh (owner re-downloads); "periodic"
    # with generous thresholds so a stale flag = "GEM is overdue for a refresh", not noise.
    ("gem_global_power",        "gem_power",                "periodic", 150),
    ("gem_lng_terminals",       "gem_gas",                  "periodic", 220),
    ("gem_pipelines",           "gem_gas_pipelines",        "periodic", 220),
    ("gem_coal_mines",          "gem_coal_mines",           "periodic", 220),
]
_CAT = {l[0]: l[2] for l in _LAYERS}
_STALE = {l[0]: l[3] for l in _LAYERS}

# label -> the REAL ingestion-timestamp column on its source table. Every entry
# was read out of information_schema on 2026-08-07, not guessed: the names are
# inconsistent across tables (created_at / loaded_at / ingested_at / first_seen)
# and two of them are stored as TEXT, so they need an explicit cast.
# A label ABSENT from this dict has no usable timestamp column and is reported
# as freshness_measurable=False — see power_plants_discovered below.
_FRESH_COL = {
    "substations":             "created_at",
    "data_centers":            "first_seen",      # discovered_at is TEXT; first_seen is tz-aware, 0 nulls
    "gas_pipelines":           "created_at",
    "fcc_fiber_hexes":         "loaded_at",
    "metro_fiber_routes":      "created_at",
    "gas_compressors":         "loaded_at",
    "gas_processing":          "loaded_at",
    "transmission_lines":      "created_at",
    "power_plants_eia":        "created_at",
    "power_plants_discovered": "discovered_at",   # TEXT — cast below
    "gem_global_power":        "ingested_at",
    "gem_lng_terminals":       "ingested_at",
    "gem_pipelines":           "ingested_at",
    "gem_coal_mines":          "ingested_at",
    # Both subsea upserts set updated_at=NOW() on INSERT and again in their
    # ON CONFLICT DO UPDATE, so updated_at is the refresh stamp for every row
    # the sync touched — created_at would only ever show the first write and
    # would report a live loader as 133 days dead.
    "subsea_cables":           "updated_at",
    "subsea_landings":         "updated_at",
}
# Columns stored as TEXT rather than a timestamp type; need ::timestamptz.
_FRESH_TEXT = {"power_plants_discovered"}

# label -> date its source table was repointed. Snapshots recorded before this
# counted a DIFFERENT table, so a delta spanning the boundary would invent a
# one-time spike (transmission_lines would have read +95,560 overnight, and
# power_plants_eia +1,034). History before the cutover is ignored, not deleted.
_HISTORY_FROM = {
    "transmission_lines": "2026-08-07",
    "power_plants_eia":   "2026-08-07",
}

# Expected republish cadence per layer, so a board can judge "quiet" against
# INTENT instead of one global threshold. Federal/NGO datasets genuinely
# republish a few times a year — silence from them is not a broken loader.
_EXPECTED_CADENCE = {
    "substations":             "daily",
    "data_centers":            "daily",
    "gas_pipelines":           "quarterly",     # HIFLD/EIA republish
    "fcc_fiber_hexes":         "semiannual",    # FCC BDC release cycle
    "metro_fiber_routes":      "quarterly",
    "gas_compressors":         "quarterly",
    "gas_processing":          "quarterly",
    "transmission_lines":      "quarterly",
    "power_plants_eia":        "monthly",       # EIA-860M
    "power_plants_discovered": "adhoc",
    "gem_global_power":        "quarterly",     # owner re-downloads GEM
    "gem_lng_terminals":       "quarterly",
    "gem_pipelines":           "quarterly",
    "gem_coal_mines":          "quarterly",
    "subsea_cables":           "weekly",
    "subsea_landings":         "weekly",
}

# ── Why a layer looks quiet ────────────────────────────────────────────────
# ★ THE DEFECT THIS CLOSES (operator-reported 2026-08-07): twelve of fifteen
# layers on /whats-new rendered a bare total under a chip reading "periodic" or
# "static", and nothing else. Those are CADENCE words, not health words, and
# alone they read as "this layer is dead". Three genuinely different situations
# were rendering identically:
#
#   (a) NOT MEASURED — subsea had no _LAYERS entry at all (SH52-059), so the
#       page could not show growth it was never computing. Fixed above by
#       registering the layer, not by relabelling it.
#   (b) GENUINELY FROZEN, cause known — terrestrial fiber was capped by a live
#       UNIQUE(name, provider) key (SH52-054) and the substation bulk refresh
#       was blocked upstream (SH52-056). These had an owner and an open
#       finding; both closed 2026-08-14 (see _RESOLVED).
#   (c) EARNED IDLE — the FCC BDC republishes twice a year, so 56 quiet days is
#       ON SCHEDULE. Flagging it would be the cry-wolf failure this repo has
#       already rejected twice.
#
# So every layer now publishes a DERIVED status plus the reason that produced
# it. Derived, because a hand-maintained health label rots the moment a loader
# changes; the only hand-written parts are _KNOWN_ISSUE and _RESOLVED, which
# cite the audit finding by id so a stale annotation is a dangling reference a
# test can catch — and, since 2026-08-14, a LIFECYCLE conflict a live check can
# catch: _annotation_lifecycle_checks in routes/audit_closure_master_shell.py
# FAILS the closure board when an entry here cites a finding the registry
# records as fixed. That is the class fix for the pattern where an annotation
# outlives its own repair (SH52-054's "structurally capped" note over a fixed
# cap, SH52-056's "pinned vintage" over a completed backfill, SH52-051's
# "failing" over a fixed gate — three instances in one month).

# label -> (audit finding id, why this layer still carries an open finding).
# PROSE ONLY, NO FIGURES: every number on this board is measured at request
# time. A count baked into an annotation is exactly the frozen-figure class
# that qa-whats-new-fence.mjs exists to catch on the page itself.
#
# ★★★ AND NO CLAIM ABOUT WHETHER THE COUNT MOVES. Figures are not the only
# thing that goes stale here; a VERB does too. The fiber note below used to
# end "The route count moves only on a bulk refresh." That was true when it
# was written and false three days later, and because status/status_reason/
# added are all measured per request, the SAME response published "growing,
# +N new rows in the last 7d" directly above it (live 2026-08-12). A
# hand-written note may say what is STRUCTURALLY true — what the arbiter keys
# on, what is unidentified, what was left in place — and must leave every
# statement about movement to the measured fields, which cannot go stale.
# Fenced by test_known_issue_notes_make_no_claim_about_the_count_moving.
# ★ 2026-08-14: all three original entries retired to _RESOLVED below after
# the owner-gated backfill and the discovery-gate fixes landed (SH52-054 fiber
# cap, SH52-056 substation vintage pin, SH52-051 energy-discovery gate).
# EMPTY IS A VALID STATE and means "no layer is known to be structurally
# stuck" — a different claim from "every layer is healthy", exactly as
# known_issue: None is per layer.
_KNOWN_ISSUE = {}

# label -> (audit finding id, resolved-on ISO date, what changed).
# The quiet credit line for a finding that JUST closed. A fixed note rendered
# as a yellow warning is itself a falsehood (the SH52-054 note spent four days
# opening with "the structural cap here is fixed" inside warning styling), so
# a closed finding moves HERE: neutral prose, served only for
# _RESOLVED_TTL_DAYS after its date, after which _resolved_credit() stops
# emitting it and the line ages off the page with no further edit.
# Same rules as _KNOWN_ISSUE, enforced by the same tests: prose only, no
# figures, and no claims about whether the count moves — say what changed
# structurally and leave movement to the measured fields.
_RESOLVED = {
    "metro_fiber_routes": (
        "SH52-054", "2026-08-14",
        "Resolved 2026-08-14: routes are now keyed on the upstream asset id "
        "the source itself publishes, and a supervised backfill re-swept "
        "every market and recovered the routes the earlier "
        "UNIQUE(name, provider) key had been discarding. Rows whose source "
        "publishes no stable id remain UNIDENTIFIED rather than assumed "
        "unique, and duplicate twins minted under the earlier identity stay "
        "reported rather than deleted."),
    "substations": (
        "SH52-056", "2026-08-14",
        "Resolved 2026-08-14: the identity strategy shipped and the "
        "supervised HIFLD bulk refresh ran — held rows were refreshed in "
        "place and now carry the upstream asset key, and the previously "
        "pinned 2026-03-17 vintage is discharged. Substation type and "
        "max-voltage enrichment, previously unpopulated, now comes from the "
        "same source."),
    "power_plants_discovered": (
        "SH52-051", "2026-08-14",
        "Resolved 2026-08-14: the data-sync energy-discovery step no longer "
        "fails on its own gate — the market filter now reaches the SQL and "
        "the weekly arm authenticates. The finding was related context for "
        "this layer, never the whole explanation of its measured gap; judge "
        "growth here from the ingest age, as before."),
}

# A layer re-ingested inside this many days is demonstrably alive even with a
# flat count — the full-reload case. Deliberately shorter than every layer's
# max_stale_days so "refreshed" means recently, not merely within tolerance.
_RELOAD_FRESH_DAYS = 7

# How long a resolved credit line stays on the board. Long enough for a
# returning reader to see the state change and its cause; short enough that
# "resolved" never becomes permanent furniture the way the yellow notes it
# replaces did.
_RESOLVED_TTL_DAYS = 30


def _resolved_credit(label):
    """Time-limited {ref, on, note} for a layer whose finding just closed.

    Aging is computed at request time, so retirement needs no second edit:
    past _RESOLVED_TTL_DAYS the line simply stops being served (the entry
    itself is then dead code, removable at leisure — the page is already
    clean). An unparseable date serves NOTHING rather than serving forever:
    fail-closed, because the whole point of this table is that its lines
    expire."""
    entry = _RESOLVED.get(label)
    if not entry:
        return None
    ref, on, note = entry
    try:
        import datetime
        age = (datetime.date.today() - datetime.date.fromisoformat(on)).days
    except Exception:
        return None
    if age > _RESOLVED_TTL_DAYS:
        return None
    return {"ref": ref, "on": on, "note": note}


def _layer_status(delta_window, window_days, ingest_age, stale, expected):
    """(status, reason) for one layer, derived ONLY from measured signals.

    ★ THE TWO TRAPS THIS ENCODES, both of which have shipped here before:

    FRESH IS NOT GROWTH. A recent ingest timestamp proves the loader ran; it
    proves nothing about new rows. So "refreshed" says the count is flat and
    says why that is expected — it never implies the layer grew.

    RELOAD IS NOT GROWTH, and its converse: an UNMEASURED delta is not zero.
    A layer whose count history does not yet span the window returns
    delta_window=None, which formats as "—" and MUST NOT render as "no growth".
    That case gets its own status ("measuring") instead of falling through to
    the freshness branches, which would state the layer is merely being
    refreshed when the truth is that growth has not been measured yet.
    """
    if delta_window is not None and delta_window > 0:
        return ("growing",
                f"+{delta_window:,} new rows in the last {window_days}d")
    if delta_window is None:
        base = ("growth not measured yet — this layer has no count snapshot "
                "old enough to difference against")
        if ingest_age is not None:
            base += f"; last ingest {ingest_age}d ago"
        return ("measuring", base)
    if ingest_age is None:
        return ("unmeasurable",
                "no ingestion-timestamp column exists on this table, so "
                "freshness here is UNKNOWN — it is never inferred from the total")
    if ingest_age <= _RELOAD_FRESH_DAYS:
        # ★ MEASUREMENT ONLY, NO MECHANISM. The first draft of this branch read
        # "this loader rewrites every row rather than appending, so a flat count
        # is what a healthy run looks like" — true of gas and GEM, and FALSE of
        # metro_fiber_routes, which is flat because a UNIQUE(name, provider) key
        # discards its inserts (SH52-054). Both layers reach this branch, so
        # asserting the reload mechanism here published a reassuring explanation
        # over a capped feed — the exact flattering-direction error this board
        # was built to catch. What is actually known is that the table was
        # written and the count did not move; WHY belongs in known_issue, where
        # it is cited rather than inferred.
        return ("refreshed",
                f"table written {ingest_age}d ago and the row count did not "
                f"move — the loader ran; no net new rows persisted")
    if stale is None:
        # No threshold declared means WE are not judging this layer, which is a
        # fact about this config and not about the source. Saying "no scheduled
        # loader, ad-hoc by design" would be a claim about intent, and for
        # power_plants_discovered a false one — it has a driver (data-sync's
        # per-market energy discovery) that SH52-051 reports as failing. So
        # publish the age and decline to reassure.
        # Reads on the page as prose, not as a field reference: this string is
        # rendered verbatim to visitors, so naming the JSON key here ("read
        # known_issue") would ship an API detail into the product copy.
        # "and the open finding" was dropped 2026-08-14 when SH52-051 closed:
        # a status string must not assert that a finding exists — that is the
        # annotation's job, and only when one is actually open.
        return ("unjudged",
                f"no staleness threshold is declared for this layer, so it is "
                f"never flagged as overdue; last ingest {ingest_age}d ago — "
                f"judge it from that number, not from this status")
    if ingest_age <= stale:
        return ("on_cadence",
                f"last ingest {ingest_age}d ago; this source republishes "
                f"{expected or 'periodically'}, so quiet up to {stale}d is "
                f"on schedule, not a fault")
    return ("overdue",
            f"last ingest {ingest_age}d ago, past the {stale}d window for a "
            f"{expected or 'periodic'} source — the loader may have broken")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _admin_ok():
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    got = (request.headers.get("X-Admin-Key") or request.headers.get("X-Internal-Key")
           or request.args.get("admin_key") or "")
    return bool(expected) and got == expected


def _ensure(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS infra_growth_snapshot (
            snapshot_date DATE NOT NULL,
            layer TEXT NOT NULL,
            count BIGINT,
            captured_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (snapshot_date, layer)
        )""")


def _count(cur, tbl, label):
    """COUNT(*) for a layer.

    ★ There used to be a hardcoded `if label == "transmission_lines"` branch
    here that ignored `tbl` and counted `infrastructure_layers WHERE
    category='transmission'` (0 rows — that category does not exist). It is
    gone: while it stood, repointing the layer in _LAYERS was a silent no-op,
    because the table named in the tuple was never the table queried.
    """
    cur.execute("SELECT to_regclass(%s)", (tbl,))
    if not cur.fetchone()[0]:
        return None
    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
    return cur.fetchone()[0]


def _freshness(cur, tbl, label):
    """(last_ingest_iso, age_days) for a layer, or (None, None) if unmeasurable.

    Isolated in its own try/except on purpose: a freshness read that blows up
    (bad cast on a TEXT column, column dropped upstream) must degrade to
    "unmeasurable" for that one layer, never take down the whole snapshot.
    """
    col = _FRESH_COL.get(label)
    if not col:
        return None, None
    cast = "::timestamptz" if label in _FRESH_TEXT else ""
    try:
        cur.execute(
            f"SELECT MAX({col}{cast})::timestamptz, "
            f"       (NOW()::date - MAX({col}{cast})::date) FROM {tbl}")
        row = cur.fetchone()
        if not row or row[0] is None:
            return None, None
        return row[0].isoformat(), int(row[1])
    except Exception:
        cur.connection.rollback()
        return None, None


def _at_or_before(hist, target):
    """Most recent count at or before a target date. hist = [(date,count)] newest-first."""
    for d, c in hist:
        if d <= target:
            return c, d
    return None, None


def _days_since_change(hist):
    """Days since the count last changed (hist newest-first). None if <2 points."""
    if len(hist) < 2:
        return None
    newest = hist[0][1]
    change_date = hist[0][0]
    for d, c in hist[1:]:
        if c != newest:
            break
        change_date = d
    return (hist[0][0] - change_date).days


def _summary(cur):
    cur.execute("SELECT MAX(snapshot_date) FROM infra_growth_snapshot")
    today = (cur.fetchone() or [None])[0]
    # One probe for all 16 layer tables instead of one per layer: measured
    # 1,691ms -> 79ms against the production replica, and this endpoint sits
    # behind the edge's 15s admin timeout. None means the probe itself failed,
    # which falls back to per-table probing inside the helper — never to
    # "no table has a source column", which would disable the check in silence.
    has_src = tables_with_a_source_column(cur, [t for _, t, _, _ in _LAYERS])
    out, flatlines = [], []
    for label, tbl, cat, stale in _LAYERS:
        # _HISTORY_FROM drops snapshots taken while this layer pointed at a
        # different table — counting across that boundary invents a spike.
        # captured_at comes along so the page can state WHEN the count was
        # taken. Without it the lag is invisible: on 2026-08-07 this board
        # published gas_pipelines = 30,918 while the table held 33,769, because
        # the 06:34 UTC snapshot ran ~2h BEFORE that morning's ingest. The count
        # was not wrong for its timestamp — the timestamp was never shown.
        cur.execute("""SELECT snapshot_date, count, captured_at FROM infra_growth_snapshot
                        WHERE layer=%s AND snapshot_date >= COALESCE(%s::date, '-infinity'::date)
                        ORDER BY snapshot_date DESC LIMIT 90""",
                    (label, _HISTORY_FROM.get(label)))
        rows = cur.fetchall()
        if not rows:
            continue
        # _at_or_before / _days_since_change consume (date, count) pairs.
        hist = [(r[0], r[1]) for r in rows]
        captured_at = rows[0][2]
        cur_date, cur_count = hist[0]   # SELECT order is (snapshot_date, count)
        d1 = d7 = None
        if today:
            import datetime
            prev_c, _ = _at_or_before(hist[1:], cur_date - datetime.timedelta(days=1))
            wk_c, _ = _at_or_before(hist, cur_date - datetime.timedelta(days=7))
            if prev_c is not None:
                d1 = int(cur_count) - int(prev_c)
            if wk_c is not None:
                d7 = int(cur_count) - int(wk_c)
        dsc = _days_since_change(hist)
        last_ingest, ingest_age = _freshness(cur, tbl, label)
        # ★★★ THE FRESHNESS READ ABOVE IS A BARE MAX() OVER THE WHOLE TABLE, so
        # on a table fed by several independent lanes it answers "is ANY lane
        # alive" — the weakest question available — and is pinned green by the
        # most trivial writer. Measured 2026-09-03: `substations` reported
        # ingest_age 0d against a 10d threshold AND status "growing", while
        # HIFLD (79,788 rows, 63% of the table) had not written since 08-14 and
        # its loader was 500ing nightly. The 708-row `auto_discovery` lane —
        # 0.56% of the table — carried the entire signal.
        #
        # ★ RUNS FOR EVERY LAYER, not the ones we suspect. A checker that scans
        # a subset of what it publishes certifies the rest clean by never
        # looking. On the four healthy multi-source layers it measures a lag of
        # 0 and stays silent, so this cannot manufacture a red for slow federal
        # data — the measure is relative to the table's OWN newest row, never to
        # a clock.
        mask_lag, mask_src, mask_rows = dominant_source_lag(
            cur, tbl, _FRESH_COL.get(label),
            has_source=(None if has_src is None else tbl in has_src))
        # ★ A flat COUNT(*) is NOT evidence of a dead layer — full-reload
        # loaders rewrite every row and leave the count unchanged. If the table
        # was re-ingested inside its own staleness window, it is alive, so the
        # flatline warning is withheld and the freshness read is why.
        flat = bool(stale is not None and dsc is not None and dsc > stale)
        if flat and ingest_age is not None and ingest_age <= stale:
            flat = False
        # ★ THIS SUPPRESSION HAS THE SAME SHAPE AS THE DEFECT ABOVE — it lets a
        # freshness read excuse a flat count, and a MASKED freshness read would
        # excuse it falsely. It is left alone on purpose: it bites only when a
        # layer is flat AND masked at once, and measured 2026-09-03 against the
        # live snapshot history, NO layer is in that state (the flat ones are
        # subsea_landings 20d/10d and gas_pipelines 23d/130d, neither masked;
        # substations, the one masked layer, changes daily and is never flat).
        # Changing what flatline means needs its own evidence and its own diff,
        # so this records a measured-zero gap rather than fixing it blind.
        # Best-available rolling window: current vs the OLDEST snapshot still
        # within 7d. Lets the public feed show a real delta even while the
        # tracker is younger than 7 days (then window_days < 7, labelled so).
        dwin = wdays = None
        for d, cc in reversed(hist):            # reversed(newest-first) = oldest-first
            age = (cur_date - d).days
            if 1 <= age <= 7:
                dwin, wdays = int(cur_count) - int(cc), age
                break
        status, status_reason = _layer_status(
            dwin, wdays, ingest_age, stale, _EXPECTED_CADENCE.get(label))
        # ★ APPENDED TO EVERY STATUS, including "growing" — which is the one
        # substations actually reads. A layer gaining 1-8 rows a day from a
        # 0.56% lane while its canonical loader is dead is BOTH growing and
        # broken, and the second half is the part nobody could see. The status
        # itself is left alone: it is derived from measured signals and each
        # branch is still true as far as it goes.
        if mask_lag is not None and mask_lag >= MASK_LAG_DAYS:
            status_reason = (status_reason or "") + (
                f" [⚠ freshness is NOT coming from the main source: "
                f"'{mask_src}' holds {mask_rows:,} rows and is {mask_lag}d "
                f"behind this table's newest row — the {_FRESH_COL.get(label)} "
                f"read above is being satisfied by a smaller lane]")
        known = _KNOWN_ISSUE.get(label)
        rec = {"layer": label, "category": cat, "count": int(cur_count),
               "delta_1d": d1, "delta_7d": d7, "delta_window": dwin, "window_days": wdays,
               "days_since_change": dsc, "flatline": flat, "as_of": str(cur_date),
               # Derived health, so a cadence chip ("periodic"/"static") never
               # ships alone — it is a schedule, not a verdict.
               "status": status, "status_reason": status_reason,
               # Hand-written ONLY where an audit finding documents a structural
               # block. None means "nothing known is stuck here", which is a
               # different claim from "healthy".
               "known_issue": ({"ref": known[0], "note": known[1]} if known else None),
               # Time-limited credit for a finding that just closed — neutral
               # prose, never a warning, expires server-side (_RESOLVED_TTL_DAYS).
               "resolved": _resolved_credit(label),
               "count_captured_at": (captured_at.isoformat() if captured_at else None),
               # Freshness: the only signal that separates a full-reload layer
               # from an abandoned one. False = no timestamp column exists on
               # the source table, so freshness is UNKNOWN — never assumed.
               "last_ingest_at": last_ingest,
               "ingest_age_days": ingest_age,
               "freshness_measurable": last_ingest is not None,
               "freshness_column": _FRESH_COL.get(label),
               # Which lane actually supplies this table's rows, and how far it
               # sits behind the table's newest row. None = the table has no
               # `source` column or only one source, so this check has nothing
               # to say — that is not the same as a clean bill of health.
               "dominant_source": mask_src,
               "dominant_source_rows": mask_rows,
               "dominant_source_lag_days": mask_lag,
               "expected_cadence": _EXPECTED_CADENCE.get(label)}
        out.append(rec)
        if flat:
            flatlines.append(f"{label} (no change in {dsc}d, expected <{stale}d, "
                             f"last ingest {last_ingest or 'UNMEASURABLE'})")
    return out, flatlines


@infra_growth_bp.route("/api/v1/admin/infra-growth/snapshot", methods=["POST"])
def snapshot():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                _ensure(cur)
                recorded = 0
                for label, tbl, cat, stale in _LAYERS:
                    n = _count(cur, tbl, label)
                    if n is None:
                        continue
                    cur.execute("""
                        INSERT INTO infra_growth_snapshot (snapshot_date, layer, count)
                        VALUES (CURRENT_DATE, %s, %s)
                        ON CONFLICT (snapshot_date, layer)
                        DO UPDATE SET count=EXCLUDED.count, captured_at=NOW()""",
                        (label, n))
                    recorded += 1
                c.commit()
                summary, flatlines = _summary(cur)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, recorded=recorded, flatlines=flatlines, layers=summary)


@infra_growth_bp.route("/api/v1/admin/infra-growth", methods=["GET"])
def growth():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                _ensure(cur)
                summary, flatlines = _summary(cur)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, flatlines=flatlines, layers=summary)


@infra_growth_bp.route("/api/v1/admin/infra-growth/history", methods=["GET"])
def history():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    layer = (request.args.get("layer") or "").strip()
    try:
        days = min(int(request.args.get("days", 30)), 365)
    except (TypeError, ValueError):
        days = 30
    dsn = _dsn()
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                _ensure(cur)
                if layer:
                    cur.execute("""SELECT snapshot_date, count FROM infra_growth_snapshot
                                    WHERE layer=%s AND snapshot_date > CURRENT_DATE - %s
                                    ORDER BY snapshot_date""", (layer, days))
                else:
                    cur.execute("""SELECT snapshot_date, layer, count FROM infra_growth_snapshot
                                    WHERE snapshot_date > CURRENT_DATE - %s
                                    ORDER BY snapshot_date, layer""", (days,))
                rows = [list(map(str, r)) for r in cur.fetchall()]
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, layer=layer or "all", days=days, rows=rows)


_FRIENDLY = {
    "data_centers": "Data centers", "substations": "Substations",
    "gas_pipelines": "Gas pipelines", "metro_fiber_routes": "Fiber routes",
    "fcc_fiber_hexes": "Broadband / middle-mile coverage", "gas_compressors": "Gas compressor stations",
    "gas_processing": "Gas processing plants", "transmission_lines": "Transmission lines",
    "power_plants_eia": "Power plants", "power_plants_discovered": "Discovered power plants",
    "subsea_cables": "Subsea cables", "subsea_landings": "Subsea cable landings",
}

# Provenance so the public feed — and anything downstream that messages these
# numbers (media shell, agents) — never implies a third-party open-data layer
# was "discovered" by DC Hub. "curated" = DC Hub crawls/curates the rows;
# "public" = we UNIFY a third-party open dataset (still valuable, but say so).
_PROVENANCE = {
    "data_centers":            ("curated", "DC Hub crawlers"),
    "power_plants_discovered": ("curated", "DC Hub discovery"),
    "substations":             ("public",  "HIFLD"),
    # Measured 2026-08-07 on the repointed table: eia-arcgis-runner 94,619 +
    # hifld 934 + news_extraction 7. It was labelled HIFLD-only while pointing
    # at a table that returned 0 rows.
    "transmission_lines":      ("public",  "EIA / HIFLD"),
    "gas_pipelines":           ("public",  "EIA / HIFLD"),
    "gas_compressors":         ("public",  "HIFLD"),
    "gas_processing":          ("public",  "HIFLD"),
    "fcc_fiber_hexes":         ("public",  "FCC"),
    "metro_fiber_routes":      ("public",  "public + DC Hub"),
    "power_plants_eia":        ("public",  "EIA"),
    "subsea_cables":           ("public",  "TeleGeography"),
    "subsea_landings":         ("public",  "TeleGeography"),
}


@infra_growth_bp.route("/api/v1/whats-new", methods=["GET"])
def whats_new():
    """PUBLIC: recent additions per category (7d / 1d) for the on-site 'What's New'
    feed. No auth — it's a freshness/marketing signal. Reuses the growth snapshots."""
    import datetime
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    deals = None
    # Platform capability announcements. Stays None until the block below runs,
    # so an unreachable DB publishes `platform: null` + a reason rather than an
    # empty list (an empty list would read as "nothing shipped" — a false claim).
    plat = None
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                _ensure(cur)
                layers, _flat = _summary(cur)
                # Deals: count by DB-insertion time (created_at = when WE added the
                # row), not the text `date` column (that's the deal's announcement
                # date, 2018→today, and is text so date math errors).
                # r-wn-dealcanon (2026-07-17): exclude quarantined rows (the
                # 07-17 deals-integrity pass flagged ~2,823 duplicate/garbage
                # rows via data_flag='quarantine_*'; bare COUNT(*) republished
                # the ~2.9x over-claim here as total=4,304 while /api/v1/stats
                # already reports the deduped ~1,42x). Same predicate as the
                # served /api/deals query — literally the same string now.
                # This was a function-LOCAL copy (`_live = "..."`), the exact
                # shape that made the 07-27 capacity_pipeline audit's "every
                # read is guarded" claim false for fourteen reads: nothing
                # could import it, so nothing could check anything against it.
                # Spelled DEALS_OK at each site rather than re-aliased, so the
                # census in tests/test_deals_guard.py can see it.
                try:
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + DEALS_OK +
                                " AND created_at::timestamptz >= NOW() - INTERVAL '7 days'")
                    d7 = int(cur.fetchone()[0])
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + DEALS_OK +
                                " AND created_at::timestamptz >= NOW() - INTERVAL '1 day'")
                    d1 = int(cur.fetchone()[0])
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + DEALS_OK)
                    dtot = int(cur.fetchone()[0])
                    deals = (d7, d1, dtot)
                except Exception:
                    c.rollback()
                # Distinct BUILDINGS — the citable facility figure, mirroring
                # /api/v1/stats/canonical.facilities_distinct exactly so the site
                # never shows two different facility numbers.
                #
                # ★2026-08-31: this was COUNT(*) WHERE is_duplicate = 0, published
                # as `facilities_verified`. Three defects in one line:
                #   1. NAME. /api/v1/stats/canonical's own provenance block says of
                #      it, verbatim: "Both of the last two are DE-DUPLICATION
                #      states, not source verifications — do not publish either as
                #      'verified'." We were publishing exactly that, live, at
                #      20,019 — ABOVE the 19,969 distinct-building count, so the
                #      banned field was also the biggest number on the page.
                #   2. FIELD. COUNT(*) WHERE is_duplicate = 0 is canon's
                #      facilities_with_keeper, not facilities_verified
                #      (COUNT(*) WHERE duplicate_of_id IS NULL). The comment named
                #      a third thing again. Neither is citable.
                #   3. NULLs. `is_duplicate = 0` drops rows where is_duplicate IS
                #      NULL; the fleet filter is COALESCE(is_duplicate,0) = 0.
                # Same defect was fixed in public_endpoints.py on 2026-08-01 and
                # never swept here. Mirror the citable query instead.
                try:
                    cur.execute("SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities "
                                "WHERE canonical_slug IS NOT NULL")
                    dc_distinct = int(cur.fetchone()[0])
                except Exception:
                    c.rollback()
                    dc_distinct = None
                # ── Platform capability announcements (brain-staged, owner-approved)
                # The "New platform capabilities" cards on /whats-new were hardcoded
                # HTML and went stale ("36 grids", "tool #73"). They are data now.
                #
                # ★ ONE STORE (2026-08-01). This used to read a SECOND registry
                # (routes/capability_announcements.ANNOUNCEMENTS) while the brain
                # staged its cards into data/platform_updates.json. Two stores
                # shipped the same day and the brain wrote to the one this route
                # does not read, so four owner-approved cards were live at
                # /api/v1/platform-updates and invisible on the page they were
                # written for. Worse, the two card SHAPES differ: the page's
                # renderer reads `metric`/`link_href`/`code`, which the other
                # registry never emitted, so even the five cards that did render
                # lost their metric tile and their CTA link.
                # data/platform_updates.json is now the ONLY source — the store the
                # brain already writes to, and the shape the page already renders.
                # ★ APPROVAL is unchanged and still the whole point: a card is
                # served only when its entry carries the literal status
                # "published", which happens only by the owner merging the PR that
                # sets it. There is no write endpoint.
                # ★ Numbers are bound HERE, at serve time, from in-process canon —
                # no nested connection and no HTTP egress in a public request. The
                # page binds the SAME token from the SAME source, so the JSON an
                # agent reads and the figure a human sees cannot disagree.
                # FAIL SOFT: its own try/except, touching nothing above it — an
                # announcement failure must never 500 the route or blank the
                # coverage items[] that already work.
                try:
                    from routes.platform_updates import (
                        canon_values, published_updates, resolve_card_metrics)
                    plat = resolve_card_metrics(published_updates(),
                                                canon_values())
                except Exception as _pe:
                    try:
                        c.rollback()
                    except Exception:
                        pass
                    plat = {"ok": False,
                            "reason": f"announcement source unavailable: {str(_pe)[:120]}"}
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    def _prov(layer_key):
        p, s = _PROVENANCE.get(layer_key, ("public", "public data"))
        return {"provenance": p, "source_name": s}

    items = []
    if deals is not None:
        # Deals are DC Hub-curated (created_at = when WE logged it) — the
        # strongest "we did the work" number, so it leads the feed.
        # Deals are the one item counted LIVE rather than from a snapshot (the
        # three COUNT(*)s above run in this request), so its status is measured
        # directly and its count has no capture lag to disclose.
        items.append({"category": "Data-center deals", "total": deals[2],
                      "added": deals[0], "window_days": 7, "added_1d": deals[1],
                      "cadence": "daily", "as_of": None,
                      "status": ("growing" if deals[0] > 0 else "flat"),
                      "status_reason": (f"+{deals[0]:,} publishable deals logged in the last 7d"
                                        if deals[0] > 0 else
                                        "no new publishable deals logged in the last 7d"),
                      "known_issue": None,
                      "resolved": None,
                      "freshness_measurable": True,
                      "count_captured_at": None,
                      "provenance": "curated", "source_name": "DC Hub curated"})
    for l in layers:
        if not l["count"]:        # don't advertise empty layers
            continue
        item = {"category": _FRIENDLY.get(l["layer"], l["layer"]), "total": l["count"],
                "added": l.get("delta_window"), "window_days": l.get("window_days"),
                "added_1d": l["delta_1d"], "cadence": l["category"], "as_of": l["as_of"],
                # ★ Freshness travels with the total. Without it a layer that
                # refreshes in place is unreadable: 55,064 fiber routes looks
                # the same whether it reloaded today or was abandoned in March.
                # `added` legitimately stays 0 for a full-reload layer — the
                # honest signal there is last_ingest_at, not the delta.
                "last_ingest_at": l.get("last_ingest_at"),
                "ingest_age_days": l.get("ingest_age_days"),
                "freshness_measurable": l.get("freshness_measurable", False),
                "expected_cadence": l.get("expected_cadence"),
                # ★ A cadence word must never ship alone. "periodic"/"static"
                # describe a SCHEDULE; on their own they read to a visitor as
                # "dead". status + status_reason say which of the three actual
                # situations this is, and known_issue names the open finding
                # when the layer is structurally stuck.
                "status": l.get("status"),
                "status_reason": l.get("status_reason"),
                "known_issue": l.get("known_issue"),
                # ★ A fixed note is not a warning. When a finding closes, its
                # annotation moves from known_issue to this short-lived credit
                # line ({ref, on, note}), which expires server-side.
                "resolved": l.get("resolved"),
                # When the COUNT was taken. Counts come from the daily snapshot
                # (re-baselined at the end of every ingest); freshness above is
                # read live. Publishing both stops a lagging total from looking
                # like a live one.
                "count_captured_at": l.get("count_captured_at"),
                **_prov(l["layer"])}
        # Data centers: distinct BUILDINGS next to the source RECORDS they were
        # resolved from, so the headline can never read as "27.7K verified DCs".
        #
        # ★2026-08-31: this emitted `verified` + `tracked`. whats-new.html renders
        # `it.distinct` + `it.records` and deliberately does NOT render it.verified
        # (see its comment at whats-new.html:333). Neither key was ever emitted, so
        # `factLines` built an empty string and the data-centers card rendered NO
        # facility count at all — silently, 200 on both sides. The client half of
        # the 2026-08-06 fix shipped; the server half did not.
        #
        # That is the FOURTH instance of the bug class qa-api-contract.mjs was
        # written for, and it landed in that guard's documented blind spot: it is
        # intra-procedural and tracks keys read off the fetch identifier, while
        # `it` is a forEach loop variable over d.items[].
        if l["layer"] == "data_centers":
            item["label"] = "Data centers (tracked)"
            item["distinct"] = dc_distinct       # distinct buildings — the citable figure
            item["records"] = l["count"]         # raw source rows they resolved from
            item["tracked"] = l["count"]         # retained: pre-existing consumers
        items.append(item)
    # Everything counted here was added within the last 7 days (layer windows are ≤7d subsets).
    total_added = sum(i["added"] for i in items if isinstance(i["added"], int) and i["added"] > 0)
    # data_as_of = newest real snapshot date (a DATE, never future). The page
    # should render THIS, not generated_at (whose UTC instant tips into
    # "tomorrow" for US readers late in the day → a future "updated" date).
    _asof_dates = [i["as_of"] for i in items if i.get("as_of")]
    data_as_of = max(_asof_dates) if _asof_dates else None
    # Publish the announcements block. Three distinct states, deliberately:
    #   unavailable  -> platform: null  + platform_unavailable_reason (UNMEASURED)
    #   ok, none approved -> platform: [] + platform_pending count (a true "nothing
    #                        approved yet", which is NOT the same claim as null)
    #   ok, approved -> platform: [cards], each with its own figures[] + verify[]
    _plat_ok = bool(plat and plat.get("ok"))
    platform = plat.get("cards") if _plat_ok else None
    platform_reason = None if _plat_ok else (
        (plat or {}).get("reason") or "announcements not resolved this request")
    # `platform_pending` keeps its published meaning — entries the brain staged
    # that the owner has NOT approved. In the one-store model that is exactly
    # the set the approval gate withheld, so it is counted from the gate's own
    # reason rather than tracked separately (two counters would drift).
    _plat_pending = None
    if _plat_ok:
        _plat_pending = sum(
            1 for w in (plat.get("withheld") or [])
            if "not approved" in str((w or {}).get("reason") or ""))
    resp = jsonify(ok=True,
                   generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   data_as_of=data_as_of,
                   platform=platform,
                   platform_unavailable_reason=platform_reason,
                   platform_as_of=(datetime.datetime.now(datetime.timezone.utc)
                                   .isoformat() if _plat_ok else None),
                   platform_withheld=((plat or {}).get("withheld") or []) if _plat_ok else [],
                   platform_pending=_plat_pending,
                   total_added=total_added, items=items,
                   facilities_tracked=(layers and next((l["count"] for l in layers if l["layer"] == "data_centers"), None)) or None,
                   facilities_distinct=dc_distinct,
                   note="Live additions to DC Hub across infrastructure layers (rolling 7-day window). "
                        "Every layer carries a derived 'status' with the 'status_reason' that produced "
                        "it, because 'cadence' is a SCHEDULE and not a health verdict: 'growing' = new "
                        "rows measured; 'refreshed' = re-ingested with a flat count, which is what a "
                        "full-reload loader looks like when healthy; 'on_cadence' = quiet and on "
                        "schedule for a source that republishes a few times a year; 'measuring' = "
                        "growth NOT YET measured (never read as zero); 'unmeasurable' = the table has "
                        "no ingestion-timestamp column, so freshness is unknown rather than assumed; "
                        "'overdue'/'idle' = past its own window. 'known_issue' names the open audit "
                        "finding when a layer is structurally stuck; 'resolved' is the short-lived "
                        "credit line for a finding that recently closed — what changed and when, not "
                        "a warning — and it expires automatically. Totals come from the daily count "
                        "snapshot, re-baselined at the end of each ingest — 'count_captured_at' says "
                        "exactly when, so a lagging total can never pass for a live one; the freshness "
                        "fields beside it are read live at request time. "
                        "'Data centers' total is the raw tracked count; 'verified' is the deduped subset. "
                        "Layers marked provenance='public' unify third-party open data (HIFLD/FCC/EIA); "
                        "'curated' layers are crawled/curated by DC Hub. "
                        "'platform' lists owner-approved capability announcements. A card never "
                        "stores a figure: it carries metric.token plus the basis and the "
                        "source_url you can call to verify it, and metric.value is bound live at "
                        "request time from that same canonical source. A token with no live "
                        "keyless source stays value=null with metric.unmeasured_reason — never 0, "
                        "never a frozen literal. platform=null means the announcement source was "
                        "unavailable (see platform_unavailable_reason), NOT that nothing shipped.",
                   source="DC Hub (dchub.cloud), CC-BY-4.0")
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


def register_infra_growth(app):
    try:
        app.register_blueprint(infra_growth_bp)
    except Exception as e:
        print(f"[infra_growth] registration: {e}", flush=True)
