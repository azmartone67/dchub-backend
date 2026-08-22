#!/usr/bin/env python3
"""tests/test_programmatic_access_probe_tiers.py — the public-API probe has two
tiers: real clients are a GUARD (403 → exit 1), bare urllib is a GAUGE (403/1010
→ reported, exit 0).

★ 2026-08-22: the urllib 1010 is the Cloudflare PAGES pipeline's own Browser
Integrity Check (pages.dev 403s it; api.dchub.cloud 200s; zone BIC off since
08-13; no zone object can reach it). Owner chose not to reroute production
paths for a UA no real client sends, so the check became a gauge — but the
clients third parties actually use must keep failing the run if blocked.
These tests EXECUTE main() with probe() stubbed per (UA, path).
"""
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "check_public_api_programmatic_access.py")


def _load():
    spec = importlib.util.spec_from_file_location("pa_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Opener:
    def __init__(self, ua):
        self.addheaders = [("User-agent", ua)]


def _arm(monkeypatch, mod, decide, default_ua="Python-urllib/3.11"):
    """decide(ua, path) -> (status, body_bytes)."""
    monkeypatch.setattr(mod, "build_honest_opener", lambda: (_Opener(default_ua), default_ua))
    monkeypatch.setattr(mod, "build_client_opener", lambda ua: _Opener(ua))

    def fake_probe(opener, url, timeout=30):
        ua = dict(opener.addheaders)["User-agent"]
        path = url.split("dchub.cloud", 1)[1].split("?")[0]
        status, body = decide(ua, path)
        return (status, "BYPASS", "ray", body)

    monkeypatch.setattr(mod, "probe", fake_probe)
    monkeypatch.setattr(sys, "argv", ["probe", "--base", "https://dchub.cloud", "--report", "/dev/null"])


def test_pages_layer_urllib_block_is_a_gauge_not_a_failure(monkeypatch, capsys, tmp_path):
    mod = _load()
    rep = tmp_path / "r.json"

    def decide(ua, path):
        if ua.startswith("Python-urllib/"):
            return 403, b"error code: 1010"
        return 200, b"ok"

    _arm(monkeypatch, mod, decide)
    monkeypatch.setattr(sys, "argv", ["probe", "--base", "https://dchub.cloud", "--report", str(rep)])
    assert mod.main() == 0, "the known Pages-layer urllib block must not fail the run"
    out = capsys.readouterr().out
    assert "::warning::GAUGE" in out and "PAGES" in out
    d = json.loads(rep.read_text())
    assert d["gauge"]["blocked_by_pages_bic"] is True
    assert set(d["gauge"]["paths"]) == set(mod.DEFAULT_PATHS)
    assert d["guard"]["blocked"] == []


def test_a_real_client_403_still_fails_the_run(monkeypatch, capsys):
    mod = _load()

    def decide(ua, path):
        if ua.startswith("python-requests/") and path == "/api/v1/stats":
            return 403, b"blocked"
        return 200, b"ok"

    _arm(monkeypatch, mod, decide)
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "REGRESSION" in err and "/api/v1/stats [python-requests/" in err


def test_every_real_client_ua_is_actually_probed(monkeypatch):
    mod = _load()
    seen = set()

    def decide(ua, path):
        seen.add(ua)
        return 200, b"ok"

    _arm(monkeypatch, mod, decide)
    assert mod.main() == 0
    assert set(mod.REAL_CLIENT_UAS) <= seen, "a guard UA was never sent"
    assert "Python-urllib/3.11" in seen, "the gauge UA was never sent"


def test_transport_error_on_a_real_client_is_not_a_pass(monkeypatch):
    mod = _load()

    def decide(ua, path):
        if ua.startswith("curl/") and path == "/robots.txt":
            return None, b"timed out"
        return 200, b"ok"

    _arm(monkeypatch, mod, decide)
    assert mod.main() == 1


def test_gauge_lift_is_announced_as_a_notice(monkeypatch, capsys):
    mod = _load()
    _arm(monkeypatch, mod, lambda ua, path: (200, b"ok"))
    assert mod.main() == 0
    assert "::notice::GAUGE" in capsys.readouterr().out


def test_overridden_default_ua_still_aborts_the_gauge_honestly(monkeypatch):
    """The vacuity trap from 2026-08-10 still holds: if something forces a
    browser UA onto urllib, the gauge refuses to report rather than pass."""
    mod = _load()
    monkeypatch.setattr(mod, "build_client_opener", lambda ua: _Opener(ua))
    monkeypatch.setattr(mod, "probe", lambda opener, url, timeout=30: (200, "-", "-", b"ok"))
    monkeypatch.setattr(sys, "argv", ["probe", "--base", "https://dchub.cloud"])
    real_build = mod.urllib.request.build_opener

    def browser_opener():
        o = real_build()
        o.addheaders = [("User-agent", "Mozilla/5.0")]
        return o

    monkeypatch.setattr(mod.urllib.request, "build_opener", browser_opener)
    with pytest.raises(SystemExit) as ex:
        mod.main()
    assert ex.value.code == 2
