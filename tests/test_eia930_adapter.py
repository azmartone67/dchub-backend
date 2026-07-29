"""ws2 (2026-07-29): the ONE EIA-930 adapter — routes/eia930.py.

Locks in the five things that can silently break ingestion for 43 balancing
authorities plus PJM/MISO/TVA/BPA:

  1. the built URL is BYTE-IDENTICAL to the five hand-written copies it
     replaced. A drifting query string returns 0 rows with no exception and no
     log — exactly the failure that kept PJM at 0 rows for weeks.
  2. our code → EIA respondent divergences (APS→AZPS, BPA→BPAT, ERCOT→ERCO,
     CAISO→CISO, NYISO→NYIS, SPP→SWPP, ISONE→ISNE). Forwarding our own label
     returns an empty 200 for six of the seven ISOs.
  3. the api_key NEVER appears in anything the adapter returns. It rides in the
     query string and leaked through /extract once already
     (see tests/test_iso_common_helpers.py).
  4. no key → no request. A keyless fan-out is 43 guaranteed 403s.
  5. the hourly cache: a second call inside the TTL must not hit the network,
     and a FAILED call must not be cached for the full TTL.

Loaded with the same exec harness the sibling _iso_common tests use, so this
file never imports main.py, Flask or psycopg2. Nothing here runs at module
scope — a module-scope failure aborts collection for the whole session.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "eia930.py")
IC = os.path.join(ROOT, "routes", "_iso_common.py")

_BODY_OK = ('{"response": {"data": ['
            '{"period": "2026-07-29T02", "respondent": "AZPS", "fueltype": "NG",'
            ' "value": 2437, "value-units": "megawatthours"}]}}')


def _real_scrub_url():
    """Pull the REAL scrub_url out of routes/_iso_common.py, so the leak test
    exercises shipped redaction rather than a stub that always passes."""
    src = open(IC, encoding="utf-8").read()
    m = re.search(r"^def scrub_url\(.*?(?=^def |^class |\Z)", src,
                  re.DOTALL | re.MULTILINE)
    assert m, "scrub_url not found in routes/_iso_common.py"
    ns = {}
    exec(m.group(0), ns)
    return ns["scrub_url"]


def _load(stub_fetch=None, stub_parse=None):
    """Exec routes/eia930.py with its routes._iso_common import stripped, then
    inject the collaborators into the module namespace."""
    src = open(MOD, encoding="utf-8").read()
    stripped = re.sub(r"^from routes\._iso_common import \(.*?\)\n", "", src,
                      flags=re.MULTILINE | re.DOTALL)
    assert stripped != src, \
        "the routes._iso_common import block moved — fix this harness"
    ns = {"__name__": "eia930_under_test"}
    exec(compile(stripped, MOD, "exec"), ns)
    # Prove the module actually parsed into the functions we are about to test.
    # An empty/renamed module would otherwise pass every assertion below by
    # never being exercised.
    for name in ("resolve_respondent", "eia930_url", "fetch_eia930_ba",
                 "cache_stats", "_CACHE", "_NEG_TTL_S", "_ttl_s"):
        assert name in ns, f"routes/eia930.py does not define {name}"
    ns["scrub_url"] = _real_scrub_url()
    ns["fetch_first_working"] = stub_fetch or (lambda *a, **k: ("{}", "u"))
    ns["parse_eia_v2_fuel_mix"] = stub_parse or (lambda t, prefix="fuel_": {})
    ns["_CACHE"].clear()
    return ns


class _Key:
    """Set EIA_API_KEY for the duration of a block and restore it exactly."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("EIA_API_KEY")
        if self.value is None:
            os.environ.pop("EIA_API_KEY", None)
        else:
            os.environ["EIA_API_KEY"] = self.value
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("EIA_API_KEY", None)
        else:
            os.environ["EIA_API_KEY"] = self.old
        return False


# ── the URL, frozen ────────────────────────────────────────────────

def test_url_is_byte_identical_to_the_copies_it_replaced():
    """These five strings are what routes/iso_{tva,bpa,pjm,miso}.py and
    routes/eia_utility_bas.py sent before the refactor."""
    ns = _load()
    u = ns["eia930_url"]
    base = "https://api.eia.gov/v2/electricity/rto"
    tail = "&sort[0][column]=period&sort[0][direction]=desc&length=12"
    for code, respondent in [("TVA", "TVA"), ("BPA", "BPAT"), ("PJM", "PJM"),
                             ("MISO", "MISO"), ("AZPS", "AZPS")]:
        assert u(code, api_key="K") == (
            f"{base}/fuel-type-data/data/?api_key=K"
            f"&frequency=hourly&data[0]=value&facets[respondent][]={respondent}"
            f"{tail}"), code
    # routes/iso_tva.py:50 — the region-data variant, length=24
    assert u("TVA", dataset="region-data", length=24, api_key="K") == (
        f"{base}/region-data/data/?api_key=K"
        "&frequency=hourly&data[0]=value&facets[respondent][]=TVA"
        "&sort[0][column]=period&sort[0][direction]=desc&length=24")


