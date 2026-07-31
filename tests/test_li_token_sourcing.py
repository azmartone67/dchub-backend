"""LinkedIn token sourcing — DB-first with env fallback (2026-07-31).

The incident: the auto-publish drain read ONLY the LINKEDIN_ACCESS_TOKEN env
var and 401'd (EXPIRED_ACCESS_TOKEN, post 105426) while the proactive refresh
cron kept a perfectly healthy token in the DB (13 days to expiry,
refresh_token present). linkedin_poster's own posts never hit this because
_get_valid_token() is already DB-first. content_publisher._li_access_token()
closes the split for every content_publisher publish path.

DB-free: linkedin_poster is faked via sys.modules (the helper lazy-imports
it), so these tests never import the real poster module or touch a DB.
Never imports main.
"""
import inspect
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cp = pytest.importorskip("content_publisher")  # noqa: E402


def _fake_poster(monkeypatch, get_valid_token):
    fake = types.ModuleType('linkedin_poster')
    fake._get_valid_token = get_valid_token
    monkeypatch.setitem(sys.modules, 'linkedin_poster', fake)


def test_db_token_wins_over_stale_env(monkeypatch):
    _fake_poster(monkeypatch, lambda: 'db-tok-fresh')
    monkeypatch.setenv('LINKEDIN_ACCESS_TOKEN', 'env-tok-stale')
    assert cp._li_access_token() == 'db-tok-fresh'


def test_env_fallback_when_db_has_no_token(monkeypatch):
    _fake_poster(monkeypatch, lambda: None)
    monkeypatch.setenv('LINKEDIN_ACCESS_TOKEN', 'env-tok')
    assert cp._li_access_token() == 'env-tok'


def test_env_fallback_when_poster_lookup_raises(monkeypatch):
    def boom():
        raise RuntimeError('db down')
    _fake_poster(monkeypatch, boom)
    monkeypatch.setenv('LINKEDIN_ACCESS_TOKEN', 'env-tok')
    # fail-open: a poster problem must never be darker than env-only behaviour
    assert cp._li_access_token() == 'env-tok'


def test_empty_when_both_sources_missing(monkeypatch):
    _fake_poster(monkeypatch, lambda: '')
    monkeypatch.delenv('LINKEDIN_ACCESS_TOKEN', raising=False)
    assert cp._li_access_token() == ''


def test_whitespace_db_token_falls_through(monkeypatch):
    _fake_poster(monkeypatch, lambda: '   ')
    monkeypatch.setenv('LINKEDIN_ACCESS_TOKEN', 'env-tok')
    assert cp._li_access_token() == 'env-tok'


def test_all_publish_paths_use_helper_not_raw_env():
    """The regression guard: no publish path may re-grow its own env read.
    (The stats badge, env-reset route and worker-proxy keep their own
    semantics — this pins the three publish paths + the badge.)"""
    for fn in (cp.start_auto_publisher, cp.publish_linkedin,
               cp.enqueue_custom, cp.content_stats):
        src = inspect.getsource(fn)
        assert '_li_access_token()' in src, fn.__name__
        assert "os.environ.get('LINKEDIN_ACCESS_TOKEN'" not in src, fn.__name__
