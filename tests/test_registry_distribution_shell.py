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
    # ★2026-08-12: was a hardcoded 5. That is a second copy of a number _LANES
    # already owns, and it failed the moment a sixth lane landed — the same
    # duplicated-constant shape that produced a false DCPI green-on-stale alarm
    # the same day. The GUARANTEE this test exists for is the loop below (an
    # undecidable lane must render "?", never a pass); the count is incidental,
    # so derive it.
    assert t["lanes_total"] == len(m._LANES)
    for lane in t["lanes"]:
        decided = [c for c in lane["checks"] if c["pass"] is not None]
        if not decided:
            assert lane["verdict"] == "?", lane["id"]


# ═══════════════════════════════════════════════════════════════════════════
# LANE A (discovery) and LANE B (staleness) — added 2026-08-13.
#
# These two lanes are the ones that can lie in a NEW way, so they get mutation
# tests rather than assertions. The failure they must never commit is the one
# the owner hit by hand: a registry that could not be READ being reported as a
# registry we are ABSENT from, which manufactures a work order for a listing
# that may already exist. Every case below is monkeypatched — CI must grade our
# semantics, never mcphive.com's uptime.
# ═══════════════════════════════════════════════════════════════════════════

_TOKENS = ["owner%d/server%d" % (i, i) for i in range(20)]
_DIRECTORY = ("mcp " * 40 + " ".join(_TOKENS) + " x" * 2000)
_LISTED = _DIRECTORY + " dchub.cloud"
_GATEWAY = "mcp " * 40 + "x" * 3000


def _text(monkeypatch, m, body, reason=None):
    monkeypatch.setattr(m, "_get_text",
                        lambda *_a, **_k: (body, reason))


def test_absent_requires_a_directory_that_actually_answered(monkeypatch):
    m = _mod()
    _text(monkeypatch, m, _DIRECTORY)
    assert m._probe_index("https://x.test/", _TOKENS)[0] == "ABSENT"


def test_our_name_in_the_corpus_reads_listed(monkeypatch):
    m = _mod()
    _text(monkeypatch, m, _LISTED)
    assert m._probe_index("https://x.test/", _TOKENS)[0] == "LISTED"


@pytest.mark.parametrize("reason", ["HTTP 403", "HTTP 404", "HTTP 500",
                                    "ConnectionError"])
def test_a_failed_fetch_is_never_absence(monkeypatch, reason):
    """★ THE TRAP, in one test. PulseMCP 403s a bot and mcp.so answers a
    guessed slug with an error. Both are UNREADABLE. Reading either as 'not
    listed' invents work that may already be done."""
    m = _mod()
    _text(monkeypatch, m, None, reason)
    state, detail, _u = m._probe_index("https://x.test/", _TOKENS)
    assert state == "UNREADABLE", state
    assert "NOT concluded" in detail


def test_a_gateway_is_not_a_directory_and_yields_no_work_order(monkeypatch):
    """Submitting DC Hub to a CLI is not an action anyone can take. The first
    run of this lane emitted twelve such work orders."""
    m = _mod()
    _text(monkeypatch, m, _GATEWAY)
    assert m._probe_index("https://x.test/", _TOKENS)[0] == "NOT_A_DIRECTORY"


def test_without_a_token_set_absence_is_not_concluded(monkeypatch):
    """If we cannot tell a directory from a gateway, we may not claim absence."""
    m = _mod()
    _text(monkeypatch, m, _DIRECTORY)
    assert m._probe_index("https://x.test/", [])[0] == "UNREADABLE"


