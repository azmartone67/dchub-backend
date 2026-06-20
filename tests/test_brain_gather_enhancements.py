"""
tests/test_brain_gather_enhancements.py — Brain GATHER-step Source 7 + Source 8.

Covers the two NEW evidence sources wired into the Investigator's GATHER step:
  · Source 7 — routes.brain_data_gatherer.gather_facility_breakdowns()
  · Source 8 — routes.brain_capability_ledger.gather_live_capabilities()

These run INSIDE a live autonomous system, so the tests are SAFETY tests, not
data tests. NO real Anthropic API, NO network, NO Postgres — every DB touch is
monkeypatched. We prove the contract the brain depends on:

  1. SHAPE — each _gather function returns a list[dict] in the GATHER evidence
     shape {"claim","source","value"} (str/num value), with BOTH a None conn
     (DB unreachable) AND a fake cursor (DB present) stubbed.
  2. NEVER RAISES — a _conn whose cursor explodes mid-query degrades to a
     skipped source ([] or partial), never an exception that would break
     gather_evidence()'s synchronous ~48s investigation.
  3. FLAGS GATE — flag OFF -> [] and ZERO DB access (a _conn that raises if
     ever called proves the gate short-circuits before any connection).
  4. SQL IS SELECT-ONLY — the pre-written query constants contain no INSERT /
     UPDATE / DELETE / DROP / TRUNCATE outside the capability-ledger's single
     CREATE TABLE IF NOT EXISTS + its idempotent seed UPSERT.
  5. WIRING GUARD — brain_investigator.gather_evidence() still runs and returns
     a list with BOTH new flags ON and OFF (a failing source never breaks it).
"""
import re

import pytest

# Both modules import cleanly with no heavy deps (psycopg2 is imported lazily
# INSIDE _conn, so the module imports even where psycopg2 is absent); use
# importorskip for parity with the other brain tests + CI's no-flask lanes.
dg = pytest.importorskip("routes.brain_data_gatherer")
cl = pytest.importorskip("routes.brain_capability_ledger")


EVIDENCE_KEYS = {"claim", "source", "value"}


# ════════════════════════════════════════════════════════════════════
#  Fakes — a cursor/connection that NEVER touches Postgres.
# ════════════════════════════════════════════════════════════════════
class _FakeCursor:
    """A minimal DB-API cursor that answers the exact queries Source 7 / 8 run.

    Routes on SQL substrings so it works regardless of whitespace. Records every
    executed statement so a test can assert nothing destructive ran."""

    def __init__(self, executed):
        self._executed = executed
        self._result = []

    def execute(self, sql, params=None):
        self._executed.append(sql)
        s = " ".join(str(sql).split()).lower()
        if "information_schema.columns" in s:
            # Source 7 schema probe — report every column it asks about exists.
            self._result = [(c,) for c in
                            ("country", "provider", "state", "is_duplicate", "merged_at")]
        elif "set statement_timeout" in s:
            self._result = []
        elif "count(*) as tracked" in s and "group by" not in s:
            # _TOTAL_SQL: (tracked, verified, unverified)
            self._result = [(21804, 2102, 19702)]
        elif "group by country" in s or ("group by 1" in s and "country" in s and "provider" not in s and " m." not in s):
            self._result = [("US", 9632, 1859), ("DE", 978, 32)]
        elif "group by provider" in s or ("group by 1" in s and "provider" in s):
            self._result = [("Equinix", 664, 48), ("Digital Realty", 676, 32)]
        elif "country = 'us'" in s or "left join" in s:
            # US-ISO breakdown
            self._result = [("PJM", 2141, 307), ("WECC", 1311, 201)]
        elif "from brain_live_capabilities" in s:
            # Source 8 ledger read: (name, status, location)
            self._result = [
                ("persist_command / durable-key-at-first-call", "LIVE", "server.mjs:1380"),
                ("anonymous per-IP daily soft cap (ANON_DAILY_CAP)", "INERT", "server.mjs:1287"),
            ]
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, executed=None):
        self.executed = executed if executed is not None else []
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self.executed)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _BoomCursor:
    """A cursor that raises on EVERY execute — proves best-effort degrades to a
    skipped source instead of propagating the exception."""

    def execute(self, *a, **k):
        raise RuntimeError("boom — simulated DB explosion")

    def fetchone(self):
        raise RuntimeError("boom")

    def fetchall(self):
        raise RuntimeError("boom")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _BoomConn:
    autocommit = False

    def cursor(self):
        return _BoomCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _boom_conn(*a, **k):
    raise AssertionError("_conn must NOT be called — the flag gate should have "
                         "short-circuited before any DB access")


