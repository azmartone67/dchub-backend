"""Guard: one table name must not mean two tables. shell(#41), 2026-07-29.

WHAT THIS PINS
──────────────
`transmission_lines` (94,626 rows, MAINTAINED, refreshed by
routes/transmission_ingest.py) stores NO geometry. `transmission_lines_eia`
(56,108 rows) is a frozen GEOCODED SNAPSHOT with no writer in the repo and no
timestamp column. Before this repair, the served field `transmission_lines`
was bound to the SNAPSHOT, so the product published 56,108 as the transmission
layer and 38,518 maintained lines (40.7%) were silently missing.

The chosen contract (the fresh table cannot serve a spatial layer, so a repoint
is impossible — see util/transmission_tables.py):
  C1. The COUNT surface binds `transmission_lines` to the MAINTAINED table and
      publishes the snapshot separately under a name that says so.
  C2. No asset total ever counts the snapshot (or facilities) into it.
  C3. Unmeasured members emit None + a reason, never 0.
  C4. Every SPATIAL transmission surface labels its count as a FLOOR and ships a
      coverage block naming the served table, its vintage, and the gap.
  C5. Published shortfall figures round DOWN, and an unmeasured gap is None
      (never a confident 0).
  C6. The docstrings/comments that credited the 56K table with ~94K rows are gone.

★ C1/C2/C3 WERE LANDED ON MAIN BY #1922 (044cd4ed, `_STATS_MEMBERS` role tags),
  independently of this branch and better than the version this branch first
  carried — so that work was DROPPED here rather than re-applied, which would
  have reverted it. This guard pins the contract, not an implementation: the
  C1-C3 tests assert against main's structure. #1922 explicitly scoped itself to
  the COUNT surfaces and recorded the spatial repoint as "UNVERIFIED ... a
  separate, DB-verified change". C4-C6 are that follow-up: live schema probe
  2026-07-29 proves transmission_lines has NO coordinate column, so the repoint
  is impossible rather than merely unattempted, and the honest fix is disclosure.

EXPECTED PASS/FAIL — the whole point of a guard test. MEASURED, not predicted.
─────────────────────────────────────────────────────────────────────────────
UNPATCHED (origin/main @ 044cd4ed, extracted via `git archive origin/main`):
    10 failed, 9 passed, 1 xfailed
    Collection does NOT abort — every `util.transmission_tables` import lives
    inside a test body, so the tests needing it fail individually instead of
    taking the whole file down with a collection error.
    The 9 that legitimately pass unpatched, and must pass in BOTH states:
      C1/C2/C3, already satisfied by #1922:
        test_count_surface_binds_transmission_lines_to_the_maintained_table
        test_no_stats_member_is_the_snapshot_under_the_layer_name
        test_asset_total_excludes_every_non_asset_member
        test_stats_never_publishes_zero_for_an_unmeasured_member
        test_registration_log_no_longer_misstates_the_layer
      Live-verified schema facts and wiring, which pin no patch at all:
        test_live_schema_maintained_table_has_no_coordinates
        test_live_schema_snapshot_is_the_only_geocoded_table
        test_no_consumer_repoints_a_spatial_query_at_the_maintained_table
        test_helpers_actually_resolve
    The other 10 must fail on main — they are this branch's actual contribution.
PATCHED (this branch):
    19 passed, 1 xfailed, 0 failed
    Now 20 passed, 1 xfailed: test_parametrized_node_ids_are_checkout_independent
    was added 2026-09-01 with the switch to repo-relative parametrize values
    (it passes in both states — it pins this file's node IDs, not the contract).

MUST-FAIL CONTROL
─────────────────
test_zzz_must_fail_control is an xfail(strict=True) that asserts a falsehood.
pytest reports it as `xfailed`. If a future conftest/collection accident makes
this file silently not run, the control disappears from the summary and the
suite's "0 failed / 0 tests" is exposed as fake green rather than read as a
pass. Never delete it.

Run:  python3 -m pytest tests/test_transmission_table_identity_guard.py -v
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _abs(rel):
    """Repo-RELATIVE (posix-style) path -> absolute path in this checkout."""
    return os.path.join(ROOT, *rel.split("/"))


# NEVER import main (house rule). Nothing here runs at module scope beyond
# cheap, side-effect-free path arithmetic and file reads.
#
# ★ PARAMETRIZE OVER THE `REL_*` FORMS, NEVER THE ABSOLUTE ONES, and call
#   _abs() inside the test body. A parametrized absolute path is copied
#   verbatim into the pytest node ID, so the ID carries the checkout location:
#     ...::test_x[/Users/me/.claude/worktrees/hungry-brattain-0a65fd/foo.py]
#   Two runs from two worktrees then share no test NAMES, and diffing failures
#   by name — the standard way to tell a real regression from a pre-existing
#   one — reports every test in this file as removed and a same-named one as
#   added. With a worktree per change that is the normal case, not the corner
#   case, and it makes CI-vs-local diffs noise too. Same class as #3530, which
#   fixed the absolute path baked into contracts/api_response_surface.json.
#   Fenced below by test_parametrized_node_ids_are_checkout_independent.
REL_INFRA = "routes/infrastructure_data_routes.py"
REL_GRID = "routes/grid_intelligence_routes.py"
REL_ENERGY = "routes/energy_discovery_routes.py"
REL_MCP = "dchub_mcp_server.py"
REL_TXMOD = "util/transmission_tables.py"

INFRA = _abs(REL_INFRA)
GRID = _abs(REL_GRID)
ENERGY = _abs(REL_ENERGY)
MCP = _abs(REL_MCP)
TXMOD = _abs(REL_TXMOD)

# Live-verified 2026-07-29 via /api/v1/admin/schema (prod, DATABASE_URL-backed)
# and /api/v1/energy/discovery/status. These are the facts the contract rests on.
LIVE_MAINTAINED_COLS = {
    "id", "hifld_id", "name", "operator", "voltage_kv", "from_sub", "to_sub",
    "length_miles", "state", "status", "line_type", "source", "last_updated",
    "created_at",
}
LIVE_SNAPSHOT_COLS = {
    "id", "owner", "voltage_kv", "sub_1", "sub_2", "lat", "lng", "state",
    "shape_length", "source", "source_id", "name",
}
COORD_NAMES = {"lat", "lng", "lon", "latitude", "longitude", "geom", "geometry"}


def _src(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _parsed(path):
    """Parse a file and PROVE the parse produced something.

    An empty/failed parse yields a Module with an empty body, which satisfies
    any isinstance() check — so assert on the body, never just the type.
    """
    tree = ast.parse(_src(path), filename=path)
    assert isinstance(tree, ast.Module), f"{path}: not a Module"
    assert len(tree.body) > 0, (
        f"{path}: parsed to an EMPTY module — a vacuous parse would satisfy "
        "every structural assertion below")
    return tree


# ── The live schema facts the contract depends on ────────────────────────────

def test_live_schema_maintained_table_has_no_coordinates():
    """The deciding fact: the fresh table cannot serve a spatial layer."""
    assert not (LIVE_MAINTAINED_COLS & COORD_NAMES), (
        "transmission_lines gained coordinate columns — if this is real, the "
        "repair path changes from rename-and-file-gap to an actual REPOINT. "
        "Re-probe /api/v1/admin/schema?table=transmission_lines and revisit "
        "util/transmission_tables.py before relaxing this guard.")


def test_live_schema_snapshot_is_the_only_geocoded_table():
    assert {"lat", "lng"} <= LIVE_SNAPSHOT_COLS
    assert "last_updated" not in LIVE_SNAPSHOT_COLS, (
        "the snapshot gained a timestamp column — it can now self-report its "
        "vintage, so SNAPSHOT_VINTAGE_UNKNOWN_REASON is stale")


# ── C1/C2/C3: the stats endpoint publishes the honest count ──────────────────

def test_count_surface_binds_transmission_lines_to_the_maintained_table():
    """C1 — the COUNT surface is the one consumer that needs no coordinates, so
    it is the one place the repoint is possible. It must take it.

    NOTE: C1/C2/C3 were landed on main by #1922 (`_STATS_MEMBERS` role tags),
    independently and better than the version this branch originally carried.
    This guard therefore pins THAT structure rather than a competing one — the
    point of the contract is that it holds, not who implemented it.
    """
    members = _stats_members()
    assert members["transmission_lines"] == ("transmission_lines", "asset"), (
        "the member published as `transmission_lines` must COUNT the maintained "
        "table. Binding it to transmission_lines_eia publishes 56,108 as the "
        "national count and understates the layer by 38,518 lines.")
    assert members.get("transmission_lines_geocoded_snapshot") == (
        "transmission_lines_eia", "subset"), (
        "the geocoded snapshot must keep a name of its own and be tagged "
        "`subset` so it is published but never summed — dropping it would hide "
        "the gap the other way, summing it would double-count the layer")


def test_no_stats_member_is_the_snapshot_under_the_layer_name():
    """C1 — the exact original defect: the served key `transmission_lines`
    pointing at the 56,108-row snapshot. It must not come back."""
    for key, (table, _role) in _stats_members().items():
        if key == "transmission_lines":
            assert table != "transmission_lines_eia", (
                "the served key `transmission_lines` is bound back to the "
                "snapshot — the original defect, restored")


def test_asset_total_excludes_every_non_asset_member():
    """C2 — a subset of a counted population, and data-centre facilities, must
    not sum into an infrastructure asset total."""
    import importlib
    mod = importlib.import_module("routes.infrastructure_data_routes")
    assert "transmission_lines" in mod.ASSET_KEYS
    assert "transmission_lines_geocoded_snapshot" not in mod.ASSET_KEYS, (
        "the geocoded snapshot sums into the asset total — double-counting the "
        "transmission layer by ~56K")
    assert "discovered_facilities" not in mod.ASSET_KEYS, (
        "data-centre facilities sum into an infrastructure asset total")


def test_stats_never_publishes_zero_for_an_unmeasured_member():
    """C3 — house rule. Before #1922 this surface published submarine_cables: 0
    for a table whose ingest has never run; 0 was a fabricated figure.

    Asserted BEHAVIOURALLY by executing the real _measure_member against stub
    cursors, not by grepping for `= 0`: a comment describing the old shape would
    satisfy a substring check and let the defect back in.
    """
    import importlib
    mod = importlib.import_module("routes.infrastructure_data_routes")

    class _Cur:
        def __init__(self, script):
            self._script = list(script)
            self._last = None

        def execute(self, sql, params=None):
            self._last = self._script.pop(0) if self._script else None

        def fetchone(self):
            return self._last

    class _Conn:
        def rollback(self):
            pass

    # absent table: to_regclass -> NULL
    val, reason = mod._measure_member(_Cur([(None,)]), _Conn(), "x", "no_such_t")
    assert val is None and reason, "an absent table must be unmeasured + reason"

    # present but genuinely 0 rows -> still NOT published as the figure 0
    val, reason = mod._measure_member(
        _Cur([("public.t",), (0,)]), _Conn(), "submarine_cables", "submarine_cables")
    assert val != 0, (
        "a 0 row count is published as the figure 0 — 'never populated' and "
        "'truly zero' are different claims and COUNT(*) cannot tell them apart")
    assert val is None and reason, "a zero count must be unmeasured + reason"

    # a real population passes through unchanged
    val, reason = mod._measure_member(
        _Cur([("public.t",), (94626,)]), _Conn(), "transmission_lines",
        "transmission_lines")
    assert val == 94626 and reason is None


# ── C4: every spatial surface labels its count and ships coverage ────────────

@pytest.mark.parametrize("rel,label", [
    (REL_INFRA, "paid Land & Power map layer"),
    (REL_MCP, "MCP transmission layer served to agents"),
    (REL_ENERGY, "Energy Discovery panel"),
])
def test_spatial_consumers_declare_their_count_is_a_floor(rel, label):
    """C4 — a 41%-incomplete count published bare is the silent shortfall."""
    path = _abs(rel)  # relative in the ID, absolute in the body — see _abs()
    _parsed(path)
    src = _src(path)
    assert "count_is_floor" in src, (
        f"{label} ({os.path.basename(path)}) publishes a transmission count "
        "with no indication it is a floor. UNPATCHED: absent everywhere.")


@pytest.mark.parametrize("rel", [REL_INFRA, REL_MCP])
def test_spatial_consumers_ship_a_coverage_block(rel):
    """C4 — the vintage and the gap must travel with the rows."""
    path = _abs(rel)  # relative in the ID, absolute in the body — see _abs()
    src = _src(path)
    assert "_tx_coverage" in src, (
        f"{os.path.basename(path)} serves snapshot rows without the coverage "
        "block naming the served table, its vintage and the geocoding gap")
    assert "coverage_unmeasured_reason" in src, (
        "coverage must fail soft to None + reason, never to a stale or zero "
        "figure, and must never cost the caller its rows")


def test_no_consumer_repoints_a_spatial_query_at_the_maintained_table():
    """The dangerous 'fix'. transmission_lines has no lat/lng, and every one of
    these call sites swallows errors into an empty result or a 0 — so a repoint
    turns the layer BLANK instead of fuller. Proven locally: the query raises
    UndefinedColumn ('column "lat" does not exist')."""
    for path in (INFRA, MCP, GRID, ENERGY):
        src = _src(path)
        for bad in ("FROM transmission_lines\n", "FROM transmission_lines "):
            assert bad not in src, (
                f"{os.path.basename(path)} appears to query the maintained "
                "table directly. It stores no geometry — a spatial query "
                "against it raises UndefinedColumn and the surrounding "
                "except-handler serves an empty layer silently.")


def test_grid_proximity_count_is_null_not_zero_when_unmeasured():
    """C3/C4 on the paid per-site path — BUG-021 recurring
    (dchub-frontend/admin-qa.html:68). A 0 here reads to a paying caller as
    'no transmission near this site'."""
    src = _src(GRID)
    fn_src = _func_src(GRID, "_get_infra_counts")
    assert "'transmission_lines': None" in fn_src, (
        "UNPATCHED shape: pre-initialised to 0, so a failed query publishes a "
        "confident zero")
    assert "transmission_lines_unmeasured_reason" in src
    assert "transmission_lines_is_floor" in src


# ── C5: the gap arithmetic can never overstate reality ───────────────────────

def test_geocoding_gap_rounds_down_and_refuses_to_invent():
    """C5 — bound live in prod today: 94,626 - 56,108 = 38,518 (40.7%)."""
    from util.transmission_tables import geocoding_gap  # no DB, no main

    from util.transmission_tables import GEOCODING_GAP_TRACKING

    gap = geocoding_gap(94626, 56108)
    assert gap["lines_without_coordinates"] == 38518
    # 38518/94626 = 40.705…% — must floor to 40.7, never round up to 40.8
    assert gap["pct_of_maintained_absent"] == 40.7
    assert gap["reason"], "the shortfall must always ship a reason"

    # PAYLOAD DISCIPLINE (deliberate): the long remediation text is the standing
    # engineering record, not wire content. It ships on every paid map response
    # and every agent tool result otherwise — measured at 2,006 bytes for the
    # whole disclosure block, 45% overhead on a 27-row result, now 1,171.
    assert "tracking" not in gap, (
        "the remediation text is back on the wire; keep it in "
        "GEOCODING_GAP_TRACKING, which is not serialized")
    assert GEOCODING_GAP_TRACKING, "the standing gap record must still exist"
    assert "returnGeometry=true" in GEOCODING_GAP_TRACKING, (
        "the gap record no longer states how to close the gap")

    # unmeasured in → None out, NOT a confident zero
    assert geocoding_gap(None, 56108) is None
    assert geocoding_gap(94626, None) is None
    assert geocoding_gap(0, 0) is None


def test_gap_module_never_claims_the_snapshot_has_a_writer_or_a_vintage():
    from util.transmission_tables import (
        GEOCODED_SNAPSHOT_KEY, MAINTAINED_TABLE,
        SNAPSHOT_VINTAGE_UNKNOWN_REASON,
    )
    assert MAINTAINED_TABLE == "transmission_lines"
    assert GEOCODED_SNAPSHOT_KEY != "transmission_lines", (
        "the snapshot must not be served under the name of the maintained set "
        "— that ambiguity IS the defect")
    assert "no timestamp column" in SNAPSHOT_VINTAGE_UNKNOWN_REASON


# ── C6: the docstrings that lied ─────────────────────────────────────────────

def test_registration_log_no_longer_misstates_the_layer(   # C6
):
    """C6, behavioural half — the string this module actually LOGS at startup.

    It read "94K+ HIFLD lines": wrong count (the endpoint serves the 56K
    geocoded snapshot) and wrong provenance (the 94,626 set is EIA; the
    52,244-row HIFLD snapshot was superseded by routes/transmission_ingest.py).
    Extracted from the AST so only real log arguments are inspected — a comment
    describing the old text cannot satisfy this.
    """
    fn = _func(_parsed(INFRA), "register_infra_data_routes")
    logged = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            for arg in node.args:
                logged.extend(_str_parts(arg))
    tx_lines = [s for s in logged if "transmission-lines" in s]
    assert tx_lines, "the transmission-lines registration log line disappeared"
    joined = " ".join(tx_lines)
    assert "94K" not in joined, (
        "the startup log still advertises a 94K figure for an endpoint that "
        f"serves the 56,108-row snapshot. Logged: {joined!r}")
    assert "HIFLD" not in joined, (
        "the startup log still attributes this layer to HIFLD; the maintained "
        f"94,626-row set is EIA. Logged: {joined!r}")


def test_the_94k_figure_is_no_longer_attached_to_the_56k_table():
    """C6, docstring half. The literal lying strings are banned outright, so
    they cannot reappear even inside a comment — which means the notes recording
    this repair must PARAPHRASE the old wording, never reproduce it."""
    src = _src(INFRA)
    for lie in ("transmission_lines_eia (94K+ rows)",
                "94K+ HIFLD lines",
                "94K-row table"):
        assert lie not in src, (
            f"the lying literal {lie!r} is present in "
            "routes/infrastructure_data_routes.py. If this is a comment about "
            "the fix, paraphrase it — this guard bans the string itself.")
    # and the honest figures are stated instead
    assert "56,108" in src and "94,626" in src


def test_snapshot_docstrings_state_it_has_no_writer_or_is_a_snapshot():
    for path in (INFRA, GRID, ENERGY, MCP):
        src = _src(path)
        assert ("SNAPSHOT" in src or "snapshot" in src), (
            f"{os.path.basename(path)} names transmission_lines_eia without "
            "saying it is a partial frozen snapshot")


# ── AST helpers ──────────────────────────────────────────────────────────────

def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name}() not found — it was renamed or "
                         "removed; this guard must be re-pointed, not deleted")


def _func_src(path, name):
    node = _func(_parsed(path), name)
    lines = _src(path).splitlines()
    return "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])


def _targets_subscript_of(target, name):
    """True if `target` is a subscript into the local variable `name`."""
    return (isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == name)


def _str_parts(node):
    """Every string literal reachable from an expression (handles implicit
    concatenation, f-strings and BinOp '+' joins)."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


