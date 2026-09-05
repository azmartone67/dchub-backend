"""Flywheel lanes 1+5 honest-gate guards (2026-07-31).

The two red lanes decomposed into four defect classes, each pinned here:

  A. SLUG-COMPOSER TWINS. Three serving surfaces (301 fallback, directory
     fallback, sitemap fallback) hand-composed `{provider}-{name}-{hash}`
     WITHOUT the provider-prefix dedupe the freeze stores — so every
     not-yet-frozen row was served/sitemapped under a URL that MOVED the
     moment the freeze ran. All composers now DELEGATE to
     facility_slug_freeze.build_canonical_slug (the one owner).
  B. RETENTION CHECK TWINS. flywheel + backfunnel each carried a copy of the
     claim-carry/activation SQL ("mirrors" = drifts). Both now render from
     routes/retention_claim_checks.
  C. REBASELINE ARTIFACT. Those checks gated a 30d window blending claims
     minted BEFORE the activation wiring existed (07-21/22) — un-fixable
     history reading as a live defect. The gate now scores the post-fix
     cohort at the SAME thresholds, abstains under MIN_POST_FIX_COHORT, and
     always shows pre-fix + blended.
  D. CADENCE ARTIFACT. Lane 5a gated instantaneous pending against a daily
     freeze cron — red whenever ingestion landed between beats. It now gates
     STALE pending (>26h = the loop is actually broken) and the cron runs
     every 6h.

No DB, no network: fake connections feed canned rows; the rest is source and
pure-function pinning.
"""
import re
from pathlib import Path

import pytest

rcc = pytest.importorskip("routes.retention_claim_checks")
fsf = pytest.importorskip("routes.facility_slug_freeze")

ROOT = Path(__file__).resolve().parent.parent
FLY = (ROOT / "routes" / "flywheel_master_shell.py").read_text()
BACK = (ROOT / "routes" / "backfunnel_master_shell.py").read_text()
SEO = (ROOT / "routes" / "seo_pages.py").read_text()
MAIN = (ROOT / "main.py").read_text()
WF = (ROOT / ".github" / "workflows" / "slug-freeze-daily.yml").read_text()


class _Cur:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=None):
        assert params is None, "retention checks must run WITHOUT bound params"
        self.sql = sql

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cur(self._row)

    def rollback(self):
        pass


# ── A. one slug composer ───────────────────────────────────────────────────

def test_seo_helpers_delegate_to_the_freeze_builder():
    """Both seo_pages helpers must produce EXACTLY what the freeze stores —
    including the provider-prefix dedupe and ascii folding — or unfrozen rows
    301/list under URLs that flip at freeze time."""
    from routes.seo_pages import (_canonical_facility_slug,
                                  _facility_canonical_slug)
    cases = [
        ("Iron Mountain", "Iron Mountain LON-3"),   # the live lane-5 sample
        ("Vantage Data Centers", "Vantage Data Centers LHR1"),
        ("Equinix", "DA11 Dallas"),                 # no embedding — prefix kept
        ("Int", "Internap Dallas"),                 # token boundary — kept
        ("", "Standalone Site"),
        ("Télécom", "Télécom Paris DC"),            # folding parity
    ]
    for provider, name in cases:
        want = fsf.build_canonical_slug(provider, name)
        assert _canonical_facility_slug(provider, name) == want, (provider, name)
        assert _facility_canonical_slug(provider, name) == want, (provider, name)


def test_dedupe_actually_applies_through_the_delegation():
    from routes.seo_pages import _canonical_facility_slug
    s = _canonical_facility_slug("Iron Mountain", "Iron Mountain LON-3")
    assert s and s.startswith("iron-mountain-lon-3-"), s
    assert "iron-mountain-iron-mountain" not in s


def test_short_name_contract_survives_delegation():
    """The 301/sitemap guards rely on None for UN-SLUGGABLE names — and a
    one- or two-character name is not un-sluggable.

    ★ 2026-09-05: this asserted `("X","ab") is None`. The rejection it pinned
    was `len(name_slug) < 3` in build_canonical_slug, which measured the NAME
    FRAGMENT while the slug it returns always carries a provider prefix and an
    8-char hash. It stranded 28 Operational facilities (RZ, Oi, B4, 1A, SC, L7)
    with no URL from March to September.

    The 301 guard is the reason to widen it, not a reason to keep it: the one
    caller (routes/seo_pages.py ~491) does `if _canon_slug: redirect(..., 301)`,
    so None means "this legacy /facility/<id> has NO canonical target". Those 28
    rows had none. Now they do.

    A MISSING name still returns None, and that is the contract this test
    actually protects."""
    from routes.seo_pages import _canonical_facility_slug
    assert _canonical_facility_slug("X", "ab") == "x-ab-c343e6d0"
    assert _canonical_facility_slug("X", "") is None
    assert _canonical_facility_slug("X", "!!!") is None