def _assert_evidence_shape(items):
    """Every item is a dict with EXACTLY the GATHER keys and a str/num value."""
    assert isinstance(items, list)
    for it in items:
        assert isinstance(it, dict)
        assert set(it.keys()) == EVIDENCE_KEYS, it
        assert isinstance(it.get("value"), (str, int, float)), it
        assert isinstance(it.get("claim"), str) and it["claim"]
        assert isinstance(it.get("source"), str) and it["source"]


# ════════════════════════════════════════════════════════════════════
#  Source 7 — gather_facility_breakdowns()
# ════════════════════════════════════════════════════════════════════
def test_s7_flag_off_returns_empty_zero_db(monkeypatch):
    """Flag OFF -> [] and _conn is NEVER called (gate short-circuits)."""
    monkeypatch.setattr(dg, "_enabled", lambda: False)
    monkeypatch.setattr(dg, "_conn", _boom_conn)
    assert dg.gather_facility_breakdowns() == []


def test_s7_flag_on_none_conn_returns_empty(monkeypatch):
    """Flag ON but DB unreachable (_conn -> None) -> [] (source unavailable)."""
    monkeypatch.setattr(dg, "_enabled", lambda: True)
    monkeypatch.setattr(dg, "_conn", lambda: None)
    out = dg.gather_facility_breakdowns()
    assert out == []
    _assert_evidence_shape(out)


def test_s7_flag_on_fake_cursor_returns_shaped_items(monkeypatch):
    """Flag ON + a working fake cursor -> list[dict] of the evidence shape."""
    monkeypatch.setattr(dg, "_enabled", lambda: True)
    monkeypatch.setattr(dg, "_conn", lambda: _FakeConn())
    out = dg.gather_facility_breakdowns()
    assert isinstance(out, list) and len(out) >= 1
    _assert_evidence_shape(out)
    # The canonical total line is the anchor item.
    joined = " ".join(str(i["value"]) for i in out)
    assert "tracked" in joined.lower()


def test_s7_boom_cursor_never_raises(monkeypatch):
    """A cursor that explodes mid-query degrades to [] (or partial), NEVER
    raises — gather_evidence() must not be broken by a bad DB."""
    monkeypatch.setattr(dg, "_enabled", lambda: True)
    monkeypatch.setattr(dg, "_conn", lambda: _BoomConn())
    out = dg.gather_facility_breakdowns()  # must not raise
    assert isinstance(out, list)
    _assert_evidence_shape(out)


def test_s7_conn_is_best_effort_none_on_failure(monkeypatch):
    """_conn() is itself best-effort: a DB failure makes it return None (it has
    its own try/except), and gather_facility_breakdowns() then degrades to [].
    This is the realistic 'DB unreachable' contract — the gather fn never sees a
    raising _conn; and the gather_evidence() wiring also wraps the whole call
    (see test_gather_evidence_survives_a_throwing_new_source)."""
    monkeypatch.setattr(dg, "_enabled", lambda: True)
    monkeypatch.setattr(dg, "_conn", lambda: None)
    out = dg.gather_facility_breakdowns()  # must not raise
    assert out == []


# ════════════════════════════════════════════════════════════════════
#  Source 8 — gather_live_capabilities()
# ════════════════════════════════════════════════════════════════════
def test_s8_flag_off_returns_empty_zero_db(monkeypatch):
    """Flag OFF -> [] and _conn is NEVER called."""
    monkeypatch.setattr(cl, "_enabled", lambda: False)
    monkeypatch.setattr(cl, "_conn", _boom_conn)
    assert cl.gather_live_capabilities() == []


