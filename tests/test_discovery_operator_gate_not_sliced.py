"""tests/test_discovery_operator_gate_not_sliced.py — the operator relevance
gate must scan the WHOLE list (2026-09-04).

WHY. discovery_pipeline.py scored an article's relevance partly on whether it
names a known operator:

    has_operator = any(op.lower() in text for op in KNOWN_OPERATORS[:100])

KNOWN_OPERATORS holds 192 names. The slice silently excluded 92 of them —
Telehouse, Keppel Data Centres, ST Telemedia, EdgeCore Digital Infrastructure,
Yondr Group, CloudHQ, Vantage Data Centers, Zayo, Crown Castle, CoreWeave,
Lambda, Cerebras. An article naming only one of those lost a signal it had
earned, and nothing said so.

The slice carried no comment and no measurement. Measured before removing it:
all 192 costs 1,092ms per 1,000 articles against 556ms for 100 — half a
millisecond per article. It was not bought with anything.

★ A LIST THAT IS APPENDED TO AND READ WITH A FIXED SLICE ROTS BY CONSTRUCTION.
Every name added past the cut is dead on arrival, and the failure is invisible:
discovery simply scores a little lower and no test notices. That is the same
shape as the heal target lists that went stale five times this week — the fix
is to stop truncating, and this test keeps it that way.

Run:  python3 -m pytest tests/test_discovery_operator_gate_not_sliced.py -v
"""
from __future__ import annotations

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "discovery_pipeline.py")


def _source() -> str:
    return open(SRC, encoding="utf-8").read()


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in text.split("\n"))


def test_operator_gate_reads_the_whole_list():
    code = _strip_comments(_source())
    sliced = re.findall(r"KNOWN_OPERATORS\s*\[\s*:?\s*\d+\s*:?\s*\d*\s*\]", code)
    assert not sliced, (
        "discovery_pipeline.py reads KNOWN_OPERATORS through a slice "
        f"({sliced}). Every name past the cut is dead on arrival and nothing "
        "reports it — the article just scores lower. Scanning all of them costs "
        "~0.5ms per article. Read the whole list."
    )


def test_the_list_is_bigger_than_any_plausible_slice():
    """If the list ever shrinks below ~100 this guard would pass vacuously,
    because a [:100] slice would then be a no-op. Pin the premise."""
    code = _source()
    i = code.index("KNOWN_OPERATORS = [")
    seg = code[i:code.index("\n]", i)]
    names = re.findall(r'"([^"]+)"', seg)
    assert len(names) > 100, (
        f"KNOWN_OPERATORS holds {len(names)} names. This guard exists because "
        "the list outgrew a hardcoded [:100] slice; below that size the guard "
        "still passes but proves nothing. Re-check why the list shrank."
    )
