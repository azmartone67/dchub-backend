"""One vocabulary for the rankings that are NOT the DCPI composite.

r-deployability-rank (2026-09-06).

WHY THIS MODULE EXISTS
----------------------
DC Hub derives FOUR different single numbers from the same three
market_power_scores columns (excess_power_score, constraint_score,
time_to_power_months). One of them is the published DCPI composite:

    routes/dcpi.derive_composite_score
        0.6·excess + 0.3·(100−constraint) + 0.1·ttp_term, times a verdict
        quality multiplier, BOUNDED 0-100. This is what /dcpi/<market>,
        /api/v1/dcpi/scores and util/market_entity's "DCPI Score"
        PropertyValue publish, described there as "buildability composite
        scored 0-100".

The other three are DEPLOYABILITY RANKINGS. They answer "which market should
I pick", weight time-to-power far harder, and are unbounded — three
legitimately different questions, and none of them is the DCPI composite:

    /pockets                      a fixed editorial leaderboard ordering
    /api/v1/brief/developer       ranked against the CALLER's deadline
    /api/v1/dcpi/recommend        ranked against the caller's full envelope

Each was named by whoever wrote it, and two of the three called the result
some form of "composite score". Measured live 2026-09-05/06:

    /pockets/ashburn                    "DCPI composite score -4.3"
    /dcpi/ashburn                       "DCPI 27.4"

    /api/v1/dcpi/recommend  midland-tx  scores.composite   82.3
    /api/v1/dcpi/scores/midland-tx      composite_score    83.0
    /api/v1/dcpi/recommend  upper-michigan  scores.composite 72.4
    /api/v1/dcpi/scores/upper-michigan      composite_score  77.0

★ THE SECOND PAIR IS THE DANGEROUS ONE. -4.3 against 27.4 is visibly two
different quantities. 82.3 against 83.0 reads as rounding or staleness, so an
agent takes whichever it saw and never notices — and both live under
/api/v1/dcpi/, in a dict whose sibling keys (excess_power, constraint) really
ARE DCPI columns.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not unify the formulas. They are not copies of one thing: the pockets
penalty is fixed at 24 months, the developer brief's is relative to a deadline
the caller supplied, and recommend uses no penalty at all but an urgency
BONUS. Collapsing them would silently re-rank three surfaces. The formula was
never the defect; publishing it under another quantity's name was.

So this module owns the NAMING — one label, one formula line, one disclaimer
sentence per ranking, in a registry a guard can enumerate — and the one
scoring function whose published description had provably drifted from it.
"""

from __future__ import annotations

#: The quantity all three are mistaken for, and the range that makes the
#: mistake detectable: a deployability rank can be negative, the DCPI
#: composite cannot.
DCPI_COMPOSITE_RANGE = "0-100"
DCPI_COMPOSITE_URL = "https://dchub.cloud/dcpi/"


def short_disclaimer() -> str:
    """The form that fits in a meta description or beside a number in JSON."""
    return (f"a deployability ranking, not the {DCPI_COMPOSITE_RANGE} "
            f"DCPI composite")


def basis(label: str, scope: str, prose_formula: str) -> str:
    """The full sentence: what the number is, and what it is not.

    ★ NO APOSTROPHE, and no & or <. This is interpolated into
    <script type="application/ld+json"> on /pockets, where Jinja autoescape
    rewrites ' as &#39; — and a JSON parser does not decode HTML entities, so
    the agent reads the entity literally.
    """
    return (f"{label} is the DC Hub deployability ranking for {scope} "
            f"({prose_formula}); it is unbounded and can be negative. "
            f"It is NOT the DCPI composite, which is scored "
            f"{DCPI_COMPOSITE_RANGE} and published at {DCPI_COMPOSITE_URL}")


class Ranking:
    """One surface's ranking: what it is called and how it is described."""

    __slots__ = ("key", "label", "surface", "formula", "scope", "prose_formula")

    def __init__(self, key, label, surface, formula, scope, prose_formula):
        self.key = key
        self.label = label
        self.surface = surface
        self.formula = formula              # the one-line form, for UI
        self.scope = scope
        self.prose_formula = prose_formula  # the spelled-out form, for prose

    @property
    def basis(self) -> str:
        return basis(self.label, self.scope, self.prose_formula)

    @property
    def short(self) -> str:
        return short_disclaimer()


