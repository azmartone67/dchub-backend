"""Integrity master shell (#25) + failover stale-gate test suite (2026-07-24).

All mocked (no DB, no network, never imports main). Contract under test:

  1. ★ THE SAFETY PROPERTY: the PRIMARY can never gate itself. No combination
     of staleness may cause a 503 on Railway — only RENDER / RENDER_SERVICE_ID
     / DCHUB_FAILOVER flip the gate on, and Railway sets none of them.
  2. A stale FAILOVER origin 503s the metrics/ops/admin surfaces (the ones
     where "0 agents · 0 real calls" reads as a real collapse) and keeps
     serving content surfaces (where stale beats an error page).
  3. FAIL-OPEN on probe error — an unknown age must never take out a mirror.
  4. The freshness probe endpoint stays reachable while gated (it is the
     cross-origin signal, so gating it would blind the shell exactly when it
     matters) and leaks no secrets.
  5. _probe_target refuses to probe dchub.cloud (the 07-06 self-request
     footgun) and refuses to probe itself.
  6. Shell kill switch ⇒ 404, never 5xx (a 5xx trips the CF failover breaker).
     Admin gate ⇒ 403.
  7. Lane pass logic is decided-only: gauges (pass=None) never fail a lane.
  8. ★ REGRESSION for the dead DCPI guard: the live 2026-07-24 distribution
     (BUILD=5 CAUTION=17 AVOID=295) must now be reported as degenerate. The
     old guard returned "verdicts adequate" for it, forever.
"""
import flask
import pytest

import routes.failover_stale_gate as gate
import routes.integrity_master_shell as ims


# ── helpers ───────────────────────────────────────────────────────────

