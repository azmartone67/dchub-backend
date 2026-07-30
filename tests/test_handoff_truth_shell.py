"""Guards for master shell #44 — the two-artifact relay handoff.

Pure-function tests: token audience separation, schema, mint payload,
funnel definition v2, the relay view's non-binding contract, and the
conductor's read-only construction. No DB, no network, never imports main.

The defect this shell repairs, for the record: one single-use claim token
served both audiences — the gateway auto-redeemed it in median 0.85s, every
human click landed on 410, claim_page_opened_at fired 0x all-time, and that
structurally-blind zero fed the conclusion "the human buyer does not exist".
These tests hold the separation so it cannot quietly fuse back together.
"""
import time

import pytest

ct = pytest.importorskip("utils.claim_token")
hi = pytest.importorskip("routes.mcp_high_intent_claim")
sh = pytest.importorskip("routes.handoff_truth_master_shell")


# ── audience separation at the token layer ─────────────────────────────

def test_human_token_is_its_own_kind():
    tok = hi.sign_human_view_token("sess-abc", "get_market_intel")
    p = hi.verify_human_view_token(tok)
    assert p and p["session_id"] == "sess-abc" and p["tool"] == "get_market_intel"


def test_agent_token_never_verifies_as_human():
    """A claim token pasted into the human verifier must die at the kind
    check — otherwise the single-token failure rebuilds itself."""
    agent_tok = hi.sign_claim_token("sess-abc", "get_market_intel")
    assert hi.verify_human_view_token(agent_tok) is None


def test_human_token_outlives_the_claim_ttl():
    """The whole point of the second artifact: humans read on human time.
    A 2-day-old human token verifies; the same-age token is DEAD to the
    default (claim) verifier."""
    old_ts = int(time.time()) - 2 * 24 * 3600
    tok = hi.sign_human_view_token("sess-abc", "rank_markets", ts=old_ts)
    assert hi.verify_human_view_token(tok) is not None, \
        "human token must live 7 days"
    assert ct.verify_claim_token(tok) is None, \
        "the 24h default bound must still reject it — TTL is per-kind at the caller"


def test_human_token_expires_at_seven_days():
    tok = hi.sign_human_view_token(
        "sess-abc", "rank_markets", ts=int(time.time()) - 8 * 24 * 3600)
    assert hi.verify_human_view_token(tok) is None


def test_default_claim_verify_contract_unchanged():
    """max_age_s is additive — every existing caller keeps the 24h bound."""
    fresh = hi.sign_claim_token("sess-abc", "tool")
    assert ct.verify_claim_token(fresh) is not None
    stale = ct.sign_claim_token(session_id="sess-abc", extra="tool",
                                ts=int(time.time()) - 25 * 3600)
    assert ct.verify_claim_token(stale) is None


# ── audience separation at the route layer ─────────────────────────────

def _src():
    return open(hi.__file__, encoding="utf-8").read()


def test_all_three_doors_guard_the_audience():
    """/claim GET+POST bounce human tokens to /relay; /redeem 403s them.
    Source-level pin: the guard must exist at every verify site."""
    src = _src()
    assert src.count("== KIND_HUMAN_VIEW") >= 3, \
        "an audience guard was removed from one of the doors"
    assert "audience_mismatch" in src, "the redeem door lost its named 403"
    assert 'redirect(f"/relay/{token}"' in src, \
        "/claim no longer sends humans to their own page"


def test_relay_bounces_agent_tokens_back():
    assert 'redirect(f"/claim/{token}"' in _src(), \
        "an agent token pasted into /relay must go to its own door"


# ── the second artifact rides every mint response ──────────────────────

def test_every_mint_site_carries_the_human_fields():
    """Three return sites mint/reuse a claim; all three must ship human_url
    alongside — a reused claim with no human link is the old world back."""
    src = _src()
    assert src.count("**_human_handoff_fields(sid, tool)") == 3


