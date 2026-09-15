"""/connect/<client>: the trial terms on the page are the terms auto_trial enforces.

r-connect-trial-terms (2026-09-15). Every install page renders from
routes/mcp_connect._PAGE_TEMPLATE_RAW, and its steps 1 and 4 typed the trial's
terms into it: "No email required", then a daily figure and a trial length
that routes/auto_trial.py gives only to an EMAIL-BOUND key. What auto_trial
enforces:

  * an unbound key gets TRIAL_DAILY_UNBOUND calls a day, and validate_trial_key
    refuses it (bind_email_required) once it has used TRIAL_FREE_CALLS_UNBOUND
    calls in all;
  * binding an email (POST /api/v1/keys/auto-trial/bind) lifts that, and the
    same key gets TRIAL_DAILY_CALLS a day until mint + TRIAL_DAYS;
  * a mint seeds a new key with its network's spent count, so a network that
    used its free calls gets a key refused from its first call.

Rendered with mc._render_page, as tests/test_connect_return_nudge.py does, so
every check reads bytes that ship. The figures are patched to values the
defaults never take, and patched AGAIN between two renders, so a figure typed
into the template, or read once and kept, goes red.

Run:  python3 -m pytest tests/test_connect_trial_copy_follows_auto_trial.py -v
"""

from __future__ import annotations

import re
import sys

import pytest

import canonical_stats as cs
import routes.auto_trial as at
import routes.mcp_connect as mc

ALL_CLIENTS = sorted(mc._CLIENTS.keys())
B = '<b style="color:var(--text)">'


@pytest.fixture
def stats_state():
    """Restore canonical_stats' module cache, which rendering reads."""
    prev_cache, prev_ts, prev_live = cs._cache, cs._cache_ts, set(cs._live_keys)
    yield
    cs._cache, cs._cache_ts = prev_cache, prev_ts
    cs._live_keys.clear()
    cs._live_keys.update(prev_live)


def _terms(monkeypatch, free, daily_unbound, daily_bound, days):
    monkeypatch.setattr(at, "TRIAL_FREE_CALLS_UNBOUND", free)
    monkeypatch.setattr(at, "TRIAL_DAILY_UNBOUND", daily_unbound)
    monkeypatch.setattr(at, "TRIAL_DAILY_CALLS", daily_bound)
    monkeypatch.setattr(at, "TRIAL_DAYS", days)


def _copy(html, n):
    """The paragraph under step n's heading, markup included."""
    start = html.index(f"<!-- STEP {n}:")
    p = html.index("<p", start)
    return html[html.index(">", p) + 1:html.index("</p>", p)]


def _sentences(copy):
    return re.split(r"(?<=\.)\s+", re.sub(r"<[^>]+>", "", copy).strip())


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_both_steps_state_the_terms_auto_trial_enforces(client, monkeypatch, stats_state):
    # Two renders, two sets of terms: the second render has to see the second set.
    for free, daily_unbound, daily_bound, days in ((7, 23, 61, 9), (4, 19, 83, 11)):
        _terms(monkeypatch, free, daily_unbound, daily_bound, days)
        html = mc._render_page(client, 4242)
        bound = f"{B}{daily_bound} requests/day</b> for the rest of its {days}-day trial"
        for n in (1, 4):
            copy = _copy(html, n)
            assert f"{B}{free} free calls</b>" in copy, (
                f"/connect/{client} step {n} does not state TRIAL_FREE_CALLS_UNBOUND={free} "
                f"as the free calls without an email: {copy!r}")
            assert bound in copy, (
                f"/connect/{client} step {n} does not state TRIAL_DAILY_CALLS={daily_bound} "
                f"and TRIAL_DAYS={days} as the email-bound terms: {copy!r}")


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_the_bound_quota_is_only_offered_with_an_email(client, stats_state):
    html = mc._render_page(client, 4242)
    assert "No email required" not in html
    for n in (1, 4):
        said = [s for s in _sentences(_copy(html, n))
                if f"{at.TRIAL_DAILY_CALLS} requests/day" in s]
        assert said, f"/connect/{client} step {n} no longer states the email-bound quota"
        for s in said:
            assert "Bind an email" in s and "the same key keeps working" in s, (
                f"/connect/{client} step {n} states the email-bound quota in a sentence "
                f"that does not say it takes an email: {s!r}")


def test_the_daily_cap_is_named_only_when_it_bites_before_the_free_calls(monkeypatch, stats_state):
    _terms(monkeypatch, free=40, daily_unbound=12, daily_bound=61, days=9)
    html = mc._render_page("chatgpt", 4242)
    for n in (1, 4):
        assert f"{B}40 free calls</b>, at most 12 a day." in _copy(html, n)

    _terms(monkeypatch, free=12, daily_unbound=12, daily_bound=61, days=9)
    html = mc._render_page("chatgpt", 4242)
    for n in (1, 4):
        copy = _copy(html, n)
        assert f"{B}12 free calls</b>." in copy
        assert "at most" not in copy, copy


def test_step_one_tells_a_network_that_spent_its_free_calls(stats_state):
    copy = _copy(mc._render_page("chatgpt", 4242), 1)
    assert "a new key needs the email from its first call" in copy


def test_unreadable_terms_render_no_figure_rather_than_a_wrong_one(monkeypatch, stats_state):
    # None in sys.modules makes the render path's `from routes.auto_trial import`
    # raise, which is what an unimportable module does.
    monkeypatch.setitem(sys.modules, "routes.auto_trial", None)
    html = mc._render_page("chatgpt", 4242)
    for n in (1, 4):
        copy = _copy(html, n)
        assert "a limited number of free calls" in copy, copy
        assert "the same key keeps working" in copy, copy
        assert not re.search(r"\d+\s*(?:free calls|requests/day|-day)", copy), copy
