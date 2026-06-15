"""Regression guard for the 2026-06-15 brain_findings 22h freeze.

routes/brain_consistency_radar.py used `logger.info(...)` in the durable-persist
summary block but never defined `logger`. The NameError fired AFTER the
bf_summary savepoint was released, so the except handler did ROLLBACK TO an
already-released savepoint → aborted the transaction → the trailing
conn.commit() silently discarded every upsert. brain_findings froze for ~22h
while the scan kept reporting success.

This is a STATIC check (parses the source, no import, no DB) so it runs in CI
where DATABASE_URL is absent. It asserts:
  1. `logger` is assigned at module level, AND
  2. every `_persist_*_release_sp(... "bf_summary")` is the LAST statement in its
     try-block is too structural to assert statically — instead we assert there
     is no `logger.` usage that precedes the module-level `logger =` definition
     (the minimal guarantee that the name is bound before use).
"""
import ast
import os

_RADAR = os.path.join(os.path.dirname(__file__), "..", "routes",
                      "brain_consistency_radar.py")


def _module_level_names(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:  # top-level only
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def test_logger_is_defined_at_module_level():
    with open(os.path.abspath(_RADAR), "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    names = _module_level_names(tree)
    assert "logger" in names, (
        "brain_consistency_radar.py uses logger.* (e.g. in _persist_findings_to_db) "
        "but does not define `logger` at module level — this caused the 22h "
        "brain_findings persist freeze. Add: logger = logging.getLogger(__name__)"
    )
