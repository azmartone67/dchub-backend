"""The press queue and the badge above it must agree (2026-08-24).

WHAT THIS PINS
──────────────
#3136 repaired /api/admin/content-queue?type=press by mapping the queue's four
tabs onto press_releases' single `published` boolean. That mapping is correct
and is not revisited here. What it left behind is a screen that disagrees with
itself, and three smaller gaps around it:

  1. ★ THE BADGE DISOWNED THE TAB. /api/admin/content/stats counted press into
     `published` but not into `draft`. Measured live 2026-08-24: the press tab
     lists 43 drafts while the Draft badge above it reads 4. press lands in
     exactly two of the four buckets and in neither of the other two.

  2. AN EMPTY PAGE CANNOT SAY WHY IT IS EMPTY. approved/rejected and any
     platform filter correctly return no rows, but the admin UI renders "No
     approved content ready to publish found" either way — so a state press
     CANNOT BE IN reads as a state it merely has none of. The response now
     carries `not_applicable`. Status code and SQL are unchanged, so #3136's
     guards still see exactly the statements they pin.

  3. AN UNKNOWN `type` FELL THROUGH TO SOCIAL. `?type=pres` served social rows
     under a press heading — the same defect class as the action routes'
     `request.args.get('type', 'social')` that #3131 removed. Now 400.

  4. THE MAPPING WAS STATED THREE TIMES — the branch's if/elif, the SELECT's
     CASE, and the comments. Three copies of a product decision drift.

Live facts behind all of it, measured on the Neon replica the same day:
    press_releases            196 rows = 43 unpublished + 153 published
    social_media_posts      2,466 rows; status draft=4, published=438,
                            rejected=790, expired=1,138, failed=96
`published` is the only truthful state source — it disagrees with
`published_at` on 3 of 196 rows (2 published rows carry a NULL timestamp, 1
unpublished row carries one), so timestamp-derived state mis-files all three.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (content_publisher.py from origin/main @ 11c54489 swapped in over
this branch, __pycache__ cleared, `hasattr(cp, "_PRESS_STATUS_SQL") is False`
asserted first):
        8 failed, 5 passed          (pytest exit 1, read unpiped)
The 5 that pass unpatched pin facts #3136/#3134/#3131 already satisfy:
        test_live_press_rows_partition_into_exactly_two_buckets
        test_a_state_press_can_be_in_carries_no_such_note
        test_default_type_is_still_social
        test_social_published_at_coalesce_resolves_one_type
        test_press_is_readable_here_but_still_not_actionable
PATCHED (this branch):
        13 passed, 0 failed         (pytest exit 0)

★ MUTATION-VERIFIED, each applied over a pristine copy, asserted present in
the file, __pycache__ cleared, then reverted:
    stats stops counting press drafts                    2 red
    press reports 0 rather than None for approved        1 red
    not_applicable dropped from the body                 3 red
    the unknown-type 400 removed                         1 red
    the mapping re-stated as a CASE in the SELECT        1 red
    'published IS NOT TRUE' -> 'published = FALSE'       1 red
    an ADDED type-mixing COALESCE, pinned string intact  3 red
  The fake cursor returns DICT rows on purpose: _get_db connects with
  RealDictCursor, and a tuple-returning fake lets `row[0]` pass — which is how
  {"error":"0"} reached production in the first place.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_press_queue_coherence.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
from contextlib import contextmanager

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "content_publisher.py")

LIVE_PRESS_DRAFT = 43
LIVE_PRESS_PUBLISHED = 153
LIVE_PRESS_ROWS = 196

#: Live column types, measured 2026-08-24. COALESCE resolves ONE type for its
#: arguments, so mixing two of these is an error. `::text` on every arm is the
#: form that holds before and after the TEXT -> timestamptz migration in flight
#: (social_media_posts.approved_at flipped mid-measurement the same afternoon).
_COL_KIND = {
    "posted_at": "timestamp without time zone",
    "created_at": "timestamp without time zone",
    "scheduled_at": "timestamp without time zone",
    "approved_at": "timestamp with time zone",
    "published_at": "text",
}
_COL_KIND.update({"%s::text" % c: "text" for c in list(_COL_KIND)})


def _cp():
    spec = importlib.util.spec_from_file_location("cp_coherence_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DatatypeMismatch(Exception):
    """Stand-in for psycopg2.errors.DatatypeMismatch."""


class _Row(dict):
    """RealDictRow stand-in: a dict, so `row[0]` raises KeyError: 0."""


class _Cur:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
        self.executed = []
        self._pending = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        for expr in re.findall(r"COALESCE\(([^)]*)\)", flat, re.I):
            kinds = {_COL_KIND.get(a.strip(), "?") for a in expr.split(",")
                     if not a.strip().startswith("'")}
            kinds.discard("?")
            if len(kinds) > 1:
                raise DatatypeMismatch(
                    "COALESCE types %s cannot be matched" % " and ".join(sorted(kinds)))
        m = re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", flat, re.I)
        table = m.group(1) if m else ""
        rows = [] if " WHERE FALSE" in flat.upper() or " AND FALSE" in flat.upper() \
            else self.rows_by_table.get(table, [])
        self._pending = [_Row(n=len(rows))] if re.match(r"SELECT\s+COUNT\(", flat, re.I) \
            else [_Row(r) for r in rows]

    def fetchone(self):
        return self._pending[0] if self._pending else None

    def fetchall(self):
        return list(self._pending)

    @property
    def sql(self):
        return [s for s, _ in self.executed]


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def bench(monkeypatch):
    flask = pytest.importorskip("flask")
    cp = _cp()
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-key")
    cur = _Cur({
        "press_releases": [
            {"id": 100249, "type": "press", "content": "T\n\nbody", "status": "draft",
             "publish_platform": "", "created_at": "2026-08-23", "published_at": None},
        ],
        "social_media_posts": [
            {"id": 303, "type": "social", "content": "post", "status": "draft",
             "publish_platform": "linkedin", "created_at": "2026-08-23",
             "published_at": None, "approved_at": None, "og_image": None},
        ],
    })

    @contextmanager
    def _fake_db_conn():
        yield _Conn(cur)

    monkeypatch.setattr(cp, "_db_conn", _fake_db_conn)
    app = flask.Flask(__name__)
    app.register_blueprint(cp.content_bp)
    client = app.test_client()

    def call(**qs):
        return client.get("/api/admin/content-queue?key=test-key"
                          + "".join("&%s=%s" % kv for kv in qs.items()))

    call.cur, call.cp, call.client = cur, cp, client
    return call


# ── 1. The badge and the tab ────────────────────────────────────────────────

def test_live_press_rows_partition_into_exactly_two_buckets():
    assert LIVE_PRESS_DRAFT + LIVE_PRESS_PUBLISHED == LIVE_PRESS_ROWS


def test_stats_counts_press_into_draft_as_well_as_published():
    """★REGRESSION. Counting press into `published` but not `draft` put 43 rows
    on a screen whose Draft badge said they were not there."""
    src = open(_SRC, encoding="utf-8").read()
    stats = src[src.index("def content_stats("):src.index("def content_queue(")]
    assert "FROM press_releases WHERE published IS NOT TRUE" in stats, (
        "press drafts are still uncounted while the press tab lists them")
    assert "stats['draft'] += press_draft" in stats
    for absent in ("stats['approved'] += press", "stats['rejected'] += press"):
        assert absent not in stats, (
            "press has no such state — a count there would be an invention")


def test_stats_publishes_the_per_type_split(bench):
    """A blended total that cannot be taken apart cannot be checked."""
    r = bench.client.get("/api/admin/content/stats?key=test-key")
    assert r.status_code == 200, r.get_json()
    by_type = r.get_json()["stats_by_type"]
    assert by_type["press"]["approved"] is None, (
        "0 would read as 'none pending'; press has no approved state at all")
    assert by_type["press"]["rejected"] is None
    assert by_type["press"]["draft"] == 1 and by_type["press"]["published"] == 1
    assert by_type["social"]["draft"] == 1


def test_stats_draft_is_the_sum_of_both_types(bench):
    body = bench.client.get("/api/admin/content/stats?key=test-key").get_json()
    for bucket in ("draft", "published"):
        assert body["stats"][bucket] == (body["stats_by_type"]["social"][bucket]
                                         + body["stats_by_type"]["press"][bucket])


# ── 2. An empty page that says why ──────────────────────────────────────────

@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_a_state_press_cannot_be_in_says_so(bench, status):
    r = bench(type="press", status=status)
    assert r.status_code == 200
    body = r.get_json()
    assert body["items"] == [] and body["total"] == 0
    na = body["not_applicable"]
    assert na["status"] == status and na["type"] == "press"
    assert sorted(na["available_statuses"]) == ["draft", "published"]


def test_a_platform_filter_on_press_says_why_it_matched_nothing(bench):
    r = bench(type="press", status="draft", platform="linkedin")
    assert r.status_code == 200
    assert r.get_json()["not_applicable"]["filter"] == "platform"


def test_a_state_press_can_be_in_carries_no_such_note(bench):
    for status in ("draft", "published"):
        body = bench(type="press", status=status).get_json()
        assert "not_applicable" not in body, status
    assert "not_applicable" not in bench(type="social", status="draft").get_json()


# ── 3. The type argument ────────────────────────────────────────────────────

def test_unknown_type_is_refused_instead_of_serving_social(bench):
    r = bench(type="pres", status="draft")
    assert r.status_code == 400
    assert sorted(r.get_json()["expected"]) == ["press", "social"]
    assert bench.cur.executed == []


def test_default_type_is_still_social(bench):
    """The served admin page omits `type` for 'All Types'."""
    assert bench(status="draft").status_code == 200
    assert "social_media_posts" in " ".join(bench.cur.sql)


# ── 4. One copy of the mapping ──────────────────────────────────────────────

def test_the_mapping_is_declared_once_and_read_from_there(bench):
    cp = bench.cp
    assert dict(cp._PRESS_STATUS_SQL) == {
        "published": "published IS TRUE",
        "draft": "published IS NOT TRUE",
    }
    tree = ast.parse(open(_SRC, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "content_queue")
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "published IS" not in node.value, (
                "content_queue states a press predicate itself instead of "
                "reading _PRESS_STATUS_SQL: %r" % node.value)


# ── 5. The COALESCE fault, generalised ──────────────────────────────────────

def test_social_published_at_coalesce_resolves_one_type(bench):
    """★REGRESSION. `COALESCE(posted_at, published_at)` mixes `timestamp
    without time zone` with `text`. Measured live 2026-08-24 as this route's
    500 on the DEFAULT tab; #3134 landed the ::text repair.

    PAIRED with test_date_window_half_open_range.py::
    test_content_queue_never_coalesces_two_different_timestamp_types, which
    pins the two exact SQL strings. Which mutation separates them was MEASURED:

        replacing the cast expression        both red
        ADDING `COALESCE(scheduled_at, published_at)` beside it, pinned
        string left intact                   this file red, that one GREEN

    A string check cannot see a NEW type-mixing COALESCE; this drives the real
    route against a cursor that resolves any pair of the table's four timestamp
    types. Keep both — neither is the other's duplicate.
    """
    assert bench(type="social", status="draft").status_code == 200
    row_sql = [s for s in bench.cur.sql if "COALESCE(" in s and "posted_at" in s]
    assert row_sql, bench.cur.sql
    for expr in re.findall(r"COALESCE\(([^)]*)\)", row_sql[0], re.I):
        arms = [a.strip() for a in expr.split(",")]
        assert all(a.endswith("::text") for a in arms), (
            "every arm must be cast to one type or this breaks again the "
            "moment a column changes type: %r" % expr)


# ── 6. Visible here, still not writable ─────────────────────────────────────

def test_press_is_readable_here_but_still_not_actionable(bench):
    """★ The hazard the queue repair could have armed. #3131 landed the write
    refusal first; this asserts the two halves still disagree in the right
    direction — press READS, press does not WRITE."""
    assert bench(type="press", status="draft").status_code == 200
    for action in ("approve", "reject", "edit"):
        r = bench.client.post(
            f"/api/admin/content/100249/{action}?type=press&key=test-key",
            json={"content": "x"})
        assert r.status_code == 400, (action, r.status_code, r.get_json())
        assert r.get_json()["type"] == "press"
    assert not [s for s in bench.cur.sql
                if s.upper().startswith(("UPDATE ", "INSERT ", "DELETE "))]
