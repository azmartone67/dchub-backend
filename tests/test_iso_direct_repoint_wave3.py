"""tests/test_iso_direct_repoint_wave3.py — NYISO, SPP, AESO, MISO off four
keyless feeds.

The third repoint.

    mis.nyiso.com          isolf CSV, "NYISO" column       -> nyiso_load_forecast
    portal.spp.org         file-browser OP-MTLF csv        -> spp_load_forecast
    ets.aeso.ca            CSD report csv, DCR row         -> aeso_reserves
    public-api.misoenergy  MediumTermLoadForecast          -> miso_load_forecast

Probed and verified end to end 2026-09-03: NYISO 21,860 MW at 14:00 EPT (the
eleven zone columns reconcile to the total exactly) · SPP 51,550 MW MTLF
against a 50,774 MW actual · AESO 530 MW DCR = 490 gen + 40 other · MISO
117,546 MW for HE14, published 13:25 EST.

    before   direct 7  | parked 11 | reachable 10
    after    direct 11 | parked 7  | reachable 14

★ MISO WAS NOT BROKEN — IT MOVED. The DataBroker .asmx endpoints were retired
  2025-12-12 and still answer 200 with {"error":"no data"} — a success-shaped
  body carrying nothing, which reads as an upstream outage rather than a moved
  endpoint. The live feed is public-api.misoenergy.org, keyless.

★ as_of FOLLOWS WHAT THE NUMBER IS, and the FEED decides which rule applies.
  MISO publishes an instant, so its forecast is stamped when PUBLISHED (the
  ERCOT rule) with the target hour in `raw`. SPP and NYISO publish none, so
  they take the newest ELAPSED interval (the CAISO rule). AESO's DCR is an
  observation and carries its own instant. No row is ever ahead of the clock.

★ ONLY SPP HANDS US AN OFFSET (GMTIntervalEnd) and it is used verbatim. The
  other three are anchored on their OWN ISO's zone — never the server's, which
  is UTC in prod and UTC-7 on the dev laptop. MISO additionally REFUSES a RefId
  not marked EST rather than guessing through a changed convention.

House rules: no DB, no network, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_iso_direct_repoint_wave3.py -v
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect

import pytest

from routes import grid_data_master_shell as g

UTC = dt.timezone.utc
# 14:30 EPT (NYISO) · 13:30 CDT (SPP) · 12:30 MDT (AESO) · 13:30 EST (MISO)
NOW = dt.datetime(2026, 9, 3, 18, 30, tzinfo=UTC)
# The newest ELAPSED hourly interval — what NYISO/SPP/AESO stamp, since none of
# the three publishes an instant of its own.
SLOT = dt.datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
# MISO's RefId instant — what MISO stamps, because it DOES publish one.
PUB = dt.datetime(2026, 9, 3, 18, 25, tzinfo=UTC)


def _code_strings(mod):
    """Every string literal in `mod` that a CALL SITE could use.

    ★ Docstrings and block comments are ast.Constant strings too, so a naive
    AST walk still trips over documentation — the very trap the raw-text
    version had. Anything standing alone as its own statement is prose.
    """
    tree = ast.parse(inspect.getsource(mod))
    prose = set()
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant) \
                    and isinstance(child.value.value, str):
                prose.add(id(child.value))
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in prose}


def _ny(ts, total, capitl=1275, nyc=8278):
    return {"Time Stamp": ts, "Capitl": str(capitl), "N.Y.C.": str(nyc),
            "NYISO": str(total)}


# The newest ELAPSED slot is neither first nor last in iteration order, and the
# LAST row is a FUTURE slot. So "take the last row", "take the last elapsed row"
# and "take the newest overall" each land somewhere different from the answer.
NYISO_ROWS = [
    _ny("09/03/2026 15:00", 22500),        # future — 19:00 UTC
    _ny("09/03/2026 14:00", 21860),        # ← the answer, 18:00 UTC
    _ny("09/03/2026 13:00", 21000),        # last iterated
]

SPP_ROWS = [
    {"Interval": "09/03/2026 14:00:00", "GMTIntervalEnd": "09/03/2026 19:00:00",
     "MTLF": "53332", "Averaged Actual": "", "BAA": "SPP"},          # future
    {"Interval": "09/03/2026 13:00:00", "GMTIntervalEnd": "09/03/2026 18:00:00",
     "MTLF": "3191", "Averaged Actual": "3050", "BAA": "SWPW"},      # wrong BA
    {"Interval": "09/03/2026 13:00:00", "GMTIntervalEnd": "09/03/2026 18:00:00",
     "MTLF": "51550", "Averaged Actual": "50774", "BAA": "SPP"},     # ← answer
    {"Interval": "09/03/2026 12:00:00", "GMTIntervalEnd": "09/03/2026 17:00:00",
     "MTLF": "50000", "Averaged Actual": "49900", "BAA": "SPP"},     # last iterated
]

SPP_LISTING = [
    {"name": "OP-MTLF-202609031200.csv", "type": "file"},
    {"name": "OP-MTLF-202609031300.csv", "type": "file"},            # newest
    {"name": "OP-MTLF-202609031100.csv", "type": "file"},
]

AESO_CSV = (
    'Current Supply Demand Report\n'
    '\n'
    '"Last Update : Sep 03, 2026 12:00"\n'
    '\n'
    '"Alberta Total Net Generation","11461"\n'
    '"Alberta Internal Load (AIL)","10867"\n'
    '"Contingency Reserve Required","518"\n'
    '"Dispatched Contingency Reserve (DCR)","530"\n'
    '"Dispatched Contingency Reserve -Gen","490"\n'
    '"Dispatched Contingency Reserve -Other","40"\n'
    '\n'
    '"COGENERATION","6122","4195","0"\n'
)

MISO_LOAD = {
    "LoadInfo": {
        "RefId": "03-Sep-2026 - Interval 13:25 EST",
        "MediumTermLoadForecast": [
            {"Forecast": {"HourEnding": "13", "LoadForecast": "114492"}},   # elapsed
            {"Forecast": {"HourEnding": "14", "LoadForecast": "117546"}},   # ← answer
            {"Forecast": {"HourEnding": "24", "LoadForecast": "92334"}},    # far out
        ],
        "ClearedMW": [], "FiveMinTotalLoad": [],
    }
}


@pytest.fixture
def wave3(monkeypatch):
    def _csv(url, timeout=8):
        if "isolf" in url:
            return NYISO_ROWS
        if "mtlf-vs-actual" in url:
            return SPP_ROWS
        return []

    def _json(url, timeout=10):
        if "file-browser-api" in url:
            return SPP_LISTING
        if "RealTimeTotalLoad" in url:
            return MISO_LOAD
        return None

    monkeypatch.setattr(g, "_http_csv", _csv)
    monkeypatch.setattr(g, "_http_json", _json)
    monkeypatch.setattr(g, "_http_text", lambda *a, **k: AESO_CSV)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)


# ── timestamps: read where given, ISO-anchored where not ─────────────────

def test_spp_reads_the_offset_the_column_names():
    """GMTIntervalEnd says GMT, so the instant is read, never reconstructed."""
    assert g._spp_gmt("09/03/2026 18:00:00") == SLOT
    assert g._spp_gmt("09/10/2026 18:00:00") == dt.datetime(2026, 9, 10, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize("stamp,expect", [
    # 14:00 EPT is 18:00 UTC (EDT, UTC-4) — NOT 14:00, and NOT 21:00 (UTC-7 laptop)
    ("09/03/2026 14:00", dt.datetime(2026, 9, 3, 18, 0, tzinfo=UTC)),
    ("01/15/2026 14:00", dt.datetime(2026, 1, 15, 19, 0, tzinfo=UTC)),   # EST, UTC-5
])
def test_nyiso_anchors_on_new_york_not_the_server(stamp, expect):
    """★ The January case is load-bearing: a fixed-offset shortcut passes the
    September row and fails here, because EPT is EDT in summer and EST in
    winter. Reading either in the server's zone is the CAISO bug class."""
    assert g._nyiso_ts(stamp) == expect


