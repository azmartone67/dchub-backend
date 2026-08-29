"""tests/test_brain_findings_triage_reads_table.py — triage reads the durable
table, and a failed read never looks like an empty one (2026-08-29).

/api/v1/brain/findings/triage merged the IN-PROCESS dchub_self_heal caches and
never read brain_findings. Those caches are per-replica and empty on the web
dyno, so triage reported source_findings=0 against 4,241 durable rows: nothing
was ever actionable_now, so an approval landed nowhere. The diagnosis was
already committed in routes/loop_control_master_shell.py item 3 (written when
the table held 3,012 rows) and the fix never landed.

Ways this fix could go wrong, one test each:
  (1) READS NOTHING — wired to the table but scoped so no row qualifies.
  (2) ★ FAILURE AS EMPTY — the DB is down and triage answers "0 open, all
      clear". That is the ORIGINAL bug one layer down and the reason
      FindingsReadError exists rather than a `return {}`.
  (3) CLOSED WORK COUNTED — resolved findings re-enter the queue forever.
  (4) MAGNITUDE AS TALLY — `count` is per-detector free-form; radar once
      wrote int(seconds_since), making 5.5 days of silence read as 477,455
      sightings. Only seen_count (episodes) and count_kind='occurrence' may
      carry weight.
  (5) TYPE TOKEN LOST — triage classifies on the token before the first ':',
      so a label that drops `issue` classifies everything as unknown.
  (6) SILENT TRUNCATION — top-N presented as the whole picture.
  (7) ASSUMED SCHEMA — the live table has drifted from the repo DDL before.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_brain_findings_triage_reads_table.py -v
"""
from __future__ import annotations

import pytest


class ScriptedCursor:
    """A cursor that answers ONLY what it was scripted for and RAISES on
    anything else. A fake that invents zeros makes every assertion vacuous —
    the assertion passes because the fake agreed, not because the code is
    right."""

    def __init__(self, script):
        self._script = list(script)
        self._result = None
        self.queries = []

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.queries.append((flat, params))
        for pattern, rows in self._script:
            if pattern in flat:
                self._result = rows
                return
        raise AssertionError("unscripted query: %s" % flat)

    def fetchall(self):
        return list(self._result or [])

    def fetchone(self):
        rows = list(self._result or [])
        return rows[0] if rows else None


_COLS = ("issue", "url", "detail", "detector", "status", "seen_count",
         "count", "count_kind", "last_seen", "resolved_at", "id")


def _cols_reply(cols=_COLS):
    return [(c,) for c in cols]


def _reader():
    from routes.brain_findings_reader import (
        open_findings_for_triage, FindingsReadError, _label_for, _weight_for)
    return open_findings_for_triage, FindingsReadError, _label_for, _weight_for


# ── (1) it actually reads the table ────────────────────────────────────────

def test_durable_rows_become_triage_input():
    read, _, _, _ = _reader()
    rows = [
        ("cron_schedule_collision", "https://dchub.cloud/a", "two crons collide",
         "consistency_radar", "open", 25, 2, None, None),
        ("customer_nudge_failed_needs_human", "https://dchub.cloud/b",
         "9 payers stranded", "autonomy_runtime", "open", 27, 9, None, None),
    ]
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(2,)]),
        ("ORDER BY", rows),
    ])
    merged, index, basis = read(cur, limit=100)
    assert basis["rows_read"] == 2
    assert basis["source"] == "brain_findings"
    assert sum(len(v) for v in merged.values()) == 2, \
        "durable rows did not survive into the classifier's input map"
    assert len(index) == 2, "index must let the caller enrich without re-querying"


def test_the_map_is_the_shape_the_classifier_consumes():
    """merged must be {url: {label: count}} — triage_findings iterates it
    exactly that way and skips anything whose value is not a dict."""
    read, _, _, _ = _reader()
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(1,)]),
        ("ORDER BY", [("cron_schedule_collision", "https://dchub.cloud/a",
                       "d", "consistency_radar", "open", 3, 1, None, None)]),
    ])
    merged, _, _ = read(cur)
    for url, labels in merged.items():
        assert isinstance(url, str)
        assert isinstance(labels, dict)
        for label, weight in labels.items():
            assert isinstance(label, str) and isinstance(weight, int)


