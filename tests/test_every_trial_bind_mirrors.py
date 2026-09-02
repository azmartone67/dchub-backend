"""Every path that binds an email to a trial key must mirror it into mcp_dev_keys.

★★★ THE DEFECT (found 2026-09-02 while auditing two dch_trial_ rows in
mcp_dev_keys). `routes/auto_trial._mirror_trial_to_mcp_dev_keys` (r88h) exists so
a LATER Stripe payment by the bound email flips THAT EXACT KEY's tier — the
webhook reads mcp_dev_keys, so without a row there is nothing to lift and the
agent pays while its own key stays free. That is the identified->paid leak r88h
was built to close.

routes/auto_trial.py calls it from BOTH its bind endpoints (line 940
signed_up_email, line 987 operator_email). `flask_mcp_endpoints.py` POST
/api/v1/keys/identify writes the SAME columns and never called it — zero
occurrences of the symbol in that file.

Measured on production 2026-09-02:

    minted      expires     signedup operator mirrored calls
    2026-07-01  2026-07-08  True     True     False    10
    2026-07-27  2026-08-03  False    True     True     113   <- the only success
    2026-07-31  2026-08-07  True     True     False    10
    2026-08-02  2026-08-09  True     True     False    10

1 of 4 binds after the mirror shipped (2026-06-30) has an mcp_dev_keys row. The
five earlier binds predate the mirror and are not evidence.

★ This is the SECOND miss on this same endpoint of exactly this shape: the code
above the bind already carries a comment explaining that it wrote only
operator_email, so "every successful bind read as 0 downstream" for the funnel
metrics. One bind path quietly not doing what the other two do.

★ WHY THIS TEST IS STRUCTURAL, NOT A SPOT-FIX. Pinning only the endpoint that
was broken would let bind path #4 ship with the same hole. This walks EVERY
writer of the bind columns and requires each one's function to mirror, so a new
path fails here on the day it is written.

Run:  python3 -m pytest tests/test_every_trial_bind_mirrors.py -v
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MIRROR = "_mirror_trial_to_mcp_dev_keys"
# The columns that mean "an email is now bound to this trial key".
_BIND_COLS = ("signed_up_email", "operator_email")
# Files that may bind. Anything outside these is out of scope for this guard;
# _test_the_finder_still_finds_the_known_paths below fails if that stops holding.
_SEARCH = ("flask_mcp_endpoints.py", "routes/auto_trial.py")


def _mirror_call_lines(src: str, fn: ast.FunctionDef) -> list[int]:
    """Line numbers of REAL calls to the mirror inside `fn`.

    ★★★ THIS IS AN AST WALK, NOT A SUBSTRING TEST, AND THAT IS THE POINT.
    The first cut asserted `_MIRROR in body`. The body is raw source INCLUDING
    COMMENTS, so the block comment above the call — which names the symbol while
    explaining it — satisfied the assertion on its own. Deleting the actual call
    left the suite GREEN. Verified by mutation: removing `_mirror(api_key, email)`
    (i.e. restoring the exact defect this file exists to catch) passed 6/6.

    Resolves the local alias too: the cross-module caller imports it `as _mirror`,
    so looking only for the full name would miss every real call.
    """
    names = {_MIRROR}
    for n in ast.walk(fn):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == _MIRROR:
                    names.add(a.asname or a.name)
    hits = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            nm = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if nm in names:
                hits.append(n.lineno)
    return hits


def _innermost_try_swallows(fn: ast.FunctionDef, lineno: int) -> bool:
    """Does the call at `lineno` sit in a try whose handler actually SWALLOWS?

    ★★★ "inside a try" IS NOT THE INVARIANT, and asserting it was wrong.
    identify_key() already wraps its whole body in a try whose handler RETURNS an
    error response. A bare import there is lexically "inside a try" while an
    ImportError would still fail the bind — the exact thing being guarded. Verified
    by mutation: unwrapping the dedicated try left this GREEN.

    So: take the INNERMOST try containing the call, and require its handlers to
    neither return nor raise. That is what separates a dedicated
    `except Exception: pass` from a handler that changes the caller's answer.
    """
    best = None
    for t in ast.walk(fn):
        if not isinstance(t, ast.Try):
            continue
        if not any(isinstance(n, ast.Call) and n.lineno == lineno
                   for n in ast.walk(t)):
            continue
        span = (t.end_lineno or t.lineno) - t.lineno
        if best is None or span < best[0]:
            best = (span, t)
    if best is None:
        return False
    for h in best[1].handlers:
        for n in ast.walk(h):
            if isinstance(n, (ast.Return, ast.Raise)):
                return False
    return True


def _binding_functions():
    """Every (file, funcname, lineno) whose body issues an UPDATE that SETs a
    bind column on auto_trial_keys.

    ★ Resolved through the AST to the ENCLOSING FunctionDef, not by grepping a
    line window: a window cannot tell which function a SQL string belongs to,
    which is the whole question here.
    """
    out = []
    for rel in _SEARCH:
        path = _ROOT / rel
        if not path.exists():
            continue
        src = path.read_text()
        tree = ast.parse(src)
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            body = ast.get_source_segment(src, fn) or ""
            if "auto_trial_keys" not in body:
                continue
            flat = " ".join(body.split())
            if not re.search(r"UPDATE\s+auto_trial_keys", flat, re.I):
                continue
            if any(re.search(rf"SET\s+[^;]*{c}\s*=", flat, re.I) or
                   re.search(rf",\s*{c}\s*=", flat, re.I) for c in _BIND_COLS):
                out.append((rel, fn.name, fn.lineno, body))
    return out


def test_the_finder_still_finds_the_known_paths():
    """★ GUARD THE GUARD. If the finder silently matches nothing — a reworded
    UPDATE, a moved file, a renamed column — every assertion below passes
    vacuously and this file becomes decoration.

    Both auto_trial bind endpoints and the identify endpoint must be found.
    """
    found = _binding_functions()
    assert found, (
        "found NO trial-bind functions — the finder is broken, not the code. "
        "Do not delete this test; fix the finder.")
    files = {rel for rel, _, _, _ in found}
    assert "routes/auto_trial.py" in files, files
    assert "flask_mcp_endpoints.py" in files, (
        f"the identify endpoint's bind was not found; files seen: {files}")
    assert len(found) >= 4, (
        f"expected at least the 4 known bind paths (2 in auto_trial, identify, "
        f"and mint_trial_for_request), found {len(found)}: "
        f"{[(r, n) for r, n, _, _ in found]}")


# ★ A KNOWN, TRACKED GAP — not an exemption. mint_trial_for_request() also sets
# operator_email (3 branches) and does not mirror, so it carries the same leak.
# It is a 494-line function with 8 return points; threading the call through
# every success path is a different and riskier change than the one this PR
# makes, so it is marked strict-xfail rather than half-fixed. strict=True means
# that when someone DOES fix it this test XPASSes and fails the build, forcing
# the marker off — a gap that cannot be quietly forgotten.
_KNOWN_GAPS = {("routes/auto_trial.py", "mint_trial_for_request")}


def _cases():
    out = []
    for rel, name, lineno, body in _binding_functions():
        marks = [pytest.mark.xfail(strict=True, reason=(
            "known gap: mint-time operator_email bind does not mirror "
            "(tracked separately)"))] if (rel, name) in _KNOWN_GAPS else []
        out.append(pytest.param(rel, name, lineno, body,
                                id=f"{rel.rsplit('/', 1)[-1]}::{name}",
                                marks=marks))
    return out


@pytest.mark.parametrize("rel,name,lineno,body", _cases())
def test_every_trial_bind_path_mirrors_into_mcp_dev_keys(rel, name, lineno, body):
    """A bind that does not mirror silently disarms the Stripe unlock for that
    key: the webhook lifts mcp_dev_keys.tier, and there is no row to lift."""
    src, fnode = _fn(rel, name)
    calls = _mirror_call_lines(src, fnode)
    assert calls, (
        f"{rel}:{lineno} {name}() binds an email to a trial key but never CALLS "
        f"{_MIRROR}(). Without it, a later payment by that email cannot flip "
        f"this key's tier — the agent pays and its own key stays free. "
        "(A comment naming the symbol does not count.)")


def _fn(rel, name):
    src = (_ROOT / rel).read_text()
    return src, next(n for n in ast.walk(ast.parse(src))
                     if isinstance(n, ast.FunctionDef) and n.name == name)


def test_the_mirror_is_internally_fail_soft():
    """★ THE ACTUAL CONTRACT, asserted once at the source rather than per-caller.

    The mirror's own docstring says "must NEVER affect the bind": the bind is
    what the user asked for, the mirror is a conversion nicety. So its DB work
    lives inside a try/except that swallows and reports.

    An earlier cut of this file asserted a `try:` within 400 chars BEFORE each
    call site. That was proximity, not structure, and it was wrong in both
    directions — it FALSE-PASSED the two auto_trial callers by matching an
    unrelated `try: c.close()` above them, and FALSE-FAILED the identify caller
    because the first textual hit for the symbol was a comment mentioning it.
    Measure the thing itself.
    """
    src, fn = _fn("routes/auto_trial.py", _MIRROR)
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, f"{_MIRROR}() has no try/except — it can fail a bind"
    seg = " ".join((ast.get_source_segment(src, fn) or "").split())
    assert re.search(r"INSERT\s+INTO\s+mcp_dev_keys\b(?!_)", seg, re.I), (
        f"{_MIRROR}() no longer writes mcp_dev_keys — the unlock it exists for "
        "cannot work")
    assert "note_swallowed_write" in seg, (
        f"{_MIRROR}() swallows failures; it must REPORT them or the next silent "
        "mirror gap is invisible again")


def test_cross_module_callers_guard_the_import():
    """★ Only a caller OUTSIDE routes/auto_trial.py has an import that can fail;
    in-module callers reference the function directly. So the guard applies
    exactly where the risk is, instead of demanding a pointless try in-module."""
    for rel, name, lineno, body in _binding_functions():
        if rel == "routes/auto_trial.py":
            continue
        src, fnode = _fn(rel, name)
        calls = _mirror_call_lines(src, fnode)
        assert calls, f"{rel} {name}(): no real mirror call to check"
        guarded = all(_innermost_try_swallows(fnode, L) for L in calls)
        assert guarded, (
            f"{rel}:{lineno} {name}() imports {_MIRROR} across modules without a "
            "try that SWALLOWS — an ImportError there would fail a bind the "
            "user asked for. A surrounding handler that returns an error "
            "response does not count.")
