"""served_table_freshness.py — the dead-man board's view of tables with NO producer.

★★★ THE GAP (measured 2026-09-12)
================================
/api/v1/ops/deadman carries 210 feeds and every one is a PRODUCER beating the
ingest_runs ledger. A table whose producer was never built — or was left out on
purpose — emits no beat, so it cannot go red, and nothing on the board can say
it stopped being written. Of eleven tables known dead that day, TEN were absent
from the board entirely. One of them, metro_fiber_summary, was being served
live by get_metro_fiber with as_of 2026-03-17. That is the March 2026 cluster's
failure mode, still open: four datasets froze within four days and none was
restarted for five months while every signal stayed green.

WHAT THIS DOES
==============
Once a day (ingestion-integrity-tick.yml, admin-gated, Railway origin) it
measures the newest INGEST timestamp of every table that
contracts/dataset_inventory.json lists as (a) served — by a public route that
claims freshness, or by an MCP tool — and (b) having no live write path. It then
beats ONE feed, `served-table-freshness`, whose note names the frozen tables.
The class reaches the board through machinery that already alarms: the
off-worker watcher's dedup'd issue, red_triage, and every shell that reads red.
No reader of the board changes.

★ NOT A FOURTH REGISTRY. Three dataset registries already exist and their
intersection is zero. This hand-maintains nothing: it READS the derived
inventory, so a table leaves the candidate set as soon as a writer lands and
the baseline is regenerated. It is deliberately NOT added to
scripts/dataset_inventory._REGISTRIES — `watched_by` there means a registry
DECLARES the table, and letting a measurement satisfy it would shrink the
unwatched count without anyone declaring anything.

★ THE INVENTORY CHOOSES, THE DATABASE DECIDES. `live_write_paths: []` is a
static derivation, and it is wrong in both directions: eia_gas_prices is listed
with no live writer while `worker:eia_gas_prices` beats success on the board,
and a candidate with no writer may simply be a VIEW. So no table is ever called
frozen from the inventory alone. Its own newest row is measured, and a table
that measures fresh stays silent however the inventory classified it.

★ TIER IS NOT THE FILTER. metro_fiber_summary is tier 2 — DC Hub minted its
rows, so the inventory does not call it foreign — and it is the one freeze
verified live on 2026-09-12. A tier-1-only rule would have measured nine tables
and missed it.

★ ONLY INGEST-TIME COLUMNS COUNT, AND ONLY TYPED ONES. generator_retirements
carries retirement_date and construction_permits carries expiration_date:
business dates that can lie in the FUTURE and would read a dead table as fresh.
A column counts only when its NAME is a recognised ingest stamp AND its TYPE is
a timestamp or date. TEXT stamps are not cast — one malformed row breaks the
whole MAX (routes/infra_growth._FRESH_TEXT is the precedent). The newest of the
recognised columns wins, which errs toward fresh: an alarm here must be earned.

★ UNMEASURED IS NEVER GREEN. A candidate with no usable column, a query that
failed, a pass out of budget, or a relation that does not exist is published as
such and beats `tables_unmeasured` — a different word from `tables_frozen`, so
triage can tell "restore the writer" from "fix the measurement". `error` is
reserved for a producer that could not run at all (inventory unreadable, no
database): that is what ingestion_integrity's producer_liveness lane reads it to
mean (tests/test_shell_beat_status_vocabulary.py). Views and materialized views
are listed but kept out of the verdict: their freshness is their base tables',
and a scheduled MAX() through a view is an unbounded scan of those tables that
adds no signal a base-table measurement lacks.

Pure stdlib at import time — psycopg2 is imported inside run() — so every rule
above is testable in the pytest-only CI environment.

Kill: SERVED_TABLE_FRESHNESS_DISABLE=1 (the feed then goes LATE on the board,
which is how a disabled monitor should look).
"""
from __future__ import annotations

import datetime
import json
import re
import os
import time