@pytest.mark.parametrize("stamp,expect", [
    ("Sep 03, 2026 12:00", dt.datetime(2026, 9, 3, 18, 0, tzinfo=UTC)),   # MDT, UTC-6
    ("Jan 15, 2026 12:00", dt.datetime(2026, 1, 15, 19, 0, tzinfo=UTC)),  # MST, UTC-7
])
def test_aeso_anchors_on_edmonton_not_the_server(stamp, expect):
    assert g._aeso_ts(stamp) == expect


@pytest.mark.parametrize("bad", ["", None, "nope", "2026-09-03", "09/03/2026"])
def test_an_unparseable_stamp_is_refused_by_every_parser(bad):
    assert g._nyiso_ts(bad) is None
    assert g._aeso_ts(bad) is None
    assert g._spp_gmt(bad) is None


def test_miso_refid_yields_the_operating_day_and_the_publication_instant():
    day, pub = g._miso_refid("03-Sep-2026 - Interval 13:25 EST")
    assert day == dt.date(2026, 9, 3)
    assert pub == dt.datetime(2026, 9, 3, 18, 25, tzinfo=UTC)   # EST is UTC-5


@pytest.mark.parametrize("bad", [
    "03-Sep-2026 - Interval 13:25 CDT",   # ★ a CHANGED convention, not a typo
    "03-Sep-2026 - Interval 13:25 EDT",
    "03-Sep-2026 - Interval 13:25",       # no zone marker at all
    "03-Sep-2026", "", None, "Interval 13:25 EST",
    "03-Sep-2026 - Interval 25:00 EST",   # not an hour
])
def test_a_refid_that_is_not_est_is_refused_never_guessed(bad):
    """★ MISO publishes EST year-round. A CDT marker would mean the convention
    moved under us — guessing through it puts every summer row an hour off."""
    assert g._miso_refid(bad) == (None, None)


