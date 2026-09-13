"""The autonomous brain's extraction_intelligence rows say what was measured.

AutonomousBrain._record_cycle_to_extraction_intelligence writes one row per
brain step, and /api/v1/extractor-brain/insights sums rows_inserted per source
and scores health from outcome. rows_inserted must be a committed-insert count
or NULL, never a regex match counter, an UPDATE count, or a tally that may
include rows a rollback discarded.

The recorder tests capture the INSERT parameters it actually sends:
psycopg2.connect is patched on the module the recorder's own `import psycopg2`
resolves, over a fake connection with only the driver members it uses. The
writer tests run the real capacity and deals steps through the real
db_utils.PGConnectionWrapper over the same fake driver.
"""
import contextlib
import datetime
import importlib
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

STEPS = {
    "capacity": "extract_capacity_from_news",
    "deals": "extract_deals_from_news",
    "quality": "auto_fix_quality_issues",
    "infrastructure": "extract_infrastructure_from_news",
    "gas_infrastructure": "extract_gas_infrastructure_from_news",
    "transmission_infrastructure": "extract_transmission_infrastructure_from_news",
    "fiber_infrastructure": "extract_fiber_infrastructure_from_news",
}

NO_DB = "no database in this test"


def _no_db(*_a, **_k):
    raise RuntimeError(NO_DB)


class _FakeCursor:
    """A psycopg2 cursor reduced to what the recorder, the two writers and
    db_utils.PGCursorWrapper touch. A statement answers rows only when the
    connection's respond() gives it a result set; fetching after one that has
    none raises, as the driver does."""

    def __init__(self, conn, cursor_factory=None):
        self.connection = conn
        self.description = None
        self.rowcount = -1
        self._rows = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.connection.statements.append((sql, params))
        rows = self.connection.respond(sql, params)
        self._rows = None if rows is None else list(rows)
        self.description = None if rows is None else [("?column?",)]
        self.rowcount = -1 if rows is None else len(self._rows)

    def fetchone(self):
        if self._rows is None:
            raise importlib.import_module("psycopg2").ProgrammingError("no results to fetch")
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        if self._rows is None:
            raise importlib.import_module("psycopg2").ProgrammingError("no results to fetch")
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, respond=None):
        self.respond = respond or (lambda sql, params: None)
        self.statements = []
        self.connects = []
        self.commits = 0

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self, cursor_factory)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def test_the_fakes_carry_only_members_the_real_driver_has():
    ext = importlib.import_module("psycopg2.extensions")
    for fake, real in ((_FakeConnection, ext.connection), (_FakeCursor, ext.cursor)):
        methods = [n for n, v in vars(fake).items() if callable(v) and n != "__init__"]
        missing = [n for n in methods if not hasattr(real, n)]
        assert not missing, f"{fake.__name__} fakes {missing}, which {real.__name__} lacks"
    for name in ("connection", "description", "rowcount"):
        assert hasattr(ext.cursor, name), name


