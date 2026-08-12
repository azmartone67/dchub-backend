"""tests/test_outreach_pii_probe_removed.py — recipient PII stays out of the
prompt (2026-08-12).

/api/v1/media/outreach-log answers:

    {"error": "admin only — media outreach log contains recipient PII",
     "hint": "send X-Admin-Key / X-Internal-Key or append ?admin_key="}

L14 and L8 both probed it WITHOUT a credential, so it returned 403 for their
entire lives. The bare-{} swallow rendered that as "no outreach activity", and
L8 published fabricated Nones (total_sent, replied, reply_rate_pct) as outreach
state. util/internal_fetch surfaced it.

The obvious fix — send X-Internal-Key, as routes/radar.py did on 2026-07-31 for
its own 402'd loopback call — would pipe recipient PII into the Claude prompt on
every tick and into the brain_llm_spend token ledger. The owner chose to DROP
the probe instead.

Guards:
  (1) RE-ADD — someone "fixes the 403" by restoring the probe, with or without a
      key. Either way PII risk returns or the fake-Nones return.
  (2) SILENT-GAP — the context key is deleted outright, so the model sees no
      outreach field and infers zero from silence. That is the exact failure
      this whole series exists to end, so the gap must be EXPLICIT.
  (3) CREDENTIAL-CREEP — an admin/internal key is attached to that path.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_outreach_pii_probe_removed.py -v
"""
from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PATH = "/api/v1/media/outreach-log"
_LAYERS = ("brain_layer14_causal.py", "brain_layer8_orchestrator.py")


def _src(mod: str) -> str:
    return (_ROOT / "routes" / mod).read_text(encoding="utf-8")


@pytest.mark.parametrize("mod", _LAYERS)
def test_the_path_appears_only_in_a_comment_or_the_not_read_marker(mod):
    """★This guard was VACUOUS on its first cut and a mutation proved it. It
    looked for `probe(` / `_internal(` on the same line, so re-adding L14's
    entry as a bare registry tuple —

        ("outreach", "/api/v1/media/outreach-log", 8),

    — sailed straight through: a tuple contains no call. That is the identical
    false-negative shape as phx_live's ternary swallow, committed inside the
    guard written to prevent this class.

    The rule is now positional, not syntactic: any NON-comment line mentioning
    the path must also carry the explicit "NOT READ" marker. A registry tuple, a
    call, an f-string URL — none of them can satisfy that by accident."""
    offenders = []
    for n, line in enumerate(_src(mod).splitlines(), 1):
        stripped = line.strip()
        if _PATH not in stripped or stripped.startswith("#"):
            continue
        if "NOT READ" in stripped:
            continue
        offenders.append("%s:%d  %s" % (mod, n, stripped))
    assert not offenders, (
        "the PII-gated outreach path is referenced outside a comment:\n  "
        + "\n  ".join(offenders)
        + "\nIt returns 403 without a credential, and adding one puts recipient "
          "PII into the Claude prompt. Removal was an owner decision.")


@pytest.mark.parametrize("mod", _LAYERS)
def test_no_credential_is_attached_to_that_path(mod):
    """★If the probe ever comes back WITH a key, recipient PII enters the Claude
    prompt. That is a decision, not a bug fix — it must not arrive quietly."""
    src = _src(mod)
    for cred in ("X-Internal-Key", "X-Admin-Key", "admin_key",
                 "DCHUB_INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        for line in src.splitlines():
            if cred in line and "outreach" in line.lower() \
                    and not line.strip().startswith("#"):
                raise AssertionError(
                    "%s attaches %s to the outreach path — that sends recipient "
                    "PII into the prompt:\n  %s" % (mod, cred, line.strip()))


def test_l8_states_the_gap_instead_of_hiding_it():
    """★A missing key reads as an oversight and the model infers zero from
    silence. The gap must be explicit and labelled."""
    src = _src("brain_layer8_orchestrator.py")
    assert '"outreach"' in src, \
        "L8 dropped the outreach key entirely — silence reads as zero"
    assert "NOT READ" in src, \
        "L8's outreach gap is no longer labelled as deliberate"
    assert "DELIBERATE GAP" in src, \
        "the model is no longer told this absence is not a measurement"


def test_l8_no_longer_publishes_fabricated_outreach_numbers():
    """total_sent / replied / reply_rate_pct were all None-from-403, presented
    as though measured."""
    src = _src("brain_layer8_orchestrator.py")
    for ghost in ("outreach.get(\"total\")", "outreach.get(\"replied\")",
                  "outreach.get(\"reply_rate_pct\")", "outreach.get(\"log\")"):
        assert ghost not in src, \
            "L8 is publishing %s again — a 403 rendered as a measurement" % ghost


def test_l8_docstrings_do_not_claim_outreach_is_read():
    """The docstring claimed the layer pulls the outreach log, which is how a
    permanently-403'd input passed for a working one for months."""
    src = _src("brain_layer8_orchestrator.py")
    head = src[:src.index("def ")] if "def " in src else src
    assert "+ outreach log +" not in head, \
        "module docstring still claims outreach is read"
