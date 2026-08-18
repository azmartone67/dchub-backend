"""Registry truth × defunct list (r-registry-defunct-link, 2026-08-18).

WHY: mcp_registry_cleanup.py has recorded dead registries in
`mcp_registry_defunct` since 2026-05-25 (mcphive, dxt_so, …). routes/
registry_truth.py had ZERO references to that table, so both kept getting
crawled every day and kept recording a permanent RED verdict that nobody
could ever clear — 2 of 16 slots and 2 of the 5 non-green verdicts on the
board, describing something already known and unactionable.

These tests pin the two halves that matter and that a naive fix gets wrong:

  1. a defunct listing is REMOVED FROM THE DENOMINATOR, never passed — it
     must not appear in `counts`, must not become verified_ok, and must not
     be fetched at all; and
  2. the removal is PUBLISHED, never silent — `excluded_defunct` carries the
     name and the recorded reason on BOTH surfaces, and
     `tracked_including_defunct` still accounts for every tracked row so the
     set cannot shrink without a reader noticing.

(2) is the whole point. A silent skip is indistinguishable from the
`drift_detected = FALSE` blind spot this module exists to fix.

CI-SAFETY: no network, no real DB — the connection is a stub.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def rt():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import registry_truth as m
    return m


# ── a DB stub that answers by inspecting the SQL it is handed ────────────

class _Cur:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self._conn.sql.append(s)
        if "from mcp_registry_defunct" in s:
            if self._conn.defunct_raises:
                raise RuntimeError("relation does not exist")
            self._rows = list(self._conn.defunct)
        elif "from mcp_presence_listings" in s and "select registry_name" in s:
            self._rows = list(self._conn.listings)
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, listings=(), defunct=(), defunct_raises=False):
        self.listings = listings
        self.defunct = defunct
        self.defunct_raises = defunct_raises
        self.sql = []
        self.closed = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed += 1


def _install(rt, monkeypatch, conn):
    monkeypatch.setattr(rt, "_db", lambda: conn)
    monkeypatch.setattr(rt, "_ensure_columns", lambda cur: None)


# ── defunct_registries() ─────────────────────────────────────────────────

def test_defunct_registries_reads_key_and_reason(rt, monkeypatch):
    conn = _Conn(defunct=[("mcphive", "URL dead per r47 manual probe"),
                          ("dxt_so", "domain lapsed — parking page")])
    _install(rt, monkeypatch, conn)
    got = rt.defunct_registries()
    assert set(got) == {"mcphive", "dxt_so"}
    assert got["dxt_so"] == "domain lapsed — parking page"


def test_defunct_registries_fails_OPEN_on_db_error(rt, monkeypatch):
    """Fail open: a transient DB error must crawl everything, not silently
    drop listings off the board."""
    conn = _Conn(defunct_raises=True)
    _install(rt, monkeypatch, conn)
    assert rt.defunct_registries() == {}


def test_defunct_registries_empty_when_no_db(rt, monkeypatch):
    monkeypatch.setattr(rt, "_db", lambda: None)
    assert rt.defunct_registries() == {}


# ── run_scan() ───────────────────────────────────────────────────────────

def _scan(rt, monkeypatch, listings, defunct):
    conn = _Conn(listings=listings, defunct=defunct)
    _install(rt, monkeypatch, conn)
    fetched = []

    def _fake_fetch(url):
        fetched.append(url)
        return 200, url, "<html>DC Hub — dchub.cloud/mcp</html>"

    monkeypatch.setattr(rt, "_fetch", _fake_fetch)
    monkeypatch.setattr(rt, "load_canon", lambda: {"tools": 82}, raising=False)
    return rt.run_scan(), fetched


LISTINGS = [("dxt_so", "https://dxt.so/server/dchub"),
            ("glama", "https://glama.ai/mcp/connectors/cloud.dchub/x"),
            ("mcphive", "https://mcphive.com/")]
DEFUNCT = [("dxt_so", "domain lapsed"), ("mcphive", "URL dead")]


def test_scan_does_not_fetch_a_defunct_listing(rt, monkeypatch):
    out, fetched = _scan(rt, monkeypatch, LISTINGS, DEFUNCT)
    assert fetched == ["https://glama.ai/mcp/connectors/cloud.dchub/x"]
    assert out["checked"] == 1


def test_scan_publishes_the_exclusion_with_its_reason(rt, monkeypatch):
    out, _ = _scan(rt, monkeypatch, LISTINGS, DEFUNCT)
    ex = {e["registry"]: e["reason"] for e in out["excluded_defunct"]}
    assert ex == {"dxt_so": "domain lapsed", "mcphive": "URL dead"}


def test_scan_never_counts_a_defunct_listing_as_a_verdict(rt, monkeypatch):
    """A removal from the denominator, NOT a pass."""
    out, _ = _scan(rt, monkeypatch, LISTINGS, DEFUNCT)
    assert sum(out["counts"].values()) == 1
    names_with_verdicts = {r["registry"] for r in out["listings"]}
    assert names_with_verdicts == {"glama"}
    assert "dxt_so" not in names_with_verdicts


def test_scan_total_still_accounts_for_every_tracked_row(rt, monkeypatch):
    """The set cannot shrink silently."""
    out, _ = _scan(rt, monkeypatch, LISTINGS, DEFUNCT)
    assert out["tracked_including_defunct"] == len(LISTINGS)
    assert out["checked"] + len(out["excluded_defunct"]) == len(LISTINGS)


def test_scan_with_no_defunct_rows_is_unchanged(rt, monkeypatch):
    out, fetched = _scan(rt, monkeypatch, LISTINGS, [])
    assert len(fetched) == 3
    assert out["checked"] == 3
    assert out["excluded_defunct"] == []


# ── read_state() ─────────────────────────────────────────────────────────

class _StateCur(_Cur):
    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self._conn.sql.append(s)
        if "from mcp_registry_defunct" in s:
            self._rows = list(self._conn.defunct)
        else:
            self._rows = list(self._conn.state_rows)


class _StateConn(_Conn):
    def __init__(self, state_rows, defunct):
        super().__init__(defunct=defunct)
        self.state_rows = state_rows

    def cursor(self):
        return _StateCur(self)


STATE_ROWS = [
    # (name, verdict, reason, checked, ok_at, days_since_ok)
    ("dxt_so", "unverified", "fetch failed", None, None, 99.0),
    ("glama", "verified_ok", "in sync", None, None, 0.0),
    ("mcphive", "broken", "not our page", None, None, 99.0),
]


def test_read_state_drops_defunct_from_counts_and_critical(rt, monkeypatch):
    conn = _StateConn(STATE_ROWS, DEFUNCT)
    _install(rt, monkeypatch, conn)
    out = rt.read_state()
    assert out["counts"] == {"verified_ok": 1}
    assert out["broken"] == []
    assert out["stale_unverified"] == []
    # mcphive was the ONLY broken row and dxt_so the only stale unverified —
    # with both recorded defunct the board is no longer critical.
    assert out["critical"] is False


def test_read_state_publishes_exclusion_and_last_verdict(rt, monkeypatch):
    conn = _StateConn(STATE_ROWS, DEFUNCT)
    _install(rt, monkeypatch, conn)
    out = rt.read_state()
    ex = {e["registry"]: e for e in out["excluded_defunct"]}
    assert set(ex) == {"dxt_so", "mcphive"}
    assert ex["mcphive"]["reason"] == "URL dead"
    # the last real observation stays readable rather than being overwritten
    assert ex["mcphive"]["last_verdict"] == "broken"
    assert out["tracked_including_defunct"] == 3


def test_read_state_still_goes_critical_on_a_live_broken_listing(rt, monkeypatch):
    """The exclusion must not be able to suppress a REAL red."""
    rows = STATE_ROWS + [("cline", "broken", "not our page", None, None, 9.0)]
    conn = _StateConn(rows, DEFUNCT)
    _install(rt, monkeypatch, conn)
    out = rt.read_state()
    assert out["broken"] == ["cline"]
    assert out["critical"] is True


def test_read_state_with_no_defunct_rows_is_unchanged(rt, monkeypatch):
    conn = _StateConn(STATE_ROWS, [])
    _install(rt, monkeypatch, conn)
    out = rt.read_state()
    assert sum(out["counts"].values()) == 3
    assert out["excluded_defunct"] == []
    assert out["critical"] is True
