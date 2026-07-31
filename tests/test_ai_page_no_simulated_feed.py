"""The /ai activity feed must never fabricate entries (r-honest-feed, 2026-07-30).

The 'Latest AI Requests' ticker used to pad itself when quiet: fewer than 5
real items → invented platform×endpoint pairs (grok/groq/claude/… rotation)
stamped "Just now". That fabricated activity on the page whose entire job is
honest AI-traffic numbers, and it manufactured a feed-vs-chart contradiction
that cost a real debugging session — the operator saw "Grok, just now" in the
feed while the (honest) 7-day chart said 2 hits all week.

These tests pin the removal across every copy of the page in THIS repo (the
frontend repo's copy is pinned by its own PR; the multi-copy layout is itself
a known drift hazard). A tombstone comment quoting the removed behaviour is
allowed — the MECHANISM is what must stay dead.

Pure file reads; no DB, no network, never imports main.
"""
import os

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_AI_PAGE_COPIES = [
    p for p in (
        os.path.join(_REPO, "ai.html"),
        os.path.join(_REPO, "static", "ai.html"),
        os.path.join(_REPO, "dchub-frontend", "ai.html"),
    ) if os.path.exists(p)
]


def test_copies_exist():
    """If every copy vanished, the guards below would pass vacuously."""
    assert _AI_PAGE_COPIES, "no ai.html copies found — page moved? update this test"


@pytest.mark.parametrize("path", _AI_PAGE_COPIES,
                         ids=[os.path.relpath(p, _REPO) for p in _AI_PAGE_COPIES])
def test_feed_has_no_simulation_mechanism(path):
    src = open(path, encoding="utf-8").read()
    assert "simPlatforms" not in src, \
        "the simulated-feed platform rotation is back — a quiet feed is a true feed"
    assert "simEndpoints" not in src and "simTimes" not in src
    assert "while (feedItems.length < 5)" not in src, \
        "the padding loop is back under a new name"


@pytest.mark.parametrize("path", _AI_PAGE_COPIES,
                         ids=[os.path.relpath(p, _REPO) for p in _AI_PAGE_COPIES])
def test_feed_declares_honest_empty_state_and_classifier(path):
    src = open(path, encoding="utf-8").read()
    assert "Quiet right now" in src, \
        "the honest empty-state was removed — an empty feed must say so, not invent"
    assert "User-Agent + Referer" in src, \
        "the feed's classifier declaration is gone — feed and chart classify " \
        "differently, and the page must say so instead of contradicting itself"
