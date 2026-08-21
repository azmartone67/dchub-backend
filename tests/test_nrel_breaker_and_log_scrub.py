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
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
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
