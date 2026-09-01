"""The alarm for the outage nobody was watching: an AI Gateway spend rule that
refuses every Claude call.

WHY THIS EXISTS
---------------
2026-09-01: a Cloudflare AI Gateway spend rule ($100 per 604800s, sliding) went
over and the gateway returned 429 to EVERY Anthropic request — every model,
including a 1-token haiku probe — before the request reached Anthropic. The
brain stopped drafting PRs, writing narratives and running layers, and nothing
paged. It surfaced when a human clicked approve and read "PR draft skipped:
claude call failed: http_429".

Cloudflare cannot send this alert: its notification catalog has no AI Gateway
type, and under BYOK the spend is billed by Anthropic, so its usage-billing
alerts never see the charge. So the probe lives here.

WHAT THIS TEST PROVES
---------------------
  1. a gateway spend block is classified "blocked" off the real 429 body;
  2. ★ an ANTHROPIC 429 (a genuine rate limit) is NOT "blocked" — paging on
     that would train the owner to ignore the alarm;
  3. 401/403 is "open" — the healthy answer, since the probe key is invalid on
     purpose and Anthropic is supposed to reject it;
  4. ★ the probe NEVER sends the real ANTHROPIC_API_KEY — it costs nothing and
     puts no live credential on the wire;
  5. no gateway configured means "unknown" and NO network call at all;
  6. "unknown" never clears a live block (that would disarm the alarm
     mid-outage) and never raises one;
  7. the block pages once, then nags at most once per _GW_RENOTIFY_S, and a
     recovery re-arms it.

Run:  python3 -m pytest tests/test_gateway_spend_alarm.py -v
"""

import ast
import email.message
import io
import pathlib
import sys
import types
import urllib.error
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SRC = _ROOT / "routes" / "health_alerter.py"

_REAL_KEY = "sk-ant-THE-REAL-PRODUCTION-KEY-must-never-be-sent"

_GATEWAY_429 = (
    '{"success":false,"error":[{"code":2041,"message":"Spend limit exceeded: '
    "rule '5e7f1b6b' (cost limit 100 per 604800s, sliding) for anthropic "
    'anthropic/claude-haiku-4.5"}],"name":"AiGatewayError","httpCode":429}'
)
_ANTHROPIC_429 = (
    '{"type":"error","error":{"type":"rate_limit_error",'
    '"message":"Number of requests has exceeded your per-minute rate limit"}}'
)
_ANTHROPIC_401 = (
    '{"type":"error","error":{"type":"authentication_error",'
    '"message":"invalid x-api-key"}}'
)


def _http_error(code: int, body: str):
    return urllib.error.HTTPError(
        "https://gateway.ai.cloudflare.com/v1/acct/dchub/anthropic/v1/messages",
        code, "err", email.message.Message(), io.BytesIO(body.encode()))


@pytest.fixture
def mod(monkeypatch):
    """The SHIPPED probe + decision functions, sliced out with `ast` and run
    against fakes. Records every request the probe actually sends."""
    src = _SRC.read_text()
    tree = ast.parse(src)
    ns = {
        "os": __import__("os"), "time": __import__("time"),
        "json": __import__("json"), "log": types.SimpleNamespace(
            warning=lambda *a, **k: None, info=lambda *a, **k: None),
    }
    # module-level constants the functions close over
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.startswith(("_GW_", "_gw_"))
                for t in node.targets):
            exec(compile(ast.Module([node], []), str(_SRC), "exec"), ns)  # noqa: S102
    for name in ("_gateway_spend_state", "_gateway_alert_decision"):
        seg = next(ast.get_source_segment(src, n) for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(seg, str(_SRC), "exec"), ns)  # noqa: S102
        assert ns[name].__code__.co_code, f"{name} compiled to empty code"

    sent: list = []
    helper = types.ModuleType("utils.anthropic_helper")
    helper.gateway_active = lambda: True
    helper.anthropic_messages_url = lambda: (
        "https://gateway.ai.cloudflare.com/v1/acct/dchub/anthropic/v1/messages")
    monkeypatch.setitem(sys.modules, "utils.anthropic_helper", helper)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _REAL_KEY)

    def respond(exc_or_resp):
        def _urlopen(req, timeout=None):
            sent.append(req)
            if isinstance(exc_or_resp, Exception):
                raise exc_or_resp
            return exc_or_resp
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    return types.SimpleNamespace(ns=ns, sent=sent, respond=respond, helper=helper,
                                 state=ns["_gateway_spend_state"],
                                 decide=ns["_gateway_alert_decision"])


