"""/api/admin/probe-network must read the body, not only the status line.

Measured 2026-09-13: services1.arcgis.com/Hp6G80Pky0om7QvQ's
Electric_Substations and Power_Plants services no longer exist. The two URLs
this probe checks answer HTTP 200 with
{"error": {"code": 400, "message": "Invalid URL"}}, and the probe published
ok:true for both, because urlopen returned 200 and nothing read what came
back. Its sibling /api/admin/probe-hifld-deep already read has_error_key.

House rule: tests never import main. The handler is compiled out of main.py's
AST with its decorators stripped, then called inside a Flask request context
with urlopen replaced. What is asserted is the JSON the route serves.
"""
import ast
import copy
import functools
import io
import os
import urllib.error
import urllib.request

from flask import Flask, jsonify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HANDLER = "phase12i_probe_network"
ARCGIS_ERROR = b'{"error":{"code":400,"message":"Invalid URL","details":["Invalid URL"]}}'


@functools.lru_cache(maxsize=1)
def _handler_def():
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == HANDLER]
    assert len(fns) == 1, f"expected one {HANDLER} in main.py, found {len(fns)}"
    fn = copy.deepcopy(fns[0])
    decorators = [ast.unparse(d) for d in fn.decorator_list]
    fn.decorator_list = []
    return fn, tuple(decorators)


class _Response:
    """The parts of http.client.HTTPResponse the handler uses."""

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, amt=None):
        return self._body if amt is None or amt < 0 else self._body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answers(**overrides):
    answers = {
        "Electric_Substations": (200, ARCGIS_ERROR),
        "Power_Plants": (200, ARCGIS_ERROR),
        "hifld-geoplatform": (200, b"<!DOCTYPE html><html><head><title>ArcGIS Hub</title>"),
        "api.eia.gov": urllib.error.HTTPError(
            "https://api.eia.gov/v2/", 403, "Forbidden", {}, io.BytesIO(b"")),
        "peeringdb": (200, b'{"data": [{"id": 1}], "meta": {}}'),
        "1.1.1.1": (200, b"<!DOCTYPE html><html><title>1.1.1.1</title></html>"),
    }
    answers.update(overrides)
    return answers


def _probe(monkeypatch, answers):
    calls = []

    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", req)
        calls.append(url)
        for needle, answer in answers.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                return _Response(*answer)
        raise AssertionError(f"probe target with no scripted answer: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fn, _ = _handler_def()
    ns = {"jsonify": jsonify, "utc_iso_z": lambda: "2026-09-13T00:00:00Z"}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    with Flask(__name__).test_request_context("/api/admin/probe-network"):
        body = ns[HANDLER]().get_json()
    assert len(calls) == 6, f"expected the six probe targets to be called, got {calls}"
    return body["targets"]


def test_the_compiled_function_is_the_routed_probe():
    """Non-vacuity: the function under test is the one main.py serves."""
    _, decorators = _handler_def()
    assert any("/api/admin/probe-network" in d for d in decorators), decorators


def test_an_arcgis_error_body_on_http_200_is_not_ok(monkeypatch):
    targets = _probe(monkeypatch, _answers())
    for name in ("hifld_substations_arcgis", "hifld_power_plants_arcgis"):
        rec = targets[name]
        assert rec["status"] == 200, rec
        assert rec["ok"] is False, (
            f"{name}: an HTTP 200 whose body is an ArcGIS error was published "
            f"as ok — the probe did not read the body: {rec}")
        assert "Invalid URL" in rec["error"], rec


def test_an_error_key_holding_a_string_is_not_ok(monkeypatch):
    targets = _probe(monkeypatch, _answers(
        peeringdb=(200, b'{"error": "rate limited"}')))
    assert targets["peeringdb"]["ok"] is False, targets["peeringdb"]
    assert "rate limited" in targets["peeringdb"]["error"], targets["peeringdb"]


def test_bodies_that_answer_stay_ok(monkeypatch):
    targets = _probe(monkeypatch, _answers())
    for name in ("hifld_opendata", "peeringdb", "cloudflare"):
        rec = targets[name]
        assert rec["ok"] is True, f"{name} answered and was marked failed: {rec}"
        assert "error" not in rec, rec


def test_a_json_body_that_is_not_an_object_is_an_answer(monkeypatch):
    targets = _probe(monkeypatch, _answers(peeringdb=(200, b'[{"id": 1}]')))
    assert targets["peeringdb"]["ok"] is True, targets["peeringdb"]


def test_an_http_error_is_still_not_ok(monkeypatch):
    targets = _probe(monkeypatch, _answers())
    rec = targets["eia_v2"]
    assert rec["ok"] is False and rec["status"] == 403, rec
    assert "HTTPError 403" in rec["error"], rec


def test_a_transport_failure_is_still_not_ok(monkeypatch):
    targets = _probe(monkeypatch, _answers(
        **{"1.1.1.1": urllib.error.URLError("timed out")}))
    rec = targets["cloudflare"]
    assert rec["ok"] is False and "URLError" in rec["error"], rec
