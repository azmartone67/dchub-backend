"""Four numbers, three of them not the DCPI composite. Each must say so.

r-deployability-rank (2026-09-06). Follows r-pocket-score-label, which fixed
the first and loudest of these.

WHAT WAS MEASURED
-----------------
Live through the edge, cache-busted:

    2026-09-05  /pockets/ashburn                      "DCPI composite score -4.3"
                /dcpi/ashburn                         "DCPI 27.4"

    2026-09-06  /api/v1/dcpi/recommend  midland-tx      scores.composite  82.3
                /api/v1/dcpi/scores/midland-tx         composite_score   83.0
                /api/v1/dcpi/recommend  upper-michigan  scores.composite  72.4
                /api/v1/dcpi/scores/upper-michigan     composite_score   77.0

★ THE SECOND PAIR IS THE DANGEROUS ONE. -4.3 against 27.4 is visibly two
quantities. 82.3 against 83.0 reads as rounding or staleness, so an agent
takes whichever it saw. Both live under /api/v1/dcpi/, and the sibling keys
in that same `scores` dict — excess_power, constraint — really ARE DCPI
columns.

THE FORMULAS ARE NOT THE DEFECT AND ARE NOT UNIFIED
---------------------------------------------------
/pockets penalises time-to-power past a fixed 24 months; the developer brief
penalises it past the deadline the CALLER sent; recommend applies no penalty
at all but an urgency bonus and a no-queue-signal deduction. Three questions.
Collapsing them would silently re-rank three surfaces. What they must share is
the NAME.

WHAT THIS GUARD ASSERTS
-----------------------
  A. The registry is complete and each entry disclaims itself, parametrised
     over util.deployability_rank.RANKINGS rather than over the one that was
     reported.
  B. The two are really different functions — EXECUTED, both scorers, so the
     premise of every assertion here stays true.
  C. /api/v1/dcpi/recommend's published `methodology` names every term its
     scorer applies. The literal it replaced omitted the -5 entirely.
  D. The routes bind the registry, and persona_briefs filters on
     PUBLISHED_ONLY — a retired twin in a paid shortlist is the same
     read-side hole closed on /markets and /pockets.

Never imports main. routes/dcpi cannot be imported without a DB, so (C) and
(D) read it via AST; the scorer itself lives in util/ precisely so it CAN be
executed.
"""
import ast
import io
import os

import pytest

from util.deployability_rank import (
    DCPI_COMPOSITE_RANGE,
    NO_QUEUE_SIGNAL_PENALTY,
    RANKINGS,
    URGENCY_BONUS_BANDS,
    envelope_fit_methodology,
    envelope_fit_score,
)
from util.dcpi_score_row import PUBLISHED_ONLY

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── A. the registry ─────────────────────────────────────────────────────
def test_every_known_ranking_is_registered():
    """Anti-vacuity, and the list itself. A surface that invents a fourth
    ranking without registering it makes every parametrised test below miss
    it — which is exactly how three of these got named by whoever wrote
    them."""
    assert set(RANKINGS) == {"pockets", "developer_brief", "envelope_fit"}, (
        f"the ranking registry changed: {sorted(RANKINGS)}. Add the new "
        f"surface here so it inherits the naming, or explain why it is "
        f"exempt.")


@pytest.mark.parametrize("key", sorted(RANKINGS))
def test_a_ranking_never_claims_to_be_the_dcpi_composite(key):
    r = RANKINGS[key]
    for field, value in (("label", r.label), ("formula", r.formula)):
        low = value.lower()
        assert "dcpi" not in low, f"{key}.{field} claims DCPI: {value!r}"
        assert "composite" not in low, (
            f"{key}.{field} calls itself a composite: {value!r}")


@pytest.mark.parametrize("key", sorted(RANKINGS))
def test_a_ranking_disowns_the_dcpi_composite_in_prose(key):
    r = RANKINGS[key]
    assert "NOT the DCPI composite" in r.basis, r.basis
    assert DCPI_COMPOSITE_RANGE in r.basis, (
        f"{key}.basis does not name the range that makes the confusion "
        f"detectable — a deployability rank can be negative, the DCPI "
        f"composite cannot")
    assert "negative" in r.basis.lower(), r.basis
    assert DCPI_COMPOSITE_RANGE in r.short and "not" in r.short.lower()