FEED = "served-table-freshness"

# ingestion-integrity-tick.yml fires daily; 36 = 24 x 1.5, the house value for a
# daily producer (tools/deadman/watch.py): one missed fire never alarms, two do.
CADENCE_HOURS = 36

# A served table with no live producer is FROZEN once its newest ingest is older
# than this. 60 days = two missed MONTHLY publications, the slowest common loader
# cadence here (EIA-860M, gem-refresh), under the house rule of alarming at 2x
# cadence. A table with a hidden monthly writer the inventory cannot see stays
# green, and the March cluster (~180 days) is named on day 60 instead of never.
FROZEN_AFTER_DAYS = 60

# Recognised ingest-time column names. NAME and TYPE must both match — see the
# module docstring for why a business date (retirement_date, expiration_date,
# issue_date) never counts, however recent it looks.
INGEST_TS_COLUMNS = (
    "updated_at", "ingested_at", "loaded_at", "retrieved_at", "refreshed_at",
    "fetched_at", "scraped_at", "synced_at", "computed_at", "captured_at",
    "inserted_at", "created_at", "last_seen_at", "last_updated",
    "last_updated_at", "last_refreshed", "first_seen", "discovered_at",
    "occurred_at", "recorded_at", "collected_at", "proposed_at",
)

# relkinds whose rows are read. v = view, m = materialized view — listed, kept out
# of the verdict (see the docstring). Anything else is not a table.
_TABLE_RELKINDS = ("r", "p")
_VIEW_RELKINDS = ("v", "m")

_STATES = ("frozen", "fresh", "not_measured", "absent", "view")

PER_QUERY_TIMEOUT_MS = 5000
BUDGET_SECONDS = 45

# The HTTP beat handler caps a note at 280 characters (routes/ingest_runs.beat);
# record_beat does not. Bounded here so both paths would publish the same note.
NOTE_CAP = 280

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY_PATH = os.path.join(_ROOT, "contracts", "dataset_inventory.json")

BASIS = (
    "Candidates are the tables contracts/dataset_inventory.json lists as served "
    "(a public route claims freshness, or an MCP tool reads them) with no live "
    "write path. Each is measured by the newest of its recognised, typed "
    f"ingest-timestamp columns: frozen when older than {FROZEN_AFTER_DAYS} days, "
    "fresh otherwise. Views are listed but not judged. A candidate that could "
    "not be measured is reported as not measured, never as fresh."
)

CATALOG_SQL = (
    "SELECT c.relname, n.nspname, c.relkind, a.attname, "
    "format_type(a.atttypid, a.atttypmod) "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "LEFT JOIN pg_catalog.pg_attribute a "
    "ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
    "WHERE c.relname = ANY(%s) AND n.nspname = ANY(current_schemas(false)) "
    "ORDER BY array_position(current_schemas(false), n.nspname), c.relname"
)


def candidates_from_inventory(inv):
    """(candidates, None) or (None, reason). Never raises.

    A candidate is a table the inventory says is served — a public route claims
    its freshness, or an MCP tool reads it — and that has NO live write path.

    ★ SELF-CHECK, NOT A PINNED NUMBER. The inventory publishes its own
    tier1_no_live_writer list, and every name on it satisfies this rule by
    definition. If one is missing, this rule has drifted from the inventory's
    shape — that is UNMEASURED, never an empty (and therefore green) set."""
    if not isinstance(inv, dict) or not isinstance(inv.get("tables"), dict):
        return None, "inventory has no 'tables' map"
    anchor = inv.get("tier1_no_live_writer")
    if not isinstance(anchor, list):
        return None, "inventory has no tier1_no_live_writer list to check against"
    out = []
    for name, rec in inv["tables"].items():
        # A record without the key is malformed, not writer-less: skipping it
        # keeps a shape change from turning every served table into a candidate.
        if not isinstance(rec, dict) or "live_write_paths" not in rec:
            continue
        if rec.get("tier") == "write_only" or rec.get("live_write_paths"):
            continue
        served = {"freshness_claimed", "mcp_served"} & set(rec.get("why") or ())
        if not served:
            continue
        out.append({"table": name, "tier": rec.get("tier"), "why": sorted(served),
                    "mcp_tools": list(rec.get("mcp_tools") or []),
                    "claim_routes": list(rec.get("claim_routes") or []),
                    "zero_writer": bool(rec.get("zero_writer"))})
    out.sort(key=lambda c: c["table"])
    missing = sorted(set(anchor) - {c["table"] for c in out})
    if missing:
        return None, ("candidate rule drifted from the inventory: "
                      f"tier1_no_live_writer lists {missing} but none is a candidate")
    return out, None