def test_human_note_tells_the_agent_to_surface_the_link():
    """claim_page_opened_at fired 0x ALL-TIME — the old link never reached
    human eyes. A durable link fixes half of that; the note asking the agent
    to SHOW it fixes the other half. Lose the note, lose the instrument."""
    fields = hi._human_handoff_fields("sess-abc", "get_market_intel")
    assert fields["human_url"].startswith("https://dchub.cloud/relay/")
    note = fields["human_note"].upper()
    assert "SHOW" in note and "HUMAN" in note
    assert "single-use" in fields["human_note"], \
        "the note must keep the two artifacts' contracts distinct"


def test_human_tokens_are_stateless():
    """Two mints for the same (session, tool) yield different strings that
    both verify to the same row identity — no token storage, no burn state."""
    a = hi.sign_human_view_token("s1", "t1", ts=int(time.time()) - 10)
    b = hi.sign_human_view_token("s1", "t1")
    assert a != b
    pa, pb = hi.verify_human_view_token(a), hi.verify_human_view_token(b)
    assert (pa["session_id"], pa["tool"]) == (pb["session_id"], pb["tool"])


# ── schema ─────────────────────────────────────────────────────────────

def test_schema_gains_the_open_instrument_idempotently():
    s = hi._SCHEMA_SQL
    assert "ADD COLUMN IF NOT EXISTS human_view_first_opened_at" in s
    assert "ADD COLUMN IF NOT EXISTS human_view_opens" in s
    assert "ix_mhis_human_opened_at" in s


# ── the relay view's contract ──────────────────────────────────────────

def test_relay_view_binds_nothing():
    """View-only: no form, no POST handler, never cacheable, not indexable."""
    html = hi._render_relay_view("get_market_intel", 12, True)
    assert "<form" not in html.lower()
    assert "noindex" in html
    src = _src()
    assert '"/relay/<token>", methods=["GET"]' in src, \
        "/relay grew a non-GET method — the human page must stay side-effect-free"
    assert src.count("private, no-store") >= 2, \
        "relay responses must never be cached (the open IS the measurement)"


def test_relay_view_states_both_key_states_honestly():
    with_key = hi._render_relay_view("t", 5, True)
    without = hi._render_relay_view("t", 5, False)
    assert "already carries" in with_key
    assert "No key is attached yet" in without


# ── funnel definition v2 ───────────────────────────────────────────────

def test_funnel_declares_the_discontinuity():
    src = open(hi.__file__.replace("routes/mcp_high_intent_claim.py",
                                   "flask_mcp_endpoints.py"),
               encoding="utf-8").read()
    assert "human_view_first_opened_at is not null" in src, \
        "human_acted no longer reads the new instrument"
    assert '"definition_version": 2' in src
    assert "human_acted_legacy_claim_page" in src, \
        "v1's instrument must stay visible as a labelled legacy diagnostic"
    assert "0.85s" in src, "the changelog lost the structural cause"


# ── conductor: read-only by construction ───────────────────────────────

def test_shell_number_and_lanes():
    assert sh.SHELL_NUMBER == 44
    names = [f.__name__ for f in (sh._lane_a_instrument, sh._lane_b_pending_sends,
                                  sh._lane_c_in_flight, sh._lane_d_hygiene)]
    assert len(names) == 4


def test_shell_cannot_send():
    """Lane B reminds; it must be physically incapable of sending."""
    src = open(sh.__file__, encoding="utf-8").read()
    for banned in ("smtplib", "sendmail", "requests.post", "urlopen(req, data",
                   "notify_operator"):
        assert banned not in src, f"conductor gained send capability: {banned}"


def test_demand_verdict_is_time_gated():
    """A zero in week one must read ACCUMULATING, never as demand evidence —
    the contaminated conclusion this shell exists to prevent repeating."""
    assert sh.MIN_WEEKS_BEFORE_DEMAND_VERDICT >= 2
    src = open(sh.__file__, encoding="utf-8").read()
    assert "ACCUMULATING" in src
    assert "humans decline" in src, \
        "the verdict lost its don't-misread-the-early-zero warning"


def test_pending_sends_are_dated_and_static():
    for item in sh.PENDING_HUMAN_SENDS:
        assert item["artifact"].strip() and item["queued"].startswith("2026-")
