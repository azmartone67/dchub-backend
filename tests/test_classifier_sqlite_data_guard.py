"""SQLite-as-DATA guard for the mechanical classifier.

The brain's `sqlite_datetime_on_pg` transform class swaps SQLite-isms like
``datetime('now','-7 days')`` for the Postgres ``NOW() - INTERVAL ...`` form.
That is correct when the matched syntax is EXECUTABLE SQL — but it is a
disaster when the same syntax is DATA: db_utils.SQLITE_TO_PG_FUNC holds
``"datetime('now', '-7 days')"`` as a DICT KEY (the SOURCE side of the
translation table). The brain once "fixed" that key to Postgres, DESTROYING
the SQLite->PG mapping.

These tests pin the guard added to routes/brain_mechanical_classifier.py:
  (a) the db_utils dict-key / translation-table case  -> is_mechanical False
  (b) the SAME sqlite syntax inside a real cur.execute(...) SQL string in a
      normal file                                     -> still is_mechanical True
  + conservative behaviors (unreadable file blocks; other classes untouched).

classify_mechanical lives in a flask-importing module, so we install
lightweight `flask` / `internal_auth` shims in sys.modules when the real
modules are absent (mirrors tests/test_brain_draft_pr_writer.py). The guard
reads the target file off disk, so each case writes a real temp file under
the repo root and uses its repo-relative path.

Run:  python3 -m pytest tests/test_classifier_sqlite_data_guard.py -v
"""
import os
import sys
import types
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _flask_shim():
    """Install lightweight flask / internal_auth shims ONLY when the real
    modules are absent, and ALWAYS restore sys.modules on teardown so this file
    never pollutes other test files sharing the process. Force a fresh
    classifier import under whatever flask is present."""
    saved = {k: sys.modules.get(k) for k in
             ("flask", "internal_auth", "routes.brain_mechanical_classifier")}
    installed = []
    if "flask" not in sys.modules:
        flask = types.ModuleType("flask")

        class _BP:
            def __init__(self, *a, **k):
                pass

            def _noop(self, *a, **k):
                return lambda fn: fn

            get = post = route = _noop

        flask.Blueprint = _BP
        flask.jsonify = lambda *a, **k: (a, k)
        flask.request = types.SimpleNamespace(
            headers={}, args={}, get_json=lambda *a, **k: {})
        sys.modules["flask"] = flask
        installed.append("flask")
    if "internal_auth" not in sys.modules:
        ia = types.ModuleType("internal_auth")
        ia.accepted_internal_keys = lambda: set()
        ia.is_valid_internal_key = lambda k: False
        sys.modules["internal_auth"] = ia
        installed.append("internal_auth")
    sys.modules.pop("routes.brain_mechanical_classifier", None)
    try:
        yield
    finally:
        for k in installed:
            sys.modules.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v
            else:
                sys.modules.pop(k, None)


def _clf():
    import routes.brain_mechanical_classifier as m
    return m


# ── temp-file helper: write a real repo-relative file, yield its rel path ──
class _TempRepoFile:
    """Write `content` to a unique temp file under the repo root and expose its
    repo-relative path (what the classifier's _read_file resolves). Removed on
    exit. We put it under tests/ so it never collides with package imports."""

    def __init__(self, content: str, suffix: str = ".py"):
        self.rel = os.path.join("tests", f"_tmp_sqlite_guard_{uuid.uuid4().hex[:10]}{suffix}")
        self.full = os.path.join(ROOT, self.rel)
        self._content = content

    def __enter__(self):
        with open(self.full, "w", encoding="utf-8") as f:
            f.write(self._content)
        return self.rel

    def __exit__(self, *exc):
        try:
            os.remove(self.full)
        except OSError:
            pass


SQLITE_SEARCH = "datetime('now', '-7 days')"
PG_REPLACE = "NOW() - INTERVAL '7 days'"


# ─────────────────────────────────────────────────────────────────────
# (a) DATA: the sqlite syntax is a DICT KEY in a SQLITE_TO_PG table.
#     This is the exact false-positive that corrupted db_utils.py.
# ─────────────────────────────────────────────────────────────────────
def test_sqlite_dict_key_in_translation_table_is_not_mechanical():
    m = _clf()
    # A faithful miniature of db_utils.SQLITE_TO_PG_FUNC — sqlite syntax as KEY.
    content = (
        "SQLITE_TO_PG_FUNC = {\n"
        "    \"datetime('now', '-7 days')\": \"(NOW() - INTERVAL '7 days')\",\n"
        "    \"datetime('now', '-30 days')\": \"(NOW() - INTERVAL '30 days')\",\n"
        "}\n"
    )
    with _TempRepoFile(content) as rel:
        proposal = {
            "id": 183,
            "file_path": rel,
            "search_text": SQLITE_SEARCH,
            "replace_text": PG_REPLACE,
            "confidence": 0.95,
        }
        v = m.classify_mechanical(proposal)

    assert v["klass"] == "sqlite_datetime_on_pg", v
    assert v["is_mechanical"] is False, v
    # The context guard must be the reason (dict-key and/or translation-map).
    guard = [b for b in v["blocked_by"] if "sqlite-data guard" in b]
    assert guard, v["blocked_by"]
    assert any("DATA" in b for b in guard), guard


