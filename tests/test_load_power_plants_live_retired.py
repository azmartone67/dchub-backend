"""POST /api/admin/load-power-plants-live is retired, and no admin loader
endpoint can start the EIA generator reseed it used to run (2026-09-13).

House rule: tests never import main. Every handler here is compiled out of
main.py's AST with its decorators stripped and its module globals stubbed, then
called inside a Flask request context. What is asserted is what main.py's own
code returns and calls, not a copy of it.

Measured before the change, read-only against the live database:
  * information_schema.columns: the reseed's INSERT named 12 columns, and 3 of
    them (name, fuel_type, capacity_mw) are not columns of eia_generators, so
    every insert chunk failed.
  * The only code that reads eia_generators' columns,
    routes/api_integration_wiring.enrich_site_analysis, has no caller, and
    contracts/dataset_inventory.json records serving_routes 0 for the table.

The route and both synchronous loader endpoints reached the same module, so all
three are covered: the route answers 410, and the registry no longer names the
module.
"""
import ast
import builtins
import copy
import functools
import os

import pytest

flask = pytest.importorskip("flask")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PY = os.path.join(ROOT, "main.py")
RETIRED = "eia_generator_reseed"
ROUTE = "/api/admin/load-power-plants-live"
HEADER_VALUE = "unit-test-internal-header"


@functools.lru_cache(maxsize=1)
def _main_tree():
    with open(MAIN_PY, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=MAIN_PY)


def _top_level(name):
    """A deep copy of main.py's top-level def or assignment named `name`."""
    for node in _main_tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node = copy.deepcopy(node)
            node.decorator_list = []          # @app.route needs the real app
            return node
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return copy.deepcopy(node)
    raise AssertionError(f"main.py has no top-level {name}")


def _load(names, namespace):
    module = ast.Module(body=[_top_level(n) for n in names], type_ignores=[])
    exec(compile(module, MAIN_PY, "exec"), namespace)
    return namespace


class _Recorder:
    def __init__(self, result):
        self.calls = []
        self.result = result

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


def _post_retired_route(authorised):
    start_loader = _Recorder({"started": True})
    ns = _load(["phase12g_load_power_plants_live"], {
        "jsonify": flask.jsonify,
        "_phase12g_check_auth": lambda: authorised,
        "phase12g_loader_async": start_loader,
    })
    app = flask.Flask(__name__)
    with app.test_request_context(ROUTE, method="POST"):
        resp = app.make_response(ns["phase12g_load_power_plants_live"]())
    return resp, start_loader


def test_an_authenticated_caller_gets_410_with_instead_and_nothing_starts():
    resp, start_loader = _post_retired_route(authorised=True)
    body = resp.get_json()
    assert resp.status_code == 410, (resp.status_code, body)
    assert body.get("error") == "route_retired" and body.get("retired") is True, body
    assert str(body.get("instead", "")).startswith("/api/"), body
    assert "started" not in body, body
    assert start_loader.calls == [], (
        f"the retired route still started a loader: {start_loader.calls}")


def test_an_anonymous_caller_is_refused_before_the_410():
    resp, start_loader = _post_retired_route(authorised=False)
    assert resp.status_code == 403, resp.status_code
    assert start_loader.calls == []


@pytest.mark.parametrize("endpoint", ["phase12f_run_all_loaders",
                                      "phase12c_admin_load_all"])
def test_no_synchronous_loader_endpoint_runs_the_retired_module(endpoint, monkeypatch):
    for var in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY",
                "DCHUB_ADMIN_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", HEADER_VALUE)
    ran = []

    def run_loader(mod_name):
        ran.append(mod_name)
        return {"ok": True}

    ns = _load(["_STANDALONE_LOADERS", endpoint], {
        "os": os, "request": flask.request, "jsonify": flask.jsonify,
        "utc_iso_z": lambda: "2026-09-13T00:00:00Z",
        "_run_standalone_loader": run_loader,
    })
    app = flask.Flask(__name__)
    with app.test_request_context("/", method="POST",
                                  headers={"X-Internal-Key": HEADER_VALUE}):
        resp = app.make_response(ns[endpoint]())
    assert resp.status_code == 200, (endpoint, resp.status_code)
    assert ran, f"control: {endpoint} ran no loader at all, so the check below is empty"
    assert RETIRED not in ran, f"{endpoint} still runs {RETIRED}: {ran}"


def test_the_standalone_runner_refuses_the_retired_name_without_importing_it():
    imported = []

    class _AnyEntryPoint:
        """Answers every attribute with a no-argument loader, so the control
        runs whatever entry point the registry declares for it."""
        def __getattr__(self, name):
            return lambda: "ran"

    def spy_import(name, *args, **kwargs):
        imported.append(name)
        return _AnyEntryPoint()

    ns = _load(["_STANDALONE_LOADERS", "_run_standalone_loader"], {
        "__builtins__": dict(vars(builtins), __import__=spy_import),
        "get_db": lambda: pytest.fail("the control loader must not need get_db"),
    })
    runner = ns["_run_standalone_loader"]

    control = next(m for m, spec in ns["_STANDALONE_LOADERS"].items()
                   if spec.get("needs") == "none" and not spec.get("unrunnable"))
    rec = runner(control)
    assert rec.get("ok") is True and imported == [control], (
        "control: the import spy did not see a registered loader, so it could not "
        f"see the retired one either: {rec}, {imported}")

    imported.clear()
    rec = runner(RETIRED)
    assert rec.get("ok") is False, rec
    assert imported == [], f"the runner imported {imported} for a retired loader"
