#!/usr/bin/env python3
"""exclusive_listings is watched PER LISTING, and the exemption says so truthfully.

NO NETWORK, NO DB.

2026-09-16. Publishing each listing's `updated_at` as a sitemap `<lastmod>` is a
freshness claim, so scripts/dataset_inventory.py::check demands that some
freshness registry watch the table (NEW_TIER1_UNWATCHED). Neither registry can:

  * BOTH SCAN WHOLE TABLES. data_freshness_radar._max_ts_and_count runs
    `MAX(<col>) FROM <table>` with no predicate; infra_growth._LAYERS is the
    same (label, table, category, stale_days) shape. Liveness here is
    routes.exclusive_listings._LIVE_WHERE, and a withdrawn or expired listing
    keeps its updated_at — so a whole-table MAX reads `fresh` off exactly the
    rows the sitemap must not publish.
  * DORMANT AT ZERO WOULD PAGE A HUMAN. Capacity Source is deliberately dormant
    at zero live listings. With the table empty the radar returns `unknown`
    with "no usable timestamp" in its detail — the exact substring
    dchub_self_heal.fix_data_freshness_radar matches to escalate
    `data_source_missing`.
  * THE THRESHOLD IS PER-ROW. Both registries hold ONE scalar per entry; the
    real rule is _CADENCE_OVERDUE_DAYS against each listing's own
    verification.verified_at.

So the exemption is recorded in contracts/dataset_inventory_exceptions.json.
★ THIS FILE IS WHAT STOPS THAT EXEMPTION OUTLIVING ITS REASON. It pins that the
  per-listing watcher really works (an overdue listing IS flagged), that the
  dormant state really is quiet, and that the exemption is written in the shape
  the guard actually READS — the file previously carried an `allowed_unwatched`
  key that no code has ever loaded.

Run:  python3 -m pytest tests/test_listings_freshness_is_watched_per_listing.py -rEf
"""
import datetime as _dt
import json
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXC_PATH = ROOT / "contracts" / "dataset_inventory_exceptions.json"
TABLE = "exclusive_listings"
CODE = "NEW_TIER1_UNWATCHED"

NOW = _dt.datetime(2026, 9, 16, 12, 0, tzinfo=_dt.timezone.utc)


def _el():
    import routes.exclusive_listings as el
    return el


# ── 1. the exemption is in force, in the shape the guard READS ──────────────

def test_the_exemption_is_written_where_the_guard_actually_looks():
    """★ The file carried `allowed_unwatched: {}` — a key
    scripts/dataset_inventory.py::_exceptions() has never loaded. An exemption
    written there is not an exemption; the guard would still fail and the entry
    would read as deliberate. _exceptions() is executed here, not imitated."""
    from scripts.dataset_inventory import _exceptions
    assert CODE in _exceptions().get(TABLE, set()), (
        f"{TABLE}/{CODE} is not exempt as far as the guard's own reader is "
        f"concerned — check the key name in {EXC_PATH.name}")


def test_the_exemption_carries_a_reason_a_reviewer_can_judge():
    raw = json.loads(EXC_PATH.read_text(encoding="utf-8"))
    entry = next(e for e in raw["allowed"] if e.get("table") == TABLE)
    reason = entry.get("reason") or ""
    assert len(reason) > 400, "a one-line reason is not a judgeable reason"
    # The three load-bearing facts, each pinned below by a behavioural test.
    for token in ("_LIVE_WHERE", "_CADENCE_OVERDUE_DAYS", "dormant"):
        assert token in reason, f"the reason does not rest on {token}"
    assert "WHAT WOULD RETIRE THIS EXCEPTION" in reason, (
        "an exemption with no stated exit is a permanent hole")


def test_no_dead_key_is_left_looking_like_the_right_place():
    raw = json.loads(EXC_PATH.read_text(encoding="utf-8"))
    assert "allowed_unwatched" not in raw, (
        "the key no code reads is back; the next person will write their "
        "exemption into it and watch the guard fail anyway")


# ── 2. the per-listing watcher genuinely works ──────────────────────────────

