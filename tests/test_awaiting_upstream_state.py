"""A period the UPSTREAM has not published yet is not a fault of ours.

★ THE STATE THIS EXISTS FOR, live on /api/v1/ops/deadman 2026-09-03 01:16Z:

    iso-intl   status=degraded   red=true
    note: failed: OCCTO (mix: hokkaido:month_not_published_yet:202609
                          (day 3 JST; monthly file absent upstream …))

On that same tick NINE of the ten Japanese areas wrote fresh 30-minute fuel
mix. Measured 2026-09-03 ~10:20 JST, the nine monthly `eria_jukyu` files:

    hokkaido 404   ← the only absentee
    tepco 200 (0.8h fresh)    chubu 200 (1.3h)     hokuriku 200 (1.3h)
    kansai 200 (1.3h)         chugoku 200 (0.8h)   shikoku 200 (0.8h)
    kyushu 200                okinawa 200 (1.3h)

`degraded` says WE are missing data we should have — a call to action. It is
the wrong word for a file HEPCO has not posted to anybody: no engineer, owner
or customer can act on it, and it clears itself. One waiting area turned a
working feed red.

★ THIS IS NOT PERMISSION TO GO QUIET, AND THESE TESTS EXIST TO KEEP IT THAT
WAY. Three properties are load-bearing, and each has a test whose only job is
to fail if the state widens:

  1. A REAL FAILURE ALWAYS OUTRANKS A WAIT — at the extractor, at the family,
     and at the board. An extractor that is waiting on one source and broken
     on another is `failed`, not waiting.
  2. THE WAIT EXPIRES. The producer may only claim it inside a bounded grace
     window (iso_jp_denkiyoho._UPSTREAM_MONTH_GRACE_DAYS). Past the window the
     same absent file is reported verbatim and the feed is red again.
  3. WAITING IS NOT GREEN. `awaiting_upstream` is deliberately NOT in
     routes.ingest_runs._OK_STATUS. The board publishes it in its own list,
     with the period being waited on, so a reader SEES the hole in coverage.

Behavioural throughout: the board tests drive the real GET handler over a fake
ledger, and the adapter tests drive _fetch_eria's real return path with a
stubbed HTTP layer and a stubbed clock.
"""
from __future__ import annotations

import datetime as dt

import pytest

from routes.ingest_runs import _OK_STATUS, _AWAITING_UPSTREAM

_H = dt.timedelta(hours=1)


# ── the evaluator: GET /api/v1/ops/deadman over a fake ledger ──────────
# Same shape as tests/test_deadman_late_vs_red.py, deliberately: these two
# files judge the same handler and a second harness would let them drift.

def _client(monkeypatch, rows):
    flask = pytest.importorskip("flask")
    import routes.ingest_runs as ir

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://ledger.example/db")
    monkeypatch.setattr(ir.psycopg2, "connect", lambda *a, **k: _Conn())
    app = flask.Flask(__name__)
    app.register_blueprint(ir.ingest_runs_bp)
    return app.test_client()


def _board(monkeypatch, rows):
    return _client(monkeypatch, rows).get("/api/v1/ops/deadman").get_json()


def _row(status, *, feed="iso-intl", age_h=0.1, rows_inserted=16,
         cz=0, mcd=None, note="awaiting upstream: OCCTO (hokkaido)"):
    now = dt.datetime.now(dt.timezone.utc)
    return (feed, now - age_h * _H, status, rows_inserted, mcd, 24.0, cz, note)


def _feed(body, name="iso-intl"):
    return next(f for f in body["feeds"] if f["feed"] == name)


# ── PROPERTY 3: waiting is published, and it is not green ──────────────

def test_awaiting_upstream_is_not_an_ok_status():
    """Kills: 'just add it to _OK_STATUS'.

    That one-line version passes every other test in this file while making
    the board say nothing at all about a feed with a hole in its coverage —
    which is the failure the whole ledger exists to stop."""
    assert "awaiting_upstream" not in _OK_STATUS
    assert not (_AWAITING_UPSTREAM & _OK_STATUS)


def test_a_feed_waiting_on_an_unpublished_period_is_neither_red_nor_overdue(monkeypatch):
    body = _board(monkeypatch, [_row("awaiting_upstream")])
    f = _feed(body)
    assert f["red"] is False
    assert f["overdue"] is False
    assert f["unhealthy"] is False
    assert body["red_count"] == 0
    assert body["any_red"] is False


def test_the_wait_is_published_in_its_own_list_with_the_period(monkeypatch):
    """Not-red must not mean invisible. A reader has to be able to see WHICH
    feed is short of coverage and WHAT it is waiting for."""
    note = ("awaiting upstream: OCCTO (mix: hokkaido:"
            "month_not_published_yet:202609) — 9 of 10 member(s) reported")
    body = _board(monkeypatch, [_row("awaiting_upstream", note=note)])
    assert body["awaiting_upstream_count"] == 1
    entry, = body["awaiting_upstream"]
    assert entry["feed"] == "iso-intl"
    assert "202609" in entry["note"]
    assert _feed(body)["awaiting_upstream"] is True
    assert "awaiting_upstream" in _feed(body)["kinds"]


