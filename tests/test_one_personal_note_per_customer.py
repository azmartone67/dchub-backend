"""A founding customer gets ONE personal note from the founder, not two.

`founding:founder_note` and `founding:cohort_welcome` are both plain-text notes
from Jonathan, and BOTH audiences are the founding cohort — so every founding
customer was queued for two. The cohort welcome is strictly richer (cohort
position, the 15-minute founder call, the /cited-by consent link), so it wins
and the founder note becomes the fallback for anyone the cohort lane will not
reach.

The guard lives in SQL (a NOT EXISTS against founding_customers), and CI has no
DATABASE_URL, so these are static assertions — deliberately anchored on tokens
that appear exactly once per query. Behaviour was verified separately against
the live database: candidates over a 400-day lookback went 1 -> 0.
"""

import ast
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = open(os.path.join(ROOT, "founder_note.py"), encoding="utf-8").read()


def _candidate_queries():
    """The SQL literals inside find_candidates, one per candidate source."""
    fn = [n for n in ast.walk(ast.parse(SRC))
          if isinstance(n, ast.FunctionDef) and n.name == "find_candidates"]
    assert fn, "find_candidates not found — test target moved, not passing"
    qs = [n.value for n in ast.walk(fn[0])
          if isinstance(n, ast.Constant) and isinstance(n.value, str)
          and "SELECT" in n.value and "FROM" in n.value]
    assert len(qs) == 2, (
        f"expected 2 candidate-source queries, found {len(qs)}. Both must "
        f"carry the guard; guarding one silently leaves the other sending.")
    return qs


def test_both_candidate_sources_defer_to_the_cohort_welcome():
    for q in _candidate_queries():
        assert "founding_customers" in q, (
            "a candidate source does not check founding_customers — that "
            "source will still send a second personal note")
        assert "contact_status" in q, (
            "the guard does not read contact_status, so it cannot tell "
            "whether a cohort welcome is queued")


def test_the_guard_covers_queued_as_well_as_already_sent():
    """'auto-tagged'/'new' means the cohort welcome is still QUEUED for the
    09/21 sweep. Skipping only 'welcomed' would send the second note in the
    window before the better one goes out — which is the exact overlap."""
    for q in _candidate_queries():
        block = q[q.index("founding_customers"):]
        for status in ("new", "auto-tagged", "welcomed"):
            assert f"'{status}'" in block, (
                f"guard does not cover contact_status {status!r}; a customer "
                f"in that state still gets two personal notes")


def test_the_guard_is_a_not_exists_not_a_join():
    """A JOIN would DROP candidates with no founding_customers row at all —
    the fallback audience this note is being kept for."""
    for q in _candidate_queries():
        i = q.index("founding_customers")
        head = q[:i]
        # ★Anchor on the EXISTS that IMMEDIATELY precedes founding_customers.
        # `"NOT EXISTS" in q[:i]` was VACUOUS: the welcome_email_log guard
        # earlier in the same query already contains one, so flipping THIS one
        # to a bare EXISTS still passed (mutation M3).
        j = head.rindex("EXISTS")
        assert head[max(0, j - 4):j] == "NOT ", (
            "founding_customers is reached through a bare EXISTS — that "
            "INVERTS the guard and sends the second note to exactly the "
            "customers who are getting the cohort welcome")


def test_queries_stay_parameterised_safely():
    """psycopg2 scans the whole string for format specs; a literal % raises
    'tuple index out of range' and find_candidates returns nothing at all."""
    for q in _candidate_queries():
        assert not re.search(r"(?<!%)%(?![%s])", q), "bare percent in query"


def test_the_note_still_exists_for_the_fallback_case():
    """This narrows the audience; it must not delete the sender."""
    # Match the definition exactly — "def _send_note" alone still matches a
    # renamed _send_note_RETIRED (mutation M4).
    assert "def _send_note(" in SRC and "NOTE_TEMPLATE" in SRC, (
        "the founder note sender is gone — this change was meant to make it a "
        "fallback, not remove it")