@pytest.mark.parametrize("key", sorted(RANKINGS))
def test_the_basis_is_safe_to_embed_in_ld_json(key):
    """/pockets interpolates its basis inside
    <script type="application/ld+json">, where Jinja autoescape rewrites ' as
    &#39; — and a JSON parser does not decode HTML entities. Enforced for
    every entry, not just the one that ships into ld+json today."""
    r = RANKINGS[key]
    for ch in ("'", "&", "<", ">", '"'):
        assert ch not in r.basis, (
            f"{key}.basis contains {ch!r}; autoescape turns it into an entity "
            f"a JSON parser will not decode")


def test_the_pockets_strings_are_unchanged():
    """r-pocket-score-label's strings are live in indexed meta descriptions
    and ld+json. Moving them into the registry must not have reworded them —
    that is an invisible content change on pages already crawled."""
    r = RANKINGS["pockets"]
    assert r.label == "Pocket rank score"
    assert r.formula == "excess − constraint/2 − time-to-power penalty ± verdict"
    assert r.short == "a deployability ranking, not the 0-100 DCPI composite"
    assert r.basis == (
        "Pocket rank score is the DC Hub deployability ranking for this page "
        "(excess power minus half grid-constraint minus a time-to-power "
        "penalty, plus a verdict bonus); it is unbounded and can be negative. "
        "It is NOT the DCPI composite, which is scored 0-100 and published at "
        "https://dchub.cloud/dcpi/")


# ── B. the two really are different functions ───────────────────────────
def test_envelope_fit_and_the_dcpi_composite_disagree_on_real_rows():
    """Both scorers EXECUTED on the rows measured live 2026-09-06.

    If these ever converged, the labels would stop being wrong and this whole
    file would be asserting the wrong thing — so pin the divergence with the
    numbers that motivated it.
    """
    from routes.dcpi import derive_composite_score
    # The real inputs and both outputs, read off the live endpoints rather
    # than reconstructed — an invented `ttp` here would pin the scorer to a
    # row that never existed.
    #   GET /api/v1/dcpi/recommend?top_n=4      (scores.*)
    #   GET /api/v1/dcpi/scores/<slug>          (composite_score)
    # (excess, constraint, ttp, verdict, has_queue, live_envelope, live_dcpi)
    rows = [
        (85.7, 22.8, 9.6, "BUILD", True, 82.3, 83.0),   # midland-tx
        (73.9, 19.1, 9.6, "BUILD", True, 72.4, 77.0),   # upper-michigan
        (71.4, 17.7, 8.4, "BUILD", True, 70.6, 76.1),   # williston-nd
    ]
    for excess, constraint, ttp, verdict, has_q, want_env, want_dcpi in rows:
        got = round(envelope_fit_score(excess, constraint, ttp, has_q), 1)
        assert got == want_env, (
            f"envelope_fit_score no longer reproduces the live value: "
            f"{got} != {want_env}")
        dcpi = round(derive_composite_score(excess, constraint, ttp, verdict), 1)
        assert dcpi == want_dcpi, f"DCPI composite moved: {dcpi} != {want_dcpi}"
        assert got != dcpi


def test_the_envelope_score_is_unbounded_below_and_the_dcpi_composite_is_not():
    """The fact that makes a mislabel impossible on its face."""
    from routes.dcpi import derive_composite_score
    worst = envelope_fit_score(0.0, 100.0, 48, has_queue_signal=False)
    assert worst < 0, worst
    dcpi = derive_composite_score(0.0, 100.0, 48, "AVOID")
    assert 0 <= dcpi <= 100, dcpi


# ── C. the published methodology names every term the scorer applies ────
def test_the_methodology_names_every_branch_of_the_scorer():
    """The literal this replaced said "+ urgency_bonus" and never mentioned
    the -5 for a missing queue signal — a penalty big enough to reorder the
    top of the list, published nowhere."""
    m = envelope_fit_methodology()
    assert f"{NO_QUEUE_SIGNAL_PENALTY:g}" in m, (
        f"the no-queue-signal penalty is applied but not described: {m}")
    assert "queue_capacity_mw" in m
    for ceiling, bonus in URGENCY_BONUS_BANDS:
        assert f"+{bonus:g}" in m, f"urgency band {bonus} undescribed: {m}"
        assert str(ceiling) in m, f"urgency ceiling {ceiling} undescribed: {m}"
    assert "NOT the" in m and DCPI_COMPOSITE_RANGE in m


