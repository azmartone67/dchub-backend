"""repair_fiber_routes_hifld_transmission.py, driven against a fake connection.

The script's docstring carries the production measurement (9,695 HIFLD power
transmission lines in fiber_routes). These tests pin what an operator relies on
when running it: the SQL selection and the Python re-check pick the same rows;
a dry run writes nothing; --apply saves every row before it deletes anything,
refuses a selection it cannot vouch for, and rolls back on a count mismatch;
and --rollback puts the rows back.
"""
import importlib.util
import json
import os
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "repair_fiber_routes_hifld_transmission",
    os.path.join(ROOT, "repair_fiber_routes_hifld_transmission.py"))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

COLS = ("id", "name", "provider", "route_type", "source", "source_id",
        "start_location", "coordinates")


def _row(id_, **kw):
    row = {"id": id_,
           "name": f"ONCOR ELECTRIC DELIVERY CO. 138kV Line - Dallas-Fort Worth [{id_:012x}]",
           "provider": "ONCOR ELECTRIC DELIVERY CO.", "route_type": "transmission",
           "source": "hifld", "source_id": f"hifld_tl_{id_}_faf7628e458fc4bb",
           "start_location": "Dallas-Fort Worth", "coordinates": None}
    row.update(kw)
    return row


# The removable population beside its nearest non-members.
FIXTURE = [
    _row(1), _row(2),
    _row(3, route_type="metro"),                              # hifld, not transmission
    _row(4, source="osm", source_id="osm_fiber_4"),           # transmission, another source
    _row(5, source_id="peeringdb_ix_5"),                      # another id family
    _row(6, route_type="Transmission"),                       # not the stored value
    _row(7, source_id="xhifld_tl_7"),                         # prefix not at the start
    _row(8, source="osm"),                                    # hifld_tl_ id, another source
]
REMOVABLE_IDS = {1, 2}


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rowcount, self.description, self._result = conn, -1, None, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append(("execute", " ".join(sql.split()), params))
        head = sql.split(None, 1)[0].upper()
        if head == "SELECT":
            self.description = [(c,) for c in COLS]
            self._result = [tuple(r[c] for c in COLS) for r in self.conn.selected]
        elif head == "DELETE":
            self.conn.log.append(("files_at_delete", sorted(os.listdir(self.conn.rbdir))))
            self.rowcount = (len(params[0]) if self.conn.delete_rowcount is None
                             else self.conn.delete_rowcount)
        elif head == "INSERT":
            self.rowcount = 1

    def fetchall(self):
        return self._result


class FakeConn:
    def __init__(self, selected, rbdir, delete_rowcount=None):
        self.selected, self.rbdir, self.delete_rowcount = selected, rbdir, delete_rowcount
        self.log = []

    def cursor(self):
        return FakeCursor(self)

    def set_session(self, **kw):
        self.log.append(("set_session", kw))

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))

    def close(self):
        pass


def _stmts(conn, verb):
    return [e for e in conn.log if e[0] == "execute" and e[1].upper().startswith(verb)]


def test_sql_selection_and_python_recheck_pick_the_same_rows():
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE fiber_routes ({', '.join(COLS)})")
    db.executemany(f"INSERT INTO fiber_routes VALUES ({', '.join('?' * len(COLS))})",
                   [tuple(r[c] for c in COLS) for r in FIXTURE])
    selected = {i for (i,) in db.execute(
        "SELECT id FROM fiber_routes WHERE " + R.REMOVABLE_SQL)}
    assert selected == REMOVABLE_IDS, f"REMOVABLE_SQL selected {sorted(selected)}"
    rechecked = {r["id"] for r in FIXTURE if R.is_removable(r)}
    assert rechecked == REMOVABLE_IDS, f"is_removable() accepted {sorted(rechecked)}"


def test_dry_run_is_read_only_and_writes_nothing(tmp_path):
    conn = FakeConn([_row(1), _row(2)], str(tmp_path))
    assert R.dry_run(conn) == 0
    executed = [e[1] for e in conn.log if e[0] == "execute"]
    assert executed[:1] == ["SET TRANSACTION READ ONLY"], (
        f"the dry run opened with {executed[:1]}, not a transaction-scoped read-only")
    assert not [e for e in conn.log if e[0] == "set_session"], (
        "session-level setting: on the -pooler DSN it can outlive this connection")
    assert not _stmts(conn, "DELETE") and not _stmts(conn, "INSERT")
    assert ("commit",) not in conn.log
    assert not os.listdir(tmp_path)


def test_apply_saves_every_row_before_it_deletes(tmp_path):
    conn = FakeConn([_row(1), _row(2)], str(tmp_path))
    assert R.apply(conn, str(tmp_path)) == 0
    deletes = _stmts(conn, "DELETE")
    assert len(deletes) == 1 and deletes[0][2][0] == [1, 2]
    files = next(e[1] for e in conn.log if e[0] == "files_at_delete")
    assert len(files) == 1, "the DELETE ran before the rollback file existed"
    with open(tmp_path / files[0]) as fh:
        assert [r["id"] for r in json.load(fh)["rows"]] == [1, 2]
    assert conn.log.index(("commit",)) > conn.log.index(deletes[0])


def test_apply_refuses_a_row_that_is_not_a_hifld_transmission_line(tmp_path):
    conn = FakeConn([_row(1), _row(3, route_type="metro")], str(tmp_path))
    assert R.apply(conn, str(tmp_path)) != 0
    assert not _stmts(conn, "DELETE"), "deleted a selection containing a non-member"
    assert ("commit",) not in conn.log
    assert not os.listdir(tmp_path)


def test_apply_rolls_back_when_the_delete_count_differs(tmp_path):
    conn = FakeConn([_row(1), _row(2)], str(tmp_path), delete_rowcount=1)
    assert R.apply(conn, str(tmp_path)) != 0
    assert ("commit",) not in conn.log and ("rollback",) in conn.log


def test_rollback_reinserts_every_row_in_the_file(tmp_path):
    path = tmp_path / "rollback.json"
    path.write_text(json.dumps({"rows": [_row(1), _row(2)]}))
    conn = FakeConn([], str(tmp_path))
    assert R.rollback(conn, str(path)) == 0
    inserts = _stmts(conn, "INSERT")
    assert [p[0] for _, _, p in inserts] == [1, 2]
    assert ("commit",) in conn.log


@pytest.mark.xfail(strict=True, reason="control: proves this file actually runs")
def test_zzz_must_fail_control():
    assert False, "control"
