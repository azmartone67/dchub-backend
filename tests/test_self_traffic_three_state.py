"""self_traffic must have a third state (2026-09-20).

mcp_call_log is written by a SEPARATE fire-and-forget /track request, so the
first signal of a session routinely has no evidence yet. The resolver used to
call that External and store False; the backfill only revisits
`WHERE self_traffic IS NULL`; and nothing schedules the backfill anyway. So the
first signal of every self-traffic session could be latched as real demand
forever, on the board that sets priorities.

Measured 2026-09-20: one manual POST /api/v1/admin/schema/repair moved
2_paywall_signals 1,280 -> 1,033 by itself — 247 signals of our own traffic had
been published as demand because nobody had run it.

CI-SAFETY: the DB is simulated in-process; no network, no real DB.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm(sql):
    return " ".join((sql or "").split())


class _FakeCursor:
    """Answers the resolver's query the way Postgres would, for BOTH the
    current shape and the one it replaced.

    ★ It reads the SQL it is handed rather than returning a canned row. A fake
    that ignores the query passes whatever the code asks, which is how a test
    survives the very revert it exists to catch."""

    def __init__(self, rows):
        self.rows = rows          # platform strings in this session
        self._result = None
        self.seen = []

    def execute(self, sql, params=None):
        self.seen.append(_norm(sql))
        q = _norm(sql).lower()
        ours = any((p or "").lower().startswith("dchub-") for p in self.rows)
        if "bool_or" in q:
            # aggregate over zero rows yields one row holding NULL
            self._result = (None,) if not self.rows else (ours,)
        elif "select 1" in q:
            self._result = (1,) if ours else None
        else:
            raise AssertionError(f"unrecognised resolver SQL: {sql!r}")

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._c = _FakeCursor(rows)

    def cursor(self):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def resolve(monkeypatch):
    import mcp_signal_canonical as m

    def _run(rows, mcp_client=None, session_id="sess-1"):
        monkeypatch.setattr(m, "_conn", lambda: _FakeConn(rows))
        return m._resolve_self_traffic(session_id, mcp_client)
    return _run


# ── the third state ──────────────────────────────────────────────────

def test_no_call_log_row_yet_is_unknown_not_external(resolve):
    """THE bug. The /track write has not landed, so there is no evidence
    either way. False here is permanent; None is healable."""
    assert resolve([]) is None


def test_calls_exist_and_none_are_ours_is_external(resolve):
    """Evidence that says no is different from no evidence. This one SHOULD be
    False — otherwise every real caller stays NULL forever and the heal churns
    over them on every read."""
    assert resolve(["claude", "chatgpt"]) is False


def test_any_call_in_the_session_resolved_to_ours(resolve):
    """Session-level, not per-row: platform resolution is per-request and
    flaky, so one tagged call in the session is enough."""
    assert resolve(["mcp", "dchub-internal"]) is True


def test_self_identifying_client_needs_no_lookup(resolve):
    assert resolve([], mcp_client="dchub-regression-test") is True


def test_missing_session_is_external_without_a_query(resolve):
    """No session id means nothing to correlate on — and it must not leave a
    row NULL that no future evidence could ever resolve."""
    assert resolve([], session_id="no-session") is False
    assert resolve([], session_id="") is False


def test_the_simulator_can_distinguish_the_two_query_shapes():
    """Guard on the guard. If the simulator answered both shapes identically,
    every test above would pass against the reverted SQL."""
    old, new = _FakeCursor([]), _FakeCursor([])
    old.execute("SELECT 1 FROM mcp_call_log WHERE session_id = %s LIMIT 1")
    new.execute("SELECT bool_or(platform LIKE 'dchub-%%') FROM mcp_call_log")
    assert old.fetchone() is None and new.fetchone() == (None,)


# ── the latch that made False permanent ──────────────────────────────

@pytest.fixture(scope="module")
def schema_src():
    with open(os.path.join(ROOT, "routes", "schema_repair.py"),
              encoding="utf-8") as fh:
        return fh.read()


def test_the_false_latch_has_a_grace_window(schema_src):
    """`SET self_traffic = FALSE WHERE self_traffic IS NULL` is irreversible —
    the TRUE backfill above it only fires on NULL. Unbounded, it freezes rows
    whose /track write is still in flight."""
    i = schema_src.index("UPDATE mcp_upgrade_signals SET self_traffic = FALSE")
    stmt = _norm(schema_src[i:schema_src.index('"""', i)])
    assert "self_traffic IS NULL" in stmt
    assert re.search(r"created_at < NOW\(\) - INTERVAL '[^']+'", stmt), (
        f"the FALSE latch has no age bound, so it races the evidence: {stmt}")


# ── and something must actually run the heal ─────────────────────────

@pytest.fixture(scope="module")
def heal_block(schema_src):
    i = schema_src.index("HEAL BEFORE READING")
    return schema_src[i:schema_src.index("# Stage 1: total tool calls", i)]


def test_the_board_heals_itself_before_reading(heal_block):
    """POST /api/v1/admin/schema/repair is a manual admin route with no
    scheduler and no boot hook, so the board decayed between invocations. It
    must not depend on somebody remembering."""
    q = _norm(heal_block)
    assert "UPDATE mcp_upgrade_signals" in q
    assert "SET self_traffic = TRUE" in q
    assert "self_traffic IS NULL" in q


def test_the_heal_never_writes_false(heal_block):
    """It promotes NULL -> TRUE only. Writing FALSE here would reinstate the
    permanent latch on the hottest path in the file."""
    assert "self_traffic = FALSE" not in _norm(heal_block)


def test_the_heal_is_bounded(heal_block):
    """An unbounded UPDATE on a GET that admin dashboards poll. The bound is an
    explicit LIMIT, not a statement_timeout — the pooler does not reliably
    honour a SET on a borrowed connection."""
    assert re.search(r"LIMIT \d+", _norm(heal_block)), "no LIMIT on the heal"


def test_a_failed_heal_reports_none_not_zero(heal_block):
    """0 rows healed is a MEASURED zero — "nothing needed healing". A failure
    that renders as 0 tells the reader the board is clean when nobody checked."""
    assert 'out["self_traffic_healed"] = None' in heal_block
    assert 'out["self_traffic_healed"] = cur.rowcount' in heal_block


def test_the_heal_cannot_take_the_board_down(heal_block):
    """Every stage below it is wrapped; this must be too, or one bad UPDATE
    turns the whole endpoint into a 500."""
    tree = ast.parse("if 1:\n" + "\n".join(
        "    " + ln for ln in heal_block.split("\n")[heal_block.split("\n").index(
            [l for l in heal_block.split("\n") if l.strip().startswith("try:")][0]):]))
    assert any(isinstance(n, ast.Try) for n in ast.walk(tree))
