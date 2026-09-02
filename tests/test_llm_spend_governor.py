"""LLM spend governor — sheds customer-facing consumers before the brain starves.

Measured 2026-09-01: one $100/7d gateway rule shared by every consumer; the
reasoning layers (<5% of calls) went dark with the demo/narrative generators
(~60%). Ships DARK: no cap set -> nothing sheds, requests.post is called as
before. Never imports main; requests.post is stubbed.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

gov = pytest.importorskip("util.llm_spend_governor")  # noqa: E402


@pytest.fixture(autouse=True)
def _dark(monkeypatch):
    for k in (gov.CAP_ENV, gov.RATE_ENV, gov.SHED_LIST_ENV, gov.DISABLE_ENV):
        monkeypatch.delenv(k, raising=False)
    gov._CACHE.update(at=0.0, tokens=None)


def _arm(monkeypatch, cap="100", rate="1"):
    monkeypatch.setenv(gov.CAP_ENV, cap)
    monkeypatch.setenv(gov.RATE_ENV, rate)


def test_dark_by_default():
    # Kills: any default cap (the governor must not shed on a fresh deploy).
    cfg = gov.config()
    assert cfg["active"] is False and "must both be set" in cfg["why_inactive"]
    assert gov.decide("demo", 99.0, cfg)["shed"] is False
    assert gov.shed_response("demo") is None


def test_both_numbers_are_required(monkeypatch):
    monkeypatch.setenv(gov.CAP_ENV, "100")
    assert gov.config()["active"] is False
    monkeypatch.setenv(gov.RATE_ENV, "abc")
    assert gov.config()["active"] is False


def test_sheds_listed_consumers_at_80_percent_only(monkeypatch):
    _arm(monkeypatch)
    cfg = gov.config()
    assert gov.decide("demo", 79.9, cfg)["shed"] is False
    d = gov.decide("demo", 80.0, cfg)
    assert d["shed"] is True and d["ratio"] == 0.8 and "headroom" in d["reason"]
    assert gov.decide("report_narrative", 95.0, cfg)["shed"] is True
    assert gov.decide("market_deep_dive", 95.0, cfg)["shed"] is True


def test_brain_layers_are_never_shed(monkeypatch):
    # Kills: inverting the list check (shedding everything BUT the list).
    _arm(monkeypatch)
    cfg = gov.config()
    for layer in ("brain_layer16_outcomes", "brain_layer14", "brain_inspector"):
        assert gov.decide(layer, 150.0, cfg)["shed"] is False


def test_unmeasured_spend_never_sheds(monkeypatch):
    _arm(monkeypatch)
    d = gov.decide("demo", None, gov.config())
    assert d["shed"] is False and "unmeasured" in d["reason"]


def test_kill_switch(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv(gov.DISABLE_ENV, "1")
    assert gov.config()["active"] is False


def test_spend_is_tokens_times_the_operator_rate(monkeypatch):
    _arm(monkeypatch, cap="100", rate="2.5")
    monkeypatch.setattr(gov, "tokens_7d", lambda conn_fn=None: 40_000_000)
    assert gov.spent_usd(gov.config()) == 100.0


def test_shed_response_is_429_shaped_and_structured(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(gov, "tokens_7d", lambda conn_fn=None: 90_000_000)
    r = gov.shed_response("demo")
    assert r is not None and r.status_code == 429 and r.ok is False
    body = r.json()
    assert body["type"] == "shed" and body["error"]["type"] == "spend_governor_shed"
    assert body["governor"]["shed"] is True


def test_instrumented_post_refuses_before_the_request(monkeypatch):
    # Kills: calling requests.post anyway (spend happens, then we "shed").
    from routes import brain_llm_spend as s
    import requests
    _arm(monkeypatch)
    monkeypatch.setattr(gov, "tokens_7d", lambda conn_fn=None: 90_000_000)
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, **k: calls.append(url) or None)
    recorded = []
    monkeypatch.setattr(s, "record", lambda *a, **k: recorded.append(k) or True)
    r = s.instrumented_post("demo", "http://gw", json={"model": "m"})
    assert calls == [] and r.status_code == 429 and r.json()["type"] == "shed"
    assert recorded and recorded[0]["stop_reason"] == "shed"


def test_instrumented_post_is_untouched_when_dark(monkeypatch):
    from routes import brain_llm_spend as s
    import requests
    sentinel = type("R", (), {"status_code": 200, "json": lambda self: {}})()
    monkeypatch.setattr(requests, "post", lambda url, **k: sentinel)
    monkeypatch.setattr(s, "record", lambda *a, **k: True)
    assert s.instrumented_post("demo", "http://gw") is sentinel


def test_a_brain_layer_passes_through_even_when_armed(monkeypatch):
    from routes import brain_llm_spend as s
    import requests
    _arm(monkeypatch)
    monkeypatch.setattr(gov, "tokens_7d", lambda conn_fn=None: 90_000_000)
    sentinel = type("R", (), {"status_code": 200, "json": lambda self: {}})()
    monkeypatch.setattr(requests, "post", lambda url, **k: sentinel)
    monkeypatch.setattr(s, "record", lambda *a, **k: True)
    assert s.instrumented_post("brain_layer16_outcomes", "http://gw") is sentinel


def test_status_reports_dark_and_armed(monkeypatch):
    st = gov.status()
    assert st["active"] is False and st["shedding_now"] is False
    _arm(monkeypatch)
    monkeypatch.setattr(gov, "tokens_7d", lambda conn_fn=None: 85_000_000)
    st = gov.status()
    assert st["active"] and st["ratio"] == 0.85 and st["shedding_now"] is True
    assert "demo" in st["shed_consumers"]


def test_model_probe_carries_the_governor(monkeypatch):
    src = open(os.path.join(ROOT, "routes", "brain_v3.py"), encoding="utf-8").read()
    assert "spend_governor" in src and "llm_spend_governor" in src
