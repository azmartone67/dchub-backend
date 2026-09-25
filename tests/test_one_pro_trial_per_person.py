"""One Pro trial per person (r-one-trial, 2026-09-24, owner decision).

The Pro 7-day trial Payment Link (checkout metadata offer=pro_trial_7d) cannot
limit trials itself. routes/pro_trial_guard.enforce_one_trial records each
trial redemption and, when the same email / Stripe customer / key ref already
had one on another subscription, ends the NEW trial now (Stripe charges the
first $99). Guards: first trial untouched; repeats by each identity ended;
webhook retries never act twice; an unreadable ledger ends nothing; only the
offer's subscription checkouts are considered; main.py calls it on
checkout.session.completed and alerts on a repeat.

The ledger is faked in memory: the SQL's matching rule is re-implemented here
(email lower-cased, customer, pk- key ref; different subscription). It is not
run against Postgres in this file.
"""
import os

import pytest

from routes import pro_trial_guard as g

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _DB:
    def __init__(self, rows=None, boom=False):
        self.rows = list(rows or [])      # dicts: sub, email, cust, kref, action
        self.boom = boom
        self.sql = []


class _Cur:
    def __init__(self, db):
        self.db, self._r = db, None

    def execute(self, sql, params=None):
        db = self.db
        db.sql.append(sql)
        if db.boom and "SELECT" in sql:
            raise RuntimeError("db down")
        if sql == g.SEEN_SQL:
            self._r = (1,) if any(r["sub"] == params[0] for r in db.rows) else None
        elif sql == g.PRIOR_SQL:
            p = params
            hit = [r for r in db.rows if r["sub"] != p["sub"] and (
                (p["email"] and (r.get("email") or "").lower() == p["email"])
                or (p["cust"] and r.get("cust") == p["cust"])
                or (p["kref"] and r.get("kref") == p["kref"]))]
            self._r = (hit[0]["sub"],) if hit else None
        elif sql == g.INSERT_SQL:
            if not any(r["sub"] == params["sub"] for r in db.rows):
                db.rows.append({"sub": params["sub"], "email": params["email"],
                                "cust": params["cust"], "kref": params["kref"],
                                "action": params["action"], "repeat_of": params["repeat_of"]})
        elif sql == g.UPDATE_ACTION_SQL:
            for r in db.rows:
                if r["sub"] == params[1]:
                    r["action"] = params[0]
        else:
            self._r = None

    def fetchone(self):
        return self._r

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cur(self.db)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _run(session, db, *, end_raises=None):
    ended = []

    def end(sub):
        if end_raises:
            raise end_raises
        ended.append(sub)
    out = g.enforce_one_trial(session, conn_factory=lambda: _Conn(db), end_trial=end)
    return out, ended


def _sess(sub="sub_new", email="Buyer@Example.com", cust="cus_new", ref=None, offer=g.OFFER, mode="subscription"):
    s = {"id": "cs_" + sub, "mode": mode, "subscription": sub, "customer": cust,
         "customer_details": {"email": email}, "metadata": {"offer": offer} if offer else {}}
    if ref is not None:
        s["client_reference_id"] = ref
    return s


KREF = "pk-" + "ab" * 32


def test_a_first_trial_is_recorded_and_left_alone():
    db = _DB()
    out, ended = _run(_sess(), db)
    assert out["repeat"] is False and ended == []
    assert db.rows[0]["sub"] == "sub_new" and db.rows[0]["action"] == "first_trial"
    assert db.rows[0]["email"] == "buyer@example.com"


@pytest.mark.parametrize("prior", [
    {"sub": "sub_old", "email": "buyer@example.com", "cust": "cus_other", "kref": ""},   # same email
    {"sub": "sub_old", "email": "else@x.io", "cust": "cus_new", "kref": ""},            # same customer
    {"sub": "sub_old", "email": "else@x.io", "cust": "cus_other", "kref": KREF},        # same agent key
], ids=["email", "customer", "key_ref"])
def test_a_repeat_trial_is_ended_now(prior):
    db = _DB([prior])
    out, ended = _run(_sess(ref=KREF), db)
    assert ended == ["sub_new"], out
    assert out["repeat"] is True and out["repeat_of"] == "sub_old"
    assert out["action"] == "trial_ended_now"
    new = [r for r in db.rows if r["sub"] == "sub_new"][0]
    assert new["action"] == "trial_ended_now" and new["repeat_of"] == "sub_old"


def test_email_match_ignores_case():
    db = _DB([{"sub": "sub_old", "email": "BUYER@example.COM", "cust": "", "kref": ""}])
    _, ended = _run(_sess(email="buyer@EXAMPLE.com", cust=""), db)
    assert ended == ["sub_new"]