# ── the picker: newest ELAPSED, never the last row, never the future ──────

def test_nyiso_takes_the_newest_elapsed_slot_not_the_last_row(wave3):
    """★ REGRESSION GUARD. The fixture puts a FUTURE slot first and the oldest
    slot last, so 'take the last row', 'take the last elapsed row' and 'take
    the newest overall' each score differently from the answer."""
    out = g._nyiso_load_forecast({"id": "nyiso_load_forecast"})
    assert out["ok"]
    assert out["primary_value"] == 21860.0, (
        "%s — 22500 is the future slot, 21000 is the last row iterated"
        % out["primary_value"])
    assert out["as_of"] == SLOT


def test_spp_takes_the_newest_elapsed_interval_not_the_last_row(wave3):
    out = g._spp_load_forecast({"id": "spp_load_forecast"})
    assert out["ok"]
    assert out["primary_value"] == 51550.0, (
        "%s — 53332 is the future interval, 50000 is the last row iterated"
        % out["primary_value"])
    assert out["as_of"] == SLOT


def test_spp_filters_out_the_western_balancing_authority(wave3):
    """★ SWPW rides in the same file on its own rows and carries the SAME
    interval. Dropping the BAA filter makes it win the tie at 18:00 GMT and
    publishes 3,191 MW as the RTO-wide forecast; summing the two publishes a
    number SPP never states."""
    out = g._spp_load_forecast({"id": "spp_load_forecast"})
    assert out["primary_value"] == 51550.0
    assert out["primary_value"] != 3191.0
    assert out["primary_value"] != 51550.0 + 3191.0
    assert out["raw"]["baa"] == "SPP"


