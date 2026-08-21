#!/usr/bin/env python3
"""tests/test_evidence_status_convention.py — the convention has to be ON THE
WIRE, because a convention in a memo evaporates.

NO NETWORK, NO DB (the app boots with psycopg2 stubbed).

On 2026-08-17 seven AI partners reviewed DC Hub's telemetry and could not tell
our measurements from our interpretations, because both ship in the same shape.
Four successive wrong root-causes for the relay funnel went out and all seven
adopted each one verbatim, hardening "diagnosed" into "solved" into "fully
functional" inside one round. The observation — `human_acted == 0` — never
changed across all four. Only the unmarked story attached to it did.

The agreed fix was ChatGPT's three states, and the agreed DELIVERY was
Mistral's question answered: put it in the envelope, not a memo. The 08-17
handoff said so in as many words — "a convention in a memo evaporates, that is
the whole lesson of this session" — and then went into a memo. Measured
2026-08-21, four days later: the funnel payload contained neither "observed"
nor "hypothesis" anywhere. Nothing had shipped.

So these tests are about DELIVERY, not vocabulary. The interesting failure is
not "someone renamed a state", it is "the block quietly stopped being served
and nobody noticed for another four days".
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from routes.evidence_status import (  # noqa: E402
    EVIDENCE_STATUS_VOCABULARY,
    EvidenceStatusError,
    is_valid,
    stamp,
    vocabulary_block,
)

# ★★★ THE DELIVERY CHECK IS NOT IN THIS FILE, AND MUST NOT BE MOVED BACK.
#
# "Is the block actually on the wire" is the assertion that matters — it is the
# only one that would have caught the four days when this convention existed
# solely in a handoff document. It lives in scripts/app_contract_gate.py
# (section 5), which runs as its own required check.
#
# Two attempts to keep it here both failed, for two different reasons, and both
# are worth recording because the second is the real one:
#
#   1. Booting inline via app_contract_gate.boot() -> AttributeError:
#      'types.SimpleNamespace' object has no attribute 'app'. Other tests in
#      this suite replace sys.modules['main'] with a stub, so the import
#      returned that stub. Green alone, red behind ~8,900 siblings.
#   2. Booting in a clean subprocess dodged the pollution and hit the actual
#      constraint: ModuleNotFoundError: No module named 'dotenv'. THE UNIT-TEST
#      JOB INSTALLS LIGHT DEPENDENCIES AND CANNOT IMPORT main AT ALL. That is
#      *why* everything here stubs it. A test that cannot boot the app cannot
#      prove the app serves anything, and the only ways to make it pass here
#      are to skip on ImportError or to assert about source text — a vacuous
#      guard and a grep, which is the failure mode this whole convention exists
#      to prevent.
#
# What remains in this file is vocabulary hygiene: pure-data tests on the leaf
# module, which need no app and are correct to run in the light job.


def test_an_unstamped_value_is_not_claimed_as_measured():
    """The contract must forbid the default that caused the original failure."""
    contract = (vocabulary_block().get("contract") or "").lower()
    assert "unstamped" in contract and "never" in contract, (
        "the contract text must state that an unstamped value is unclassified "
        "and must NOT be read as observed — silently defaulting to 'measured' "
        "is precisely how an interpretation became a finding four times"
    )


def test_stamping_outside_the_vocabulary_raises():
    """No silent default. A typo must not publish a story as a measurement."""
    for bad in ("Observed", "OBSERVED", "confirmed", "true", "", None, "measured"):
        try:
            stamp(1, bad)
        except EvidenceStatusError:
            continue
        raise AssertionError(
            f"stamp(1, {bad!r}) was accepted. A mis-stamped claim is worse than "
            "an unstamped one: it is machine-readable and consumers propagate it "
            "without the hedging that surrounds prose."
        )


def test_a_valid_stamp_keeps_the_value_and_the_status():
    got = stamp([1, 2], "observed", note="counted directly")
    assert got["value"] == [1, 2]
    assert got["status"] == "observed"
    assert got["note"] == "counted directly"
    assert is_valid("verified") and not is_valid("Verified")


def test_the_canon_cannot_be_edited_by_a_consumer():
    """Copies out, same reason handoff_definition hands out copies: one renderer
    mutating what it shows must not redefine the vocabulary for every other
    request in the same worker process."""
    a = vocabulary_block()
    a["states"]["observed"] = "POISONED"
    a["states"]["invented"] = "nope"
    b = vocabulary_block()
    assert b["states"]["observed"] == EVIDENCE_STATUS_VOCABULARY["observed"]
    assert "invented" not in b["states"]


def test_verified_is_reserved_for_an_isolated_mechanism():
    """The word does the work; if its definition drifts the stamp means nothing."""
    assert "experiment" in EVIDENCE_STATUS_VOCABULARY["verified"].lower()
    assert "not experimentally confirmed" in EVIDENCE_STATUS_VOCABULARY["hypothesis"].lower()


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