def _reset_probe():
    with gate._probe_lock:
        gate._probe["ts"] = 0.0
        gate._probe["age"] = None
        gate._probe["err"] = None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from a Railway-shaped environment (no failover
    markers, no kill switch)."""
    for k in ("RENDER", "RENDER_SERVICE_ID", "DCHUB_FAILOVER",
              "STALE_GATE_DISABLE", "STALE_GATE_MAX_AGE_H",
              "INTEGRITY_SHELL_DISABLE", "INTEGRITY_MIRROR_URL"):
        monkeypatch.delenv(k, raising=False)
    _reset_probe()
    yield
    _reset_probe()


def _app(monkeypatch, age_seconds):
    """Flask app with the gate wired and the freshness probe stubbed."""
    monkeypatch.setattr(gate, "_probe_age_seconds",
                        lambda force=False: age_seconds)
    app = flask.Flask(__name__)
    gate.init_app(app)

    @app.route("/api/v1/admin/flywheel/master-tick")
    def _tick():
        return flask.jsonify(ok=True, agents=75)

    @app.route("/api/v1/facilities/search")
    def _content():
        return flask.jsonify(ok=True, results=[1, 2, 3])

# AUTO-REPAIR: duplicate route '/health' also in main.py:7778 — review and remove one
    @app.route("/health")
    def _health():
        return flask.jsonify(ok=True)

    return app


VERY_STALE = 262.6 * 3600   # the actual 2026-07-24 mirror age
FRESH = 0.1 * 3600


# ── 1. the safety property ────────────────────────────────────────────

def test_primary_never_gates_itself_however_stale(monkeypatch):
    """No failover marker set ⇒ Railway. Even a 262h-stale heartbeat must
    not produce a 503, or a bad probe could take the whole site down."""
    app = _app(monkeypatch, VERY_STALE)
    with app.test_client() as cl:
        assert cl.get("/api/v1/admin/flywheel/master-tick").status_code == 200


@pytest.mark.parametrize("marker,value", [
    ("RENDER", "true"),
    ("RENDER_SERVICE_ID", "srv-abc123"),
    ("DCHUB_FAILOVER", "1"),
])
def test_each_failover_marker_arms_the_gate(monkeypatch, marker, value):
    monkeypatch.setenv(marker, value)
    app = _app(monkeypatch, VERY_STALE)
    with app.test_client() as cl:
        assert cl.get("/api/v1/admin/flywheel/master-tick").status_code == 503


# ── 2. surgical scope ─────────────────────────────────────────────────

def test_stale_mirror_503s_metrics_but_still_serves_content(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    app = _app(monkeypatch, VERY_STALE)
    with app.test_client() as cl:
        r = cl.get("/api/v1/admin/flywheel/master-tick")
        assert r.status_code == 503
        assert r.get_json()["error"] == "stale_failover_origin"
        assert r.headers["X-DC-Hub-Stale-Origin"].startswith("262")
        # Content still flows: during a real Railway outage, old facility
        # data beats an error page.
        assert cl.get("/api/v1/facilities/search").status_code == 200


def test_fresh_mirror_is_not_gated(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    app = _app(monkeypatch, FRESH)
    with app.test_client() as cl:
        assert cl.get("/api/v1/admin/flywheel/master-tick").status_code == 200


def test_kill_switch_disarms_the_gate(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("STALE_GATE_DISABLE", "1")
    app = _app(monkeypatch, VERY_STALE)
    with app.test_client() as cl:
        assert cl.get("/api/v1/admin/flywheel/master-tick").status_code == 200


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("STALE_GATE_MAX_AGE_H", "999")
    app = _app(monkeypatch, VERY_STALE)
    with app.test_client() as cl:
        assert cl.get("/api/v1/admin/flywheel/master-tick").status_code == 200


# ── 3. fail-open ──────────────────────────────────────────────────────

def test_probe_failure_fails_open(monkeypatch):
    """age=None means 'we could not tell', which must never be treated as
    'stale' — a probe hiccup must not 503 a healthy mirror."""
    monkeypatch.setenv("RENDER", "true")
    app = _app(monkeypatch, None)
    with app.test_client() as cl:
        assert cl.get("/api/v1/admin/flywheel/master-tick").status_code == 200


# ── 4. the freshness endpoint ─────────────────────────────────────────

def test_freshness_endpoint_answers_while_gated_and_leaks_nothing(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    app = _app(monkeypatch, VERY_STALE)
    with app.test_client() as cl:
        r = cl.get("/api/v1/ops/origin-freshness")
        assert r.status_code == 200          # exempt from its own gate
        body = r.get_json()
        assert body["role"] == "failover"
        assert body["stale"] is True
        assert body["gate_active"] is True
        assert round(body["data_age_hours"]) == 263
        # only a role + an age; nothing resembling a connection string
        blob = str(body).lower()
        for leak in ("postgres://", "postgresql://", "password", "@neon", "sslmode"):
            assert leak not in blob


def test_health_is_never_gated(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    app = _app(monkeypatch, VERY_STALE)
    with app.test_client() as cl:
        assert cl.get("/health").status_code == 200


# ── 5. probe-target guard rails ───────────────────────────────────────

def test_probe_target_refuses_the_public_edge(monkeypatch):
    """Probing dchub.cloud would re-enter this same Flask process through
    the edge — the 2026-07-06 pool-saturation incident."""
    monkeypatch.setenv("INTEGRITY_MIRROR_URL", "https://dchub.cloud")
    app = flask.Flask(__name__)
    with app.test_request_context("/", base_url="https://x.up.railway.app"):
        assert ims._probe_target() is None


def test_probe_target_refuses_self(monkeypatch):
    monkeypatch.setenv("INTEGRITY_MIRROR_URL",
                       "https://dchub-backend-render.onrender.com")
    app = flask.Flask(__name__)
    with app.test_request_context(
            "/", base_url="https://dchub-backend-render.onrender.com"):
        assert ims._probe_target() is None


def test_primary_probes_the_render_mirror():
    app = flask.Flask(__name__)
    with app.test_request_context(
            "/", base_url="https://dchub-backend-production.up.railway.app"):
        assert ims._probe_target() == ims._RENDER_ORIGIN


# ── 6. shell gating ───────────────────────────────────────────────────

def _shell_app():
    app = flask.Flask(__name__)
    app.register_blueprint(ims.integrity_master_shell_bp)
    return app


def test_shell_kill_switch_is_404_not_5xx(monkeypatch):
    """A 5xx here would look like a dead origin to proxyWithRetry and bounce
    traffic onto the very mirror this shell exists to distrust."""
    monkeypatch.setenv("INTEGRITY_SHELL_DISABLE", "1")
    with _shell_app().test_client() as cl:
        assert cl.get("/api/v1/admin/integrity/master-tick").status_code == 404
        assert cl.get("/admin/integrity").status_code == 404


def test_shell_requires_admin_key(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    with _shell_app().test_client() as cl:
        assert cl.get("/api/v1/admin/integrity/master-tick").status_code == 403
        assert cl.get("/api/v1/admin/integrity/master-tick",
                      headers={"X-Admin-Key": "wrong"}).status_code == 403


def test_empty_admin_key_does_not_open_the_door(monkeypatch):
    """Both sides empty must not compare equal."""
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    with _shell_app().test_client() as cl:
        assert cl.get("/api/v1/admin/integrity/master-tick").status_code == 403


# ── 7. lane aggregation ───────────────────────────────────────────────

def test_gauges_do_not_fail_a_lane():
    assert ims._lane_verdict(
        [ims._check("a", "a", True, ""), ims._check("b", "b", None, "")]) is True


def test_all_gauges_is_not_a_pass():
    """A lane that decided nothing has proven nothing."""
    assert ims._lane_verdict([ims._check("a", "a", None, "")]) is None


def test_undetermined_critical_check_blocks_a_green_lane():
    """★ The shell must not commit the sin it was built to catch. Lane 1
    initially rendered PASS while its own detail said the mirror was
    unreachable — green on the strength of the checks that happened to
    succeed. An undetermined CRITICAL check now yields '?', not True."""
    checks = [
        ims._check("self", "self fresh", True, "fine"),
        ims._check("mirror", "mirror fresh", None, "unreachable", critical=True),
    ]
    assert ims._lane_verdict(checks) is None


def test_critical_check_that_decides_still_governs_normally():
    ok = [ims._check("m", "mirror", True, "", critical=True)]
    bad = [ims._check("m", "mirror", False, "", critical=True)]
    assert ims._lane_verdict(ok) is True
    assert ims._lane_verdict(bad) is False


def test_commit_is_read_from_either_platform(monkeypatch):
    """Render injects RENDER_GIT_COMMIT, Railway RAILWAY_GIT_COMMIT_SHA.
    /api/v1/version's `build` is a hand-maintained constant reading 91 on
    BOTH origins, so it can never reveal drift — this can."""
    for var in ("RENDER_GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA",
                "SOURCE_VERSION", "GIT_COMMIT"):
        monkeypatch.delenv(var, raising=False)
    assert gate._commit() is None
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "8cac23ca1234567890")
    assert gate._commit() == "8cac23c"
    monkeypatch.setenv("RENDER_GIT_COMMIT", "deadbeef999")
    assert gate._commit() == "deadbee"   # Render wins on the mirror


def test_commit_is_published_in_the_freshness_probe(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc1234def")
    app = _app(monkeypatch, FRESH)
    with app.test_client() as cl:
        assert cl.get("/api/v1/ops/origin-freshness").get_json()["commit"] == "abc1234"


def test_undetermined_lane_is_not_counted_green(monkeypatch):
    """A '?' lane must not inflate lanes_pass — that number is the headline."""
    def _lane(c):
        return [ims._check("m", "mirror", None, "unreachable", critical=True)]

    monkeypatch.setattr(ims, "_LANES", [("x", "x", _lane, "act")])
    monkeypatch.setattr(ims, "_conn", lambda: None)
    payload = ims._run_tick()
    assert payload["lanes"][0]["pass"] is None
    assert payload["lanes_pass"] == 0


def test_lane_crash_never_sinks_the_tick(monkeypatch):
    def _boom(c):
        raise RuntimeError("lane exploded")

    monkeypatch.setattr(ims, "_LANES", [("x", "x", _boom, "act")])
    monkeypatch.setattr(ims, "_conn", lambda: None)
    payload = ims._run_tick()
    assert payload["ok"] is True
    assert payload["lanes"][0]["checks"][0]["detail"].startswith("lane exploded")


# ── 8. the DCPI regression ────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return None

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self._row)

    def commit(self):
        return None


def _run_guard(monkeypatch, builds, cautions, avoids):
    import dchub_self_heal as sh
    monkeypatch.setattr(sh, "DATABASE_URL", "postgres://stub", raising=False)
    total = builds + cautions + avoids
    monkeypatch.setattr(
        sh, "_conn", lambda: _FakeConn((builds, avoids, cautions, total)))
    return sh.fix_relax_verdict_thresholds()


def test_live_0724_distribution_is_now_reported_degenerate(monkeypatch):
    """BUILD=5 CAUTION=17 AVOID=295 — the exact live distribution. The old
    guard (builds+avoids >= 5 → 'adequate') returned OK for this forever,
    which is why the media arm had five subjects to write about."""
    ok, msg = _run_guard(monkeypatch, builds=5, cautions=17, avoids=295)
    assert ok is False
    assert "DEGENERATE" in msg
    assert "BUILD=5" in msg and "AVOID=295" in msg


def test_degenerate_spread_does_not_rewrite_verdicts(monkeypatch):
    """Relabelling published verdicts to fix a histogram manufactures signal.
    The guard must report and stop.

    r-verdict-one-band (2026-08-08): this used to assert the message named
    DCPI_RELAX_VERDICTS_ARM, because the rewrite existed and that flag gated
    it. The rewrite is now DELETED, so there is no arming flag to name — the
    detector cannot relabel under any environment. That is strictly stronger
    than the gate it replaces, so this asserts the stronger property: the
    function contains no verdict write at all. (The flag still means
    something in routes/dcpi_excess_master_shell.py, whose honesty-invariant
    lane asserts it is disarmed; that lane is unaffected.)
    """
    import ast
    import inspect

    import dchub_self_heal as sh

    ok, msg = _run_guard(monkeypatch, builds=5, cautions=17, avoids=295)
    assert ok is False
    assert "relaxed" not in msg.lower()

    src = ast.unparse(ast.parse(
        inspect.getsource(sh.fix_relax_verdict_thresholds))).lower()
    assert "update market_power_scores" not in src, (
        "the spread detector writes verdicts again — it must report and stop"
    )


def test_healthy_spread_passes(monkeypatch):
    ok, msg = _run_guard(monkeypatch, builds=90, cautions=120, avoids=107)
    assert ok is True
    assert "healthy" in msg


def test_monoculture_fails_even_with_many_builds(monkeypatch):
    """>=10 BUILD is necessary but not sufficient — 90% in one bucket is
    still an index that has stopped disagreeing with itself."""
    ok, msg = _run_guard(monkeypatch, builds=12, cautions=8, avoids=300)
    assert ok is False


def test_no_rows_is_a_finding_not_a_pass(monkeypatch):
    ok, msg = _run_guard(monkeypatch, builds=0, cautions=0, avoids=0)
    assert ok is False