def test_s8_flag_on_none_conn_falls_back_to_curated(monkeypatch):
    """Flag ON but DB unreachable -> falls back to the in-code curated list and
    still returns ONE shaped evidence item (works before the seed has run)."""
    monkeypatch.setattr(cl, "_enabled", lambda: True)
    monkeypatch.setattr(cl, "_conn", lambda: None)
    out = cl.gather_live_capabilities()
    assert isinstance(out, list) and len(out) == 1
    _assert_evidence_shape(out)
    # Must mention the surface the brain kept mis-flagging.
    assert "persist_command" in out[0]["value"]


def test_s8_flag_on_fake_cursor_reads_ledger(monkeypatch):
    """Flag ON + a working fake cursor -> reads the ledger and returns ONE
    shaped item summarizing live capabilities."""
    monkeypatch.setattr(cl, "_enabled", lambda: True)
    monkeypatch.setattr(cl, "_conn", lambda: _FakeConn())
    out = cl.gather_live_capabilities()
    assert isinstance(out, list) and len(out) == 1
    _assert_evidence_shape(out)
    assert "ALREADY LIVE" in out[0]["value"]


def test_s8_boom_cursor_never_raises(monkeypatch):
    """A ledger read that explodes degrades to the curated fallback or [],
    NEVER raises."""
    monkeypatch.setattr(cl, "_enabled", lambda: True)
    monkeypatch.setattr(cl, "_conn", lambda: _BoomConn())
    out = cl.gather_live_capabilities()  # must not raise
    assert isinstance(out, list)
    _assert_evidence_shape(out)
    # Even when the read blows up, the in-code curated fallback still answers.
    assert len(out) == 1


# ════════════════════════════════════════════════════════════════════
#  SQL is SELECT-ONLY (no destructive DML outside the ledger CREATE/seed).
# ════════════════════════════════════════════════════════════════════
_DESTRUCTIVE = re.compile(r"\b(insert|update|delete|drop|truncate|alter)\b", re.I)


def _module_sql_constants(mod):
    """Yield every module-level str constant that looks like SQL (contains
    SELECT / FROM / a DDL/DML verb)."""
    for name in dir(mod):
        val = getattr(mod, name)
        if isinstance(val, str) and re.search(r"\b(select|from|insert|update|delete|drop|create|truncate|alter)\b", val, re.I):
            yield name, val


def test_s7_sql_constants_are_select_only():
    """Source 7 exposes ONLY read queries — no INSERT/UPDATE/DELETE/DROP/etc.
    The schema-probe + 4 breakdown queries must all be SELECT-only."""
    found = list(_module_sql_constants(dg))
    assert found, "expected to find Source 7 SQL constants to audit"
    for name, sql in found:
        assert not _DESTRUCTIVE.search(sql), f"{name} contains destructive SQL: {sql!r}"
        assert re.search(r"\bselect\b", sql, re.I), f"{name} is not a SELECT: {sql!r}"


def test_s8_sql_outside_ledger_create_is_select_only():
    """Source 8's ONLY permitted writes are the capability-ledger CREATE TABLE
    IF NOT EXISTS + its idempotent seed UPSERT. Every OTHER SQL constant must be
    read-only. We allow CREATE/INSERT...ON CONFLICT ONLY when it targets
    brain_live_capabilities."""
    for name, sql in _module_sql_constants(cl):
        low = sql.lower()
        is_ledger_write = "brain_live_capabilities" in low and (
            "create table if not exists" in low or "on conflict" in low or low.strip().startswith("insert into")
        )
        if is_ledger_write:
            # Permitted ledger DDL/UPSERT — must NOT touch any other table and
            # must NOT contain DELETE/DROP/TRUNCATE.
            assert not re.search(r"\b(delete|drop|truncate|alter)\b", low), \
                f"{name} ledger write contains destructive verb: {sql!r}"
            continue
        assert not _DESTRUCTIVE.search(sql), \
            f"{name} contains destructive SQL outside the ledger write: {sql!r}"