def _staleness(monkeypatch, m, canon, glama, wk=None, official=None):
    jm = {"canon/phrases": canon,
          ".well-known/mcp.json": wk or {"tools": [1] * 82, "description": ""},
          "glama.ai": glama,
          "registry.modelcontextprotocol.io": official or {"servers": []}}

    def _fake(url, headers=None):
        for k, v in jm.items():
            if k in url:
                return (v, None)
        return (None, "HTTP 404")

    monkeypatch.setattr(m, "_get_json", _fake)
    monkeypatch.setattr(m, "_get_text", lambda *_a, **_k: (None, "HTTP 404"))
    monkeypatch.setattr(m, "_clock_touch", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_clock_clear", lambda *_a, **_k: None)
    return {c["id"]: c for c in m._lane_listing_staleness()}


_CANON = {"tools": 82, "facilities": "17,600+", "deals": "1,800+",
          "markets": "300+"}


def test_glama_empty_tools_and_stale_numbers_must_fail(monkeypatch):
    """If this lane does not fail on the live Glama listing, it is not working —
    tools: [] against an 82-tool canon is the whole reason it exists."""
    m = _mod()
    res = _staleness(monkeypatch, m, _CANON,
                     {"tools": [],
                      "description": "33 tools covering 21,000+ facilities"})
    assert res["glama_staleness"]["pass"] is False
    d = res["glama_staleness"]["detail"]
    assert "EMPTY" in d
    # OURS vs THEIRS is not decoration: it decides whether the work order is a
    # support ticket or a code change.
    assert "THEIRS" in d


def test_staleness_reads_live_canon_not_a_baked_in_number(monkeypatch):
    """★ MUTATION. Move canon to whatever Glama publishes. A lane comparing
    against a literal 82 keeps failing; one that reads canon stops. This is the
    only way to tell those two implementations apart from outside."""
    m = _mod()
    moved = {"tools": 33, "facilities": "21,000+", "deals": "1,800+",
             "markets": "232+"}
    res = _staleness(
        monkeypatch, m, moved,
        {"tools": [{"name": "t%d" % i} for i in range(33)],
         "description": "33 tools covering 21,000+ facilities, 232 markets"},
        wk={"tools": [1] * 33,
            "description": "33 tools over 21,000+ facilities, 1,800+ deals"})
    assert res["glama_staleness"]["pass"] is True, \
        res["glama_staleness"]["detail"]


def test_unreadable_canon_renders_unmeasured_not_pass(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_get_json", lambda *_a, **_k: (None, "HTTP 500"))
    monkeypatch.setattr(m, "_get_text", lambda *_a, **_k: (None, "HTTP 500"))
    out = m._lane_listing_staleness()
    assert all(c["pass"] is None for c in out), out
    assert m._lane_verdict(out) == "?"


def test_unreadable_glama_is_neither_pass_nor_fail(monkeypatch):
    m = _mod()
    jm = {"canon/phrases": _CANON,
          ".well-known/mcp.json": {"tools": [1] * 82, "description": ""},
          "registry.modelcontextprotocol.io": {"servers": []}}

    def _fake(url, headers=None):
        for k, v in jm.items():
            if k in url:
                return (v, None)
        return (None, "HTTP 502")

    monkeypatch.setattr(m, "_get_json", _fake)
    monkeypatch.setattr(m, "_get_text", lambda *_a, **_k: (None, "HTTP 404"))
    monkeypatch.setattr(m, "_clock_touch", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_clock_clear", lambda *_a, **_k: None)
    res = {c["id"]: c for c in m._lane_listing_staleness()}
    assert res["glama_staleness"]["pass"] is None


def test_official_registry_islatest_is_read_at_the_real_path(monkeypatch):
    """★ REGRESSION. isLatest lives at
    _meta['io.modelcontextprotocol.registry/official']['isLatest'], not at
    _meta['isLatest']. Reading the shallow path silently selects servers[0] —
    the OLDEST version, which carries no toolCount — and renders a permanent
    '?' on our healthiest listing."""
    m = _mod()
    official = {"servers": [
        {"_meta": {"io.modelcontextprotocol.registry/official":
                   {"isLatest": False}},
         "server": {"version": "1.0.0", "description": "old"}},
        {"_meta": {"io.modelcontextprotocol.registry/official":
                   {"isLatest": True}},
         "server": {"version": "2.12.0", "description": "current",
                    "_meta": {"ns": {"toolCount": 82}}}}]}
    res = _staleness(monkeypatch, m, _CANON,
                     {"tools": [1] * 82, "description": ""},
                     official=official)
    c = res["official_staleness"]
    assert c["pass"] is True, c["detail"]
    assert "2.12.0" in c["detail"], c["detail"]


def test_discovery_on_a_dead_network_is_unmeasured_not_red(monkeypatch):
    """★ A flattering zero is a bug, and so is a punishing one. Zero candidates
    because nothing answered is UNMEASURED; rendering it RED reports an outage
    as a distribution defect."""
    m = _mod()
    monkeypatch.setattr(m, "_get_json", lambda *_a, **_k: (None, "down"))
    monkeypatch.setattr(m, "_get_text", lambda *_a, **_k: (None, "down"))
    monkeypatch.setattr(m, "_draft_meta", lambda: {})
    out = m._lane_discovery_absent()
    assert m._lane_verdict(out) == "?", [(c["id"], c["pass"]) for c in out]
    assert not any(c["pass"] is False for c in out)


def test_finder_control_goes_red_when_a_held_listing_reads_absent(monkeypatch):
    """★ MUTATION (a). Strip DC Hub from the one aggregator known to carry us.
    The lane must go RED — otherwise a broken finder reports a clean board."""
    m = _mod()
    monkeypatch.setattr(m, "_draft_meta", lambda: {})
    monkeypatch.setattr(
        m, "_discover_candidates",
        lambda: ([{"name": "punkpeye/awesome-mcp-servers",
                   "probe_url": "https://raw.githubusercontent.com/punkpeye/"
                                "awesome-mcp-servers/main/README.md",
                   "origin": "test", "submit_url": None, "evidence": "awesome"}],
                 ["test source"], 1))
    monkeypatch.setattr(m, "_third_party_tokens", lambda: _TOKENS)

    _text(monkeypatch, m, _LISTED)
    ok_res = {c["id"]: c for c in m._lane_discovery_absent()}
    assert ok_res["finder_control"]["pass"] is True

    # THE MUTATION — same corpus, our name removed.
    _text(monkeypatch, m, _DIRECTORY)
    mut = {c["id"]: c for c in m._lane_discovery_absent()}
    assert mut["finder_control"]["pass"] is False, mut["finder_control"]
    assert m._lane_verdict(list(mut.values())) == "FAIL"
