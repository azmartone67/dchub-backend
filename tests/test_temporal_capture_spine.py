"""tests/test_temporal_capture_spine.py — WS6 change-capture spine (#41).

Guards routes/temporal_capture.py, the append-only history spine. Every
assertion here corresponds to a failure this repo has actually shipped:

  (1) ZERO IS NOT EVIDENCE — routes/changes_feed.py:49-61 swallows a broken
      query into [] and reports it as "nothing changed". A layer this module
      could not measure must report None, never 0.
  (2) THE BACKFILL-CLUSTER TRAP — a bulk load stamps every row with one
      timestamp, which then reads as history. The first pass over a layer must
      write first_seen_exact=False and emit NO change events.
  (3) WEB-WORKER RECYCLE — a daemon thread writing single rows was killed
      twice with zero rows persisted. The chunk loop must COMMIT inside the
      loop and batch with execute_values(page_size=500).
  (4) LATE-LINE BLUEPRINT REGISTRATION silently 404s in prod (bit
      press_loop, market_deep_dive, competitor_recon). Registration must live
      in main.py's early safe zone.
  (5) DDL STORM — schema creation at import time runs on every boot/healthcheck.
  (6) LIVE != repo DDL — the layer registry must never be trusted blind; the
      module has to introspect and skip, which means every spec needs a key.

House rules (reference_dchub_green_main_0709): no `import main`; nothing runs
at module scope.

Run:  python3 -m pytest tests/test_temporal_capture_spine.py -v
"""
from __future__ import annotations

import ast
import datetime
import decimal
import hashlib
import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MOD = _ROOT / "routes" / "temporal_capture.py"
_MAIN = _ROOT / "main.py"
_LINT = _ROOT / "scripts" / "regression_lint.py"

# main.py's early wiring block. Registrations past this silently fail in prod.
_SAFE_ZONE_MAX_LINE = 3000


def _src() -> str:
    return _MOD.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    tree = ast.parse(_src())
    # ★ AST guard: an empty/failed parse passes every isinstance filter.
    # Assert the parse produced a real module before asserting anything on it.
    assert tree.body, "routes/temporal_capture.py parsed to an EMPTY module"
    return tree


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in routes/temporal_capture.py")


def _load_pure():
    """Execute the pure helpers against stubs — never import the module (it
    binds a Flask blueprint) and never import main."""
    ns = {"datetime": datetime, "decimal": decimal, "hashlib": hashlib,
          "json": json, "_KEY_SEP": "\x1f"}
    wanted = {"_norm_value", "_entity_key", "_payload_hash", "_diff_payload",
              "_blank_result"}
    got = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, "<temporal_capture>", "exec"), ns)
            got.add(node.name)
    assert got == wanted, f"missing pure helpers: {wanted - got}"
    return ns


def _layer_specs():
    """Read _LAYERS out of the source without importing the module."""
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_LAYERS":
                    specs = ast.literal_eval(node.value)
                    assert specs, "_LAYERS is empty — the spine captures nothing"
                    return specs
    raise AssertionError("_LAYERS not found")


def test_module_exists_and_parses():
    assert _MOD.exists(), "routes/temporal_capture.py is missing"
    assert len(_tree().body) > 5


def test_no_main_import():
    src = _src()
    assert "import main" not in src
    assert "from main import" not in src


def test_unmeasured_layers_report_none_not_zero():
    """(1) A layer we could not measure must not publish 0 — 0 reads as
    'nothing changed', which is the exact lie changes_feed tells today."""
    ns = _load_pure()
    for status in ("skipped_recent", "no_such_table", "missing_key_columns",
                   "no_tracked_columns", "no_database", "schema_failed"):
        res = ns["_blank_result"]("some_layer", status)
        assert res["status"] == status
        for field in ("scanned", "appeared", "changed", "unkeyable"):
            assert res[field] is None, (
                f"{status} published {field}={res[field]!r}; UNMEASURED must "
                "be None, never 0")


def test_norm_value_is_type_stable():
    ns = _load_pure()
    n = ns["_norm_value"]
    # NUMERIC(10,2) vs REAL vs float must not manufacture a phantom change.
    assert n(decimal.Decimal("100.00")) == n(100.0) == n(decimal.Decimal("100"))
    assert n(True) == "true" and n(1) == "1"      # bool checked before int
    assert n(None) is None                        # absent != empty string
    assert n(datetime.date(2026, 7, 29)) == "2026-07-29"
    assert n("  PJM ") == "PJM"


def test_entity_key_refuses_to_fold_unkeyable_rows():
    ns = _load_pure()
    k = ns["_entity_key"]
    assert k({"iso": "PJM", "queue_id": "AC1"}, ["iso", "queue_id"]) == "PJM\x1fAC1"
    # All-empty key => None, so the caller counts it instead of collapsing
    # every unkeyable row onto one shared key (which would emit fake changes).
    assert k({"iso": None, "queue_id": None}, ["iso", "queue_id"]) is None
    assert k({"iso": None, "queue_id": ""}, ["iso", "queue_id"]) is None
    assert k({"iso": "", "queue_id": "AC1"}, ["iso", "queue_id"]) is not None


