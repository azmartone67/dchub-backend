"""A provider key leaves in a header, never in the URL — checked on the wire.

tests/test_no_provider_key_in_url.py reads the SOURCE for the shapes that put a
credential in a query string. These tests drive the call sites that send EIA's
and Google AI Studio's keys, record the request each would actually send, and
assert the key is absent from the URL and present in the header the provider
reads:

  X-Api-Key       routes/eia930.fetch_eia930_ba, and every api.eia.gov entry
                  in the EIA-930 ISO URL lists, through the real
                  routes/_iso_common.fetch_first_working
                  api_auto_discovery.APIAutoDiscovery.discover_eia_catalog
                  routes/grid_data_master_shell._eia_henry_hub, through the
                  real _http_json
                  eia_api.make_eia_request
                  main._grid_intel_fetch, both RTO calls, compiled out of
                  main.py (importing main boots the whole app)
                  services/nepa_scraper._request, api.data.gov, which
                  reads the same header
  x-goog-api-key  ai_wars_battle_runner.call_platform_api("gemini") — and no
                  platform in that table sends its key in a URL

Requests are recorded at the transport — urllib.request.urlopen, or a requests
adapter — so requests still builds its real PreparedRequest (params merged into
the URL, session headers into the request's). Nothing here can reach the
network: the autouse fixture fails any connection attempt.
"""
import ast
import datetime as dt
import importlib
import json
import os
import socket
import sys
import urllib.request
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KEY = "sentinel-provider-key-not-a-credential-7301"

# Query parameter names a credential rides under, lower-cased.
_CREDENTIAL_PARAMS = {"key", "api_key", "apikey", "api-key", "access_token",
                      "token", "auth", "secret", "password"}

_EIA_BODY = json.dumps({"response": {"total": 1, "data": [
    {"period": "2026-09-12T04", "respondent": "PJM", "fueltype": "NG",
     "value": 41234, "value-units": "megawatthours"}]}}).encode()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a test in this file tried to open a real connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _real(module_name):
    """Import the module and prove it is the file on disk, not a stub another
    test left in sys.modules."""
    mod = importlib.import_module(module_name)
    expected = os.path.join(ROOT, *module_name.split(".")) + ".py"
    path = getattr(mod, "__file__", None)
    assert path and os.path.samefile(path, expected), (module_name, path)
    return mod


def _assert_keyless(url, secret=KEY):
    assert secret not in url, url
    names = {k.lower() for k, _v in parse_qsl(urlsplit(url).query,
                                              keep_blank_values=True)}
    assert not names & _CREDENTIAL_PARAMS, url


def _sent_url(req):
    return req.full_url if hasattr(req, "full_url") else req.url


def _sent_headers(req):
    """Header name (lower-cased) -> value, for a urllib Request or a requests
    PreparedRequest."""
    items = req.header_items() if hasattr(req, "header_items") else req.headers.items()
    return {k.lower(): v for k, v in items}


class _UrlopenResponse:
    """What these call sites use of urlopen's response — status, read() and
    the context-manager protocol — and nothing the real one lacks."""
    status = 200

    def __init__(self, body):
        self._body = body

    def read(self, *_amt):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _record_urlopen(monkeypatch, body, owner=urllib.request):
    sent = []

    def urlopen(req, data=None, timeout=None, **_kw):
        sent.append(req)
        return _UrlopenResponse(body)

    monkeypatch.setattr(owner, "urlopen", urlopen)
    return sent


class _RecordingAdapter(requests.adapters.BaseAdapter):
    """A requests transport that keeps the PreparedRequest instead of sending
    it. By send() time requests has merged params into .url and the session's
    headers into .headers, so this is the request as it would go out."""

    def __init__(self, body):
        super().__init__()
        self.body = body
        self.sent = []

    def send(self, request, **_kwargs):
        self.sent.append(request)
        resp = requests.models.Response()
        resp.status_code = 200
        resp._content = self.body
        resp.encoding = "utf-8"
        resp.headers["Content-Type"] = "application/json"
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


# ── EIA: X-Api-Key ───────────────────────────────────────────────────────────

def test_the_eia930_adapter_sends_the_key_in_x_api_key(monkeypatch):
    eia930 = _real("routes.eia930")
    monkeypatch.setenv("EIA_API_KEY", KEY)
    sent = _record_urlopen(monkeypatch, _EIA_BODY)
    res = eia930.fetch_eia930_ba("PJM", max_age_s=0)
    assert res["status"] == "ok", res
    assert len(sent) == 1, sent
    assert urlsplit(_sent_url(sent[0])).netloc == "api.eia.gov"
    _assert_keyless(_sent_url(sent[0]))
    assert _sent_headers(sent[0]).get("x-api-key") == KEY
    assert KEY not in json.dumps(res, default=str)