# ── (2) ★ THE LEAD GUARD: a failed read is not an empty table ──────────────

def test_a_failed_select_raises_rather_than_returning_an_empty_map():
    """★REGRESSION (2). Returning {} here would republish the original bug:
    a failure rendered as a benign value. 'no open findings' and 'I could not
    look' must not be the same answer."""
    read, FindingsReadError, _, _ = _reader()

    class Dead(ScriptedCursor):
        def execute(self, sql, params=None):
            flat = " ".join(str(sql).split())
            if "information_schema" in flat:
                self._result = _cols_reply()
                return
            raise RuntimeError("connection already closed")

    with pytest.raises(FindingsReadError):
        read(Dead([]), limit=10)


def test_a_failed_introspection_raises_too():
    read, FindingsReadError, _, _ = _reader()

    class NoIntrospect(ScriptedCursor):
        def execute(self, sql, params=None):
            raise RuntimeError("permission denied for information_schema")

    with pytest.raises(FindingsReadError):
        read(NoIntrospect([]), limit=10)


def test_a_missing_table_raises_rather_than_reporting_zero_open_work():
    read, FindingsReadError, _, _ = _reader()
    cur = ScriptedCursor([("information_schema", [])])
    with pytest.raises(FindingsReadError):
        read(cur)


def test_an_empty_but_readable_table_is_a_legitimate_zero():
    """THE PAIRED CONTROL for the guard above. Failing loudly must not mean
    every zero is an error — a readable table with no open rows is a real,
    trustworthy 'nothing actionable'."""
    read, _, _, _ = _reader()
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(0,)]),
        ("ORDER BY", []),
    ])
    merged, index, basis = read(cur)
    assert merged == {} and index == {}
    assert basis["rows_read"] == 0
    assert "read_failed" not in basis


# ── (3) closed work is not open work ───────────────────────────────────────

def test_resolved_rows_are_excluded_by_the_query():
    read, _, _, _ = _reader()
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(0,)]),
        ("ORDER BY", []),
    ])
    read(cur)
    select = [q for q, _ in cur.queries if "ORDER BY" in q][0]
    assert "resolved_at IS NULL" in select, \
        "resolved findings would re-enter the queue forever"
    params = [p for q, p in cur.queries if "ORDER BY" in q][0]
    for closed in ("resolved", "wont_fix", "dismissed"):
        assert closed in params, "%s rows are not open work" % closed


# ── (4) ★ COUNT SEMANTICS: a magnitude must not buy agenda leverage ────────

def test_seen_count_is_the_weight():
    _, _, _, weight = _reader()
    assert weight({"seen_count": 27, "count": 9}) == 27


def test_a_free_form_count_without_an_occurrence_kind_carries_no_weight():
    """★REGRESSION (4). brain_consistency_radar wrote int(seconds_since) into
    `count`; 5.5 days of cron silence became 477,455 'sightings' and re-won
    the agenda every tick. Without count_kind='occurrence' the integer is a
    magnitude, not a tally."""
    _, _, _, weight = _reader()
    assert weight({"count": 477455, "count_kind": None}) == 1
    assert weight({"count": 477455, "count_kind": "seconds"}) == 1


def test_an_occurrence_kind_does_license_the_raw_count():
    """THE PAIRED CONTROL. Distrusting `count` must not mean ignoring the
    detectors that correctly declared it a tally."""
    _, _, _, weight = _reader()
    assert weight({"count": 12, "count_kind": "occurrence"}) == 12


def test_weight_never_falls_below_one():
    _, _, _, weight = _reader()
    assert weight({}) == 1, "a finding that exists has been seen at least once"


# ── (5) the type token survives into the label ─────────────────────────────

def test_the_label_keeps_issue_parseable_as_the_finding_type():
    """★REGRESSION (5). brain_error_classes._finding_type_of() takes the token
    before the first ':'. If the label drops `issue`, every finding classifies
    as unknown and actionable_now stays empty — the same symptom, new cause."""
    _, _, label_for, _ = _reader()
    from routes.brain_error_classes import _finding_type_of
    label = label_for("cron_schedule_collision", "two workflows share a cron")
    assert _finding_type_of(label) == "cron_schedule_collision"
    assert "two workflows share a cron" in label, \
        "the classifier's regex fallback matches on detail text"


