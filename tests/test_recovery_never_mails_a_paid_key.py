"""Key recovery must decide paid-vs-free on `plan`, and must fail closed.

★ WHAT THIS IS NOT. It is NOT a disclosure fix. The module docstring still says
"for security we don't email paid keys", and that sentence is STALE — the
2026-07-03 connector fix (defect #14) deliberately mails a paid account its
dch_live_ key, both inside the ?api_key= connector URL and as an X-API-Key
example. Both the paid branch and the fallthrough send the same key to the same
bound address. Reading the docstring instead of the template makes this look
like a leak; it is not one, and a fix sold as closing a leak would be wrong.

WHAT IS ACTUALLY WRONG. The paid branch required subscription_status =
'active', while api_tier_gating.resolve_effective_plan keeps a customer paid
through a dunning window during which that column reads something else. An
account the platform still serves as paid therefore missed its own branch and
fell through, which changes TWO things:

  * the mail. It gets the free-tier template, "Your DC Hub API key", instead of
    the connector setup the paid route sends.
  * WHICH KEY. The paid branch takes the newest active key (ORDER BY created_at
    DESC). The fallthrough takes ORDER BY 1 — lexical order on the key string,
    i.e. arbitrary — so a customer who has rotated keys can be mailed a stale
    one and told it is theirs.

The second failure is the error path: the paid lookup's `except` set prow =
None, so "could not determine whether this account is paid" was answered as
"not paid", routing the customer down the same wrong branch.

★ THE FAKE HONOURS THE SQL IT IS GIVEN. The users branch applies the
subscription_status filter only when the statement actually contains that
column, so this file drives the old and the new query correctly and a mutation
back to the old predicate really does change the outcome. A fake that ignored
the SQL would pass either way and prove nothing.
"""
import re

import pytest

import routes.keys_recover as kr

PAID_PLANS = ("pro", "founding", "enterprise", "starter", "developer")
EMAIL = "someone@example.com"


class _Cur:
    def __init__(self, account, fail_on=None):
        self.a, self.fail_on, self._row = account, fail_on, None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if self.fail_on and self.fail_on in s:
            raise RuntimeError("simulated query failure")
        if "FROM users" in s:
            plan = (self.a.get("plan") or "free")
            # ★ Read the plan set FROM THE STATEMENT, never from the test's own
            # constant. A fake using its own list let a mutation dropping
            # 'developer' survive the whole suite — it answered for a query the
            # module no longer sends.
            #
            # Two forms, because the statement moved from an inline IN (...) to
            # `= ANY(%s)` when the set became derived (2026-09-20). Both are
            # handled so a revert to either shape is still measured, and a
            # statement carrying NEITHER is an error rather than a pass.
            if "= ANY(" in s:
                sql_plans = {str(x) for x in (params[1] if params else [])}
                assert sql_plans, "paid lookup passed an EMPTY plan set: %r" % (params,)
            else:
                m = re.search(r"IN \(([^)]*)\)", s)
                assert m, "paid lookup lost its plan set: %r" % s
                sql_plans = {p.strip().strip("'") for p in m.group(1).split(",")}
            ok = plan in sql_plans
            # Only apply the billing filter when the statement asks for it.
            if ok and "subscription_status" in s:
                ok = (self.a.get("subscription_status") or "") == "active"
            self._row = (plan,) if ok else None
        elif "FROM mcp_dev_keys" in s:
            k = self.a.get("mcp_key")
            self._row = (k,) if k else None
        elif "FROM auto_trial_keys" in s:
            k = self.a.get("trial_key")
            self._row = (k,) if k else None
        else:
            raise AssertionError("unmodelled SQL: %r" % s)

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, account, fail_on=None):
        self.a, self.fail_on = account, fail_on

    def cursor(self):
        return _Cur(self.a, self.fail_on)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(kr, "_send",
                        lambda to, subject, html: out.append((to, subject, html)) or True)
    return out


def _run(account, sent_box, fail_on=None):
    return kr._lookup_and_send(EMAIL, connect=lambda: _Conn(account, fail_on))


