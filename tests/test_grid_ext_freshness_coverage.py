"""GUARD — no grid metric may be promoted to the payload without an age.

The defect (live 2026-08-08): main.py promoted NINE grid_ext_metrics categories
to top-level fields, only FOUR carried an `*_as_of`, and build_freshness_block
covered 7 payload fields. reserves, margin, capacity, emissions and
dc_load_queue_measured reached callers as bare numbers with no timestamp
anywhere in the response.

That matters because _grid_ext_metrics_for takes the LATEST row per category
with NO age bound, and those rows come from gridstatus.io — free tier 250
calls/MONTH, which returned 403 "Usage: 375, Limit: 250" on 2026-07-31. When
the feed stops, the last ingested row stays in grid_ext_metrics and the
endpoint keeps serving it as live telemetry on every US ISO, forever, with
nothing in the payload able to reveal it.

The structural fix is one table (EXT_PROMOTIONS) driving BOTH the promotion and
the freshness layers. These tests fence that: a promoted field with no
timestamp companion, or no layer, fails the build.

Pure — this module is import-safe by design (no Flask, no DB).
"""
import datetime
import os
import re

import pytest

from routes.grid_payload_freshness import (
    EXT_PROMOTIONS, FRESHNESS_LAYERS, LAYER_CADENCE, build_freshness_block,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime.datetime(2026, 8, 8, 12, 0)


# ── the structural guarantee ────────────────────────────────────────────────

def test_every_promoted_field_has_a_timestamp_companion():
    assert EXT_PROMOTIONS, "promotion table is empty"
    for cat, p in EXT_PROMOTIONS.items():
        assert p.as_of_field, f"{cat} promotes {p.field} with no *_as_of field"
        assert p.as_of_field != p.field


def test_every_promoted_field_has_a_freshness_layer():
    """THE regression: 9 promoted, 4 timestamped, 7 layers."""
    layers = {name for name, _key, _sla in FRESHNESS_LAYERS}
    for cat, p in EXT_PROMOTIONS.items():
        assert p.layer in layers, (
            f"{p.field} (category {cat}) is promoted to the payload but has no "
            f"freshness layer — a consumer cannot age it")


def test_all_nine_categories_are_still_promoted():
    """Coverage must not be 'fixed' by dropping fields."""
    assert set(EXT_PROMOTIONS) == {
        "load_forecast", "reserves", "margin", "capacity", "emissions",
        "lmp", "capacity_price", "dc_load_queue", "dc_load_queue_measured"}


def test_the_five_that_had_no_timestamp_now_do():
    for cat in ("reserves", "margin", "capacity", "emissions",
                "dc_load_queue_measured"):
        p = EXT_PROMOTIONS[cat]
        assert p.as_of_field and p.layer


def test_main_promotes_from_the_table_not_by_hand():
    """If main.py goes back to hand-written `if _ext.get(...)` blocks the two
    lists can drift apart again, which is the whole defect."""
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert code.strip(), "comment-stripping ate the whole file"
    assert "EXT_PROMOTIONS" in code, "main.py no longer promotes from the table"
    for legacy in ("out['operating_reserves_mw'] =",
                   "out['operating_margin_mw'] =",
                   "out['marginal_emissions_lb_mwh'] ="):
        assert legacy not in code, (
            f"hand-written promotion is back ({legacy}) — it is how a field "
            f"reaches the payload without a timestamp")


def test_every_layer_names_its_source_cadence():
    for name, _key, _sla in FRESHNESS_LAYERS:
        assert name in LAYER_CADENCE, f"layer {name} does not say what feeds it"


def test_the_gridstatus_cadence_is_stated_honestly():
    """The SLA has to reflect the real ingest rate — one dataset per daily tick
    over a ~22-entry registry — or `within_sla` is theatre."""
    c = LAYER_CADENCE["reserves"].lower()
    assert "gridstatus" in c and "one dataset per daily tick" in c
    assert "weeks" in c
    assert EXT_PROMOTIONS["reserves"].sla_hours >= 24 * 7


# ── behaviour ───────────────────────────────────────────────────────────────

def _payload(**over):
    p = {
        "demand_period": "2026-08-08T11",
        "generation_mix_period": "2026-08-08T06",
        "extended_metrics": {},
    }
    p.update(over)
    return p


def test_a_frozen_gridstatus_metric_is_aged_and_flagged_stale():
    """The exact scenario: the feed died, the row is months old, the number is
    still served. It must now come with an age and appear in stale_layers."""
    p = _payload(operating_reserves_mw=13303.0,
                 operating_reserves_as_of="2026-05-01T00:00:00Z")
    fb = build_freshness_block(p, now=NOW)
    assert "reserves" in fb["layers"]
    age_h = fb["layers"]["reserves"]["age_minutes"] / 60.0
    assert age_h > 2000, "a 99-day-old reading must report as such"
    assert fb["layers"]["reserves"]["within_sla"] is False
    assert any(s["layer"] == "reserves" for s in fb["stale_layers"])


def test_a_metric_with_no_timestamp_is_named_not_silently_omitted():
    """Skipping it is exactly how five fields went un-aged. If the value is in
    the payload and the age is unknown, say so."""
    p = _payload(operating_margin_mw=4200.0)          # no *_as_of at all
    fb = build_freshness_block(p, now=NOW)
    assert "margin" not in fb["layers"]
    unaged = {u["layer"] for u in fb.get("unaged_layers", [])}
    assert "margin" in unaged
    reason = next(u for u in fb["unaged_layers"] if u["layer"] == "margin")["reason"]
    assert "unknown age" in reason.lower()


def test_absent_metrics_produce_no_layer_and_no_noise():
    fb = build_freshness_block(_payload(), now=NOW)
    assert "reserves" not in fb["layers"]
    assert not fb.get("unaged_layers")


def test_the_timestamp_can_come_from_the_extended_metrics_row():
    """Promotion is skipped for lmp when a primary LMP already exists, so the
    layer must still find its age on the extended_metrics row."""
    p = _payload(extended_metrics={"emissions": {"value": 812.0,
                                                 "as_of": "2026-08-08T09:00:00Z"}})
    fb = build_freshness_block(p, now=NOW)
    assert fb["layers"]["emissions"]["age_minutes"] == 180


def test_a_fresh_metric_is_within_sla():
    p = _payload(lmp_usd_mwh=41.2, lmp_as_of="2026-08-08T11:30:00Z")
    fb = build_freshness_block(p, now=NOW)
    assert fb["layers"]["lmp"]["within_sla"] is True
    assert fb["stale_layers"] == []
    assert fb["within_sla_core"] is True


def test_core_sla_still_breaks_on_a_stale_core_layer():
    p = _payload(lmp_usd_mwh=41.2, lmp_as_of="2026-08-01T00:00:00Z")
    fb = build_freshness_block(p, now=NOW)
    assert fb["within_sla_core"] is False


def test_demand_and_fuel_mix_layers_survive_the_refactor():
    fb = build_freshness_block(_payload(), now=NOW)
    assert fb["layers"]["demand"]["age_minutes"] == 60
    assert fb["layers"]["fuel_mix"]["age_minutes"] == 360


def test_every_layer_carries_a_cadence_in_the_response():
    p = _payload(operating_reserves_mw=1.0,
                 operating_reserves_as_of="2026-08-08T11:00:00Z")
    fb = build_freshness_block(p, now=NOW)
    for name, entry in fb["layers"].items():
        assert entry.get("source_cadence"), f"{name} shipped without a cadence"
