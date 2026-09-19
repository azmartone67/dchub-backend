"""Cover the decision `tests/_prod_shaped_db.reset_tables` makes per relation.

The 40 setup errors this helper removes only appear against a prod-shaped
database. What is testable here -- and what actually went wrong twice -- is the
CHOICE: drop, truncate, create, or refuse. So the cursor below answers the
catalog probe from a declared relkind map, refuses the drops it is told to
refuse, and records the statements it was handed. It reads the SQL it is given;
a stub that answered the same way whatever it was asked would let the helper
probe the wrong catalog, or skip the probe entirely, and still pass.
"""
from __future__ import annotations

import types

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import errors  # noqa: E402

from tests._prod_shaped_db import reset_tables  # noqa: E402


class _Cur:
    def __init__(self, catalog, refuses=(), autocommit=True):
        self.catalog = catalog
        self.refuses = set(refuses)
        self.statements = []
        self.asked = []
        self._answer = None
        self.connection = types.SimpleNamespace(autocommit=autocommit)

    def execute(self, q, params=None):
        text = q if isinstance(q, str) else str(q)
        if "pg_class" in text and "relkind" in text:
            assert params and len(params) == 1, f"probe unparameterised: {params}"
            self.asked.append(params[0])
            self._answer = self.catalog.get(params[0])
            return
        self.statements.append(text)
        if "DROP" in text and any(f"'{n}'" in text for n in self.refuses):
            raise errors.DependentObjectsStillExist(
                "cannot drop table because other objects depend on it")

    def fetchone(self):
        return None if self._answer is None else (self._answer,)

    def only(self):
        assert len(self.statements) == 1, self.statements
        return self.statements[0]


def test_an_absent_table_is_handed_back_for_the_caller_to_create():
    cur = _Cur({})
    assert reset_tables(cur, "substations") == ("substations",)
    assert cur.statements == []


def test_a_droppable_table_is_dropped_and_handed_back():
    """The empty-postgres path, unchanged. Two files build mcp_calls_identity
    with different columns and the DROP is what reconciles them -- truncating
    here instead made the second file inherit the first's table (8 errors)."""
    cur = _Cur({"mcp_calls_identity": "r"})
    assert reset_tables(cur, "mcp_calls_identity") == ("mcp_calls_identity",)
    assert "DROP" in cur.only()
    assert "TRUNCATE" not in cur.only()


def test_a_table_whose_drop_is_refused_is_truncated_and_kept():
    """Production's case: a view depends on the table, so it survives and the
    caller's CREATE ... IF NOT EXISTS no-ops onto production's real shape."""
    cur = _Cur({"transmission_lines": "r"}, refuses=["transmission_lines"])
    assert reset_tables(cur, "transmission_lines") == ()
    assert len(cur.statements) == 2, cur.statements
    assert "TRUNCATE" in cur.statements[1]
    assert "transmission_lines" in cur.statements[1]


def test_the_fallback_never_cascades_a_drop():
    """A DROP ... CASCADE would delete production's views out of a branch all
    19 lanes share. Only the TRUNCATE may cascade -- rows, not objects."""
    cur = _Cur({"transmission_lines": "r"}, refuses=["transmission_lines"])
    reset_tables(cur, "transmission_lines")
    drop, truncate = cur.statements
    assert "CASCADE" not in drop
    assert "DROP" not in truncate.upper()
    assert "CASCADE" in truncate


def test_a_partitioned_table_is_handled_like_a_table():
    cur = _Cur({"mcp_upgrade_signals": "p"}, refuses=["mcp_upgrade_signals"])
    assert reset_tables(cur, "mcp_upgrade_signals") == ()
    assert "TRUNCATE" in cur.statements[1]


def test_a_view_skips_instead_of_erroring_or_passing():
    """Production renders `mcp_calls_identity` as a view. Dropping it to make
    the fixture fit would delete a production object; erroring is what we are
    removing; passing would test a shape production does not have."""
    cur = _Cur({"mcp_calls_identity": "v"})
    with pytest.raises(pytest.skip.Exception) as exc:
        reset_tables(cur, "mcp_calls_identity")
    assert "mcp_calls_identity" in str(exc.value)
    assert "view" in str(exc.value)
    assert cur.statements == []


def test_a_materialized_view_skips_too():
    cur = _Cur({"x": "m"})
    with pytest.raises(pytest.skip.Exception) as exc:
        reset_tables(cur, "x")
    assert "materialized view" in str(exc.value)


def test_an_unresettable_relkind_skips_rather_than_touching_it():
    cur = _Cur({"x": "S"})
    with pytest.raises(pytest.skip.Exception):
        reset_tables(cur, "x")
    assert cur.statements == []


def test_it_refuses_a_connection_that_is_not_autocommit():
    """The fallback depends on surviving a failed DROP. In a transaction that
    failure aborts the session and every later statement fails instead."""
    cur = _Cur({"x": "r"}, autocommit=False)
    with pytest.raises(RuntimeError) as exc:
        reset_tables(cur, "x")
    assert "autocommit" in str(exc.value)
    assert cur.statements == []


def test_it_probes_every_name_and_splits_them():
    cur = _Cur({"kept": "r", "dropped": "r"}, refuses=["kept"])
    assert reset_tables(cur, "kept", "dropped", "gone") == ("dropped", "gone")
    assert cur.asked == ["kept", "dropped", "gone"]