def test_real_db_utils_file_is_blocked():
    """End-to-end against the LIVE db_utils.py (the real translation table).
    Belt-and-suspenders: BOTH the per-class forbidden path AND the content
    context-check fire, so the brain can never re-corrupt the live mapping."""
    m = _clf()
    db_utils_path = os.path.join(ROOT, "db_utils.py")
    if not os.path.exists(db_utils_path):
        pytest.skip("db_utils.py not present in this checkout")
    proposal = {
        "id": 184,
        "file_path": "db_utils.py",
        "search_text": SQLITE_SEARCH,
        "replace_text": PG_REPLACE,
        "confidence": 0.95,
    }
    v = m.classify_mechanical(proposal)
    assert v["is_mechanical"] is False, v
    guard = [b for b in v["blocked_by"] if "sqlite-data guard" in b]
    assert guard, v["blocked_by"]
    # per-class forbidden path defense present
    assert any("translation-table path" in b for b in guard), guard


# ─────────────────────────────────────────────────────────────────────
# (b) SQL: the SAME sqlite syntax inside a real cur.execute(...) string in a
#     normal file -> still mechanical (we must not over-block).
# ─────────────────────────────────────────────────────────────────────
def test_sqlite_in_real_execute_sql_is_still_mechanical():
    m = _clf()
    content = (
        "def recent(cur):\n"
        "    cur.execute(\n"
        "        \"SELECT count(*) FROM events \"\n"
        "        \"WHERE ts > datetime('now', '-7 days')\"\n"
        "    )\n"
        "    return cur.fetchone()\n"
    )
    with _TempRepoFile(content) as rel:
        proposal = {
            "id": 500,
            "file_path": rel,
            "search_text": SQLITE_SEARCH,
            "replace_text": PG_REPLACE,
            "confidence": 0.9,
        }
        v = m.classify_mechanical(proposal)

    assert v["klass"] == "sqlite_datetime_on_pg", v
    assert v["is_mechanical"] is True, v
    assert not v["blocked_by"], v["blocked_by"]
    assert any("executable SQL" in r for r in v["reasons"]), v["reasons"]


# ─────────────────────────────────────────────────────────────────────
# Conservative: an unreadable target file BLOCKS the sqlite class (a
# false-positive here is harmful, so when we cannot verify we refuse).
# ─────────────────────────────────────────────────────────────────────
def test_unreadable_file_blocks_sqlite_class():
    m = _clf()
    proposal = {
        "id": 501,
        "file_path": "routes/__no_such_file_for_sqlite_guard__.py",
        "search_text": SQLITE_SEARCH,
        "replace_text": PG_REPLACE,
        "confidence": 0.9,
    }
    v = m.classify_mechanical(proposal)
    assert v["is_mechanical"] is False, v
    guard = [b for b in v["blocked_by"] if "sqlite-data guard" in b]
    assert guard and any("unreadable" in b for b in guard), v["blocked_by"]


# ─────────────────────────────────────────────────────────────────────
# Do NOT weaken the OTHER classes: interval_literal must keep its reach,
# even when pointed at db_utils.py, and the sqlite guard must not fire on it.
# ─────────────────────────────────────────────────────────────────────
def test_other_class_not_affected_by_sqlite_guard():
    m = _clf()
    content = (
        "def q(cur, n):\n"
        "    cur.execute(\"SELECT 1 FROM t WHERE ts > NOW() - INTERVAL '%s days'\", (n,))\n"
    )
    with _TempRepoFile(content) as rel:
        proposal = {
            "id": 502,
            "file_path": rel,
            "search_text": "NOW() - INTERVAL '%s days'",
            "replace_text": "NOW() - %s * INTERVAL '1 day'",
            "confidence": 0.9,
        }
        v = m.classify_mechanical(proposal)

    assert v["klass"] == "interval_literal", v
    assert v["is_mechanical"] is True, v
    assert not any("sqlite-data guard" in b for b in v["blocked_by"]), v["blocked_by"]


def test_interval_literal_at_db_utils_does_not_trigger_sqlite_guard():
    """Scope check: the sqlite guard is keyed on the MATCHED class, not the
    path. An interval_literal proposal aimed at db_utils.py must not be
    rejected BY the sqlite-data guard (it may pass or fail other gates, but the
    sqlite guard specifically must stay silent)."""
    m = _clf()
    proposal = {
        "id": 503,
        "file_path": "db_utils.py",
        "search_text": "NOW() - INTERVAL '%s days'",
        "replace_text": "NOW() - %s * INTERVAL '1 day'",
        "confidence": 0.9,
    }
    v = m.classify_mechanical(proposal)
    assert v["klass"] == "interval_literal", v
    assert not any("sqlite-data guard" in b for b in v["blocked_by"]), v["blocked_by"]


# ─────────────────────────────────────────────────────────────────────
# Direct unit coverage of the guard helpers.
# ─────────────────────────────────────────────────────────────────────
def test_helper_sqlite_class_forbidden_paths():
    m = _clf()
    assert m._sqlite_class_forbidden_path_hits("db_utils.py")
    assert m._sqlite_class_forbidden_path_hits("routes/sql_translation.py")
    assert m._sqlite_class_forbidden_path_hits("lib/sqlite_to_pg.py")
    assert not m._sqlite_class_forbidden_path_hits("routes/grid_status.py")


def test_helper_context_block_detects_dict_key_without_marker():
    """A dict-key match must be caught even when the file does NOT contain the
    SQLITE_TO_PG marker (the colon-after-quoted-key shape alone is enough)."""
    m = _clf()
    content = (
        "MY_MAP = {\n"
        "    \"datetime('now', '-7 days')\": \"week ago\",\n"
        "}\n"
    )
    with _TempRepoFile(content) as rel:
        block = m._sqlite_data_context_block(rel, SQLITE_SEARCH)
    assert block is not None
    assert "DICT KEY" in block, block


def test_helper_context_block_allows_real_sql():
    m = _clf()
    content = "x = run(\"WHERE ts > datetime('now', '-7 days') ORDER BY ts\")\n"
    with _TempRepoFile(content) as rel:
        block = m._sqlite_data_context_block(rel, SQLITE_SEARCH)
    assert block is None, block
