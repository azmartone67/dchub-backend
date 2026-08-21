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

def _funnel_payload() -> dict:
    """Fetch the funnel envelope from the real app, in a CLEAN SUBPROCESS.

    ★ Why a subprocess and not `gate.boot()` inline. The first version of this
    test booted the app lazily in the shared interpreter. It passed alone and
    FAILED in CI with:

        AttributeError: 'types.SimpleNamespace' object has no attribute 'app'

    Another test in the suite had already replaced `sys.modules['main']` with a
    stub, so `import main` inside boot() returned that stub. The test was
    order-dependent: green in isolation, red behind ~8,900 siblings.

    Skipping when `main` is already stubbed would have been the easy fix and
    the wrong one — it would make the ONE test that proves delivery silently
    vacuous in exactly the run that matters. A fresh interpreter is immune to
    whatever the suite has done to sys.modules, and still exercises the real
    application.
    """
    import json
    import subprocess

    code = (
        "import sys, json, os\n"
        "sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
        "import app_contract_gate as gate\n"
        "app, _ = gate.boot()\n"
        "r = app.test_client().get('/api/v1/mcp/handoff-funnel')\n"
        "sys.stderr.write('PAYLOAD:' + json.dumps(r.get_json() or {}) + '\\n')\n"
        "os._exit(0)\n"
    ) % (os.path.join(ROOT, "scripts"), ROOT)

    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=300, cwd=ROOT)
    marker = "PAYLOAD:"
    line = next((l for l in p.stderr.splitlines() if l.startswith(marker)), None)
    assert line, (
        "the app did not boot or the funnel did not answer; stderr tail:\n"
        + "\n".join(p.stderr.splitlines()[-12:])
    )
    return json.loads(line[len(marker):])


def test_the_envelope_actually_carries_the_convention():
    """★ THE TEST THAT MATTERS. Everything else here is vocabulary hygiene.

    Fetched from the real app, not asserted about source. This is the only
    check that would have caught the four days where the convention existed
    solely in a handoff document."""
    payload = _funnel_payload()
    ev = payload.get("evidence_status")
    assert ev, (
        "the funnel envelope carries no `evidence_status` block. Seven partners "
        "agreed this convention on 2026-08-17 and it reached nothing for four "
        "days — that is the regression this test exists for."
    )
    states = ev.get("states") or {}
    assert set(states) == {"observed", "hypothesis", "verified"}, (
        f"published states are {sorted(states)} — partners consume these "
        "literally; changing the set silently breaks the shared contract"
    )
    assert ev.get("contract"), "the block must say how to read an UNSTAMPED value"


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
