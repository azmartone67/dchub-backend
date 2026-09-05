"""The `Z` wire shape survives the utcnow retirement — batch 1 fence.

★ THE TRAP THIS EXISTS FOR. `datetime.utcnow()` is naive and deprecated
(removal scheduled), so the reflex fix is `datetime.now(timezone.utc)`. For a
SERIALIZED timestamp that reflex is wrong, and wrong in a way that ships:

    naive  utcnow().isoformat() + "Z"          -> 2026-09-05T04:24:58.917155Z
    aware  now(utc).isoformat() + "Z"          -> 2026-09-05T04:24:58.917282+00:00Z   ← malformed

Measured across the backend on 2026-09-05: 358 call sites carry that exact
`+ "Z"` shape and 363 more emit a bare `.isoformat()` that would silently gain
a `+00:00` offset. A find-and-replace would have corrupted 721 serialized
timestamps into API responses and DB rows — a far larger blast radius than the
87 naive/aware COMPARISON sites that the deprecation notice draws attention to.

★ `Z` IS CANONICAL BY DECISION. It is the majority shape here.
routes/ops_activation.py and routes/ops_claims.py already serve `+00:00` from
their own aware `utcnow()` helpers — measured live at
/api/v1/ops/install-stats: "2026-09-01T03:39:34.221213+00:00". That is drift to
reconcile in a later batch, not the standard to copy.

★ WHY A TEST AND NOT A COMMENT. The offending expression is one token away from
correct and reads fine at a glance. The next batch is written by someone (or
something) that did not run this measurement.
"""
import ast
import os
import re

from routes.pockets import _utc_iso_z

MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "routes", "pockets.py")

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def test_emits_the_canonical_Z_shape():
    got = _utc_iso_z()
    assert ISO_Z.match(got), f"not the canonical ...Z wire shape: {got!r}"


def test_never_emits_an_offset():
    """The exact defect a naive swap to now(timezone.utc) would introduce."""
    got = _utc_iso_z()
    assert "+00:00" not in got, f"offset leaked into the wire shape: {got!r}"
    assert not got.endswith("+00:00Z"), f"malformed double suffix: {got!r}"


def test_shape_is_identical_to_what_this_module_used_to_emit():
    """Digits differ (time passes); the SHAPE must not. This is the whole
    claim of batch 1 — the clock became aware, the bytes did not change."""
    import datetime
    legacy = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    shape = lambda s: re.sub(r"\d", "N", s)
    assert shape(_utc_iso_z()) == shape(legacy)


def test_the_module_has_no_utcnow_calls_left():
    """AST, not grep: this file's own docstring contains the literal
    `datetime.utcnow()`, so a text search reports a false positive here and
    would report a false NEGATIVE nowhere useful."""
    tree = ast.parse(open(MODULE, encoding="utf-8").read())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "utcnow"]
    assert calls == [], f"{len(calls)} utcnow() call(s) remain at lines " \
                        f"{[n.lineno for n in calls]}"


# ── must-fail controls: the assertions above must be able to fail ──
def test_the_shape_regex_rejects_the_defect():
    assert not ISO_Z.match("2026-09-05T04:24:58.917282+00:00")
    assert not ISO_Z.match("2026-09-05T04:24:58.917282+00:00Z")
    assert not ISO_Z.match("2026-09-05T04:24:58.917282")      # naive, no suffix
    assert ISO_Z.match("2026-09-05T04:24:58.917282Z")         # and accepts the real one
