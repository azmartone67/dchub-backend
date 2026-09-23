"""Guards for the What's New energy layers counted from the first-seen registry.

THE DEFECT THIS PREVENTS (measured 2026-09-22)
  interconnect_queue (5,559 rows), planned_generators (2,341) and
  generator_inventory (27,700) were not on /whats-new. All three loaders
  restamp every row they write, so their timestamps cannot say what is new.
  A COUNT(*) delta cannot either: on a delete-and-reinsert table it is NET
  churn, and on a table rebuilt from nothing it is +everything. The board
  counts `added` for these layers from util/first_seen.py, and every key
  present when the registry started is a baseline that is never counted.

WHAT IS PINNED HERE (no DB; tests/test_energy_first_seen_sql.py runs the real
SQL against Postgres in the db-parity job)
  1. plan(): a first run is all baseline, a mass of unknown keys is treated as
     a re-key and not as news, ordinary new keys are counted.
  2. _summary(): a _FIRST_SEEN layer publishes the registry's count and NEVER
     the snapshot difference, including when the registry is unreadable.
  3. _first_seen_status(): no "row count did not move" claim, and an
     unreadable registry reads "measuring", never zero.
  4. Every registry layer the board reads is one some ingest route writes, so
     a renamed literal cannot leave a layer "measuring" for ever.

Nothing runs at module scope.
"""
import ast
import datetime
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _fs():
    from util import first_seen
    return first_seen


def _growth():
    pytest.importorskip("flask")
    pytest.importorskip("psycopg2")
    from routes import infra_growth
    return infra_growth


# ── 1. plan(): baseline is never news ──────────────────────────────────────

def test_first_run_for_a_layer_is_all_baseline_including_the_table_seed():
    fs = _fs()
    baseline, reason, keys = fs.plan(set(), False, ["A", "B"], seed_keys=["B", "C"])
    assert baseline is True and reason == fs.SEED
    assert keys == ["A", "B", "C"], (
        "a first run must baseline the table's existing keys too, or a project "
        "that dropped out of the feed returns later and is counted as new")


def test_first_run_for_a_new_scope_under_an_existing_layer_is_baseline():
    fs = _fs()
    baseline, reason, keys = fs.plan(set(), True, ["X1", "X2"], seed_keys=["X3"])
    assert (baseline, reason, keys) == (True, fs.FIRST_SCOPE, ["X1", "X2", "X3"])


def test_ordinary_new_keys_are_counted_and_known_keys_are_not():
    fs = _fs()
    known = {f"K{i}" for i in range(1000)}
    baseline, reason, keys = fs.plan(known, True, list(known) + ["N1", "N2"],
                                     seed_keys=["IGNORED"])
    assert (baseline, reason, keys) == (False, fs.NEW, ["N1", "N2"]), (
        "seed keys are for a first run only; on a normal run they must not be "
        "inserted, and only keys the registry lacks are new")


def test_a_mass_of_unknown_keys_is_a_rekey_not_news():
    fs = _fs()
    known = {f"OLD-{i}" for i in range(100)}
    incoming = [f"NEW-FORMAT-{i}" for i in range(100)]
    baseline, reason, keys = fs.plan(known, True, incoming)
    assert baseline is True and reason == fs.REKEY and len(keys) == 100


def test_a_real_burst_below_the_rekey_bar_is_still_counted():
    fs = _fs()
    known = {f"K{i}" for i in range(1900)}          # ERCOT-sized scope
    incoming = list(known) + [f"N{i}" for i in range(120)]
    baseline, reason, keys = fs.plan(known, True, incoming)
    assert baseline is False and reason == fs.NEW and len(keys) == 120


def test_a_small_scope_below_rekey_min_is_counted_not_suppressed():
    fs = _fs()
    known = {f"K{i}" for i in range(20)}
    incoming = [f"N{i}" for i in range(fs.REKEY_MIN - 1)]
    baseline, _reason, keys = fs.plan(known, True, incoming)
    assert baseline is False and len(keys) == fs.REKEY_MIN - 1


def test_empty_and_blank_keys_are_dropped():
    fs = _fs()
    _b, _r, keys = fs.plan({"K"}, True, ["", None, "  ", "K", "N"])
    assert keys == ["N"]


