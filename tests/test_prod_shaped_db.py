"""Cover the decision `tests/_prod_shaped_db.reset_tables` makes per relation.

The 40 setup errors this helper exists to remove only appear against a
prod-shaped database, which no local test has. What IS testable here — and what
actually went wrong — is the CHOICE: truncate, create, or refuse. So the cursor
below answers the catalog probe from a declared relkind map and records what it
was asked to run. It reads the SQL it is handed; a stub that answered the same
way whatever it was asked would let the helper probe the wrong catalog, or skip
the probe entirely, and still pass.
"""
from __future__ import annotations

import pytest

from tests._prod_shaped_db import reset_tables


class _Cur:
    def __init__(self, catalog: dict[str, str]):
        self.catalog = catalog
        self.truncated: list[str] = []
        self.asked: list[str] = []
        self._answer: str | None = None

    def execute(self, q, params=None):
        text = q if isinstance(q, str) else str(q)
        if "pg_class" in text and "relkind" in text:
            assert params and len(params) == 1, f"probe unparameterised: {params}"
            self.asked.append(params[0])
            self._answer = self.catalog.get(params[0])
            return
        if "TRUNCATE" in text:
            self.truncated.append(text)
            return
        raise AssertionError(f"unexpected sql: {text[:90]}")

    def fetchone(self):
        return None if self._answer is None else (self._answer,)


def test_an_absent_table_is_reported_for_the_caller_to_create():
    cur = _Cur({})
    assert reset_tables(cur, "substations") == ("substations",)
    assert cur.truncated == []


def test_an_existing_table_is_truncated_and_not_reported_absent():
    cur = _Cur({"transmission_lines": "r"})
    assert reset_tables(cur, "transmission_lines") == ()
    assert len(cur.truncated) == 1
    assert "transmission_lines" in cur.truncated[0]
    assert "TRUNCATE" in cur.truncated[0]


def test_it_truncates_rather_than_drops():
    """The whole point. A DROP is what raised DependentObjectsStillExist, and
    CASCADE on a DROP would delete production's views from a shared branch."""
    cur = _Cur({"transmission_lines": "r"})
    reset_tables(cur, "transmission_lines")
    assert "DROP" not in cur.truncated[0].upper()


def test_a_partitioned_table_is_also_truncatable():
    cur = _Cur({"mcp_upgrade_signals": "p"})
    assert reset_tables(cur, "mcp_upgrade_signals") == ()
    assert len(cur.truncated) == 1


def test_a_view_skips_instead_of_erroring_or_passing():
    """Production renders `mcp_calls_identity` as a view. Dropping it to make
    the fixture fit would delete a production object; erroring is what we are
    removing; passing would test a shape production does not have."""
    cur = _Cur({"mcp_calls_identity": "v"})
    with pytest.raises(pytest.skip.Exception) as exc:
        reset_tables(cur, "mcp_calls_identity")
    assert "mcp_calls_identity" in str(exc.value)
    assert "view" in str(exc.value)
    assert cur.truncated == []


def test_a_materialized_view_skips_too():
    cur = _Cur({"x": "m"})
    with pytest.raises(pytest.skip.Exception) as exc:
        reset_tables(cur, "x")
    assert "materialized view" in str(exc.value)


def test_an_unresettable_relkind_skips_rather_than_truncating():
    cur = _Cur({"x": "S"})
    with pytest.raises(pytest.skip.Exception):
        reset_tables(cur, "x")
    assert cur.truncated == []


def test_it_probes_every_name_and_splits_them():
    cur = _Cur({"here": "r"})
    assert reset_tables(cur, "here", "gone") == ("gone",)
    assert cur.asked == ["here", "gone"]
    assert len(cur.truncated) == 1
