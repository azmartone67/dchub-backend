"""Regression tests for the 2026-07-11 energy-intel unstick fixes.

Covers the two stuck-loop defects found in the master shells (pure logic —
no Flask app, no DB, no network; DB/HTTP seams are monkeypatched):

  1. grid_data_master_shell.tier3_act: the freshness lever used to fire an
     unconditional iso/all/extract even with every core feed healthy, starving
     the gridstatus ingest lane (zero re-ingests 07-03 -> 07-10). It must now
     fall through to grid_ext_metrics upkeep when the cores are serving.
  2. grid_data_master_shell.tier1_measure: breadth counted the Depth shell's
     foreign dataset_ids (66/20 artifact) — registry rows only now.
  3. depth_master_shell._act_large_load: the ISO's own PUBLISHED large-load
     figure (ERCOT ~225 GW) must win over the 1-project name-match inference
     (0.13 GW), and act() must rotate lanes when no lane has a gap.
"""
import datetime as _dt

import routes.grid_data_master_shell as gds
import routes.depth_master_shell as dms


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def _tapped_all(stale_hours=100):
    """Every registry dataset tapped, last ingested `stale_hours` ago."""
    ts = _now() - _dt.timedelta(hours=stale_hours)
    return {t["id"]: {"iso": t["iso"], "category": t["cat"],
                      "as_of": ts, "ingested_at": ts}
            for t in gds.TARGET_DATASETS}


# ── 1. freshness lever: heal only EMPTY cores, else fall through to ingest ──