def test_url_rejects_an_unknown_dataset():
    ns = _load()
    try:
        ns["eia930_url"]("TVA", dataset="totally-made-up")
    except ValueError:
        return
    raise AssertionError("an unknown dataset must not silently build a URL")


# ── the registry ───────────────────────────────────────────────────

def test_respondent_aliases_cover_the_known_divergences():
    """Our label != EIA's respondent for six of seven ISOs plus APS."""
    ns = _load()
    for ours, eia in [("APS", "AZPS"), ("BPA", "BPAT"), ("ERCOT", "ERCO"),
                      ("CAISO", "CISO"), ("NYISO", "NYIS"), ("SPP", "SWPP"),
                      ("ISONE", "ISNE"), ("ISO-NE", "ISNE"), ("iso ne", "ISNE")]:
        assert ns["resolve_respondent"](ours) == (eia, "alias"), ours


def test_identity_resolution_is_reported_as_a_guess():
    """42 of the 43 registered BAs have code == respondent. That is correct,
    but it is an assumption, so the basis must come back with it."""
    ns = _load()
    for same in ["TVA", "PJM", "MISO", "SOCO", "GCPD", "AZPS", "SRP", "SC"]:
        assert ns["resolve_respondent"](same) == (same, "identity"), same
    assert ns["resolve_respondent"]("") == (None, None)
    assert ns["resolve_respondent"](None) == (None, None)


# ── secrets ────────────────────────────────────────────────────────

def test_api_key_is_sent_but_never_returned():
    seen = {}

    def _fetch(urls, **kw):
        seen["url"] = urls[0]
        return _BODY_OK, urls[0]

    ns = _load(stub_fetch=_fetch,
               stub_parse=lambda t, prefix="fuel_": {
                   "fuel_ng": {"value": 2437.0, "unit": "megawatthours"}})
    with _Key("SENTINEL-EIA-KEY-9999"):
        res = ns["fetch_eia930_ba"]("APS")
    assert "SENTINEL-EIA-KEY-9999" in seen["url"], "the key must actually be sent"
    assert "SENTINEL-EIA-KEY-9999" not in repr(res)
    assert res["status"] == "ok"
    assert res["respondent"] == "AZPS"
    assert res["metrics"]["fuel_ng"]["value"] == 2437.0


def test_no_key_means_no_request():
    calls = []

    def _fetch(urls, **kw):
        calls.append(urls)
        return _BODY_OK, urls[0]

    ns = _load(stub_fetch=_fetch)
    with _Key(None):
        res = ns["fetch_eia930_ba"]("APS")
    assert res["status"] == "no_key"
    assert res["metrics"] == {}
    assert "EIA_API_KEY" in (res["error"] or "")
    assert calls == [], "made an HTTP call we know answers 403"


# ── honest basis fields ────────────────────────────────────────────

def test_observation_period_is_carried_out_of_the_envelope():
    """grid_data.timestamp is the INSERT time (DEFAULT NOW()), so the EIA
    observation hour is the only honest freshness basis we have."""
    ns = _load(stub_fetch=lambda urls, **kw: (_BODY_OK, urls[0]),
               stub_parse=lambda t, prefix="fuel_": {"fuel_ng": {"value": 1.0}})
    with _Key("K"):
        res = ns["fetch_eia930_ba"]("APS")
    assert res["period"] == "2026-07-29T02"
    assert res["rows_returned"] == 1
    assert res["truncated_risk"] is False


def test_full_page_of_rows_raises_truncation_risk():
    rows = ",".join(
        '{"period": "2026-07-29T02", "fueltype": "F%d", "value": %d}' % (i, i + 1)
        for i in range(3))
    ns = _load(stub_fetch=lambda urls, **kw: ('{"response": {"data": [%s]}}' % rows,
                                              urls[0]),
               stub_parse=lambda t, prefix="fuel_": {"fuel_f0": {"value": 1.0}})
    with _Key("K"):
        res = ns["fetch_eia930_ba"]("APS", length=3)
    assert res["rows_returned"] == 3
    assert res["truncated_risk"] is True, \
        "a full page means the newest hour may hold fuels we never saw"


