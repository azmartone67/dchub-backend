"""Registry Distribution Master Shell — UNREADABLE-IS-NOT-DRIFT contract.

The shell's whole claim is that a check it could not read renders '?' — never
a pass, never a failure. These tests exist because the migration off
urllib.request (banned by scripts/regression_lint.py as
`urllib-request-on-railway`) is exactly the kind of change that can break that
claim silently: urlopen RAISED on 4xx/5xx, `requests` does not. Without an
explicit status check, api.github.com's 404 body — well-formed JSON,
{"message":"Not Found"} — parses cleanly and the catalog lane reads it as zero
catalogued servers, rendering RED on a registry we simply could not read.

Source-level and monkeypatched only: these must not reach the network, or CI
would grade GitHub's uptime instead of our semantics.
"""
import ast
import json
from pathlib import Path

import pytest

MOD = "routes.registry_distribution_master_shell"
SRC_PATH = (Path(__file__).resolve().parents[1] / "routes"
            / "registry_distribution_master_shell.py")
SRC = SRC_PATH.read_text()


def _mod():
    return pytest.importorskip(MOD)


class _Resp:
    """Minimal stand-in for requests.Response — status, bytes, no network."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.content = body.encode("utf-8")
        self.text = body


def _patch_get(monkeypatch, resp_or_exc):
    """Point requests.get at a canned response (or make it raise)."""
    requests = pytest.importorskip("requests")

    def _fake(*_a, **_k):
        if isinstance(resp_or_exc, BaseException):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr(requests, "get", _fake)


def test_source_parses_and_is_substantial():
    """Guards every assertion below from passing vacuously on an empty read —
    an empty parse satisfies every 'X not in source' assertion trivially."""
    tree = ast.parse(SRC)
    assert len(SRC) > 4000, "source suspiciously short — assertions would be vacuous"
    fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for want in ("_get_json", "_lane_catalog_presence", "_lane_capability_visible",
                 "_lane_listing_accuracy", "_lane_no_duplicate_listings",
                 "_lane_ledger_integrity", "_tick"):
        assert want in fns, f"{want} missing — file is not the shell"


def test_no_urllib_urlopen_call_anywhere():
    """Detected the way scripts/regression_lint.py detects it: over the AST, not
    over the text. A substring test would trip on the docstring that EXPLAINS
    the ban and would miss `from urllib.request import urlopen`."""
    banned = []
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            if (f.attr == "urlopen"
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "request"
                    and isinstance(f.value.value, ast.Name)
                    and f.value.value.id == "urllib"):
                banned.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
            banned.extend(n.lineno for n in node.names if n.name == "urlopen")
    assert not banned, f"urllib.request.urlopen at line(s) {banned} — use requests"


def test_no_network_at_import_time():
    """The shell may only fetch inside lane functions. A module-level call into
    the fetch helper would make importing routes/ hit four registries."""
    tree = ast.parse(SRC)
    called = set()
    for node in tree.body:
        # Skip def/class bodies: ast.walk descends into them, and a call made
        # INSIDE a lane function is the whole point — it is only a module-level
        # call that fires on import.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                called.add(sub.func.id)
    assert "_get_json" not in called, "fetch at import time"
    assert "_canon" not in called, "canon fetch at import time"


# ── the invariant ────────────────────────────────────────────────────────────
def test_non_2xx_with_parseable_json_body_is_unreadable_not_data(monkeypatch):
    """★ The regression the requests migration invites. GitHub answers 404 with
    valid JSON; if status is not checked first, that body becomes a payload."""
    m = _mod()
    _patch_get(monkeypatch, _Resp(404, json.dumps({"message": "Not Found"})))
    payload, reason = m._get_json("https://api.github.invalid/x")
    assert payload is None, "a 404 body was returned as data"
    assert reason == "HTTP 404", reason


@pytest.mark.parametrize("status", [301, 400, 403, 404, 429, 500, 503])
def test_every_non_2xx_status_is_unreadable(monkeypatch, status):
    m = _mod()
    _patch_get(monkeypatch, _Resp(status, '{"anything": true}'))
    payload, reason = m._get_json("https://x.invalid/y")
    assert payload is None and reason == f"HTTP {status}"


def test_transport_failure_is_unreadable(monkeypatch):
    m = _mod()
    _patch_get(monkeypatch, OSError("connection reset"))
    payload, reason = m._get_json("https://x.invalid/y")
    assert payload is None and reason == "OSError"


def test_unparseable_body_is_unreadable(monkeypatch):
    m = _mod()
    _patch_get(monkeypatch, _Resp(200, "<html>not json</html>"))
    payload, reason = m._get_json("https://x.invalid/y")
    assert payload is None and reason, "HTML body must not read as a payload"


def test_2xx_json_still_parses(monkeypatch):
    """Must-not-over-reject control: the happy path has to survive the change,
    or every lane would render '?' forever and the board would look calm."""
    m = _mod()
    _patch_get(monkeypatch, _Resp(200, '{"tree": [{"path": "servers/foo/x"}]}'))
    payload, reason = m._get_json("https://x.invalid/y")
    assert reason is None and payload == {"tree": [{"path": "servers/foo/x"}]}


def test_utf8_body_is_not_mangled(monkeypatch):
    """requests guesses ISO-8859-1 for text/* with no charset; registry copy is
    UTF-8. Decoding .content keeps urlopen's behaviour."""
    m = _mod()
    _patch_get(monkeypatch, _Resp(200, '{"name": "ドコモ"}'))
    payload, _ = m._get_json("https://x.invalid/y")
    assert payload == {"name": "ドコモ"}


# ── the invariant, observed through the lanes ────────────────────────────────
def test_catalog_lane_renders_none_when_registry_404s(monkeypatch):
    """The end the reader actually sees: an unreadable Docker catalog must show
    '?', not the RED 'ABSENT from 0 catalogued servers'."""
    m = _mod()
    _patch_get(monkeypatch, _Resp(404, json.dumps({"message": "Not Found"})))
    checks = m._lane_catalog_presence()
    docker = [c for c in checks if c["id"] == "docker_catalog"]
    assert docker, "docker_catalog check disappeared"
    assert docker[0]["pass"] is None, (
        "unreadable registry rendered as %r — UNREADABLE IS NOT DRIFT"
        % (docker[0]["pass"],))
    assert m._lane_verdict(checks) != "FAIL", "unreadable lane rendered FAIL"


def test_capability_lane_renders_none_when_glama_5xxs(monkeypatch):
    """An unreadable Glama must not be scored as the empty-tools failure."""
    m = _mod()
    _patch_get(monkeypatch, _Resp(503, '{"error": "upstream"}'))
    checks = m._lane_capability_visible()
    glama = [c for c in checks if c["id"] == "glama_tools"]
    assert glama and glama[0]["pass"] is None, "5xx scored as an empty tool list"


def test_accuracy_lane_is_unmeasured_when_canon_unreadable(monkeypatch):
    """Canon is fetched, never remembered — losing it must degrade to '?',
    never to a comparison against a stale baked-in number."""
    m = _mod()
    _patch_get(monkeypatch, _Resp(500, "{}"))
    checks = m._lane_listing_accuracy()
    assert checks and all(c["pass"] is None for c in checks)
    assert m._lane_verdict(checks) == "?"


def test_no_lane_raises_when_every_fetch_fails(monkeypatch):
    """A board that raises is a board nobody reads."""
    m = _mod()
    _patch_get(monkeypatch, OSError("down"))
    for _lid, _name, fn in m._LANES:
        out = fn()
        assert isinstance(out, list) and out, f"{fn.__name__} returned nothing"
        assert all("pass" in c for c in out), fn.__name__


def test_tick_never_counts_an_unreadable_check_as_a_pass(monkeypatch):
    """lanes_pass is the number the reader trusts at a glance."""
    m = _mod()
    _patch_get(monkeypatch, OSError("down"))
    t = m._tick()
    assert t["lanes_total"] == 5
    for lane in t["lanes"]:
        decided = [c for c in lane["checks"] if c["pass"] is not None]
        if not decided:
            assert lane["verdict"] == "?", lane["id"]
