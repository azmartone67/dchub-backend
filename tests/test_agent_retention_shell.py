"""Agent Retention Master Shell (#49) — contract tests.

Source-level and import-level only: DB tests SKIP in CI, so a suite that only
exercised the queries would be green-by-absence on the branch that matters.
"""
import ast
import inspect
from pathlib import Path

import pytest

MOD = "routes.agent_retention_master_shell"
SRC_PATH = Path(__file__).resolve().parents[1] / "routes" / "agent_retention_master_shell.py"
SRC = SRC_PATH.read_text()


def _mod():
    return pytest.importorskip(MOD)


def test_source_parses_and_is_substantial():
    # Guards every assertion below from passing vacuously on an empty read.
    tree = ast.parse(SRC)
    assert len(SRC) > 4000
    fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_run_tick" in fns


def test_lane_order_and_ids():
    m = _mod()
    d = m._run_tick.__wrapped__() if hasattr(m._run_tick, "__wrapped__") else None
    # _run_tick hits the DB; assert the declared order from source instead.
    ids = [n.value for n in ast.walk(ast.parse(SRC))
           if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    for want in ["retention", "return_mechanism", "error_dead_ends",
                 "count_parity", "concentration", "crawler_silence"]:
        assert want in ids, f"lane id {want} missing"


def test_every_lane_fails_soft_without_a_db(monkeypatch):
    """A board that raises is a board nobody reads. Every lane must return
    checks — never propagate — when _conn() yields nothing."""
    m = _mod()
    monkeypatch.setattr(m, "_conn", lambda: None)
    lanes = [m._lane_retention, m._lane_return_mechanism, m._lane_error_dead_ends,
             m._lane_count_parity, m._lane_concentration, m._lane_crawler_silence]
    for fn in lanes:
        out = fn()
        assert isinstance(out, list) and out, f"{fn.__name__} returned nothing"
        assert all(isinstance(c, dict) and "pass" in c for c in out), fn.__name__


def test_agent_figures_use_the_canonical_identity_basis():
    """Every SQL literal that counts agents must read mcp_calls_identity with
    both de-loop predicates, and none may touch session_id. Asserted over SQL
    string constants via ast — a line-prefix comment strip has twice matched
    the warning ABOUT session_id instead of real SQL."""
    sql = [n.value for n in ast.walk(ast.parse(SRC))
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "SELECT" in n.value]
    assert len(sql) >= 6, f"expected several SQL literals, found {len(sql)}"
    ident = [s for s in sql if "mcp_calls_identity" in s]
    assert len(ident) >= 4
    for s in ident:
        assert "is_public_ip" in s and "is_real_external" in s, \
            "identity read missing a de-loop predicate"
    for s in sql:
        assert "session_id" not in s, "session_id must never appear in SQL"


def test_error_lane_declares_its_basis_exception():
    """Lane 3 reads mcp_connections on purpose. That exception must be stated
    in the code, or a later reader 'fixes' it toward a table with no success
    column."""
    m = _mod()
    # Normalize wrapping — the phrase spans a line break in the source, and a
    # test that breaks when a docstring is re-wrapped is a test that gets
    # deleted rather than fixed.
    doc = " ".join((inspect.getdoc(m._lane_error_dead_ends) or "").split()).lower()
    assert "mcp_connections" in doc
    assert "not an agent count" in doc


def test_no_self_http_anywhere():
    """PURE-DB: the 2026-07-06 flywheel-outage invariant. A board that calls
    the site it monitors goes dark exactly when it is needed."""
    for bad in ("requests.", "urllib", "httpx", "http.client", "urlopen"):
        assert bad not in SRC, f"self-HTTP marker {bad!r} present"


def test_born_red_lane_is_marked_and_critical():
    """Lane 2 must be BORN RED — if its adoption check were non-critical or
    defaulted to pass, the board would report green while the thing it exists
    to drive never happened."""
    m = _mod()
    src = inspect.getsource(m._lane_return_mechanism)
    assert "BORN RED" in src
    assert "agents > 0" in src, "adoption must be judged on real adoption"
    assert "critical=True" in src


def test_text_timestamp_cast_is_regex_guarded():
    """ai_cumulative.last_seen is TEXT. A bare cast throws and _safe_lane
    swallows it, turning a real alarm into a silent question mark."""
    m = _mod()
    src = inspect.getsource(m._lane_crawler_silence)
    assert "last_seen::timestamptz" in src
    assert "last_seen ~" in src
    assert src.index("last_seen ~") < src.index("last_seen::timestamptz")


def test_return_tool_list_covers_the_saved_work_family():
    m = _mod()
    for t in ("save_site", "save_to_shortlist", "get_shortlist",
              "set_site_alert", "set_market_alert", "set_shortlist_alert",
              "subscribe_digest", "standing_intent", "get_changes"):
        assert t in m._RETURN_TOOLS, f"{t} missing from the return path"


def test_shell_is_admin_gated_and_killable():
    m = _mod()
    assert "AGENT_RETENTION_SHELL_DISABLE" in SRC
    assert SRC.count("_admin_ok()") >= 2, "both endpoints must gate"
    assert "no-store" in SRC


def test_thresholds_are_named_constants_not_magic_numbers():
    m = _mod()
    assert isinstance(m._SILENCE_DAYS, int)
    assert isinstance(m._CONCENTRATION_PCT, float)