def test_spp_downloads_the_newest_file_in_the_listing(wave3):
    out = g._spp_load_forecast({"id": "spp_load_forecast"})
    assert out["raw"]["file"] == "OP-MTLF-202609031300.csv", out["raw"]["file"]


def test_nyiso_publishes_the_rto_total_not_our_sum_of_zones(wave3):
    """The zone columns are reported, never added into the primary — the number
    we publish has to be NYISO's own."""
    out = g._nyiso_load_forecast({"id": "nyiso_load_forecast"})
    assert out["primary_value"] == 21860.0
    assert out["raw"]["zone_sum_mw"] == 1275.0 + 8278.0
    assert out["primary_value"] != out["raw"]["zone_sum_mw"]
    assert "NYISO" not in out["raw"]["zone_mw"]


# ── the ISO-midnight fallback ────────────────────────────────────────────
#
# ★ Both files are named for their ISO's OWN day and both span several days
#   forward, so yesterday's drop still covers now. Without the fallback each
#   feed goes dark for the minutes between the ISO's midnight and the new file
#   appearing — a nightly refusal that looks exactly like an upstream outage.

# 21:00 EDT / 20:00 CDT on 03 Sep — the ISO day is still the 3rd while UTC has
# already rolled to the 4th. This is the ONLY window where "the ISO's own day"
# and "the server's day" name different files.
LATE = dt.datetime(2026, 9, 4, 1, 0, tzinfo=UTC)


def test_the_first_file_asked_for_is_named_in_the_ISOs_day_not_the_servers(monkeypatch):
    """★ The previous-day fallback MASKS this: a UTC-named request 404s and the
    fallback quietly rescues it, so the feed works while the anchor is wrong.
    That makes the fallback load-bearing for correctness instead of for the
    midnight gap it exists to cover — so the FIRST request is pinned, not just
    the answer.
    """
    ny, spp = [], []
    monkeypatch.setattr(g, "_utcnow", lambda: LATE)
    monkeypatch.setattr(g, "_http_csv",
                        lambda url, timeout=8: (ny.append(url) or NYISO_ROWS)
                        if "isolf" in url else SPP_ROWS)
    g._nyiso_load_forecast({"id": "nyiso_load_forecast"})
    assert ny[0].endswith("20260903isolf.csv"), (
        "%s — 20260904 is the SERVER's day; NYISO is still on the 3rd" % ny[0])

    monkeypatch.setattr(g, "_http_json",
                        lambda url, timeout=10: spp.append(url) or SPP_LISTING)
    g._spp_load_forecast({"id": "spp_load_forecast"})
    assert "2026%2F09%2F03" in spp[0], (
        "%s — 2026/09/04 is the SERVER's day; SPP is still on the 3rd" % spp[0])


def test_nyiso_falls_back_to_yesterdays_file_across_ny_midnight(monkeypatch):
    asked = []

    def _csv(url, timeout=8):
        asked.append(url)
        return [] if "20260903" in url else NYISO_ROWS   # today not posted yet

    monkeypatch.setattr(g, "_http_csv", _csv)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    out = g._nyiso_load_forecast({"id": "nyiso_load_forecast"})
    assert out["ok"] and out["primary_value"] == 21860.0
    assert [u.rsplit("/", 1)[-1] for u in asked] == [
        "20260903isolf.csv", "20260902isolf.csv"], asked
    assert out["raw"]["source_url"].endswith("20260902isolf.csv")


def test_spp_falls_back_to_yesterdays_folder_across_the_spp_day(monkeypatch):
    asked = []

    def _json(url, timeout=10):
        asked.append(url)
        return [] if "2026%2F09%2F03" in url else SPP_LISTING

    monkeypatch.setattr(g, "_http_json", _json)
    monkeypatch.setattr(g, "_http_csv", lambda *a, **k: SPP_ROWS)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    out = g._spp_load_forecast({"id": "spp_load_forecast"})
    assert out["ok"] and out["primary_value"] == 51550.0
    assert len(asked) == 2, asked
    assert "2026%2F09%2F03" in asked[0] and "2026%2F09%2F02" in asked[1]
    assert "2026%2F09%2F02" in out["raw"]["source_url"]