# Subjects are the honest discriminator: every branch mails to the same bound
# address, so "was a key sent" cannot tell the branches apart. The SUBJECT can.
PAID_SUBJECTS = ("Connect DC Hub to Claude — your key inside",
                 "Recover your DC Hub access")
FREE_KEY_SUBJECT = "Your DC Hub API key"
TRIAL_SUBJECT = "Your DC Hub trial key"


def _subjects(sent_box):
    return [subj for _to, subj, _html in sent_box]


def _mailed_the_key(sent_box, key):
    return any(key in html for _to, _subj, html in sent_box)


@pytest.mark.parametrize("status", ["payment_failed", "past_due", "trialing", "", None])
def test_a_paid_account_off_active_status_still_gets_the_paid_mail(status, sent):
    """★ THE REGRESSION. Every one of these is an account the platform still
    treats as paid, and each used to receive the free-tier template."""
    got = _run({"plan": "pro", "subscription_status": status,
                "mcp_key": "dch_live_PAIDKEY"}, sent)
    assert got == "sent"
    assert _subjects(sent)[0] in PAID_SUBJECTS, (
        "status=%r routed a paid account to %r" % (status, _subjects(sent))
    )


@pytest.mark.parametrize("plan", PAID_PLANS)
def test_every_paid_plan_is_routed_not_just_pro(plan, sent):
    _run({"plan": plan, "subscription_status": "payment_failed",
          "mcp_key": "dch_live_PAIDKEY"}, sent)
    assert _subjects(sent)[0] in PAID_SUBJECTS, "%s was not routed as paid" % plan


def test_the_paid_branch_mails_the_NEWEST_key_not_an_arbitrary_one(sent):
    """The concrete harm. The paid branch orders by created_at DESC; the
    fallthrough orders by 1 — lexical on the key string. A customer who rotated
    keys could be mailed a stale one under the free template."""
    _run({"plan": "pro", "subscription_status": "payment_failed",
          "mcp_key": "dch_live_NEWEST"}, sent)
    assert _subjects(sent)[0] in PAID_SUBJECTS
    assert _mailed_the_key(sent, "dch_live_NEWEST")


def test_a_paid_account_with_only_a_trial_key_is_not_sent_the_trial_mail(sent):
    """Branch 3 would tell a paying customer their trial key is their key."""
    _run({"plan": "pro", "subscription_status": "payment_failed",
          "trial_key": "dch_trial_X"}, sent)
    assert TRIAL_SUBJECT not in _subjects(sent), _subjects(sent)


def test_an_unanswerable_paid_check_sends_nothing(sent):
    """Unknown is not free. A failure of the paid lookup must stop the walk,
    not fall through to the branches that mail a key."""
    key = "dch_live_PAIDKEY"
    got = _run({"plan": "pro", "subscription_status": "active", "mcp_key": key},
               sent, fail_on="FROM users")
    assert got == "unknown"
    assert sent == [], "sent %r after failing to establish whether the account is paid" % sent


def test_a_genuinely_free_account_still_receives_its_key(sent):
    """The fix must not break the path it narrows around."""
    key = "dch_free_KEY"
    got = _run({"plan": "free", "mcp_key": key}, sent)
    assert got == "sent"
    assert _subjects(sent) == [FREE_KEY_SUBJECT]
    assert _mailed_the_key(sent, key), "free recovery regressed"


def test_a_free_account_with_only_a_trial_key_still_receives_it(sent):
    key = "dch_trial_KEY"
    got = _run({"plan": "free", "trial_key": key}, sent)
    assert got == "sent"
    assert _mailed_the_key(sent, key)


def test_nothing_bound_reports_none_and_sends_nothing(sent):
    assert _run({"plan": "free"}, sent) == "none"
    assert sent == []


def test_exactly_one_mail_is_sent(sent):
    """No branch may run twice, and the walk must stop at the first match."""
    _run({"plan": "pro", "subscription_status": "payment_failed",
          "mcp_key": "dch_live_PAIDKEY", "trial_key": "dch_trial_X"}, sent)
    assert len(sent) == 1, _subjects(sent)
