"""Failover CODE-staleness gate (2026-08-28).

THE DEFECT THIS WAS BUILT FROM — read before changing anything here.

On 2026-08-28 the canonical `GET /api/v1/sponsorships/active` returned the
PRE-#3256 three-slot contract from `render-failover`:

    cf-cache-status: HIT      age: 115 … 167     (declared max-age: 60)
    x-dc-hub-served-by: render-failover
    {"digest_banner":null,"digest_featured":null,"site_banner":null}

The same URL with any query string missed cache, reached Railway, and
returned the correct six slots 10/10. Three of the six — facility_module,
market_module, ai_source_block — are the slots the two SOLD advertising
products render into, so the mirror was publishing an ad-inventory
contract that no longer existed.

★ The existing stale gate could not catch it. That gate keys on DATA age
(`_probe_age_seconds` vs STALE_GATE_MAX_AGE_H). The mirror's data was
fine; its CODE was old. A data-age gate is structurally blind to that.

So the contract under test here is ROLE-ONLY: on a failover origin, a
code-dependent path 503s regardless of data age, regardless of the probe,
and even when the probe says the data is perfectly fresh.

Mocked throughout — no DB, no network, never imports main.
"""
import flask
import pytest

import routes.failover_stale_gate as gate


def _reset_probe():
    with gate._probe_lock:
        gate._probe["ts"] = 0.0
        gate._probe["age"] = None
        gate._probe["err"] = None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts Railway-shaped: no failover markers, no kill switch."""
    for k in ("RENDER", "RENDER_SERVICE_ID", "DCHUB_FAILOVER",
              "STALE_GATE_DISABLE", "STALE_GATE_MAX_AGE_H"):
        monkeypatch.delenv(k, raising=False)
    _reset_probe()
    yield
    _reset_probe()


# The gate is a before_request hook keyed on request.path, so the fixture
# only needs each path to EXIST and answer 200 when it is reached.
#
# Registered via add_url_rule from a list rather than with @app.route
# decorators, deliberately: a decorator here would be a second literal
# definition of a real production route, which regression_lint flags as
# duplicate-route — correctly, since that rule cannot tell a test fixture
# from a genuinely duplicated service surface. Keeping the paths as data
# also lets one builder serve every case below.
_FIXTURE_PATHS = (
    "/api/v1/sponsorships/active",
    "/api/v1/sponsorships/block",
    "/api/v1/facilities/search",
    "/health",
)


def _app(monkeypatch, age_seconds=0.0, probe=None):
    """App with the gate wired and the freshness probe stubbed.

    age_seconds defaults to 0 — i.e. PERFECTLY FRESH DATA. That default is
    the point of this suite: the code gate must fire anyway.

    Pass `probe` to substitute the probe callable itself (used to prove the
    code gate never consults it).
    """
    monkeypatch.setattr(gate, "_probe_age_seconds",
                        probe or (lambda force=False: age_seconds))
    app = flask.Flask(__name__)
    gate.init_app(app)

    for i, path in enumerate(_FIXTURE_PATHS):
        app.add_url_rule(path, f"fixture_{i}", lambda: flask.jsonify(ok=True))

    return app.test_client()


# ── 1. the safety property: the PRIMARY can never gate itself ─────────

def test_primary_serves_the_sponsorship_contract_with_fresh_data(monkeypatch):
    c = _app(monkeypatch, age_seconds=0.0)
    assert c.get("/api/v1/sponsorships/active").status_code == 200


def test_primary_serves_the_sponsorship_contract_even_when_data_is_ancient(monkeypatch):
    """No amount of staleness may gate Railway — it sets none of the markers."""
    c = _app(monkeypatch, age_seconds=90 * 24 * 3600.0)
    assert c.get("/api/v1/sponsorships/active").status_code == 200


# ── 2. the new contract: role alone, no age condition ────────────────

@pytest.mark.parametrize("marker,value", [
    ("RENDER", "true"),
    ("RENDER_SERVICE_ID", "srv-d86g7g6gvqtc73dlpojg"),
    ("DCHUB_FAILOVER", "1"),
])
def test_failover_refuses_the_sponsorship_contract_on_role_alone(
        monkeypatch, marker, value):
    """★ THE REGRESSION. Data age 0 — the old gate would pass this through."""
    monkeypatch.setenv(marker, value)
    c = _app(monkeypatch, age_seconds=0.0)
    r = c.get("/api/v1/sponsorships/active")
    assert r.status_code == 503, (
        f"{marker} origin served the sponsorship contract with fresh data — "
        "this is exactly the 2026-08-28 defect: a mirror publishing an ad "
        "slot set that no longer matches the primary."
    )
    assert r.get_json()["error"] == "code_dependent_surface_on_failover_origin"
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-DC-Hub-Code-Dependent-Gate"] == "1"


def test_the_gate_does_not_consult_the_freshness_probe_at_all(monkeypatch):
    """Role-only means role-only: a probe that would explode must not run.

    If the implementation ever moves this branch below the age probe, this
    test fails loudly instead of the gate silently becoming age-conditional.
    """
    monkeypatch.setenv("RENDER", "true")

    def _explode(force=False):
        raise AssertionError("the code gate consulted the data-age probe")

    c = _app(monkeypatch, probe=_explode)
    assert c.get("/api/v1/sponsorships/active").status_code == 503


def test_whole_sponsorship_prefix_is_covered_not_just_active(monkeypatch):
    """#3284 added /block. Prefix coverage means new siblings inherit it."""
    monkeypatch.setenv("RENDER", "true")
    c = _app(monkeypatch, age_seconds=0.0)
    assert c.get("/api/v1/sponsorships/block").status_code == 503


