"""Cron dispatch outcomes (r-cron-outcome 2026-08-29).

The heartbeat dispatches ~60 jobs per fire and observed NONE of them:
_run submitted `_hit` to a thread pool and never read the future, and `_hit`
had already thrown the response BODY away (`resp.read(512)` ->
{"status","bytes"}). So three different failures all counted as a successful
run in /api/v1/cron/last-fired's jobs_run:

  · HTTP 500 from a job
  · a job that timed out
  · HTTP 200 carrying {"ok":false,"disabled":true}

The third is the expensive one, because our own endpoints self-report at
HTTP 200. brain_fix_verify_sweep answers {"ok":false,"disabled":true}
whenever BRAIN_FIX_VERIFY!=1 — so an unarmed verifier fires at 10:00 and
22:00 forever while every dashboard says the job ran.

These tests pin (a) that each of those is classified as its own outcome,
(b) that a body we cannot parse is NEVER reported as a failure, and (c) that
the dispatch loop actually reads its futures.

No network, no DB.
Run with:  python3 -m pytest tests/test_cron_dispatch_outcomes.py -v
"""
import json
import pytest

import routes.cron_heartbeat as ch
import routes.cron_observability as co


def _b(doc):
    return json.dumps(doc).encode()


# ── classification ────────────────────────────────────────────────────
def test_disarmed_verifier_is_not_a_success():
    """The payload brain_fix_verify_sweep actually returns when unarmed."""
    body = _b({"ok": False, "disabled": True,
               "error": "BRAIN_FIX_VERIFY!=1 — verifier not armed"})
    res = ch._classify(200, body)
    assert res["outcome"] == "disarmed"
    assert "BRAIN_FIX_VERIFY" in res["detail"]


def test_skipped_shell_is_not_a_success():
    res = ch._classify(200, _b({"skipped": "already_ran_today"}))
    assert res["outcome"] == "skipped"
    assert res["detail"] == "already_ran_today"


def test_self_reported_failure_at_200():
    res = ch._classify(200, _b({"ok": False, "error": "db_unavailable"}))
    assert res["outcome"] == "self_reported_failure"
    assert res["detail"] == "db_unavailable"


def test_a_working_job_is_ok():
    assert ch._classify(200, _b({"ok": True, "embedded": 42}))["outcome"] == "ok"


@pytest.mark.parametrize("status,expected", [
    (500, "http_error"), (503, "http_error"), (404, "http_error"),
    (0, "unreachable"),
])
def test_transport_failures(status, expected):
    assert ch._classify(status, b"")["outcome"] == expected


@pytest.mark.parametrize("body", [
    b"<html><body>OK</body></html>",     # HTML endpoints exist in _DISPATCH
    b"",                                  # empty body
    b'{"ok": true, "trunc',                # truncated mid-JSON
    b"[1,2,3]",                           # JSON but not an object
])
def test_unparseable_body_is_never_called_a_failure(body):
    """Plenty of dispatched endpoints answer HTML or text. Crying wolf on
    those would bury the real signal on day one."""
    assert ch._classify(200, body)["outcome"] == "ok"


def test_body_read_is_large_enough_to_see_the_verdict():
    """The old 512-byte read truncated mid-JSON on any real payload, which is
    WHY the body was unparseable and the verdict invisible. This pins that a
    realistic response is still classifiable."""
    doc = {"ok": False, "disabled": True,
           "error": "BRAIN_FIX_VERIFY!=1 — verifier not armed",
           "detail": "x" * 800}
    body = _b(doc)
    assert len(body) > 512, "test payload must exceed the OLD read size"
    assert len(body) <= ch._HIT_BODY_BYTES
    assert ch._classify(200, body)["outcome"] == "disarmed"
    # and prove the old size could NOT have seen it
    assert ch._classify(200, body[:512])["outcome"] == "ok"


# ── the dispatch loop actually reads its futures ──────────────────────
def test_run_batch_surfaces_only_non_ok():
    def fake(url, method):
        if "verify" in url:
            return {"status": 200, "outcome": "disarmed", "detail": "unarmed"}
        return {"status": 200, "outcome": "ok"}
    out = ch._run_batch([("brain_fix_verify_sweep", "http://x/verify", "POST"),
                         ("grid_warm", "http://x/grid", "POST")], 4, hit=fake)
    assert [o["label"] for o in out] == ["brain_fix_verify_sweep"]


def test_run_batch_catches_a_raising_hit():
    """_hit swallows its own errors; if that ever changes, the dispatch must
    still record rather than lose the job."""
    def boom(url, method):
        raise RuntimeError("socket exploded")
    out = ch._run_batch([("j", "http://x", "POST")], 2, hit=boom)
    assert len(out) == 1 and out[0]["outcome"] == "unreachable"


def test_run_batch_is_empty_when_everything_works():
    out = ch._run_batch([("a", "http://x", "POST"), ("b", "http://y", "POST")],
                        4, hit=lambda u, m: {"status": 200, "outcome": "ok"})
    assert out == []


# ── the writer drops what it should ───────────────────────────────────
def test_recorder_drops_ok_and_unknown_outcomes(monkeypatch):
    captured = {}
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, payload): captured["payload"] = payload
    class _C:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(co, "_conn", lambda: _C())
    n = co.record_job_outcomes([
        {"label": "a", "outcome": "ok"},                    # dropped
        {"label": "b", "outcome": "not_a_real_kind"},       # dropped
        {"label": "c", "outcome": "disarmed", "status": 200, "detail": "d"},
    ])
    assert n == 1
    assert [p[0] for p in captured["payload"]] == ["c"]


def test_recorder_never_raises_when_the_db_is_down(monkeypatch):
    def boom(): raise RuntimeError("no db")
    monkeypatch.setattr(co, "_conn", boom)
    assert co.record_job_outcomes(
        [{"label": "a", "outcome": "disarmed"}]) == 0