# ── forecast vs observation ──────────────────────────────────────────────

def test_miso_stamps_its_forecast_when_PUBLISHED_not_when_it_applies(wave3):
    """★ MISO hands us an instant, so it follows the ERCOT rule: as_of is the
    publication time and the target hour rides in `raw`. Stamping at the target
    writes a row ahead of the clock."""
    out = g._miso_load_forecast({"id": "miso_load_forecast"})
    assert out["ok"] and out["primary_value"] == 117546.0, (
        "%s — 114492 is HE13, which has already ELAPSED at 13:30 EST"
        % out["primary_value"])
    assert out["as_of"] == PUB
    assert out["as_of"] < NOW                    # published, so behind the clock
    assert out["raw"]["for_hour_utc"] == "2026-09-03 19:00:00+00:00"
    assert dt.datetime.fromisoformat(out["raw"]["for_hour_utc"]) > NOW
    assert out["raw"]["hour_ending_est"] == 14


def test_miso_hour_ending_24_is_next_day_midnight_not_hour_zero(monkeypatch):
    """★ HE24 ends at the NEXT day's 00:00 EST. Setting the hour instead of
    adding it puts the last hour of the operating day 24 hours early — and it
    would still look plausible, because 00:00 of the right date is a real
    instant."""
    monkeypatch.setattr(g, "_http_json", lambda *a, **k: {"LoadInfo": {
        "RefId": "03-Sep-2026 - Interval 23:30 EST",
        "MediumTermLoadForecast": [
            {"Forecast": {"HourEnding": "24", "LoadForecast": "92334"}}]}})
    monkeypatch.setattr(g, "_utcnow",
                        lambda: dt.datetime(2026, 9, 4, 4, 30, tzinfo=UTC))
    out = g._miso_load_forecast({"id": "miso_load_forecast"})
    assert out["ok"] and out["primary_value"] == 92334.0
    # 00:00 EST on 04 Sep == 05:00 UTC on 04 Sep — a day later than 00:00 on 03 Sep
    assert out["raw"]["for_hour_utc"] == "2026-09-04 05:00:00+00:00", out["raw"]
    assert out["as_of"] == dt.datetime(2026, 9, 4, 4, 30, tzinfo=UTC)


def test_aeso_reports_the_dispatched_total_not_the_split_or_the_requirement(wave3):
    """★ Four MW numbers sit next to each other in this report. The registry
    declares the DISPATCHED TOTAL; gen alone, other alone, and the REQUIRED
    figure are all plausible mis-reads and all wrong."""
    out = g._aeso_reserves({"id": "aeso_reserves"})
    assert out["ok"] and out["primary_value"] == 530.0
    for wrong in (490.0, 40.0, 518.0):
        assert out["primary_value"] != wrong
    assert out["raw"]["dcr_gen_mw"] == 490.0
    assert out["raw"]["dcr_other_mw"] == 40.0
    assert out["raw"]["contingency_reserve_required_mw"] == 518.0


def test_aeso_observation_carries_its_own_instant(wave3):
    out = g._aeso_reserves({"id": "aeso_reserves"})
    assert out["as_of"] == SLOT          # "Sep 03, 2026 12:00" Edmonton