def load_candidates(path=INVENTORY_PATH):
    """candidates_from_inventory() over the committed file. Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            inv = json.load(fh)
    except FileNotFoundError:
        return None, f"inventory not found at {path}"
    except Exception as e:  # noqa: BLE001 — malformed is UNMEASURED, reported by name
        return None, f"inventory unreadable: {type(e).__name__}"
    return candidates_from_inventory(inv)


def ingest_columns(columns):
    """The recognised ingest-time columns among [(name, sql_type)] whose TYPE is
    a timestamp or date, in INGEST_TS_COLUMNS order."""
    typed = set()
    for name, sql_type in columns or ():
        t = str(sql_type or "").lower()
        if t == "date" or t.startswith("timestamp"):
            typed.add(str(name))
    return [c for c in INGEST_TS_COLUMNS if c in typed]


def quote_ident(name):
    """A SQL identifier, double-quoted. Names reaching this come from pg_catalog."""
    return '"' + str(name).replace('"', '""') + '"'


def newest_sql(schema, table, columns):
    """One scan: the newest value across every recognised column."""
    exprs = ", ".join("MAX(" + quote_ident(c) + ")::timestamptz" for c in columns)
    return ("SELECT GREATEST(" + exprs + ") FROM "
            + quote_ident(schema) + "." + quote_ident(table))


def catalog(cur, names):
    """{table: {schema, relkind, columns: [(name, type)]}} for names that exist.

    Keeps the FIRST schema on the search_path per name — the relation an
    unqualified read in the serving code actually hits."""
    cur.execute(CATALOG_SQL, (list(names),))
    out = {}
    for relname, nspname, relkind, attname, attype in cur.fetchall():
        entry = out.get(relname)
        if entry is None:
            entry = out[relname] = {"schema": nspname, "relkind": relkind, "columns": []}
        elif entry["schema"] != nspname:
            continue
        if attname is not None:
            entry["columns"].append((attname, attype))
    return out


# ── the feed's own declared cadence ──────────────────────────────────
#
# ★★ 2x THE DECLARED INTERVAL, because that is the rule this repo already uses
# — "overdue at 2x cadence", the dead-man ledger's threshold, and the one
# tools/qa_superuser/probe_data applies when it cross-examines these same feeds.
# Nothing is invented: the threshold is the feed's OWN promise, so a verdict
# here is the feed contradicting itself rather than us imposing a number.
#
# ★ The parser is deliberately a SECOND implementation rather than an import:
# the probe runs on a GH Actions runner from the repo checkout and must not
# depend on the deployed backend, and the backend must not import tools/. They
# are held together by test_interval_parsers_agree, which runs both over one
# table of cases including every interval string the live feeds publish.
STALE_AT_MULTIPLE_OF_CADENCE = 2.0

_INTERVAL_RE = re.compile(r"(\d+)\s*(minute|hour|day|week)", re.I)
_WORD_INTERVALS = {"hourly": 1, "daily": 24, "weekly": 168, "monthly": 720}
_UNIT_HOURS = {"minute": 1 / 60, "hour": 1, "day": 24, "week": 168}


def parse_interval_hours(text):
    """A feed's own `refresh_interval` prose as hours, or None.

    None means the feed states no checkable cadence ("on-demand",
    "real-time (DB counts)"). That is not a default to paper over — with no
    promise there is nothing to be late against, so the caller must report
    `unknown` rather than `healthy`.
    """
    if not isinstance(text, str):
        return None
    m = _INTERVAL_RE.search(text)
    if m:
        return int(m.group(1)) * _UNIT_HOURS[m.group(2).lower()]
    for word, hours in _WORD_INTERVALS.items():
        if word in text.lower():
            return float(hours)
    return None


def feed_health_fields(col, newest, count, interval=None, now=None):
    """The freshness fields + health verdict for one feed. Pure.

    ★★★ THE RULE, IN ONE PLACE: `healthy` requires EVIDENCE a caller can check.
      • rows + a measured timestamp → healthy, and the timestamp is published so
        the declared refresh_interval can finally be compared against something.
      • rows + no measurable freshness → `unknown`. Not `healthy`: that is a
        claim nobody can falsify, which is what put six feeds on the QA board.
        Not `stale` either — unmeasured is not broken.
      • no rows → `stale`, regardless. An empty served table is a real alarm
        whether or not we can date it.

    ★★ THE KEY SET IS FIXED, INCLUDING THE NULLS, AND THAT IS NOT COSMETIC.
      `newest_record` was omitted when unmeasured, on the reasoning that a null
      reads as measured-and-empty. The response-key contract guard then failed
      the change as UNMEASURED: a conditional key makes the whole level dynamic
      and the endpoint drops out of coverage entirely. Trading a guard going
      dark for a slightly tidier envelope is the wrong way round — and the null
      is not ambiguous anyway, because `freshness_source: "none"` sits beside
      it saying exactly why. `markets` already publishes `last_updated: None`
      the same way.
    """
    if col and newest is not None:
        iso = newest.isoformat() if hasattr(newest, "isoformat") else str(newest)
        return {"freshness_source": col, "last_updated": iso,
                "newest_record": iso,
                "health": _timed_health(newest, count, interval, now)}
    return {"freshness_source": "none", "last_updated": None,
            "newest_record": None,
            "health": "unknown" if count > 0 else "stale"}


def _timed_health(newest, count, interval, now):
    """`healthy` only while the feed is inside its OWN promised cadence.

    ★★★ PUBLISHING THE TIMESTAMP WAS NOT ENOUGH. After the previous change this
    endpoint served `transactions: health 'healthy', last_updated 2026-07-27`
    against a declared `5 minutes (via autopilot)` refresh — 54 days late, still
    called healthy, with the contradicting evidence sitting in the same object.
    Only the off-box probe called it stale. An endpoint holding BOTH numbers and
    declining to compare them is the same "claim nobody checked" one level up.
    """
    if count <= 0:
        return "stale"
    hours = parse_interval_hours(interval)
    if hours is None or hours <= 0:
        # No stated cadence, so nothing to be late against. Evidence without a
        # promise cannot be called healthy.
        return "unknown"
    utc = datetime.timezone.utc
    ts = newest
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=utc)
    ref = now or datetime.datetime.now(utc)
    if getattr(ref, "tzinfo", None) is None:
        ref = ref.replace(tzinfo=utc)
    age_h = (ref - ts).total_seconds() / 3600.0
    return "stale" if age_h > hours * STALE_AT_MULTIPLE_OF_CADENCE else "healthy"


def table_freshness(cur, table, rollback=None):
    """(column, newest_at) for one table's newest INGEST timestamp, or (None, None).

    ★★★ SHARED SO THE HEALTH CLAIM AND THE FREEZE DETECTOR CANNOT DISAGREE.
    /api/health/data-freshness derived `health: 'healthy' if row_count > 0` for
    six of its nine feeds — a row count is a measure of PRESENCE, not freshness,
    so a feed whose producer died months ago still read healthy while its
    declared `refresh_interval: '6 hours'` was never checked against anything.
    This module already knows how to answer the real question; it just was not
    reachable from there.

    Same rules as the sweep: only recognised ingest-time columns, only when the
    COLUMN TYPE is a timestamp or date (a `construction_permits.expiration_date`
    is about the permit, not about when we fetched it), and the first schema on
    the search_path — the relation an unqualified read actually hits.

    Never raises: any failure degrades to (None, None), which the caller must
    report as `unknown` rather than as healthy.

    ★★ `rollback` IS NOT OPTIONAL WHEN THE CURSOR IS SHARED. A failed query
    aborts the whole transaction, so without it every LATER read on that cursor
    dies with "current transaction is aborted" and each remaining feed cascades
    to 0/stale — the #1683 regression that /api/health/data-freshness already
    guards against in its own safe_query. Swallowing the exception here without
    rolling back would reintroduce it through a new door.
    """
    try:
        info = (catalog(cur, [table]) or {}).get(table)
        if not info or info.get("relkind") not in _TABLE_RELKINDS:
            return None, None
        cols = ingest_columns(info.get("columns"))
        if not cols:
            return None, None
        cur.execute(newest_sql(info["schema"], table, cols))
        row = cur.fetchone()
        return (cols[0], row[0]) if row and row[0] is not None else (None, None)
    except Exception:  # noqa: BLE001 - a miss is "unknown", never "healthy"
        if rollback is not None:
            try:
                rollback()
            except Exception:  # noqa: BLE001
                pass
        return None, None


def classify_age(newest_at, now, frozen_after_days=FROZEN_AFTER_DAYS):
    """('frozen' | 'fresh', age_days) for a table's newest ingest timestamp."""
    if newest_at.tzinfo is None:
        newest_at = newest_at.replace(tzinfo=datetime.timezone.utc)
    age = max(0.0, (now - newest_at).total_seconds() / 86400.0)
    return ("frozen" if age > frozen_after_days else "fresh"), round(age, 1)


