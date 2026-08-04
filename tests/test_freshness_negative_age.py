#!/usr/bin/env python3
"""A negative data age must never report `within_sla`.

Measured live on 2026-08-04, before this guard:

    "news": {"real_data_age_hours": -1145.71, "status": "within_sla",
             "target_hours": 6}

The news freshness SLA was being *satisfied* by a record that does not exist
yet — one RSS row carrying an EVENT date 48 days ahead, which is also
`MAX(published_at)`. `effective_age <= target` is trivially true for any
negative number, so the check that exists to detect stale data was reporting
healthy on impossible data.

This is the same shape as the other lies this codebase has recorded: a status
field that cannot say red about the specific thing it is watching. The read
paths were fixed separately, but freshness reads `MAX()` straight off the table,
so it needs its own guard.

The status logic is a plain arithmetic ladder, so it is tested by extraction —
running the real source rather than restating it — because the suite has no
database and must never import main.py.
"""
from __future__ import annotations

import ast
import pathlib
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "routes" / "freshness_public.py").read_text()


def _classify(effective_age, target):
    """Run the SHIPPED ladder, lifted out of the module by AST.

    ★ Extracted rather than reimplemented. A hand-copied ladder in the test
    would pass forever while the real one drifted — which is precisely how a
    check ends up unable to fail.
    """
    tree = ast.parse(SRC)
    ladder = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        seg = ast.get_source_segment(SRC, node) or ""
        if "status = \"unknown\"" in seg and "within_sla" in seg and "breach" in seg:
            ladder = seg
            break
    assert ladder, "the status ladder moved — update this extraction"
    # ast.get_source_segment starts AT the `if`, so line 0 has no indent while
    # every continuation line keeps its in-function indentation. Restore the
    # first line's indent, then dedent the whole block, or exec raises
    # IndentationError and the test fails against correct code.
    lines = ladder.splitlines()
    # The base indent is the shallowest continuation line — the `elif`s, which
    # sit at the same level as the opening `if`. Taking it from lines[1] instead
    # picks up the if-BODY's deeper indent and produces an unparseable block.
    tails = [l for l in lines[1:] if l.strip()]
    base = min((len(l) - len(l.lstrip()) for l in tails), default=0)
    block = textwrap.dedent(" " * base + lines[0] + "\n" + "\n".join(lines[1:]))
    scope = {"effective_age": effective_age, "target": target, "status": None}
    exec(block, {}, scope)  # noqa: S102 — running the shipped code IS the test
    return scope["status"]


class TestNegativeAgeIsNotHealth:
    def test_a_future_dated_source_does_not_report_within_sla(self):
        # The exact live values that prompted this.
        assert _classify(-1145.71, 6) != "within_sla", \
            "an SLA satisfied by a record from the future is not a satisfied SLA"

    def test_a_future_dated_source_breaches(self):
        assert _classify(-1145.71, 6) == "breach"

    def test_a_barely_negative_age_still_breaches(self):
        # No grace here on purpose: any negative value means the source contains
        # a record dated after now, and that is a data fault regardless of size.
        assert _classify(-0.01, 6) == "breach"

    def test_zero_age_is_healthy(self):
        # The boundary must not be swept up — zero is "just written", not future.
        assert _classify(0, 6) == "within_sla"

    def test_normal_freshness_is_unaffected(self):
        assert _classify(3, 6) == "within_sla"
        assert _classify(9, 6) == "warning"
        assert _classify(20, 6) == "breach"

    def test_unknown_is_still_unknown(self):
        assert _classify(None, 6) == "unknown"


class TestTheReasonIsReported:
    def test_a_future_dated_breach_says_why(self):
        # A breach caused by BAD DATA must not read as a breach caused by
        # staleness — the operator response is completely different.
        assert "future_dated_hours" in SRC, \
            "a future-dated breach must be distinguishable from a stale one"

    def test_the_reason_field_is_conditional(self):
        # It must appear only on the future-dated path; a key present on every
        # healthy domain would be noise, not signal.
        assert "if effective_age is not None and effective_age < 0 else {}" in SRC
