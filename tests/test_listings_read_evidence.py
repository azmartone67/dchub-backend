"""Reading Capacity Source leaves evidence, and stays open (2026-09-16).

The program's risk is not that too many people read the catalogue — an agent
keeping a capacity dashboard current is a reader DC Hub wants, and the
introduction itself is already controlled: operator, site, coordinates,
substation and contact are released only after that provider accepts that
buyer's registration, under terms carrying a 12-month non-circumvention
clause. The gap these pin is the READ: who read what, and a handle on the
answer they were served.

What each test protects:
  * one `catalogue_read` entry per identified read, carrying the number of
    listings returned and the normalised filters — one per READ, never one per
    listing, or a 200-item feed would put 200 rows in the chain;
  * an identical read inside _CATALOGUE_READ_WINDOW collapses, so a poll loop
    cannot flood the chain, while a different filter set is a different read;
  * an anonymous read records NOTHING and is still answered with a receipt —
    the register evidences identified reads only, and no coverage claim beyond
    that is available from it;
  * the receipt commits to the reader without naming them: identity, time and
    filters go into an HMAC, and the response carries four provenance fields
    and nothing else;
  * the same reference sits on the register entry, so a receipt found in the
    wild traces back to the read;
  * NOTHING here restricts reading — both citations are byte-for-byte what
    they were, a missing ledger secret costs the receipt and not the answer,
    and a register that will not take a write does not cost the answer either.

Reuses the request harness from test_pocket_listings_wall_and_leads: the real
blueprint, the real map_tier_gating resolver, storage behind the module's own
_db_* seams, and register writes through the real listing_ledger.append SQL.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("flask")
import jwt  # noqa: E402 — PyJWT is a runtime dependency (routes/auth_routes.py)

import routes.exclusive_listings as el  # noqa: E402
from util import listing_ledger as ledger  # noqa: E402
from tests.test_pocket_listings_wall_and_leads import (  # noqa: E402,F401
    JWT_SECRET, _accept_terms, _bearer, _listing, _summary_live, env)


def _reads(e):
    """The catalogue_read rows in the register, in order."""
    return [r for r in e.rows if r["event"] == "catalogue_read"]


def _token(secret, user_id="u-jane", email="Jane@Acme.com", plan="free"):
    """A bearer signed with `secret` — for the case where the environment's
    JWT_SECRET is deliberately not a usable ledger key."""
    return {"Authorization": "Bearer " + jwt.encode(
        {"user_id": user_id, "email": email, "plan": plan, "role": "user",
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        secret, algorithm="HS256")}


# ── the read event ───────────────────────────────────────────────────────

def test_an_identified_feed_read_is_one_entry_with_the_count_and_the_filters(env):
    r = env.client.get("/api/v1/listings?market=Dallas", headers=_bearer())
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 1 and body["viewer"]["identified"] is True

    rows = _reads(env)
    assert len(rows) == 1
    entry = rows[0]
    assert entry["lead_id"] is None          # read evidence opens no lead
    assert entry["user_ref"] == "u-jane"
    assert entry["channel"] == "web"
    assert entry["meta"]["count"] == 1
    assert entry["meta"]["filters"] == {"market": "Dallas"}
    # The entry is in the chain proper, signed and linked like any other.
    assert ledger.check_entry(entry, ledger.ledger_keys())["hash_valid"] is True


def test_the_entry_counts_the_listings_returned_instead_of_multiplying_by_them(env):
    """A 200-item feed must cost the chain ONE entry, not 200."""
    env.listings.append(_listing(id=2, slug="phx-20", market="Phoenix", capacity_mw=20.0))
    body = env.client.get("/api/v1/listings", headers=_bearer()).get_json()
    assert body["count"] == 2                # non-vacuity: two listings really came back
    rows = _reads(env)
    assert len(rows) == 1 and rows[0]["meta"]["count"] == 2


def test_an_identical_read_collapses_inside_the_window_and_lands_after_it(env):
    h = _bearer()
    env.client.get("/api/v1/listings?market=Dallas", headers=h)
    assert len(_reads(env)) == 1

    # Same identity, same normalised filters, inside the window: collapsed.
    env.client.get("/api/v1/listings?market=Dallas", headers=h)
    assert len(_reads(env)) == 1

    # Control — a DIFFERENT filter set is a different read, not a repeat, so
    # the collapse above is the window at work and not a dead write path.
    env.client.get("/api/v1/listings?market=Phoenix", headers=h)
    assert len(_reads(env)) == 2

    # Control — another identity's identical read is also its own read.
    env.client.get("/api/v1/listings?market=Dallas",
                   headers=_bearer(user_id="u-ann", email="ann@firm.example"))
    assert len(_reads(env)) == 3

    # Age the recorded reads past the window. Simulating elapsed time in the
    # stand-in store, not editing evidence: in production the append-only
    # trigger refuses exactly this.
    old = datetime.now(timezone.utc) - el._CATALOGUE_READ_WINDOW - timedelta(minutes=1)
    for row in _reads(env):
        row["created_at"] = old
    env.client.get("/api/v1/listings?market=Dallas", headers=h)
    assert len(_reads(env)) == 4


def test_an_anonymous_read_is_not_recorded_and_is_still_answered_with_a_receipt(env):
    """No identity, no entry — and the register therefore does not show
    everyone who has read the catalogue. The read is answered in full."""
    body = env.client.get("/api/v1/listings").get_json()
    assert body["viewer"]["identified"] is False
    assert body["ok"] is True and body["count"] == 1     # answered, not refused
    assert _reads(env) == []
    assert body["retrieval_receipt"]["reference"]


def test_a_register_failure_does_not_fail_or_empty_the_read(env, monkeypatch):
    attempted = []

    def refuse(**kwargs):
        attempted.append(kwargs.get("event"))
        raise ledger.LedgerUnavailable("register down")

    monkeypatch.setattr(el, "_append_event", refuse)
    r = env.client.get("/api/v1/listings", headers=_bearer())
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["count"] == 1
    assert body["retrieval_receipt"]["reference"]
    # The write was attempted and refused — the pass above is the failure
    # being swallowed, not the write path never running.
    assert attempted == ["catalogue_read"]
    assert _reads(env) == []


# ── the retrieval receipt ────────────────────────────────────────────────

def test_the_receipt_reference_is_the_one_stored_on_the_register_entry(env):
    """A receipt found in the wild traces back to the read that produced it."""
    body = env.client.get("/api/v1/listings?market=Dallas", headers=_bearer()).get_json()
    receipt = body["retrieval_receipt"]
    ref = receipt["reference"]
    assert len(ref) == el._RECEIPT_REF_HEX
    rows = _reads(env)
    assert len(rows) == 1
    assert rows[0]["meta"]["receipt_reference"] == ref


def test_the_reference_commits_to_identity_time_and_filters_and_to_nothing_else(env):
    secret = b"ledger-secret-for-reference-tests"  # secretscan:allow (test placeholder)
    jane = {"identified": True, "user_ref": "u-jane", "session_hash": None}
    ann = {"identified": True, "user_ref": "u-ann", "session_hash": None}
    at = "2026-09-16T12:00:00.000000+00:00"
    filters = {"market": "Dallas"}
    base = el._receipt_reference(secret, jane, filters, at)

    # Stable for ONE read: the same inputs give the same reference.
    assert base == el._receipt_reference(secret, dict(jane), dict(filters), at)
    # And different across each input it commits to.
    assert base != el._receipt_reference(secret, ann, filters, at)
    assert base != el._receipt_reference(
        secret, jane, filters, "2026-09-16T12:00:01.000000+00:00")
    assert base != el._receipt_reference(secret, jane, {"market": "Phoenix"}, at)
    assert base != el._receipt_reference(b"another-key", jane, filters, at)

    # Where there is no identity the SESSION stands in for it, so an anonymous
    # read still gets a reference of its own — and two sessions differ.
    anon_a = el._receipt_reference(
        secret, {"identified": False, "session_hash": "s1"}, filters, at)
    anon_b = el._receipt_reference(
        secret, {"identified": False, "session_hash": "s2"}, filters, at)
    no_session = el._receipt_reference(
        secret, {"identified": False, "session_hash": None}, filters, at)
    assert len({base, anon_a, anon_b, no_session}) == 4

    # An API key is not an input at all: presenting one cannot change or enter
    # the reference.
    assert el._receipt_reference(
        secret, dict(jane, api_key="dchub_sentinel_key"),  # secretscan:allow (test placeholder)
        filters, at) == base


def test_the_receipt_carries_provenance_and_no_identity_material(env):
    body = env.client.get("/api/v1/listings", headers=_bearer()).get_json()
    # Non-vacuity: this response DOES know who is asking, so an absent
    # identity in the receipt is the receipt's doing, not an anonymous read.
    assert body["viewer"]["identified"] is True
    assert body["viewer"]["email_masked"] == "j***@acme.com"

    receipt = body["retrieval_receipt"]
    # Exhaustive — four provenance fields, so there is nowhere for identity to
    # hide rather than merely nowhere we happened to look.
    assert set(receipt) == {"source", "retrieved_at", "terms_version", "reference"}
    assert receipt["source"] == el.PROGRAM_NAME
    assert receipt["terms_version"] == el.TERMS_VERSION
    assert ledger.iso_utc(datetime.fromisoformat(receipt["retrieved_at"]))
    blob = json.dumps(receipt).lower()
    for material in ("u-jane", "jane@acme.com", "jane", JWT_SECRET.lower()):
        assert material not in blob

    # The register entry commits to the read without republishing it either.
    meta = _reads(env)[0]["meta"]
    assert set(meta) == {"count", "filters", "filters_key", "receipt_reference"}
    assert "jane" not in json.dumps(meta).lower()


def test_no_ledger_secret_serves_the_listings_without_a_receipt(env, monkeypatch, caplog):
    """ledger_secret() refuses a placeholder JWT_SECRET while _decode_jwt still
    verifies against it, so the caller stays identified and the only thing
    missing is the key the receipt is signed with."""
    placeholder = "change-in-production-placeholder-0123456789abcdef"  # secretscan:allow (test placeholder)
    monkeypatch.setenv("JWT_SECRET", placeholder)
    monkeypatch.delenv("DCHUB_LEAD_LEDGER_SECRET", raising=False)
    assert ledger.ledger_secret() is None

    disabled = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    try:
        with caplog.at_level(logging.WARNING, logger="routes.exclusive_listings"):
            r = env.client.get("/api/v1/listings", headers=_token(placeholder))
    finally:
        logging.disable(disabled)

    assert r.status_code == 200
    body = r.get_json()
    assert body["viewer"]["identified"] is True      # non-vacuity: still a real read
    assert body["ok"] is True and body["count"] == 1
    assert "retrieval_receipt" not in body
    assert _reads(env) == []
    assert "without a retrieval receipt" in caplog.text


def test_the_receipt_changes_neither_citation(env, monkeypatch):
    """Provenance, not a restriction. The teaser feed, the summary and a locked
    detail response stay CC-BY-4.0 / permitted_with_attribution; granted detail
    stays confidential. Byte-for-byte the citations the module defines."""
    feed = env.client.get("/api/v1/listings").get_json()
    assert feed["retrieval_receipt"]["reference"]     # a receipt IS on the response
    assert feed["citation"] == el._teaser_citation()
    assert feed["citation"]["license"] == "CC-BY-4.0"
    assert feed["citation"]["redistribution"] == "permitted_with_attribution"

    locked = env.client.get("/api/v1/listings/dfw-40").get_json()
    assert locked["locked"] is True
    assert locked["retrieval_receipt"]["reference"]
    assert locked["citation"] == el._teaser_citation()

    _accept_terms(env, _bearer())
    granted = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()
    assert granted["locked"] is False                 # non-vacuity: really unlocked
    assert granted["retrieval_receipt"]["reference"]
    assert granted["citation"] == el._citation()
    assert granted["citation"]["redistribution"] == "not_permitted"

    summary = _summary_live(env, monkeypatch).get_json()
    assert summary["ok"] is True and summary["live_count"] == 1
    assert summary["retrieval_receipt"]["reference"]
    assert summary["citation"] == el._teaser_citation()


# ── the caller-level unlock block ────────────────────────────────────────

def test_the_catalogue_says_what_this_caller_must_do_before_a_listing_opens(env):
    anon = env.client.get("/api/v1/listings").get_json()["caller_access"]
    assert anon == {"required": "registered", "granted": False,
                    "reason": "sign_in_required",
                    "unlock": {"web_sign_in_url": el._sign_in_url("/listings"),
                               "mcp_steps": ["claim_free_key", "bind_email"],
                               "pricing_url": None}}

    signed = env.client.get("/api/v1/listings", headers=_bearer()).get_json()["caller_access"]
    assert signed["granted"] is False
    assert signed["reason"] == "terms_acceptance_required"
    assert signed["unlock"]["mcp_steps"] == ["accept_capacity_terms"]
    assert signed["unlock"]["terms"] == el._terms_block()
    assert signed["unlock"]["accept"] == {"method": "POST",
                                          "path": "/api/v1/listings/terms/accept"}

    _accept_terms(env, _bearer())
    granted = env.client.get("/api/v1/listings", headers=_bearer()).get_json()["caller_access"]
    assert granted == {"required": "registered", "granted": True,
                       "reason": None, "unlock": None}


def test_a_key_without_a_bound_inbox_is_told_to_bind_it(env):
    block = el._caller_access({"identified": False, "reason": "email_binding_required"},
                              False, "/listings")
    assert block["reason"] == "email_binding_required"
    assert block["unlock"]["mcp_steps"] == ["bind_email"]


def test_the_catalogue_adds_keys_and_takes_none_away(env):
    body = env.client.get("/api/v1/listings", headers=_bearer()).get_json()
    assert set(body) == {"ok", "citation", "program", "viewer", "filters", "count",
                         "items", "pocket_locked_count", "caller_tier",
                         "can_see_pocket", "caller_access", "retrieval_receipt"}
    # CALLER-level. The block is on the response and never on an item: a
    # listing needing a higher plan is reported by upgrade_for_pocket and by
    # the item's own lock_reason, so the catalogue stays one flat read.
    assert body["items"]
    for item in body["items"]:
        assert {"locked", "lock_reason"} <= set(item)
        assert "unlock" not in item and "access" not in item and "caller_access" not in item


def test_a_listing_and_the_catalogue_name_the_same_steps_for_the_same_reason(env):
    """One _unlock_for, so the two surfaces cannot drift apart."""
    feed = env.client.get("/api/v1/listings").get_json()
    detail = env.client.get("/api/v1/listings/dfw-40").get_json()
    assert detail["access"]["reason"] == feed["caller_access"]["reason"] == "sign_in_required"
    assert detail["access"]["unlock"]["mcp_steps"] == feed["caller_access"]["unlock"]["mcp_steps"]
