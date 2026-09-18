"""The automated sitemap submit must leave a server-side row.

2026-09-18, production: `gsc_sitemap_submissions` held exactly 2 rows (ids 1
and 2, 2026-06-15 and 2026-07-02) while GET /api/gsc/sitemap/status reported
`last_submitted: 2026-09-18T05:47:29Z` against 27,642 URLs. Submissions were
happening DAILY and none was recorded, because two paths submit and only one
wrote a row:

    POST /api/gsc/sitemap/submit    -> submit_sitemap()      PUT + INSERT
    /api/v1/admin/gsc/submit-sitemap-> auto_submit_sitemap()  PUT, no INSERT  <- cron

So a resubmission after a sitemap cleanup had nothing to point at afterwards.

These tests drive the real auto_submit_sitemap() with the HTTP calls and the
database connection replaced -- the fake cursor KEEPS the SQL and params it is
handed, so an INSERT against the wrong table or with the wrong status fails
here rather than passing on a cursor that ignores its argument.
"""
import google_search_console as gsc
import pytest


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = "" if payload is None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _Cursor:
    def __init__(self, sink, fail=False):
        self._sink = sink
        self._fail = fail

    def execute(self, sql, params=None):
        if self._fail:
            raise RuntimeError("connection already closed")
        self._sink.append((sql, params))

    def close(self):
        pass


class _Conn:
    def __init__(self, fail=False):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._fail = fail

    def cursor(self):
        return _Cursor(self.executed, self._fail)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def wired(monkeypatch):
    """Real auto_submit_sitemap(), fake network + fake database."""
    state = {"conn": _Conn(), "put": 200, "get": _Resp(
        200, {"contents": [{"type": "web", "submitted": "27642", "indexed": "9"}]})}

    monkeypatch.setattr(gsc, "get_access_token", lambda: "tok")
    monkeypatch.setattr(gsc, "get_db", lambda: state["conn"])
    monkeypatch.setattr(gsc.requests, "put",
                        lambda *a, **k: _Resp(state["put"]))
    monkeypatch.setattr(gsc.requests, "get", lambda *a, **k: state["get"])
    return state


def _insert(conn):
    rows = [(s, p) for (s, p) in conn.executed if "INSERT" in s.upper()]
    assert rows, (
        "auto_submit_sitemap() issued no INSERT. The daily submit is invisible "
        "server-side again -- this is the 2026-09-18 bug."
    )
    assert len(rows) == 1, "expected exactly one INSERT, got %d" % len(rows)
    return rows[0]


def test_a_successful_submit_writes_a_row(wired):
    out = gsc.auto_submit_sitemap()
    sql, params = _insert(wired["conn"])

    assert "gsc_sitemap_submissions" in sql, (
        "the INSERT does not target gsc_sitemap_submissions: %s" % sql)
    assert wired["conn"].commits == 1, "the row was never committed"
    assert wired["conn"].closed, "the connection leaked"
    assert out["success"] is True
    assert out["recorded"] is True, out


def test_the_row_says_which_path_submitted(wired):
    gsc.auto_submit_sitemap()
    _, params = _insert(wired["conn"])
    assert "auto_submitted" in params, (
        "the row does not distinguish the automated path from the manual one; "
        "params were %r. submit_sitemap() writes 'submitted'/'failed'." % (params,))


def test_a_rejected_submit_is_recorded_as_failed(wired):
    wired["put"] = 500
    out = gsc.auto_submit_sitemap()
    _, params = _insert(wired["conn"])
    assert "auto_failed" in params, params
    assert out["success"] is False
    # A rejected PUT changed nothing at Google, so there is no count to read.
    assert None in params, (
        "a failed submit stored a URL count: %r. 0 would read as a measured "
        "zero." % (params,))


def test_the_google_reported_count_is_stored(wired):
    out = gsc.auto_submit_sitemap()
    _, params = _insert(wired["conn"])
    assert 27642 in params, (
        "urls_submitted was not filled from the GSC response: %r. The column "
        "exists and has been 0 on every row." % (params,))
    assert out["urls_submitted"] == 27642


def test_an_unreported_count_is_null_not_zero(wired):
    """0 must keep meaning a measured zero, not 'Google said nothing'."""
    wired["get"] = _Resp(200, {})
    out = gsc.auto_submit_sitemap()
    _, params = _insert(wired["conn"])
    assert None in params and 0 not in params, (
        "an absent GSC count was stored as 0, which is indistinguishable from "
        "a real zero and from the two legacy never-filled rows: %r" % (params,))
    assert out["urls_submitted"] is None


def test_string_counts_are_not_concatenated(wired):
    """GSC returns these as strings; summing them as strings yields '1''2'."""
    wired["get"] = _Resp(200, {"contents": [
        {"type": "web", "submitted": "10"}, {"type": "web", "submitted": "5"}]})
    gsc.auto_submit_sitemap()
    _, params = _insert(wired["conn"])
    assert 15 in params, params


def test_a_database_failure_does_not_lose_the_submit(wired):
    """Google accepted it; the caller must hear BOTH facts, not a 500."""
    wired["conn"] = _Conn(fail=True)
    out = gsc.auto_submit_sitemap()
    assert out["success"] is True, "a DB error masked an accepted submission"
    assert out["recorded"] is False, out
    assert out.get("record_error"), (
        "the write failed silently -- a swallowed failure here recreates the "
        "exact silence this change removes")
    assert wired["conn"].rollbacks == 1
    assert wired["conn"].closed, "the connection leaked on the error path"


def test_no_token_attempts_nothing(wired, monkeypatch):
    monkeypatch.setattr(gsc, "get_access_token", lambda: None)
    out = gsc.auto_submit_sitemap()
    assert out["success"] is False
    assert not wired["conn"].executed, (
        "a submit that never happened was recorded: %r" % (wired["conn"].executed,))
