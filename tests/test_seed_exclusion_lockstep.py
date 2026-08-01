"""Lock-step guard for the honest-conversion seed/comp exclusion (2026-08-01).

The "real conversions" definition lives inline in FIVE files. On 2026-07-28 a
filter added to three of them but not the fourth made /health and
/admin/funnel-health publish different values for the same named metric within
minutes. This test pins the 2026-08-01 addition — the %-free free-text seed
guard POSITION('seed' IN LOWER(COALESCE(plan_to,''))) = 0 — to ALL five sites,
so the next drift is a red test instead of a dashboard discrepancy.

Why the guard exists: the live NLR seed rows carry the Stripe price nickname
'Year 1 Research Seed — FY2026 calibration' as plan_to, which no NOT IN list
can enumerate, so seed-program revenue was passing as real new business.
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# file → the column expression its query uses (flask CTE aliases the table).
SITES = {
    "canonical_funnel.py": "plan_to",
    "main.py": "plan_to",
    "flask_mcp_endpoints.py": "c.plan_to",
    os.path.join("routes", "funnel_health.py"): "plan_to",
    os.path.join("routes", "metric_integrity_master_shell.py"): "plan_to",
}


def _predicate(col):
    return f"POSITION('seed' IN LOWER(COALESCE({col},''))) = 0"


def _code_lines_with(path, needle):
    """Lines containing needle that are CODE, not a #/-- comment — a comment
    mentioning the predicate must not satisfy this test."""
    hits = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if needle in line and not s.startswith("#") and not s.startswith("--"):
                hits.append(line)
    return hits


def test_seed_guard_present_at_every_site():
    missing = []
    for rel, col in SITES.items():
        path = os.path.join(REPO, rel)
        if not _code_lines_with(path, _predicate(col)):
            missing.append(rel)
    assert not missing, (
        f"free-text seed guard missing from {missing} — the five "
        "real-conversion definitions must stay in lock-step"
    )


def test_seed_guard_sits_inside_the_exclusion_filter():
    # The guard must appear in the same query as the NOT IN list (within a
    # few hundred chars), not merely somewhere in the file.
    for rel, col in SITES.items():
        with open(os.path.join(REPO, rel), encoding="utf-8") as f:
            body = f.read()
        pred = _predicate(col)
        ok = any(
            0 <= body.find(pred, m.end()) - m.end() <= 600
            for m in re.finditer(
                r"'comp','complimentary','research_seed_nlr','seed'", body
            )
        )
        assert ok, f"{rel}: seed guard not adjacent to the NOT IN exclusion list"


def test_guard_semantics_match_measured_labels():
    """Python mirror of the SQL predicate against the plan_to values actually
    measured in mcp_conversions on 2026-08-01: seed labels drop, real business
    survives."""

    def kept(plan_to):
        return "seed" not in (plan_to or "").lower()

    excluded = ["Year 1 Research Seed — FY2026 calibration",
                "research_seed_nlr", "seed", "SEED grant"]
    real = ["pro", "developer", "founding", "unknown",
            "Founding Member $99/mo (limited licenses)",
            "metered_usage", "one_time", None]
    assert not any(kept(p) for p in excluded)
    assert all(kept(p) for p in real)


def test_no_percent_literals_in_guard():
    # The guard must stay %-free: several of these queries execute with no
    # params tuple, where a literal % triggers psycopg2 %-substitution errors.
    for rel, col in SITES.items():
        for line in _code_lines_with(os.path.join(REPO, rel), _predicate(col)):
            assert "%" not in line.replace("%s", ""), (
                f"{rel}: seed-guard line contains a literal % — use "
                "POSITION, not LIKE"
            )
