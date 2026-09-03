"""tests/test_board_tells_the_truth.py — four ways a finding was WRONG.

Not "the board had a backlog" — the board had a backlog of statements that were
not true. Each was verified live on 2026-09-03 and each is a different way a
detector can publish something nobody can act on:

  1. AN ADDRESS THAT CANNOT ROUTE. check_operator_profile_gap hand-rolled
     `name.lower().replace(" ", "-")`, so "Equinix, Inc." was filed as
     /operators/equinix,-inc. — 404 live. The route resolves through
     routes.operators._slugify, which yields /operators/equinix-inc — 200 live.
     Two slug rules for one URL space; the detector called neither.

         /operators/equinix-inc     200
         /operators/equinix,-inc.   404

  2. AN ADDRESS NOBODY CAN OPEN. check_report_content_drift bound the probe
     address and the actionable address to one name, so it filed
     "http://localhost:8080/api/v1/reports/monthly" as the thing an operator
     should go look at. Squasher row 305 sat in awaiting_ops pointing at
     loopback.

  3. A SENTENCE THAT CONTRADICTED ITSELF. check_cf_account_health printed
     `cached_requests` — a key routes/cf_analytics.py has never emitted, so a
     hardcoded 0 forever — over `total_requests`, the ACCOUNT-scope count
     truncated at limit:100 (live 673), while the rate beside it came from the
     ZONE-scope query (live denominator 1,803,470). It read "23.83% over last
     7d (0 cached / 673 total)". 0/673 is 0%.

  4. A PARSER THAT COULD NOT READ HALF ITS OWN SPEC. schema.org allows
     "@type" to be a string OR an array; the extractor's regex demanded a quote
     after the colon, so /vs — which declares ["CollectionPage","ItemList"] —
     was filed wrong_type on its nested ListItem/Organization.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_board_tells_the_truth.py -v
"""
from __future__ import annotations

import inspect
import re

import pytest

from routes import brain_consistency_radar as r
from routes import schema_org_saturation as sos


# ── 1. one slug rule, and it is the router's ─────────────────────────────

@pytest.mark.parametrize("name,slug", [
    ("Equinix, Inc.", "equinix-inc"),        # the board's 404, measured live
    ("TierPoint, LLC", "tierpoint-llc"),
    ("Equinix", "equinix"),
    ("Digital Realty", "digital-realty"),
])
def test_the_detector_mints_the_slug_the_router_actually_routes(name, slug):
    assert r._operator_slug(name) == slug


def test_the_slug_helper_defers_to_the_router_not_a_copy():
    """★ ANTI-DRIFT. routes.operators._slugify owns this rule. If the detector
    ever grows its own regex as the PRIMARY path, the two drift and the board
    goes back to publishing addresses that do not resolve."""
    from routes import operators as ops
    for n in ("Equinix, Inc.", "Iron Mountain Data Centers", "NTT Ltd."):
        assert r._operator_slug(n) == ops._slugify(n)


def test_the_old_broken_rule_is_gone():
    src = inspect.getsource(r.check_operator_profile_gap)
    assert "replace(' ', '-')" not in src and 'replace(" ", "-")' not in src, (
        "the space-only slug rule is what produced /operators/equinix,-inc.")
    assert "_operator_slug(name)" in src


@pytest.mark.parametrize("name", ["Equinix, Inc.", "TierPoint, LLC", "NTT Ltd."])
def test_a_minted_operator_url_can_never_contain_punctuation(name):
    """The property, not the examples: whatever the name, the filed URL must be
    something a browser can resolve."""
    url = "/operators/%s" % r._operator_slug(name)
    assert re.fullmatch(r"/operators/[a-z0-9-]+", url), url


# ── 2. a finding's address must be reachable by a human ──────────────────

def test_the_probe_stays_on_loopback_but_the_filed_url_does_not():
    src = inspect.getsource(r.check_report_content_drift)
    assert "for window, probe_url, url in [" in src, (
        "probe address and actionable address must be separate names")
    assert "_http_get(probe_url" in src, (
        "the PROBE must stay on loopback — it is fast and skips the CF edge")
    assert '"/api/v1/reports/monthly"' in src, (
        "the FILED url must be the site-relative path")


def test_no_finding_in_this_detector_can_record_a_loopback_address():
    """★ THE GENERAL GUARD. Reads the rendered `"url": url` sites rather than
    the literal, so a future edit that re-points them at the probe fails."""
    src = inspect.getsource(r.check_report_content_drift)
    body = src.split("for window, probe_url, url in [")[1]
    filed = body.split("]:", 1)[1]
    for bad in ("localhost", "127.0.0.1", ":8080"):
        assert bad not in filed, (
            "%r reached the part of the loop that files findings — an operator "
            "cannot open a loopback address" % bad)