def test_payload_hash_is_order_independent_and_null_sensitive():
    ns = _load_pure()
    h = ns["_payload_hash"]
    assert h({"a": "1", "b": "2"}) == h({"b": "2", "a": "1"})
    assert h({"a": None}) != h({"a": ""})


def test_diff_payload_reports_appearing_and_vanishing_fields():
    ns = _load_pure()
    d = ns["_diff_payload"]
    assert d({"s": "active"}, {"s": "withdrawn"}) == [("s", "active", "withdrawn")]
    assert d({"s": "a"}, {"s": "a"}) == []
    assert d({}, {"s": "a"}) == [("s", None, "a")]
    assert d({"s": "a"}, {}) == [("s", "a", None)]


def test_layer_registry_is_well_formed():
    """(6) Every layer needs a key the module can verify against the LIVE
    table, tracked columns, and a stated reason it is being captured."""
    specs = _layer_specs()
    names = [s["layer"] for s in specs]
    assert len(names) == len(set(names)), f"duplicate layer names: {names}"
    for s in specs:
        assert s.get("table"), s
        assert s.get("key"), f"{s['layer']} has no entity key"
        assert s.get("track"), f"{s['layer']} tracks no columns"
        assert s.get("why"), f"{s['layer']} has no stated basis for capture"
        assert not (set(s["track"]) & set(s["key"])), (
            f"{s['layer']}: key column inside track — identity is not a change")


def test_layers_cover_the_history_destroying_tables():
    """interconnect_queue history is DELETEd/DROPped by its loaders and
    hosting_capacity_feeders re-stamps ingested_at on every conflict. If the
    spine ever stops covering them, nothing else can."""
    names = {s["layer"] for s in _layer_specs()}
    for required in ("interconnect_queue", "hosting_capacity_feeders",
                     "capacity_pipeline", "discovered_facilities"):
        assert required in names, f"{required} dropped from the capture spine"


def test_no_disappearance_events_are_emitted():
    """A budget-truncated pass cannot tell 'missing' from 'deleted'. Emitting
    a disappearance would manufacture false events at every truncation."""
    src = _src()
    fn = _func("capture_layer")
    seg = ast.get_source_segment(src, fn) or ""
    for banned in ('"disappeared"', "'disappeared'", '"deleted"', "'removed'"):
        assert banned not in seg, (
            f"capture_layer emits {banned}; v1 must not infer disappearance")


def test_chunk_loop_commits_inside_the_loop():
    """(3) Web-worker recycle killed a daemon-thread writer twice with zero
    rows persisted. Progress must be durable per chunk, not at the end."""
    fn = _func("capture_layer")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
    assert loops, "capture_layer has no chunk loop"
    commits = [n for loop in loops for n in ast.walk(loop)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "commit"]
    assert commits, "no commit() inside the chunk loop — a recycle loses everything"


def test_batched_writes_use_page_500():
    fn = _func("capture_layer")
    pages = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "execute_values"):
            for kw in n.keywords:
                if kw.arg == "page_size":
                    pages.append(ast.literal_eval(kw.value))
    assert pages, "writes are not batched through execute_values"
    assert all(p == 500 for p in pages), f"page_size drift: {pages}"


def test_no_ddl_at_import_scope():
    """(5) Schema creation at import runs on every boot and every healthcheck
    restart — a DDL storm. _ensure_schema must only run inside a capture."""
    for node in _tree().body:
        assert not isinstance(node, (ast.Expr,)) or not isinstance(
            getattr(node, "value", None), ast.Call), (
            f"module-scope call at line {node.lineno} — no work at import time")


def test_registered_in_main_safe_zone():
    """(4) Late-line registration silently 404s in prod."""
    main_src = _MAIN.read_text(encoding="utf-8")
    hits = [i for i, line in enumerate(main_src.splitlines(), 1)
            if "temporal_capture_bp" in line]
    assert hits, "temporal_capture_bp is never registered in main.py"
    assert min(hits) < _SAFE_ZONE_MAX_LINE, (
        f"temporal_capture_bp registered at line {min(hits)} — past the safe "
        "zone; late-line registration silently fails in prod")


def test_append_only_tables_are_lint_whitelisted():
    """entity_changes / entity_capture_runs are append-only history with no
    natural key. regression_lint whitelists append-only tables BY NAME."""
    lint_src = _LINT.read_text(encoding="utf-8")
    for tbl in ("entity_changes", "entity_capture_runs"):
        assert f"'{tbl}'" in lint_src, (
            f"{tbl} missing from regression_lint WHITELIST_TABLES — the "
            "insert-no-on-conflict rule will fail the build")
