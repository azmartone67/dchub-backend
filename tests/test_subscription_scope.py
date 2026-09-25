"""routes/subscription_scope (backend#5555, P1): which subscription an
account's access rests on. Handler-level Postgres coverage lives in
tests/test_lifecycle_writers_tier_sql.py (the #5555 block)."""
import pytest

from routes import subscription_scope as ss


class _List:
    def __init__(self, data):
        self.data = data


def _fake(customers_by_email, subs_by_customer, boom=False):
    class Customer:
        @staticmethod
        def list(email=None, limit=None):
            if boom:
                raise RuntimeError("stripe down")
            return _List([{"id": c} for c in customers_by_email.get(email, [])])

    class Subscription:
        @staticmethod
        def list(customer=None, status=None, limit=None):
            return _List([{"id": i, "status": st, "created": cr}
                          for i, st, cr in subs_by_customer.get(customer, [])])

    return type("S", (), {"Customer": Customer, "Subscription": Subscription})


def test_finds_another_live_subscription_and_skips_the_ended_one():
    f = _fake({"a@x.com": ["cus_1", "cus_2"]},
              {"cus_1": [("sub_1", "active", 100)], "cus_2": [("sub_2", "active", 200)]})
    assert ss.other_live_subscription(["a@x.com"], "sub_2", stripe_mod=f) == ("cus_1", "active")


def test_newest_live_wins_across_addresses_and_statuses():
    f = _fake({"a@x.com": ["cus_1"], "b@x.com": ["cus_3"]},
              {"cus_1": [("sub_1", "past_due", 100)], "cus_3": [("sub_3", "trialing", 300)]})
    assert ss.other_live_subscription(["a@x.com", "b@x.com"], "sub_x", stripe_mod=f) == ("cus_3", "trialing")


@pytest.mark.parametrize("status", ["canceled", "incomplete", "incomplete_expired", "unpaid", "paused"])
def test_a_subscription_that_does_not_pay_is_not_live(status):
    f = _fake({"a@x.com": ["cus_1"]}, {"cus_1": [("sub_1", status, 100)]})
    assert ss.other_live_subscription(["a@x.com"], "sub_x", stripe_mod=f) is None


def test_no_addresses_means_none_and_duplicates_are_asked_once():
    calls = []

    class Customer:
        @staticmethod
        def list(email=None, limit=None):
            calls.append(email)
            return _List([])

    f = type("S", (), {"Customer": Customer, "Subscription": None})
    assert ss.other_live_subscription([], "s", stripe_mod=f) is None
    assert ss.other_live_subscription(["A@x.com", "a@x.com", "", None], "s", stripe_mod=f) is None
    assert calls == ["A@x.com"]


def test_a_stripe_failure_raises_for_the_caller_to_decide():
    with pytest.raises(RuntimeError):
        ss.other_live_subscription(["a@x.com"], "s", stripe_mod=_fake({}, {}, boom=True))


def test_customer_has_live_subscription():
    f = _fake({}, {"cus_1": [("s", "active", 1)], "cus_2": [("s", "canceled", 1)]})
    assert ss.customer_has_live_subscription("cus_1", stripe_mod=f) is True
    assert ss.customer_has_live_subscription("cus_2", stripe_mod=f) is False
    assert ss.customer_has_live_subscription("", stripe_mod=f) is False
