"""An over-length rewrite is RE-ASKED, not guillotined.

_clamp_description was written as a backstop, but the model kept overshooting
and the backstop became the normal path: over the 2026-09-07 02:28Z
regeneration, 37 of 132 stored descriptions had been cut and each ended
mid-thought — "...fiber carriers, costs, and decision", "...distance_miles
ready for". Cutting a long answer can only produce a broken sentence; asking
again produces a short one.

★ THE FAILURE MODE THIS PINS IS A SILENT NO-OP. If the inner model call clamps
its own output, it can never return a string longer than _DESC_MAX, the
length check upstream is never true, and the retry is dead code that looks
present. The first draft of this change had exactly that bug.

Loaded from the shipped source with `ast` — routes/* import flask and a pool.
"""
import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "ai_platform_tool_tuner.py"


def _fn(name, parent=None):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    scope = tree
    if parent:
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == parent:
                scope = n
                break
        else:
            raise AssertionError(f"{parent} not found")
    for n in ast.walk(scope):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found")


def _calls(node, name):
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == name]


def test_the_inner_call_does_not_clamp_its_own_output():
    """The load-bearing one. If _ask clamps, the retry can never fire."""
    ask = _fn("_ask", parent="_claude_rewrite")
    assert not _calls(ask, "_clamp_description"), (
        "_ask clamps its own return value, so it can never exceed _DESC_MAX "
        "and the over-length retry upstream becomes unreachable dead code")


def test_the_retry_is_guarded_by_a_length_check_on_the_cap():
    fn = _fn("_claude_rewrite")
    ask_calls = _calls(fn, "_ask")
    assert len(ask_calls) == 2, (
        f"expected exactly two _ask calls (first pass + one retry), got "
        f"{len(ask_calls)}")
    # the second must sit inside an `if` whose test compares len(...) to _DESC_MAX
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "_DESC_MAX" in test_src and "len" in test_src:
            if any(c in ast.walk(node) for c in ask_calls):
                guarded = True
    assert guarded, (
        "the retry _ask is not inside an `if len(...) > _DESC_MAX` guard")


def test_exactly_one_retry_not_a_loop():
    """132 cells per seed. An unbounded retry loop reintroduces the runtime
    that got the DB connection force-reclaimed."""
    fn = _fn("_claude_rewrite")
    for node in ast.walk(fn):
        if isinstance(node, (ast.While, ast.For)):
            inner = _calls(node, "_ask")
            assert not inner, (
                "_ask is called inside a loop in _claude_rewrite — the retry "
                "must be a single extra pass, not a retry loop")


def test_clamp_survives_as_the_backstop():
    fn = _fn("_claude_rewrite")
    assert _calls(fn, "_clamp_description"), (
        "the clamp was removed entirely; it must remain for the case where "
        "even the retry comes back over the cap")


def test_retry_prompt_states_the_measured_length_and_the_limit():
    """A corrective prompt that does not say what was wrong is just the same
    prompt again."""
    fn = _fn("_claude_rewrite")
    joined = ""
    for node in ast.walk(fn):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    joined += v.value
                if isinstance(v, ast.FormattedValue):
                    joined += "{" + ast.dump(v.value) + "}"
    assert "characters" in joined, "retry prompt never mentions characters"
    assert "_DESC_MAX" in joined or "_DESC_ASK" in joined, (
        "the retry prompt does not interpolate the actual limit, so it cannot "
        "tell the model what to aim for")
    assert "len" in joined, (
        "the retry prompt does not interpolate the measured length of the "
        "previous answer")


def test_retry_telemetry_is_reset_per_run_and_published():
    src = SRC.read_text(encoding="utf-8")
    reset = _fn("reset_claim_run")
    assigned = {ast.dump(t) for n in ast.walk(reset)
                if isinstance(n, ast.Assign) for t in n.targets}
    assert any("_RETRY_STATS" in a for a in assigned), (
        "_RETRY_STATS is not reset per run, so its counts accumulate across "
        "every seed the process ever serves")
    assert "overlong_retries=" in src, (
        "the retry count is not published in the seed response, so 'the clamp "
        "is the exception' stays unverifiable")

def test_the_retry_actually_asks_the_corrective_prompt():
    """★ _ask takes the prompt as a PARAMETER and must use it. If the body
    closes over the enclosing `prompt` instead, the corrective retry re-sends
    the IDENTICAL question — it fires, costs a second model call, and differs
    only by sampling noise. That regression was in this branch and no other
    test here caught it: the retry was called, guarded and counted, just
    pointed at the wrong string."""
    ask = _fn("_ask", parent="_claude_rewrite")
    param = ask.args.args[0].arg
    names = {n.id for n in ast.walk(ask) if isinstance(n, ast.Name)}
    assert param in names, (
        f"_ask never reads its own {param!r} parameter")
    assert "prompt" not in names, (
        "_ask references the enclosing `prompt`, so the corrective retry would "
        "send the original question instead of the correction")


def test_the_model_call_uses_requests_not_urllib():
    """scripts/regression_lint.py enforces `urllib-request-on-railway`."""
    src = SRC.read_text(encoding="utf-8")
    code = re.sub(r"#[^\n]*", "", src)
    code = re.sub(r'"""(?:.|\n)*?"""', "", code)
    assert "urllib.request.urlopen" not in code, (
        "urllib.request.urlopen is back; the lint blocks it repo-wide")
    ask = _fn("_ask", parent="_claude_rewrite")
    posts = [c for c in ast.walk(ask)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "post"]
    assert posts, "_ask makes no requests.post call"


def test_the_error_body_is_passed_to_the_detail_helper():
    """A bare f"http_{code}" is the blindness http_error_detail exists to end:
    a gateway budget rule, a rate limit and a dead key all look identical."""
    ask = _fn("_ask", parent="_claude_rewrite")
    calls = [c for c in ast.walk(ask)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id == "http_error_detail"]
    assert calls, "the HTTP error path never calls http_error_detail"
    for c in calls:
        assert c.args, "http_error_detail called with no response object"


def test_the_model_rung_fallback_survived_the_rewrite():
    """404/400 must still fall through to the next model, or a single dead
    model id takes the whole tuner down — which it did once already."""
    ask = _fn("_ask", parent="_claude_rewrite")
    conts = [n for n in ast.walk(ask) if isinstance(n, ast.Continue)]
    assert conts, "no `continue` in _ask — the model-rung fallback is gone"
    src = ast.unparse(ask)
    assert "404" in src and "400" in src, (
        "the 404/400 rung condition is missing from _ask")