def test_no_adapter_stamps_a_row_ahead_of_the_clock(wave3):
    """★ THE INVARIANT ALL FOUR SHARE. A row ahead of the clock breaks every
    freshness reader downstream, whichever as_of rule the feed earned."""
    for fn, did in (("_nyiso_load_forecast", "nyiso_load_forecast"),
                    ("_spp_load_forecast", "spp_load_forecast"),
                    ("_aeso_reserves", "aeso_reserves"),
                    ("_miso_load_forecast", "miso_load_forecast")):
        out = getattr(g, fn)({"id": did})
        assert out["ok"], out
        assert out["as_of"] <= NOW, "%s stamped %s, ahead of %s" % (did, out["as_of"], NOW)


# ── fail-soft ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", [
    "_nyiso_load_forecast", "_spp_load_forecast", "_aeso_reserves",
    "_miso_load_forecast",
])
def test_every_fetcher_fails_soft_when_its_upstream_is_down(monkeypatch, fn):
    monkeypatch.setattr(g, "_http_csv", lambda *a, **k: [])
    monkeypatch.setattr(g, "_http_json", lambda *a, **k: None)
    monkeypatch.setattr(g, "_http_text", lambda *a, **k: "")
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    out = getattr(g, fn)({"id": "x"})
    assert out["ok"] is False and out["error"]


def test_the_retired_miso_endpoint_shape_is_a_refusal_not_a_row(monkeypatch):
    """★ THE TRAP THAT HID THIS. The old .asmx endpoints answer 200 with
    {"error":"no data"} — success-shaped and empty. A fetcher that trusts a 200
    publishes nothing while reporting fine."""
    monkeypatch.setattr(g, "_http_json",
                        lambda *a, **k: {"error": "no data",
                                         "See": "https://www.misoenergy.org/"})
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    out = g._miso_load_forecast({"id": "miso_load_forecast"})
    assert out["ok"] is False and out["error"] == "miso_refid_not_est_dated"


def test_a_feed_with_only_future_rows_is_a_refusal_not_a_future_row(monkeypatch):
    """NYISO and SPP have no publication instant, so with nothing elapsed there
    is no honest stamp — refusing beats inventing one."""
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    monkeypatch.setattr(g, "_http_csv", lambda *a, **k: [_ny("09/03/2026 20:00", 24000)])
    assert g._nyiso_load_forecast({"id": "x"})["ok"] is False
    monkeypatch.setattr(g, "_http_json", lambda *a, **k: SPP_LISTING)
    monkeypatch.setattr(g, "_http_csv", lambda *a, **k: [SPP_ROWS[0]])
    assert g._spp_load_forecast({"id": "x"})["ok"] is False


def test_aeso_without_a_last_update_line_is_a_refusal(monkeypatch):
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    monkeypatch.setattr(g, "_http_text",
                        lambda *a, **k: '"Dispatched Contingency Reserve (DCR)","530"\n')
    out = g._aeso_reserves({"id": "aeso_reserves"})
    assert out["ok"] is False and out["error"] == "aeso_csd_unparseable_last_update"


# ── wiring, provenance, and the count ────────────────────────────────────

@pytest.mark.parametrize("did,label", [
    ("nyiso_load_forecast", "nyiso_mis_isolf"),
    ("spp_load_forecast", "spp_portal_mtlf"),
    ("aeso_reserves", "aeso_ets_csd"),
    ("miso_load_forecast", "miso_public_api"),
])
def test_each_dataset_is_registered_direct_with_its_own_provenance(did, label):
    """★ A row that names the wrong upstream is how a feed gets 'repointed' on
    paper and audited as still-gridstatus."""
    assert did in g._DIRECT_SOURCES
    got_label, fn = g._DIRECT_SOURCES[did]
    assert got_label == label and got_label != "gridstatus"
    assert callable(fn)
    assert did not in {t["id"] for t in g.parked_datasets()}


@pytest.mark.parametrize("did", [
    "nyiso_load_forecast", "spp_load_forecast", "aeso_reserves", "miso_load_forecast",
])
def test_each_dataset_routes_direct_through_the_dispatcher(monkeypatch, did):
    seen = {}
    monkeypatch.setattr(g, "_ingest_direct", lambda e: seen.setdefault("direct", e["id"]))
    monkeypatch.setattr(g, "_ingest_gridstatus_dataset",
                        lambda e: seen.setdefault("gridstatus", e["id"]))
    g._ingest_dataset({"id": did})
    assert seen == {"direct": did}, seen