def test_zero_metrics_reports_a_reason_not_a_verdict():
    ns = _load(stub_fetch=lambda urls, **kw: ('{"response": {"data": []}}', urls[0]),
               stub_parse=lambda t, prefix="fuel_": {})
    with _Key("K"):
        res = ns["fetch_eia930_ba"]("APS")
    assert res["status"] == "ok"
    assert res["metrics_count"] == 0
    assert res["no_metrics_reason"], "0 metrics must never be reported bare"
    assert "0 rows" in res["no_metrics_reason"]


# ── the cache ──────────────────────────────────────────────────────

def test_second_call_inside_the_ttl_does_not_refetch():
    n = {"calls": 0}

    def _fetch(urls, **kw):
        n["calls"] += 1
        return _BODY_OK, urls[0]

    ns = _load(stub_fetch=_fetch,
               stub_parse=lambda t, prefix="fuel_": {"fuel_ng": {"value": 2437.0}})
    with _Key("K"):
        a = ns["fetch_eia930_ba"]("APS")
        b = ns["fetch_eia930_ba"]("APS")
    assert n["calls"] == 1, "EIA-930 is hourly; a 15-min re-pull must be memoised"
    assert a["cached"] is False and b["cached"] is True
    assert b["metrics"] == a["metrics"]
    assert b["cache_age_s"] is not None


def test_cached_result_cannot_be_mutated_by_a_caller():
    ns = _load(stub_fetch=lambda urls, **kw: (_BODY_OK, urls[0]),
               stub_parse=lambda t, prefix="fuel_": {"fuel_ng": {"value": 1.0}})
    with _Key("K"):
        a = ns["fetch_eia930_ba"]("APS")
        a["metrics"]["fuel_ng"] = {"value": 999999.0}
        b = ns["fetch_eia930_ba"]("APS")
    assert b["metrics"]["fuel_ng"]["value"] == 1.0


def test_max_age_zero_bypasses_the_cache():
    n = {"calls": 0}

    def _fetch(urls, **kw):
        n["calls"] += 1
        return _BODY_OK, urls[0]

    ns = _load(stub_fetch=_fetch,
               stub_parse=lambda t, prefix="fuel_": {"fuel_ng": {"value": 1.0}})
    with _Key("K"):
        ns["fetch_eia930_ba"]("APS", max_age_s=0)
        ns["fetch_eia930_ba"]("APS", max_age_s=0)
    assert n["calls"] == 2


def test_separate_respondents_do_not_share_a_cache_entry():
    seen = []

    def _fetch(urls, **kw):
        seen.append(urls[0])
        return _BODY_OK, urls[0]

    ns = _load(stub_fetch=_fetch,
               stub_parse=lambda t, prefix="fuel_": {"fuel_ng": {"value": 1.0}})
    with _Key("K"):
        ns["fetch_eia930_ba"]("APS")
        ns["fetch_eia930_ba"]("SRP")
    assert len(seen) == 2
    assert "AZPS" in seen[0] and "SRP" in seen[1]


def test_a_failure_is_negative_cached_only_briefly():
    """A long negative TTL would hide an EIA outage until it healed itself."""

    def _boom(urls, **kw):
        raise RuntimeError("all URLs failed; last=HTTP 503")

    ns = _load(stub_fetch=_boom)
    with _Key("K"):
        res = ns["fetch_eia930_ba"]("APS")
    assert res["status"] == "error"
    assert res["metrics"] == {}
    assert "RuntimeError" in res["error"]
    entries = list(ns["_CACHE"].values())
    assert len(entries) == 1
    held = entries[0]["expires"] - entries[0]["stored_at"]
    assert held == ns["_NEG_TTL_S"]
    assert ns["_NEG_TTL_S"] < ns["_ttl_s"](), \
        "a failure must expire sooner than a good reading"
    assert ns["_NEG_TTL_S"] < 900, \
        "must expire before the next 15-min data-pulse tick"


def test_a_failure_never_serves_a_stale_success():
    state = {"fail": False}

    def _fetch(urls, **kw):
        if state["fail"]:
            raise RuntimeError("upstream down")
        return _BODY_OK, urls[0]

    ns = _load(stub_fetch=_fetch,
               stub_parse=lambda t, prefix="fuel_": {"fuel_ng": {"value": 1.0}})
    with _Key("K"):
        ok = ns["fetch_eia930_ba"]("APS")
        assert ok["metrics"]
        ns["_CACHE"].clear()          # simulate the entry having expired
        state["fail"] = True
        bad = ns["fetch_eia930_ba"]("APS")
    assert bad["status"] == "error"
    assert bad["metrics"] == {}, "serving the old reading would fabricate freshness"


def test_cache_stats_names_its_own_scope():
    ns = _load()
    s = ns["cache_stats"]()
    assert "per-process" in s["scope"]
    assert s["entries"] == 0
    assert s["oldest_entry_age_s"] is None, \
        "no entries must report null, not a number derived from nothing"
