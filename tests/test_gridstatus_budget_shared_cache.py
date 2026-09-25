"""gridstatus budget: shared cache for PJM-DOM, refusals not counted (2026-09-25).

Measured 2026-09-24/25 on production:
  * gridstatus_call_ledger 2026-09 = 1,543 against a 200 budget — the row was
    bumped on every ATTEMPT, refusals included, so it stopped meaning requests
  * the last 160 [gridstatus] log lines (2 days) were all REFUSED,
    caller=pjm_dataminer, pjm_load + pjm_lmp_real_time_5_min; the budget was
    spent by 2026-09-09 at the latest
  * the PJM-DOM answer was cached 6h in an in-process dict, so every worker on
    every replica bought its own copy
  * no gridstatus dataset has reached grid_ext_metrics since 2026-07-20

Pinned here (no database needed; the SQL itself was checked against a real
Postgres, see the PR):
  1. once refused, the process stops asking the ledger for the rest of the month
  2. a provider 403 does the same
  3. shared_ttl_s: a fresh cache entry answers without a provider request; only
     a successful, non-empty answer is stored; ttl 0 never touches the cache
  4. the PJM-DOM lookup asks for the shared cache on both datasets
"""
import pytest

import gridstatus_client as gsc


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(gsc, "_EXHAUSTED_MONTH", None)
    monkeypatch.setenv("GRIDSTATUS_API_KEY", "k-test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")


class _Cur:
    def __init__(self, row):
        self.row = row
    def execute(self, *a, **k):
        pass
    def fetchone(self):
        return self.row
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, row):
        self.row = row
    def cursor(self):
        return _Cur(self.row)
    def commit(self):
        pass
    def close(self):
        pass


def _fake_pg(monkeypatch, row):
    import psycopg2
    n = {"connects": 0}
    def _connect(*a, **k):
        n["connects"] += 1
        return _Conn(row)
    monkeypatch.setattr(psycopg2, "connect", _connect)
    return n


def test_refusal_is_remembered_for_the_month(monkeypatch):
    n = _fake_pg(monkeypatch, None)            # the conditional UPDATE returned no row
    assert gsc._budget_spend() is False
    assert gsc._budget_spend() is False
    assert n["connects"] == 1, "a spent budget kept asking the ledger"


def test_control_under_budget_keeps_asking(monkeypatch):
    n = _fake_pg(monkeypatch, (5,))
    assert gsc._budget_spend() is True and gsc._budget_spend() is True
    assert n["connects"] == 2 and gsc._EXHAUSTED_MONTH is None


def test_provider_403_marks_the_month_spent(monkeypatch):
    _fake_pg(monkeypatch, (1,))
    class _R:
        status_code = 403
    monkeypatch.setattr(gsc.requests, "get", lambda *a, **k: _R())
    assert gsc.gs_request("/datasets/pjm_load/query") == (None, "http_403")
    assert gsc._EXHAUSTED_MONTH == gsc._month()


def _stub_cache(monkeypatch, hit=None):
    store = {"reads": [], "writes": []}
    def _cache(key, rows=None, ttl_s=0):
        if rows is None:
            store["reads"].append(key)
            return hit
        store["writes"].append((key, rows, ttl_s))
        return None
    monkeypatch.setattr(gsc, "_shared_cache", _cache)
    return store


def test_fresh_shared_entry_answers_without_a_request(monkeypatch):
    store = _stub_cache(monkeypatch, hit=[{"dom": 1.0}])
    monkeypatch.setattr(gsc, "gs_request",
                        lambda *a, **k: pytest.fail("provider was asked despite a fresh entry"))
    assert gsc.gridstatus_get("pjm_load", {"limit": 1}, shared_ttl_s=60) == ([{"dom": 1.0}], None)
    assert store["reads"] == ['pjm_load|{"limit": 1}']


def test_miss_requests_then_stores_success(monkeypatch):
    store = _stub_cache(monkeypatch)
    monkeypatch.setattr(gsc, "gs_request", lambda *a, **k: ({"data": [{"dom": 2.0}]}, None))
    assert gsc.gridstatus_get("pjm_load", {"limit": 1}, shared_ttl_s=60) == ([{"dom": 2.0}], None)
    assert store["writes"] == [('pjm_load|{"limit": 1}', [{"dom": 2.0}], 60)]


@pytest.mark.parametrize("answer", [(None, "budget_exhausted: x"), ({"data": []}, None)])
def test_errors_and_empty_answers_are_not_stored(monkeypatch, answer):
    store = _stub_cache(monkeypatch)
    monkeypatch.setattr(gsc, "gs_request", lambda *a, **k: answer)
    gsc.gridstatus_get("pjm_load", {"limit": 1}, shared_ttl_s=60)
    assert store["writes"] == []


def test_ttl_zero_never_touches_the_cache(monkeypatch):
    store = _stub_cache(monkeypatch, hit=[{"stale": True}])
    monkeypatch.setattr(gsc, "gs_request", lambda *a, **k: ({"data": [{"dom": 3.0}]}, None))
    assert gsc.gridstatus_get("pjm_load", {"limit": 1}) == ([{"dom": 3.0}], None)
    assert store == {"reads": [], "writes": []}


def test_dom_lookup_uses_the_shared_cache_for_both_datasets(monkeypatch):
    import pjm_dataminer as pjm
    seen = {}
    def _fake(dataset, params=None, timeout=15, caller="unknown", shared_ttl_s=0):
        seen[dataset] = shared_ttl_s
        return [{"dom": 9000.0, "lmp": 40.0}], None
    monkeypatch.setattr(gsc, "gridstatus_get", _fake)
    assert pjm._gridstatus_dom({"region": "PJM-DOM"}) is not None
    assert seen == {"pjm_load": pjm._PJM_TTL, "pjm_lmp_real_time_5_min": pjm._PJM_TTL}
    assert pjm._PJM_TTL > 0


def test_dom_ttl_default_fits_the_monthly_budget(monkeypatch):
    """2 datasets, refetched once per TTL, for a 31-day month, must fit the budget."""
    import importlib
    import pjm_dataminer as pjm
    monkeypatch.delenv("PJM_DOM_CACHE_TTL_S", raising=False)
    pjm = importlib.reload(pjm)
    assert pjm._PJM_TTL == 43200
    worst = 2 * (31 * 86400 // pjm._PJM_TTL)
    assert worst <= gsc.MONTHLY_BUDGET, f"{worst} requests/month > budget {gsc.MONTHLY_BUDGET}"