def test_the_basis_string_tells_a_reader_it_is_neither_green_nor_red(monkeypatch):
    """The board's own prose is a published surface; a new state that the
    basis does not explain is a state nobody can interpret."""
    basis = _board(monkeypatch, [_row("awaiting_upstream")])["basis"]
    assert "awaiting_upstream" in basis
    assert "grace" in basis.lower()


# ── PROPERTY 1: a real failure always outranks a wait ──────────────────

def test_a_degraded_feed_is_still_red(monkeypatch):
    """★ NARROWNESS. The one-character version of this change — treating any
    non-OK status as a wait — would pass every test above."""
    body = _board(monkeypatch, [_row("degraded")])
    assert _feed(body)["red"] is True
    assert body["red_count"] == 1


@pytest.mark.parametrize("status", ["failed", "lanes_failing", "error", "degraded"])
def test_no_other_non_ok_status_becomes_a_wait(monkeypatch, status):
    body = _board(monkeypatch, [_row(status)])
    f = _feed(body)
    assert f["red"] is True
    assert f["awaiting_upstream"] is False
    assert body["awaiting_upstream_count"] == 0


def test_a_waiting_feed_that_also_stopped_inserting_rows_is_still_red(monkeypatch):
    """A wait must not be able to mask a SECOND fault arriving underneath it.
    Three consecutive zero-row runs is a fault in its own right."""
    body = _board(monkeypatch, [_row("awaiting_upstream", rows_inserted=0, cz=4)])
    f = _feed(body)
    assert f["red"] is True
    assert f["awaiting_upstream"] is False, "a red feed must not also be listed as merely waiting"
    assert body["awaiting_upstream_count"] == 0


def test_a_waiting_feed_that_stops_running_is_still_overdue(monkeypatch):
    """Waiting on an upstream period says nothing about whether OUR loop ran."""
    body = _board(monkeypatch, [_row("awaiting_upstream", age_h=72.0)])
    f = _feed(body)
    assert f["overdue"] is True
    assert f["unhealthy"] is True


# ── PROPERTY 1 at the extractor: classify_result ───────────────────────

def test_a_structured_wait_with_no_errors_is_a_wait_not_a_failure():
    from routes.iso_orchestrator import classify_result
    verdict, reason = classify_result(
        {"iso": "OCCTO", "errors": [], "rows_inserted": 16,
         "awaiting_upstream": ["hokkaido:month_not_published_yet:202609"]})
    assert verdict == "awaiting_upstream"
    assert "202609" in reason


def test_an_extractor_that_is_waiting_AND_broken_is_failed():
    """★ NARROWNESS. Order is the guard: if `waiting` were checked before
    `errors`, an extractor could launder a genuine failure by also waiting."""
    from routes.iso_orchestrator import classify_result
    verdict, reason = classify_result(
        {"iso": "OCCTO", "errors": ["mix: kansai:http_500"],
         "awaiting_upstream": ["hokkaido:month_not_published_yet:202609"]})
    assert verdict == "failed"
    assert "kansai" in reason


def test_an_extractor_with_no_wait_and_no_errors_is_unchanged():
    from routes.iso_orchestrator import classify_result
    assert classify_result({"iso": "OCCTO", "errors": []})[0] == "ok"


# ── PROPERTY 1 at the family: summarize_families ───────────────────────

_FAM = (("iso-intl", ("OCCTO", "AEMO")),)


def test_a_family_whose_only_shortfall_is_a_wait_is_not_degraded():
    from routes.iso_orchestrator import summarize_families
    by_iso = {"OCCTO": {"verdict": "awaiting_upstream", "rows_inserted": 0,
                        "reason": "hokkaido:month_not_published_yet:202609"},
              "AEMO": {"verdict": "ok", "rows_inserted": 40}}
    out = summarize_families(by_iso, [], families=_FAM)["iso-intl"]
    assert out["status"] == "awaiting_upstream"
    assert "202609" in out["note"]
    assert out["rows_inserted"] == 40


def test_the_family_note_names_how_many_members_did_report():
    """A note that only names the absentee reads as 'the family is waiting'."""
    from routes.iso_orchestrator import summarize_families
    by_iso = {"OCCTO": {"verdict": "awaiting_upstream", "rows_inserted": 0,
                        "reason": "hokkaido:month_not_published_yet:202609"},
              "AEMO": {"verdict": "ok", "rows_inserted": 40}}
    note = summarize_families(by_iso, [], families=_FAM)["iso-intl"]["note"]
    assert "1 of 2 member(s) reported" in note