def test_every_branch_the_methodology_describes_actually_moves_the_score():
    """The inverse: a description may not advertise a term the code dropped.

    Both directions matter. An omitted term understates the model; an
    advertised-but-dead term is a methodology that does not reproduce.
    """
    base = envelope_fit_score(50.0, 20.0, 36, has_queue_signal=True)
    assert envelope_fit_score(50.0, 20.0, 36, has_queue_signal=False) == \
        base - NO_QUEUE_SIGNAL_PENALTY
    for ceiling, bonus in URGENCY_BONUS_BANDS:
        got = envelope_fit_score(50.0, 20.0, ceiling, has_queue_signal=True)
        assert got == base + bonus, (
            f"the methodology advertises +{bonus} at {ceiling}mo but the "
            f"scorer gave {got - base}")


# ── D. the routes bind it ───────────────────────────────────────────────
def _assigns(rel, name):
    """`ast.unparse` of the module-level assignment to `name`."""
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.unparse(node.value)
    raise AssertionError(f"no module-level `{name}` in {rel}")


@pytest.mark.parametrize("rel,name,key", [
    ("routes/pockets.py",        "_RANK",          "pockets"),
    ("routes/persona_briefs.py", "_RANK",          "developer_brief"),
    ("routes/dcpi.py",           "_ENVELOPE_RANK", "envelope_fit"),
])
def test_each_route_reads_its_ranking_from_the_registry(rel, name, key):
    """Not a hand-copied label. A second copy of the naming is the same defect
    one layer up — it is how the /markets slug canon drifted from /dcpi."""
    expr = _assigns(rel, name)
    assert "RANKINGS" in expr and key in expr, (
        f"{rel} binds {name} = {expr!r}; it must index the shared registry")


def test_the_developer_brief_filters_on_published_only():
    """Retired alias-twins were unpublished, not deleted. Without this the
    shortlist can rank a row frozen at 2026-07-19 beside daily-recomputed
    ones, and hand a paying developer a market under a slug every other
    surface 301s away from.

    Read from the AST rather than by substring: `PUBLISHED_ONLY` also appears
    in the import line and in a comment, and either would satisfy a grep while
    the predicate sat outside the WHERE list.
    """
    src = _src("routes/persona_briefs.py")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "where"
                and isinstance(node.value, ast.List)):
            continue
        found = True
        names = [ast.unparse(e) for e in node.value.elts]
        assert "PUBLISHED_ONLY" in names, (
            f"the developer shortlist's WHERE list is {names} — the publish "
            f"predicate is not in it")
    assert found, "no `where = [...]` list literal in persona_briefs.py"


def test_the_recommend_route_uses_the_shared_scorer_and_methodology():
    """Both, and for different reasons: the scorer so the number is one
    definition, the methodology so its description cannot drift from it."""
    src = _src("routes/dcpi.py")
    assert "_envelope_fit_score(excess, constraint, ttp," in src, (
        "the recommend route hand-computes its composite again")
    assert "_envelope_fit_methodology()" in src, (
        "the methodology string is hand-written again")
    assert "Same weighting as persona_briefs" not in src, (
        "the false 'Same weighting ... keeps DCPI consistent' comment is "
        "back; it is the sentence that justified the copy")


def test_the_recommend_payload_names_the_score_beside_it():
    """`composite` stays (a published key), but it may not travel unnamed
    next to excess_power and constraint, which are real DCPI columns."""
    src = _src("routes/dcpi.py")
    for key in ('"composite_label"', '"composite_basis"'):
        assert key in src, f"{key} is not published beside scores.composite"
    assert '"composite":             round(composite, 1),' in src, (
        "scores.composite was renamed or dropped — that is a response-key "
        "contract break on an endpoint third-party agents read")
