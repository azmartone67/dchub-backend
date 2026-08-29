"""/api/v1/mcp/standing must not claim a #1 rank it does not hold.

★2026-08-28. The Smithery rank_highlight read:

    "#1 server for “data centers”, “energy”, “grid”, “power”, “fiber”,
     “hyperscale”, “interconnection”"

The frontend re-measured all ten original badges on 2026-08-15 and found SEVEN
false: energy #24, grid #11, power #6, renewables #10, "power grid" and "fiber"
#2 rather than #1, and **hyperscale not in the top 100**. dchub-frontend/ai.html
was corrected that day. THIS API WAS NOT — and it is the more citable of the
two: it serves `cite_as: "Source: dchub.cloud"` and agents read it.

★The comment justifying the hardcode said the rank was "actively defended by
registry_monitor.py, which pages on a slip". That file does not exist anywhere
in the repo. A guard named in a comment is not a guard — so this file is the
guard, and it asserts against the MEASURED not-#1 set by name, because a term
that is merely omitted cannot be asserted against.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes.mcp_standing import (            # noqa: E402
    _RANK_HIGHLIGHTS, _SMITHERY_AT_1, _SMITHERY_NOT_AT_1,
    _SMITHERY_RANK_MEASURED_AT,
)

_SMITHERY = next(r for r in _RANK_HIGHLIGHTS if r["registry"] == "Smithery")


def _claimed_at_1(claim: str) -> set[str]:
    """The terms a claim asserts #1 for — parsed from the claim STRING, which
    is where the defect lived. Only the '#1 server for …' segment counts; the
    parenthetical that names what we do NOT lead must not read as a claim."""
    head = claim.split("(", 1)[0]
    m = re.search(r"#1[^“]*", head)
    return {t.strip().lower()
            for t in re.findall(r"[“\"']([^”\"']+)[”\"']", head[m.start():] if m else "")}


def test_the_claim_asserts_no_rank_we_measured_as_not_first():
    """THE guard. Would have failed on the shipped string."""
    claimed = _claimed_at_1(_SMITHERY["claim"])
    bad = claimed & set(_SMITHERY_NOT_AT_1)
    assert not bad, (
        f"/api/v1/mcp/standing claims #1 for {sorted(bad)}, measured "
        f"{ {k: _SMITHERY_NOT_AT_1[k] for k in sorted(bad)} } on "
        f"{_SMITHERY_RANK_MEASURED_AT}")


def test_hyperscale_is_never_claimed():
    """Measured NOT IN THE TOP 100 — the most wrong of the seven."""
    assert "hyperscale" not in _claimed_at_1(_SMITHERY["claim"])
    assert "hyperscale" not in {t.lower() for t in _SMITHERY_AT_1}


def test_structured_terms_and_the_claim_string_agree():
    """at_1_terms is machine-readable and the claim is prose; an agent may read
    either, so they must not diverge."""
    assert _claimed_at_1(_SMITHERY["claim"]) == {t.lower() for t in _SMITHERY_AT_1}


def test_the_two_measured_sets_are_disjoint():
    assert not (set(_SMITHERY_AT_1) & set(_SMITHERY_NOT_AT_1))


def test_the_claim_carries_its_measurement_date():
    """A rank claim with no date cannot be judged for staleness — that is how
    this one survived from launch to 2026-08-28."""
    assert _SMITHERY_RANK_MEASURED_AT in _SMITHERY["claim"]
    assert _SMITHERY.get("measured_at") == _SMITHERY_RANK_MEASURED_AT


def test_must_fail_control_the_shipped_claim_is_caught():
    """CONTROL: run the parser over the ACTUAL string that shipped. If this
    ever stops flagging, the parser has gone blind and the guard above is
    vacuous regardless of what it asserts."""
    shipped = ("#1 server for “data centers”, “energy”, “grid”, “power”, "
               "“fiber”, “hyperscale”, “interconnection”")
    caught = _claimed_at_1(shipped) & set(_SMITHERY_NOT_AT_1)
    assert caught >= {"energy", "grid", "power", "fiber", "hyperscale"}, \
        f"parser failed to flag the shipped over-claim; saw {caught}"


def test_every_highlight_still_carries_registry_claim_and_source():
    """The /mcp-standing HTML page and its schema.org sameAs read these keys."""
    for r in _RANK_HIGHLIGHTS:
        assert r.get("registry") and r.get("claim") and r.get("source")
        assert r["source"].startswith("https://")
