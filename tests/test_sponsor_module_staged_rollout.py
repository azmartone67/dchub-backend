"""tests/test_sponsor_module_staged_rollout.py — the first sponsor does not
find out about templated-content risk on 7,292 pages at once (P1-4, 2026-08-28).

THE RISK BEING MANAGED. About 80% of sitemapped facility pages are thin (35–98
unique words). A visually identical sponsored module repeated across thousands
of them is the shape search engines read as templated. Rolling a first placement
onto the whole proven set means discovering that on a paying customer's
campaign, with no control group to tell a module effect apart from anything else
that happened that fortnight.

★★★ THE DEFAULT IS UNCAPPED, AND THAT IS THE POINT OF THIS FILE. /advertise
    says publicly "Runs across the 7,292 pages with proven search demand — not
    the whole sitemap". A silent cap under that sentence makes the published
    claim false in the OPPOSITE direction from the one P1-3 fixed. The
    mechanism ships; arming it is a deliberate act that has to be paired with a
    copy change. test_the_cap_is_off_by_default is the fence.

★★★ THIS FILE ALREADY EARNED ITS KEEP. The first implementation sorted the
    proven set by hash and took the first N. test_the_cohort_survives_the_
    proven_set_growing killed it: growing the set 2,000 -> 2,400 EVICTED 22 of
    every 100 cohort members, because new slugs with smaller hashes displaced
    them. seo_proven_pages grows daily, so over a 14-day read pages would enter
    and leave the treatment group and the comparison would measure that churn.
    Membership is now a pure function of (slug, percent) against a fixed key
    space, so no page can ever leave.

★ AND: the cohort must be a RANDOM sample of the proven set, not its strongest
  members. Top-N-by-impressions would make the treatment group systematically
  better than the control, so a 14-day Search Console read would measure page
  quality instead of the module.
"""
import os

import pytest

from routes import sponsor_render as sr


@pytest.fixture(autouse=True)
def _clean_env_and_caches(monkeypatch):
    monkeypatch.delenv("SPONSOR_MODULE_PAGE_PERCENT", raising=False)
    sr._cohort_cache.clear()
    sr._proven_cache.clear()
    yield
    sr._cohort_cache.clear()
    sr._proven_cache.clear()


def _proven(n, monkeypatch):
    """Pin a proven-slug set without touching the database."""
    slugs = frozenset(f"facility-{i:05d}" for i in range(n))
    monkeypatch.setattr(sr, "proven_slugs", lambda: slugs)
    return slugs


# ── the default ──────────────────────────────────────────────────────
def test_the_cap_is_off_by_default():
    """★★★ /advertise states the full proven set. A cap that arrives switched
    on makes that published sentence false the day a sponsor activates."""
    assert sr.rollout_percent() is None
    assert sr.rollout_status()["capped"] is False


@pytest.mark.parametrize("value", ["", "0", "-1", "nonsense", "  ", "100", "250"])
def test_unusable_values_mean_uncapped(monkeypatch, value):
    """"No usable value configured" must resolve to what we advertise, never to
    an accidental cohort of 0 pages — which would silently switch the product
    off while an advertiser is paying for it."""
    monkeypatch.setenv("SPONSOR_MODULE_PAGE_PERCENT", value)
    assert sr.rollout_percent() is None


def test_uncapped_lets_every_proven_page_render(monkeypatch):
    _proven(2000, monkeypatch)
    assert sr.rollout_cohort() is None
    assert sr.page_is_eligible("facility_module", ("facility-01999",)) is True


# ── the cohort ───────────────────────────────────────────────────────
def test_the_cohort_is_about_the_configured_share(monkeypatch):
    """A percentage of the key space is a percentage of the pages, within
    sampling noise. 7% of 7,292 proven pages is the ~500 the rollout plan asks
    for; the operator sets the share and reads the resulting count back out of
    rollout_status()."""
    _proven(4000, monkeypatch)
    monkeypatch.setenv("SPONSOR_MODULE_PAGE_PERCENT", "10")
    n = len(sr.rollout_cohort())
    assert 300 <= n <= 500, f"10% of 4,000 pages came out as {n}"


def test_pages_outside_the_cohort_are_withheld(monkeypatch):
    allowed = _proven(2000, monkeypatch)
    monkeypatch.setenv("SPONSOR_MODULE_PAGE_PERCENT", "25")
    cohort = sr.rollout_cohort()
    inside = next(iter(cohort))
    outside = next(s for s in allowed if s not in cohort)
    assert sr.page_is_eligible("facility_module", (inside,)) is True
    assert sr.page_is_eligible("facility_module", (outside,)) is False


def test_a_page_outside_the_proven_set_is_still_withheld(monkeypatch):
    """The cap narrows the proven set; it never widens it.

    ★ BOTH PATHS, and the uncapped one is the load-bearing half. Mutation
    testing deleted the proven-set check and this test still passed while it
    only exercised the capped case — the cohort is built FROM the proven set,
    so a non-proven slug is absent from it either way. With no cap configured
    (the default, and therefore the state this runs in today) the proven check
    is the only thing standing between the module and all ~17k facility pages."""
    _proven(2000, monkeypatch)
    assert sr.rollout_percent() is None, "this half must run uncapped"
    assert sr.page_is_eligible("facility_module", ("not-a-proven-slug",)) is False
    monkeypatch.setenv("SPONSOR_MODULE_PAGE_PERCENT", "25")
    assert sr.page_is_eligible("facility_module", ("not-a-proven-slug",)) is False


