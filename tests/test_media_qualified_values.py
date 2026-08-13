"""tests/test_media_qualified_values.py — the qualifier IS the fact (2026-08-13).

On 2026-08-13 DC Hub Media published to LinkedIn:

    "Where AI compute can actually land in 90 days:
     🟢 Ashburn ~500 MW · Sterling ~418 MW · Phoenix ~273 MW"

There is no such available capacity in Ashburn, and the platform cannot make a
90-day claim about anything. Two independent errors compounded:

  (1) THE NUMBER. /api/v1/ai-capacity-index returns deployable_mw QUALIFIED —
      {"value": 500.0, "note": "Estimate from market depth - refined via ISO
      interconnect queue join (Q3 2026)."}. The ingest read .value and dropped
      .note, so an estimate of MARKET DEPTH published as available power.

  (2) THE HEADLINE. "can actually land in 90 days" was a hardcoded string. DCPI's
      time_to_power is queue_wait_months x a reserve adjustment, and
      queue_wait_months is clip(12 + active_queue_GW*0.6, 12, 66) — the model
      FLOORS AT 12 MONTHS and cannot express 3. `horizon=90` is a capacity-index
      horizon, not a delivery promise.

★AND THE EXISTING FACT-CHECK GUARD PASSED IT. media_showcase's numeric gate
verifies every number in the copy matches a tracked fact. 500 *was* a real fact.
The guard checks the provenance of digits, never whether the sentence means what
the fact means — so a true number under a false claim sailed through. That is
why these tests assert on CLAIMS, not on numerals.

★THIRD INSTANCE OF ONE SHAPE IN TWO DAYS:
    _internal()        collapsed a failure    into {}
    _fetch_findings()  collapsed unmeasured   into []
    deployable_mw      collapsed an estimate  into a bare number
Each time the producer modelled the caveat correctly and the CONSUMER discarded
the half that carried the truth.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_media_qualified_values.py -v
"""
from __future__ import annotations

import importlib
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _mod():
    return importlib.import_module("routes.media_showcase")


def _code_only(rel: str) -> str:
    """Source with comments AND docstrings stripped.

    ★These guards assert that old, wrong strings are GONE — and the fix quotes
    those strings in a comment and a docstring, because naming the bug is how
    the next person understands the change. The first cut stripped only `#`
    comments and failed on correct code, tripping over its own documentation.
    That is the third time a guard in this codebase has flagged its own
    explanation; strip both, then assert."""
    import ast as _ast
    src = (_ROOT / rel).read_text(encoding="utf-8")
    lines = src.splitlines()
    drop = set()
    try:
        tree = _ast.parse(src)
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.Module, _ast.FunctionDef,
                                     _ast.AsyncFunctionDef, _ast.ClassDef)):
                continue
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], _ast.Expr)
                    and isinstance(body[0].value, _ast.Constant)
                    and isinstance(body[0].value.value, str)):
                d = body[0]
                drop.update(range(d.lineno - 1, (d.end_lineno or d.lineno)))
    except SyntaxError:
        pass
    out = []
    for i, line in enumerate(lines):
        if i in drop or line.strip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


def _facts(note: str | None) -> dict:
    bm = {"market": "Ashburn", "deployable_mw": 500.0}
    if note:
        bm["deployable_mw_note"] = note
    return {"markets": 320, "facilities": 17698,
            "isos": [{"iso": "ERCOT", "total_gw": 440.3,
                      "dc_share_pct": 51.1, "projects": 1893}],
            "build_markets": [bm], "platforms": ["Claude", "ChatGPT"]}


_REAL_NOTE = ("Estimate from market depth - refined via ISO interconnect "
              "queue join (Q3 2026).")


# ── (2) the unsupportable headline ────────────────────────────────────

def test_the_90_day_energisation_claim_is_gone_from_the_source():
    src = _code_only("routes/media_showcase.py")
    assert "actually land in 90 days" not in src, \
        "the 90-day energisation headline is back — DCPI time_to_power floors " \
        "at 12 months and cannot express 3"


@pytest.mark.parametrize("note", [_REAL_NOTE, None])
def test_no_composed_post_promises_a_delivery_timeframe(note):
    """★Asserts on the CLAIM, not the numerals — the existing numeric gate
    already passed this exact post."""
    text = _mod().compose_market_pulse(_facts(note))
    banned = re.compile(
        r"\b(land|landed|landing|delivered|energis|energiz|online|available)\b"
        r"[^.\n]{0,40}\b(in\s+)?\d+\s*(day|days|week|weeks|month|months)\b", re.I)
    m = banned.search(text)
    assert not m, "composed post promises a delivery timeframe: %r" % (
        m.group(0) if m else "")
    assert "90 days" not in text


# ── (1) the dropped qualifier ─────────────────────────────────────────

def test_the_note_survives_ingest():
    """The ingest must not read .value and discard .note."""
    src = _code_only("routes/media_showcase.py")
    assert "deployable_mw_note" in src, \
        "the ingest no longer carries deployable_mw's qualifier"
    assert 'dep.get("value") if isinstance(dep, dict) else dep' not in src, \
        "the ingest re-grew the .value-only read that dropped the estimate label"


def test_a_qualified_number_is_published_with_its_qualifier():
    """★THE ONE THAT MATTERS. If the API says it's an estimate, the post says so."""
    text = _mod().compose_market_pulse(_facts(_REAL_NOTE))
    assert "500" in text, "the number should still be published"
    assert "Estimate from market depth" in text, \
        "the post published a qualified number WITHOUT its qualifier — this is " \
        "exactly what shipped to LinkedIn on 2026-08-13"


def test_a_qualified_post_does_not_also_claim_not_a_guess():
    """'not a guess' beside a modelled estimate is the contradiction that made
    the original post indefensible to a research analyst."""
    text = _mod().compose_market_pulse(_facts(_REAL_NOTE))
    assert "not a guess" not in text, \
        "post claims 'not a guess' while publishing a modelled estimate"
    assert "modelled inputs labelled as such" in text


def test_an_unqualified_number_may_still_say_not_a_guess():
    """The fix must not blanket-weaken the copy. With no note from the source,
    the stronger sentence is legitimate."""
    text = _mod().compose_market_pulse(_facts(None))
    assert "not a guess" in text
    assert "Estimate from market depth" not in text


def test_market_depth_is_never_described_as_available_power():
    text = _mod().compose_market_pulse(_facts(_REAL_NOTE))
    bad = re.compile(r"\b(available|deliverable|deployable)\s+(power|capacity)\b", re.I)
    assert not bad.search(text), \
        "market-depth estimate described as available power"


def test_the_iso_queue_figures_still_publish():
    """★Do not over-correct. The ISO queue numbers ARE live and citable; a fix
    that quietly dropped them would trade a false claim for a useless post."""
    text = _mod().compose_market_pulse(_facts(_REAL_NOTE))
    assert "440.3 GW queued" in text
    assert "1,893 projects" in text or "1893 projects" in text
    assert "17,698" in text and "320" in text
