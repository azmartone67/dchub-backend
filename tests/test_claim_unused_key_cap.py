"""r-unused-key-cap (2026-09-04) — the claim endpoint published a limit it
did not enforce, and somebody checked.

POST /api/v1/keys/claim returns, in its own `rate_limit_note`:

    "The /api/v1/keys/claim endpoint is rate-limited to 1 key per IP per 24h."

It was not. Reuse is keyed on (client_name, ip) — deliberately, so several
agents on one host each get a key — and `client_name` is caller-supplied and
unvalidated. Incrementing a string minted keys without limit from one address.

★ MEASURED, NOT HYPOTHETICAL. On 2026-09-01, 4.43.13.119 minted TWELVE keys in
twenty-five minutes as client_name `pentest`, `pentest2` … `pentest12`, and
used none of them:

    16:50:58 pentest    16:55:27 pentest5   17:00:50 pentest9
    16:51:05 pentest2   16:55:37 pentest6   17:00:58 pentest10
    16:54:38 pentest3   16:55:48 pentest7   17:15:42 pentest11
    16:55:18 pentest4   16:55:52 pentest8   17:15:49 pentest12

That is a rate-limit probe. It succeeded, and the published contract said it
could not happen.

★ THE MULTI-AGENT FEATURE IS REAL AND MUST SURVIVE. In the same 30 days
152.55.177.123 minted 11 keys and used 11; 152.55.176.168 minted 10, used 10;
152.55.176.25 minted 8, used 8. A flat per-IP cap breaks exactly the callers
the feature exists for. So the cap counts UNUSED keys: every legitimate
repeat claimer in the window carried at most ONE unused key at a time.

WHAT THIS GUARD PINS:
  · the cap counts only UNUSED, UNBOUND, ACTIVE keys inside the reuse window;
  · it hands back a working key rather than refusing (nobody is locked out);
  · it fails OPEN, like the dedup check beside it;
  · the published note describes what is actually enforced.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SRC = open(os.path.join(os.path.dirname(__file__), "..",
                         "flask_mcp_endpoints.py"), encoding="utf-8").read()


def _cap_block():
    i = _SRC.find("r-unused-key-cap")
    assert i > 0, "the unused-key cap is gone — client_name enumeration is free again"
    j = _SRC.find("CARRY THE COUNTER FORWARD", i)
    return _SRC[i:j]


def test_the_cap_exists_and_is_configurable():
    blk = _cap_block()
    assert "_UNUSED_KEY_CAP" in blk
    assert re.search(r'_UNUSED_KEY_CAP\s*=\s*max\(\s*1\s*,\s*int\(\s*os\.environ\.get\(\s*"DCHUB_CLAIM_UNUSED_CAP"',
                     blk), "the cap must come from env with a >=1 floor"


def test_the_cap_counts_only_unused_unbound_active_keys_in_window():
    """Each clause is load-bearing. Drop `last_used_at IS NULL` and the cap
    hits the multi-agent deployments that USE all 10 of their keys."""
    blk = _cap_block()
    q = blk[blk.find("SELECT api_key"):blk.find("ORDER BY created_at DESC")]
    assert "last_used_at IS NULL" in q, (
        "the cap must count UNUSED keys only — without this it breaks "
        "152.55.176.168, which minted 10 keys and used all 10")
    assert "email IS NULL OR email = ''" in q, (
        "a key with an email is bound; binding is the documented way to lift "
        "the cap, so bound keys must not count toward it")
    assert "status = 'active'" in q, "a revoked key must not hold the cap down"
    assert "make_interval(hours =>" in q, "the cap must be windowed, not all-time"
    assert "'claim_api'" in q, "the cap applies to the claim door"


def test_it_hands_back_a_working_key_rather_than_refusing():
    """Nobody is locked out: over-claiming returns the newest unused key."""
    blk = _cap_block()
    assert "_unused[0][0]" in blk, "must return the NEWEST unused key"
    assert "_restamp_claim_session" in blk, (
        "the returned key must be re-pointed at the claiming session, like "
        "every other reuse branch")
    assert re.search(r"ok=True", blk), "the capped response must not be an error"
    assert "), 200" in blk, "must answer 200, not 429 — this is idempotence"
    assert "bind_endpoint" in blk, "must name the free way to lift the cap"


def test_the_cap_fails_open():
    """A failed count must claim through. Better an extra key than a broken
    agent — the same trade the dedup check above it makes."""
    blk = _cap_block()
    assert "except Exception" in blk, "the cap must not raise into the caller"
    tail = blk[blk.rfind("except Exception"):]
    assert "return" not in tail.split("\n\n")[0], (
        "the except branch must fall through to minting, never return an error")


def test_the_published_note_describes_what_is_enforced():
    """A published limit nobody enforces is worse than none: it is the
    sentence an auditor checks."""
    i = _SRC.find("rate_limit_note=(")
    assert i > 0
    note = _SRC[i:i + 1200]
    assert "1 key per IP" not in note, (
        "the note still claims a 1-key-per-IP limit the endpoint does not "
        "enforce — client_name enumeration walks straight past it")
    assert "client_name" in note, "the note must name the real reuse key"
    assert "never USED are" in note or "never used" in note.lower(), (
        "the note must describe the unused-key cap that IS enforced")
