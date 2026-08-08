"""r-one-dcpi-card (2026-08-08) — the social card and embed are OCR-able public
images, and they were truncating scores and drawing zeros for absent ones.

Both `/dcpi/og/<slug>` (SVG) and `/dcpi/embed/<slug>` opened with:

    excess_score     = int(s["excess_power_score"] or 0)
    constraint_score = int(s["constraint_score"] or 0)
    ttp              = int(s["time_to_power_months"] or 0)

TWO faults per line:

1. `int()` TRUNCATES rather than rounds. Live, tokyo's excess of 19.8 rendered
   as 19 and johor's 43.8 as 43 — so the card a reader sees when a market link
   is shared disagreed with the page it links to on essentially every market.

2. `or 0` turns an ABSENT score into a confident 0. Drawing a zero in an image
   for a market that has no score is the null-as-zero defect, on the least
   correctable surface we publish — an image cannot carry a caveat.

These tests read the shipped source rather than importing main.py, per the house
rule. They assert the CONTRACT (round, and never coerce null to a number),
which is what a future refactor would break.
"""
import ast
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "routes" / "dcpi.py"

# The two public card renderers, by the route each is registered under.
CARD_ROUTES = ("/dcpi/og/<slug>", "/dcpi/embed/<slug>")


@pytest.fixture(scope="module")
def src():
    return SRC.read_text(encoding="utf-8")


def _card_functions(src):
    """Source of EVERY function registered under a card route, keyed by name.

    ★Keyed by function name, not by route, and this matters: FIVE functions are
    registered under these two routes — og_card (SVG), og_card_png (PNG),
    embed_widget, plus two 2-line delegating aliases. The first version of this
    helper keyed by ROUTE, so the dict collapsed five functions into two entries
    last-write-wins — and og_card_png, a whole card renderer that still
    truncated its scores, was invisible. Enumerating every match is what found
    it. Aliases are skipped by body length: they carry no score logic.
    """
    out = {}
    tree = ast.parse(src)
    offsets = [0]
    for ln in src.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(ln))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            dumped = ast.dump(dec)
            if not any(r in dumped for r in CARD_ROUTES):
                continue
            body = src[offsets[node.lineno - 1]:offsets[node.end_lineno]]
            # 2-line alias stubs delegate and hold no score handling
            if "excess" not in body:
                continue
            out[node.name] = body
    return out


def test_every_card_renderer_is_covered(src):
    """There are THREE renderers behind the two card routes, not two."""
    fns = _card_functions(src)
    assert {"og_card", "og_card_png", "embed_widget"} <= set(fns), (
        f"a card renderer is not being checked; found {sorted(fns)}")


def test_no_card_truncates_a_score(src):
    """int(x) floors. 19.8 must render as 20, not 19."""
    offenders = []
    for route, body in _card_functions(src).items():
        for m in re.finditer(
                r'int\(\s*s(?:\[|\.get\()["\'](?:excess_power_score|'
                r'constraint_score|time_to_power_months)["\']', body):
            offenders.append(f"{route}: {m.group(0)}")
    assert not offenders, (
        "a card truncates a score instead of rounding it:\n  "
        + "\n  ".join(offenders))


def test_no_card_coerces_a_missing_score_to_zero(src):
    """`or 0` on a score is a drawn zero for a market that has none."""
    offenders = []
    for route, body in _card_functions(src).items():
        for field in ("excess_power_score", "constraint_score",
                      "time_to_power_months"):
            if re.search(rf's(?:\[|\.get\()["\']{field}["\']\)?\]?\s+or\s+0', body):
                offenders.append(f"{route}: {field} or 0")
    assert not offenders, (
        "a card coerces an absent score to 0:\n  " + "\n  ".join(offenders))


def test_each_card_rounds_explicitly(src):
    """Positive form: the fix must be present, not merely the defect absent."""
    for route, body in _card_functions(src).items():
        assert "round(float(" in body, (
            f"{route} does not round its score values explicitly")


# ★ A per-value "each display expression has its own n/a arm" guard was
# attempted THREE times and dropped, deliberately, rather than shipped flaky:
#   v1 searched for "n/a" anywhere in the function — removing the PNG card's
#      excess n/a still passed, satisfied by its time-to-power one.
#   v2 regexed the assignment then did `if not disp: continue` — so it SKIPPED
#      when its pattern missed, i.e. it was vacuous.
#   v3 asserted the expression was locatable first, and then false-failed on a
#      clean tree because the multi-line ternaries do not yield to a single
#      lookahead.
# The n/a arms are instead verified by RENDERING each card against a
# score-less market (recorded in the PR body). Shipping a guard that either
# skips or false-fails would be worse than admitting this one is unguarded —
# a green test nobody trusts is how the Pune collision survived.