def _stats_members():
    """{published_key: (table, role)} from the live _STATS_MEMBERS tuple.

    Imports the module (which pulls in flask + util only — never main).
    """
    import importlib
    mod = importlib.import_module("routes.infrastructure_data_routes")
    members = getattr(mod, "_STATS_MEMBERS", None)
    assert members, (
        "_STATS_MEMBERS is missing — the stats surface was restructured. "
        "Re-point this guard at whatever now decides which table each published "
        "key counts; do not delete it.")
    return {k: (t, role) for k, t, role in members}


# ── Node-ID hygiene: an ID must not name the checkout it ran in ──────────────

def test_parametrized_node_ids_are_checkout_independent():
    """No parametrized value in this file may be an absolute path.

    pytest copies a string argvalue verbatim into the node ID, so parametrizing
    over INFRA/MCP/ENERGY stamps the checkout location into the ID:
        ...::test_x[/Users/me/.claude/worktrees/<name>/dchub_mcp_server.py]
    Runs from two worktrees then share no test NAMES at all, which breaks the
    one reliable way to separate a real regression from a pre-existing failure
    (diff the failures by name against a baseline) and makes every CI-vs-local
    diff noise. The repo's worktree-per-change workflow makes that the normal
    case. #3530 was the same class in contracts/api_response_surface.json.

    Reads this module's OWN live marks, so re-adding an absolute path to a
    decorator fails here; a comment describing the old shape cannot satisfy it.
    """
    import sys
    checked = 0
    for name, obj in vars(sys.modules[__name__]).items():
        if not (name.startswith("test_") and callable(obj)):
            continue
        for mark in getattr(obj, "pytestmark", []):
            if mark.name != "parametrize":
                continue
            argvalues = (mark.args[1] if len(mark.args) > 1
                         else mark.kwargs.get("argvalues", []))
            for row in argvalues:
                row = getattr(row, "values", row)  # unwrap pytest.param()
                for value in (row if isinstance(row, (tuple, list)) else (row,)):
                    if not isinstance(value, str):
                        continue
                    checked += 1
                    assert not os.path.isabs(value), (
                        f"{name} parametrizes over the absolute path {value!r}; "
                        "the checkout location lands in the node ID. Pass the "
                        "repo-relative form and call _abs() in the test body.")
                    assert ROOT not in value, (
                        f"{name} parametrizes over a value carrying this "
                        f"checkout's root: {value!r}")
    assert checked >= 5, (
        f"only {checked} parametrized string values were inspected — the two "
        "spatial-consumer parametrizations alone supply 8. The scan has gone "
        "vacuous (marks moved, or the attribute is no longer `pytestmark`) and "
        "is passing without checking anything.")


def test_helpers_actually_resolve():
    """Free-variable check: a helper that raises NameError, or a target function
    that has been renamed, would otherwise make the tests above vacuous or error
    out ambiguously. Prove every name resolves before relying on it.
    """
    for h in (_abs, _func, _func_src, _stats_members, _targets_subscript_of,
              _str_parts):
        assert callable(h), f"{h} is not callable"
    assert _abs("routes/x.py") == os.path.join(ROOT, "routes", "x.py")
    fn = _func(_parsed(INFRA), "get_infrastructure_stats")
    assert fn.name == "get_infrastructure_stats"
    assert len(_func_src(INFRA, "get_infrastructure_stats")) > 200
    members = _stats_members()
    assert "transmission_lines" in members, "member extraction returned nothing useful"
    assert _str_parts(ast.parse("f('a' 'b')").body[0].value)


@pytest.mark.xfail(strict=True, reason="MUST-FAIL CONTROL — proves this file "
                                       "actually ran. Reported as `xfailed`. "
                                       "If it vanishes from the summary, the "
                                       "suite's green is fake. Do not delete.")
def test_zzz_must_fail_control():
    assert False, "control: this assertion is designed to fail"
