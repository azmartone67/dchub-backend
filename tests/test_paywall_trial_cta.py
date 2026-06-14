"""Guard the auto-trial paywall CTA: surface a usable key with ACCURATE terms.

The MCP paywall auto-mints a trial key and leads its message with a
'retry with X-API-Key' CTA — the single highest-leverage retention lever
(key minted but never used → 0 conversions). This test guards two things the
fix established:

  1. The message must NOT re-introduce the old overpromise. The minted trial is
     15 calls/day (50 when email-bound) for 7 days — the block used to hardcode
     '200 calls/day, expires 30d', which broke trust the moment the cap/expiry
     bit. Terms must come from the minted object, not a hardcoded literal.
  2. A machine-readable retry_with field must be present so programmatic agents
     retry without parsing prose.

mcp_gatekeeper imports Flask/db at module load and the CI unit-tests job
installs only pytest, so we assert against the SOURCE rather than importing.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GK = os.path.join(ROOT, "mcp_gatekeeper.py")


def _src():
    return open(GK, encoding="utf-8").read()


def test_no_hardcoded_overpromise():
    src = _src()
    # The exact old overpromise string must be gone.
    assert "200 calls/day, expires 30d" not in src, (
        "auto-trial CTA must not hardcode '200 calls/day, expires 30d' — "
        "surface the real minted terms instead.")


def test_retry_with_field_present():
    src = _src()
    assert 'payload["retry_with"]' in src, (
        "paywall must expose a machine-readable retry_with field so agents "
        "can retry without parsing the natural-language message.")
    # The retry header is the canonical one agents must send.
    assert '"X-API-Key"' in src


def test_cta_uses_minted_terms_not_literals():
    src = _src()
    # The surfaced cap must come from the minted object, not a literal.
    assert 'auto_trial.get("daily_calls"' in src
    # The accurate email-bind upgrade CTA from the mint is appended.
    assert 'auto_trial.get("upgrade_cta")' in src
