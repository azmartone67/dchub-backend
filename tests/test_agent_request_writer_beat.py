"""The agent_requests writer's dead-man beat, turned into a red CI job (2026-08-17).

WHAT HAPPENED
=============
`agent_request_writer.py`'s module docstring told operators to verify the writer
via the `agent_requests_writer:<idx>` feed on GET /api/v1/ops/deadman. That feed
had never existed. Measured against production 2026-08-17:

    curl https://dchub.cloud/api/v1/ops/deadman
    -> tracked: 68, and ZERO feeds matching `agent_request` or `writer`

...while AGENT_REQUESTS_WRITER_ENABLE=1 on the Railway web service, so the
flusher thread was running and beating once a minute the entire time.

THREE faults, each individually sufficient, none of them visible:

  1. AUTH. `_beat` POSTed loopback with `X-Admin-Key: $ADMIN_API_KEY`.
     `ADMIN_API_KEY` is a name no other module in this repo uses;
     `routes.ingest_runs._admin_ok()` compares against DCHUB_ADMIN_KEY /
     DCHUB_INTERNAL_KEY. Both names are set in prod to DIFFERENT values, so the
     `or` fallback never fired. Confirmed live against the Railway origin:
     POST .../ingest-runs/beat with the ADMIN_API_KEY value -> HTTP 401.
  2. FIELD NAME. The body carried the counters as `detail`. The handler reads
     `note` and has never known a `detail` field — so a beat that HAD
     authenticated would still have dropped every counter, including the
     `poisoned` counter added that same day (PR #2798) specifically so poison
     drops would be observable.
  3. SILENCE. The whole thing was wrapped in `except: log.debug(...)`, which is
     below the default level. Nothing was ever emitted.

The fix calls `routes.ingest_runs.record_beat` directly — the same upsert the
HTTP handler calls — so there is no loopback hop, no admin key and no gate to
get wrong, and a failure logs at WARNING.

House rules honoured: nothing runs at module scope, and `main` is never
imported — `_beat` imports `routes.ingest_runs` lazily, so a stub is enough.
"""
import importlib
import logging
import sys
import types


def _writer():
    """Fresh module state per test (the buffer and counters are globals)."""
    sys.modules.pop("agent_request_writer", None)
    m = importlib.import_module("agent_request_writer")
    m._started = True          # never start the real background flusher thread
    m._buf.clear()
    for k in ("enqueued", "inserted", "dropped", "poisoned"):
        m._stats[k] = 0
    return m


def _install_ledger(monkeypatch, sink=None, boom=None):
    """Stub the lazy `from routes.ingest_runs import record_beat` inside _beat.

    Stubs the parent package too, so the real routes/ package is never imported
    and this test cannot depend on Flask app state or a live DATABASE_URL.
    """
    fake = types.ModuleType("routes.ingest_runs")

    def record_beat(feed, **kw):
        if boom is not None:
            raise boom
        if sink is not None:
            sink.append((feed, kw))

    fake.record_beat = record_beat
    monkeypatch.setitem(sys.modules, "routes", types.ModuleType("routes"))
    monkeypatch.setitem(sys.modules, "routes.ingest_runs", fake)
    return fake


def test_beat_records_to_the_ledger():
    """THE REGRESSION: a beat must actually reach the dead-man upsert."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        m = _writer()
        sink = []
        _install_ledger(mp, sink)

        m._beat("ok")

        assert len(sink) == 1, "the beat never reached record_beat"
        feed, kw = sink[0]
        assert feed == m._FEED
        assert feed.startswith("agent_requests_writer:")
        assert kw["status"] == "ok"
        assert kw["rows"] == 1              # liveness sentinel, never a zero-row alarm
    finally:
        mp.undo()


def test_counters_ride_in_note_the_field_the_ledger_actually_reads():
    """Fault 2. `detail` was silently discarded; `note` is what deadman renders."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        m = _writer()
        sink = []
        _install_ledger(mp, sink)
        m._stats["poisoned"] = 7
        m._stats["inserted"] = 42

        m._beat("ok")

        _, kw = sink[0]
        assert "detail" not in kw, "the ledger has no `detail` field — counters vanish"
        note = kw["note"]
        assert "poisoned=7" in note, "the counter PR #2798 added must be readable"
        assert "inserted=42" in note
        assert len(note) <= 280, "must respect the handler's own note cap"
    finally:
        mp.undo()


def test_beat_needs_no_admin_key_at_all():
    """Fault 1. Strip EVERY admin env: the beat must still land.

    Pins the property, not the spelling — this passes for any future key name
    only because auth is no longer on the path.
    """
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        m = _writer()
        sink = []
        _install_ledger(mp, sink)
        for name in ("ADMIN_API_KEY", "DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY"):
            mp.delenv(name, raising=False)

        m._beat("ok")

        assert len(sink) == 1, "beat still depends on an admin key"
    finally:
        mp.undo()


def test_source_carries_no_admin_api_key_and_no_loopback_post():
    """Belt-and-braces on fault 1: the wrong env name must be gone for good.

    A behavioural test alone would pass if someone re-added the loopback POST as
    a *fallback*, which is how this reappears.
    """
    import inspect
    m = _writer()
    src = inspect.getsource(m)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    # docstrings deliberately NAME the dead env var to explain the outage, so
    # only look at the half of the module that executes.
    body = code.split('"""')
    executable = "".join(body[::2])
    assert "ADMIN_API_KEY" not in executable, "the 401-causing env name is back"
    assert "urlopen" not in executable, "the loopback beat POST is back"


def test_a_dropped_beat_is_logged_at_warning(caplog):
    """Fault 3. The failure must be visible at the DEFAULT log level."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        m = _writer()
        _install_ledger(mp, boom=RuntimeError("no DATABASE_URL"))

        with caplog.at_level(logging.WARNING, logger="agent_request_writer"):
            m._beat("ok")       # must not raise into the flusher loop

        hits = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert hits, "a dropped beat logged below WARNING is a silent failure"
        assert "no DATABASE_URL" in hits[0].getMessage()
    finally:
        mp.undo()


def test_oracle_actually_bites_on_a_debug_level_log(caplog):
    """Guard the guard: prove the WARNING oracle above can FAIL.

    If caplog captured debug records at this threshold, the test above would
    pass unchanged against the original `log.debug` and guard nothing.
    """
    log = logging.getLogger("agent_request_writer")
    with caplog.at_level(logging.WARNING, logger="agent_request_writer"):
        log.debug("writer beat failed: %s", "HTTP Error 401: UNAUTHORIZED")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
