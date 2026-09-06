#!/usr/bin/env python3
"""tests/test_nrel_breaker_and_log_scrub.py — the NREL client must never log
its API key, and must not re-dial a dead upstream on every poll.

★ 2026-08-21 live: nrel.gov lost its NS delegation at the .gov registry; an
external monitor polls /api/renewable/solar every ~20s; every poll re-dialled
the dead host and logged
    NREL solar fetch failed: 502 … /pvwatts/v8.json?api_key=<THE REAL KEY>&lat=…
to Railway — 180 key-bearing lines an hour. These tests EXECUTE
get_solar_potential against a failing session stub.
"""
import logging
import os
import sys

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import enhancements.nrel_renewable as nr  # noqa: E402

SECRET = "SEKRIT-KEY-VALUE-0123456789"


class _FailingSession:
    """★ The double MIRRORS requests.Session.get, kwargs included.

    It used to declare only (url, params, timeout). When the client started
    sending headers= (2026-09-06, key out of the query string) every test in
    this file died on TypeError rather than on the behaviour it was written to
    check — a double narrower than the real interface turns a correct change
    into four red tests that say nothing about the change.

    It also now RECORDS what it was handed, so the tests below can assert where
    the credential actually went instead of only where it ended up in the log.
    """

    def __init__(self):
        self.calls = 0
        self.last_params = None
        self.last_headers = None

    def get(self, url, params=None, timeout=None, headers=None, **kwargs):
        self.calls += 1
        self.last_params = dict(params or {})
        self.last_headers = dict(headers or {})
        raise requests.exceptions.ConnectionError(
            f"502 Server Error: Bad Gateway for url: {url}?api_key={SECRET}&lat=36.17&lon=-115.14")


@pytest.fixture(autouse=True)
def _closed_breaker(monkeypatch):
    monkeypatch.setattr(nr, "_NREL_DOWN_UNTIL", 0.0)
    monkeypatch.setattr(nr, "NREL_BREAKER_SECONDS", 600)
    yield


def _client():
    c = nr.NRELClient(api_key=SECRET)
    c.session = _FailingSession()
    return c


def test_the_api_key_never_reaches_the_log(caplog):
    c = _client()
    with caplog.at_level(logging.WARNING):
        out = c.get_solar_potential(36.17, -115.14)
    assert out["available"] is False
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "NREL solar fetch failed" in joined, "the failure must still be logged"
    assert SECRET not in joined, f"API key leaked into the log: {joined[:200]}"
    assert "api_key=REDACTED" in joined


def test_a_failure_opens_the_breaker_so_the_next_poll_does_not_dial(monkeypatch):
    c = _client()
    first = c.get_solar_potential(36.17, -115.14)
    assert c.session.calls == 1 and first["breaker"] == "open"
    second = c.get_solar_potential(36.17, -115.14)
    assert c.session.calls == 1, "the second call re-dialled the dead upstream"
    assert second["available"] is False and 0 < second["retry_after_s"] <= 600


def test_the_breaker_closes_after_the_window(monkeypatch):
    c = _client()
    c.get_solar_potential(36.17, -115.14)
    assert c.session.calls == 1
    monkeypatch.setattr(nr._time, "time", lambda: nr._NREL_DOWN_UNTIL + 1)
    c.get_solar_potential(36.17, -115.14)
    assert c.session.calls == 2, "the breaker never closed"


def test_breaker_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setattr(nr, "NREL_BREAKER_SECONDS", 0)
    c = _client()
    c.get_solar_potential(36.17, -115.14)
    c.get_solar_potential(36.17, -115.14)
    assert c.session.calls == 2


def test_scrub_handles_both_query_shapes():
    assert nr._scrub_secret("x?api_key=abc&lat=1") == "x?api_key=REDACTED&lat=1"
    assert nr._scrub_secret("api_key=abc") == "api_key=REDACTED"
    assert "abc" not in nr._scrub_secret("…'api_key=abc'…")


def test_the_key_travels_in_a_header_not_the_query_string(monkeypatch):
    """BASE_URL is our own dchub.cloud worker proxy, and that zone's CF request
    logs record the full path — so a credential in params= is a credential in
    a log. The worker reads X-Api-Key (dchub-frontend #1397)."""
    monkeypatch.setattr(nr, "_NREL_DOWN_UNTIL", 0.0)
    c = nr.NRELClient(SECRET)
    sess = _FailingSession()
    c.session = sess
    c.get_solar_potential(36.17, -115.14)

    assert sess.calls == 1, "the client did not dial at all"
    assert sess.last_headers.get("X-Api-Key") == SECRET, (
        f"credential missing from headers: {sorted(sess.last_headers)}")
    leaked = [k for k, v in sess.last_params.items() if v == SECRET]
    assert not leaked, (
        f"credential handed to params={leaked} — it becomes a query string "
        "on a request to dchub.cloud, which logs the full path")


class _OkSession:
    """Minimal happy-path double, same signature discipline as the failing one."""

    def __init__(self):
        self.last_headers = None

    def get(self, url, params=None, timeout=None, headers=None, **kwargs):
        self.last_headers = dict(headers or {})

        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"outputs": {"ac_annual": 1234.0, "solrad_annual": 5.0,
                                    "capacity_factor": 21.0}}
        return _R()


def _beats(monkeypatch):
    seen = []
    monkeypatch.setattr(nr, "_beat", lambda status, error=None: seen.append(status))
    return seen


def test_a_dead_upstream_beats_error_so_the_silence_is_visible(monkeypatch):
    """★ The point of the heartbeat. This lane returned 502 for an unknown
    length of time (developer.nrel.gov has no DNS records at all) and nothing
    said so: the breaker logs once per window and the caller gets a tidy
    'temporarily unavailable'. Without a beat on the failure edge, a dead lane
    is indistinguishable from an idle one."""
    monkeypatch.setattr(nr, "_NREL_DOWN_UNTIL", 0.0)
    seen = _beats(monkeypatch)
    c = nr.NRELClient(SECRET)
    c.session = _FailingSession()
    c.get_solar_potential(36.17, -115.14)
    assert "error" in seen, "breaker tripped but nothing reported the lane down"


def test_a_working_upstream_beats_success(monkeypatch):
    """Positive control: without this, an always-error beat would satisfy the
    test above while telling the registry nothing about recovery."""
    monkeypatch.setattr(nr, "_NREL_DOWN_UNTIL", 0.0)
    seen = _beats(monkeypatch)
    c = nr.NRELClient(SECRET)
    c.session = _OkSession()
    c.get_solar_potential(36.17, -115.14)
    assert seen == ["success"], seen


def test_the_heartbeat_never_breaks_the_lane_it_watches(monkeypatch):
    """A monitoring call must not raise into a user-facing lookup path."""
    import dchub_heartbeat
    monkeypatch.setattr(dchub_heartbeat, "heartbeat",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("registry down")))
    monkeypatch.setattr(nr, "_NREL_DOWN_UNTIL", 0.0)
    c = nr.NRELClient(SECRET)
    c.session = _OkSession()
    out = c.get_solar_potential(36.17, -115.14)   # must not raise
    assert isinstance(out, dict)
