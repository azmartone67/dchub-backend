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


def test_a_declared_success_is_not_a_skip(caplog=None):
    """★ The false positive this sensor shipped with.

    brain_weekly_digest returns {"ok": True, "skipped": "already_sent_this_week"}
    — the handler is DECLARING SUCCESS. strategic_digest_weekly is dispatched on
    every heartbeat by design (`lambda now: True`, chosen after narrow minute
    windows lost weeks to GitHub-cron latency), so reading that as a failure
    wrote ~450 rows/day into a table whose premise is that a healthy system
    writes ~0 and the table itself is the alert."""
    res = ch._classify(200, _b({"ok": True, "skipped": "already_sent_this_week",
                                "week_of": "2026-08-24"}))
    assert res["outcome"] == "ok"


@pytest.mark.parametrize("doc", [
    {"skipped": 1},                                   # dedup_drain, live
    {"ok": True, "merged": 3, "skipped": 12},         # brain_automerge shape
    {"filed": 0, "resolved": 0, "skipped": 7},        # cadence_sentinel shape
])
def test_a_numeric_skipped_is_a_count_not_a_verdict(doc):
    """`skipped` is a COUNT in at least eight handlers (skipped=len(...)).
    Reading it as a skip reason reported failure in proportion to how much
    work a job legitimately filtered — and skipped=0 passed only by being
    falsy, so the bug scaled with activity."""
    assert ch._classify(200, _b(doc))["outcome"] == "ok"


def test_a_real_declared_skip_still_registers():
    """Do not over-correct into silence: a shell that declares a skip REASON
    and does not claim success is still a skip."""
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


# ── a read timeout is not a verdict (2026-08-30) ──────────────────────
# The sensor's THIRD false alarm in two days, same shape as the first two: a
# true observation ("we stopped waiting") wired into a failure claim ("the job
# is unreachable"). All four "unreachable" rows in the 08-30 window were
# `TimeoutError: timed out` at http_status 0, and the one with an independent
# watermark — iso_queue_ingest_daily, logged unreachable 06:03:33.106Z — had
# written 10 of 10 ISOs by 06:03:47Z. It succeeded 14s after we condemned it.
def _hit_raising(exc):
    """Run the real _hit with urlopen replaced by a raiser."""
    import urllib.request as _u
    real = _u.urlopen
    _u.urlopen = lambda *a, **k: (_ for _ in ()).throw(exc)
    try:
        return ch._hit("http://127.0.0.1:8080/api/v1/iso-queue/ingest")
    finally:
        _u.urlopen = real


def test_a_read_timeout_is_not_called_unreachable():
    res = _hit_raising(TimeoutError("timed out"))
    assert res["outcome"] == "dispatch_timeout", (
        "a timeout means the handler is still working, not that nothing "
        "was listening")
    assert res["outcome"] != "unreachable"


def test_a_real_transport_failure_is_still_unreachable():
    """★ The other direction. The fix must not launder every transport
    failure into silence — a closed port is genuinely unreachable and must
    still say so."""
    assert _hit_raising(ConnectionRefusedError(61, "Connection refused")
                        )["outcome"] == "unreachable"
    assert _hit_raising(OSError("No route to host"))["outcome"] == "unreachable"


def test_dispatch_timeout_is_recorded_not_swallowed():
    """★ Not silenced either. A handler exceeding the dispatch budget on web
    is pool pressure and stays visible — it just stops claiming failure."""
    assert "dispatch_timeout" in co.CRON_OUTCOME_KINDS
    out = ch._run_batch(
        [("iso_queue_ingest_daily", "http://x/i", "POST")], 2,
        hit=lambda u, m: {"status": 0, "outcome": "dispatch_timeout"})
    assert [o["label"] for o in out] == ["iso_queue_ingest_daily"]


def test_info_and_failure_kinds_are_disjoint_and_complete():
    assert set(co.CRON_INFO_KINDS).isdisjoint(co.CRON_FAILURE_KINDS)
    assert (set(co.CRON_FAILURE_KINDS) | set(co.CRON_INFO_KINDS)
            == set(co.CRON_OUTCOME_KINDS))
    assert "dispatch_timeout" in co.CRON_INFO_KINDS
    assert "dispatch_timeout" not in co.CRON_FAILURE_KINDS


# ── the endpoint: healthy ignores info kinds, and is readable past CF ──
def _client(by_label):
    """A test client over the blueprint with the DB stubbed to `by_label`."""
    from flask import Flask

    class _Cur:
        def __init__(self): self._n = 0
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): self._n += 1
        def fetchall(self):
            if self._n == 1:                      # the rows query
                return []
            return [(r["label"], r["outcome"], r["count"]) for r in by_label]

    class _C:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    co._pg = object()
    co._dsn = lambda: "postgres://stub"
    co._conn = lambda: _C()
    app = Flask(__name__)
    app.register_blueprint(co.cron_observability_bp)
    return app.test_client()


def test_a_dispatch_timeout_alone_still_reads_healthy():
    c = _client([{"label": "iso_queue_ingest_daily",
                  "outcome": "dispatch_timeout", "count": 4}])
    doc = c.get("/api/v1/cron/job-outcomes").get_json()
    assert doc["healthy"] is True, "an info kind must never flip healthy"
    assert doc["failing_labels"] == 0
    assert doc["by_label"], "…and must still be visible in the body"


def test_a_real_failure_still_reads_unhealthy():
    """★ The over-correction guard: healthy must still be able to go false."""
    c = _client([{"label": "dcpi_chat_prewarm_cheyenne",
                  "outcome": "http_error", "count": 5}])
    doc = c.get("/api/v1/cron/job-outcomes").get_json()
    assert doc["healthy"] is False
    assert doc["failing_labels"] == 1


def test_the_table_is_served_on_a_cf_bypassing_path():
    """Cloudflare caches /api/v1/cron/* and REWRITES the origin's no-store on
    the way out (measured MISS -> HIT -> HIT, age climbing, 2026-08-30), so a
    "what is failing right now" table was answered from the edge. No origin
    header can fix that. /api/v1/brain/* carries the bypass — DYNAMIC on every
    read — so the same view is served there too."""
    c = _client([])
    assert c.get("/api/v1/brain/cron-job-outcomes").status_code == 200
    assert c.get("/api/v1/cron/job-outcomes").status_code == 200, (
        "the original path must keep working")
