"""Guards for scripts/render_reveal_grid.py — the reVeal grid pre-render job.

/api/v1/reveal-grid-export serves an artifact only when it can HEAD it in R2
(#2093). This job is what puts artifacts there. Two things can silently break
that handshake, and neither shows up as an error anywhere:

  1. KEY DRIFT. The renderer writes a key; the endpoint HEADs a key. If those
     stop agreeing, the upload succeeds, the endpoint 404s `not_rendered`, and
     both halves look healthy. Nothing joins them at runtime.

  2. SEMANTIC DRIFT in the substation lookup. The job replaces
     nlr_intelligence._query_substations with an in-memory index so a state is
     one query instead of ~10,000. If the replacement stops matching the SQL,
     the pre-rendered export quietly disagrees with what reveal-cell-bulk
     computes live for the same cell — two answers to one question, neither
     obviously wrong.

★ The SQL has three behaviours the in-memory version must reproduce, all of
which were found by testing against the real query rather than reading it:
  · it ORDERs by manhattan distance and LIMITs 50 BEFORE the haversine filter,
    so the truncation is part of the contract, not an optimisation
  · it does NOT filter on the state column despite taking a `state` argument
  · lat/lng are `real`; the ORDER BY sees the exact float4 widened to float8,
    but psycopg2 hands Python the shortest decimal repr, which is a DIFFERENT
    number in the ~7th place. Two substations 4e-7 apart sorted in OPPOSITE
    order in SQL and in Python, and with LIMIT 50 that changed the result SET.
    Both sides now select lat::float8 / lng::float8. Parity went 4 mismatches
    -> 0 across 270 sampled (point, radius) pairs in VA/TX/CA.

Pure source/AST + in-process asserts. No DB, no network, no flask, no
`import main`. Nothing here runs at module scope.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCRIPT = "scripts/render_reveal_grid.py"
_REVEAL = "reveal_endpoints.py"
_NLR = "nlr_intelligence.py"
_WORKFLOW = ".github/workflows/reveal-grid-render.yml"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _tree(rel):
    tree = ast.parse(_read(rel))
    assert isinstance(tree, ast.Module) and len(tree.body) > 5, (
        f"{rel} parsed to a degenerate module — this harness is not looking at "
        "the real file")
    return tree


def _fn(rel, name):
    for node in ast.walk(_tree(rel)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{rel} no longer defines {name}()")


def _const(rel, name):
    for node in _tree(rel).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{rel} no longer defines {name}")


def _code_src(rel, name):
    """Function CODE as text — comments and docstring stripped.

    Both files document the old broken behaviour verbatim, so scanning raw
    source lets prose satisfy (or trip) a guard. ast.unparse drops comments.
    """
    fn = _fn(rel, name)
    node = ast.FunctionDef(name=fn.name, args=fn.args, decorator_list=[],
                           returns=None, type_comment=None, type_params=[],
                           body=list(fn.body))
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    assert node.body, f"{name} has no body beyond its docstring"
    return ast.unparse(ast.fix_missing_locations(node))


def _load_script():
    """Import the renderer without its heavy deps or a DB.

    Executes the module with __name__ != '__main__', so main() does not run.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_render_reveal_grid_under_test", os.path.join(ROOT, _SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 0. must-fail control ─────────────────────────────────────────────────────

def test_harness_reads_the_real_files():
    """A collection abort exits SILENT GREEN (2026-07-28), so all-passing is
    not by itself evidence these ran."""
    assert "render_reveal_grid" in _read(_SCRIPT)
    mod = _load_script()
    assert mod.export_key("VA", "geojson") == \
        "reveal-grid-exports/VA/reveal_grid_VA_5km.geojson"
    assert mod.CELL_SIZE_KM == 5.0


# ── 1. the key handshake with the endpoint ───────────────────────────────────

def test_renderer_key_matches_the_endpoint_key_exactly():
    """The whole contract. Upload to one key, HEAD another, and both halves
    report success while the partner gets a 404."""
    mod = _load_script()
    endpoint_prefix = _const(_REVEAL, "GRID_EXPORT_PREFIX")
    assert mod.GRID_EXPORT_PREFIX == endpoint_prefix, (
        f"prefix drift: renderer writes {mod.GRID_EXPORT_PREFIX!r}, "
        f"{_REVEAL} HEADs {endpoint_prefix!r}")

    # Rebuild the endpoint's key with its own source, not a copy of it.
    ns = {"GRID_EXPORT_PREFIX": endpoint_prefix}
    exec(ast.unparse(_fn(_REVEAL, "_grid_export_key")), ns)
    for state in ("VA", "TX", "NV"):
        for fmt in ("parquet", "geojson", "csv"):
            assert ns["_grid_export_key"](state, fmt) == mod.export_key(state, fmt), (
                f"key drift for {state}.{fmt}: endpoint expects "
                f"{ns['_grid_export_key'](state, fmt)!r}, renderer writes "
                f"{mod.export_key(state, fmt)!r}")


