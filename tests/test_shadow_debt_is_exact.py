"""The shadowed-route check in scripts/app_contract_gate.py must be able to FAIL.

A rule+method pair with two registrations lets one of them lose silently, and a
hand-built fixture can register the loser alone and grade it green. That shipped
/llms-full.txt without its policy block (#4996 -> #5016 -> #5034). The gate boots
the real app and compares shadowed(app) to tests/app_contract.json's
"shadowed_debt" EXACTLY; these cases drive that comparison without a boot, so
they run in unit-tests. The booted run itself is app-contract-gate's.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import app_contract_gate as g  # noqa: E402  (stdlib-only at import time)

PAIR = {"GET /team": ["redirects_404_killer.redir_team", "team_landing.team"]}


def test_a_new_duplicate_fails_and_names_itself():
    failures = g.classify_shadow_drift({}, PAIR)
    assert len(failures) == 1, failures
    assert failures[0].startswith("NEW SHADOWED ROUTE(S)"), failures[0]
    assert "GET /team -> ['redirects_404_killer.redir_team', 'team_landing.team']" in failures[0]


def test_a_fixed_duplicate_left_pinned_fails():
    failures = g.classify_shadow_drift(PAIR, {})
    assert len(failures) == 1, failures
    assert failures[0].startswith("FIXED SHADOW STILL PINNED: GET /team"), failures[0]


def test_a_different_pair_on_a_pinned_rule_is_new():
    live = {"GET /team": ["team_landing.team", "zzz.other_team"]}
    failures = g.classify_shadow_drift(PAIR, live)
    assert len(failures) == 1 and failures[0].startswith("NEW SHADOWED ROUTE(S)"), failures
    assert "zzz.other_team" in failures[0]


def test_exactly_the_pinned_debt_passes():
    assert g.classify_shadow_drift(PAIR, dict(PAIR)) == []
    assert g.classify_shadow_drift({}, {}) == []


def test_the_committed_debt_has_the_shape_shadowed_emits():
    """shadowed() emits {"METHOD /rule": sorted([>=2 endpoints])} and the
    comparison is list equality, so an unsorted or malformed pin can never
    match — and a non-dict would raise inside a gate that must not hang."""
    with open(os.path.join(ROOT, "tests", "app_contract.json"), encoding="utf-8") as fh:
        base = json.load(fh)
    assert "max_shadowed_routes" not in base, "the count ratchet is retired"
    debt = base["shadowed_debt"]
    assert isinstance(debt, dict), type(debt)
    for key, eps in debt.items():
        method, _, rule = key.partition(" ")
        assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"} and rule.startswith("/"), key
        assert isinstance(eps, list) and len(set(eps)) >= 2 and eps == sorted(eps), (key, eps)


def _run_gate_on(monkeypatch, tmp_path, capsys, debt):
    """main_() on a two-blueprint app that shadows GET /dup — no real boot."""
    import flask
    app = flask.Flask("shadow_wiring")
    for name in ("first", "second"):
        bp = flask.Blueprint(name, name)
        bp.add_url_rule("/dup", "dup", lambda: "x")
        app.register_blueprint(bp)
    base = {"min_routes": 0, "min_blueprints": 0, "contract_routes": []}
    if debt is not None:
        base["shadowed_debt"] = debt
    monkeypatch.setattr(g, "boot", lambda: (app, 0.0))
    monkeypatch.setattr(g, "load_baseline", lambda: base)
    monkeypatch.setattr(g, "ROUTE_MAP", str(tmp_path / "absent.json"))
    monkeypatch.setattr(sys, "argv", ["app_contract_gate.py"])
    rc = g.main_()
    return rc, capsys.readouterr().out


def test_the_gate_itself_fails_on_a_new_duplicate(monkeypatch, tmp_path, capsys):
    """The classifier is only a guard if main_() calls it. This runs main_():
    an unpinned duplicate must be one of its failures, by name."""
    rc, out = _run_gate_on(monkeypatch, tmp_path, capsys, {})
    assert rc == 1, out
    assert "NEW SHADOWED ROUTE(S)" in out and "GET /dup -> ['first.dup', 'second.dup']" in out, out


def test_the_gate_accepts_the_same_duplicate_once_pinned(monkeypatch, tmp_path, capsys):
    """Control: pinned exactly, the shadow is not reported. (rc is still 1 —
    the fake app serves no evidence route — so the message is what's checked.)"""
    _, out = _run_gate_on(monkeypatch, tmp_path, capsys,
                          {"GET /dup": ["first.dup", "second.dup"]})
    assert "booted in 0.0s" in out and "1 shadowed" in out, out
    assert "SHADOWED ROUTE" not in out and "FIXED SHADOW" not in out, out


def test_a_baseline_without_the_debt_key_is_strict(monkeypatch, tmp_path, capsys):
    """No shadowed_debt at all must mean "nothing is pinned", never "all is"."""
    rc, out = _run_gate_on(monkeypatch, tmp_path, capsys, None)
    assert rc == 1 and "NEW SHADOWED ROUTE(S)" in out, out