# ── 2. _summary publishes the registry, never the snapshot difference ──────

TODAY = datetime.date(2026, 9, 22)


class _Cur:
    """Answers each query shape _summary issues, for ONE layer. Dispatches on
    the SQL, not call order, so a new query cannot shift the answers."""

    def __init__(self, label, history, registry_exists, registry_row):
        self.label = label
        self.history = history
        self.registry_exists = registry_exists
        self.registry_row = registry_row
        self.connection = self
        self._one = None
        self._all = []
        self.sql = []

    def rollback(self):
        pass

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s)
        self._all = []
        if "MAX(snapshot_date)" in s:
            self._one = [TODAY]
        elif "FROM infra_growth_snapshot" in s:
            self._all = self.history if (params or [None])[0] == self.label else []
        elif "information_schema" in s:
            self._one, self._all = None, []
        elif "to_regclass" in s:
            self._one = ["energy_first_seen" if self.registry_exists else None]
        elif "FROM energy_first_seen" in s:
            self._one = self.registry_row
        elif s.startswith("SELECT MAX("):
            self._one = [datetime.datetime(2026, 9, 22, 5, 57,
                                           tzinfo=datetime.timezone.utc), 0]
        else:
            self._one = [None]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


def _hist(counts):
    """counts newest-first, one per day ending TODAY."""
    cap = datetime.datetime(2026, 9, 22, 5, 38, tzinfo=datetime.timezone.utc)
    return [(TODAY - datetime.timedelta(days=i), c, cap) for i, c in enumerate(counts)]


def _run(label, history, registry_exists=True, registry_row=None):
    G = _growth()
    cur = _Cur(label, history, registry_exists, registry_row)
    rows, _flat = G._summary(cur)
    match = [r for r in rows if r["layer"] == label]
    assert match, f"_summary returned no record for {label}"
    return match[0], cur


def _reg_row(added_7d, added_1d, baseline, age_days):
    since = datetime.datetime(2026, 9, 22, tzinfo=datetime.timezone.utc) \
        - datetime.timedelta(days=age_days)
    return [added_7d, added_1d, baseline, since, float(age_days)]


@pytest.mark.parametrize("label", ["interconnection_requests", "planned_generators",
                                   "generator_inventory"])
def test_a_full_reload_jump_is_not_published_as_new(label):
    """The snapshot says +5,559 in 7d (a table rebuilt from nothing). The
    registry says nothing new. The board must publish 0, not 5,559."""
    rec, _ = _run(label, _hist([5559] * 3 + [0] * 5),
                  registry_row=_reg_row(0, 0, 5300, 30))
    assert rec["delta_window"] == 0 and rec["delta_1d"] == 0 and rec["delta_7d"] == 0, rec
    assert rec["growth_basis"] == "first_seen_registry"
    assert rec["status"] != "growing", rec["status_reason"]
    assert "5,559" not in (rec["status_reason"] or ""), rec["status_reason"]


def test_registry_count_is_what_the_board_publishes():
    rec, _ = _run("interconnection_requests", _hist([5561] * 8),
                  registry_row=_reg_row(7, 2, 5559, 30))
    assert (rec["delta_window"], rec["window_days"], rec["delta_1d"], rec["delta_7d"]) \
        == (7, 7, 2, 7)
    assert rec["status"] == "growing"
    assert "+7 interconnection requests first seen in the last 7d" in rec["status_reason"]


def test_a_young_registry_never_claims_a_seven_day_window():
    rec, _ = _run("interconnection_requests", _hist([5561] * 8),
                  registry_row=_reg_row(3, 3, 5559, 1.5))
    assert rec["window_days"] == 2, "a 1.5-day-old registry cannot speak for 7 days"
    assert rec["delta_7d"] is None, "delta_7d is a 7-day claim; the registry is younger"


@pytest.mark.parametrize("exists,row", [(False, None), (True, [0, 0, 0, None, None])])
def test_an_unreadable_registry_is_measuring_never_the_count_delta(exists, row):
    """No registry (or no rows for the layer) must NOT fall back to the
    snapshot difference: that is net churn on exactly these tables."""
    rec, _ = _run("planned_generators", _hist([2400] + [2341] * 7),
                  registry_exists=exists, registry_row=row)
    assert rec["delta_window"] is None and rec["delta_1d"] is None, rec
    assert rec["status"] == "measuring", rec["status_reason"]
    assert "not measured" in rec["status_reason"]


