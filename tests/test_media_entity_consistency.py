"""Entity-scope consistency — 2026-07-17 wrong-stat fix (post 100292).

The published claim was "609 GW ... NESO's interconnection queue, 35% of
all US queued load" — NESO is the GB operator, and the 35% came from a
denominator that summed GB+US rows (609 / mixed-total), while the honest
US-vs-US number would have been 56%. The pairing scrambled because the
queue snapshot carries NO country field and the composer hardcoded 'US'.

Locked here:
  1. media_claim_verify.check_entity_scope — a non-US operator as the
     subject of a 'of all US queued load' sentence is a violation
     (wired as an always-on hard block in content_publisher and as a
     block in verify_claims).
  2. media_claim_verify.queue_share_clause — the share clause renders
     ONLY when scope is US, the ratio is sane, and the rounded pct
     recomputes within ±5%; otherwise the claim is dropped from prose.
  3. media_editorial._queue_lead_from_snapshot — scope-aware lead: US
     shares over US-only denominators; non-US operators get an honest
     region label and no share clause.

Pure functions only; never imports main.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

mcv = pytest.importorskip("routes.media_claim_verify")  # noqa: E402
med = pytest.importorskip("routes.media_editorial")  # noqa: E402


BAD_100292 = ("609 GW of requested load now sit in NESO's interconnection "
              "queue, 35% of all US queued load — a multi-year wait.")
GOOD_ERCOT = ("ERCOT's interconnection queue holds 427 GW of requested "
              "load, 38% of all US queued load.")


# ── 1. operator ↔ claimed-scope consistency ──────────────────────────

def test_neso_with_us_scope_claim_is_a_violation():
    v = mcv.check_entity_scope(BAD_100292)
    assert v and "NESO" in v[0] and "GB" in v[0]


def test_us_operator_with_us_scope_claim_is_fine():
    assert mcv.check_entity_scope(GOOD_ERCOT) == []


def test_non_us_operator_without_scope_claim_is_fine():
    # The honest non-US phrasing the fixed lead now produces.
    assert mcv.check_entity_scope(
        "Great Britain's NESO interconnection queue holds 609 GW of "
        "requested load.") == []


def test_transmission_queue_phrasing_also_checked():
    assert mcv.check_entity_scope(
        "IESO's transmission queue backlog is 12% of all US queued "
        "capacity this year.")


def test_unknown_operator_never_blocks():
    # Fail-open for operators the map doesn't know.
    assert mcv.check_entity_scope(
        "Fooland's interconnection queue holds 9 GW, 1% of all US queued "
        "load.") == []


def test_operator_scope_map_stays_in_lockstep_with_ingest():
    """Every ingestor in iso_queue_ingest.INGESTORS must have a scope entry —
    a NEW international feed without one recreates this bug silently.
    Source-scan (no import: the ingest module pulls heavy deps)."""
    import re
    src = open(os.path.join(ROOT, "routes", "iso_queue_ingest.py"),
               encoding="utf-8").read()
    m = re.search(r"INGESTORS\s*=\s*\{(.*?)\}", src, re.S)
    assert m, "INGESTORS registry not found"
    isos = re.findall(r'"([A-Z-]+)"\s*:', m.group(1))
    assert isos, "no ISO keys parsed from INGESTORS"
    for iso in isos:
        assert iso.lower() in mcv.OPERATOR_SCOPE, (
            f"{iso} writes iso_queue_snapshots but has no OPERATOR_SCOPE "
            "entry — its GW would be mislabeled in media prose")


def test_verify_claims_blocks_the_scramble():
    out = mcv.verify_claims(BAD_100292)
    assert out["ok"] is False
    assert any("entity-scope" in b for b in out["blocks"])


# ── 2. the share clause must recompute or drop ───────────────────────

def test_share_clause_renders_when_consistent():
    assert mcv.queue_share_clause(427, 1124, "US") == \
        ", 38% of all US queued load"


def test_share_clause_dropped_for_non_us_scope():
    assert mcv.queue_share_clause(609, 609, "GB") == ""


def test_share_clause_dropped_on_insane_ratio():
    assert mcv.queue_share_clause(609, 0, "US") == ""
    assert mcv.queue_share_clause(0, 1090, "US") == ""
    assert mcv.queue_share_clause(2000, 1090, "US") == ""  # numerator > total


def test_share_clause_dropped_when_rounding_breaks_5pct():
    # 3.4% rounds to 3 — an 11.8% relative error; the claim is dropped
    # rather than published loosely.
    assert mcv.queue_share_clause(37, 1090, "US") == ""


# ── 3. the scope-aware lead builder ──────────────────────────────────

SNAP_NESO_TOP = {
    "by_iso": [
        {"iso": "NESO",  "queued_load_total_gw": 609,
         "source_url": "https://neso.example"},
        {"iso": "ERCOT", "queued_load_total_gw": 427},
        {"iso": "PJM",   "queued_load_total_gw": 300},
        {"iso": "MISO",  "queued_load_total_gw": 240},
        {"iso": "CAISO", "queued_load_total_gw": 123},
    ],
    "totals": {"queued_load_gw": 1699},  # the OLD mixed denominator
}

SNAP_ERCOT_TOP = {
    "by_iso": [
        {"iso": "ERCOT", "queued_load_total_gw": 427},
        {"iso": "PJM",   "queued_load_total_gw": 300},
        {"iso": "MISO",  "queued_load_total_gw": 240},
        {"iso": "CAISO", "queued_load_total_gw": 157},
        {"iso": "NESO",  "queued_load_total_gw": 200},  # must NOT pollute
    ],
    "totals": {"queued_load_gw": 1324},
}


def test_neso_top_gets_region_label_and_no_us_share():
    lead = med._queue_lead_from_snapshot(SNAP_NESO_TOP)
    assert lead is not None
    h = lead["headline_number"]
    assert "Great Britain's NESO" in h
    assert "609 GW" in h
    assert "US queued load" not in h
    assert lead["queue_scope"] == "GB"
    # a GB lead's fixed prose must clear the always-on publish gate
    assert mcv.check_entity_scope(h) == []


def test_ercot_top_share_uses_us_only_denominator():
    lead = med._queue_lead_from_snapshot(SNAP_ERCOT_TOP)
    assert lead is not None
    h = lead["headline_number"]
    # US-only denominator = 427+300+240+157 = 1124 → 38%. The mixed
    # totals (1324 → 32%) must NOT be used.
    assert "38% of all US queued load" in h
    assert "32%" not in h
    assert lead["queue_scope"] == "US"
    assert lead["queue_scope_total_gw"] == 1124.0
    assert mcv.check_entity_scope(h) == []


def test_lead_headline_still_clears_the_number_gate():
    for snap in (SNAP_NESO_TOP, SNAP_ERCOT_TOP):
        lead = med._queue_lead_from_snapshot(snap)
        assert med.leads_with_number(lead["headline_number"])


def test_empty_snapshot_yields_no_lead():
    assert med._queue_lead_from_snapshot({}) is None
    assert med._queue_lead_from_snapshot({"by_iso": []}) is None