#: Every non-DCPI ranking DC Hub publishes. A surface that invents a fifth
#: without adding it here fails tests/test_deployability_rank_is_not_dcpi.py,
#: which is the point: the registry is what stops the next one being named by
#: whoever happens to write it.
#:
#: `pockets` reproduces r-pocket-score-label's shipped strings EXACTLY — those
#: are live in meta descriptions and ld+json, and a rewording here would be an
#: invisible content change on indexed pages.
RANKINGS = {
    "pockets": Ranking(
        key="pockets",
        label="Pocket rank score",
        surface="/pockets",
        formula="excess − constraint/2 − time-to-power penalty ± verdict",
        scope="this page",
        prose_formula="excess power minus half grid-constraint minus a "
                      "time-to-power penalty, plus a verdict bonus",
    ),
    "developer_brief": Ranking(
        key="developer_brief",
        label="Shortlist rank",
        surface="/api/v1/brief/developer",
        formula="excess − constraint/2 − 5×months past your deadline "
                "(+10 BUILD)",
        scope="this shortlist",
        prose_formula="excess power minus half grid-constraint minus five "
                      "points per month past the deadline you asked for, "
                      "plus a bonus for a BUILD verdict",
    ),
    "envelope_fit": Ranking(
        key="envelope_fit",
        label="Envelope fit score",
        surface="/api/v1/dcpi/recommend",
        formula="excess − constraint/2 − 5 if no queue signal + urgency bonus",
        scope="the capacity and deadline envelope you sent",
        prose_formula="excess power minus half grid-constraint, minus five "
                      "when the market has no queue-capacity signal at all, "
                      "plus an urgency bonus for sub-18-month time-to-power",
    ),
}


# ─────────────────────────────────────────────────────────────────────────
# The one scorer that moved here, and why only this one
# ─────────────────────────────────────────────────────────────────────────
#
# /api/v1/dcpi/recommend publishes a `methodology` string beside its numbers.
# Hand-written, it had already drifted: it said
#
#     "composite = excess_power_score − 0.5 × constraint_score + urgency_bonus"
#
# and omitted the −5 a market takes for having NO queue-capacity signal — a
# penalty large enough to reorder the top of the list, described nowhere. That
# is the same class as every other defect in this family: a second,
# hand-maintained account of one thing.
#
# The scorer and the sentence that describes it now come from the same place,
# so the description cannot omit a branch the code applies.
#
# The other two rankings did not move: neither publishes a formula string, so
# neither has a second account to keep in sync, and moving them would change
# code that is not broken.

#: Points removed when a market has no queue-capacity signal at all. Not a
#: neutral 0: "we cannot see this market's queue" is a worse answer than a
#: small queue, and the ranking says so.
NO_QUEUE_SIGNAL_PENALTY = 5.0

#: (ceiling in months, bonus) — first match wins, so order matters.
URGENCY_BONUS_BANDS = ((12, 8.0), (18, 4.0))


def envelope_fit_score(excess, constraint, ttp_months, has_queue_signal):
    """/api/v1/dcpi/recommend's ranking. Pure, so a test can execute it.

    `excess` and `constraint` arrive already coerced (None -> 0.0) by the
    caller's _safe_round, and that coercion is deliberately NOT repeated here:
    a market with no excess reading should rank at the bottom, which 0.0 does,
    and silently re-coercing would hide a caller that stopped coercing.
    """
    score = float(excess) - 0.5 * float(constraint)
    if not has_queue_signal:
        score -= NO_QUEUE_SIGNAL_PENALTY
    if ttp_months is not None:
        for ceiling, bonus in URGENCY_BONUS_BANDS:
            if float(ttp_months) <= ceiling:
                score += bonus
                break
    return score


def envelope_fit_methodology() -> str:
    """The published description, GENERATED from the constants above.

    Every term the scorer applies appears here because both read the same
    numbers. The filter list is appended by the caller, which owns it.
    """
    bands = "; ".join(f"+{b:g} when time-to-power ≤ {c} months"
                      for c, b in URGENCY_BONUS_BANDS)
    r = RANKINGS["envelope_fit"]
    return (f"{r.label} = excess_power_score − 0.5 × constraint_score "
            f"− {NO_QUEUE_SIGNAL_PENALTY:g} when queue_capacity_mw is unknown; "
            f"{bands}. This is a deployability ranking, NOT the "
            f"{DCPI_COMPOSITE_RANGE} DCPI composite served at "
            f"{DCPI_COMPOSITE_URL}<market> — the two differ by design")