def measure(cur, candidates, now, budget_seconds=BUDGET_SECONDS,
            per_query_ms=PER_QUERY_TIMEOUT_MS, clock=time.monotonic):
    """One result per candidate. A single table's failure never ends the pass.

    Each probe runs inside its own SAVEPOINT: a failed statement aborts the
    enclosing transaction, and without the savepoint every later table in the
    pass would fail with it and be reported as unmeasurable for a reason that
    was never its own."""
    started = clock()
    cur.execute("SET LOCAL statement_timeout = " + str(int(per_query_ms)))
    cat = catalog(cur, [c["table"] for c in candidates])
    results = []
    for cand in candidates:
        res = dict(cand)
        entry = cat.get(cand["table"])
        if entry is None:
            res.update(state="absent", reason="no such relation on the search_path, "
                       "though code serves it")
        elif entry["relkind"] in _VIEW_RELKINDS:
            res.update(state="view", reason="a view's freshness is its base tables'; "
                       "listed, not judged")
        elif entry["relkind"] not in _TABLE_RELKINDS:
            res.update(state="not_measured",
                       reason=f"relkind {entry['relkind']!r} is not a table")
        else:
            cols = ingest_columns(entry["columns"])
            if not cols:
                res.update(state="not_measured",
                           reason="no recognised, typed ingest-timestamp column")
            elif clock() - started > budget_seconds:
                res.update(state="not_measured",
                           reason=f"measurement budget of {budget_seconds}s ran out first")
            else:
                res["columns"] = cols
                try:
                    cur.execute("SAVEPOINT stf_probe")
                    cur.execute(newest_sql(entry["schema"], cand["table"], cols))
                    row = cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT stf_probe")
                except Exception as e:  # noqa: BLE001 — one table, reported by name
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT stf_probe")
                    except Exception:  # noqa: BLE001
                        pass
                    res.update(state="not_measured", reason=f"query failed: {type(e).__name__}")
                else:
                    newest = row[0] if row else None
                    if newest is None:
                        res.update(state="not_measured",
                                   reason="no row carries an ingest timestamp")
                    else:
                        state, age = classify_age(newest, now)
                        res.update(state=state, newest_ingest_at=newest.isoformat(),
                                   age_days=age)
        results.append(res)
    return results