def test_market_module_is_untouched_by_the_cap(monkeypatch):
    """seo_proven_pages is facility URLs only; market_module runs on ~250
    curated pages and gating it on a facility table would zero it."""
    _proven(2000, monkeypatch)
    monkeypatch.setenv("SPONSOR_MODULE_PAGE_PERCENT", "1")
    assert sr.page_is_eligible("market_module", ("anything",)) is True


# ── the cohort must be reproducible and unbiased ─────────────────────
def test_the_cohort_is_stable_across_processes(monkeypatch):
    """★ hashlib, never the builtin hash(): str hashing is salted per process
    (PYTHONHASHSEED), so a builtin-hash cohort would differ between workers and
    the module would flicker per request. Recomputed here in a SEPARATE
    interpreter with a different seed."""
    import json
    import subprocess
    import sys
    code = (
        "import os,sys;sys.path.insert(0,%r);"
        "from routes import sponsor_render as sr;"
        "s=frozenset('facility-%%05d'%%i for i in range(2000));"
        "sr.proven_slugs=lambda: s;"
        "os.environ['SPONSOR_MODULE_PAGE_PERCENT']='5';"
        "import json;print(json.dumps(sorted(sr.rollout_cohort())))"
    ) % os.path.dirname(os.path.dirname(os.path.abspath(sr.__file__)))
    outs = []
    for seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        env.pop("SPONSOR_MODULE_PAGE_PERCENT", None)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=env)
        assert r.returncode == 0, r.stderr[-800:]
        outs.append(json.loads(r.stdout.strip().splitlines()[-1]))
    assert outs[0] == outs[1], (
        "the cohort changed with PYTHONHASHSEED — it is not reproducible across "
        "workers, so a page would be in the holdout on one process and out on "
        "another")
    assert 50 <= len(outs[0]) <= 150, len(outs[0])


def test_the_cohort_is_not_the_top_of_the_set(monkeypatch):
    """★ A cohort selected by rank — impressions, or slug order standing in for
    it — makes treatment and control systematically different, so a 14-day
    Search Console comparison measures page quality, not the module."""
    _proven(2000, monkeypatch)
    monkeypatch.setenv("SPONSOR_MODULE_PAGE_PERCENT", "10")
    cohort = sorted(sr.rollout_cohort())
    ordered = sorted(f"facility-{i:05d}" for i in range(2000))[:200]
    assert cohort != ordered, "the cohort is just the head of the ordered set"
    # ...and it should be spread across the whole set, not clustered in it.
    idx = [int(s.split("-")[1]) for s in cohort]
    assert max(idx) > 1500 and min(idx) < 500, (
        f"cohort is clustered (min {min(idx)}, max {max(idx)}) rather than "
        f"sampled across the proven set")


def test_the_cohort_survives_the_proven_set_growing(monkeypatch):
    """★★★ THE TEST THAT KILLED THE FIRST IMPLEMENTATION. Sorting the proven set
    by hash and taking the first N evicted 22 of every 100 cohort members when
    the set grew 2,000 -> 2,400, because new slugs with smaller hashes displaced
    them. seo_proven_pages grows every day as pages cross the impression floor,
    so pages would enter and leave the treatment group mid-experiment and a
    14-day Search Console read would measure that churn.

    NOT "mostly stable" — a strict superset. No page may EVER leave."""
    _proven(2000, monkeypatch)
    monkeypatch.setenv("SPONSOR_MODULE_PAGE_PERCENT", "5")
    before = sr.rollout_cohort()
    assert before, "empty cohort — the assertion below would be vacuous"
    sr._cohort_cache.clear()
    _proven(2400, monkeypatch)          # 400 more pages earn demand
    after = sr.rollout_cohort()
    lost = before - after
    assert not lost, (
        f"{len(lost)} page(s) left the cohort when the proven set grew; the "
        f"holdout roster is not stable and the comparison is contaminated")


# ── what an operator sees ────────────────────────────────────────────
def test_status_reports_the_copy_consequence_when_armed(monkeypatch):
    _proven(2000, monkeypatch)
    monkeypatch.setenv("SPONSOR_MODULE_PAGE_PERCENT", "25")
    st = sr.rollout_status()
    assert st["capped"] is True
    assert st["percent"] == 25
    assert st["proven_pages"] == 2000
    assert 0 < st["eligible_pages"] < 2000
    assert "/advertise" in st["note"], (
        "arming the cap makes a published sentence false until the copy "
        "changes; the status must say so where the operator will read it")


def test_status_when_the_proven_set_has_never_loaded(monkeypatch):
    monkeypatch.setattr(sr, "proven_slugs", lambda: None)
    st = sr.rollout_status()
    assert st["proven_set_loaded"] is False
    assert st["proven_pages"] is None
    assert st["eligible_pages"] is None