# ── 3. the rate and its counts must come from one query ──────────────────

def _cf_finding(monkeypatch, payload):
    """Drive the real detector against a stubbed cf-analytics payload."""
    import requests

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return payload

    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    out = r.check_cf_account_health()
    return next((f for f in out if f["issue"] == "cf_cache_rate_low"), None)


# The live payload shape, measured 2026-09-03: the rate is ZONE-scope, and the
# account-scope total_requests sits beside it truncated at limit:100.
LIVE_CF = {"ok": True, "cache_rate_pct": 23.83,
           "zone_requests_7d": 1_803_470, "total_requests": 673}


def test_the_cf_detail_no_longer_prints_a_key_nobody_emits(monkeypatch):
    """Behaviour, not source text: render the finding and read the sentence."""
    f = _cf_finding(monkeypatch, LIVE_CF)
    assert f is not None
    assert "0 cached" not in f["detail"], (
        "cached_requests was never emitted by routes/cf_analytics.py, so the "
        "numerator rendered as a hardcoded 0 forever")
    assert "673" not in f["detail"] and f["count"] != 673, (
        "673 is the ACCOUNT-scope count truncated at limit:100 — not the "
        "denominator the 23.83%% rate was computed against")
    assert f["count"] == 1_803_470


def test_the_rendered_sentence_is_arithmetically_self_consistent(monkeypatch):
    """It used to read "23.83% over last 7d (0 cached / 673 total)". 0/673 is
    0%. Pull the two numbers back out of the rendered sentence and divide."""
    f = _cf_finding(monkeypatch, LIVE_CF)
    m = re.search(r"~([\d,]+) cached / ([\d,]+) zone requests", f["detail"])
    assert m, f["detail"]
    cached = int(m.group(1).replace(",", ""))
    total = int(m.group(2).replace(",", ""))
    assert abs((cached / total) * 100.0 - 23.83) < 0.01, (
        "the counts must agree with the rate printed beside them: %s" % f["detail"])
    assert cached == 429_767 and total == 1_803_470


def test_the_derived_count_is_marked_as_derived(monkeypatch):
    """The cached count is inferred from the rate, never measured. The ~ is the
    difference between a reading and an inference."""
    f = _cf_finding(monkeypatch, LIVE_CF)
    assert "~429,767 cached" in f["detail"], f["detail"]


def test_a_missing_zone_denominator_cannot_raise(monkeypatch):
    for z in (0, None):
        p = dict(LIVE_CF)
        p["zone_requests_7d"] = z
        f = _cf_finding(monkeypatch, p)
        assert f is not None and "0 zone requests" in f["detail"]


def test_the_threshold_is_deliberately_untouched():
    """The 25% cutoff is a human judgement (the site's steady state is
    23.8-25.0%, so it sits inside the noise band and re-files forever). This
    change fixes the REPORTING only — it must not silently retune the alarm."""
    assert "if cr < 25:" in inspect.getsource(r.check_cf_account_health)


# ── 4. read the whole spec, not half of it ───────────────────────────────

def test_array_valued_at_type_is_extracted():
    blk = '{"@type": ["CollectionPage", "ItemList"], "n": {"@type": "ListItem"}}'
    types = [m.group(1) for m in sos._TYPE_RE.finditer(blk)]
    for m in sos._TYPE_ARR_RE.finditer(blk):
        types.extend(sos._TYPE_STR_RE.findall(m.group(1)))
    assert "CollectionPage" in types and "ItemList" in types, (
        "the array form is half the spec — /vs was filed wrong_type on its "
        "nested ListItem because the top-level declaration was invisible")
    assert "ListItem" in types, "the scalar path must still work"


def test_the_scalar_form_is_untouched():
    blk = '{"@type": "Organization"}'
    assert [m.group(1) for m in sos._TYPE_RE.finditer(blk)] == ["Organization"]
    assert not sos._TYPE_ARR_RE.findall(blk)


def test_the_collector_uses_both_patterns():
    src = inspect.getsource(sos)
    body = src.split("for b in blocks:", 1)[1][:400]
    assert "_TYPE_RE.finditer" in body and "_TYPE_ARR_RE.finditer" in body


def test_a_malformed_array_does_not_raise():
    for blk in ('{"@type": []}', '{"@type": [}', '{"@type": ["a"'):
        out = []
        for m in sos._TYPE_ARR_RE.finditer(blk):
            out.extend(sos._TYPE_STR_RE.findall(m.group(1)))
        assert isinstance(out, list)
