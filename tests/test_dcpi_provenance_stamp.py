"""r-provenance-writer (2026-08-08) — a published DCPI score must name the
method that produced it.

THE BUG: util/dcpi_method.py commits publicly, at /api/v1/dcpi/methodology, to
"method_version stamped on every score row and daily snapshot". Measured on the
live table 2026-08-08 09:24 UTC, that was false for 8 published markets:

    laurel MD, lenoir NC, luckey OH, maiden NC,
    modesto CA, monroe NC, salem OR, west-chester OH

all written 08:51 UTC carrying constraint_score, excess_power_score and verdict
with method_version, signal_tier and data_basis_json all NULL — published,
scored, publicly served, unattributable. They are exactly the 8 rows
dcpi_method 2.1.1 calls "new to the table" under r-universe-dedup: no prior row
existed, so the gap-filling writer inserted them fresh.

THE WRITER was routes/dcpi_freshness_watchdog.py::recompute_missing_markets
(cron `37 2,8,14,20`, i.e. 08:37 + GitHub Actions scheduling delay). Attributed
by column fingerprint, not by assumption — the four candidate paths write
disjoint column sets:

    tier_required    DEFAULT 'free'; all three lite writers set 'lite-pro'
                     explicitly, and the 8 rows were 'free'
    published        DEFAULT false; force_recompute_market omits it, so its
                     fresh rows are invisible — the 8 were published=true
    top_risks_json   written only by the full-scorer paths; present on all 8

Leaving exactly one path that inserts published=TRUE without the triple.

WHY IT MATTERS: DCPI_METHOD_VERSION moved twice on 2026-08-08 (2.1.0
r-universe-dedup, 2.2.0 r-radius-dedup), both score-moving. A NULL
method_version means a consumer cannot tell which of those produced a row —
precisely the question the stamp exists to answer. NULL signal_tier is
separately documented to mean "unknown", not "low", so readers surface these
rows as unknown-confidence.

WHY IT DRIFTED: six hand-written INSERT column lists for one table. Only the
one in routes/dcpi.py was updated when data_basis_json (r65), signal_tier
(r-ws3-signal-tier) and method_version (r-ws3-methodology) were added, because
nothing connected the others to it. Same bug class as r-ws3-methodology's own
hand-copied weights and as _SQL_FOOTPRINT_DEDUP's "no third definition" note.

WHAT IS PINNED HERE:
  1. CENSUS — no module may write a scored row without stamping all three. Read
     out of the AST, so a column that appears only in a comment cannot satisfy
     it, and f-string holes are rendered so a predicate reaching the SQL through
     a shared constant still counts. This is the test that fails if a SEVENTH
     hand-copy appears.
  2. BEHAVIORAL — the shared writer is executed and its bound parameters are
     inspected, so the triple is asserted as a VALUE that reaches the driver in
     the right position, not as a substring of a query.
  3. REFUSAL — writing a score with no method_version raises rather than
     silently publishing an unattributable row.

SCOPE NOTE, deliberately: dchub_self_heal.py rewrites `verdict` on PUBLISHED
rows from four different, mutually inconsistent, hand-copied band tables, none
of which matches util/dcpi_method.VERDICT_BANDS. That is very likely the
mechanism behind the 67% band mismatch dcpi_method's own docstring reports, and
it is a real defect — but it is a VERDICT-bands defect, not a provenance one,
and choosing the canonical band set is a scoring decision that needs a version
bump and an owner. The census below keys on statements that BIND a score
(`constraint_score=%s`), so those verdict rewrites are out of its scope by
construction rather than by oversight. Tracked separately.
"""
import ast
import os

import pytest


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_SKIP_DIRS = {".git", "tests", "node_modules", "__pycache__", ".venv", "venv",
              ".pytest_cache", "site-packages", ".claude"}


def _candidate_files():
    """Every repo .py that so much as mentions the table.

    Filtering on the text first keeps this off the ~600 unrelated modules and
    means an unrelated syntax error elsewhere in the repo cannot turn this
    guard into a blind pass.
    """
    root = _repo_root()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "market_power_scores" in text:
                yield path, text