def test_freshness_falls_through_to_ingest_when_cores_healthy(monkeypatch):
    ingested = _tapped_all()
    calls = []
    monkeypatch.setattr(gds, "_ingest_gridstatus_dataset",
                        lambda e: calls.append(e["id"]) or {"ok": True, "dataset": e["id"]})
    monkeypatch.setattr(gds, "_fire",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not self-heal")))
    m = {"core": {"iso_zone_count": 90, "lmp_locations": 6, "queue_isos": 10},
         "_ingested": ingested, "forecast_isos_tapped": sorted(gds._LOAD_FC_ISOS)}
    out = gds.tier3_act(m, {"weakest": "freshness", "scores": {}})
    assert out["action"] == "ingest_gridstatus"
    assert out["mode"] == "maintain_freshness"
    assert calls, "expected a gridstatus re-ingest"


def test_freshness_still_self_heals_empty_core_feed(monkeypatch):
    fired = []
    monkeypatch.setattr(gds, "_fire", lambda p, **k: fired.append(p) or {"dispatched": True})
    monkeypatch.setattr(gds, "_ingest_gridstatus_dataset",
                        lambda e: (_ for _ in ()).throw(AssertionError("must not ingest")))
    m = {"core": {"iso_zone_count": 90, "lmp_locations": 0, "queue_isos": 10},
         "_ingested": _tapped_all()}
    out = gds.tier3_act(m, {"weakest": "freshness", "scores": {}})
    assert out["action"] == "self_heal_refresh"
    assert out["target"] == "iso-lmp/ingest"
    assert fired == ["/api/v1/iso-lmp/ingest"]


def test_failing_absorb_falls_back_to_stalest(monkeypatch):
    ingested = _tapped_all()
    # leave one registry id untapped and make its ingest fail
    untapped = gds.TARGET_DATASETS[0]["id"]
    del ingested[untapped]
    attempts = []

    def _fake_ingest(entry):
        attempts.append(entry["id"])
        if entry["id"] == untapped:
            return {"ok": False, "dataset": entry["id"], "error": "http_404"}
        return {"ok": True, "dataset": entry["id"]}

    # ★ 2026-09-03 — BOTH ARMS OF THE DISPATCHER MUST BE STUBBED.
    #   tier3_act now calls _ingest_dataset, which routes a repointed dataset
    #   to its free direct source instead of gridstatus. Stubbing only the
    #   gridstatus arm let the FALLBACK leg escape the stub — it recorded no
    #   attempt and, worse, would have made a live HTTP call from a unit test
    #   whenever the stalest dataset happened to be a repointed one.
    monkeypatch.setattr(gds, "_ingest_gridstatus_dataset", _fake_ingest)
    monkeypatch.setattr(gds, "_ingest_direct", _fake_ingest)
    m = {"core": {"iso_zone_count": 1, "lmp_locations": 1, "queue_isos": 1},
         "_ingested": ingested}
    out = gds.tier3_act(m, {"weakest": "breadth", "scores": {}})
    assert out["mode"] == "maintain_freshness_fallback"
    assert out["failed_absorb"]["dataset"] == untapped
    assert len(attempts) == 2 and attempts[0] == untapped


def test_no_ingest_path_can_escape_a_stubbed_dispatcher(monkeypatch):
    """★ The guard for the trap above: _ingest_dataset has exactly two arms,
    and a test that stubs one and not the other reaches the network."""
    import inspect
    src = inspect.getsource(gds._ingest_dataset)
    assert "_ingest_direct(entry)" in src and "_ingest_gridstatus_dataset(entry)" in src

    called = []
    monkeypatch.setattr(gds, "_ingest_direct", lambda e: called.append("direct") or {"ok": True})
    monkeypatch.setattr(gds, "_ingest_gridstatus_dataset",
                        lambda e: called.append("gridstatus") or {"ok": True})
    for t in gds.TARGET_DATASETS:
        gds._ingest_dataset(t)
    assert len(called) == len(gds.TARGET_DATASETS), "a dataset reached neither arm"
    assert called.count("direct") == len(
        [t for t in gds.TARGET_DATASETS if t["id"] in gds._DIRECT_SOURCES])


def test_stalest_tapped_ranks_by_ingested_at():
    ingested = _tapped_all(stale_hours=1)
    stale_id = gds.TARGET_DATASETS[3]["id"]
    ingested[stale_id]["ingested_at"] = _now() - _dt.timedelta(days=9)
    # old as_of alone must NOT win (auction/capacity rows carry ancient as_of)
    decoy = gds.TARGET_DATASETS[5]["id"]
    ingested[decoy]["as_of"] = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
    assert gds._stalest_tapped(ingested)["id"] == stale_id


# ── 2. breadth counts registry rows only ─────────────────────────────────

def test_tier1_breadth_ignores_foreign_dataset_ids(monkeypatch):
    state = _tapped_all()
    state["hosting_capacity:cheyenne"] = {"iso": "cheyenne", "category": "hosting_capacity",
                                          "as_of": _now(), "ingested_at": _now()}
    state["dc_load_queue:ERCOT"] = {"iso": "ERCOT", "category": "dc_load_queue",
                                    "as_of": _now(), "ingested_at": _now()}
    monkeypatch.setattr(gds, "_ingested_state", lambda: state)
    monkeypatch.setattr(gds, "_req", lambda *a, **k: {"ok": True, "data": {}})
    m = gds.tier1_measure()
    assert m["breadth_tapped"] == len(gds.TARGET_DATASETS)
    assert m["breadth_target"] == len(gds.TARGET_DATASETS)
    assert "dc_load_queue:ERCOT" not in m["ingested_ids"]
    # everything stale (100h) -> ext freshness ratio must flag it
    assert m["core"]["ext_fresh_ratio_48h"] == 0.0


# ── 3. depth shell: published wins, lanes rotate when full ────────────────

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass

    def rollback(self):
        pass


def test_published_figure_wins_over_inference(monkeypatch):
    cur = _FakeCursor(rows=[("ERCOT", 0.13, 1), ("NYISO", 13.9, 53)])
    monkeypatch.setattr(dms, "_conn", lambda: _FakeConn(cur))
    monkeypatch.setattr(dms, "_published_dc_load", lambda: {
        "ERCOT": {"gw": 225.0, "as_of": "2026-07-10", "source_url": "u", "source_name": "s"}})
    out = dms._act_large_load()
    assert out["ok"] is True
    assert out["by_iso"]["ERCOT"] == 225.0          # published, not 0.13
    assert out["by_iso"]["NYISO"] == 13.9           # inference kept where no published figure
    inserts = [(sql, p) for sql, p in cur.executed if sql.startswith("INSERT")]
    # shell#35 (2026-07-26) deliberately added an INDEPENDENT
    # dc_load_queue_measured series (source dchub_classified) for EVERY core
    # ISO alongside these rows, so a bare INSERT count no longer isolates the
    # behaviour under test. This test guards PUBLISHED-WINS-OVER-INFERRED, so
    # scope the count to the verdict category it actually asserts on.
    verdict_inserts = [(sql, p) for sql, p in inserts
                       if "dc_load_queue_measured" not in sql]
    assert len(verdict_inserts) == 2, [s[:60] for s, _ in verdict_inserts]
    inferred_isos = [p[1] for sql, p in verdict_inserts if "'inferred'" in sql]
    published_isos = [p[1] for sql, p in verdict_inserts if "'published_queue'" in sql]
    assert inferred_isos == ["NYISO"] and published_isos == ["ERCOT"]


def test_act_rotates_lane_when_no_gap(monkeypatch):
    sentinels = {}
    for name, fn in [("capacity_price", "_act_capacity_price"),
                     ("large_load_queue", "_act_large_load"),
                     ("hosting_capacity", "_act_hosting_capacity")]:
        monkeypatch.setattr(dms, fn, (lambda n: lambda: sentinels.setdefault("ran", n) or {"ok": True})(name))
    scores = {"capacity_price": 1.0, "large_load_queue": 1.0,
              "hosting_capacity": 1.0, "fiber_longhaul": 1.0}
    out = dms.act({}, {"weakest": "capacity_price", "scores": dict(scores)})
    lanes = list(scores)
    expected = lanes[_now().timetuple().tm_yday % len(lanes)]
    assert out["lever"] == expected


def test_act_keeps_weakest_when_gap_exists(monkeypatch):
    monkeypatch.setattr(dms, "_act_large_load", lambda: {"ok": True, "by_iso": {}})
    scores = {"capacity_price": 1.0, "large_load_queue": 0.4,
              "hosting_capacity": 1.0, "fiber_longhaul": 1.0}
    out = dms.act({}, {"weakest": "large_load_queue", "scores": scores})
    assert out["lever"] == "large_load_queue"
    assert out["action"] == "classify_dc_load"
