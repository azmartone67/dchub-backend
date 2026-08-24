"""Guard: the LinkedIn quad's own published copy must be readable from outside.

**2026-08-23.** `/api/v1/linkedin-quad/status` SELECTed 14 columns from
`linkedin_quad_posts` and `post_text` was not one of them. The quad does NOT
write the `linkedin_posts` table (content_publisher does — separate publisher,
separate table), so every endpoint reading `linkedin_posts.content` saw zero
quad posts. The only other reader of `post_text` is `testimonials_seeder`'s
admin POST, which WRITES rows. Net effect: the feed's copy was **write-only**,
and two consecutive sessions could not answer "are the LinkedIn posts good?"
The media pulse scores this feed on cadence, image-attach and count — nothing
about what the posts actually say — so nothing else covered the gap.

★ THE FENCE IS `success`, NOT AN ADMIN HEADER, AND THAT IS DELIBERATE.
`/status` is a GET under `/api/v1/*`. CF Rule #3 caches that family with
`mode: override_origin` and does **not** put `X-Admin-Key` in the cache key.
Measured on this exact path 2026-08-23: MISS → HIT age:2 → junk admin key HIT
age:6 (origin sends `Cache-Control: private, max-age=0, must-revalidate`; the
edge ignores it). A header gate here would publish the admin payload to every
anonymous caller for the TTL, and hand the admin whichever variant was cached
first. #2439 measured the same thing on `/heal/log`. POST is the only sound
origin-side gate on this path family.

So the split is by row, in SQL:
  * `success=TRUE`  → text served. It is already public — it is on the feed.
  * suppressed/failed → text withheld. That copy NEVER published.
  * `post_text_chars` → the FULL length, **unfenced**, so `post_text: null`
    can be told apart from "no copy was ever composed" (a bare claim row).

What each test proves, so nothing here reads stronger than it is: the
behavioural test proves the PLUMBING (the columns survive the row loop and
jsonify). The fence tests read the SQL, because the CASE is evaluated by
Postgres and no fake cursor can execute it.

Run:  python3 -m pytest tests/test_quad_post_text_readable.py -v
"""
from __future__ import annotations

import ast
import contextlib
import datetime
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SRC_PATH = os.path.join(ROOT, "routes", "linkedin_quad_daily.py")


# ── read the recent-rows SELECT out of the source ────────────────────
# ast, not import: this repo's tests never import main.py, and reading the
# statement the endpoint actually runs beats re-describing it here.

