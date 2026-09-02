"""The first-mint CRM capture must stay reachable from the first-mint path.

`mint_trial_for_request()` ends with a big `try:` that holds the DB work, a
`finally:` that closes the connection, and — AFTER both — the r74 CRM
reverse-ETL capture, gated on `operator_email`:

    try:
        ...                                   # probe / reuse / mint
        api_key = "dch_trial_" + ...
        try:
            cur.execute("INSERT INTO auto_trial_keys ...")
            ...
        except Exception:
            return {"error": "mint_failed", "ok": False}
    finally:
        c.close()
        ...
    if operator_email:
        _crm_capture("trial_key_activated", ...)   # <- only reached by FALLING THROUGH

★ THIS IS CORRECT TODAY AND IT IS FRAGILE. The capture is reached only because
the new-key INSERT is the LAST statement in the try body and its only `return`
is the mint_failed error, so a successful first mint falls out of the try. Add
one `return` on the success path — an early exit, a new guard clause, a
refactor that returns the payload from inside the try — and first-mint capture
silently stops, with no test failing and no error logged. The reuse paths
already return from inside the try (that is by design: the capture's scope is
"first mint (not reuse)").

★ WHY THIS FILE EXISTS. On 2026-09-02 the placement was reported as a probable
BUG — "it only fires on the fall-through path" — on the reasoning that four of
the six returns skip it. That reasoning was wrong: the fall-through path IS the
first-mint path, which is exactly what the capture is scoped to. Rather than
leave the question to be re-litigated from the shape of the code, pin the
property the reasoning turned on.

Structural, no DB: the control flow is read from the AST.

Run:  python3 -m pytest tests/test_first_mint_reaches_crm_capture.py -v
"""
from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "routes" / "auto_trial.py"
_FN = "mint_trial_for_request"
_CAPTURE = "trial_key_activated"


def _parts():
    src = _SRC.read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == _FN), None)
    assert fn is not None and fn.body, f"{_FN} parsed with an EMPTY body"
    tries = [n for n in fn.body if isinstance(n, ast.Try) and n.finalbody]
    assert len(tries) == 1, (
        f"expected exactly one top-level try/finally in {_FN}, found "
        f"{len(tries)} — this guard can no longer tell which one holds the DB "
        "work; fix it, do not delete it")
    return src, fn, tries[0]


def _insert_line(src: str, fn: ast.FunctionDef) -> int:
    """Line of the statement that mints a NEW trial key."""
    hits = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "INSERT INTO auto_trial_keys" in " ".join(n.value.split())]
    assert hits, (
        "found no `INSERT INTO auto_trial_keys` in the mint function — the "
        "finder is broken, not the code")
    return max(hits)          # the mint INSERT is the last such statement


def test_the_crm_capture_sits_after_the_try():
    """If it moved inside, this file's whole premise changes."""
    src, fn, t = _parts()
    cap = [i for i, l in enumerate(src.splitlines(), 1) if _CAPTURE in l]
    assert cap, f"no {_CAPTURE!r} capture found — did it move or get deleted?"
    end = t.finalbody[-1].end_lineno
    assert cap[0] > end, (
        f"the CRM capture (L{cap[0]}) is no longer after the try/finally "
        f"(ends L{end}); the reachability argument below no longer applies")


def test_the_first_mint_success_path_falls_through_to_the_capture():
    """★ THE PIN. Between the new-key INSERT and the end of the try body, the
    only `return`s allowed are inside `except` handlers.

    A `return` on the success path there would exit before the capture — first
    mints would stop being captured, silently. That is the defect this file
    exists to catch, and it is NOT present today.
    """
    src, fn, t = _parts()
    ins = _insert_line(src, fn)
    end = t.body[-1].end_lineno

    handler_returns = {
        r.lineno
        for h in [n for n in ast.walk(t) if isinstance(n, ast.ExceptHandler)]
        for r in ast.walk(h) if isinstance(r, ast.Return)
    }
    offenders = [
        r.lineno for r in ast.walk(t)
        if isinstance(r, ast.Return)
        and ins <= r.lineno <= end
        and r.lineno not in handler_returns
    ]
    assert not offenders, (
        f"return(s) at {offenders} sit between the new-key INSERT (L{ins}) and "
        f"the end of the try body (L{end}) on a NON-error path. A first mint "
        "would exit before the r74 CRM capture, which fires only by falling "
        "through the try — captures would stop with nothing failing.")


def test_the_capture_is_still_scoped_to_a_bound_operator_email():
    """Its contract is 'first mint, and only when an email was bound' — a
    capture with no email is not a lead, and widening it silently would inflate
    the funnel rather than fix anything."""
    src, fn, t = _parts()
    lines = src.splitlines()
    cap = next(i for i, l in enumerate(lines, 1) if _CAPTURE in l)
    window = " ".join(l.strip() for l in lines[cap - 8:cap])
    assert "if operator_email" in window, (
        f"the CRM capture at L{cap} is no longer gated on operator_email; "
        f"preceding lines: {window[-200:]!r}")
