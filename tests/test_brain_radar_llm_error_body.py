"""The `llm_error_body_discarded` detector — the one that would have caught
`claude call failed: http_429`.

WHY THIS EXISTS
---------------
2026-09-01: a Cloudflare AI Gateway spend rule ("Spend limit exceeded: rule
'5e7f1b6b' (cost limit 100 per 604800s, sliding)") blocked EVERY model
pre-auth, and `brain_v2_layer4._call_claude` reported it as bare `http_429`
because the handler never read the body. The detector shipped with that fix
watches the invariant: an Anthropic caller that builds an error from `e.code`
must also read the body that explains it.

WHAT THIS TEST PROVES
---------------------
  1. RED on the exact pre-fix shape (`last_err = f"http_{e.code}"`);
  2. GREEN on the shipped fix — the handler hands the exception to a helper,
     which the detector must NOT call a discard (it cannot see inside, so it
     must not claim it did);
  3. `ast`, not grep: a COMMENTED-OUT `e.read()` does not turn it green, and a
     `.read()` on a different object in the same handler does not either;
  4. unparseable source yields no finding (UNMEASURED, not "clean");
  5. the check is REGISTERED in scan_all's sweep tuple — located with `ast`,
     so a name in a comment does not count.

Run:  python3 -m pytest tests/test_brain_radar_llm_error_body.py -v
"""

import ast
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_RADAR = _ROOT / "routes" / "brain_consistency_radar.py"
_CHECK = "check_llm_error_body_discarded"
_CORE = "_handlers_discarding_error_body"


def _module_consts() -> dict:
    """Module-level constants the sliced functions close over, read from the
    SHIPPED source so retuning them there retunes the test."""
    src = _RADAR.read_text()
    ns: dict = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_ANTHROPIC_MARKERS"
                for t in node.targets):
            exec(compile(ast.Module([node], []), str(_RADAR), "exec"), ns)  # noqa: S102
    assert "_ANTHROPIC_MARKERS" in ns, "_ANTHROPIC_MARKERS vanished from the radar"
    return ns


def _load(name: str, seed: dict | None = None):
    """Execute one function out of the SHIPPED radar source — no Flask, no DB."""
    src = _RADAR.read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg and seg.strip(), f"{name} extracted empty"
            # __file__ is how the detector locates the repo root
            ns: dict = {"__file__": str(_RADAR), **_module_consts(),
                        **(seed or {})}
            exec(compile(seg, str(_RADAR), "exec"), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f"{name} not found in {_RADAR}")


scan = _load(_CORE)


_PRE_FIX = '''
import urllib.error
def call():
    url = anthropic_messages_url()
    try:
        pass
    except urllib.error.HTTPError as e:
        last_err = f"http_{e.code}"
        return None, last_err
'''

_SHIPPED_FIX = '''
import urllib.error
def call():
    url = anthropic_messages_url()
    try:
        pass
    except urllib.error.HTTPError as e:
        detail = _anthropic_error_body(e)
        last_err = f"http_{e.code}" + (f": {detail}" if detail else "")
        return None, last_err
'''

_INLINE_READ = '''
import urllib.error
def call():
    url = anthropic_messages_url()
    try:
        pass
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return None, f"http_{e.code}: {body}"
'''


def test_red_on_the_pre_fix_shape():
    assert scan(_PRE_FIX), "the exact bug shape must be reported"


def test_green_when_the_exception_is_handed_to_a_helper():
    # The shipped fix. The detector cannot see inside `_anthropic_error_body`,
    # so it must not assert the body was discarded.
    assert scan(_SHIPPED_FIX) == []


def test_green_on_an_inline_read():
    assert scan(_INLINE_READ) == []


def test_a_commented_out_read_does_not_satisfy_it():
    grep_trap = _PRE_FIX.replace(
        '        last_err = f"http_{e.code}"',
        '        # body = e.read()\n        last_err = f"http_{e.code}"')
    assert "e.read()" in grep_trap                  # a grep would go green here
    assert scan(grep_trap), "a commented-out read must not clear the finding"


def test_a_read_on_another_object_does_not_satisfy_it():
    other = _PRE_FIX.replace(
        '        last_err = f"http_{e.code}"',
        '        other.read()\n        last_err = f"http_{e.code}"')
    assert scan(other), "reading some OTHER object is not reading the body"


def test_a_handler_that_never_touches_code_is_not_flagged():
    quiet = _PRE_FIX.replace('last_err = f"http_{e.code}"', "last_err = None")
    assert scan(quiet) == []


def test_unparseable_source_is_unmeasured_not_clean():
    assert scan("def broken(:\n") == []


def test_the_check_is_registered_in_the_sweep():
    """Located with `ast` inside scan_all — a name in a comment does not count,
    and a check that is not in the tuple never runs."""
    src = _RADAR.read_text()
    tree = ast.parse(src)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "scan_all"), None)
    assert fn is not None, "scan_all not found"
    registered = {
        el.id
        for node in ast.walk(fn) if isinstance(node, ast.Tuple)
        for el in node.elts if isinstance(el, ast.Name)
    } | {
        node.args[0].id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append" and node.args
        and isinstance(node.args[0], ast.Name)
    }
    assert _CHECK in registered, f"{_CHECK} is defined but never runs"


def test_the_detector_runs_against_the_real_tree():
    """It must EXECUTE, not just parse — and return well-formed findings."""
    findings = _load(_CHECK, {_CORE: scan})()
    assert isinstance(findings, list)
    for f in findings:
        assert f["issue"] == "llm_error_body_discarded"
        assert f["count"] >= 1 and f["detail"]


# ── the narrowing that keeps this readable ───────────────────────────────────
_NOT_AN_LLM_CALL = '''
import urllib.error
def probe_a_website(url):
    """A HEAD probe. Reads e.code for CONTROL FLOW, has no error string to
    enrich — and lives in a file that also calls Anthropic elsewhere."""
    try:
        pass
    except urllib.error.HTTPError as e:
        if e.code == 405:
            return retry_with_get(url)
        return (False, e.code)

def ask_claude(prompt):
    url = anthropic_messages_url()
    try:
        pass
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
'''


def test_only_functions_that_call_anthropic_are_in_scope():
    """★ 8 of the first 14 reports were website probes, redirect checks and an
    internal _req helper that happened to share a file with an Anthropic call.
    They read e.code for control flow and have no reason to lose. A detector
    whose majority is noise gets ignored."""
    lines = _NOT_AN_LLM_CALL.splitlines()
    probe_at = next(i for i, l in enumerate(lines, 1) if "def probe_a_website" in l)
    claude_at = next(i for i, l in enumerate(lines, 1) if "def ask_claude" in l)

    hits = scan(_NOT_AN_LLM_CALL)

    assert len(hits) == 1, f"expected only the Claude call, got {hits}"
    assert "except urllib.error.HTTPError" in lines[hits[0] - 1]
    # the reported handler is inside ask_claude, NOT probe_a_website
    assert hits[0] > claude_at > probe_at


def test_the_tree_is_clean():
    """★ REGRESSION FENCE. Every Anthropic call site in this repo reports its
    HTTP failure body. A new one that does not will fail here by name."""
    findings = _load(_CHECK, {_CORE: scan})()
    sites = findings[0]["sites"] if findings else []
    assert sites == [], (
        "Anthropic call site(s) discarding the HTTP error body:\n  "
        + "\n  ".join(sites)
        + "\nUse utils.anthropic_helper.http_error_detail(e) in the handler.")
