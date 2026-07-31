"""ONE gridstatus.io client, budget-ledgered, loud on refusal (2026-07-31).

July 2026: the provider 403'd ("API requests limit reached. Usage: 375,
Limit: 250") while gridstatus_call_ledger showed only 45 calls —
routes/grid_data_master_shell._gs_get and
enhancements/iso_integrations.GridStatusClient made ~330 HTTP calls that never
consulted the budget guard, and the every-5-min heartbeat re-fired the
grid-data master tick 11-22x/day across its hour-wide dispatch window.

These tests fence the fix:
  1. the provider-host literal lives in exactly ONE production module
     (gridstatus_client.py) — a new bypass cannot land without tripping this;
  2. the chokepoint refuses BEFORE any HTTP once the ledger says the monthly
     budget is spent, with a machine-readable `budget_exhausted:` marker;
  3. all three former call paths route through the chokepoint;
  4. the master tick dedupes to one completed run per UTC day.
"""
import ast
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Built from pieces so this file can never satisfy its own fence.
PROVIDER_HOST = "api." + "gridstatus" + ".io"


# ── 1. the URL fence ──────────────────────────────────────────────────

def test_provider_host_literal_only_in_the_one_client():
    try:
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "*.py"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.splitlines()
    except Exception as e:  # pragma: no cover — exotic non-git checkouts only
        pytest.skip(f"git ls-files unavailable: {e}")
    assert tracked, "git ls-files returned nothing — fence would be vacuous"
    offenders = []
    for rel in tracked:
        if rel.startswith("tests/"):
            continue
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if PROVIDER_HOST in text and rel != "gridstatus_client.py":
            offenders.append(rel)
    assert not offenders, (
        f"{PROVIDER_HOST} referenced outside gridstatus_client.py: {offenders} — "
        "every provider call must go through the budget-ledgered client "
        "(owner directive 2026-07-26; July 2026 burned 375/250)"
    )
    assert PROVIDER_HOST in (ROOT / "gridstatus_client.py").read_text(), (
        "gridstatus_client.py no longer contains the provider host — "
        "the fence is checking the wrong module"
    )


# ── 2. refusal happens BEFORE HTTP ────────────────────────────────────

def test_budget_refusal_happens_before_any_http(monkeypatch):
    import gridstatus_client as gsc
    monkeypatch.setenv("GRIDSTATUS_API_KEY", "k-test")
    monkeypatch.setattr(gsc, "_budget_spend", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("HTTP was attempted after the budget refused")

    monkeypatch.setattr(gsc.requests, "get", _boom)
    rows, err = gsc.gridstatus_get("pjm_load", {"limit": 1}, caller="test")
    assert rows is None
    assert err is not None and err.startswith("budget_exhausted"), err


def test_missing_key_is_a_distinct_marker(monkeypatch):
    import gridstatus_client as gsc
    monkeypatch.delenv("GRIDSTATUS_API_KEY", raising=False)
    payload, err = gsc.gs_request("/datasets/pjm_load/query", caller="test")
    assert payload is None
    # radar keys off this exact marker to distinguish "no key" from a 403
    assert err == "source_unavailable: GRIDSTATUS_API_KEY not set"


# ── 3. every former bypass routes through the chokepoint ──────────────

def test_master_shell_gs_get_routes_through_the_client(monkeypatch):
    import gridstatus_client as gsc
    import routes.grid_data_master_shell as gds
    seen = {}

    def _fake(dataset, params=None, timeout=15, caller="unknown"):
        seen["dataset"], seen["caller"] = dataset, caller
        return [{"x": 1}], None

    monkeypatch.setattr(gsc, "gridstatus_get", _fake)
    rows, err = gds._gs_get("pjm_load", {"limit": 1})
    assert err is None and rows == [{"x": 1}]
    assert seen == {"dataset": "pjm_load", "caller": "grid_data_master_shell"}


def test_pjm_dataminer_routes_through_the_client(monkeypatch):
    import gridstatus_client as gsc
    import pjm_dataminer as pjm
    seen = {}

    def _fake(dataset, params=None, timeout=15, caller="unknown"):
        seen["dataset"], seen["caller"] = dataset, caller
        return [{"dom": 9000.0}], None

    monkeypatch.setattr(gsc, "gridstatus_get", _fake)
    rows, err = pjm._gridstatus_get("pjm_load", {"limit": 1})
    assert err is None and rows == [{"dom": 9000.0}]
    assert seen == {"dataset": "pjm_load", "caller": "pjm_dataminer"}


def test_iso_integrations_routes_through_the_client(monkeypatch):
    pytest.importorskip("requests")  # top-level import of the module under test
    import gridstatus_client as gsc
    from enhancements.iso_integrations import GridStatusClient
    calls = {}

    def _fake(path, params=None, timeout=15, caller="unknown", api_key=None):
        calls["path"], calls["caller"] = path, caller
        return None, "budget_exhausted: test"

    monkeypatch.setattr(gsc, "gs_request", _fake)
    out = GridStatusClient(api_key="k").get_fuel_mix("PJM")
    assert calls["caller"] == "iso_integrations"
    assert str(out.get("error", "")).startswith("budget_exhausted")
    assert out.get("status") == "failed"


# ── 4. one completed master tick per UTC day ──────────────────────────

class _Cur:
    def __init__(self, row):
        self._row = row
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a, **k):
        pass
    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row):
        self._row = row
    def cursor(self):
        return _Cur(self._row)
    def close(self):
        pass


def test_ticked_today_reads_todays_snapshot(monkeypatch):
    import routes.grid_data_master_shell as gds
    monkeypatch.setattr(gds, "_conn", lambda: _Conn((1,)))
    assert gds._ticked_today() is True
    monkeypatch.setattr(gds, "_conn", lambda: _Conn(None))
    assert gds._ticked_today() is False
    monkeypatch.setattr(gds, "_conn", lambda: None)  # db down → tick proceeds
    assert gds._ticked_today() is False


def test_master_tick_wires_the_dedup_guard():
    src = (ROOT / "routes" / "grid_data_master_shell.py").read_text()
    tree = ast.parse(src)
    assert tree.body, "empty parse — guard would be vacuous"
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "master_tick")
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "_ticked_today" in called, (
        "master_tick no longer consults _ticked_today — the every-5-min "
        "heartbeat would fire the gridstatus ingest 11-22x/day again"
    )