def test_a_non_registry_layer_still_uses_the_snapshot_difference():
    rec, cur = _run("substations", _hist([110] + [100] * 7))
    assert rec["delta_window"] == 10 and rec["growth_basis"] == "count_snapshot"
    assert not any("energy_first_seen" in s for s in cur.sql), (
        "a count-snapshot layer must not read the registry")


# ── 3. status reasons ──────────────────────────────────────────────────────

def test_no_new_reason_does_not_claim_the_row_count_held_still():
    G = _growth()
    fs = {"added_window": 0, "window_days": 7, "added_1d": 0, "baseline": 2341,
          "since": "2026-09-22T00:00:00+00:00"}
    status, reason = G._first_seen_status(fs, "planned generating units",
                                          "EIA (plant id, generator id)", 1, 45, "monthly")
    assert status == "refreshed"
    assert "did not move" not in reason, (
        "on a delete-and-reinsert table the count can move with nothing new in "
        "it; the registry measured 'no new key', not 'flat count'")
    assert "no planned generating units first seen in the last 7d" in reason


def test_quiet_and_overdue_keep_their_classification():
    G = _growth()
    fs = {"added_window": 0, "window_days": 7, "added_1d": 0, "baseline": 10,
          "since": "2026-09-01T00:00:00+00:00"}
    assert G._first_seen_status(fs, "u", "k", 21, 45, "monthly")[0] == "on_cadence"
    assert G._first_seen_status(fs, "u", "k", 60, 45, "monthly")[0] == "overdue"


# ── 4. the board and the writers agree on the registry layer names ─────────

def _record_layers_written():
    """Registry layer names passed as a string literal to first_seen.record()
    (or the queue helper) anywhere under routes/."""
    found = set()
    routes = os.path.join(ROOT, "routes")
    for fn in sorted(os.listdir(routes)):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(routes, fn), encoding="utf-8").read()
        if "record(" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == "record" and len(node.args) >= 2 \
                    and isinstance(node.args[1], ast.Constant):
                found.add(node.args[1].value)
    return found


def test_every_registry_layer_the_board_reads_is_written_by_an_ingest():
    G = _growth()
    written = _record_layers_written()
    assert written, "found no first_seen.record() call at all — the scan is blind"
    for label, (layer, _noun, _key) in G._FIRST_SEEN.items():
        assert layer in written, (
            f"/whats-new reads registry layer {layer!r} for {label}, and no "
            f"ingest route writes it; the layer would read 'measuring' for ever")


def test_both_queue_ingest_modes_record_first_seen_after_the_upsert():
    """/ingest-projects has two write paths: self-fetch on Railway, and rows
    POSTed back by the GH runner when an ISO blocks Railway egress. A path
    that skips the registry leaves that ISO's new projects uncounted, which
    reads as a quiet queue. The record must come after the upsert, so its
    first-run seed read of the table includes that day's rows."""
    src = open(os.path.join(ROOT, "routes", "iso_queue_ingest.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "ingest_projects")
    upserts, records = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == "upsert":
                upserts.append(node.lineno)
            elif name == "_fs_record_queue":
                records.append(node.lineno)
    assert len(upserts) == 2, f"expected the two upsert paths, found {upserts}"
    assert len(records) == 2, (
        f"_fs_record_queue is called {len(records)}x in ingest_projects; each "
        f"of the two upsert paths needs one")
    for up, rec in zip(sorted(upserts), sorted(records)):
        assert rec > up, f"first-seen record at line {rec} runs before the upsert at {up}"


def test_every_registry_layer_is_declared_on_the_board():
    G = _growth()
    labels = {l[0] for l in G._LAYERS}
    for label in G._FIRST_SEEN:
        assert label in labels, f"{label} has a registry but no _LAYERS entry"
        for d in ("_FRESH_COL", "_EXPECTED_CADENCE", "_FRIENDLY", "_PROVENANCE"):
            assert label in getattr(G, d), f"{label} missing from {d}"