def test_renderer_formats_are_a_subset_of_what_the_endpoint_serves():
    mod = _load_script()
    served = set(_const(_REVEAL, "GRID_EXPORT_FORMATS"))
    produced = set(mod.SERIALISERS)
    assert produced <= served, (
        f"renderer emits {sorted(produced - served)}, which reveal-grid-export "
        "will never serve")
    assert set(mod.CONTENT_TYPES) == produced, (
        "every serialised format needs a content type for the R2 object")


def test_workflow_renders_every_state_the_endpoint_advertises():
    """states_in_scope is published in the endpoint's 404 body. A state listed
    there but never rendered is a promise with nothing behind it."""
    import re
    in_scope = set(_const(_REVEAL, "GRID_EXPORT_STATES_IN_SCOPE"))
    wf = _read(_WORKFLOW)
    # Tokenise the STATES= defaults rather than substring-matching: "FL" ends
    # the list and is followed by a quote, not a space, so a naive f" {s} "
    # check reports it missing when it is right there.
    listed = set()
    for m in re.findall(r'STATES=\"([A-Z ]+)\"', wf):
        listed |= set(m.split())
    assert listed, "no STATES= default found in the workflow"
    missing = sorted(in_scope - listed)
    assert not missing, (
        f"{_WORKFLOW} never renders {missing}, but reveal-grid-export lists "
        "them in states_in_scope")


# ── 2. parity of the in-memory substation lookup with the SQL ────────────────

def test_index_reproduces_the_sql_limit_and_ordering():
    """LIMIT 50 lands BEFORE the haversine filter, so it can drop a row that is
    inside the radius. Reproducing the radius but not the truncation would
    silently ADD substations the live endpoint never sees."""
    src = _code_src(_SCRIPT, "query")
    assert "[:50]" in src, (
        "the in-memory lookup dropped the LIMIT 50 truncation — the export "
        "would see substations reveal-cell-bulk does not")
    sql = _code_src(_NLR, "_query_substations")
    assert "LIMIT 50" in sql, (
        "the SQL LIMIT changed; the in-memory [:50] no longer mirrors it")


def test_both_paths_sort_ties_the_same_way_and_numerically():
    """Ties are common (distance_km is rounded to 0.1) and compute_reveal_cell
    sums subs[:10], so tie order is a real variance in transmission_hosting_mw.
    The tie-break must be NUMERIC — ordering by `name` puts Postgres's text
    collation between the SQL and any Python re-implementation."""
    sql = _code_src(_NLR, "_query_substations")
    idx = _code_src(_SCRIPT, "query")
    assert "ABS(lat - %(lat)s) + ABS(lng - %(lon)s), lat, lng" in sql, (
        "the SQL tie-break is gone or is no longer numeric")
    assert ", name," not in sql.split("ORDER BY")[-1].split("LIMIT")[0], (
        "the SQL orders ties by `name` — that reintroduces collation-dependent "
        "ordering that Python cannot reproduce")
    assert "r[3], r[4]" in idx, (
        "the in-memory tie-break no longer matches the SQL's lat, lng")
    for src, who in ((sql, _NLR), (idx, _SCRIPT)):
        assert "'distance_km'" in src.replace('"', "'") and "sort" in src, (
            f"{who} lost its final distance sort")


def test_both_paths_widen_lat_lng_to_float8():
    """lat/lng are `real`. Postgres orders on the exact float4 widened to
    float8; psycopg2 gives Python the shortest decimal repr, a different number
    in the ~7th place. With LIMIT 50 that changed which rows survived — 4
    mismatches in VA before the cast, 0 after."""
    for rel, fname in ((_NLR, "_query_substations"), (_SCRIPT, "load_substations")):
        src = _code_src(rel, fname)
        assert "lat::float8" in src and "lng::float8" in src, (
            f"{rel}:{fname} no longer widens lat/lng to float8 — the two paths "
            "will order ties differently and select a different 50")


