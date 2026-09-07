"""Unit tests for the media master shell's measure/score logic — no DB / no HTTP.

Regression coverage for the 2026-07-11 "media 35/100 · citations None/7d"
deep-dive findings (routes/media_master_shell.py):

  1. r-cv-none — tier1_measure used `a or b or c` over the north-star
     fields, so a REAL citation_velocity_7d of 0 (falsy) fell through to
     fields that don't exist and the lane displayed "citations None/7d"
     forever. cv7 must take the FIRST PRESENT field; None only when the
     north-star request itself failed.
  2. r-multiplatform-cadence — the durable posts_24h fallback only counted
     linkedin_posts, so X/Bluesky publishes (durable only in
     social_media_posts) were invisible (lane read 1 post/24h while 7
     shipped). The durable count must sum both ledgers and be taken as a
     max with the in-memory loop counters every tick.
  3. tier2_score decomposition — 1 post + no citations is exactly 35.0
     (the observed lane score); 2+ posts with citations is 100.
"""
from routes import media_master_shell as ms


# ── helpers ──────────────────────────────────────────────────────────────────

class _Cursor:
    """Cursor stub returning canned COUNT(*) values per ledger table."""

    def __init__(self, linkedin_count, social_count):
        self._linkedin = linkedin_count
        self._social = social_count
        self._last = 0

    def execute(self, sql, *a, **k):
        if "linkedin_posts" in sql:
            self._last = self._linkedin
        elif "social_media_posts" in sql:
            self._last = self._social
        else:
            self._last = 0

    def fetchone(self):
        return (self._last,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _measure_with(monkeypatch, north, pub=None, conn=None):
    def fake_req(path, method="GET", timeout=10):
        if "north-star" in path:
            return {"ok": True, "http": 200, "data": north}
        return {"ok": True, "http": 200, "data": (pub or {})}

    monkeypatch.setattr(ms, "_req", fake_req)
    monkeypatch.setattr(ms, "_conn", lambda: conn)
    return ms.tier1_measure()


# ── 1 · cv7 None/0 conflation (r-cv-none) ───────────────────────────────────

def test_cv7_real_zero_is_zero_not_none(monkeypatch):
    m = _measure_with(monkeypatch, {"citation_velocity_7d": 0,
                                    "prior_7d": 0, "trend_pct": None})
    assert m["citation_velocity_7d"] == 0
    assert m["citation_velocity_7d"] is not None


def test_cv7_positive_value_passes_through(monkeypatch):
    m = _measure_with(monkeypatch, {"citation_velocity_7d": 2})
    assert m["citation_velocity_7d"] == 2


def test_cv7_none_only_when_request_failed(monkeypatch):
    # north-star unreachable -> data {} -> no field present -> None.
    m = _measure_with(monkeypatch, {})
    assert m["citation_velocity_7d"] is None


def test_cv7_falls_back_to_legacy_field_names(monkeypatch):
    m = _measure_with(monkeypatch, {"citations_7d": 3})
    assert m["citation_velocity_7d"] == 3


# ── 2 · durable multi-platform cadence (r-multiplatform-cadence) ─────────────

def test_posts_24h_sums_linkedin_and_social_ledgers(monkeypatch):
    conn = _Conn(_Cursor(linkedin_count=1, social_count=6))
    m = _measure_with(monkeypatch, {"citation_velocity_7d": 2}, conn=conn)
    # 1 linkedin_posts success + 6 non-LinkedIn social_media_posts publishes.
    assert m["posts_24h"] == 7


def test_posts_24h_durable_beats_zeroed_loop_counters(monkeypatch):
    # In-memory counters zero out on redeploy; durable ledger must win.
    pub = {"loops": {"linkedin": {"successes_24h": 0, "attempts_24h": 0}}}
    conn = _Conn(_Cursor(linkedin_count=2, social_count=0))
    m = _measure_with(monkeypatch, {"citation_velocity_7d": 0},
                      pub=pub, conn=conn)
    assert m["posts_24h"] == 2


def test_posts_24h_loop_counters_win_when_higher(monkeypatch):
    pub = {"loops": {"linkedin": {"successes_24h": 5, "attempts_24h": 6}}}
    conn = _Conn(_Cursor(linkedin_count=1, social_count=1))
    m = _measure_with(monkeypatch, {"citation_velocity_7d": 0},
                      pub=pub, conn=conn)
    assert m["posts_24h"] == 5


def test_posts_24h_no_db_degrades_to_loop_counters(monkeypatch):
    pub = {"loops": {"linkedin": {"successes_24h": 1, "attempts_24h": 1}}}
    m = _measure_with(monkeypatch, {"citation_velocity_7d": 0},
                      pub=pub, conn=None)
    assert m["posts_24h"] == 1


# ── 3 · score decomposition ──────────────────────────────────────────────────

def test_score_35_reproduces_the_observed_lane(monkeypatch):
    # 1 post/24h, citations 0 -> 0.7*0.5 + 0.3*0 = 0.35 -> 35.0.
    # ★ starved flipped True here in r-cadence-floor (2026-09-07): the score
    # was ALWAYS 35/100 for this input, but the lane still called it "not
    # starved" and acted none. See test_one_post_a_day_is_starved below.
    sc = ms.tier2_score({"posts_24h": 1, "citation_velocity_7d": 0})
    assert sc["media_score"] == 35.0
    assert sc["starved"] is True


def test_score_full_health_with_cadence_and_citations():
    sc = ms.tier2_score({"posts_24h": 7, "citation_velocity_7d": 2})
    assert sc["media_score"] == 100.0
    assert sc["starved"] is False


def test_score_starved_at_zero_posts():
    sc = ms.tier2_score({"posts_24h": 0, "citation_velocity_7d": 2})
    assert sc["starved"] is True


# ── 4 · cadence floor (r-cadence-floor, 2026-09-07) ─────────────────────────
#
# THE DEFECT: `starved = (posts == 0)`. A floor of zero only detects a feed
# that has stopped COMPLETELY. Measured live on 2026-09-07, twenty consecutive
# ticks of /api/v1/admin/media/master-state read
#     posts_24h=1, score=65, starved=False, acted=none
# with reason "feed not starved (1 posts/24h)" — for 12+ hours, while the
# analyst voice was effectively silent and X/Bluesky shipped nothing at all.
# One surviving post per day held the dead-man above its own floor forever.

def test_one_post_a_day_is_starved(monkeypatch):
    """★ THE REGRESSION. 1 post/24h is below the 2/day cadence target."""
    monkeypatch.delenv("MEDIA_CADENCE_FLOOR", raising=False)
    sc = ms.tier2_score({"posts_24h": 1, "citation_velocity_7d": 4})
    assert sc["starved"] is True
    assert sc["cadence_floor"] == 2


def test_two_posts_a_day_is_fed(monkeypatch):
    monkeypatch.delenv("MEDIA_CADENCE_FLOOR", raising=False)
    sc = ms.tier2_score({"posts_24h": 2, "citation_velocity_7d": 0})
    assert sc["starved"] is False


def test_floor_matches_the_cadence_target_the_score_uses(monkeypatch):
    """The two must not drift: full cadence score == not starved."""
    monkeypatch.delenv("MEDIA_CADENCE_FLOOR", raising=False)
    full_cadence = ms.tier2_score({"posts_24h": 2, "citation_velocity_7d": 2})
    assert full_cadence["media_score"] == 100.0
    assert full_cadence["starved"] is False


def test_floor_cannot_be_tuned_back_to_zero(monkeypatch):
    """★ An env var must not be able to restore the original defect."""
    for bad in ("0", "-5", "not-a-number", ""):
        monkeypatch.setenv("MEDIA_CADENCE_FLOOR", bad)
        sc = ms.tier2_score({"posts_24h": 0, "citation_velocity_7d": 0})
        assert sc["starved"] is True, f"floor disabled by MEDIA_CADENCE_FLOOR={bad!r}"
        assert sc["cadence_floor"] >= 1, bad


def test_floor_is_raisable_for_a_higher_cadence_target(monkeypatch):
    monkeypatch.setenv("MEDIA_CADENCE_FLOOR", "4")
    sc = ms.tier2_score({"posts_24h": 3, "citation_velocity_7d": 2})
    assert sc["starved"] is True
    assert sc["cadence_floor"] == 4
