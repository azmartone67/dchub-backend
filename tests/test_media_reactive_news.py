"""Unit tests for the reactive-news lane's pure logic — no DB / no LLM.

Covers the three pieces most likely to misfire: the analyst-respect guard
(must block disparagement of cited sources), claim extraction, and the
free-text market resolver's alias path.
See routes/media_reactive_news.py.
"""
import pytest

from routes import media_reactive_news as rn


class _RaisingCursor:
    """A cursor stub whose .execute always raises, so _resolve_market_slug
    exercises its DB-less alias fallback path deterministically."""
    def execute(self, *a, **k):
        raise RuntimeError("no db in unit test")

    def fetchone(self):
        return None

    def fetchall(self):
        return []


# ── analyst-respect guard ────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "CBRE got this wrong — DC Hub data shows the real picture.",
    "JLL is outdated; our grid data is fresher.",
    "The CBRE report is flawed and misses the grid story.",
    "DataCenterHawk is out of touch on time-to-power.",
])
def test_analyst_respect_blocks_disparagement(text):
    ok, reason = rn._analyst_respect_ok(text)
    assert ok is False
    assert "analyst-respect" in reason


@pytest.mark.parametrize("text", [
    # Regression: the exact live draft that was wrongly rejected — "behind" is
    # neutral grid language ("60-month explanation behind it"), not a dig at CBRE.
    "0.3% vacancy in Northern Virginia—CBRE's Q1 number—is a demand signal with "
    "a 60-month explanation behind it. DC Hub's grid data shows why.",
    "The markets CBRE flags are behind on interconnection, lagging the queue.",
])
def test_analyst_respect_allows_neutral_grid_language(text):
    ok, _ = rn._analyst_respect_ok(text)
    assert ok is True


@pytest.mark.parametrize("text", [
    "Per CBRE, Northern Virginia is at 0.3% vacancy. DC Hub's grid data explains why.",
    "JLL reports record absorption; on the DC Hub Power Index the market reads AVOID.",
    "Building on CBRE's Q1 read, the interconnection queue is the binding constraint.",
])
def test_analyst_respect_allows_respectful_citation(text):
    ok, _ = rn._analyst_respect_ok(text)
    assert ok is True


# ── claim extraction ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("title,summary,needle", [
    ("Northern Virginia hits 0.3% vacancy", "", "0.3%"),
    ("716.7 MW under construction in Dallas", "", "716.7 MW"),
    ("", "Colocation pricing reached $215/kW-month", "$215"),
    ("Market grows 33% year over year", "", "33%"),
])
def test_extract_claim_finds_number(title, summary, needle):
    got = rn._extract_claim(title, summary)
    assert got is not None and needle in got


def test_extract_claim_none_when_no_number():
    assert rn._extract_claim("Data centers are booming", "demand is strong") is None


# ── free-text market resolver (alias path, DB-less) ──────────────────────────
@pytest.mark.parametrize("free_text,slug", [
    ("Northern Virginia", "northern-virginia"),
    ("Loudoun County", "northern-virginia"),
    ("NoVa", "northern-virginia"),
    ("DFW", "dallas"),
    ("Dallas-Ft. Worth", "dallas"),
])
def test_resolve_market_slug_alias(free_text, slug):
    got_slug, _name = rn._resolve_market_slug(_RaisingCursor(), free_text)
    assert got_slug == slug


def test_resolve_market_slug_unknown_returns_none():
    got_slug, got_name = rn._resolve_market_slug(_RaisingCursor(), "Atlantis")
    assert got_slug is None and got_name is None


# ── cheap NER over a news item ───────────────────────────────────────────────
@pytest.mark.parametrize("title,expect", [
    ("Ashburn data center boom continues", "ashburn"),
    ("Northern Virginia vacancy collapses", "northern virginia"),
    ("Tokyo colocation pricing rises", None),
])
def test_market_from_news(title, expect):
    assert rn._market_from_news(title, "") == expect


# ── dark-by-default invariant ────────────────────────────────────────────────
def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MEDIA_REACTIVE_NEWS_ENABLED", raising=False)
    assert rn._enabled() is False
    # the importable P2 collector must no-op when dark
    assert rn.reactive_news_leads() == []
