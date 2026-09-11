"""util/listing_ledger.py — the rules an operator is asked to trust.

The pocket-listing program tells an operator "this prospect came through DC
Hub" and backs it with a hash-chained register. These tests pin what that claim
rests on:

  * a clean chain verifies from genesis, through a non-UTC TIMESTAMPTZ
    round-trip and sequence gaps (both happen in Postgres);
  * every kind of rewrite — an edited entry, a moved link, a deleted or swapped
    entry, a forged signature, an edited typed column — is reported, at the
    right sequence number;
  * a rewrite re-hashed under a key id nobody holds is a break, while a
    genuinely retired key still verifies once it is configured;
  * raw PII never enters the chained record, so redaction does not break it;
  * nothing is reported written unless the row landed, and nothing is written
    without a key;
  * confirmation and operator tokens only open what they were issued for.

Writes go through the real `append` SQL against a minimal stand-in cursor that
answers exactly the three statements append issues and fails on anything else.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from util import listing_ledger as L

SECRET = b"ledger-test-secret-0123456789abcdef"
OTHER = b"another-secret-entirely-9876543210"

_INSERT_COLS = (
    "lead_id", "event", "listing_id", "listing_slug", "listing_title", "user_ref",
    "email", "name", "role", "company", "message", "requirement", "email_domain",
    "email_verified", "verified_via", "channel", "platform", "client",
    "session_hash", "ip_hash", "user_agent", "terms_version", "meta", "created_at",
    "prev_hash", "entry_json", "entry_hash", "signature", "key_id")
_SESSION_TZ = timezone(timedelta(hours=-5))

PII = {"email": "Prospect@Firm.example", "name": "Pat Prospect",
       "role": "Head of Sites", "company": "Firm Capital",
       "message": "Need this quickly", "requirement": {"capacity_mw": 30.0}}


class _Store:
    def __init__(self, swallow_insert=False):
        self.rows, self.statements = [], []
        self.commits = self.rollbacks = 0
        self.swallow_insert = swallow_insert
        self._seq = 100

    def connect(self):
        return _Conn(self)


class _Conn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _Cursor(self.store)

    def commit(self):
        self.store.commits += 1

    def rollback(self):
        self.store.rollbacks += 1


class _Cursor:
    def __init__(self, store):
        self.store, self._one = store, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.store.statements.append(s)
        if s.startswith("SELECT pg_advisory_xact_lock("):
            self._one = ("",)
        elif s == "SELECT entry_hash FROM listing_lead_ledger ORDER BY seq DESC LIMIT 1":
            self._one = (self.store.rows[-1]["entry_hash"],) if self.store.rows else None
        elif s.startswith("INSERT INTO listing_lead_ledger"):
            assert s.endswith("ON CONFLICT (entry_hash) DO NOTHING RETURNING seq")
            row = dict(zip(_INSERT_COLS, params))
            if self.store.swallow_insert or any(
                    r["entry_hash"] == row["entry_hash"] for r in self.store.rows):
                self._one = None
                return
            for key in ("requirement", "client", "meta"):
                if row[key] is not None:
                    row[key] = json.loads(row[key])          # JSONB comes back parsed
            # TIMESTAMPTZ comes back in the SESSION zone, not the zone written.
            row["created_at"] = datetime.fromisoformat(row["created_at"]).astimezone(_SESSION_TZ)
            self.store._seq += 3                              # sequences have gaps
            row["seq"] = self.store._seq
            self.store.rows.append(row)
            self._one = (row["seq"],)
        else:
            raise AssertionError(f"append issued unexpected SQL: {s[:90]}")

    def fetchone(self):
        return self._one


def _chain(n=5, secret=SECRET):
    store = _Store()
    events = ["intro_requested", "email_confirmed", "listing_viewed",
              "operator_notified", "introduced", "interest_registered"]
    for i in range(n):
        L.append(store.connect(), secret, lead_id=L.new_lead_id(), event=events[i % len(events)],
                 listing={"id": 7, "slug": "dfw-40", "title": "DFW shell"},
                 user_ref=f"u{i}", pii=dict(PII), channel="mcp", platform="claude",
                 terms_version="2026-09-11")
    return store


# ── the clean chain ───────────────────────────────────────────────────────

def test_a_clean_chain_verifies_from_genesis():
    store = _chain(5)
    assert len(store.rows) == 5 and store.commits == 5
    summary = L.verify_chain(store.rows, SECRET)
    assert summary == {"intact": True, "entries_checked": 5, "first_break_seq": None,
                       "head_seq": store.rows[-1]["seq"],
                       "head_hash": store.rows[-1]["entry_hash"],
                       "signatures_checked": True}
    assert store.rows[0]["prev_hash"] == L.GENESIS_HASH
    for prev, row in zip(store.rows, store.rows[1:]):
        assert row["prev_hash"] == prev["entry_hash"]
    for row in store.rows:
        res = L.check_entry(row, SECRET)
        assert (res["hash_valid"], res["signature_valid"], res["columns_consistent"]) == (True, True, True)


def test_raw_pii_never_enters_the_chained_record():
    row = _chain(1).rows[0]
    chained = row["entry_json"].lower()
    for value in ("prospect@firm.example", "pat prospect", "head of sites",
                  "firm capital", "need this quickly"):
        assert value not in chained
    entry = json.loads(row["entry_json"])
    assert len(entry["pii_commitment"]) == 64 and entry["email_domain"] == "firm.example"
    assert row["email"] == "Prospect@Firm.example"    # the raw column still holds it


# ── every rewrite is reported at the right place ─────────────────────────

def _edit_entry_json(rows):
    entry = json.loads(rows[2]["entry_json"])
    entry["created_at"] = "2026-01-01T00:00:00.000000+00:00"      # back-dating
    rows[2]["entry_json"] = L.canonical_json(entry)
    return rows[2]["seq"]


def _move_link(rows):
    rows[3]["prev_hash"] = rows[1]["entry_hash"]
    return rows[3]["seq"]


def _delete_entry(rows):
    victim_next = rows[3]["seq"]
    del rows[2]
    return victim_next


def _swap_entries(rows):
    rows[1], rows[2] = rows[2], rows[1]
    return rows[1]["seq"]


def _forge_signature(rows):
    rows[4]["signature"] = L.sign(OTHER, rows[4]["entry_hash"])
    return rows[4]["seq"]


def _edit_typed_column(rows):
    rows[1]["event"] = "introduced"
    return rows[1]["seq"]


@pytest.mark.parametrize("rewrite", [_edit_entry_json, _move_link, _delete_entry,
                                     _swap_entries, _forge_signature, _edit_typed_column])
def test_every_rewrite_breaks_the_chain_where_it_happened(rewrite):
    rows = _chain(5).rows
    assert L.verify_chain(rows, SECRET)["intact"] is True       # control: untouched is intact
    expected_break = rewrite(rows)
    summary = L.verify_chain(rows, SECRET)
    assert summary["intact"] is False
    assert summary["first_break_seq"] == expected_break


def test_a_rewrite_rehashed_under_a_key_nobody_holds_is_a_break():
    """The forgery a key_id field invites: rewrite an entry, re-hash and re-link
    every entry after it, and stamp each with a key id DC Hub never held. Every
    hash and every link checks out — only the signer rule catches it."""
    rows = _chain(4).rows
    prev = rows[0]["entry_hash"]
    for i, row in enumerate(rows[1:], start=1):
        entry = json.loads(row["entry_json"])
        if i == 1:
            entry["user_ref"] = "someone-else"
        entry["prev_hash"], entry["key_id"] = prev, "000000000000"
        row["entry_json"] = L.canonical_json(entry)
        row["entry_hash"] = L.sha256_hex(row["entry_json"])
        row["prev_hash"], row["signature"] = prev, "f" * 64
        prev = row["entry_hash"]
    assert all(L.check_entry(r, SECRET)["hash_valid"] for r in rows)      # the forgery is well-formed
    summary = L.verify_chain(rows, SECRET)
    assert summary["intact"] is False and summary["first_break_seq"] == rows[1]["seq"]
    assert [L.check_entry(r, SECRET)["signature_valid"] for r in rows] == [True, False, False, False]
    unsigned = L.verify_chain(rows, None)
    assert unsigned["intact"] is True and unsigned["signatures_checked"] is False


def test_an_entry_from_a_key_we_do_not_hold_is_not_verified():
    rows = _chain(3).rows
    summary = L.verify_chain(rows, OTHER)
    assert summary["intact"] is False and summary["first_break_seq"] == rows[0]["seq"]
    assert [L.check_entry(r, OTHER)["signature_valid"] for r in rows] == [False, False, False]


def test_a_retired_key_verifies_once_it_is_configured():
    rows = _chain(3, secret=SECRET).rows
    env = {"DCHUB_LEAD_LEDGER_SECRET": OTHER.decode(),
           "DCHUB_LEAD_LEDGER_PREVIOUS_SECRETS": SECRET.decode()}
    keys = L.ledger_keys(env)
    assert keys[0] == OTHER and SECRET in keys
    assert L.verify_chain(rows, keys)["intact"] is True


def test_a_former_jwt_secret_can_be_listed_as_a_retired_key():
    old_jwt, new_jwt = "old-jwt-secret-" + "x" * 30, "new-jwt-secret-" + "y" * 30
    rows = _chain(2, secret=L.ledger_secret({"JWT_SECRET": old_jwt})).rows
    rotated = {"JWT_SECRET": new_jwt, "DCHUB_LEAD_LEDGER_PREVIOUS_SECRETS": old_jwt}
    assert L.verify_chain(rows, L.ledger_keys(rotated))["intact"] is True
    assert L.verify_chain(rows, L.ledger_keys({"JWT_SECRET": new_jwt}))["intact"] is False


def test_redaction_keeps_the_chain_and_says_so():
    rows = _chain(3).rows
    for key in ("email", "name", "role", "company", "message", "requirement"):
        rows[0][key] = None
    rows[1]["name"] = "Someone Else"
    assert L.verify_chain(rows, SECRET)["intact"] is True
    states = [L.pii_state(r, json.loads(r["entry_json"]), SECRET) for r in rows]
    assert states == ["redacted", "mismatch", "intact"]
    assert L.pii_state(rows[2], json.loads(rows[2]["entry_json"]), OTHER) == "unverifiable"


# ── writes ────────────────────────────────────────────────────────────────

def test_no_key_means_no_write_and_no_sql():
    store = _Store()
    with pytest.raises(L.LedgerUnavailable):
        L.append(store.connect(), None, lead_id="LD-X", event="intro_requested", pii=dict(PII))
    assert store.statements == [] and store.rows == []


def test_a_swallowed_insert_is_never_reported_as_written():
    store = _Store(swallow_insert=True)
    with pytest.raises(L.LedgerUnavailable):
        L.append(store.connect(), SECRET, lead_id="LD-X", event="intro_requested", pii=dict(PII))
    assert store.commits == 0 and store.rollbacks == 1
    assert any(s.startswith("INSERT INTO listing_lead_ledger") for s in store.statements)


def test_unknown_events_are_refused():
    with pytest.raises(ValueError):
        L.build_entry(secret=SECRET, prev_hash=L.GENESIS_HASH,
                      created_at=datetime.now(timezone.utc), lead_id="LD-X", event="made_up")


# ── tokens ────────────────────────────────────────────────────────────────

def test_a_confirmation_token_opens_only_its_own_lead():
    now = time.time()
    commit = L.pii_commitment(SECRET, PII)
    token = L.confirm_token(SECRET, "LD-AAAAAAAAAA", commit, issued_at=now)
    assert L.check_confirm_token(SECRET, "LD-AAAAAAAAAA", commit, token, now=now) is True
    assert L.check_confirm_token(SECRET, "LD-BBBBBBBBBB", commit, token, now=now) is False
    assert L.check_confirm_token(SECRET, "LD-AAAAAAAAAA", "0" * 64, token, now=now) is False
    assert L.check_confirm_token(OTHER, "LD-AAAAAAAAAA", commit, token, now=now) is False
    ts, mac = token.split(".")
    flipped = mac[:-1] + ("0" if mac[-1] != "0" else "1")
    assert L.check_confirm_token(SECRET, "LD-AAAAAAAAAA", commit, f"{ts}.{flipped}", now=now) is False
    old = L.confirm_token(SECRET, "LD-AAAAAAAAAA", commit, issued_at=now - L.CONFIRM_TOKEN_TTL_S - 60)
    assert L.check_confirm_token(SECRET, "LD-AAAAAAAAAA", commit, old, now=now) is False
    future = L.confirm_token(SECRET, "LD-AAAAAAAAAA", commit, issued_at=now + 3600)
    assert L.check_confirm_token(SECRET, "LD-AAAAAAAAAA", commit, future, now=now) is False
    for junk in ("", "abc", "1.2.3", "x.y", "123."):
        assert L.check_confirm_token(SECRET, "LD-AAAAAAAAAA", commit, junk, now=now) is False


def test_an_operator_token_is_scoped_to_one_listing():
    now = time.time()
    token = L.operator_token(SECRET, 7, issued_at=now)
    assert L.check_operator_token(SECRET, 7, token, now=now) is True
    assert L.check_operator_token(SECRET, 8, token, now=now) is False
    assert L.check_operator_token(OTHER, 7, token, now=now) is False
    stale = L.operator_token(SECRET, 7, issued_at=now - L.OPERATOR_TOKEN_TTL_S - 60)
    assert L.check_operator_token(SECRET, 7, stale, now=now) is False
    assert L.check_operator_token(SECRET, 7, token.replace("7.", "8.", 1), now=now) is False


# ── key, status, helpers ─────────────────────────────────────────────────

def test_the_ledger_key_fails_closed():
    assert L.ledger_secret({"DCHUB_LEAD_LEDGER_SECRET": "explicit", "JWT_SECRET": "j" * 40}) == b"explicit"
    derived = L.ledger_secret({"JWT_SECRET": "j" * 40})
    assert derived and derived != b"j" * 40 and derived == L.ledger_secret({"JWT_SECRET": "j" * 40})
    assert L.ledger_secret({"JWT_SECRET": "your-secret-change-in-production"}) is None
    assert L.ledger_secret({}) is None
    assert L.ledger_keys({}) == []


@pytest.mark.parametrize("events,expected", [
    ([], None),
    ([{"event": "intro_requested", "email_verified": False}], "pending_email_confirmation"),
    ([{"event": "intro_requested", "email_verified": True}], "registered"),
    ([{"event": "intro_requested"}, {"event": "email_confirmed"}], "registered"),
    ([{"event": "intro_requested"}, {"event": "email_confirmed"},
      {"event": "operator_notified"}, {"event": "introduced"}], "introduced"),
    ([{"event": "interest_registered"}, {"event": "email_confirmed"},
      {"event": "withdrawn"}, {"event": "introduced"}], "withdrawn"),
])
def test_lead_status_is_the_furthest_step_and_withdrawal_is_final(events, expected):
    assert L.lead_status(events) == expected


def test_display_helpers():
    assert L.mask_email("Jane.Doe@Acme.COM") == "J***@acme.com"
    assert L.mask_email("nope") is None and L.mask_email("@acme.com") is None
    assert L.email_domain("jane@Acme.com") == "acme.com"
    lead = L.new_lead_id()
    assert L.looks_like_lead_id(lead) and L.looks_like_lead_id(lead.lower())
    assert not L.looks_like_lead_id("LD-ILOU000000") and not L.looks_like_lead_id("LD-123")
