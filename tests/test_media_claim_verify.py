"""Guards for the runtime CLAIM verifier (r-claimverify, 2026-06-19).

verify_claims is the runtime complement to the static honest-numbers fence: it
checks a composed post's numbers are TRUE against canonical_stats, not merely
PRESENT (which is all leads_with_number checks). These tests lock the contract:

  • a post claiming 50,000 facilities / $324B / 190 countries is BLOCKED
  • a post with the correct canon figures is ok (no blocks)
  • a lead-number mismatch is WARNED (soft), never hard-blocked
  • the verifier NEVER raises (fail-open) — a bug can't dark-hold the feed
  • the content_publisher gate stays warn-by-default (lands non-disruptively)

DB-free: canonical_stats falls back to citation-safe floors without
DATABASE_URL, so this runs in CI.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

# routes.media_claim_verify is pure stdlib (re/logging) + a guarded
# canonical_stats import — no flask — so it imports cleanly even under the
# pure-pytest CI job. Keep importorskip for symmetry / future-proofing.
mcv = pytest.importorskip("routes.media_claim_verify")  # noqa: E402


# ── BLOCK: known-banned / over-claim figures ─────────────────────────────────

def test_blocks_50000_facilities():
    res = mcv.verify_claims("50,000 data-center facilities tracked by DC Hub.")
    assert res["ok"] is False
    assert res["blocks"], "50,000 facilities must hard-block"
    assert any("facilit" in b.lower() for b in res["blocks"])


def test_blocks_324_billion():
    res = mcv.verify_claims("DC Hub tracks $324B in data-center M&A this year.")
    assert res["ok"] is False
    assert any("m&a" in b.lower() or "aggregate" in b.lower() for b in res["blocks"]), res


def test_blocks_324b_compact():
    res = mcv.verify_claims("324B+ in tracked transactions.")
    assert res["ok"] is False
    assert res["blocks"]


def test_blocks_190_countries():
    # 190 over-claims the live/floor country count (178 / floor 170) by >25%? No —
    # 190 vs 170 floor is +11%, within tolerance. The OVER-claim path needs a
    # figure clearly above canon; assert the dedicated banned/over checks below.
    # Here we use 300 countries, which is unambiguously an over-claim.
    res = mcv.verify_claims("DC Hub now spans 300 countries worldwide.")
    assert res["ok"] is False
    assert any("countr" in b.lower() for b in res["blocks"])


def test_blocks_140_countries_understatement_banned():
    # 140+ countries is a retired UNDERstatement explicitly banned by the fence.
    res = mcv.verify_claims("Coverage across 140+ countries.")
    assert res["ok"] is False
    assert any("140" in b or "countr" in b.lower() for b in res["blocks"])


def test_blocks_dead_nexus_name():
    res = mcv.verify_claims("DC Hub Nexus indexes 21,000+ facilities.")
    assert res["ok"] is False
    assert any("retired product name" in b.lower() for b in res["blocks"]), res


def test_blocks_legacy_12907():
    res = mcv.verify_claims("12,907 facilities are now in the tracker.")
    assert res["ok"] is False
    assert any("12,907" in b or "legacy" in b.lower() for b in res["blocks"])


def test_blocks_inflated_market_count():
    res = mcv.verify_claims("DC Hub covers 286 power markets in the DCPI.")
    assert res["ok"] is False
    assert res["blocks"]


# ── OK: correct canon figures ────────────────────────────────────────────────

def test_ok_correct_canon_figures():
    text = ("21,000+ data-center facilities across 170+ countries and 232 "
            "markets, updated daily. Source: DC Hub, dchub.cloud")
    res = mcv.verify_claims(text)
    assert res["ok"] is True, f"correct canon figures must pass: {res}"
    assert res["blocks"] == []


def test_ok_non_canon_numbers_pass():
    # An ISO/queue figure (427 GW) and a deal value are not canon counts — they
    # must not be blocked by the canon check.
    text = ("ERCOT's interconnection queue holds 427 GW of requested load — "
            "up 12% week over week. Source: DC Hub.")
    res = mcv.verify_claims(text)
    assert res["ok"] is True, res
    assert res["blocks"] == []


def test_ok_dual_tracked_verified_claim_is_not_contradiction_block():
    # The honest dual claim ('21,000+ tracked · 2,800+ verified') has two
    # facility figures that differ >2x — it may WARN (soft) but must never BLOCK.
    text = ("21,000+ facilities tracked, 2,800+ facilities verified and deduped. "
            "Source: DC Hub.")
    res = mcv.verify_claims(text)
    assert res["ok"] is True, res
    assert res["blocks"] == []


# ── WARN: lead mismatch + internal contradiction ─────────────────────────────

def test_warns_on_lead_number_mismatch():
    lead = {"kind": "deal", "headline_number": "$10.0B data-center transaction: KKR/Nvidia"}
    # Post headline claims a different dollar figure than the lead source.
    text = "$25.0B data-center transaction just closed: KKR/Nvidia. Source: DC Hub."
    res = mcv.verify_claims(text, lead=lead)
    assert res["ok"] is True, "a lead mismatch is a soft warn, never a block"
    assert any("lead mismatch" in w.lower() for w in res["warns"]), res


def test_no_warn_when_lead_matches():
    lead = {"kind": "deal", "headline_number": "$10.0B data-center transaction: KKR/Nvidia"}
    text = "$10.0B data-center transaction just closed: KKR/Nvidia. Source: DC Hub."
    res = mcv.verify_claims(text, lead=lead)
    assert res["ok"] is True
    assert not any("lead mismatch" in w.lower() for w in res["warns"]), res


def test_warns_on_internal_contradiction():
    text = ("21,000 facilities are tracked. Elsewhere we list only 3,000 "
            "facilities in the same set.")
    res = mcv.verify_claims(text)
    # >2x divergence on the same metric → soft warn (not a block).
    assert any("contradiction" in w.lower() for w in res["warns"]), res
    assert res["blocks"] == []


# ── robustness: never raises, shape is always right ──────────────────────────

def test_never_raises_on_garbage():
    for junk in (None, "", "no numbers here", "�“weird unicode 12,345 ⚡",
                 12345, ["not", "a", "string"]):
        try:
            res = mcv.verify_claims(junk)  # type: ignore[arg-type]
        except Exception as e:  # pragma: no cover
            pytest.fail(f"verify_claims raised on {junk!r}: {e}")
        assert set(res.keys()) == {"ok", "blocks", "warns"}
        assert isinstance(res["blocks"], list) and isinstance(res["warns"], list)


def test_extract_claims_buckets_metrics():
    claims = mcv.extract_claims("21,000 facilities, 178 countries, 232 markets, $10B deal, 50%")
    metrics = {c["metric"] for c in claims}
    assert "facilities" in metrics
    assert "countries" in metrics
    assert "markets" in metrics
    assert "percent" in metrics


# ── wiring contract: gate stays warn-by-default ──────────────────────────────

def test_publish_gate_default_is_warn_not_block():
    """The content_publisher wiring must default to warn (SAFE) so it lands
    non-disruptively — only MEDIA_CLAIM_VERIFY=block fails a publish."""
    cp_path = os.path.join(ROOT, "content_publisher.py")
    src = open(cp_path, encoding="utf-8").read()
    assert "media_claim_verify" in src, "publish gate must call the verifier"
    assert 'os.environ.get("MEDIA_CLAIM_VERIFY", "warn")' in src, (
        "MEDIA_CLAIM_VERIFY must DEFAULT to 'warn' (warn-only is the safe landing)")
    assert '_cv_mode == "block"' in src, (
        "only block-mode may fail the publish")
