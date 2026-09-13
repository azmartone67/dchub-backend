#!/usr/bin/env python3
"""POST /api/admin/load-facilities-live is retired (2026-09-13): it answers 410 and runs nothing.

NO NETWORK, NO DB, NO main.py IMPORT. The real handler is lifted out of main.py with
`ast` and executed against tripwires that fail the test if it reaches for a loader, the
auth check, the request or an import.

WHY RETIRED RATHER THAN WIRED TO WRITE (measured 2026-09-12 and 2026-09-13)

The route ran facility_ingestion.ingest_all_sources() in a background thread. That
entry point called four fetchers and kept str(result)[:200] per source. It never called
insert_facilities(). Railway's logs for the 12:19Z and 18:15Z runs show 5,861 PeeringDB
rows, 4,798 and 4,799 OpenStreetMap rows, 30 news "mentions" and 0 Cloudscene rows
fetched and then discarded. Railway logged 29 requests to the route in the 7 days to
2026-09-13, about one per 6-hour data-sync run.

Making it write would have added a fifth facility writer, with the weakest identity in
the repo:
  * insert_facilities() writes the legacy `facilities` table. It deduplicates only
    against that table, on exact (name, city, country), and mints ids from an MD5 of
    provider-name-city-country, which no other writer uses. Facility pages render from
    discovered_facilities and are keyed by canonical_slug.
  * PeeringDB and OpenStreetMap already have live lanes into discovered_facilities,
    keyed on the source's own id: routes/discovery_routes.run_peeringdb_discovery
    (pdb_<id>) and run_osm_discovery (osm_<type>_<id>). Both use ON CONFLICT
    (source, source_id), and both run from the worker's facility_discovery slot and
    from POST /api/discovery/run. Measured from outside, 5,120 of the 5,861 PeeringDB
    facilities (87.4%) already have their slug hash in the published facility sitemaps.
    A decoy hash (name + "zzq") matched 0. That is a lower bound, because the sitemaps
    leave out twins and suppressed rows.
  * Its parsers asserted what the sources never said:
    - 'Operational' on all 5,861 PeeringDB rows
    - "Unknown Data Center" as the name of 816 of 4,800 OpenStreetMap rows
    - the article headline as the facility name on news rows, all marked country 'US'
    - a Cloudscene listing regex holding a literal "%s" where a non-greedy "?" had
      been, so it matched no listing

The module had a second door. _STANDALONE_LOADERS registered its CLI main() for
/api/admin/run-all-loaders and /api/admin/phase12c-rerun-loaders. Under start_web.sh's
gunicorn argv, main()'s argparse exits with code 2 before it opens a connection, and
_run_standalone_loader reported that as "upstream API likely down". Neither route was
called in those 7 days.

Importing the module also registered an atexit heartbeat that reported "success" for
backend-facility-ingestion whenever the importing process exited. The source registry
read that source as fresh while nothing was being written. The module is deleted and
both registrations are removed.

Run standalone:   python3 tests/test_load_facilities_live_retired.py
Run under pytest: pytest tests/test_load_facilities_live_retired.py
"""
import ast
import builtins
import json
import pathlib
import re
from urllib.parse import parse_qs, urlsplit

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
ROUTE = "/api/admin/load-facilities-live"
HANDLER = "phase12g_load_facilities_live"
MODULE = "facility_ingestion"


def _parse(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert tree.body, "%s parsed to an empty module" % path.name
    return tree


def _function(tree, name):
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fns) == 1, "expected one def %s, found %d" % (name, len(fns))
    return fns[0]


def _route_paths(fn):
    return [ast.literal_eval(d.args[0]) for d in fn.decorator_list
            if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route" and d.args]


class _Tripwire:
    """Calling it, or reading any attribute of it, fails the test and is recorded."""

    def __init__(self, name, touched):
        self._name = name
        self._touched = touched

    def __call__(self, *args, **kwargs):
        self._touched.append(self._name)
        raise AssertionError("the retired handler called %s" % self._name)

    def __getattr__(self, attr):
        self._touched.append("%s.%s" % (self._name, attr))
        raise AssertionError("the retired handler touched %s.%s" % (self._name, attr))


def _call_retired_handler():
    fn = _function(_parse(MAIN), HANDLER)
    assert _route_paths(fn) == [ROUTE], "%s no longer serves %s" % (HANDLER, ROUTE)
    fn.decorator_list = []
    touched = []
    guarded_builtins = dict(vars(builtins))
    guarded_builtins["__import__"] = _Tripwire("__import__", touched)
    ns = {"__builtins__": guarded_builtins, "jsonify": lambda body: body}
    for name in ("phase12g_loader_async", "_phase12g_check_auth", "_run_standalone_loader",
                 "request", "get_db", "threading"):
        ns[name] = _Tripwire(name, touched)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<main.py>", "exec"), ns)
    result = ns[HANDLER]()
    return result, touched


def test_retired_route_answers_410_before_any_work():
    result, touched = _call_retired_handler()
    assert touched == [], "the retired handler reached for %s" % touched
    assert isinstance(result, tuple) and len(result) == 2, "expected (body, status): %r" % (result,)
    body, status = result
    assert status == 410, "the retired route answered %r, not 410" % (status,)
    assert body["success"] is False and body["retired"] is True, body
    assert body["error"] == "route_retired", body
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["retired_at"]), body
    assert body["reason"].strip(), body


