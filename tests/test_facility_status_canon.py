"""Canonical facility-status guards — the lowercase-'active' shell-row fix.

2026-07-31: discovery ingest had stamped 10,435 discovered_facilities rows
(2,587 cities, all zero-MW) with nonconforming lowercase status 'active' —
the `_stage_facilities_batch` / `_stage_facility` defaults in
routes/discovery_routes.py, the DCM normalizer fallback, and
facility_ingestion.py's PeeringDB parser — while routes/osm_crawler.py wrote
lowercase 'operational'. The canonical vocabulary is Title-Case; consumers
key on the drifted literal (routes/radar.py excludes status='active' as its
empty-shell proxy, which is how Ashburn read 171 vs by-market's 206).

Fix under guard here: every ingest write path routes status through
util.facility_status.canon_status (behavior-tested by direct import — the
module is stdlib-only), and the writer files carry no lowercase status
literals (source fence; ast asserts on the extraction so an empty parse
FAILS rather than vacuously passing).
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


# ── 1. the normalizer itself (real behavior — module is import-safe) ───────

def test_canon_status_maps_known_variants():
    from util.facility_status import canon_status
    assert canon_status("active") == "Operational"
    assert canon_status("operational") == "Operational"
    assert canon_status(" ACTIVE ") == "Operational"
    assert canon_status("Under construction") == "Under Construction"
    assert canon_status("Operational") == "Operational"


def test_canon_status_default_and_passthrough():
    from util.facility_status import canon_status
    assert canon_status(None) is None
    assert canon_status(None, default="Operational") == "Operational"
    assert canon_status("", default="Operational") == "Operational"
    assert canon_status("   ", default="Operational") == "Operational"
    # Unknown non-empty values pass through UNCHANGED — never silently
    # rewritten, so new source vocab stays visible in the status enum.
    assert canon_status("Decommissioned") == "Decommissioned"


def test_canonical_vocabulary_is_title_case():
    from util.facility_status import CANONICAL_STATUSES
    for s in CANONICAL_STATUSES:
        assert s == s.title() or " " in s, s
        assert s != s.lower() and s != s.upper(), s


# ── 2. write-boundary wiring in discovery_routes ───────────────────────────

def test_stage_facility_default_is_none_not_active():
    """The keyword default must be None (normalized inside), never a
    hardcoded lowercase literal again."""
    tree = ast.parse(_read("routes/discovery_routes.py"))
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "_stage_facility"]
    assert len(fns) == 1, "ast extraction found no _stage_facility"
    fn = fns[0]
    args = fn.args.args
    defaults = fn.args.defaults
    # defaults align to the TAIL of args
    named = dict(zip([a.arg for a in args[-len(defaults):]], defaults))
    assert "status" in named, "status lost its keyword default"
    d = named["status"]
    assert isinstance(d, ast.Constant) and d.value is None, ast.dump(d)


def test_stager_and_dcm_normalizer_route_through_canon_status():
    src = _read("routes/discovery_routes.py")
    assert "from util.facility_status import canon_status" in src
    assert "canon_status(r.get('status'), default='Operational')" in src, (
        "_stage_facilities_batch no longer normalizes status")
    assert "canon_status(fac.get('status'), default='Operational')" in src, (
        "DCM normalizer no longer normalizes status")
    assert "canon_status(status, default='Operational')" in src, (
        "_stage_facility no longer normalizes status")


# ── 3. no lowercase status literals in the writer files ────────────────────

_FORBIDDEN = {
    "routes/discovery_routes.py": [
        "'status', 'active'", "status='active'",
        "or 'active')", "DEFAULT 'active'",
    ],
    "routes/osm_crawler.py": ["'operational'", '"operational"'],
    "facility_ingestion.py": ["'status': 'active'", "'status', 'active'"],
}


def test_writer_files_carry_no_lowercase_status_literals():
    hits = []
    for rel, patterns in _FORBIDDEN.items():
        for i, line in enumerate(_read(rel).splitlines(), 1):
            for p in patterns:
                if p in line:
                    hits.append(f"{rel}:{i}: {line.strip()[:70]}")
    assert not hits, "lowercase status literal back in a writer:\n" + "\n".join(hits)