_EIA930_URL_LISTS = (
    ("routes.iso_tva", "_tva_urls", ()),
    ("routes.iso_pjm", "_pjm_urls", ()),
    ("routes.iso_bpa", "_bpa_urls", ()),
    ("routes.iso_miso", "_miso_urls", ()),
    ("routes.eia_utility_bas", "_eia_urls", ("AZPS",)),
)


@pytest.mark.parametrize("module_name,fn,args", _EIA930_URL_LISTS,
                         ids=[m.rsplit(".", 1)[1] for m, _f, _a in _EIA930_URL_LISTS])
def test_each_eia930_entry_in_an_iso_url_list_carries_the_key_in_a_header(
        monkeypatch, module_name, fn, args):
    mod = _real(module_name)
    iso_common = _real("routes._iso_common")
    monkeypatch.setenv("EIA_API_KEY", KEY)
    entries = getattr(mod, fn)(*args)
    eia = [e for e in entries
           if urlsplit(e[0] if isinstance(e, tuple) else e).netloc == "api.eia.gov"]
    assert eia, "no api.eia.gov entry left in %s.%s" % (module_name, fn)
    for entry in eia:
        assert isinstance(entry, tuple), (
            "a bare URL goes out with no key at all: %r" % (entry,))
        url, headers = entry
        _assert_keyless(url)
        assert {k.lower(): v for k, v in headers.items()}.get("x-api-key") == KEY
    sent = _record_urlopen(monkeypatch, _EIA_BODY)
    _text, used = iso_common.fetch_first_working(eia, total_budget=5)
    assert len(sent) == 1 and used == eia[0][0]
    _assert_keyless(_sent_url(sent[0]))
    assert _sent_headers(sent[0]).get("x-api-key") == KEY


def test_eia_catalog_discovery_sends_the_key_in_x_api_key(monkeypatch):
    mod = _real("api_auto_discovery")
    monkeypatch.setenv("EIA_API_KEY", KEY)
    monkeypatch.setattr(mod.APIAutoDiscovery, "init_tables", lambda self: None)
    monkeypatch.setattr(mod.APIAutoDiscovery, "_add_discovered_api",
                        lambda self, *a, **k: False)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    discovery = mod.APIAutoDiscovery()
    rec = _RecordingAdapter(json.dumps({"response": {
        "total": 1, "data": [{"period": "2026-09"}],
        "routes": [{"id": "new-route", "name": "New", "description": "d"}]},
    }).encode())
    discovery.session.mount("https://", rec)
    discovery.session.mount("http://", rec)
    discovery.discover_eia_catalog()
    # one probe per catalog route, then the catalog root itself
    assert len(rec.sent) == len(mod.EIA_ROUTE_CATALOG) + 1, [r.url for r in rec.sent]
    for req in rec.sent:
        assert urlsplit(req.url).netloc == "api.eia.gov", req.url
        _assert_keyless(req.url)
        assert _sent_headers(req).get("x-api-key") == KEY, req.url


def test_the_henry_hub_adapter_sends_the_key_in_x_api_key(monkeypatch):
    g = _real("routes.grid_data_master_shell")
    monkeypatch.setenv("EIA_API_KEY", KEY)
    monkeypatch.setattr(g, "_utcnow",
                        lambda: dt.datetime(2026, 9, 3, 12, tzinfo=dt.timezone.utc))
    sent = _record_urlopen(monkeypatch, json.dumps({"response": {"data": [
        {"period": "2026-09-01", "series": "RNGWHHD", "value": "2.9"}]}}).encode())
    entry = next(t for t in g.TARGET_DATASETS
                 if t["id"] == "eia_henry_hub_natural_gas_spot_prices_daily")
    out = g._eia_henry_hub(entry)
    assert out["ok"] is True, out
    assert len(sent) == 1
    assert _sent_url(sent[0]) == g._EIA_HH_URL
    _assert_keyless(_sent_url(sent[0]))
    assert _sent_headers(sent[0]).get("x-api-key") == KEY
    assert KEY not in json.dumps(out, default=str)


# ── Google AI Studio: x-goog-api-key ─────────────────────────────────────────

_AI_REPLY = "a stub reply, long enough to count as a real response"
_AI_BODY = json.dumps({
    "content": [{"type": "text", "text": _AI_REPLY}],               # anthropic
    "choices": [{"message": {"content": _AI_REPLY}}],                # openai shape
    "candidates": [{"content": {"parts": [{"text": _AI_REPLY}]}}],   # google
    "message": {"content": [{"type": "text", "text": _AI_REPLY}]},   # cohere
}).encode()


