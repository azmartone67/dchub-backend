"""
tests/test_admin_gate_fail_closed.py — admin gates must not disable themselves
(2026-08-15).

WHAT WENT WRONG. `routes/brain_layer16_self_critique.py` gated its admin write
endpoint with:

    if request.method == "POST" and _ADMIN_KEY:
        ...
        return jsonify(error="unauthorized"), 401

Two independent fail-OPEN holes in one line:

  1. `and _ADMIN_KEY` — _ADMIN_KEY is an IMPORT-TIME snapshot of
     os.environ["DCHUB_ADMIN_KEY"]. On any process whose env lacks that var the
     snapshot is "" and the whole auth block is skipped. dchub-worker was in
     exactly that state on 2026-08-08, and this route is worker-delegated
     (main.py _WORKER_PROXY_POST_PATHS), so it was reachable UNAUTHENTICATED
     from the public internet while its fail-CLOSED siblings returned 403.
     A gate that disables itself precisely when the box is misconfigured is
     not a gate.

  2. `request.method == "POST"` — the route is registered
     methods=["POST", "GET"] and the body runs identically for GET, so GET was
     ungated on EVERY process regardless of the env.

This file locks both shapes out. It asserts on the AST, never on source text:
the defect is a name reference, and a literal-string audit cannot see a gate
that assigns through a variable (that miss has now cost this repo seven
separate findings).

THE RATCHET. The four brain_layer* routes are FIXED and must stay at zero
forever. A repo-wide scan found the same self-disabling shape at 46 further
sites in 36 files — a latent class, not a live exposure: on the web process
DCHUB_ADMIN_KEY IS set, so those gates currently hold. They are frozen in
_BASELINE below so that NEW ones cannot land, and so the backlog is a number
somebody can drive to zero rather than a rumour. If you fix some, lower the
number; the test fails on a DECREASE too, on purpose — a baseline nobody is
required to update stops describing anything.
"""
import ast
import pathlib

import pytest

flask = pytest.importorskip("flask")

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES = ROOT / "routes"

# Fixed 2026-08-15. These must never regress — no baseline entry, ever.
_FIXED = (
    "brain_layer14_causal.py",
    "brain_layer15_auto_action.py",
    "brain_layer16_self_critique.py",
    "brain_layer18_memory_consolidation.py",
)

# Frozen 2026-08-15. file -> count of self-disabling admin gates.
_BASELINE = {
    "autopilot_outcomes.py": 1,
    "backlink_hunter.py": 1,
    "brain_layer23_lifecycle.py": 3,
    "brain_layer7_evolving.py": 1,
    "brain_layer8_orchestrator.py": 1,
    "brain_learning.py": 1,
    "brain_memory.py": 1,
    "brain_metric_targets.py": 1,
    "brain_narrative.py": 1,
    "brain_pr_opener.py": 1,
    "broadcast.py": 1,
    "cf_purge.py": 1,
    "citation_hunter.py": 1,
    "competitor_intel.py": 1,
    "data_freshness_radar.py": 1,
    "dchub_media_hub.py": 1,
    "exclusive_listings.py": 1,
    "grid_fiber_usage_radar.py": 1,
    "hosting_capacity_ingest.py": 1,
    "industry_events.py": 1,
    "lp_alerts_cron.py": 2,
    "market_alerts.py": 1,
    "market_deep_dive.py": 2,
    "marketing_engine.py": 1,
    "metric_observatory.py": 1,
    "outcome_verifier.py": 1,
    "outreach_cap_exceeded.py": 1,
    "pattern_growth.py": 1,
    "radar_history.py": 1,
    "site_sentinel.py": 6,
    "temporal_capture.py": 1,
    "tenant_directory.py": 1,
    "upgrade_nudger.py": 1,
    "weekly_digest.py": 1,
    "weekly_movement_digest.py": 1,
    "winback_outreach.py": 2,
}


def _returns_401(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Return) and n.value is not None:
            for c in ast.walk(n.value):
                if isinstance(c, ast.Constant) and c.value == 401:
                    return True
    return False


def _env_snapshot_names(tree) -> set:
    """Module-level names bound to an os.environ read of DCHUB_ADMIN_KEY."""
    names = set()
    for n in tree.body:
        if isinstance(n, ast.Assign):
            dumped = ast.dump(n.value)
            if "DCHUB_ADMIN_KEY" in dumped and "environ" in dumped:
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
    return names