def test_index_ignores_state_exactly_as_the_sql_does():
    """The SQL takes a `state` argument and never uses it, so a border cell
    legitimately sees out-of-state substations. Filtering in the index would
    change the answer near every border."""
    idx_fn = _fn(_SCRIPT, "query")
    body = _code_src(_SCRIPT, "query")
    assert "state" in {a.arg for a in idx_fn.args.args}, (
        "index.query lost its state argument and no longer matches the "
        "_query_substations signature")
    assert "state ==" not in body and "r[5]" not in body, (
        "the in-memory lookup started filtering on state; the SQL does not, "
        "and border cells would diverge")
    sql = _code_src(_NLR, "_query_substations")
    where = sql.split("WHERE")[-1].split("ORDER BY")[0] if "WHERE" in sql else ""
    assert "state" not in where, (
        "the SQL now filters on state — the in-memory lookup must start "
        "filtering too, or the export diverges near borders")


def test_loader_takes_a_margin_beyond_the_state_box():
    """A cell on the border needs substations from the next state over."""
    fn = _fn(_SCRIPT, "load_substations")
    margins = [d for d in fn.args.defaults if isinstance(d, ast.Constant)]
    assert margins and margins[-1].value >= 1.0, (
        "load_substations lost its margin — border cells would see fewer "
        "substations than the live query returns")


# ── 3. the job must not publish a partial or empty render ────────────────────

def test_refuses_to_publish_an_empty_grid():
    src = _code_src(_SCRIPT, "main")
    assert "refusing to publish an empty grid" in src, (
        "a zero-cell render would overwrite a good artifact with nothing")


def test_upload_verifies_the_object_after_writing():
    """A green put is not proof the endpoint will find it."""
    src = _code_src(_SCRIPT, "upload")
    assert "head_object" in src, "upload no longer reads back what it wrote"
    assert "upload verify FAILED" in src, "the size check no longer fails loudly"


def test_workflow_does_not_mask_a_failed_state():
    """`set -e` would abort on the first bad state and skip the rest; a bare
    loop with no collection would exit 0 having rendered nothing."""
    wf = _read(_WORKFLOW)
    assert "failed=\"$failed $s\"" in wf, (
        "the workflow no longer collects per-state failures")
    assert "::error::render failed for:" in wf and "exit 1" in wf, (
        "a failed state would be reported as a successful run")


def test_workflow_verifies_against_the_live_endpoint():
    """The only step that actually proves the handshake end to end."""
    wf = _read(_WORKFLOW)
    # The URL is assembled from $base, so the full path never appears
    # contiguously — check the pieces that make the request a real one.
    assert "reveal-grid-export" in wf and "?state=$s" in wf, (
        "the workflow no longer asks the live endpoint whether it can serve "
        "what was just uploaded — key drift would pass silently")
    assert "status=ready" in wf.replace('"', "").replace(" ", "") or \
           '"$status" = "ready"' in wf, (
        "the verify step no longer requires status=ready")
    assert "endpoint does not serve" in wf


def test_parquet_is_not_a_backend_dependency():
    """The API presigns the object; it never parses it. pyarrow belongs in the
    render job only — requirements.txt carries neither pyarrow nor pandas."""
    reqs = _read("requirements.txt").lower()
    for pkg in ("pyarrow", "pandas", "fastparquet", "geopandas"):
        assert not any(line.strip().startswith(pkg) for line in reqs.splitlines()), (
            f"{pkg} was added to requirements.txt — the backend does not need "
            "it to serve a presigned URL, and it is a large image cost")
    assert "pyarrow" in _read(_WORKFLOW), (
        "the render workflow stopped installing pyarrow — parquet would be "
        "silently skipped for every state")
    src = _code_src(_SCRIPT, "to_parquet")
    assert "ImportError" in src and "return None" in src, (
        "to_parquet must degrade to None without pyarrow, not raise")


def test_state_bounds_are_imported_not_re_inlined():
    """Three copies of this table already exist and the third disagrees with
    the other two on WI/NH/NJ/ME/MD. A fourth would be the next drift."""
    src = _code_src(_SCRIPT, "state_bounds")
    assert "_STATE_BOUNDS" in src and "import" in src, (
        "state_bounds no longer imports the shared table")
    assert "36.5" not in _read(_SCRIPT).split('"""')[-1], (
        "a bounds literal appears to have been inlined into the renderer")
