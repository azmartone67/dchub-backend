"""A capacity_pipeline writer must name columns the table actually has.

Two writers into `capacity_pipeline` were removed on 2026-08-17 because
neither could execute. Both had been dead since they were written, and
both were invisible for the same reason: the failure happens inside a
try/except that swallows it, so the job returns its zero-initialised
result dict and the caller logs a success.

  · global_intelligence_agent.track_capacity_pipeline drove off
    `WHERE discovered_at > datetime('now', '-30 days')`. datetime() is
    SQLite. Executed against the live Neon Postgres it raises
    UndefinedFunction: function datetime(unknown, unknown) does not
    exist — the cursor errors before the INSERT is reached. It ran twice
    a day (dchub-scheduler 'global_intel', 06:00 and 18:00) doing
    nothing. extractor_cron.insert_capacity already extracts capacity
    from the same `announcements` table, and does it better: it keys
    source_announcement_id so the partial unique index dedups, probes
    for cross-article twins, and stamps the quarantine taxonomy. The
    dead writer was a worse duplicate, so it was deleted rather than
    repaired.

  · pipeline_drafts_api.approve_draft INSERTed company, project,
    investment_millions, expected_delivery, type and preleased — six
    columns that do not exist — plus `ON CONFLICT (company)`, which has
    no matching unique index even after company→operator. It was a
    standalone Flask app nothing imports and no service runs, and
    `pipeline_drafts` holds zero rows in every draft_status, so the path
    had never promoted anything. Deleted.

WHAT THESE GUARDS PIN
  1. The two writers stay gone — a deletion nothing enforces comes back.
  2. THE CLASS, NOT THE INSTANCE — every surviving `INSERT INTO
     capacity_pipeline` names only columns the live table has, and every
     ON CONFLICT names an arbiter that a unique index can actually
     satisfy. Written on the day approve_draft shipped, guard 2 would
     have failed it on both counts.

The live column set and the unique arbiters are PINNED constants,
measured against the Neon read replica on 2026-08-17 — CI runs with no
DATABASE_URL and must not need one. If the table gains a column, add it
here; that is a schema decision, not a refactor.

Every helper asserts it FOUND its target first: an empty scan satisfies
every "not in" below. Nothing runs at module scope.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# information_schema.columns for public.capacity_pipeline, read from the
# Neon replica 2026-08-17. Ordinal order preserved for readability.
_LIVE_COLUMNS = frozenset({
    "id", "operator", "market", "region", "capacity_mw", "phase", "status",
    "announcement_date", "completion_date", "source", "source_url", "notes",
    "created_at", "confidence_score", "confidence_label", "country",
    "source_announcement_id", "extraction_confidence", "extracted_via",
    "extracted_at", "data_flag",
})

# pg_indexes WHERE tablename='capacity_pipeline', same read. An ON CONFLICT
# column list must match one of these exactly, or Postgres raises
# InvalidColumnReference: there is no unique or exclusion constraint
# matching the ON CONFLICT specification.
_UNIQUE_ARBITERS = (
    frozenset({"id"}),                                    # _pkey
    frozenset({"operator", "market", "phase", "capacity_mw"}),
    frozenset({"source_announcement_id"}),                # partial, WHERE NOT NULL
)

_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__",
              "tests", "site-packages", "build", "dist"}

_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+capacity_pipeline\s*\(([^)]*)\)", re.IGNORECASE)
_CONFLICT_RE = re.compile(r"ON\s+CONFLICT\s*\(([^)]*)\)", re.IGNORECASE)


def _sources():
    """Every non-test .py in the repo, as (relpath, text)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    out.append((os.path.relpath(path, ROOT), f.read()))
            except (OSError, UnicodeDecodeError):
                continue
    return out