def _scan():
    """-> (self_disabling, method_conditional, files_parsed, gates_seen)."""
    self_disabling, method_conditional = [], []
    files_parsed = gates_seen = 0
    for path in sorted(ROUTES.glob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except Exception:
            continue
        files_parsed += 1
        snapshots = _env_snapshot_names(tree)
        for n in ast.walk(tree):
            if not isinstance(n, ast.If):
                continue
            if not any(_returns_401(b) for b in n.body):
                continue
            gates_seen += 1
            if not isinstance(n.test, ast.BoolOp) or not isinstance(n.test.op, ast.And):
                continue
            for v in n.test.values:
                if isinstance(v, ast.Name) and v.id in snapshots:
                    self_disabling.append((path.name, n.lineno, v.id))
                if isinstance(v, ast.Compare):
                    for c in ast.walk(v):
                        if isinstance(c, ast.Attribute) and c.attr == "method":
                            method_conditional.append((path.name, n.lineno))
    return self_disabling, method_conditional, files_parsed, gates_seen


# ════════════════════════════════════════════════════════════════════
#  VACUITY — this guard's likeliest failure is finding nothing at all
# ════════════════════════════════════════════════════════════════════
def test_scan_is_not_vacuous():
    """If the scan silently stops seeing routes or gates, every assertion below
    passes for the wrong reason. Measured 2026-08-15: 768 files, 646 gates."""
    _, _, files_parsed, gates_seen = _scan()
    assert files_parsed > 400, f"only parsed {files_parsed} route modules"
    assert gates_seen > 300, f"only found {gates_seen} 401-returning gates"


# ════════════════════════════════════════════════════════════════════
#  THE FIXED FOUR — zero, forever
# ════════════════════════════════════════════════════════════════════
def test_fixed_routes_have_no_self_disabling_gate():
    self_disabling, _, _, _ = _scan()
    offenders = [x for x in self_disabling if x[0] in _FIXED]
    assert not offenders, (
        "a self-disabling `and <DCHUB_ADMIN_KEY snapshot>` gate came back in a "
        f"route that was fixed on 2026-08-15: {offenders}. Use "
        "internal_auth.require_internal_or_admin — it is fail-closed and reads "
        "os.environ at request time."
    )


def test_fixed_routes_do_not_gate_on_method():
    method_conditional, = (_scan()[1],)
    offenders = [x for x in method_conditional if x[0] in _FIXED]
    assert not offenders, (
        f"auth conditioned on request.method in {offenders}. These routes accept "
        "both POST and GET and run the same body for each — gate every method "
        "the route accepts."
    )


# ════════════════════════════════════════════════════════════════════
#  THE RATCHET — no NEW self-disabling gates anywhere in routes/
# ════════════════════════════════════════════════════════════════════
def test_no_new_self_disabling_gates():
    self_disabling, _, _, _ = _scan()
    actual = {}
    for name, _lineno, _var in self_disabling:
        actual[name] = actual.get(name, 0) + 1

    grew, shrank, appeared = [], [], []
    for name, count in sorted(actual.items()):
        base = _BASELINE.get(name)
        if base is None:
            appeared.append(f"{name}: {count} new")
        elif count > base:
            grew.append(f"{name}: {base} -> {count}")
        elif count < base:
            shrank.append(f"{name}: {base} -> {count}")
    for name, base in sorted(_BASELINE.items()):
        if name not in actual:
            shrank.append(f"{name}: {base} -> 0")

    assert not appeared and not grew, (
        "NEW self-disabling admin gate(s). `if _ADMIN_KEY and provided != "
        "_ADMIN_KEY: return 401` skips auth entirely when the env var is unset "
        "— which is what a misconfigured process looks like. Use "
        "internal_auth.require_internal_or_admin instead.\n"
        f"new files: {appeared}\ngrew: {grew}"
    )
    assert not shrank, (
        "Self-disabling gates were REMOVED but the baseline in this file still "
        "claims them. Lower the numbers in _BASELINE so it keeps describing "
        f"reality.\n{shrank}"
    )


# ════════════════════════════════════════════════════════════════════
#  BEHAVIOUR — the four routes actually 401 with no credential
# ════════════════════════════════════════════════════════════════════
_ROUTES = (
    ("routes.brain_layer14_causal", "brain_layer14_bp",
     "/api/v1/brain/causal/analyze"),
    ("routes.brain_layer15_auto_action", "brain_layer15_bp",
     "/api/v1/brain/auto-action/run"),
    ("routes.brain_layer16_self_critique", "brain_layer16_bp",
     "/api/v1/brain/self-critique/run"),
    ("routes.brain_layer18_memory_consolidation", "brain_layer18_bp",
     "/api/v1/brain/lessons/consolidate"),
)


@pytest.mark.parametrize("module,bp_name,path", _ROUTES)
@pytest.mark.parametrize("method", ["get", "post"])
def test_route_is_401_without_a_credential(monkeypatch, module, bp_name, path, method):
    """Both verbs, no credential -> 401, and it happens BEFORE any table
    creation or model work. GET is listed explicitly because GET was the hole
    that stayed open on every process, key or no key."""
    mod = pytest.importorskip(module)
    # A key IS configured: this proves the gate rejects a missing credential,
    # not merely that the box is unconfigured.
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "a-real-key-is-set")
    app = flask.Flask(__name__)
    app.register_blueprint(getattr(mod, bp_name))
    resp = getattr(app.test_client(), method)(path)
    assert resp.status_code == 401, (
        f"{method.upper()} {path} returned {resp.status_code}, not 401 — "
        "this endpoint is reachable without a credential"
    )


@pytest.mark.parametrize("module,bp_name,path", _ROUTES)
@pytest.mark.parametrize("method", ["get", "post"])
def test_route_is_401_when_the_env_has_no_key(monkeypatch, module, bp_name, path, method):
    """THE REGRESSION ITSELF: with DCHUB_ADMIN_KEY absent from the process env
    — dchub-worker on 2026-08-08 — the old gate vanished and the body ran for
    anyone. Fail CLOSED instead."""
    mod = pytest.importorskip(module)
    for var in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
                "INTERNAL_WORKER_SECRET"):
        monkeypatch.delenv(var, raising=False)
    app = flask.Flask(__name__)
    app.register_blueprint(getattr(mod, bp_name))
    resp = getattr(app.test_client(), method)(path)
    assert resp.status_code == 401, (
        f"{method.upper()} {path} returned {resp.status_code} with NO admin key "
        "in the env — the gate disabled itself instead of denying"
    )