def test_sitemap_fallback_uses_the_freeze_builder():
    """The serve_sitemap fallback must not hand-compose; grep for the CODE
    call, and assert the old two-branch compose is gone from that region."""
    at = MAIN.index("r-lane5 (2026-07-31): the fallback composes via the FREEZE builder")
    region = MAIN[at - 2000: at + 2000]
    assert "build_canonical_slug(provider, name)" in region
    assert 'full_slug = f"{provider_slug}-{name_slug}-{short_hash}"' not in MAIN


# ── B. retention checks single-sourced ─────────────────────────────────────

def test_shell_twin_sql_is_gone():
    """The carry/activation EXISTS-join SQL must live ONLY in the shared
    module — a copy left in either shell is the drift this fix removes."""
    marker = "l.session_id=c.sid"
    assert marker not in FLY, "flywheel still carries inline claim-carry SQL"
    assert marker not in BACK, "backfunnel still carries inline claim-carry SQL"
    assert marker in (ROOT / "routes" / "retention_claim_checks.py").read_text()
    for src, shell in ((FLY, "flywheel"), (BACK, "backfunnel")):
        assert "claim_carry_verdict" in src, f"{shell}: not rendering from shared module"
        assert "claim_activation_verdict" in src, f"{shell}: not rendering from shared module"


def test_check_ids_stay_stable():
    """growth_ops_digest addresses these by id — renames break the digest."""
    for src in (FLY, BACK):
        assert '"ret_claim_carry"' in src
        assert '"ret_claim_activation"' in src


def test_reuse_gauges_no_longer_gate():
    """A check whose own text declares its number contaminated must not be a
    red gate. Both shells keep the number visible but pass None."""
    fly_at = FLY.index('"ret_key_reuse"')
    assert "None," in FLY[fly_at: fly_at + 200]
    back_at = BACK.index('"ret_mature_reuse"')
    # both render sites (query-failed and value) pass None now
    assert ">= 8.0" not in BACK[back_at - 400: back_at + 700]


# ── C. post-fix cohort gating ──────────────────────────────────────────────

def test_fix_date_is_declared_and_in_both_queries():
    assert rcc.CLAIM_CARRY_FIX_DATE == "2026-07-22"
    for sql in (rcc._SQL_CARRY, rcc._SQL_ACTIVATION):
        assert f"DATE '{rcc.CLAIM_CARRY_FIX_DATE}'" in sql
        assert "post_fix" in sql
        assert "mcp_dev_keys" in sql and "mcp_call_log" in sql


def test_gates_score_the_post_fix_cohort():
    # pre-fix terrible (9%), post-fix healthy (87%) → PASS: history no longer
    # drags the live gate.
    passed, detail = rcc.claim_carry_verdict(_Conn((35, 3, 69, 60)))
    assert passed is True
    assert "post-fix" in detail and "2026-07-22" in detail
    assert "blended" in detail and "pre-fix" in detail, \
        "the split must never hide the numbers it replaces"
    # post-fix genuinely failing → RED stays red (the green must be earned)
    passed, _ = rcc.claim_carry_verdict(_Conn((35, 3, 69, 30)))
    assert passed is False


def test_small_post_fix_cohort_abstains():
    passed, detail = rcc.claim_carry_verdict(_Conn((35, 3, 5, 5)))
    assert passed is None
    assert "maturing" in detail and "blended" in detail
    passed, detail = rcc.claim_activation_verdict(_Conn((100, 9, 4, 4)))
    assert passed is None


def test_activation_threshold_holds_at_40():
    passed, _ = rcc.claim_activation_verdict(_Conn((100, 9, 20, 8)))
    assert passed is True    # 40.0% exactly
    passed, _ = rcc.claim_activation_verdict(_Conn((100, 9, 20, 7)))
    assert passed is False   # 35%


def test_empty_and_failed_paths_stay_unmeasured():
    assert rcc.claim_carry_verdict(_Conn((0, 0, 0, 0)))[0] is None
    assert rcc.claim_carry_verdict(_Conn(None))[0] is None

    class _Boom:
        def cursor(self):
            raise RuntimeError("db down")

        def rollback(self):
            pass

    passed, detail = rcc.claim_carry_verdict(_Boom())
    assert passed is None and "failed" in detail


# ── D. cadence + stale-pending gate ────────────────────────────────────────

def test_freeze_cron_runs_every_6h():
    assert re.search(r"cron:\s*'17 5,11,17,23 \* \* \*'", WF), \
        "slug freeze must tick every 6h (daily cadence left batches unfrozen all day)"


def test_lane5_gates_on_stale_pending_with_strict_fallback():
    at = FLY.index('"seo_slug_frozen"')
    region = FLY[at - 200: at + 3000]
    assert "interval '26 hours'" in region, "stale threshold missing"
    assert "information_schema.columns" in region, \
        "age columns must be probed (LIVE tables are the truth, not repo DDL)"
    assert "strict gate" in region, "no-age-column fallback must stay strict"
    assert "first_seen" in region and "created_at" in region
