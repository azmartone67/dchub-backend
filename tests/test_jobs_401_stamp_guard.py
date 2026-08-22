"""cron_last_run completion stamps must require admin auth (2026-08-07).

The decommissioned heroic-reprieve zombie scheduler 401'd ~34 calls/day into
/api/jobs/* after the 07-31 admin-key rotation, and jobs_routes' after_request
completion stamp fired anyway — refreshing last_completed_at / last_status for
12 of 21 jobs and pacifying check_cron_freshness for a week while the jobs were
actually dead. Any anonymous caller could refresh job health the same way: an
unauthenticated 401 was indistinguishable, in cron_last_run, from a real run
that failed.

The fix gates the stamp on flask.g._jobs_admin_authed, set only after
_require_admin_key validates the key. These tests drive the REAL functions
under a Flask request context with the DB writers stubbed out — they assert
behavior, not source text.
"""


# Strength-checked since 2026-08-07: the admin gate drops credentials that
# look guessable, so this fixture has to be shaped like a real 64-hex key.
# See util/admin_auth.py and tests/test_admin_credential_strength.py.
REAL_KEY = "c1d" + "8b52f7" * 10 + "e4"


def _patched(monkeypatch, completes, starts=None):
    """Import the real module with both cron_last_run writers stubbed."""
    import routes.jobs_routes as jr
    monkeypatch.setattr(
        jr, "_record_cron_complete",
        lambda job_name, status='ok', duration_ms=None, error=None:
            completes.append((job_name, status)))
    monkeypatch.setattr(
        jr, "_record_cron_run",
        lambda job_name, expected_interval_s=None:
            (starts is not None) and starts.append(job_name))
    return jr


def test_unauthenticated_401_leaves_no_trace(monkeypatch):
    """The zombie-spoof bug: an anonymous 401 must stamp NOTHING."""
    import flask
    completes, starts = [], []
    jr = _patched(monkeypatch, completes, starts)
    monkeypatch.setenv('DCHUB_ADMIN_KEY', REAL_KEY)
    monkeypatch.delenv('ADMIN_SECRET', raising=False)
    app = flask.Flask(__name__)
    with app.test_request_context('/api/jobs/alert-emails', method='POST'):
        err = jr._require_admin_key()
        assert err is not None and err[1] == 401, "anonymous call should 401"
        resp = app.response_class(status=401)
        out = jr._stamp_cron_completion(resp)
        assert out is resp
    assert starts == [], "401 request start-stamped cron_last_run"
    assert completes == [], (
        "an unauthenticated 401 refreshed cron_last_run — the heroic-reprieve "
        "zombie-spoof bug is back")


def test_wrong_key_401_leaves_no_trace(monkeypatch):
    """Same as anonymous, but with a stale (pre-rotation) key — the exact
    heroic-reprieve failure mode."""
    import flask
    completes = []
    jr = _patched(monkeypatch, completes)
    monkeypatch.setenv('DCHUB_ADMIN_KEY', REAL_KEY)
    monkeypatch.delenv('ADMIN_SECRET', raising=False)
    app = flask.Flask(__name__)
    with app.test_request_context('/api/jobs/energy-discovery', method='POST',
                                  headers={'X-API-Key': 'pre-rotation-key'}):
        err = jr._require_admin_key()
        assert err is not None and err[1] == 401
        jr._stamp_cron_completion(app.response_class(status=401))
    assert completes == []


def test_authed_success_still_stamps_ok(monkeypatch):
    """The gate must not break the legitimate path: valid key -> 'ok' stamp."""
    import flask
    completes, starts = [], []
    jr = _patched(monkeypatch, completes, starts)
    monkeypatch.setenv('DCHUB_ADMIN_KEY', REAL_KEY)
    app = flask.Flask(__name__)
    with app.test_request_context('/api/jobs/alert-emails', method='POST',
                                  headers={'X-API-Key': REAL_KEY}):
        assert jr._require_admin_key() is None
        jr._stamp_cron_completion(app.response_class(status=200))
    assert starts == ['alert-emails']
    assert completes == [('alert-emails', 'ok')]


def test_authed_500_still_records_error_status(monkeypatch):
    """A job that authed, started, and 500'd must stay distinguishable from
    one that silently died — the gate must not swallow error statuses."""
    import flask
    completes = []
    jr = _patched(monkeypatch, completes)
    monkeypatch.setenv('DCHUB_ADMIN_KEY', REAL_KEY)
    app = flask.Flask(__name__)
    with app.test_request_context('/api/jobs/energy-discovery', method='POST',
                                  headers={'X-Admin-Key': REAL_KEY}):
        assert jr._require_admin_key() is None
        jr._stamp_cron_completion(app.response_class(status=500))
    assert completes == [('energy-discovery', 'http_500')]


def test_keeper_jobs_declare_tight_intervals():
    """alert-emails runs only via dchub-jobs.yml now; a dropped GH slot must
    trip check_cron_freshness within 2x cadence, not the generic 30h default.
    energy-discovery was retired from the schedule on 2026-08-21 (dead HIFLD
    sources) and must NOT declare an interval — a declared cadence on a job
    nothing fires would page forever — and must be allowlisted in the radar."""
    import routes.jobs_routes as jr
    assert jr._JOB_INTERVALS.get("alert-emails") == 4 * 3600
    assert "energy-discovery" not in jr._JOB_INTERVALS
    import routes.brain_consistency_radar as bcr
    assert "energy-discovery" in bcr._INTENTIONAL_STALE_CRONS
