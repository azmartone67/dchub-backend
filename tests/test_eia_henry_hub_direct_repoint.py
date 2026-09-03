"""tests/test_eia_henry_hub_direct_repoint.py — Henry Hub spot off EIA v2.

The fourth repoint, and the first that needed a credential rather than a feed.

    api.eia.gov/v2/natural-gas/pri/fut   series RNGWHHD, daily
        -> eia_henry_hub_natural_gas_spot_prices_daily

Probed and verified end to end 2026-09-03 against the live API with the
production key: $2.90/MMBtu for trading day 2026-09-01, stamped
2026-09-01T00:00:00+00:00.

    before   direct 11 | parked 7 | reachable 14
    after    direct 12 | parked 6 | reachable 15

★ IT WAS NEVER THE EIA-930 OUTAGE. The hand-off filed this behind the same
  upstream as eia_co2_emissions. EIA-930 is the hourly ELECTRIC grid monitor;
  Henry Hub spot is a NATURAL-GAS series on a different v2 route that never
  stopped publishing. Two products under one agency's name read as one outage,
  and the dataset sat parked behind an outage that did not apply to it.

★ THE ROUTE CARRIES THE FUTURES STRIP TOO. natural-gas/pri/fut serves the NYMEX
  contracts RNGC1..RNGC4 alongside the spot series, same product and process
  codes. The request facets to RNGWHHD, but the parser ALSO checks the series on
  every row, so a facet dropped or ignored upstream is a refusal rather than a
  futures contract quietly published as the spot price.

★ as_of IS THE TRADING DAY, NOT NOW. EIA publishes this as a date with no time —
  a settled daily observation — so it is anchored at the START of its own day.
  That cannot land ahead of the clock even on publication day, and it keeps the
  (dataset_id, as_of) upsert idempotent across re-ingests.

★ VALUE ARRIVES AS A STRING. The live API sends "2.9", not 2.9. The coercion is
  load-bearing: without it primary_value is a str and every numeric reader
  downstream either crashes or compares lexically.

★ THE KEY IS NEVER PERSISTED. `raw` is written into grid_ext_metrics as JSON, so
  source_url is the KEYLESS url and the api_key is appended at call time only.

House rules: no DB, no network, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_eia_henry_hub_direct_repoint.py -v
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import json

import pytest

from routes import grid_data_master_shell as g

UTC = dt.timezone.utc
DID = "eia_henry_hub_natural_gas_spot_prices_daily"
NOW = dt.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
# The newest ELAPSED trading day in the fixture, anchored at its own start.
DAY = dt.datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
KEY = "test_key_not_a_real_credential_000000000"


def _row(period, value, series="RNGWHHD"):
    """One row in the exact shape the live API returns (value as a STRING)."""
    return {"period": period, "duoarea": "RGC", "area-name": "NA",
            "product": "EPG0", "product-name": "Natural Gas",
            "process": "PS0", "process-name": "Spot Price",
            "series": series,
            "series-description":
                "Henry Hub Natural Gas Spot Price (Dollars per Million Btu)",
            "value": value, "units": "$/MMBTU"}


def _payload(rows, echo_key=KEY):
    """★ THE REAL BODY ECHOES THE REQUEST BACK — api_key INCLUDED. Confirmed
    against the live API 2026-09-03: the response carries request.params.api_key
    right next to the data. Every fixture carries that echo, so the "key is
    never stored" test is asserting about a body that ACTUALLY contains the key
    rather than passing because the fixture happened to be clean."""
    return {"warnings": [], "apiVersion": "2.1.9", "ExcelAddInVersion": "2.1.0",
            "request": {"command": "/v2/natural-gas/pri/fut/data/",
                        "params": {"api_key": echo_key, "frequency": "daily",
                                   "data": ["value"], "length": "10"}},
            "response": {"total": 7452, "dateFormat": "YYYY-MM-DD",
                         "frequency": "daily", "data": rows}}


# Captured verbatim from api.eia.gov on 2026-09-03, newest first.
LIVE_ROWS = [_row("2026-09-01", "2.9"), _row("2026-08-31", "2.9"),
             _row("2026-08-28", "2.82"), _row("2026-08-27", "2.89"),
             _row("2026-08-26", "2.81")]


@pytest.fixture
def entry():
    return next(t for t in g.TARGET_DATASETS if t["id"] == DID)


@pytest.fixture
def hh(monkeypatch):
    """Key present, clock pinned, upstream stubbed. Returns a setter + the
    captured request url so tests can assert on what was actually asked for."""
    seen: dict = {}

    def _install(rows_or_payload):
        def fake(url, timeout=10):
            seen["url"] = url
            seen["timeout"] = timeout
            return rows_or_payload
        monkeypatch.setattr(g, "_http_json", fake)

    monkeypatch.setenv("EIA_API_KEY", KEY)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    _install(_payload(list(LIVE_ROWS)))
    return type("HH", (), {"install": staticmethod(_install), "seen": seen})


# ── the payload as it actually arrives ───────────────────────────────────

def test_the_string_value_becomes_a_float(hh, entry):
    """★ The live API sends "2.9". A parser that trusted the JSON type would
    publish a str into a numeric column and compare prices lexically."""
    out = g._eia_henry_hub(entry)
    assert out["ok"] is True
    assert out["primary_value"] == 2.9
    assert isinstance(out["primary_value"], float)
    assert not isinstance(out["primary_value"], str)


def test_as_of_is_the_trading_day_at_its_own_start_aware_utc(hh, entry):
    out = g._eia_henry_hub(entry)
    assert out["as_of"] == DAY
    assert out["as_of"].tzinfo is not None
    assert out["as_of"].utcoffset() == dt.timedelta(0)
    assert out["raw"]["trading_day"] == "2026-09-01"


def test_as_of_is_the_observation_day_not_now(hh, entry):
    """A daily observation stamped 'now' would claim today's price is today's
    when the newest settled day is two days back."""
    out = g._eia_henry_hub(entry)
    assert out["as_of"] != NOW
    assert out["as_of"] < NOW


# ── the picker: newest ELAPSED, never the first row, never the future ────

def test_it_picks_the_newest_day_not_whatever_came_first(hh, entry):
    """The request asks for a desc sort; the parser does not TRUST it."""
    hh.install(_payload([_row("2026-08-26", "2.81"), _row("2026-09-01", "2.9"),
                         _row("2026-08-28", "2.82")]))
    out = g._eia_henry_hub(entry)
    assert out["as_of"] == DAY and out["primary_value"] == 2.9


def test_a_day_that_has_not_started_is_never_published(hh, entry):
    """★ NEVER AHEAD OF THE CLOCK. A future-dated row is skipped, and the
    newest ELAPSED day is published instead."""
    hh.install(_payload([_row("2026-09-05", "9.99"), _row("2026-09-04", "8.88")]
                        + list(LIVE_ROWS)))
    out = g._eia_henry_hub(entry)
    assert out["primary_value"] == 2.9
    assert out["as_of"] == DAY
    assert out["as_of"] <= NOW


def test_a_feed_of_only_future_days_is_a_refusal_not_a_future_row(hh, entry):
    hh.install(_payload([_row("2026-09-04", "8.88"), _row("2026-12-01", "9.99")]))
    out = g._eia_henry_hub(entry)
    assert out["ok"] is False
    assert out["error"] == "eia_henry_hub_no_usable_observation"


def test_todays_own_day_is_allowed_because_it_has_started(monkeypatch, entry):
    """The guard is 'has this day begun', not 'is this day over' — a row for
    today is legitimate the moment EIA publishes it."""
    monkeypatch.setenv("EIA_API_KEY", KEY)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    monkeypatch.setattr(g, "_http_json",
                        lambda url, timeout=10: _payload([_row("2026-09-03", "3.01")]))
    out = g._eia_henry_hub(entry)
    assert out["ok"] and out["as_of"] == dt.datetime(2026, 9, 3, tzinfo=UTC)


# ── the futures strip must never be published as spot ────────────────────

def test_a_futures_contract_is_never_published_as_the_spot_price(hh, entry):
    """★ natural-gas/pri/fut serves RNGC1..RNGC4 (the NYMEX strip) on the same
    product/process codes. If the series facet is ever dropped upstream, a
    contract price must NOT be stamped into the Henry Hub spot column."""
    hh.install(_payload([_row("2026-09-02", "3.44", series="RNGC1"),
                         _row("2026-09-02", "3.51", series="RNGC2")]
                        + list(LIVE_ROWS)))
    out = g._eia_henry_hub(entry)
    assert out["primary_value"] == 2.9
    assert out["raw"]["series"] == "RNGWHHD"
    assert out["as_of"] == DAY


def test_a_response_of_only_futures_rows_is_a_refusal(hh, entry):
    hh.install(_payload([_row("2026-09-01", "3.44", series="RNGC1"),
                         _row("2026-08-31", "3.51", series="RNGC4")]))
    out = g._eia_henry_hub(entry)
    assert out["ok"] is False
    assert out["error"] == "eia_henry_hub_no_usable_observation"


def test_the_request_asks_upstream_for_the_spot_series_and_a_desc_sort(hh, entry):
    g._eia_henry_hub(entry)
    url = hh.seen["url"]
    assert "facets[series][]=RNGWHHD" in url
    assert "frequency=daily" in url
    assert "sort[0][direction]=desc" in url


# ── the credential ───────────────────────────────────────────────────────

def test_a_missing_key_is_a_named_refusal_not_a_raise(monkeypatch, entry):
    """★ Without a key api.eia.gov answers 403 API_KEY_MISSING, which the JSON
    helper would flatten into an indistinguishable None. This says which."""
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setattr(g, "_http_json", lambda *a, **k:
                        pytest.fail("must not call upstream without a key"))
    out = g._eia_henry_hub(entry)
    assert out == {"ok": False, "error": "eia_api_key_absent"}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_counts_as_absent(monkeypatch, entry, blank):
    monkeypatch.setenv("EIA_API_KEY", blank)
    monkeypatch.setattr(g, "_http_json", lambda *a, **k:
                        pytest.fail("must not call upstream with a blank key"))
    assert g._eia_henry_hub(entry)["error"] == "eia_api_key_absent"


def test_the_key_is_read_at_call_time_not_frozen_at_import(monkeypatch, entry):
    """A module-level os.environ read would bake in whatever the process
    started with — and this shell is long-lived."""
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    assert g._eia_henry_hub(entry)["error"] == "eia_api_key_absent"
    monkeypatch.setenv("EIA_API_KEY", KEY)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    monkeypatch.setattr(g, "_http_json",
                        lambda url, timeout=10: _payload(list(LIVE_ROWS)))
    assert g._eia_henry_hub(entry)["ok"] is True


def test_the_key_is_sent_upstream_but_never_stored(hh, entry):
    """★ `raw` lands in grid_ext_metrics as JSON. A keyed source_url would
    persist a live credential into a table many readers can select.

    The fixture body CONTAINS the key (EIA echoes it in request.params), so
    this fails if the adapter ever widens `raw` to carry the upstream body.
    """
    out = g._eia_henry_hub(entry)
    assert KEY in json.dumps(_payload(list(LIVE_ROWS))), "fixture must carry the echo"
    assert "api_key=%s" % KEY in hh.seen["url"], "the key must reach upstream"
    blob = json.dumps(out["raw"])
    assert KEY not in blob
    assert "api_key" not in blob
    assert out["raw"]["source_url"] == g._EIA_HH_URL
    assert "api_key" not in g._EIA_HH_URL


def test_the_echoed_request_block_is_never_copied_into_raw(hh, entry):
    """★ THE REAL LEAK PATH. api.eia.gov returns request.params.api_key next to
    the data. An adapter that stashed the upstream body in `raw` "for
    provenance" would write a live credential into grid_ext_metrics on every
    tick. `raw` is built field by field from response.data only."""
    out = g._eia_henry_hub(entry)
    blob = json.dumps(out["raw"])
    assert KEY not in blob
    for leaked in ("request", "params", "apiVersion", "ExcelAddInVersion"):
        assert leaked not in blob, leaked


def test_no_error_string_can_carry_the_key(hh, entry):
    hh.install(None)
    out = g._eia_henry_hub(entry)
    assert out["ok"] is False and KEY not in json.dumps(out)


# ── fail-soft: a bad upstream must never break the tick ──────────────────

@pytest.mark.parametrize("body", [
    None, {}, {"response": None}, {"response": {}}, {"response": {"data": None}},
    {"response": {"data": []}}, {"data": []}, [], "not json at all", 0,
])
def test_every_malformed_body_is_a_refusal_never_a_raise(hh, entry, body):
    hh.install(body)
    out = g._eia_henry_hub(entry)
    assert out["ok"] is False and isinstance(out["error"], str)


@pytest.mark.parametrize("bad", [None, "", "   ", "n/a", "NA", True, [], {},
                                 "2.9.1"])
def test_a_row_whose_value_will_not_coerce_is_skipped(hh, entry, bad):
    """EIA publishes holiday gaps as nulls. A skipped row must not become a
    None price — the next usable day is published instead."""
    hh.install(_payload([_row("2026-09-02", bad)] + list(LIVE_ROWS)))
    out = g._eia_henry_hub(entry)
    assert out["ok"] is True
    assert out["primary_value"] == 2.9 and out["as_of"] == DAY


@pytest.mark.parametrize("bad", ["", None, "09/01/2026", "2026-09", "2026-13-01",
                                 "yesterday", 20260901, "2026-09-01T00:00:00"])
def test_a_row_whose_period_will_not_parse_is_skipped(hh, entry, bad):
    hh.install(_payload([_row(bad, "9.99")] + list(LIVE_ROWS)))
    out = g._eia_henry_hub(entry)
    assert out["ok"] is True and out["primary_value"] == 2.9


def test_a_non_dict_row_is_skipped_not_a_crash(hh, entry):
    hh.install(_payload(["nope", None, 7, []] + list(LIVE_ROWS)))
    out = g._eia_henry_hub(entry)
    assert out["ok"] is True and out["primary_value"] == 2.9


def test_an_upstream_that_raises_is_contained_by_the_dispatcher(monkeypatch, entry):
    """_ingest_direct owns the last-resort catch; the adapter still must not be
    the thing that breaks the tick."""
    monkeypatch.setenv("EIA_API_KEY", KEY)

    def boom(*a, **k):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(g, "_http_json", boom)
    monkeypatch.setattr(g, "_conn", lambda: None)
    got = g._ingest_direct(entry)
    assert got["ok"] is False
    assert got["error"] == "direct_fetch_raised:RuntimeError"


# ── the server's own zone must not reach the answer ──────────────────────

def test_the_stamp_is_utc_anchored_not_server_anchored(hh, entry):
    """The dev laptop is UTC-7 and prod is UTC. A naive datetime built from
    local time would move the trading day by a day near midnight."""
    out = g._eia_henry_hub(entry)
    assert out["as_of"] == DAY
    assert out["as_of"].tzinfo is not None
    # the helper alone, across the boundary a local-time build would break on
    assert g._eia_hh_day("2026-01-01") == dt.datetime(2026, 1, 1, tzinfo=UTC)
    assert g._eia_hh_day("2026-12-31") == dt.datetime(2026, 12, 31, tzinfo=UTC)


def test_the_adapter_never_calls_a_naive_clock(hh, entry):
    """★ AST-KEYED. datetime.now()/utcnow() without a tz gives a naive stamp
    that compares against an aware one by raising."""
    tree = ast.parse(inspect.getsource(g._eia_henry_hub))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "utcnow", "utcnow() is naive"
            if node.func.attr == "now":
                assert node.args or node.keywords, "now() with no tz is naive"


# ── wiring, provenance, and the count ────────────────────────────────────

def test_it_is_registered_direct_with_its_own_provenance():
    """★ A row that names the wrong upstream is how a feed gets 'repointed' on
    paper and audited as still-gridstatus."""
    assert DID in g._DIRECT_SOURCES
    label, fn = g._DIRECT_SOURCES[DID]
    assert label == "eia_v2_natural_gas"
    assert label != "gridstatus"
    assert callable(fn) and fn is g._eia_henry_hub


def test_it_routes_direct_through_the_dispatcher(monkeypatch):
    seen = {}
    monkeypatch.setattr(g, "_ingest_direct", lambda e: seen.setdefault("direct", e["id"]))
    monkeypatch.setattr(g, "_ingest_gridstatus_dataset",
                        lambda e: seen.setdefault("gridstatus", e["id"]))
    g._ingest_dataset({"id": DID})
    assert seen == {"direct": DID}, seen


def test_the_gridstatus_budget_was_not_widened_to_do_this():
    """★ The free tier is 250 req/month and is already over. Repointing is the
    fix; adding this id to the allowlist would have been the bug."""
    assert DID not in g._GS_ALLOWLIST


def test_the_published_value_matches_the_column_the_registry_declares(hh, entry):
    out = g._eia_henry_hub(entry)
    assert entry["value_col"] == "price"
    assert entry["unit"] == "$/MMBtu"
    assert entry["iso"] == "US" and entry["cat"] == "gas_price"
    assert out["raw"]["price_usd_per_mmbtu"] == out["primary_value"]


def test_it_is_no_longer_parked_and_carries_no_park_reason():
    assert DID not in {t["id"] for t in g.parked_datasets()}
    assert DID not in g._PARK_REASON


def test_the_finding_shrank_by_arithmetic_to_six(hh):
    """★ DERIVED, never restated. Nobody edited a count — parked_datasets()
    subtracts a registered direct source, so the finding shrinks on its own."""
    assert len(g._DIRECT_SOURCES) == 12
    assert len(g.parked_datasets()) == 6
    assert len(g.TARGET_DATASETS) - len(g.parked_datasets()) == 15
    _issue, detail = g._parked_finding()
    assert "6 of 21 registry datasets" in detail
    assert DID not in detail.split("need something obtained")[1]


def test_the_stale_eia_930_explanation_is_gone_for_this_dataset():
    """★ THE CLAIM THAT WAS WRONG. This was parked as if it shared the EIA-930
    outage with eia_co2_emissions. Different product, different route — the gas
    series never stopped. The reason must be GONE, not merely reworded."""
    _issue, detail = g._parked_finding()
    repointed, blocked = detail.split("need something obtained", 1)
    # it appears ONCE, in the repointed list — and never among the blockers
    assert DID in repointed and DID not in blocked
    assert "EIA-930" not in repointed
    # eia_co2_emissions keeps its 930 reason — that outage is real
    assert "eia_co2_emissions" in blocked and "EIA-930" in blocked
