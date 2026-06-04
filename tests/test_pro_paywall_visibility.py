"""r48.0 (2026-05-27) — Pro paywall visibility regression test.

Why this exists: the user repeatedly reported logged-in Pro users seeing
"unlock" CTAs and `🔒` badges on Deal Autopsy, DCPI, and other paywall
pages. Each surface was independently gating, and the bug kept reappearing
on the next paywall page added.

This test verifies — at the API layer — that providing a valid Pro
Bearer token actually unlocks the gated fields. If any of these
endpoints stops respecting the Bearer header (e.g. someone refactors
the tier-gate and forgets one), CI fails.

To enable: set DCHUB_QA_PRO_TOKEN in CI to a known Pro user's JWT. The
QA user is `qa-pro@dchub.cloud` (manually provisioned). Without the env
var the test is SKIPPED — CI is permissive on machines that don't have
the credentials. The skip itself is logged so we can monitor whether
the QA token is in place.

Companion to:
  - tests/test_honest_numbers.py    (data-shape fences)
  - tests/test_canonical_fields.py  (frontend field-read lint)
  - tests/test_anchor_card_clickable.py (frontend click-region lint)
  - dchub-nav.js master tier module (the runtime fix)

Run locally:
    DCHUB_QA_PRO_TOKEN=<jwt> python -m pytest tests/test_pro_paywall_visibility.py -v
"""
from __future__ import annotations

import os
import json
import urllib.request

import pytest

ORIGIN = os.environ.get(
    "DCHUB_QA_ORIGIN",
    "https://dchub-backend-production.up.railway.app",
)
TOKEN = os.environ.get("DCHUB_QA_PRO_TOKEN", "").strip()


def _get(path: str, *, with_token: bool = True, timeout: int = 15) -> tuple[int, dict | str]:
    """GET a path. Returns (status, parsed_json or raw_text)."""
    url = ORIGIN.rstrip("/") + path
    headers = {"Accept": "application/json", "User-Agent": "dchub-qa-paywall-test"}
    if with_token and TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="ignore")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="ignore"))
        except (json.JSONDecodeError, OSError):
            return e.code, ""


@pytest.fixture(scope="module")
def pro_token():
    if not TOKEN:
        pytest.skip(
            "DCHUB_QA_PRO_TOKEN not set — skipping Pro-paywall visibility "
            "checks. Set it in CI to a valid Pro user's JWT to enable."
        )
    return TOKEN


# ─── Deal Autopsy ────────────────────────────────────────────────────────
def test_deal_autopsy_unlocks_autopsy_read_for_pro(pro_token):
    """A Pro user should see the `autopsy_read` field on every deal — that's
    the paid layer. Anon callers get the deal flow + verdict only."""
    status, data = _get("/api/v1/deal-autopsy")
    assert status == 200, f"Expected 200, got {status}: {data}"
    assert isinstance(data, dict)
    assert data.get("ok") is True, f"API returned ok=False: {data}"
    deals = data.get("deals") or []
    assert deals, "Deal autopsy returned no deals — sanity check failed"
    # Pro tier should NOT see the global locked envelope.
    assert "autopsy" not in data or not data["autopsy"].get("locked"), (
        f"Pro user received a locked autopsy envelope: {data.get('autopsy')}"
    )
    # At least SOME deals should carry the autopsy_read field.
    with_read = [d for d in deals if d.get("autopsy_read")]
    assert with_read, (
        f"No deals had `autopsy_read` populated — tier-gate appears to be "
        f"refusing Pro tokens. Sample deal keys: "
        f"{list(deals[0].keys()) if deals else 'none'}"
    )


# ─── DCPI scores list ────────────────────────────────────────────────────
def test_dcpi_scores_unlock_numeric_fields_for_pro(pro_token):
    """The /api/v1/dcpi/scores list paywalls the NUMERIC fields (composite,
    excess_power, constraint, time_to_power) for anon, but a Pro user must
    see them populated."""
    status, data = _get("/api/v1/dcpi/scores?limit=50")
    assert status == 200, f"Expected 200, got {status}"
    assert isinstance(data, dict)
    scores = data.get("scores") or []
    assert scores, "DCPI scores returned empty"
    # Should NOT be gated for Pro.
    assert not data.get("_gated"), f"Pro caller got _gated=True payload: {data.get('_upgrade_cta')}"
    # At least one row should have a real composite_score (not None).
    with_score = [s for s in scores if s.get("composite_score") is not None]
    assert with_score, (
        "All composite_score values are None — paywall masking is hitting "
        "the Pro caller. The numeric fields should be unmasked."
    )


# ─── DCGI scores ─────────────────────────────────────────────────────────
def test_dcgi_scores_unlock_numeric_fields_for_pro(pro_token):
    """Same as DCPI but for the gas index."""
    status, data = _get("/api/v1/dcgi/scores")
    assert status == 200, f"Expected 200, got {status}"
    assert isinstance(data, dict)
    states = data.get("states") or []
    assert states, "DCGI scores returned empty"
    assert not data.get("_gated"), f"Pro caller got _gated=True payload"
    with_score = [s for s in states if s.get("dcgi") is not None]
    assert with_score, "All dcgi values are None — DCGI gate is refusing Pro tokens"


# ─── Sunset header sanity ────────────────────────────────────────────────
def test_stats_endpoint_advertises_deprecated_fields():
    """/api/v1/stats should signal which fields are deprecated so
    integrations (and tests/test_canonical_fields.py) can keep tracking.
    No Pro token required — this is a documentation/contract check."""
    url = ORIGIN.rstrip("/") + "/api/v1/stats"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            header = r.headers.get("X-Deprecated-Fields", "")
    except Exception as e:
        pytest.skip(f"Could not reach {url}: {e}")
    assert header, (
        "X-Deprecated-Fields header missing. r48.0 added it on /api/v1/stats "
        "to advertise main_facilities, legacy_facilities_table, and "
        "countries as deprecated. If the header is gone, integrations have "
        "no runtime signal that they're reading legacy fields."
    )
    for canonical_hint in ("total_facilities", "total_countries"):
        assert canonical_hint in header, (
            f"X-Deprecated-Fields header doesn't mention `{canonical_hint}` "
            f"as canonical: {header!r}"
        )


if __name__ == "__main__":
    # Quick CLI mode for local checks: print verdict per test.
    import sys
    if not TOKEN:
        print("WARNING: DCHUB_QA_PRO_TOKEN not set; only the public Sunset-"
              "header check will run.")
    for fn_name in [
        "test_stats_endpoint_advertises_deprecated_fields",
        "test_deal_autopsy_unlocks_autopsy_read_for_pro",
        "test_dcpi_scores_unlock_numeric_fields_for_pro",
        "test_dcgi_scores_unlock_numeric_fields_for_pro",
    ]:
        try:
            fn = globals()[fn_name]
            # Some tests don't take pro_token; some do. Try both.
            try:
                fn()
            except TypeError:
                if not TOKEN:
                    print(f"SKIP: {fn_name} (no token)")
                    continue
                fn(TOKEN)
            print(f"OK:   {fn_name}")
        except AssertionError as e:
            print(f"FAIL: {fn_name}\n      {e}")
        except Exception as e:
            print(f"ERR:  {fn_name}\n      {type(e).__name__}: {e}")