def _record_requests(monkeypatch, body=_AI_BODY):
    """Swap the transport every requests.Session mounts by default."""
    rec = _RecordingAdapter(body)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda self, request, **kw: rec.send(request, **kw))
    return rec


def test_the_gemini_lane_sends_x_goog_api_key(monkeypatch):
    mod = _real("ai_wars_battle_runner")
    rec = _record_requests(monkeypatch)
    cfg = mod.PLATFORM_CONFIGS["gemini"]
    monkeypatch.setenv(cfg["env_key"], KEY)
    out = mod.call_platform_api("gemini", "which markets are adding capacity?")
    assert out["error"] is None and out["had_real_response"], out
    assert len(rec.sent) == 1
    sent = urlsplit(rec.sent[0].url)
    assert sent.netloc == urlsplit(cfg["url"]).netloc
    assert sent.path.endswith(":generateContent") and sent.query == "", rec.sent[0].url
    assert _sent_headers(rec.sent[0]).get("x-goog-api-key") == KEY


def test_no_ai_wars_platform_sends_its_key_in_the_url(monkeypatch):
    mod = _real("ai_wars_battle_runner")
    rec = _record_requests(monkeypatch)
    assert mod.PLATFORM_CONFIGS, "the platform table read empty"
    for name, cfg in sorted(mod.PLATFORM_CONFIGS.items()):
        secret = KEY + "-" + cfg["env_key"].lower()
        monkeypatch.setenv(cfg["env_key"], secret)
        before = len(rec.sent)
        out = mod.call_platform_api(name, "which markets are adding capacity?")
        assert out["error"] is None, (name, out)
        assert len(rec.sent) == before + 1, name
        req = rec.sent[-1]
        assert urlsplit(req.url).netloc == urlsplit(cfg["url"]).netloc, name
        _assert_keyless(req.url, secret)
        assert any(secret in value for value in _sent_headers(req).values()), (
            name, "the key is in no header")


# ── call sites that wrote the key into a params dict after building it ──────
def test_make_eia_request_sends_the_key_in_x_api_key(monkeypatch):
    mod = _real("eia_api")
    monkeypatch.setattr(mod, "EIA_API_KEY", KEY)
    rec = _record_requests(monkeypatch, _EIA_BODY)
    caller = {"frequency": "monthly", "data[0]": "price"}
    data, err = mod.make_eia_request("electricity/retail-sales/data", caller)
    assert err is None and data["response"]["data"], (data, err)
    assert len(rec.sent) == 1
    req = rec.sent[0]
    assert urlsplit(req.url).netloc == "api.eia.gov", req.url
    _assert_keyless(req.url)
    assert _sent_headers(req).get("x-api-key") == KEY
    assert caller == {"frequency": "monthly", "data[0]": "price"}, "the caller's dict was written into"


def _from_main(name, **given):
    """One top-level function compiled out of main.py, with the main.py helpers
    it calls compiled beside it. A name in `given` is used instead."""
    path = os.path.join(ROOT, "main.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    ns = {"__name__": "main_extract", "os": os, **given}
    todo = [name]
    while todo:
        fn = defs[todo.pop()]
        if fn.name in ns:
            continue
        exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
        todo += [n.id for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and n.id in defs and n.id not in ns]
    return ns[name]


def test_grid_intel_sends_the_eia_key_in_x_api_key_on_both_rto_calls(monkeypatch):
    fetch = _from_main("_grid_intel_fetch", _grid_ext_metrics_for=lambda _rto: {})
    rec = _record_requests(monkeypatch, _EIA_BODY)
    monkeypatch.setenv("EIA_API_KEY", KEY)
    fetch("pjm", "PJM")
    eia = [r for r in rec.sent if urlsplit(r.url).netloc == "api.eia.gov"]
    assert sorted(urlsplit(r.url).path for r in eia) == [
        "/v2/electricity/rto/fuel-type-data/data",
        "/v2/electricity/rto/region-data/data"], [r.url for r in rec.sent]
    for r in eia:
        _assert_keyless(r.url)
        assert _sent_headers(r).get("x-api-key") == KEY, r.url
    for r in rec.sent:
        if r not in eia:
            assert KEY not in r.url and KEY not in _sent_headers(r).values(), r.url


def test_the_nepa_scraper_sends_the_key_in_x_api_key(monkeypatch):
    mod = _real("services.nepa_scraper")
    monkeypatch.setenv("NEPA_API_KEY", KEY)
    sent = _record_urlopen(monkeypatch, b'{"data": []}')
    assert mod.search_documents("data center campus") == {"data": []}
    assert len(sent) == 1
    url = _sent_url(sent[0])
    assert urlsplit(url).netloc == "api.regulations.gov", url
    _assert_keyless(url)
    assert _sent_headers(sent[0]).get("x-api-key") == KEY