def _verification(days_ago):
    d = (NOW - _dt.timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {"verified_at": d, "method": "operator_confirmed"}


@pytest.mark.parametrize("cadence,allowance", [
    ("real_time", 2), ("weekly", 9), ("monthly", 35)])
def test_an_overdue_listing_is_flagged_by_its_own_cadence(cadence, allowance):
    """★ THE WATCHER THAT REPLACES THE REGISTRY. One day past the cadence's
    allowance is overdue; one day inside it is not. A single whole-table SLA
    cannot express this — that is the whole argument for the exemption, so it
    has to be true."""
    el = _el()
    assert el._CADENCE_OVERDUE_DAYS[cadence] == allowance, (
        f"{cadence}'s allowance moved; the exemption's stated rule is stale")
    over = el._freshness(_verification(allowance + 1), NOW, cadence)
    assert over["overdue"] is True, f"{cadence} at {allowance + 1}d is not overdue"
    assert over["next_update_due"], "an overdue listing names no due date"
    under = el._freshness(_verification(allowance - 1), NOW, cadence)
    assert under["overdue"] is False, f"{cadence} at {allowance - 1}d flagged early"


def test_an_unverified_listing_is_not_silently_called_fresh():
    el = _el()
    out = el._freshness(None, NOW, "weekly")
    assert out["state"] == "unverified" and out["overdue"] is False


# ── 3. dormant at zero is quiet, and the registry route is not ──────────────

def test_zero_live_listings_raises_nothing_from_the_per_listing_watcher():
    """Dormant is the owner's chosen state, not a failure. There is no listing,
    so there is nothing to call overdue — the watcher is per listing, so an
    empty set of listings produces an empty set of findings."""
    el = _el()
    findings = [el._freshness(v, NOW, c) for v, c in []]
    assert findings == []
    # And the liveness rule itself still exists to define "no live listings".
    assert el._LIVE_WHERE and any("expires_at" in p for p in el._LIVE_WHERE)


def test_the_radar_route_would_escalate_that_same_dormant_state():
    """★ THE REASON OPTION 1 WAS REFUSED, EXECUTED RATHER THAN ASSERTED. An
    empty table drives _classify to `unknown` with a detail carrying "no usable
    timestamp", and that substring is what dchub_self_heal matches to raise
    data_source_missing. If this ever stops being true, the exemption should be
    revisited — so it fails here rather than sitting unexamined."""
    from routes.data_freshness_radar import _classify

    assert _classify(None, 336, has_table=True, has_ts=False) == "unknown"

    detail = (f"table '{TABLE}' present but no usable timestamp column "
              f"(tried: updated_at, created_at)")
    heal = (ROOT / "dchub_self_heal.py").read_text(encoding="utf-8")
    m = re.search(r'if\s+"no source table"\s+in\s+detail\s+or\s+'
                  r'"([^"]+)"\s+in\s+detail', heal)
    assert m, "the escalation branch moved; re-check what a dormant table does"
    assert m.group(1) in detail, (
        f"the radar's zero-row detail no longer matches the escalation "
        f"substring {m.group(1)!r} — option 1 may now be viable")


def test_neither_registry_can_express_a_row_predicate():
    """Both are (name, table(s), …, one scalar). Nowhere to put _LIVE_WHERE, so
    a registry entry would measure withdrawn and expired listings too."""
    from routes.data_freshness_radar import _DOMAINS
    from routes.infra_growth import _LAYERS

    for entry in _DOMAINS:
        assert len(entry) == 4 and isinstance(entry[3], int), entry
    for entry in _LAYERS:
        assert len(entry) == 4 and (entry[3] is None
                                    or isinstance(entry[3], int)), entry

    radar = (ROOT / "routes" / "data_freshness_radar.py").read_text(encoding="utf-8")
    assert re.search(r'MAX\(\{col\}::timestamptz\) FROM \{table\}"', radar), (
        "the radar's probe changed shape — if it now takes a WHERE clause, "
        "exclusive_listings can be registered and this exemption retired")

    assert TABLE not in {t for _n, tbls, _c, _s in _DOMAINS for t in tbls}
    assert TABLE not in {e[1] for e in _LAYERS}