def _discovery_run_runners():
    """Map each discovery_run source key to the function it runs, from the AST."""
    fn = _function(_parse(ROOT / "routes" / "discovery_routes.py"), "discovery_run")
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == "source_runners" for t in node.targets)):
            runners = {k.value: v.id for k, v in zip(node.value.keys, node.value.values)
                       if isinstance(k, ast.Constant) and isinstance(v, ast.Name)}
            assert runners, "discovery_run's source_runners extracted empty"
            return fn, runners
    raise AssertionError("discovery_run() has no source_runners dict")


def test_instead_names_a_served_route_that_runs_the_live_lanes():
    (body, _status), _touched = _call_retired_handler()
    method, _, target = body["instead"].partition(" ")
    parts = urlsplit(target)

    served = json.loads((ROOT / "contracts" / "route_serving_map.json").read_text(encoding="utf-8"))["serving"]
    assert len(served) > 100, "route_serving_map.json serves only %d routes" % len(served)
    assert "%s %s" % (method, parts.path) in served, (
        "`instead` names %s %s, which the backend does not serve" % (method, parts.path))

    fn, runners = _discovery_run_runners()
    assert _route_paths(fn) == [parts.path], "discovery_run serves %r" % _route_paths(fn)
    sources = [s for s in parse_qs(parts.query).get("sources", [""])[0].split(",") if s]
    unknown = [s for s in sources if s not in runners]
    assert sources and not unknown, "`instead` asks for sources discovery_run lacks: %s" % unknown
    assert {runners[s] for s in sources} == {"run_peeringdb_discovery", "run_osm_discovery"}, (
        "`instead` must run exactly the PeeringDB and OpenStreetMap lanes: %r"
        % {s: runners[s] for s in sources})


def test_the_lanes_it_points_to_run_on_the_worker_schedule():
    """The retirement loses nothing only while the worker still runs both lanes."""
    fn = _function(_parse(ROOT / "crawler_scheduler.py"), "_run_facility_discovery")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    missing = {"run_peeringdb_discovery", "run_osm_discovery"} - names
    assert not missing, "_run_facility_discovery no longer runs %s" % sorted(missing)
    shim = _parse(ROOT / "discovery_engine.py")
    reexported = {a.name for n in ast.walk(shim)
                  if isinstance(n, ast.ImportFrom) and n.module == "routes.discovery_routes"
                  for a in n.names}
    assert {"run_peeringdb_discovery", "run_osm_discovery"} <= reexported, reexported


def test_no_registry_or_route_still_loads_the_module():
    assert not (ROOT / (MODULE + ".py")).exists(), (
        "%s.py is back; importing it registers an atexit 'success' heartbeat" % MODULE)
    tree = _parse(MAIN)

    sites = [n.args[0].value for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "phase12g_loader_async" and n.args
             and isinstance(n.args[0], ast.Constant)]
    assert len(sites) >= 3, "phase12g_loader_async extraction found %d call sites" % len(sites)
    assert MODULE not in sites, "a phase12g route still runs %s" % MODULE

    registry = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_STANDALONE_LOADERS" for t in n.targets)]
    assert len(registry) == 1 and len(registry[0]) >= 8, "_STANDALONE_LOADERS extraction failed"
    assert MODULE not in registry[0], "_STANDALONE_LOADERS still registers %s" % MODULE

    c12 = [ast.literal_eval(n.value) for n in ast.walk(_function(tree, "phase12c_admin_load_all"))
           if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "loaders"
                                                for t in n.targets)]
    assert len(c12) == 1 and len(c12[0]) >= 5, "phase 12c loader list extraction failed"
    assert MODULE not in c12[0], "phase 12c still runs %s" % MODULE


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
