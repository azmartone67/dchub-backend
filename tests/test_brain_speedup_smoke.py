"""Smoke tests for brain speedup v1 — fast-QA (B) + action-queue (D).

No network, no DB: assert the modules import cleanly, the blueprints expose the
expected routes, and the pure helpers behave. (The DB-touching paths are
exercised live post-deploy, per the verify-before-claiming discipline.)
"""
import importlib

import pytest

# The minimal pre-merge unit-tests env installs only pytest (no flask); these
# modules import flask at top level, so skip cleanly there and run where flask
# is present (local dev).
pytest.importorskip("flask")


def test_fast_qa_blueprint_routes():
    m = importlib.import_module("routes.brain_fast_qa")
    # Blueprint route registration is deferred until app.register_blueprint; assert
    # the view funcs + the curated URL list + base instead.
    assert hasattr(m, "brain_fast_qa_bp")
    assert hasattr(m, "fast_sweep") and hasattr(m, "fast_sweep_status")
    assert hasattr(m, "run_fast_sweep")
    assert m._BASE == "https://dchub.cloud"
    assert "/" in m._PUBLIC_URLS and "/pricing" in m._PUBLIC_URLS
    # Was "<= 12 (1-replica safety)". The backend runs 2 replicas (railway.json
    # numReplicas); the real constraint is the sweep's time budget, pinned below.
    assert len(m._PUBLIC_URLS) <= 14, "keep the curated list small (sweep time budget)"


def test_fast_qa_worst_case_sweep_fits_the_worker_timeout():
    """A sweep runs synchronously inside one request. If every URL times out —
    the site is down, the one run that matters most — gunicorn kills the request
    at --timeout and the findings it was about to persist are lost. Both sides
    of the budget are read from source, not copied here."""
    import ast as _ast, inspect, pathlib, re
    m = importlib.import_module("routes.brain_fast_qa")
    root = pathlib.Path(m.__file__).resolve().parent.parent
    hit = re.search(r"--timeout\s+(\d+)", (root / "start_web.sh").read_text(encoding="utf-8"))
    assert hit, "could not read gunicorn --timeout from start_web.sh"
    worker_timeout = int(hit.group(1))
    fresh = None
    tree = _ast.parse((root / "routes" / "brain_fast_qa.py").read_text(encoding="utf-8"))
    for n in _ast.walk(tree):
        if (isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
                and n.func.id == "_internal" and n.args
                and isinstance(n.args[0], _ast.Constant)
                and n.args[0].value == "/api/v1/freshness/radar"):
            fresh = inspect.signature(m._internal).parameters["timeout"].default
            if len(n.args) > 1:
                fresh = _ast.literal_eval(n.args[1])
            for kw in n.keywords:
                if kw.arg == "timeout":
                    fresh = _ast.literal_eval(kw.value)
    assert fresh, "could not find the freshness read the sweep also spends time on"
    worst = sum(m.url_timeout(p) for p in m._PUBLIC_URLS) + fresh
    assert worst <= worker_timeout * 0.8, (
        "worst-case sweep %ss leaves under 20%% headroom on gunicorn --timeout %ss"
        % (worst, worker_timeout))


def test_fast_qa_freshness_parser_real_shape():
    m = importlib.import_module("routes.brain_fast_qa")
    # LIVE shape (verified 2026-06-16): radar.domains[], items keyed
    # domain/source_table/status/age_hours/sla_hours.
    radar = {"as_of": "x", "domains": [
        {"domain": "brain", "source_table": "brain_meta",
         "age_hours": 0.0, "sla_hours": 2, "status": "fresh"},
        {"domain": "facilities", "source_table": "discovered_facilities",
         "age_hours": 400, "sla_hours": 336, "status": "stale"},
        # status healthy but age>sla → still caught by the age fallback
        {"domain": "iso", "source_table": "iso_x",
         "age_hours": 50, "sla_hours": 24},
    ]}
    breached = {b[0] for b in m._iter_freshness_items(radar)}
    assert "facilities" in breached, "non-fresh status must breach"
    assert "iso" in breached, "age>sla must breach even without status"
    assert "brain" not in breached, "fresh + within SLA must NOT breach"
    # legacy surfaces shape still works
    legacy = {"surfaces": [{"surface": "news", "age_hours": 9, "sla_hours": 6}]}
    assert {b[0] for b in m._iter_freshness_items(legacy)} == {"news"}
    # empty / junk shapes don't crash
    assert list(m._iter_freshness_items({})) == []
    assert list(m._iter_freshness_items({"x": 1})) == []


def test_action_queue_severity_parser():
    m = importlib.import_module("routes.brain_action_queue")
    assert m._severity("[CRIT] homepage down")[1] == 4
    assert m._severity("[HIGH] something")[1] == 3
    assert m._severity("[MED] drift")[1] == 2
    assert m._severity("no tag here")[1] == 0
    assert hasattr(m, "brain_action_queue_bp")
    assert hasattr(m, "action_queue")
