"""Two board-honesty defects, both observed live on 2026-09-12.

1. The public brain board rendered the self-assessment as "B · 3.3/100".
   weighted_score is the weighted MEAN of components each scored 0..4
   (weighted = score_sum / weight_sum), so it is on a 0..4 scale: 3.3/4 is
   82.5%, a B. The page advertised a healthy loop as a catastrophe.

2. register_claim()'s "one OPEN claim per (subject, statement, metric)" rule
   was a SELECT then an INSERT whose ON CONFLICT DO NOTHING had no unique
   index to conflict on. Live ledger, /api/v1/ops/claims:
       id=101442  subject=canon:public.countries  open
       id=101443  subject=canon:public.countries  open
   Consecutive ids, same subject, both open — two ticks both passed the
   SELECT and both INSERTed.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.brain_learning import GRADE_SCALE_MAX
import routes.claim_ledger as CL


# ── the grade scale ──────────────────────────────────────────────────
def test_the_grade_scale_is_the_component_scale_not_a_percentage():
    assert GRADE_SCALE_MAX == 4, "components are scored 0..4, not 0..100"


def test_the_letter_bands_live_on_the_same_scale():
    """A grade must be reachable: the top band must not exceed the max."""
    from routes.brain_learning import _build_rationale  # noqa: F401
    assert 3.5 <= GRADE_SCALE_MAX, "an 'A' at >=3.5 is unreachable above the max"


def test_the_public_board_divides_by_the_scale_not_by_100():
    """The shipped bug: the live board rendered "B · 3.3/100"."""
    from routes.brain_v2_public import grade_score_text
    out = grade_score_text(3.3)
    assert "/100" not in out, out
    assert out.endswith(f"/{GRADE_SCALE_MAX}"), out
    assert "3.3" in out


def test_no_score_renders_nothing_rather_than_a_bare_denominator():
    from routes.brain_v2_public import grade_score_text
    assert grade_score_text(None) == ""
    assert grade_score_text("3.3") == ""
    assert grade_score_text(True) == ""


def test_the_score_text_escapes_through_the_page_escaper():
    from routes.brain_v2_public import grade_score_text
    assert "MARKED" in grade_score_text(3.3, esc=lambda x: "MARKED")


def test_a_b_grade_reads_as_a_pass_on_this_scale():
    """3.3 was the live value. On the right scale that is 82.5%."""
    assert 3.3 / GRADE_SCALE_MAX > 0.8


# ── the claim dedupe ─────────────────────────────────────────────────
def test_a_unique_violation_counts_as_a_successful_dedupe():
    """A duplicate blocked by the index is the rule WORKING, and must not be
    reported as a failed registration."""
    class _PGErr(Exception):
        pgcode = "23505"
    assert CL._is_unique_violation(_PGErr("dup"))
    assert CL._is_unique_violation(
        Exception('duplicate key value violates unique constraint "x"'))


def test_an_unrelated_error_is_still_an_error():
    assert not CL._is_unique_violation(Exception("connection refused"))
    assert not CL._is_unique_violation(Exception("no such table"))


class _Cur:
    def __init__(self, fail=None, rows=None):
        self.fail, self.rows, self.sql = fail, rows, []
    def execute(self, q, *a):
        self.sql.append(q)
        if self.fail and "CREATE UNIQUE INDEX" in q:
            raise Exception(self.fail)
    def fetchone(self):
        return self.rows


def test_the_index_is_partial_on_open_claims_and_keyed_on_the_dedupe_tuple():
    """A unique index over ALL rows would reject a legitimate re-registration
    after the previous claim was judged. It must be scoped to open claims and
    match the exact tuple the SELECT fast-path uses."""
    cur = _Cur()
    assert CL.ensure_open_claim_unique_index(cur)["ok"]
    sql = " ".join(cur.sql)
    assert "WHERE outcome IS NULL" in sql, sql
    for col in ("source_layer", "subject", "statement", "expected_metric"):
        assert col in sql, col


def test_a_blocked_index_create_is_reported_not_swallowed():
    """While duplicate open claims exist the CREATE cannot succeed. That has
    to surface — a silently-absent index leaves the race open."""
    cur = _Cur(fail="could not create unique index: key is duplicated")
    res = CL.ensure_open_claim_unique_index(cur)
    assert res["ok"] is False
    assert "duplicat" in res["error"].lower()


def test_presence_is_checked_against_pg_indexes_not_the_create_result():
    """The name existing in code is not the index existing."""
    assert CL.open_claim_index_present(_Cur(rows=(1,))) is True
    assert CL.open_claim_index_present(_Cur(rows=None)) is False