@pytest.fixture
def brain_mod(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if "autonomous_brain" not in sys.modules:
        # Importing builds a module-level AutonomousBrain whose state load
        # retries a missing database. Fail that fast, then give the module its
        # real get_db back.
        import db_utils
        real_get_db = db_utils.get_db
        monkeypatch.setattr(db_utils, "get_db", _no_db)
        importlib.import_module("autonomous_brain").get_db = real_get_db
    mod = sys.modules["autonomous_brain"]
    assert pathlib.Path(mod.__file__).resolve() == REPO / "autonomous_brain.py", mod
    return mod


@pytest.fixture
def brain(brain_mod, monkeypatch):
    monkeypatch.setattr(brain_mod, "get_db", _no_db)
    b = brain_mod.AutonomousBrain()
    monkeypatch.setattr(b, "_save_state", lambda *a, **k: None)
    return b


@pytest.fixture
def db(brain_mod, monkeypatch):
    """The connection the recorder opens, and what it sent."""
    conn = _FakeConnection()

    def connect(dsn, **kwargs):
        conn.connects.append(dsn)
        return conn

    monkeypatch.setattr(importlib.import_module("psycopg2"), "connect", connect)
    monkeypatch.setenv("DATABASE_URL", "postgresql://recorder-test.invalid/db")
    monkeypatch.setattr(brain_mod.AutonomousBrain, "_outcome_widened", False, raising=False)
    return conn


_INSERT = re.compile(r"INSERT\s+INTO\s+extraction_intelligence\s*\(([^)]*)\)\s*VALUES", re.I)


def _rows_sent(conn):
    assert conn.connects == ["postgresql://recorder-test.invalid/db"], (
        "the recorder did not open the patched connection exactly once")
    rows = {}
    for sql, params in conn.statements:
        m = _INSERT.search(sql)
        if not m:
            continue
        row = dict(zip([c.strip() for c in m.group(1).split(",")], params, strict=True))
        row["observations"] = json.loads(row["observations"])
        assert row["source_id"] not in rows, f"two rows for {row['source_id']}"
        rows[row["source_id"]] = row
    assert conn.commits >= 1
    return rows


@pytest.fixture
def record(brain, db):
    def run(results):
        brain._record_cycle_to_extraction_intelligence(results, 5.0)
        return _rows_sent(db)
    return run


def _shape(brain, step, **fields):
    """The dict `step` really returns (its own except path, with no database),
    carrying the counts a run would leave in it."""
    out = getattr(brain, STEPS[step])()
    assert out.pop("error") == NO_DB, (step, out)
    assert set(fields) <= set(out) | {"error", "insert_errors"}, (step, fields, out)
    out.update(fields)
    return out


def _cycle(brain, **steps):
    results = {"cycle_id": 7, **{step: _shape(brain, step) for step in STEPS}}
    results.update(steps)
    return results


def test_steps_that_insert_nothing_get_no_row(brain, record):
    rows = record(_cycle(
        brain,
        infrastructure=_shape(brain, "infrastructure", fiber_mentions=9, power_mentions=4),
        fiber_infrastructure=_shape(brain, "fiber_infrastructure", dark_fiber=2, carriers=1),
    ))
    assert sorted(rows) == [
        "autonomous-brain-capacity", "autonomous-brain-deals", "autonomous-brain-gas",
        "autonomous-brain-quality", "autonomous-brain-transmission",
    ]


def test_a_clean_step_reports_the_rows_it_inserted(brain, record):
    rows = record(_cycle(
        brain,
        capacity=_shape(brain, "capacity", new_pipeline=2),
        deals=_shape(brain, "deals", deals_found=1),
        transmission_infrastructure=_shape(brain, "transmission_infrastructure", added=3),
    ))
    got = {s: (r["outcome"], r["rows_inserted"], r["error"], r["observations"])
           for s, r in rows.items() if s != "autonomous-brain-quality"}
    assert got == {
        "autonomous-brain-capacity": ("success", 2, None, {"cycle": 7}),
        "autonomous-brain-deals": ("success", 1, None, {"cycle": 7}),
        "autonomous-brain-gas": ("idle", 0, None, {"cycle": 7}),
        "autonomous-brain-transmission": ("success", 3, None, {"cycle": 7}),
    }


@pytest.mark.parametrize("fixed,outcome", [(5, "success"), (0, "idle")])
def test_quality_updates_are_not_reported_as_inserts(brain, record, fixed, outcome):
    row = record(_cycle(brain, quality=_shape(brain, "quality", fixed=fixed)))[
        "autonomous-brain-quality"]
    assert (row["outcome"], row["rows_inserted"], row["observations"]) == (
        outcome, None, {"cycle": 7, "rows_updated": fixed})


def test_a_step_that_errored_publishes_no_row_count(brain, record):
    row = record(_cycle(
        brain, capacity=_shape(brain, "capacity", new_pipeline=3, error="connection reset"),
    ))["autonomous-brain-capacity"]
    assert (row["outcome"], row["rows_inserted"], row["error"]) == (
        "error", None, "connection reset")


def test_rejected_inserts_are_failure_or_partial_with_no_row_count(brain, record):
    rows = record(_cycle(
        brain,
        gas_infrastructure=_shape(brain, "gas_infrastructure", added=0, insert_errors=2),
        transmission_infrastructure=_shape(
            brain, "transmission_infrastructure", added=4, insert_errors=1),
    ))
    got = {s: (rows[s]["outcome"], rows[s]["rows_inserted"], rows[s]["observations"])
           for s in ("autonomous-brain-gas", "autonomous-brain-transmission")}
    assert got == {
        "autonomous-brain-gas": ("failure", None, {"cycle": 7, "insert_errors": 2}),
        "autonomous-brain-transmission": ("partial", None, {"cycle": 7, "insert_errors": 1}),
    }


def test_a_step_that_raised_is_an_error_not_idle(brain, db, monkeypatch):
    brain.state["total_cycles"] = 1  # not a 10th cycle: no infra sync, no API discovery
    monkeypatch.setattr(brain, "_has_new_announcements", lambda: (True, 42))
    monkeypatch.setattr(brain, "_save_last_processed_announcement", lambda *a, **k: None)
    monkeypatch.setattr(importlib.import_module("dchub_heartbeat"), "heartbeat",
                        lambda *a, **k: True)

    def raises():
        raise RuntimeError("capacity step raised")

    for step, method in STEPS.items():
        shape = _shape(brain, step)
        monkeypatch.setattr(brain, method, raises if step == "capacity" else (lambda s=shape: dict(s)))
    for method in ("extract_substation_infrastructure_from_news", "extract_power_plants_from_news"):
        monkeypatch.setattr(brain, method, lambda: {})

    brain.run_autonomous_cycle()
    rows = _rows_sent(db)
    assert (rows["autonomous-brain-capacity"]["outcome"],
            rows["autonomous-brain-capacity"]["rows_inserted"]) == ("error", None)
    assert rows["autonomous-brain-capacity"]["error"]
    assert (rows["autonomous-brain-deals"]["outcome"],
            rows["autonomous-brain-deals"]["rows_inserted"]) == ("idle", 0)  # control


CAPACITY_ARTICLE = {"id": 1, "title": "Acme Cloud adds 300 MW", "content": "",
                    "source": "test", "source_url": "https://example.test/capacity",
                    "published_date": None}
DEAL_ARTICLE = {"id": 2, "title": "Blackstone to acquire QTS in $10 billion deal",
                "content": "", "source": "test", "source_url": "https://example.test/deal",
                "published_date": None}


@pytest.mark.parametrize("insert_returns_row,counted", [(True, 1), (False, 0)],
                         ids=["inserted", "conflict"])
@pytest.mark.parametrize("method,article,tally", [
    ("extract_capacity_from_news", CAPACITY_ARTICLE, "new_pipeline"),
    ("extract_deals_from_news", DEAL_ARTICLE, "deals_found"),
], ids=["capacity", "deals"])
def test_writers_count_only_rows_their_insert_returned(
        brain, brain_mod, monkeypatch, method, article, tally, insert_returns_row, counted):
    def respond(sql, params):
        flat = " ".join(sql.split()).upper()
        if "FROM ANNOUNCEMENTS" in flat:
            return [dict(article)]
        if flat.startswith("SELECT ID FROM"):
            return []  # no earlier row for this article
        if flat.startswith("SELECT LASTVAL()"):
            return [(1,)]
        if flat.startswith("INSERT INTO") and "RETURNING" in flat:
            return [{"?column?": 1}] if insert_returns_row else []
        return None

    conn = _FakeConnection(respond)
    db_utils = importlib.import_module("db_utils")
    monkeypatch.setattr(brain_mod, "get_db", lambda: db_utils.PGConnectionWrapper(conn))

    out = getattr(brain, method)()
    inserts = [s for s, _ in conn.statements if s.lstrip().upper().startswith("INSERT INTO")]
    assert len(inserts) == 1, "the article never reached the INSERT"
    assert (out.get("error"), out[tally], conn.commits) == (None, counted, 1)


def test_dropped_brain_feeds_are_not_reported_failing(monkeypatch):
    eb = importlib.import_module("routes.extractor_brain")
    assert pathlib.Path(eb.__file__).resolve() == REPO / "routes" / "extractor_brain.py", eb
    last = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    silent = [("autonomous-brain-infra-news", last, 300.0),
              ("autonomous-brain-fiber", last, 300.0),
              ("some-live-feed", last, 300.0)]  # control: a feed that really went quiet
    conn = _FakeConnection(
        lambda sql, params: list(silent) if "HAVING MAX(observed_at)" in sql else [])

    @contextlib.contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(eb, "_ensure_tables", lambda: None)
    monkeypatch.setattr(eb, "_conn", fake_conn)
    out = eb._compute_source_quality()
    assert sorted(out) == ["some-live-feed"]
    assert out["some-live-feed"]["health"] == "failing"