def _status_fn_source() -> str:
    tree = ast.parse(io.open(SRC_PATH, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "status":
            return ast.get_source_segment(
                io.open(SRC_PATH, encoding="utf-8").read(), node) or ""
    raise AssertionError("routes/linkedin_quad_daily.py has no status()")


def _recent_rows_select() -> str:
    """The one SELECT in status() that pulls the recent rows."""
    tree = ast.parse(_status_fn_source())
    hits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "FROM linkedin_quad_posts" in n.value
            and "LIMIT 30" in n.value]
    assert len(hits) == 1, (
        f"expected exactly 1 recent-rows SELECT in status(), found {len(hits)}"
        " — this guard is reading the wrong statement")
    return hits[0]


def _projections(select_sql: str) -> list:
    """The SELECT list, split on TOP-LEVEL commas (a CASE ... END and a
    LEFT(x, 2000) both contain commas that must not split the item)."""
    body = re.search(r"\bSELECT\b(.*?)\bFROM\b", select_sql,
                     re.S | re.I).group(1)
    out, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(" ".join(cur.split()))
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(" ".join(cur.split()))
    return out


def _projection_aliased(alias: str) -> str:
    items = _projections(_recent_rows_select())
    for it in items:
        if re.search(rf"(^|\s|\.){re.escape(alias)}$", it, re.I):
            return it
    raise AssertionError(
        f"no projection named/aliased `{alias}` in the recent-rows SELECT; "
        f"got: {items}")


# ── 0. control — without this every assertion below could pass vacuously ──

def test_the_select_this_guard_reads_is_the_real_one():
    sql = _recent_rows_select()
    assert "linkedin_quad_posts" in sql
    items = _projections(sql)
    # the columns that were already there before post_text was added
    for known in ("slot_date", "slot_hour", "topic", "success", "linkedin_urn"):
        assert any(re.search(rf"(^|\s|\.){known}$", i, re.I) for i in items), (
            f"`{known}` vanished from the SELECT — the parser is misreading it")


# ── 1. the text is readable at all ───────────────────────────────────

def test_status_selects_post_text():
    """★ THE REGRESSION. Without this column the feed's copy is write-only."""
    assert "post_text" in _recent_rows_select(), (
        "/api/v1/linkedin-quad/status does not SELECT post_text — the quad's "
        "published copy is unreadable from every read-only endpoint again "
        "(the quad does not write linkedin_posts, so nothing else has it)")


def test_status_publishes_the_full_length_too():
    _projection_aliased("post_text_chars")


# ── 2. the fence: published copy only, and not via a request header ──

def test_post_text_is_fenced_on_success_not_served_bare():
    """A bare `post_text` column would serve gate-suppressed copy — text that
    NEVER published — to anonymous callers."""
    item = _projection_aliased("post_text")
    assert re.search(r"\bCASE\b.*\bWHEN\b.*\bsuccess\b.*\bEND\b", item,
                     re.S | re.I), (
        "post_text is projected without a success conditional: "
        f"`{item}`. Suppressed rows keep copy that was composed and then "
        "blocked by the pre-publish gate; it must not be served here.")


def test_post_text_is_truncated_to_a_bounded_length():
    """30 rows x untruncated copy is an unbounded payload; _record stores up
    to 5000 chars."""
    item = _projection_aliased("post_text")
    m = re.search(r"\bLEFT\s*\(\s*post_text\s*,\s*(\d+)\s*\)", item, re.I)
    assert m, f"post_text is not LEFT()-truncated: `{item}`"
    assert 0 < int(m.group(1)) <= 2000, (
        f"post_text truncation is {m.group(1)} chars — keep it <= 2000 so 30 "
        "rows stay a reasonable payload")


def test_post_text_chars_is_NOT_fenced():
    """★ Otherwise `post_text: null` is ambiguous between "copy exists but is
    withheld" and "no copy was ever composed" (a bare claim row) — the exact
    unmeasured-reads-as-measured shape this repo keeps getting burned by."""
    item = _projection_aliased("post_text_chars")
    assert not re.search(r"\bCASE\b", item, re.I), (
        f"post_text_chars is conditional: `{item}`. It is a length, it leaks "
        "nothing, and it is what makes a withheld row legible as withheld.")


def test_the_gate_is_not_a_request_header_on_this_cacheable_get():
    """★ #2439. CF Rule #3 caches /api/v1/* ignoring origin Cache-Control and
    WITHOUT X-Admin-Key in the cache key — measured on THIS path 2026-08-23
    (MISS, HIT age:2, junk key HIT age:6). An origin header gate on this GET
    publishes the admin payload to every anonymous caller for the TTL. If you
    need admin-only fields here, serve them from a POST (DYNAMIC at the edge)
    or land a CF cache-bypass rule for the path first — then change this test
    on purpose."""
    src = _status_fn_source()
    assert not re.search(r"request\s*\.\s*headers", src), (
        "status() reads a request header — an auth gate on this cacheable GET "
        "is bypassable AND unreliable for the admin; see the docstring")


def test_the_recent_rows_query_is_percent_free():
    """It runs with no args tuple, so a bare % is an IndexError at runtime."""
    sql = _recent_rows_select()
    for i, ch in enumerate(sql):
        if ch != "%":
            continue
        assert sql[i + 1:i + 2] in ("%", "s", "("), (
            f"bare percent at offset {i}: ...{sql[max(0, i - 40):i + 20]}...")


# ── 3. plumbing: the columns actually reach the JSON ─────────────────

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("psycopg2.extras")
from flask import Flask  # noqa: E402

lq = pytest.importorskip("routes.linkedin_quad_daily")


class _Cur:
    """Answers by SQL substring. Returns RealDict-shaped rows (plain dicts)."""

    def __init__(self, rows):
        self._rows, self._pending = rows, None

    def execute(self, sql, params=None):
        # GROUP BY before COUNT: the lead_kinds rollup selects COUNT(*) too,
        # and answering it with a {"count": …} row KeyErrors inside status().
        if "LIMIT 30" in sql:
            self._pending = self._rows
        elif "GROUP BY" in sql:
            self._pending = []
        elif "COUNT(*)" in sql:
            self._pending = [{"count": 0}]
        else:
            self._pending = []

    def fetchone(self):
        return (self._pending or [None])[0]

    def fetchall(self):
        return list(self._pending or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(monkeypatch, rows):
    cur = _Cur(rows)

    class _Conn:
        def cursor(self, *a, **k):
            return cur

    @contextlib.contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    monkeypatch.setattr(lq, "_conn", _conn)
    app = Flask(__name__)
    app.register_blueprint(lq.linkedin_quad_bp)
    return app.test_client()


def _row(**over):
    r = {
        "slot_date": datetime.date(2026, 8, 22), "slot_hour": 12,
        "topic": "hyperscaler_deal", "style": "narrative", "success": True,
        "error_msg": "", "posted_at": datetime.datetime(2026, 8, 22, 12, 0),
        "story_type": None, "lead_kind": None, "lead_entity": None,
        "og_image_url": None, "image_attached": True, "linkedin_urn": "urn:1",
        "claimed_at": None, "post_text": "Ashburn added 412 MW.",
        "post_text_chars": 21,
    }
    r.update(over)
    return r


def test_the_text_survives_the_row_loop_and_jsonify(monkeypatch):
    body = _client(monkeypatch, [_row()]).get(
        "/api/v1/linkedin-quad/status").get_json()
    assert body.get("error") is None, body.get("error")
    row = body["recent"][0]
    assert row["post_text"] == "Ashburn added 412 MW."
    assert row["post_text_chars"] == 21


def test_a_withheld_row_still_reports_its_length(monkeypatch):
    """What a suppressed row looks like once Postgres has applied the CASE."""
    body = _client(monkeypatch, [
        _row(success=False, error_msg="gate: claim-breaker",
             post_text=None, post_text_chars=1187),
    ]).get("/api/v1/linkedin-quad/status").get_json()
    row = body["recent"][0]
    assert row["post_text"] is None
    assert row["post_text_chars"] == 1187, (
        "a withheld row reads identically to a row that never had copy")


def test_no_admin_key_is_needed_to_read_it(monkeypatch):
    """The endpoint is public today (verified live 2026-08-23: 200 + 30 rows,
    no key). This asserts the shipped shape, not a preference — see the
    header-gate test above for why gating it here would not work anyway."""
    body = _client(monkeypatch, [_row()]).get(
        "/api/v1/linkedin-quad/status").get_json()
    assert body["recent"][0]["post_text"]