def test_a_webhook_retry_never_acts_twice():
    db = _DB([{"sub": "sub_old", "email": "buyer@example.com", "cust": "", "kref": ""}])
    _, first = _run(_sess(), db)
    _, again = _run(_sess(), db)
    assert first == ["sub_new"] and again == []
    assert sum(1 for r in db.rows if r["sub"] == "sub_new") == 1


def test_a_different_person_is_not_a_repeat():
    db = _DB([{"sub": "sub_old", "email": "someone@else.io", "cust": "cus_old", "kref": "pk-" + "cd" * 32}])
    out, ended = _run(_sess(ref=KREF), db)
    assert out["repeat"] is False and ended == []


def test_an_unreadable_ledger_ends_nothing():
    db = _DB([{"sub": "sub_old", "email": "buyer@example.com", "cust": "", "kref": ""}], boom=True)
    out, ended = _run(_sess(), db)
    assert ended == [] and out.get("error", "").startswith("ledger:")
    out, ended = g.enforce_one_trial(_sess(), conn_factory=lambda: None, end_trial=lambda s: pytest.fail("ended")), []
    assert out == {"error": "no_db", "identity": g.identity(_sess())}


def test_a_failed_stripe_call_is_recorded_not_raised():
    db = _DB([{"sub": "sub_old", "email": "buyer@example.com", "cust": "", "kref": ""}])
    out, _ = _run(_sess(), db, end_raises=RuntimeError("stripe down"))
    assert out["repeat"] is True and out["action"] == "end_failed:RuntimeError"
    assert [r for r in db.rows if r["sub"] == "sub_new"][0]["action"] == "end_failed:RuntimeError"


@pytest.mark.parametrize("kw", [
    {"offer": None}, {"offer": "something_else"}, {"mode": "payment"}, {"sub": ""},
], ids=["no_offer", "other_offer", "one_time", "no_subscription"])
def test_only_the_trial_offer_subscription_checkout_is_considered(kw):
    db = _DB([{"sub": "sub_old", "email": "buyer@example.com", "cust": "", "kref": ""}])
    out, ended = _run(_sess(**kw), db)
    assert out == {"skipped": "not_trial_offer"} and ended == [] and db.sql == []


@pytest.mark.parametrize("ref,kref", [
    (KREF, KREF), ("PK-" + "AB" * 32, KREF), ("DCM-1234", ""), ("tu-abc", ""), ("a-" + "0" * 32, ""), ("", ""),
])
def test_only_a_pk_key_ref_counts_as_a_key(ref, kref):
    assert g.identity(_sess(ref=ref))["kref"] == kref


def test_no_identity_is_skipped():
    s = _sess(email="", cust="")
    out, ended = _run(s, _DB())
    assert out["skipped"] == "no_identity" and ended == []


def test_main_calls_it_on_checkout_completed_and_alerts_on_repeat():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    i = src.index("    if event_type == 'checkout.session.completed':")
    body = src[i:i + 3500]
    h = body.index("handle_checkout_completed(data)")
    c = body.index("enforce_one_trial(data)")
    assert h < c, "the guard must run after the checkout handler"
    assert 'if _ot.get("repeat"):' in body[c:] and "send_admin_alert_email(" in body[c:]


# r-trial-plink (2026-09-24, live gate FAIL): the live trial link has no offer
# metadata; the guard skipped both live trials. The link id must be enough.
def _live_sess(sub="sub_new", email="Buyer@Example.com", cust="cus_new", **over):
    from routes._stripe_links import PRO_TRIAL_PAYMENT_LINK_ID
    s = _sess(sub=sub, email=email, cust=cust, offer=None)
    s["payment_link"] = PRO_TRIAL_PAYMENT_LINK_ID
    s.update(over)
    return s


def test_trial_link_without_metadata_is_the_trial_offer():
    assert g.is_trial_offer(_live_sess()) is True


def test_live_gate_repeat_same_email_new_customer_is_ended_now():
    # The live repeat: same email, a NEW Stripe customer, no metadata.
    db = _DB()
    first, ended = _run(_live_sess(sub="sub_1", cust="cus_VK3PgVxl9FTt8r",
                                   email="someone@icloud.com"), db)
    assert first["repeat"] is False and ended == []
    out, ended = _run(_live_sess(sub="sub_2", cust="cus_VK3ZlnKtjD7HIE",
                                 email="someone@icloud.com"), db)
    assert out["repeat"] is True and out["repeat_of"] == "sub_1" and ended == ["sub_2"]


def test_other_payment_links_are_not_the_trial_offer():
    assert g.is_trial_offer(_live_sess(payment_link="plink_1UCTZKJ9ey2ATcQlByJCXN3W")) is False
    assert g.is_trial_offer(_live_sess(mode="payment")) is False