def test_a_detail_less_finding_still_yields_its_type():
    _, _, label_for, _ = _reader()
    from routes.brain_error_classes import _finding_type_of
    assert _finding_type_of(label_for("fast_qa_failure", "")) == "fast_qa_failure"


# ── (6) truncation is declared, not silent ─────────────────────────────────

def test_a_truncated_read_says_so():
    """★REGRESSION (6). Presenting the top N as the whole picture is how a
    bounded answer starts reading as a complete one."""
    read, _, _, _ = _reader()
    rows = [("issue_%d" % i, "https://dchub.cloud/%d" % i, "d", "det",
             "open", 1, 1, None, None) for i in range(2)]
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(4241,)]),
        ("ORDER BY", rows),
    ])
    _, _, basis = read(cur, limit=2)
    assert basis["truncated"] is True
    assert basis["open_rows_total"] == 4241
    assert basis["rows_read"] == 2


def test_an_untruncated_read_does_not_claim_truncation():
    read, _, _, _ = _reader()
    rows = [("i", "https://dchub.cloud/1", "d", "det", "open", 1, 1, None, None)]
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(1,)]),
        ("ORDER BY", rows),
    ])
    _, _, basis = read(cur, limit=500)
    assert basis["truncated"] is False


def test_the_limit_is_bounded():
    read, _, _, _ = _reader()
    from routes.brain_findings_reader import MAX_LIMIT
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(0,)]),
        ("ORDER BY", []),
    ])
    _, _, basis = read(cur, limit=10 ** 9)
    assert basis["limit"] == MAX_LIMIT
    _, _, basis2 = read(cur, limit="not-a-number")
    assert isinstance(basis2["limit"], int)


# ── (7) the schema is introspected, not assumed ────────────────────────────

def test_a_table_without_seen_count_still_reads():
    """★REGRESSION (7). The live table drifted from the repo DDL once already
    — that drift is why brain_findings_writer exists. A reader that assumes
    columns inherits the same failure."""
    read, _, _, _ = _reader()
    lean = ("issue", "url", "detail")
    cur = ScriptedCursor([
        ("information_schema", _cols_reply(lean)),
        ("SELECT COUNT(*)", [(1,)]),
        ("ORDER BY", [("some_issue", "https://dchub.cloud/a", "detail text")]),
    ])
    merged, _, basis = read(cur)
    assert basis["columns_used"] == list(lean)
    assert sum(len(v) for v in merged.values()) == 1


def test_the_reader_never_runs_ddl():
    """db_utils.safe_db() sets SKIP_DDL by default on Railway and silently
    drops DDL. A read path that ALTERs is a read path that fails closed."""
    read, _, _, _ = _reader()
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(0,)]),
        ("ORDER BY", []),
    ])
    read(cur)
    for sql, _p in cur.queries:
        upper = sql.upper()
        for verb in ("ALTER ", "CREATE ", "DROP ", "INSERT ", "UPDATE ", "DELETE "):
            assert verb not in upper, "reader issued %s" % verb.strip()


def test_a_finding_without_a_url_does_not_collide_with_its_neighbours():
    """merged is keyed by url. Two url-less findings under '' would overwrite
    each other and vanish from the count without anything reporting a loss."""
    read, _, _, _ = _reader()
    rows = [("issue_a", "", "d1", "det_a", "open", 1, 1, None, None),
            ("issue_b", None, "d2", "det_b", "open", 1, 1, None, None)]
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(2,)]),
        ("ORDER BY", rows),
    ])
    merged, _, basis = read(cur)
    assert sum(len(v) for v in merged.values()) == 2, "a finding was overwritten"
    assert basis["findings_without_url"] == 2


def test_no_literal_percent_reaches_a_parameterised_query():
    """House trap: a literal % in SQL passed with params raises
    IndexError/ValueError inside psycopg2. The LIKE-free query must only
    carry %s placeholders."""
    read, _, _, _ = _reader()
    cur = ScriptedCursor([
        ("information_schema", _cols_reply()),
        ("SELECT COUNT(*)", [(0,)]),
        ("ORDER BY", []),
    ])
    read(cur)
    for sql, params in cur.queries:
        if params:
            assert sql.count("%") == sql.count("%s"), \
                "bare %% in a parameterised query: %s" % sql