# ── 3. scope discipline — this must not become a blanket outage ───────

def test_content_still_serves_from_the_mirror(monkeypatch):
    """The whole point of a failover origin. Old infra data beats an error."""
    monkeypatch.setenv("RENDER", "true")
    c = _app(monkeypatch, age_seconds=0.0)
    assert c.get("/api/v1/facilities/search").status_code == 200


def test_liveness_still_serves_so_the_host_does_not_kill_the_mirror(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    c = _app(monkeypatch, age_seconds=0.0)
    assert c.get("/health").status_code == 200


def test_kill_switch_reverts_to_serving(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("STALE_GATE_DISABLE", "1")
    c = _app(monkeypatch, age_seconds=0.0)
    assert c.get("/api/v1/sponsorships/active").status_code == 200


def test_freshness_probe_discloses_the_gated_prefixes(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(gate, "_probe_age_seconds", lambda force=False: 0.0)
    state = gate.origin_state()
    assert state["code_dependent_gated"] is True
    assert "/api/v1/sponsorships" in state["code_dependent_prefixes"]


# ── 4. must-fail controls ────────────────────────────────────────────
# Each asserts the check above it is capable of failing. A control that
# cannot detect its own defect makes the suite vacuous.

def test_control_role_gate_would_catch_an_empty_prefix_list():
    """If _CODE_DEPENDENT_PREFIXES were emptied, nothing would be gated."""
    saved = gate._CODE_DEPENDENT_PREFIXES
    try:
        gate._CODE_DEPENDENT_PREFIXES = ()
        assert not gate._path_is_code_dependent("/api/v1/sponsorships/active"), (
            "MUST-FAIL CONTROL DID NOT APPLY — _path_is_code_dependent still "
            "matched with an empty prefix tuple, so it is not reading the tuple."
        )
    finally:
        gate._CODE_DEPENDENT_PREFIXES = saved


def test_control_exempt_paths_really_do_win_over_the_prefix():
    """Guards the ordering inside _path_is_code_dependent."""
    saved = gate._CODE_DEPENDENT_PREFIXES
    try:
        # Force a collision: gate a prefix that an exempt path sits under.
        gate._CODE_DEPENDENT_PREFIXES = ("/api/v1/ops",)
        assert not gate._path_is_code_dependent("/api/v1/ops/origin-freshness"), (
            "MUST-FAIL CONTROL DID NOT APPLY — the exemption list is not "
            "consulted before the prefix list, so the freshness probe could "
            "be gated exactly when it is needed to diagnose the gate."
        )
        assert gate._path_is_code_dependent("/api/v1/ops/deadman")
    finally:
        gate._CODE_DEPENDENT_PREFIXES = saved
