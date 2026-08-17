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

import ast
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


_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _module_string_consts(src):
    """Module-level `NAME = "..."` string assignments, as {name: value}.

    Adjacent string literals are merged by the parser, so the parenthesised
    multi-line form used for these column lists arrives as one Constant.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                consts[target.id] = node.value.value
    return consts


def _writers():
    """(relpath, column list, following 400 chars) per capacity_pipeline INSERT.

    An f-string writer yields a literal `{_CAP_SYNC_COLS}` token where its
    columns should be. Resolve it from the module's own constants rather than
    dropping it: a skipped placeholder would leave that writer's real columns
    unchecked, which is precisely the vacuous-guard failure the rest of this
    file exists to prevent. An unresolvable placeholder is kept verbatim so
    the caller fails on it loudly.
    """
    found = []
    for rel, src in _sources():
        consts = None
        for m in _INSERT_RE.finditer(src):
            cols = []
            for raw in m.group(1).split(","):
                tok = raw.strip().strip('"')
                ph = _PLACEHOLDER_RE.match(tok)
                if ph:
                    if consts is None:
                        consts = _module_string_consts(src)
                    expanded = consts.get(ph.group(1))
                    if expanded is not None:
                        cols.extend(c.strip().strip('"').lower()
                                    for c in expanded.split(",") if c.strip())
                        continue
                if tok:
                    cols.append(tok.lower())
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


def test_the_interpolated_writer_is_read_not_waved_through():
    """Resolution must EXPAND the constant — the allowlist shortcut stays red.

    The test above resolves `{_CAP_SYNC_COLS}` so crawler_scheduler's real
    columns get checked. But the cheap way to green that test, the next time it
    fails, is to add `{_cap_sync_cols}` to _LIVE_COLUMNS and move on. Measured
    against this file as it stood at #2812: with that one token allowlisted and
    _module_string_consts returning {}, a genuinely dead column (`company`)
    added INSIDE _CAP_SYNC_COLS rode through with the suite fully green — the
    guard was reading a variable name, and ten real columns went unchecked.
    That is the vacuous-guard failure the rest of this file exists to prevent,
    so pin the expansion itself rather than trusting the column check alone.
    """
    by_file = {}
    for rel, cols, _ in _writers():
        by_file.setdefault(rel, []).extend(cols)

    for rel, cols in by_file.items():
        leftover = [c for c in cols if "{" in c or "}" in c]
        assert not leftover, (
            f"{rel}: an unresolved placeholder reached the column list "
            f"({leftover}), so this guard is checking a variable name and not "
            "the columns the writer sends. Resolve it in _writers — do NOT "
            "add the token to _LIVE_COLUMNS, which greens the check by "
            "hiding every column behind the constant.")

    cols = by_file.get("crawler_scheduler.py")
    assert cols, (
        "crawler_scheduler.py no longer INSERTs INTO capacity_pipeline. If the "
        "facility→pipeline sync moved, move this pin with it: it is the only "
        "writer that builds its column list through a constant, so it is what "
        "keeps the resolution path exercised at all.")
    assert len(cols) >= 5 and {"operator", "capacity_mw", "data_flag"} <= set(cols), (
        "the interpolated column list did not expand — _CAP_SYNC_COLS resolved "
        f"to something that is not the writer's real vocabulary: {cols}")


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
