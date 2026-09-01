"""`_call_claude` must SAY WHY a Claude call failed, and must not walk the
model chain when nothing in the chain can help.

WHY THIS EXISTS
---------------
2026-09-01, on the innovation dashboard's approve button:

    Approved #100416 · PR draft skipped: claude call failed: http_429

`http_429` was the whole story the operator got. The reason was sitting in the
response body the code never read — Cloudflare AI Gateway (ANTHROPIC_BASE_URL
points at gateway `dchub` on BOTH dchub-backend and dchub-worker) rejecting on
its own spend rule, before the request ever reached Anthropic:

    429 {"name":"AiGatewayError","internalCode":2041,
         "message":"Spend limit exceeded: rule '5e7f1b6b' (cost limit 100 per
         604800s, sliding) for anthropic anthropic/claude-fable-5"}

The old handler was `last_err = f"http_{e.code}"` and nothing else, and by
design 429 neither retried nor degraded ("other codes (401/429/5xx) don't
retry"). Two consequences, both fixed here:

  * the operator, and the toast, could not tell a gateway budget block from an
    Anthropic rate limit from a dead key — all three read `http_429`;
  * a genuine PER-MODEL rate limit killed the call outright even though the
    next rung of resolve_chain was fine.

WHAT THIS TEST PROVES
---------------------
The four functions are sliced out of the SHIPPED source with `ast` and executed
(no main.py, no Flask, no network), so a comment cannot satisfy them:

  1. a gateway spend block surfaces its own message AND fails fast — ONE HTTP
     attempt, no sleep, because no model in the chain escapes it;
  2. a plain Anthropic rate limit does the opposite — one Retry-After sleep,
     then degrades down the chain, and reports `rate_limit_error`;
  3. regression control: a faithful re-impl of the OLD handler reports bare
     `http_429` for the same response, proving the scenario reproduces and this
     test can tell fixed from broken;
  4. the success / truncation paths still behave (the fix restructured the
     try-block into an inner retry loop).

MUTATION EVIDENCE (2026-09-01) — each mutation was confirmed APPLIED, then run:

  * `last_err = f"http_{e.code}"` (the pre-fix line) ......... 2 failed
  * gateway fast-fail disabled ............................... 1 failed
  * `if e.code in _RETRY_CODES:` -> `if False:` .............. 1 failed
  * `if not _slept and _wait > 0:` -> `if _wait > 0:` ........ 1 failed

The last one HUNG the suite before `_cap` existed: dropping `_slept` let the
retry re-queue the same rung forever. That is why the loop is bounded
structurally (`_i < _cap`) and not only by the flag.

Run:  python3 -m pytest tests/test_brain_call_claude_429.py -v
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

_L4 = _ROOT / "routes" / "brain_v2_layer4.py"

# The real body observed live on 2026-09-01, trimmed to the fields that matter.
_GATEWAY_429 = (
    '{"success":false,"result":[],"messages":[],"error":[{"code":2041,'
    '"message":"Spend limit exceeded: rule \'5e7f1b6b\' (cost limit 100 per '
    '604800s, sliding) for anthropic anthropic/claude-fable-5"}],'
    '"name":"AiGatewayError","httpCode":429,"internalCode":2041}'
)
_ANTHROPIC_429 = (
    '{"type":"error","error":{"type":"rate_limit_error",'
    '"message":"Number of request tokens has exceeded your per-minute rate limit"}}'
)

_CHAIN = ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-5"]


# ── ast extraction (asserts a real, non-empty body) ──────────────────────────
def _extract_fn(name: str) -> str:
    src = _L4.read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg and seg.strip(), f"{name} extracted empty from {_L4}"
            return seg
    raise AssertionError(f"{name} not found in {_L4}")


def _extract_const(name: str):
    """Read a module-level constant from the SHIPPED source, so retuning it
    there retunes the test rather than silently diverging from it."""
    src = _L4.read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {_L4}")


def _http_error(code: int, body: str, retry_after: str | None = None):
    """A REAL urllib.error.HTTPError, so .read()/.headers behave as in prod."""
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://gateway.example/v1/messages", code, "err", hdrs,
        io.BytesIO(body.encode("utf-8")))


class _Sleeps(list):
    def __call__(self, secs):
        self.append(secs)


@pytest.fixture
def layer4(monkeypatch):
    """The shipped `_call_claude` + its three helpers, wired to fakes.

    Returns (call, state) where state records every HTTP attempt and sleep."""
    # Deterministic model chain: the function imports resolve_chain at call
    # time, so a stub in sys.modules is what it picks up.
    stub = types.ModuleType("routes.brain_models")
    stub.resolve_chain = lambda m, max_depth=5: list(_CHAIN)
    stub.supports_1m_context = lambda m: False
    stub.one_m_beta_header = lambda: ""
    monkeypatch.setitem(sys.modules, "routes.brain_models", stub)

    sleeps = _Sleeps()
    fake_time = types.SimpleNamespace(sleep=sleeps)

    ns = {
        "json": __import__("json"),
        "sys": sys,
        "time": fake_time,
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "BRAIN_MODEL": _CHAIN[0],
        "anthropic_messages_url": lambda: "https://gateway.example/v1/messages",
        "_RETRY_CODES": _extract_const("_RETRY_CODES"),
        "_RETRY_SLEEP_CAP_S": _extract_const("_RETRY_SLEEP_CAP_S"),
    }
    for name in ("_anthropic_error_body", "_is_gateway_block", "_retry_after_s",
                 "_call_claude"):
        exec(compile(_extract_fn(name), str(_L4), "exec"), ns)  # noqa: S102
        assert ns[name].__code__.co_code, f"{name} compiled to empty code"

    attempts = []

    def install(responder):
        def _urlopen(req, timeout=None):
            attempts.append(req.get_full_url())
            return responder(len(attempts))
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    return types.SimpleNamespace(ns=ns, call=ns["_call_claude"],
                                 attempts=attempts, sleeps=sleeps,
                                 install=install)


# ── 1. gateway spend block: name it, and stop ────────────────────────────────
def test_gateway_spend_block_is_named_in_the_error(layer4):
    layer4.install(lambda n: (_ for _ in ()).throw(
        _http_error(429, _GATEWAY_429)))
    text, err = layer4.call("draft this", "you are a drafter")

    assert text is None
    assert "Spend limit exceeded" in err, err
    assert "5e7f1b6b" in err, "the rule id an operator needs is missing: " + err
    assert err.startswith("http_429")


def test_gateway_spend_block_does_not_walk_the_chain(layer4):
    layer4.install(lambda n: (_ for _ in ()).throw(
        _http_error(429, _GATEWAY_429, retry_after="1")))
    layer4.call("draft this", "you are a drafter")

    # Every model gets the same 429 from the gateway (verified live 2026-09-01
    # against fable-5, opus-4-8 and sonnet-4-5), so retrying is pure latency on
    # a request the operator is waiting on.
    assert len(layer4.attempts) == 1, f"walked the chain: {layer4.attempts}"
    assert layer4.sleeps == [], f"slept on an unclearable block: {layer4.sleeps}"


# ── 2. a real rate limit behaves the opposite way ────────────────────────────
def test_plain_rate_limit_retries_once_then_degrades(layer4):
    layer4.install(lambda n: (_ for _ in ()).throw(
        _http_error(429, _ANTHROPIC_429, retry_after="1")))
    text, err = layer4.call("draft this", "you are a drafter")

    assert text is None
    assert "rate_limit_error" in err, err
    # one Retry-After sleep, once — not once per rung
    assert layer4.sleeps == [1.0], layer4.sleeps
    # first model twice (original + retry), then one attempt per remaining rung
    assert len(layer4.attempts) == len(_CHAIN) + 1, layer4.attempts


def test_anthropic_rate_limit_is_not_mistaken_for_a_gateway_block(layer4):
    assert layer4.ns["_is_gateway_block"](_ANTHROPIC_429) is False
    assert layer4.ns["_is_gateway_block"](_GATEWAY_429) is True


def test_retry_after_is_clamped_and_survives_junk(layer4):
    f = layer4.ns["_retry_after_s"]
    assert f(_http_error(429, "{}", retry_after="0.5"), 4.0) == 0.5
    assert f(_http_error(429, "{}", retry_after="600"), 4.0) == 4.0   # clamped
    assert f(_http_error(429, "{}"), 4.0) == 0.0                      # absent
    assert f(_http_error(429, "{}", retry_after="-3"), 4.0) == 0.0    # negative
    # HTTP-date form is legal in the RFC and unparseable as a float
    assert f(_http_error(429, "{}", retry_after="Wed, 01 Sep 2026 20:19:45 GMT"),
             4.0) == 0.0
    # budget already spent
    assert f(_http_error(429, "{}", retry_after="1"), 0.0) == 0.0


# ── 3. regression control — the OLD handler could not say any of this ────────
def test_old_handler_reported_only_the_status_code():
    """Faithful re-impl of the pre-fix branch (brain_v2_layer4.py, 2026-08-31):

        except urllib.error.HTTPError as e:
            last_err = f"http_{e.code}"

    If this ever produces the reason too, the scenario stopped reproducing and
    the tests above are no longer proving anything."""
    e = _http_error(429, _GATEWAY_429)
    old = f"http_{e.code}"
    assert old == "http_429"
    assert "Spend limit" not in old


# ── 4. the restructure did not break the ordinary paths ──────────────────────
def test_success_path_still_returns_text(layer4):
    class _Resp:
        def read(self):
            return (b'{"stop_reason":"end_turn","content":'
                    b'[{"type":"text","text":"{\\"refuse\\": true}"}]}')
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    layer4.install(lambda n: _Resp())
    text, err = layer4.call("draft this", "you are a drafter")

    assert err is None
    assert text == '{"refuse": true}'
    assert len(layer4.attempts) == 1


def test_max_tokens_stop_still_fails_loudly(layer4):
    class _Resp:
        def read(self):
            return b'{"stop_reason":"max_tokens","content":[{"type":"text","text":"{"}]}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    layer4.install(lambda n: _Resp())
    text, err = layer4.call("draft this", "you are a drafter")

    assert text is None
    assert err == "truncated_max_tokens"


def test_404_self_heal_still_degrades(layer4):
    """The pre-existing behaviour this fix must not disturb."""
    layer4.install(lambda n: (_ for _ in ()).throw(_http_error(404, '{"x":1}')))
    text, err = layer4.call("draft this", "you are a drafter")

    assert text is None
    assert err.startswith("http_404")
    assert len(layer4.attempts) == len(_CHAIN), layer4.attempts
    assert layer4.sleeps == []
