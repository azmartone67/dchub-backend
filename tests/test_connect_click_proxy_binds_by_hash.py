#!/usr/bin/env python3
"""/api/v1/connect/click hands Stripe a HASH of the minted key, per plan (P0-D).

frontend#1535, 2026-09-21. The install pages' tiles are now the $10 pack and
Developer. The proxy used to put the RAW minted key in client_reference_id.
routes/conversion_attribution.session_id_from_cref reads a raw ref as a bare MCP
session id, which a trial key is not, so it bound nothing, while the agent's key
sat in a third party's URL. Now:

  pack        → STRIPE_LINKS["metered"]   + pk-<sha256(key)>  (the webhook's pk-
                branch grants the pack's credits to THIS key hash)
  developer   → STRIPE_LINKS["developer"] + k-<sha256(key)>   (the k- branch lifts
                a durable key's tier; a trial key gets its paid key by email)
  pro_monthly / pro_annual: no longer tiled, still routed, also hashed.

Driven through the real route; the landing-view stamp through a recording DB.
"""
import hashlib
import pathlib
import sys

import pytest
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_connect as mc  # noqa: E402
from routes._stripe_links import STRIPE_LINKS  # noqa: E402

# A FAKE trial key, low-entropy on purpose (scripts/check_no_leaked_credentials.py).
KEY = "dch_trial_" + "0" * 23
H = hashlib.sha256(KEY.encode()).hexdigest()


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


class _Db:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(mc.mcp_connect_bp)
    return app.test_client()


def _loc(client, qs):
    r = client.get("/api/v1/connect/click?platform=cursor&" + qs)
    assert r.status_code in (302, 303), r.status_code
    return r.headers["Location"]


def _with_ref(link, ref):
    return link + ("&" if "?" in link else "?") + "client_reference_id=" + ref


@pytest.mark.parametrize("plan,link,prefix", [
    ("pack", STRIPE_LINKS["metered"], "pk-"),
    ("developer", STRIPE_LINKS["developer"], "k-"),
    ("pro_monthly", mc._STRIPE_MONTHLY, "k-"),
    ("pro_annual", mc._STRIPE_ANNUAL, "k-"),
])
def test_each_plan_lands_on_its_link_with_the_key_hash(client, plan, link, prefix):
    loc = _loc(client, "plan=%s&key=%s" % (plan, KEY))
    assert loc == _with_ref(link, prefix + H)
    assert KEY not in loc, "the raw key reached Stripe"


def test_no_key_means_no_ref(client):
    assert _loc(client, "plan=pack") == STRIPE_LINKS["metered"]


@pytest.mark.parametrize("plan", ["", "starter", "enterprise", "PACKX"])
def test_an_unknown_plan_lands_on_the_pack(client, plan):
    assert _loc(client, "plan=%s&key=%s" % (plan, KEY)) == _with_ref(STRIPE_LINKS["metered"], "pk-" + H)


def test_the_hashes_are_the_shapes_the_webhook_matches():
    """pk-/k- followed by 64 lowercase hex: the webhook's k- branch rejects any
    other length, and the pk- grant keys on the same sha256 hex of the key."""
    for prefix in ("pk-", "k-"):
        ref = prefix + H
        assert len(ref) - len(prefix) == 64 and all(c in "0123456789abcdef" for c in H)


@pytest.mark.parametrize("plan", ["pack", "developer"])
def test_the_landing_view_stamp_is_kept_and_names_the_plan(client, monkeypatch, plan):
    db = _Db()
    monkeypatch.setattr(mc, "_get_db", lambda: db)
    _loc(client, "plan=%s&view_id=77&key=%s" % (plan, KEY))
    stamps = [(sql, p) for sql, p in db.log if sql.startswith("UPDATE connect_landing_views")]
    assert stamps, db.log
    sql, params = stamps[0]
    assert "stripe_clicked_at" in sql and params == (plan, 77)