def test_one_genuinely_failed_member_still_degrades_the_family():
    """★ NARROWNESS at the family grain."""
    from routes.iso_orchestrator import summarize_families
    by_iso = {"OCCTO": {"verdict": "awaiting_upstream", "rows_inserted": 0,
                        "reason": "hokkaido:month_not_published_yet:202609"},
              "AEMO": {"verdict": "failed", "rows_inserted": 0,
                       "reason": "http_503"}}
    out = summarize_families(by_iso, [], families=_FAM)["iso-intl"]
    assert out["status"] == "degraded"
    assert "AEMO" in out["note"]


# ── PROPERTY 2: the wait expires ───────────────────────────────────────

def _stub_eria(monkeypatch, day, *, status_code=404):
    """Drive _fetch_eria's real return path: fixed JST clock, stubbed HTTP."""
    import routes.iso_jp_denkiyoho as jp

    class _Resp:
        ok = False

    r = _Resp()
    r.status_code = status_code
    monkeypatch.setattr(jp, "_rq", type("RQ", (), {"get": staticmethod(lambda *a, **k: r)}))

    real_datetime = jp.datetime

    class _Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 9, day, 7, 40, tzinfo=jp._JST)

    monkeypatch.setattr(jp, "datetime", _Clock)
    return jp


def test_inside_the_grace_window_an_absent_month_is_named_as_a_wait(monkeypatch):
    jp = _stub_eria(monkeypatch, day=3)
    parsed, note = jp._fetch_eria("hokkaido")
    assert parsed is None
    assert note.startswith(jp.AWAITING_UPSTREAM_PREFIX)
    assert "202609" in note
    assert "raw=http_404" in note, "renaming the symptom must not delete the evidence"


def test_past_the_grace_window_the_same_absence_is_a_plain_fault(monkeypatch):
    """★ THE EXPIRY. Without this the marker is a permanent excuse: a monthly
    URL upstream had genuinely abandoned would carry the same reassuring
    wording on the 30th as on the 1st."""
    jp = _stub_eria(monkeypatch, day=20)
    parsed, note = jp._fetch_eria("hokkaido")
    assert parsed is None
    assert note == "http_404"
    assert jp.AWAITING_UPSTREAM_PREFIX not in note


def test_the_grace_window_is_a_named_bounded_constant():
    """An unbounded or absent window is the silenced-check failure mode."""
    import routes.iso_jp_denkiyoho as jp
    assert isinstance(jp._UPSTREAM_MONTH_GRACE_DAYS, int)
    assert 1 <= jp._UPSTREAM_MONTH_GRACE_DAYS <= 15


def test_a_non_404_failure_is_never_renamed_a_wait(monkeypatch):
    """★ NARROWNESS at the adapter. A 500 is a broken upstream, not a lag."""
    jp = _stub_eria(monkeypatch, day=3, status_code=500)
    _parsed, note = jp._fetch_eria("hokkaido")
    assert note == "http_500"
    assert jp.AWAITING_UPSTREAM_PREFIX not in note


# ── the seam between adapter and orchestrator ──────────────────────────

def test_the_extractor_routes_a_wait_out_of_the_fatal_errors_channel(monkeypatch):
    """errors[] is FATAL (classify_result reads it as failure). A wait must
    travel in its own structured key, and everything else must not."""
    import routes.iso_jp_denkiyoho as jp
    monkeypatch.setattr(jp, "_snapshot_all", lambda: (
        {}, {},
        {"tepco": {"demand_mw": 1.0, "as_of_jst": "2026-09-03T09:30:00+09:00"}},
        {"tepco": "ok",
         "hokkaido": jp.AWAITING_UPSTREAM_PREFIX + "202609 (day 3 JST)",
         "kansai": "http_500"}))
    monkeypatch.setattr(jp, "_persist", lambda *a, **k: 3)
    s = jp.run_extraction()
    assert s["awaiting_upstream"] == [
        "hokkaido:" + jp.AWAITING_UPSTREAM_PREFIX + "202609 (day 3 JST)"]
    joined = " ".join(s["errors"])
    assert "kansai:http_500" in joined, "a real failure must stay fatal"
    assert "hokkaido" not in joined, "the wait must not also be reported as an error"


def test_a_wait_alone_leaves_the_fatal_channel_empty(monkeypatch):
    import routes.iso_jp_denkiyoho as jp
    monkeypatch.setattr(jp, "_snapshot_all", lambda: (
        {}, {},
        {"tepco": {"demand_mw": 1.0, "as_of_jst": "2026-09-03T09:30:00+09:00"}},
        {"tepco": "ok",
         "hokkaido": jp.AWAITING_UPSTREAM_PREFIX + "202609 (day 3 JST)"}))
    monkeypatch.setattr(jp, "_persist", lambda *a, **k: 3)
    s = jp.run_extraction()
    assert s["errors"] == []
    from routes.iso_orchestrator import classify_result
    assert classify_result(s)[0] == "awaiting_upstream"