def _sql_strings(tree):
    """(lineno, sql) for every string literal / f-string in a parsed module.

    An f-string hole is rendered as its SOURCE EXPRESSION, not evaluated: the
    biggest score writer lives in main.py and this suite may never import
    main.py (it opens DB pools, starts keepalive threads and registers ~200
    blueprints). Rendering the name is enough here because the only hole that
    has to be recognised is the shared lite guard, and _lite_guard_aliases
    resolves what that name is bound to straight from the same AST.
    """
    claimed, out = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            buf = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    claimed.add(id(v))
                    buf.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    claimed.add(id(v))
                    buf.append(f" {ast.unparse(v.value)} ")
            out.append((node.lineno, "".join(buf)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in claimed):
            out.append((node.lineno, node.value))
    return out


def _lite_guard_aliases(tree):
    """Names bound to LITE_MAY_NOT_CLOBBER_FULL in this module, from its AST."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "dcpi_score_row"):
            for alias in node.names:
                if alias.name == "LITE_MAY_NOT_CLOBBER_FULL":
                    names.add(alias.asname or alias.name)
    return names


def _binds_a_score(sql):
    """True when the statement writes a score the caller computed.

    Two shapes, and only two:
      UPDATE ... SET constraint_score=%s        — a scorer's output
      INSERT INTO market_power_scores (... constraint_score ...)

    Deliberately NOT "mentions constraint_score". dchub_self_heal.py repairs
    rows with `constraint_score = COALESCE(constraint_score, 0)` and gates
    others on `WHERE COALESCE(constraint_score,0) = 0`; neither produces a
    score, neither can name a method, and demanding a stamp from them would be
    demanding an invented one.
    """
    low = " ".join(sql.lower().split())
    if "market_power_scores" not in low:
        return False
    if "insert into market_power_scores" in low:
        head = low.split("insert into market_power_scores", 1)[1]
        head = head.split("values", 1)[0]
        return "constraint_score" in head
    if "update market_power_scores" in low:
        return "constraint_score=%s" in low or "constraint_score =%s" in low
    return False


def _score_writers():
    """(path, lineno, sql, lite_aliases) for every score-writing statement."""
    found = []
    for path, text in _candidate_files():
        tree = ast.parse(text)
        aliases = _lite_guard_aliases(tree)
        for lineno, sql in _sql_strings(tree):
            if _binds_a_score(sql):
                found.append((path, lineno, sql, aliases))
    return found


# ---------------------------------------------------------------------------
# 1. CENSUS
# ---------------------------------------------------------------------------

def test_every_score_writing_statement_is_attributable():
    """★ The guard. A statement that writes a score must either stamp all three
    provenance columns, or be a lite writer fenced out of full-method rows.

    A seventh hand-copied INSERT fails here — which is the entire point, since
    six hand-copies are how three columns went missing from five of them.
    """
    from util.dcpi_score_row import (LITE_MAY_NOT_CLOBBER_FULL,
                                     PROVENANCE_COLUMNS)

    writers = _score_writers()
    assert writers, "census found no score writers at all — it has gone blind"

    unattributable = []
    for path, lineno, sql, aliases in writers:
        low = sql.lower()
        if all(col in low for col in PROVENANCE_COLUMNS):
            continue
        fenced = LITE_MAY_NOT_CLOBBER_FULL.lower() in low or any(
            a in sql for a in aliases)
        if not fenced:
            unattributable.append(
                f"{os.path.relpath(path, _repo_root())}:{lineno}")

    assert not unattributable, (
        "score written with neither a provenance stamp nor the lite fence: "
        + ", ".join(sorted(unattributable))
    )


def test_the_lite_fence_has_exactly_one_definition():
    """The fence is imported by three modules. If any of them retypes the
    predicate instead, this whole file starts asserting on a copy — the
    original failure mode, one level up.
    """
    hits = []
    for path, text in _candidate_files():
        if path.endswith(os.path.join("util", "dcpi_score_row.py")):
            continue
        if "method_version IS NULL" in text:
            hits.append(os.path.relpath(path, _repo_root()))
    assert not hits, f"lite fence predicate retyped in: {hits}"


def test_the_full_scorers_do_not_carry_their_own_statement():
    """routes/dcpi.py and routes/dcpi_freshness_watchdog.py held four
    hand-copies of the scored-row write between them. They must now hold none:
    every one goes through util/dcpi_score_row.
    """
    for rel in ("routes/dcpi.py", "routes/dcpi_freshness_watchdog.py"):
        path = os.path.join(_repo_root(), *rel.split("/"))
        tree = ast.parse(open(path, encoding="utf-8").read())
        local = [ln for ln, sql in _sql_strings(tree)
                 if _binds_a_score(sql)
                 and "lite-pro" not in sql.lower()]
        assert not local, (
            f"{rel} grew a local scored-row statement again at line(s) {local}")
        assert "dcpi_score_row" in open(path, encoding="utf-8").read(), \
            f"{rel} no longer imports the shared writer"


# ---------------------------------------------------------------------------
# 2. BEHAVIORAL
# ---------------------------------------------------------------------------

class _FakeCursor:
    """Records what would reach psycopg2. rowcount is scripted per test."""

    def __init__(self, rowcounts):
        self._rowcounts = list(rowcounts)
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 0


def _metrics(**over):
    """The shape gather_metrics_for_market returns, trimmed to what the writer
    reads. method_version/signal_tier/data_basis are what it stamps."""
    m = {
        "queue_capacity_mw": 1200.0, "queue_wait_months": 41,
        "reserve_margin_pct": 17.5, "gen_additions_12mo_mw": 640.0,
        "curtailment_pct": 2.1, "stranded_capacity_mw": 88.0,
        "emergency_count_30d": 0,
        "signal_tier": "full",
        "method_version": "9.9.9",
        "data_basis": {"data_basis": "live", "method_version": "9.9.9"},
    }
    m.update(over)
    return m


_MARKET = ("luckey", "Luckey", "OH", "PJM", 41.45, -83.48)


def _bound(sql, params, column):
    """The value bound to `column` in a statement generated from _COLUMNS."""
    from util.dcpi_score_row import _COLUMNS
    return params[_COLUMNS.index(column)]


def test_a_fresh_published_row_carries_the_triple():
    """The exact 08:51 path: no existing row, insert, published. Asserted on the
    bound VALUES, so a column list that merely mentions method_version while
    binding it somewhere else still fails."""
    from util.dcpi_score_row import upsert_scored_market

    cur = _FakeCursor([0])          # UPDATE matches nothing → INSERT
    out = upsert_scored_market(cur, _MARKET, _metrics(), 61.2, 44.8, 38,
                               "CAUTION", ["r"], ["o"], publish=True)
    assert out == "inserted"
    sql, params = cur.calls[-1]
    assert "INSERT INTO market_power_scores" in sql
    assert "published" in sql and "TRUE" in sql

    assert _bound(sql, params, "method_version") == "9.9.9"
    assert _bound(sql, params, "signal_tier") == "full"
    assert '"data_basis": "live"' in _bound(sql, params, "data_basis_json")
    # and the row is genuinely a scored one, not a stub
    assert _bound(sql, params, "constraint_score") == 61.2
    assert _bound(sql, params, "verdict") == "CAUTION"


def test_rescoring_an_existing_row_restamps_it():
    """The stale-stamp half: an UPDATE that refreshes scores must refresh the
    version too, or the row advertises a method that did not produce it."""
    from util.dcpi_score_row import update_scored_market

    cur = _FakeCursor([1])
    assert update_scored_market(cur, _MARKET, _metrics(method_version="2.2.0"),
                                61.2, 44.8, 38, "CAUTION", ["r"], ["o"]) == 1
    sql, params = cur.calls[-1]
    assert sql.startswith("UPDATE market_power_scores")
    assert "method_version=%s" in sql
    assert _bound(sql, params, "method_version") == "2.2.0"


def test_a_dynamic_markets_null_centroid_cannot_wipe_a_stored_one():
    """r-market-resolve-guard: _load_markets_dynamic emits None coords for the
    ~200 dynamic markets. The watchdog copy bound `latitude=%s` with no
    COALESCE — verbatim the regression that file's own sentinel exists to
    catch. Shared writer, so it is fixed for every caller at once."""
    from util.dcpi_score_row import update_scored_market

    cur = _FakeCursor([1])
    dynamic = ("luckey", "Luckey", "OH", "PJM", None, None)
    update_scored_market(cur, dynamic, _metrics(), 1.0, 2.0, 3,
                         "CAUTION", [], [])
    sql, _ = cur.calls[-1]
    assert "latitude=COALESCE(%s, latitude)" in sql
    assert "longitude=COALESCE(%s, longitude)" in sql


def test_iso_type_is_derived_not_dropped():
    """Two of the four copies never wrote iso_type at all, so a market first
    created by the gap-filler had no taxonomy label until the next daily
    sweep. Derived from iso here, so the two cannot drift apart."""
    from util.dcpi_score_row import update_scored_market

    cur = _FakeCursor([1])
    update_scored_market(cur, _MARKET, _metrics(), 1.0, 2.0, 3, "CAUTION", [], [])
    sql, params = cur.calls[-1]
    assert _bound(sql, params, "iso_type") == "RTO"       # PJM


# ---------------------------------------------------------------------------
# 3. REFUSAL
# ---------------------------------------------------------------------------

_ABSENT = object()


@pytest.mark.parametrize("version", [_ABSENT, "", None],
                         ids=["key-missing", "empty-string", "explicit-none"])
def test_a_score_with_no_method_version_is_refused(version):
    """gather_metrics_for_market sets method_version unconditionally, so a
    missing one means the caller did not run the real scorer. Raising surfaces
    the market in the caller's error_notes / failed_sample instead of
    publishing a row nothing can attribute — which is what happened to the 8.

    All three falsy shapes, because the writer reads
    `metrics.get("method_version") or None`: an absent key, the empty string a
    misconfigured constant would produce, and an explicit None."""
    from util.dcpi_score_row import upsert_scored_market, UnprovenancedScore

    metrics = _metrics()
    if version is _ABSENT:
        metrics.pop("method_version")
    else:
        metrics["method_version"] = version

    cur = _FakeCursor([0])
    with pytest.raises(UnprovenancedScore):
        upsert_scored_market(cur, _MARKET, metrics, 1.0, 2.0, 3,
                             "CAUTION", [], [], publish=True)


def test_the_refusal_also_covers_unpublished_writes():
    """publish=False says nothing about whether the result is publicly served:
    an UPDATE lands on whatever `published` the row already carries, and
    force_recompute_market updates rows the index is serving right now."""
    from util.dcpi_score_row import upsert_scored_market, UnprovenancedScore

    cur = _FakeCursor([1])
    with pytest.raises(UnprovenancedScore):
        upsert_scored_market(cur, _MARKET, _metrics(method_version=None),
                             1.0, 2.0, 3, "CAUTION", [], [], publish=False)


def test_generated_update_and_insert_agree_on_arity():
    """Both statements are generated from one _COLUMNS list and bind the same
    tuple plus a trailing slug. The previous code hand-wrote two parallel
    column lists and needed a dedicated test to assert the %s count still
    matched, because a miscount was swallowed by the per-market try/except.
    Generating them removes that failure mode; this pins that it stays gone."""
    from util.dcpi_score_row import UPDATE_SQL, _insert_sql, _COLUMNS

    assert UPDATE_SQL.count("%s") == len(_COLUMNS) + 1
    for published in (True, False):
        assert _insert_sql(published).count("%s") == len(_COLUMNS) + 1
    assert _insert_sql(True).rstrip().endswith("TRUE, NOW())")
    assert _insert_sql(False).rstrip().endswith("FALSE, NOW())")
