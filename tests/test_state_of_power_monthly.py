"""Immutable monthly State of Power snapshots.

Audit 2026-09-26: press needs a citable monthly URL; /state-of-power/2026-09
answered 404 and the live page recomputes through the day. A month is stored
once from the live payload and served from storage forever: a GET never
recomputes, a second capture never overwrites, and each snapshot says its
values are as of the capture time, not month-end.

No DB, no network: an in-memory fake stands in for Postgres.
"""
import datetime as dt

import pytest
from flask import Flask

import routes.state_of_power as sop
import routes.state_of_power_monthly as spm
from util import ddl_once


class _Store:
    def __init__(self):
        self.rows = {}          # month -> (captured_at, basis, payload_json)
        self.ddl = 0


class _Cur:
    def __init__(self, store):
        self.s = store
        self.out = None

    def execute(self, sql, params=None):
        q = " ".join(sql.split())
        if q.startswith("CREATE TABLE"):
            self.s.ddl += 1
            self.out = None
        elif q.startswith("INSERT INTO state_of_power_snapshots"):
            month, basis, payload = params
            assert "ON CONFLICT (month) DO NOTHING" in q
            if month in self.s.rows:
                self.out = None
            else:
                cap = dt.datetime(2026, 9, 26, 8, 0, tzinfo=dt.timezone.utc)
                self.s.rows[month] = (cap, basis, payload)
                self.out = [(cap,)]
        elif q.startswith("SELECT captured_at, capture_basis, payload"):
            r = self.s.rows.get(params[0])
            self.out = [r] if r else []
        elif q.startswith("SELECT month, captured_at, capture_basis"):
            self.out = [(m, r[0], r[1]) for m, r in sorted(self.s.rows.items(), reverse=True)]
        else:
            raise AssertionError("unexpected SQL: " + q)

    def fetchone(self):
        return self.out[0] if self.out else None

    def fetchall(self):
        return list(self.out or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, store):
        self.s = store

    def cursor(self):
        return _Cur(self.s)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def env(monkeypatch):
    ddl_once.reset()
    store = _Store()
    monkeypatch.setattr(spm, "_conn", lambda: _Conn(store))
    calls = {"n": 0}
    # A real payload from the live builder, its upstreams stubbed, so the HTML
    # renderer sees every key it reads.
    import routes.energy_report as er
    monkeypatch.setattr(er, "_gather_energy", lambda w: {})
    monkeypatch.setattr(sop, "_fuel_block", lambda: {"fuel_mix": []})
    monkeypatch.setattr(sop, "_canon_mkts", lambda default=300: 331)
    live = sop._gather()

    def gather():
        calls["n"] += 1
        return dict(live, generated_at="call-%d" % calls["n"])
    monkeypatch.setattr(sop, "_gather", gather)
    monkeypatch.setattr(spm, "_this_month", lambda today=None: "2026-09")
    app = Flask(__name__)
    from routes.quarterly_report import quarterly_report_bp
    app.register_blueprint(sop.state_of_power_bp)
    app.register_blueprint(spm.state_of_power_monthly_bp)
    app.register_blueprint(quarterly_report_bp)
    yield app.test_client(), store, calls, app
    ddl_once.reset()


def test_capture_once_then_serve_from_storage_forever(env):
    client, store, calls, _ = env
    status, body = spm.capture("2026-09")
    assert status == 201 and body["capture_basis"] == "mid_month"
    assert calls["n"] == 1

    # A second capture is refused and the stored row is untouched.
    status, body = spm.capture("2026-09")
    assert status == 409 and body["error"] == "already_captured"
    assert '"call-1"' in store.rows["2026-09"][2]

    # Reads never recompute.
    def boom():
        raise AssertionError("a GET recomputed the report")
    sop._gather = boom
    r = client.get("/api/v1/reports/state-of-power/2026-09")
    assert r.status_code == 200
    d = r.get_json()
    assert d["generated_at"] == "call-1"
    s = d["snapshot"]
    assert s["month"] == "2026-09" and s["month_label"] == "September 2026"
    assert s["immutable"] is True and s["capture_basis"] == "mid_month"
    assert s["captured_at"].startswith("2026-09-26T08:00")
    assert "not the last day of the month" in s["as_of_note"]
    assert d["stable_url"] == "https://dchub.cloud/state-of-power/2026-09"
    assert d["citation"]["stable_url"] == d["stable_url"]
    assert "September 2026" in d["citation"]["apa"]
    assert "s-maxage" in r.headers["Cache-Control"]


def test_index_lists_captured_months(env):
    client, _, _, _ = env
    spm.capture("2026-08")
    spm.capture("2026-09")
    d = client.get("/api/v1/reports/state-of-power/months").get_json()
    assert [m["month"] for m in d["months"]] == ["2026-09", "2026-08"]
    assert d["months"][1]["capture_basis"] == "month_close"
    assert d["months"][0]["html"].endswith("/state-of-power/2026-09")


def test_uncaptured_and_future_months(env):
    client, _, calls, _ = env
    r = client.get("/api/v1/reports/state-of-power/2026-07")
    assert r.status_code == 404 and r.get_json()["error"] == "month_not_captured"
    assert calls["n"] == 0, "a 404 must not compute anything"
    assert spm.capture("2026-10")[0] == 400
    assert spm.capture("2026-13")[0] == 400


def test_html_is_the_dated_snapshot(env):
    client, _, _, _ = env
    spm.capture("2026-09")
    html = client.get("/state-of-power/2026-09").get_data(as_text=True)
    assert '<link rel="canonical" href="https://dchub.cloud/state-of-power/2026-09">' in html
    assert "Monthly snapshot — September 2026" in html
    assert "not the last day of the month" in html


def test_dated_path_does_not_break_the_quarterly_or_methodology_routes(env):
    _, _, _, app = env
    urls = app.url_map.bind("dchub.cloud")
    assert urls.match("/state-of-power/2026-09")[0] == "state_of_power_monthly.snapshot_html"
    assert urls.match("/state-of-power/q3-2026")[0] == "quarterly_report.state_of_power_quarter_html"
    assert urls.match("/state-of-power/methodology")[0].startswith("state_of_power.")
    assert urls.match("/api/v1/reports/state-of-power")[0] == "state_of_power.state_of_power_json"


def test_capture_endpoint_is_admin_only(env):
    client, store, _, _ = env
    r = client.post("/api/v1/reports/state-of-power/snapshot?month=2026-09")
    assert r.status_code == 403 and not store.rows
