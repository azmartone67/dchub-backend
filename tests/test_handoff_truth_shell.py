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
    assert "ADD COLUMN IF NOT EXISTS human_view_first_ua" in s, \
        "v3's probe-vs-person instrument lost its column"


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


# ── funnel definition v3 ───────────────────────────────────────────────

def _funnel_src():
    return open(hi.__file__.replace("routes/mcp_high_intent_claim.py",
                                    "flask_mcp_endpoints.py"),
                encoding="utf-8").read()


def test_funnel_declares_the_discontinuity():
    """INVARIANT, NOT VALUE (r-selftraffic-funnel, 2026-08-17).

    This asserted the literal `"definition_version": 2`, then `3`. Both times a
    legitimate redefinition had to edit the guard that existed to catch
    redefinitions — the guard failed for the version having CHANGED, which is
    the one thing that is always allowed. It caught no drift either time; it
    just made the author touch it.

    What actually matters is that the stage cannot be redefined SILENTLY: every
    version must carry a changelog entry explaining it, and each superseded
    instrument must stay published as a labelled diagnostic. Pin that, and the
    next bump passes on its merits or fails for a real reason.
    """
    import re
    src = _funnel_src()
    assert "human_view_first_opened_at is not null" in src, \
        "human_acted no longer reads the /relay open instrument"

    m = re.search(r'"definition_version":\s*(\d+)', src)
    assert m, "human_acted no longer declares a definition_version"
    version = int(m.group(1))
    assert version >= 3, "the two-artifact union (v3) was reverted"

    # Every version from 1..N must have its own changelog entry — a bump with no
    # explanation is exactly the silent redefinition this shell exists to block.
    for v in range(1, version + 1):
        assert re.search(r"\b%d:\s*[\"']" % v, src), \
            "definition_version %d has no changelog entry" % v

    assert "human_acted_legacy_claim_page" in src, \
        "v1's instrument must stay visible as a labelled legacy diagnostic"
    assert "human_acted_v2_all_view_opens" in src, \
        "v2's instrument must stay visible as a labelled legacy diagnostic"
    assert "0.85s" in src, "the changelog lost the structural cause"


def test_funnel_v3_reads_both_human_artifacts():
    """v2's blind eye: agents show humans /upgrade/h (for_your_human), whose
    opens land in relay_opens — a table v2 never read, so a real human click
    could not move the dashboard. v3 must union both artifacts."""
    src = _funnel_src()
    assert "relay_opens" in src, "the funnel stopped reading the /upgrade/h artifact"
    assert "ro.session_id = s.mcp_session_id" in src, \
        "the relay_opens join to the session lost its key"
    assert "for_your_human" in src, \
        "the definition no longer names the artifact agents actually surface"


def test_funnel_v3_probe_excludes_both_branches():
    """All 4 all-time /relay stamps and both all-time relay_opens rows were
    our own probes — an unfiltered union would relaunch the stage on fake
    opens. Both branches must carry the canonical real-UA verdict."""
    src = _funnel_src()
    assert "real_ua_predicate" in src
    assert 's.human_view_first_ua")' in src, \
        "the /relay branch lost its UA-instrument gate"
    assert 'ro.user_agent")' in src, \
        "the relay_opens branch lost its UA gate"
    assert "ro.session_id <> ''" in src, \
        "the blank-sid guard is gone — tokens mint with sid='' and " \
        "relay_opens carries a valid blank-sid probe row, so any blank-sid " \
        "session would flip to human_acted on our own probe"


def test_relay_probe_costume_is_in_the_canonical_families():
    """relay_opens' only valid all-time row is 'human-simulated/2.0' (our
    ops probe). The funnel excludes it via the SHARED families, not a local
    list — if the family disappears from the canonical predicate, the v3
    stage silently starts counting our own costume as a human."""
    dl = pytest.importorskip("mcp_calls_deloop")
    import re as _re
    m = _re.search(r"!~\*\s+'\((.+)\)'", dl.real_ua_predicate())
    assert m, "real_ua_predicate() no longer renders a !~* '(...)' regex"
    assert _re.search(m.group(1), "human-simulated/2.0", _re.I), \
        "'human-simulated' left the canonical UA families"


def test_relay_view_stamps_first_real_ua():
    """The /relay side of v3: relay_view must stamp human_view_first_ua with
    the first REAL-UA open (probes never occupy the slot), via the canonical
    predicate on the bound value — not a local UA list that can drift."""
    src = _src()
    assert "from mcp_calls_deloop import real_ua_predicate" in src
    assert 'real_ua_predicate("%s")' in src, \
        "the stamp no longer runs the canonical predicate on the incoming UA"
    assert "WHEN human_view_first_ua IS NULL" in src, \
        "the first-real-open-wins guard is gone"


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