def test_the_value_each_adapter_publishes_matches_the_column_the_registry_declares(wave3):
    """The registry names a value_col per dataset; `raw` has to carry it under a
    matching key, or the published primary and the declared column disagree."""
    for did, fn, key in (("nyiso_load_forecast", "_nyiso_load_forecast", "zone_mw"),
                         ("spp_load_forecast", "_spp_load_forecast", "load_forecast_mw"),
                         ("miso_load_forecast", "_miso_load_forecast", "load_forecast_mw"),
                         ("aeso_reserves", "_aeso_reserves",
                          "dispatched_contingency_reserve_total_mw")):
        entry = next(t for t in g.TARGET_DATASETS if t["id"] == did)
        out = getattr(g, fn)(entry)
        assert out["ok"] and key in out["raw"]
        if did == "aeso_reserves":
            assert entry["value_col"] == "dispatched_contingency_reserve_total"
            assert out["primary_value"] == out["raw"][key]
        elif did != "nyiso_load_forecast":
            assert entry["value_col"] == "load_forecast"
            assert out["primary_value"] == out["raw"][key]


def test_load_forecast_is_now_reachable_for_six_of_the_seven_us_isos():
    """★ Coverage stated as arithmetic over the registry, not a hand list — a
    static count is exactly what let the parked set rot for five weeks.

    Five of the six reach it DIRECT; PJM reaches it on the gridstatus allowlist,
    which is why this asks about reachability and not about _DIRECT_SOURCES.
    ISONE is the one that stays dark, and it needs a credential, not an adapter.
    """
    parked = {t["id"] for t in g.parked_datasets()}
    lf = {t["iso"] for t in g.TARGET_DATASETS if t["cat"] == "load_forecast"}
    dark = {t["iso"] for t in g.TARGET_DATASETS
            if t["cat"] == "load_forecast" and t["id"] in parked}
    assert dark == {"ISONE"}, sorted(dark)
    assert len(lf) == 7
    direct = {t["iso"] for t in g.TARGET_DATASETS
              if t["cat"] == "load_forecast" and t["id"] in g._DIRECT_SOURCES}
    assert direct == {"CAISO", "ERCOT", "MISO", "NYISO", "SPP"}


def test_the_parked_set_shrank_to_exactly_the_six_that_are_blocked():
    """★ Named because each of the six is blocked for a REASON that is not
    'nobody got to it': PJM ×2 and ISONE ×2 need owner-obtained credentials
    (services.pjm.com and webservices.iso-ne.com both answer 401), EIA-930 is
    mid-outage upstream, and MISO's margin has no multiday keyless source.

    ★ HENRY HUB LEFT THIS SET on 2026-09-03. It was never the EIA-930 outage —
      that is the hourly ELECTRIC grid monitor, a different product from the
      natural-gas v2 series, which never stopped publishing. It needed the key
      prod already held. See tests/test_eia_henry_hub_direct_repoint.py.
    """
    assert {t["id"] for t in g.parked_datasets()} == {
        "pjm_dispatched_reserves_verified", "pjm_marginal_emission_rates_5_min",
        "isone_lmp_real_time_5_min", "isone_load_forecast",
        "eia_co2_emissions",
        "miso_multiday_operating_margin",
    }
    assert len(g._DIRECT_SOURCES) == 12
    assert len(g.TARGET_DATASETS) - len(g.parked_datasets()) == 15


def test_the_finding_still_shrinks_by_arithmetic_and_names_the_new_four():
    _issue, detail = g._parked_finding()
    for did in ("nyiso_load_forecast", "spp_load_forecast",
                "aeso_reserves", "miso_load_forecast"):
        assert did in detail
    assert "6 of 21 registry datasets" in detail


