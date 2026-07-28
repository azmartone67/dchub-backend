"""moat_live_telemetry copy must be TRUE — 2026-07-28.

PR #1824 fixed the scorer so this card stops being silently refused (it added
a coverage_ratio pattern; the card went 0.550 -> 1.000). Correct fix. But it
left the copy alone, and the copy was wrong — so the effect of #1824 was to
make a FALSE capability claim publishable. Verified live against prod on
2026-07-28, the same day:

  • "7 of 7 US ISOs now publish measured grid headroom" — 0 of 7 do.
    get_grid_intelligence returns headroom: null for ERCOT and PJM and emits
    NO headroom_measured block at all (main.py only sets it when
    _grid_telemetry_for() finds a row). iso_grid_adapters.py — the only writer
    of grid_telemetry — describes itself as "SKELETON / FRAMEWORK —
    intentionally inert until activated … NOT yet on an active cron". The
    Shell #35 serving helpers (measured_headroom_block / adjust_headroom) are
    real and unit-tested, but nothing feeds them.
  • "refreshed every 20 minutes" — no such cadence exists. US ISO data is EIA
    HOURLY, the grid-intel payload caches ~30 min, and HEADROOM_REFRESH_INTERVAL
    (1800s) belongs to a different, point-estimator subsystem.
  • "+9.9% raw / -3.1% corrected" — the METHOD is real
    (STRUCTURAL_OFFSET_PP["ERCOT"] = 13.0, and 9.9 - 13 = -3.1 exactly), but it
    corrects a reading that is never taken.

This test locks the copy to claims that are actually served. It is deliberately
two-sided: it fails if the false claims come BACK, and it also tells you to
revisit the copy if per-ISO measured headroom ever goes live — at which point
"7 of 7 ISOs publish measured headroom" becomes true and is worth saying.

Reads the copy via ast and RENDERS it (the template carries a literal %s and
importing the route module would drag Flask/db). Never imports main.
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402

RECON = os.path.join(ROOT, "routes", "competitor_recon.py")


def _rendered():
    """_COPY out of act_on_win_moves, rendered the way the caller renders it."""
    tree = ast.parse(open(RECON, encoding="utf-8").read())
    copy = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_COPY"
                for t in node.targets):
            copy = ast.literal_eval(node.value)
            break
    assert copy, "_COPY not found in routes/competitor_recon.py"
    out = {}
    for k, v in copy.items():
        if "%s" in v:
            v = v % (("9,999", "8,888") if v.count("%s") == 2 else ("9,999",))
        out[k] = (v + "\n\nhttps://dchub.cloud/dcpi")[:2800]
    return out


@pytest.fixture(scope="module")
def telemetry():
    posts = _rendered()
    assert "moat_live_telemetry" in posts, "the telemetry card disappeared"
    return posts["moat_live_telemetry"]


# ── the claims that are NOT served ─────────────────────────────────────────

def test_does_not_claim_measured_per_iso_grid_headroom(telemetry):
    """0 of 7 US ISOs serve a measured headroom reading (headroom: null, no
    headroom_measured block). If the grid_telemetry ingest is ever activated
    and this becomes true, delete this test and say it — loudly."""
    assert not re.search(r'measured\s+(?:grid\s+)?headroom', telemetry, re.I), (
        "copy claims measured grid headroom, which no ISO serves — "
        "iso_grid_adapters is an inert skeleton on no cron, and "
        "get_grid_intelligence returns headroom: null")


def test_does_not_claim_a_sub_hourly_refresh_cadence(telemetry):
    """US ISO telemetry is EIA hourly; the payload caches ~30 min. Any
    'every N minutes' claim under 60 is unsupported."""
    for m in re.finditer(r'every\s+(\d+)\s*(?:min|minute)', telemetry, re.I):
        pytest.fail("copy claims a %s-minute refresh; US ISO data is EIA "
                    "hourly and the grid payload caches ~30 min" % m.group(1))


def test_does_not_quote_a_corrected_headroom_reading(telemetry):
    """The ERCOT +13pp offset is real, but correcting a reading that is never
    taken produces a number describing a computation that never ran."""
    assert not re.search(r'[+\-−]\s?\d+(?:\.\d+)?\s?%\s+(?:raw|corrected)',
                         telemetry, re.I), (
        "copy quotes a raw/corrected headroom percentage; there is no measured "
        "headroom reading for the ERCOT offset to correct")


# ── the claims that ARE served (keep them honest AND present) ──────────────

def test_still_names_the_seven_us_isos(telemetry):
    """All 7 are genuinely live for demand + fuel mix (verified via the
    grid scoreboard), so the coverage claim itself is fine — it is the
    'measured headroom' predicate that was false."""
    for iso in ("PJM", "ERCOT", "MISO", "CAISO", "SPP", "NYISO", "ISO-NE"):
        assert iso in telemetry, "%s missing from the coverage claim" % iso


def test_ratio_matches_the_isos_it_names(telemetry):
    """'7 of 7' must agree with the seven operators actually listed — the
    numerator is what #1824's coverage_ratio pattern scores on."""
    m = re.search(r'\b(\d{1,3})\s+of\s+(\d{1,3})\b', telemetry)
    assert m, "the coverage ratio is gone — the card drops to 0.550 and is REFUSED"
    assert m.group(1) == m.group(2) == "7", (
        "ratio says %s of %s but the copy names 7 US ISOs" % m.groups())


# ── and it must still clear the publish gate ───────────────────────────────

def test_copy_still_clears_the_quality_gate(telemetry):
    score = cp._quality_score(telemetry)
    assert score >= cp.QUALITY_MIN, (
        "moat_live_telemetry scored %.3f < %.3f and is REFUSED at publish "
        "again. Keep the 'N of N' coverage ratio in the opening (that is the "
        "signal #1824's coverage_ratio pattern scores)." % (score, cp.QUALITY_MIN))


def test_every_staged_moat_card_clears_the_gate():
    for key, post in _rendered().items():
        assert cp._quality_score(post) >= cp.QUALITY_MIN, (
            "%s is below CONTENT_QUALITY_MIN and would be refused" % key)