def _bounded_note(head, items, cap=NOTE_CAP):
    """`head | a, b, c +N more`, never longer than cap."""
    if not items:
        return head[:cap]
    shown = []
    for item in items:
        rest = len(items) - len(shown) - 1
        trial = head + " | " + ", ".join(shown + [item]) + (f" +{rest} more" if rest else "")
        if len(trial) > cap:
            break
        shown.append(item)
    rest = len(items) - len(shown)
    body = ", ".join(shown) if shown else ""
    return (head + " | " + body + (f" +{rest} more" if rest else "")).replace("|  +", "| +")[:cap]


def summarize(results):
    """(status, note, counts). status is the word the ledger will carry.

    A state this module does not know is counted as not measured — fail-closed,
    so a future state can never fall through to `success`."""
    groups = {s: [] for s in _STATES}
    for r in results:
        state = r.get("state") if r.get("state") in _STATES else "not_measured"
        groups[state].append(r)
    counts = {s: len(groups[s]) for s in _STATES}
    frozen = sorted(groups["frozen"], key=lambda r: (-(r.get("age_days") or 0), r.get("table")))
    if frozen:
        status = "tables_frozen"
    elif counts["not_measured"] or counts["absent"]:
        status = "tables_unmeasured"
    else:
        status = "success"
    head = (f"frozen {counts['frozen']} · not measured {counts['not_measured']} · "
            f"absent {counts['absent']} · fresh {counts['fresh']} · views {counts['view']}"
            f" — {len(results)} served tables with no live writer")
    if frozen:
        items = [f"{r['table']} {int(r.get('age_days') or 0)}d" for r in frozen]
    else:
        items = [r["table"] for r in groups["not_measured"] + groups["absent"]]
    return status, _bounded_note(head, items), counts