def test_every_park_reason_names_a_real_registry_dataset():
    """★ ANTI-GHOST. A reason for a dataset that no longer exists is a claim
    nothing can refute — the same shape as the static gap list that hid this
    for five weeks. Keys are checked against the live registry, so a renamed or
    dropped dataset takes its explanation with it."""
    ids = {t["id"] for t in g.TARGET_DATASETS}
    assert set(g._PARK_REASON) <= ids, sorted(set(g._PARK_REASON) - ids)


def test_a_parked_dataset_with_no_reason_reports_UNCLASSIFIED(monkeypatch):
    """★ The blanket sentence this replaced said every parked dataset just
    needed repointing. That was true in July and false now — four of the seven
    need a credential nobody here holds. A NEW registry row must not inherit
    that kind of claim: absent a probed reason it says so, in the finding."""
    extra = dict(g.TARGET_DATASETS[0], id="zz_new_dataset_nobody_probed_yet")
    monkeypatch.setattr(g, "TARGET_DATASETS", list(g.TARGET_DATASETS) + [extra])
    _issue, detail = g._parked_finding()
    assert "zz_new_dataset_nobody_probed_yet [PJM] UNCLASSIFIED — needs a probe" in detail
    assert "UNCLASSIFIED (probe these): zz_new_dataset_nobody_probed_yet." in detail
    # ★ and it must not be COUNTED as explained — 6 of 7, never 7 of 7
    assert "6 of the 7 need something obtained" in detail, detail[:400]


def test_the_finding_states_a_blocker_for_each_of_the_six_that_remain():
    """Not 'they need repointing' — WHAT each one is waiting on, so a reader
    does not go hunting for an adapter that cannot be written."""
    _issue, detail = g._parked_finding()
    assert "6 of the 6 need something obtained" in detail
    for did, needle in (
            ("pjm_dispatched_reserves_verified", "Data Miner 2 key"),
            ("isone_load_forecast", "ISO-NE web-services account"),
            ("eia_co2_emissions", "EIA-930"),
            ("miso_multiday_operating_margin", "one operating day")):
        assert did in detail and needle in detail, did
    # ★ the repointed one must be GONE from the blocked list, not merely quieter
    assert "eia_henry_hub_natural_gas_spot_prices_daily" not in detail.split(
        "need something obtained")[1]
    assert "Widening the allowlist is NOT the fix" in detail
    # ★ the claim that went stale must be GONE, not merely joined by better text
    assert "free direct source we already hold credentials for" not in detail


def test_the_retired_miso_asmx_endpoint_is_not_called_anywhere():
    """★ KEYED ON THE AST, NOT ON RAW TEXT. The module's own comment explains
    that MISORTWDDataBroker was retired — a grep for it hits that explanation
    and fails on the documentation. String literals come from the AST, so only
    a real call site counts. This repo has now hit that trap five times."""
    literals = _code_strings(g)
    assert not [s for s in literals if "MISORTWDDataBroker" in s or ".asmx" in s]
    assert "https://public-api.misoenergy.org/api" in literals


def test_aeso_stays_on_http_because_its_tls_does_not_work():
    """★ ets.aeso.ca fails the TLS handshake outright (sslv3 alert handshake
    failure) from curl and Python alike. This is a public read-only report and
    no credential crosses the wire. 'Fixing' the scheme silently kills the feed,
    so the scheme is pinned here with the reason attached."""
    assert g._AESO_CSD.startswith("http://ets.aeso.ca/")
    remote = {s for s in _code_strings(g) if s.startswith("http://")
              and "127.0.0.1" not in s and "localhost" not in s}
    assert remote == {g._AESO_CSD}, (
        "a second plaintext REMOTE upstream appeared: %s"
        % sorted(remote - {g._AESO_CSD}))
