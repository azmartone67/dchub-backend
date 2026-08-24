"""The press queue maps four tabs onto a table with one state bit (2026-08-24).

WHAT THIS PINS
──────────────
`/api/admin/content-queue?type=press` returned HTTP 500 on every call. Measured
against the Railway origin (https://dchub-backend-production.up.railway.app):

    GET /api/admin/content-queue?status=draft&type=press
      -> 500 {"error": "column \\"status\\" does not exist\\nLINE 1: SELECT
              COUNT(*) FROM press_releases WHERE status = 'draft'"}

The branch asked press_releases for FOUR columns it does not have — `status`,
`content`, `publish_platform`, `approved_at`. Live columns, measured the same
day on the Neon replica:

    id, title, summary, source, source_url, category, published_date, featured,
    created_at, slug, date, subheadline, body, meta_description,
    published (boolean), published_at (timestamptz)

So the repair is not a rename. press_releases carries ONE state bit while this
queue's tab model has four, and how those meet is a product decision:

    draft      -> published IS NOT TRUE          43 rows measured 2026-08-24
    published  -> published IS TRUE             153 rows
    approved   -> NO SUCH STATE                 press has no approval step
    rejected   -> NO SUCH STATE                 press has no rejection step

Three facts decided it, all measured on the live replica, none assumed:

  · `published` is the ONLY truthful state source. It disagrees with
    `published_at` on 3 of 196 rows — 2 published rows carry a NULL timestamp
    and 1 unpublished row carries one — so deriving state from the timestamp
    mis-files all three.
  · `IS NOT TRUE`, not `= FALSE`. The column is nullable, so the two-way split
    is total only in this form: draft(43) + published(153) = 196 = every row.
    There were 0 NULLs at the time; the partition holds anyway if one lands.
  · approved/rejected are answered with an EMPTY page and a stated reason, at
    HTTP 200. They are tabs an operator clicks, so a 4xx would blank the panel
    the way the 500 did — and inventing a third state would fill an approval
    queue whose buttons have nothing to write: approve/reject/edit need
    `status`, `approved_at` and `content`, and press_releases has none of them.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (content_publisher.py from origin/main @ 7dd11eb8, swapped in over
this branch, __pycache__ cleared, `hasattr(cp, "_PRESS_STATUS_SQL") is False`
asserted first):
        14 failed, 6 passed        (pytest exit 1, read unpiped)
    The decisive failure is the production fault, reproduced verbatim through
    the real route by a schema-aware fake cursor:
        UndefinedColumn: column "status" does not exist
The 6 that pass unpatched pin live facts and the social path, which the old code
already satisfied:
        test_live_press_schema_lacks_every_column_the_social_branch_reads
        test_published_is_the_only_truthful_state_source
        test_a_nullable_boolean_only_partitions_under_is_not_true
        test_default_type_is_still_social
        test_social_queue_is_unchanged
        test_the_fake_cursor_would_catch_a_positional_row_read
PATCHED (this branch):
        22 passed, 0 failed        (pytest exit 0)

★ MUTATION-VERIFIED. Each applied over a pristine copy one at a time, the
edit asserted present in the file, __pycache__ cleared, then reverted:
    COALESCE(a::text,b::text) -> COALESCE(a,b)          4 red
    COALESCE with only one arm cast                     1 red
    'published IS NOT TRUE' -> 'published = FALSE'      2 red
    _PRESS_STATUS_SQL gains 'approved': 'FALSE'         3 red
    E'\n\n' -> '\n\n'                                   1 red
    COALESCE(body,'') -> body                           1 red
    _scalar(cur) -> cur.fetchone()[0]                   7 red
    the unknown-type 400 removed                        1 red
    stats stops counting press drafts                   1 red
  The fake cursor returns DICT rows on purpose. A tuple-returning fake lets the
  `[0]` regression pass, which is how {"error":"0"} reached production.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_press_queue_status_mapping.py -v
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

# ── Live facts, measured 2026-08-24 on the Neon replica (read-only) ──────────
LIVE_PRESS_COLS = {
    "id", "title", "summary", "source", "source_url", "category",
    "published_date", "featured", "created_at", "slug", "date", "subheadline",
    "body", "meta_description", "published", "published_at",
}
#: What the social branch of this same route reads. press_releases has none.
SOCIAL_QUEUE_COLS = {"status", "content", "publish_platform", "approved_at"}
LIVE_PRESS_ROWS = 196
LIVE_PRESS_DRAFT = 43           # published IS NOT TRUE
LIVE_PRESS_PUBLISHED = 153      # published IS TRUE
#: rows where `published` and `published_at` disagree — why the boolean wins
LIVE_PRESS_STATE_TIMESTAMP_DISAGREEMENTS = 3


def _cp():
    spec = importlib.util.spec_from_file_location(
        "cp_press_queue_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── A cursor that refuses what the live table would refuse ───────────────────

class UndefinedColumn(Exception):
    """Stand-in for psycopg2.errors.UndefinedColumn."""


class DatatypeMismatch(Exception):
    """Stand-in for psycopg2.errors.DatatypeMismatch."""


#: Live column types, measured 2026-08-24. COALESCE must resolve ONE type for
#: its arguments, so mixing two of these is an error — `col::text` on every arm
#: is the form that holds both before and after the TEXT -> timestamptz
#: migration now in flight (social_media_posts.approved_at flipped from text to
#: timestamptz between two measurements the same afternoon).
_COL_KIND = {
    "posted_at": "timestamp without time zone",
    "created_at": "timestamp without time zone",
    "published_at": "text",
    "posted_at::text": "text",
    "published_at::text": "text",
}


def _column_reads(sql):
    """Names the statement READS, i.e. every identifier that is not an alias.

    `NULL AS publish_platform` names a column of the RESULT and is legal against
    any table; `WHERE publish_platform = %s` reads one and is not. Postgres
    draws exactly this line, so the fake has to as well — otherwise the patched
    SQL, which aliases all four missing names, would look like the bug.
    """
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                          re.sub(r"\bAS\s+[A-Za-z_][A-Za-z0-9_]*", " ", sql,
                                 flags=re.I)))


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
        m = re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", flat, re.I)
        table = m.group(1) if m else ""
        if table == "press_releases":
            illegal = sorted(_column_reads(flat) & SOCIAL_QUEUE_COLS)
            if illegal:
                raise UndefinedColumn(
                    'column "%s" does not exist' % illegal[0])
        for expr in re.findall(r"COALESCE\(([^)]*)\)", flat, re.I):
            kinds = {_COL_KIND.get(a.strip(), "?") for a in expr.split(",")
                     if not a.strip().startswith("'")}
            kinds.discard("?")
            if len(kinds) > 1:
                raise DatatypeMismatch(
                    "COALESCE types %s cannot be matched" % " and ".join(sorted(kinds)))
        if re.match(r"SELECT\s+COUNT\(", flat, re.I):
            self._pending = [_Row(n=len(self.rows_by_table.get(table, [])))]
        else:
            self._pending = [_Row(r) for r in self.rows_by_table.get(table, [])]

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
    """Flask test client over the real blueprint, with the DB faked."""
    flask = pytest.importorskip("flask")
    cp = _cp()
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-key")
    cur = _Cur({
        "press_releases": [
            {"id": 100249, "type": "press", "content": "T\n\nbody",
             "status": "draft", "publish_platform": None,
             "created_at": "2026-08-23", "published_at": None,
             "approved_at": None},
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
        url = "/api/admin/content-queue?key=test-key" + "".join(
            "&%s=%s" % (k, v) for k, v in qs.items())
        return client.get(url)

    call.cur = cur
    call.cp = cp
    call.client = client
    return call


# ── The live facts the mapping rests on ─────────────────────────────────────

def test_live_press_schema_lacks_every_column_the_social_branch_reads():
    """The deciding fact. If press_releases ever gains one of these, this
    mapping is no longer the only option and should be revisited."""
    assert SOCIAL_QUEUE_COLS & LIVE_PRESS_COLS == set(), (
        "press_releases gained %s — re-decide the mapping rather than leaving "
        "this test to rot" % sorted(SOCIAL_QUEUE_COLS & LIVE_PRESS_COLS))
    assert "published" in LIVE_PRESS_COLS


def test_published_is_the_only_truthful_state_source():
    """Measured: `published` and `published_at` disagree on 3 of 196 rows, so a
    timestamp-derived state would mis-file all three."""
    assert LIVE_PRESS_STATE_TIMESTAMP_DISAGREEMENTS > 0
    assert LIVE_PRESS_DRAFT + LIVE_PRESS_PUBLISHED == LIVE_PRESS_ROWS


def test_a_nullable_boolean_only_partitions_under_is_not_true():
    """`published = FALSE` drops NULL rows out of BOTH tabs; `IS NOT TRUE` does
    not. The column is nullable, so only one of the two is a partition."""
    universe = [True, False, None]
    is_not_true = [v for v in universe if v is not True]
    eq_false = [v for v in universe if v is False]
    is_true = [v for v in universe if v is True]
    assert len(is_not_true) + len(is_true) == len(universe)
    assert len(eq_false) + len(is_true) < len(universe), (
        "a NULL row would be invisible in every tab")


# ── The mapping, driven through the real route ──────────────────────────────

def test_press_draft_reads_the_published_boolean_not_a_status_column(bench):
    r = bench(type="press", status="draft")
    assert r.status_code == 200, r.get_json()
    sql = " ".join(bench.cur.sql)
    assert "published IS NOT TRUE" in sql
    assert not re.search(r"\bstatus\s*=", sql), sql


def test_press_published_reads_published_is_true(bench):
    r = bench(type="press", status="published")
    assert r.status_code == 200, r.get_json()
    assert "published IS TRUE" in " ".join(bench.cur.sql)


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_press_approved_and_rejected_are_empty_with_a_stated_reason(bench, status):
    """★ Not a 500 and not a bare empty list: press has no such state, and the
    response has to say so or the operator reads it as 'none pending'."""
    r = bench(type="press", status=status)
    assert r.status_code == 200
    body = r.get_json()
    assert body["items"] == [] and body["total"] == 0
    na = body["not_applicable"]
    assert na["type"] == "press" and na["status"] == status
    assert sorted(na["available_statuses"]) == ["draft", "published"]
    assert bench.cur.executed == [], (
        "a state press cannot be in must not cost a query: %s" % bench.cur.sql)


def test_press_platform_filter_is_refused_rather_than_silently_ignored(bench):
    """press_releases has no platform column. Ignoring the filter would list
    press releases under 'LinkedIn'."""
    r = bench(type="press", status="draft", platform="linkedin")
    assert r.status_code == 200
    body = r.get_json()
    assert body["items"] == [] and body["total"] == 0
    assert body["not_applicable"]["filter"] == "platform"
    assert bench.cur.executed == []


def test_press_queue_never_reads_a_column_press_releases_does_not_have(bench):
    """★REGRESSION. The fake raises UndefinedColumn on exactly what the live
    table raises it on, so the production 500 reproduces here."""
    for status in ("draft", "published"):
        r = bench(type="press", status=status)
        assert r.status_code == 200, r.get_json()
    for sql in bench.cur.sql:
        if "press_releases" in sql:
            assert _column_reads(sql) & SOCIAL_QUEUE_COLS == set(), sql


def test_press_rows_carry_the_derived_status_and_no_invented_platform(bench):
    r = bench(type="press", status="draft")
    item = r.get_json()["items"][0]
    assert item["type"] == "press"
    assert item["status"] == "draft"
    assert item["publish_platform"] is None
    assert item["approved_at"] is None
    assert item["og_image"] is None and item["card_url"] is None, (
        "the media-card endpoint reads social_media_posts by id — a press id "
        "must never be composed into that URL")


def test_press_content_is_composed_with_real_newlines(bench):
    """`'\\n\\n'` in Postgres with standard_conforming_strings on is a LITERAL
    backslash-n. Only the E'' form produces the blank line the queue shows."""
    bench(type="press", status="draft")
    row_sql = [s for s in bench.cur.sql if "title ||" in s]
    assert row_sql, bench.cur.sql
    assert "E'\\n\\n'" in row_sql[0], row_sql[0]


def test_press_body_is_null_safe(bench):
    """`title || NULL` is NULL — one missing body would blank the whole card."""
    bench(type="press", status="draft")
    row_sql = [s for s in bench.cur.sql if "title ||" in s][0]
    assert "COALESCE(body, '')" in row_sql


# ── The type argument ───────────────────────────────────────────────────────

def test_unknown_type_is_refused_instead_of_serving_social(bench):
    """`?type=pres` used to fall through and serve social rows under a press
    heading."""
    r = bench(type="pres", status="draft")
    assert r.status_code == 400
    body = r.get_json()
    assert body["items"] == [] and body["total"] == 0
    assert sorted(body["expected"]) == ["press", "social"]
    assert bench.cur.executed == []


def test_default_type_is_still_social(bench):
    """Every caller relies on this: the served admin page omits `type` for
    'All Types'."""
    r = bench(status="draft")
    assert r.status_code == 200
    assert "social_media_posts" in " ".join(bench.cur.sql)


def test_social_queue_is_unchanged(bench):
    r = bench(type="social", status="draft")
    assert r.status_code == 200
    item = r.get_json()["items"][0]
    assert item["type"] == "social" and item["publish_platform"] == "linkedin"


# ── The RealDictCursor fault this route shares with /content/stats ──────────

def test_the_fake_cursor_would_catch_a_positional_row_read():
    """The fake must be no more permissive than the driver. A tuple-returning
    fake lets `cur.fetchone()[0]` pass, which is how {"error":"0"} shipped."""
    with pytest.raises(KeyError):
        _Row(n=7)[0]


def test_the_queue_count_survives_a_realdict_cursor(bench):
    for type_ in ("press", "social"):
        r = bench(type=type_, status="draft")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["total"] == 1


# ── The badge has to agree with the tab it labels ───────────────────────────

def test_stats_counts_press_into_draft_and_published_only(bench):
    """★ /api/admin/content/stats counted press into `published` but not into
    `draft`, so the Draft badge disowned 43 rows the press tab lists."""
    src = open(_SRC, encoding="utf-8").read()
    stats = src[src.index("def content_stats("):src.index("def content_queue(")]
    assert "FROM press_releases WHERE published IS NOT TRUE" in stats
    assert "FROM press_releases WHERE published IS TRUE" in stats
    assert "stats['draft'] += press_draft" in stats
    for absent in ("stats['approved'] += press", "stats['rejected'] += press"):
        assert absent not in stats, (
            "press has no such state — a zero there would read as 'none "
            "pending' instead of 'not applicable'")


def test_stats_publishes_the_per_type_split(bench):
    """A blended total that cannot be taken apart cannot be checked."""
    r = bench.client.get("/api/admin/content/stats?key=test-key")
    assert r.status_code == 200, r.get_json()
    by_type = r.get_json()["stats_by_type"]
    assert by_type["press"]["approved"] is None
    assert by_type["press"]["rejected"] is None
    assert by_type["press"]["draft"] == 1 and by_type["press"]["published"] == 1


def test_the_mapping_is_declared_once_and_read_from_there(bench):
    """Two copies of a product decision drift. The route builds its WHERE from
    _PRESS_STATUS_SQL; nothing else may hard-code a press state."""
    cp = bench.cp
    assert dict(cp._PRESS_STATUS_SQL) == {
        "draft": "published IS NOT TRUE",
        "published": "published IS TRUE",
    }
    assert tuple(cp._PRESS_STATES) == ("draft", "published")
    tree = ast.parse(open(_SRC, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "content_queue")
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "published IS" not in node.value, (
                "content_queue hard-codes a press state instead of reading "
                "_PRESS_STATUS_SQL: %r" % node.value)


def test_social_published_at_coalesce_resolves_one_type(bench):
    """★REGRESSION. `COALESCE(posted_at, published_at)` mixes `timestamp without
    time zone` with `text`, which Postgres cannot resolve. Measured against the
    Railway origin 2026-08-24 — AFTER #3128 landed — this was the live 500 on
    `/api/admin/content-queue?status=draft`, the panel's default tab:

        500 {"error": "COALESCE types timestamp without time zone and text
             cannot be matched"}

    It stayed hidden until #3128 fixed the RealDict count above it: the count
    raised KeyError: 0 first and execution never reached this line.
    """
    r = bench(type="social", status="draft")
    assert r.status_code == 200, r.get_json()
    row_sql = [s for s in bench.cur.sql if "COALESCE(" in s and "posted_at" in s]
    assert row_sql, bench.cur.sql
    for expr in re.findall(r"COALESCE\(([^)]*)\)", row_sql[0], re.I):
        arms = [a.strip() for a in expr.split(",")]
        assert all(a.endswith("::text") for a in arms), (
            "every COALESCE arm must be cast to one type, or this breaks again "
            "the moment a column changes type: %r" % expr)


def test_press_is_readable_here_but_still_not_actionable(bench):
    """★ The hazard this change could have armed.

    Before this branch, `?type=press` 500'd, so no operator ever saw a press row
    in the queue. Making them visible is the point — but the approve/reject/edit
    buttons next to them cannot run against press_releases, which has no status,
    approved_at or content column. #3131 landed that refusal first
    (tests/test_content_action_table_identity.py). This asserts the two halves
    still disagree in the right direction: press READS, press does not WRITE.
    """
    assert bench(type="press", status="draft").status_code == 200
    for action in ("approve", "reject", "edit"):
        r = bench.client.post(
            "/api/admin/content/100249/%s?type=press&key=test-key" % action,
            json={"content": "x"})
        assert r.status_code == 400, (action, r.status_code, r.get_json())
        assert r.get_json()["type"] == "press"
    assert not [s for s in bench.cur.sql
                if s.upper().startswith(("UPDATE ", "INSERT ", "DELETE "))], (
        "a press row was mutated: %s" % bench.cur.sql)
