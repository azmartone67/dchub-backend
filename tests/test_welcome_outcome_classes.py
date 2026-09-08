"""A welcome that RAISED must not look like a welcome that was skipped.

MEASURED 2026-09-07. welcome_email_log carries four outcome shapes:

    sent / sent_via_resend              delivered by the mailer
    seeded_manual_welcome               delivered by a HUMAN (founder note)
    skipped_duplicate                   benign — they already had one
    exception:No module named 'sendgrid'  GENUINELY broken, nobody got anything

`welcomed` was `status LIKE 'sent%'`, which collapsed the last three into one
indistinguishable "not welcomed". Consequences, both measured against prod:

  · welcome_undelivered reported 2 — and BOTH were people a human had
    personally written to. Pure false positives.
  · the one real failure (2026-06-10, the sendgrid import) sat in the same
    bucket and could not be told apart from a duplicate skip.

Against production, old vs new:  old_undelivered 3 -> new 1, real_failures 1.
The one that remains IS the real one.

★ WHAT THIS GUARDS. The dangerous direction is re-merging the classes. If
  `exception%` ever folds back in with the skips, a broken mailer becomes
  invisible again — and the counter that should page will instead be dominated
  by benign rows, which is exactly how this went unnoticed since June.

★ Binds to the SQL LITERAL via AST, not to the file text — the prose above
  contains every one of these status strings, so a substring search over the
  source would pass no matter what the query does.
"""
import ast
import inspect
import re
import textwrap

import pytest


@pytest.fixture(scope="module")
def sql():
    from routes import customer_white_glove as m
    src = textwrap.dedent(inspect.getsource(m))
    tree = ast.parse(src)
    lits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "welcome_email_log" in n.value]
    assert lits, "no SQL literal touching welcome_email_log found"
    joined = " ".join(lits)
    # ★ STRIP THE SQL'S OWN `--` COMMENTS FIRST. They are inside the literal
    #   and they name every status string this file discusses, so asserting
    #   "seeded_manual_welcome" in the raw literal passed even after the clause
    #   was deleted from the predicate. Mutation caught it. Test the CODE.
    joined = re.sub(r"--[^\n]*", " ", joined)
    return " ".join(joined.split()).lower()


def test_a_human_sent_welcome_counts_as_delivered(sql):
    """★ The two false positives. seeded_manual_welcome IS a delivery."""
    assert "seeded_manual_welcome" in sql, (
        "a founder note sent by a person still reads as undelivered")


def test_an_exception_is_tracked_separately_from_a_skip(sql):
    """★ THE ONE THAT MATTERS. A raised send is the only shape meaning nobody
    received anything; it must be its own signal."""
    assert "welcome_errored" in sql, "no separate error class"
    assert re.search(r"like 'exception", sql), (
        "nothing keys on the exception status, so a crashed mailer is "
        "indistinguishable from a duplicate skip")


def test_the_error_class_is_not_folded_into_welcomed(sql):
    """If exception% were accepted as delivered, the failure would vanish
    entirely rather than merely hide."""
    m = re.search(r"and \(coalesce\(w\.status,''\) like 'sent%%'(.*?)\) as welcomed", sql, re.S)
    assert m, "could not isolate the welcomed predicate"
    assert "exception" not in m.group(1), (
        "exception% is being counted as a successful welcome")


def test_receipts_are_still_excluded(sql):
    """Pre-existing behaviour that must survive the rewrite."""
    assert "not like 'receipt" in sql


def test_both_counters_are_published():
    from routes import customer_white_glove as m
    src = inspect.getsource(m)
    assert '"welcome_undelivered": undelivered' in src
    assert '"welcome_errors": welcome_errors' in src, (
        "the error count is computed but never served — invisible again")