def test_s8_only_writes_target_the_ledger_table():
    """Defense in depth: scan the module SOURCE for any write verb and assert
    every INSERT/UPDATE/CREATE references brain_live_capabilities (the one
    table the safety rails permit), and there is NO DELETE/DROP/TRUNCATE at
    all."""
    import inspect
    src = inspect.getsource(cl)
    # No destructive table ops anywhere in the module.
    for verb in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
        assert verb.lower() not in src.lower(), f"module contains {verb}"
    # Every INSERT/CREATE TABLE statement names the ledger table. Normalize
    # whitespace first so multi-line SQL is matched, and ALWAYS consume an
    # "IF NOT EXISTS" clause when present so it isn't mistaken for the table.
    norm = " ".join(src.split())
    for m in re.finditer(
        r"(?:insert\s+into|create\s+table(?:\s+if\s+not\s+exists)?)\s+([a-z_][a-z0-9_\.\"]*)",
        norm, re.I,
    ):
        target = m.group(1).strip('"')
        # Skip the SQL-keyword false-positive where the optional IF-NOT-EXISTS
        # clause wasn't consumed (prose mentions of "CREATE TABLE IF NOT EXISTS").
        if target.lower() in ("if", "not", "exists"):
            continue
        assert target == "brain_live_capabilities", \
            f"write targets unexpected table {target!r}: {m.group(0)!r}"


# ════════════════════════════════════════════════════════════════════
#  WIRING GUARD — gather_evidence() runs with both new flags on AND off.
# ════════════════════════════════════════════════════════════════════
@pytest.fixture()
def _bi():
    return pytest.importorskip("routes.brain_investigator")


def _neutralize_all_sources(monkeypatch, bi):
    """Stub EVERY existing GATHER source so the wiring guard exercises only the
    Source 7/8 integration without needing a DB / network / canonical_stats."""
    # Sources 1-6 helpers the function calls directly or via import.
    monkeypatch.setattr(bi, "_gather_recent_findings", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(bi, "_gather_health_baseline", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(bi, "gather_growth_funnel", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(bi, "_gather_paid_reconciliation", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(bi, "gather_retention_cohorts", lambda *a, **k: [], raising=False)
    # canonical_stats import is inside the function; neutralize the conn so the
    # Source-1 try/except degrades cleanly even if the module is importable.
    monkeypatch.setattr(bi, "_conn", lambda: None, raising=False)


def test_gather_evidence_runs_with_both_flags_off(monkeypatch, _bi):
    """gather_evidence() returns a list and NEVER raises with both new flags
    OFF — the new sources contribute nothing and don't break the chain."""
    bi = _bi
    _neutralize_all_sources(monkeypatch, bi)
    monkeypatch.setattr(dg, "_enabled", lambda: False)
    monkeypatch.setattr(cl, "_enabled", lambda: False)
    out = bi.gather_evidence()
    assert isinstance(out, list)
    _assert_evidence_shape(out)


def test_gather_evidence_runs_with_both_flags_on(monkeypatch, _bi):
    """Both new flags ON with working fake cursors -> gather_evidence() includes
    Source 7 + Source 8 items and still returns a well-shaped list, never
    raising."""
    bi = _bi
    _neutralize_all_sources(monkeypatch, bi)
    monkeypatch.setattr(dg, "_enabled", lambda: True)
    monkeypatch.setattr(dg, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(cl, "_enabled", lambda: True)
    monkeypatch.setattr(cl, "_conn", lambda: _FakeConn())
    out = bi.gather_evidence()
    assert isinstance(out, list)
    _assert_evidence_shape(out)
    blob = " ".join(str(i["source"]) for i in out)
    assert "discovered_facilities" in blob          # Source 7 present
    assert "brain_live_capabilities" in blob        # Source 8 present


def test_gather_evidence_survives_a_throwing_new_source(monkeypatch, _bi):
    """If a NEW source raises despite being best-effort, gather_evidence()'s
    per-source try/except still keeps the investigation alive (never raises)."""
    bi = _bi
    _neutralize_all_sources(monkeypatch, bi)

    def _raise(*a, **k):
        raise RuntimeError("source boom")
    # Force the module-level gather fns to raise; the wiring try/except catches.
    monkeypatch.setattr(dg, "gather_facility_breakdowns", _raise, raising=False)
    monkeypatch.setattr(cl, "gather_live_capabilities", _raise, raising=False)
    out = bi.gather_evidence()  # must not raise
    assert isinstance(out, list)