def run(dsn, now=None, inventory_path=INVENTORY_PATH, connect=None):
    """Measure every candidate and return the publishable result. Never raises.

    `status` is one of success | tables_frozen | tables_unmeasured | error, and
    `measured` is how many candidates were actually timed (frozen + fresh) — the
    caller beats it as rows, so a pass that measured nothing three times running
    also trips the board's zero-row rule."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = {"ok": False, "feed": FEED, "generated_at": now.isoformat(),
           "frozen_after_days": FROZEN_AFTER_DAYS, "basis": BASIS}
    if os.environ.get("SERVED_TABLE_FRESHNESS_DISABLE") == "1":
        out.update(ok=True, disabled=True, status=None, note=None, measured=None)
        return out
    candidates, why = load_candidates(inventory_path)
    if candidates is None:
        out.update(status="error", error=why, measured=None,
                   note=("UNMEASURED: " + why)[:NOTE_CAP])
        return out
    out["candidates"] = len(candidates)
    if not dsn:
        out.update(status="error", error="no DATABASE_URL", measured=None,
                   note="UNMEASURED: no DATABASE_URL")
        return out
    if connect is None:
        import psycopg2

        def connect():
            return psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
    try:
        conn = connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    results = measure(cur, candidates, now)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — the producer could not run: `error`, by name
        out.update(status="error", error=str(e)[:200], measured=None,
                   note=f"UNMEASURED: database read failed ({type(e).__name__})")
        return out
    status, note, counts = summarize(results)
    out.update(ok=True, status=status, note=note, counts=counts,
               measured=counts["frozen"] + counts["fresh"], tables=results)
    return out
