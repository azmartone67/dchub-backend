"""routes/health_json.py stays deleted (2026-08-28, follow-up to #3248).

House rule: tests NEVER import main. This one reads source directly.

The blueprint registered seven public JSON routes — /health.json, four
/qa/*.json, /scripts/learned-skills.json and /data/growth.json — to feed a
second audit dashboard. Not one of them ever served.

#3248 established this for /data/growth.json. Running the same probe over the
whole blueprint found ALL SEVEN shadowed identically: dchub-frontend ships a
static file at every one of those paths, and none of the paths is in that
repo's _routes.json `include`, so CF Pages answers from the asset and never
invokes the worker.

Measured 2026-08-28, cache-busted, against dchub.cloud: all seven responses
carry no x-dc-worker-version and no x-dc-hub-* headers, while a known-Flask
route (/api/v1/mcp/funnel) carries both in the same second. Every served key
set matches the frontend static file rather than Flask's — /data/growth.json
and /qa/auto-fixes.json matched byte-for-byte, and 12 keys the Flask
/data/growth.json always set were absent on the wire, including top_platforms,
keys_by_tier, active_dev_keys, dev_keys_total and conversions_30d.

#3248 chose to document the route rather than remove it, on the grounds that
nothing was broken. Nothing was — but the route was still reachable, keyless,
at the Railway origin, and it is the funnel internals above that it served
there. Re-registering it on a path that DOES route would publish them on
dchub.cloud, which is the surface #3247 exists to shrink.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SHADOWED_PATHS = (
    "/health.json",
    "/qa/last-run.json",
    "/qa/auto-fixes.json",
    "/qa/discovered.json",
    "/scripts/learned-skills.json",
    "/qa/anthropic-suggestions.json",
    "/data/growth.json",
)


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_module_is_gone():
    assert not os.path.exists(os.path.join(ROOT, "routes", "health_json.py")), (
        "routes/health_json.py is back. Every route it defines is shadowed by a "
        "dchub-frontend static asset and cannot serve — see this module's docstring.")


def test_nothing_registers_the_blueprint():
    """The original registration was wrapped in try/except, so a failure logged
    a warning nobody read. A route that cannot even import still looks wired."""
    for fname in ("main.py", "app.py", "wsgi.py"):
        if not os.path.exists(os.path.join(ROOT, fname)):
            continue
        for line in _src(fname).splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue          # the tombstone comment names it on purpose
            assert "health_json_bp" not in stripped, \
                "%s re-registers health_json_bp: %s" % (fname, stripped)
            assert "routes.health_json" not in stripped, \
                "%s re-imports routes.health_json: %s" % (fname, stripped)


def test_no_flask_route_claims_a_shadowed_path():
    """The paths are the trap, not the module name. A route decorator for any of
    them anywhere in routes/ is unreachable code no matter what file it lives in."""
    routes_dir = os.path.join(ROOT, "routes")
    offenders = []
    for fname in sorted(os.listdir(routes_dir)):
        if not fname.endswith(".py"):
            continue
        src = _src("routes", fname)
        for path in SHADOWED_PATHS:
            if '.route("%s"' % path in src or ".route('%s'" % path in src:
                offenders.append("%s -> %s" % (fname, path))
    assert not offenders, (
        "these routes are shadowed by dchub-frontend static assets and never "
        "serve: %s" % ", ".join(offenders))


def test_the_dead_cdn_cache_entries_are_gone():
    """main.py kept CDN cache TTLs for /health.json and /data/growth.json. With
    no route behind them they only stamped direct-to-origin hits, which is
    nobody, while reading like proof the paths were served."""
    for line in _src("main.py").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for path in ("'/health.json'", "'/data/growth.json'"):
            assert not stripped.startswith(path), \
                "dead CDN cache-allowlist entry is back: %s" % stripped