def _writers():
    """(relpath, column list, following 400 chars) per capacity_pipeline INSERT."""
    found = []
    for rel, src in _sources():
        for m in _INSERT_RE.finditer(src):
            cols = [c.strip().strip('"').lower()
                    for c in m.group(1).split(",") if c.strip()]
            found.append((rel, cols, src[m.end():m.end() + 400]))
    return found


# ── 1 · the deletions stay deleted ───────────────────────────────────

def test_the_two_dead_writers_are_gone():
    assert not os.path.exists(os.path.join(ROOT, "pipeline_drafts_api.py")), (
        "pipeline_drafts_api.py is back. It is a standalone Flask app no "
        "service runs, its approve_draft INSERT names six columns "
        "capacity_pipeline does not have, and its ON CONFLICT (company) has "
        "no matching unique index. If it is genuinely wanted, it needs the "
        "live vocabulary (operator/market/phase/capacity_mw) and a real "
        "arbiter — not a restore.")

    gia = os.path.join(ROOT, "global_intelligence_agent.py")
    assert os.path.exists(gia), "global_intelligence_agent.py is missing"
    with open(gia, encoding="utf-8") as f:
        src = f.read()
    assert "INSERT INTO capacity_pipeline" not in src, (
        "global_intelligence_agent writes capacity_pipeline again. "
        "extractor_cron.insert_capacity already covers the announcements "
        "source, with dedup and the quarantine stamp; a second extractor "
        "over the same rows races it on UNIQUE (operator, market, phase, "
        "capacity_mw) and injects operator='Unknown' rows.")
    assert "track_capacity_pipeline" not in src, (
        "track_capacity_pipeline is back in global_intelligence_agent.")


def test_nothing_calls_the_removed_writer():
    hits = [rel for rel, src in _sources()
            if "agent.track_capacity_pipeline" in src]
    assert not hits, (
        f"{hits} calls agent.track_capacity_pipeline, which no longer "
        "exists — this raises AttributeError at runtime.")


# ── 2 · the class: only real columns, only real arbiters ─────────────

def test_every_capacity_pipeline_insert_names_only_live_columns():
    writers = _writers()
    assert len(writers) >= 3, (
        "the writer scan found almost nothing — it stopped matching, and an "
        f"empty scan passes every assertion below: {[w[0] for w in writers]}")

    bad = []
    for rel, cols, _ in writers:
        assert cols, f"{rel}: matched an INSERT but parsed no column list"
        phantom = [c for c in cols if c not in _LIVE_COLUMNS]
        if phantom:
            bad.append((rel, phantom))
    assert not bad, (
        f"these INSERTs name columns capacity_pipeline does not have: {bad}. "
        "Every call raises UndefinedColumn, and because these writers sit "
        "inside a swallowing try/except the job still reports success — "
        "which is exactly how approve_draft stayed dead from the day it was "
        "written. Live vocabulary: " + ", ".join(sorted(_LIVE_COLUMNS)))


def test_every_capacity_pipeline_on_conflict_names_a_real_arbiter():
    writers = _writers()
    assert len(writers) >= 3, "the writer scan found almost nothing"

    checked, bad = 0, []
    for rel, _, tail in writers:
        m = _CONFLICT_RE.search(tail)
        if not m:
            continue          # bare ON CONFLICT DO NOTHING, or none at all
        checked += 1
        arbiter = frozenset(c.strip().strip('"').lower()
                            for c in m.group(1).split(",") if c.strip())
        if arbiter not in _UNIQUE_ARBITERS:
            bad.append((rel, sorted(arbiter)))
    assert checked, (
        "no ON CONFLICT (...) was parsed from any writer — the regex stopped "
        "matching, so this guard is vacuous.")
    assert not bad, (
        f"these ON CONFLICT clauses name an arbiter with no unique index: "
        f"{bad}. Postgres raises InvalidColumnReference. Real arbiters: "
        + "; ".join("(" + ", ".join(sorted(a)) + ")" for a in _UNIQUE_ARBITERS))