# ── classification ───────────────────────────────────────────────────────────
def test_gateway_spend_block_is_blocked(mod):
    mod.respond(_http_error(429, _GATEWAY_429))
    state, detail = mod.state()
    assert state == "blocked"
    assert "5e7f1b6b" in detail


def test_an_anthropic_rate_limit_is_not_a_spend_block(mod):
    """A real 429 from Anthropic must not page as a spend block."""
    mod.respond(_http_error(429, _ANTHROPIC_429))
    state, _ = mod.state()
    assert state != "blocked", "a per-minute rate limit is not a budget block"


def test_401_is_open(mod):
    mod.respond(_http_error(401, _ANTHROPIC_401))
    assert mod.state()[0] == "open"


def test_a_200_from_a_bogus_key_is_unknown_not_healthy(mod):
    class _R:
        def read(self):
            return b"{}"
    mod.respond(_R())
    state, detail = mod.state()
    assert state == "unknown" and "no longer valid" in detail


def test_network_failure_is_unknown(mod):
    mod.respond(OSError("dns"))
    assert mod.state()[0] == "unknown"


# ── the safety property ──────────────────────────────────────────────────────
def test_the_probe_never_sends_the_real_api_key(mod):
    mod.respond(_http_error(429, _GATEWAY_429))
    mod.state()
    assert len(mod.sent) == 1
    hdrs = {k.lower(): v for k, v in mod.sent[0].header_items()}
    key = hdrs.get("X-api-key".lower()) or hdrs.get("x-api-key", "")
    assert key, "the probe must send some key header"
    assert _REAL_KEY not in str(hdrs), "the REAL key must never leave the process"
    assert key != _REAL_KEY
    assert "not-a-real-key" in key


def test_no_gateway_configured_makes_no_network_call(mod):
    mod.helper.gateway_active = lambda: False
    mod.respond(_http_error(429, _GATEWAY_429))
    state, detail = mod.state()
    assert state == "unknown" and "no AI gateway" in detail
    assert mod.sent == [], "must not probe when there is no gateway"


# ── transition logic ─────────────────────────────────────────────────────────
def test_first_block_pages_then_stays_quiet(mod):
    a, since, notified = mod.decide("blocked", None, 0.0, 1000.0)
    assert a == "block" and since == 1000.0 and notified == 1000.0
    a2, since2, notified2 = mod.decide("blocked", since, notified, 1600.0)
    assert a2 is None, "must not re-page every probe"
    assert since2 == 1000.0 and notified2 == 1000.0


def test_it_nags_once_a_day_while_still_blocked(mod):
    renotify = mod.ns["_GW_RENOTIFY_S"]
    a, since, notified = mod.decide("blocked", 1000.0, 1000.0, 1000.0 + renotify)
    assert a == "block" and since == 1000.0 and notified == 1000.0 + renotify


def test_recovery_notifies_and_rearms(mod):
    a, since, notified = mod.decide("open", 1000.0, 1000.0, 2000.0)
    assert a == "clear" and since is None and notified == 0.0
    # re-armed: the next block pages again
    assert mod.decide("blocked", since, notified, 3000.0)[0] == "block"


def test_unknown_never_clears_a_live_block(mod):
    """The disarm trap: a probe that cannot classify must leave the block standing."""
    a, since, notified = mod.decide("unknown", 1000.0, 1000.0, 5000.0)
    assert a is None and since == 1000.0 and notified == 1000.0


def test_unknown_never_raises_an_alarm(mod):
    assert mod.decide("unknown", None, 0.0, 5000.0) == (None, None, 0.0)


def test_open_when_never_blocked_is_silent(mod):
    assert mod.decide("open", None, 0.0, 5000.0) == (None, None, 0.0)


# ── one fleet, not every process ─────────────────────────────────────────────
@pytest.mark.parametrize("role,probes", [
    ("web", False),       # dchub-backend — serves HTTP, brain does not run here
    ("worker", True),     # dchub-worker — where the brain actually runs
    ("all", True),        # single-process deployments
    ("", True),           # unset defaults to "all"
])
def test_only_the_background_role_probes(monkeypatch, role, probes):
    """main.py starts health_alerter wherever it is imported, and BOTH services
    import it. A gateway spend block is GLOBAL, so without this gate every
    process would email about the same outage."""
    monkeypatch.setenv("DCHUB_ROLE", role)
    src = _SRC.read_text()
    ns = {"os": __import__("os")}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in ("_GW_ROLE", "_GW_ROLE_PROBES")
                for t in node.targets):
            exec(compile(ast.Module([node], []), str(_SRC), "exec"), ns)  # noqa: S102
    assert "_GW_ROLE_PROBES" in ns, "the role gate vanished from the source"
    assert ns["_GW_ROLE_PROBES"] is probes, f"role={role!r}"
