"""Two board lanes that measured the wrong thing, and now measure the thing.

★ WHY BOTH EXIST. `/api/v1/ops/deadman` publishes a lane's verdict and its
reason verbatim, and both of these lanes were telling an engineer to go do
work that was already done or could never be done:

  loop_flywheel/infra   counted down to a Neon Azure→AWS cutover that had
                        ALREADY EXECUTED (2026-07-13; the Azure project was
                        deleted 08-05). The constant was added 2026-07-24 —
                        eleven days AFTER the cutover — and observed nothing
                        but the calendar. It would have gone FAIL on
                        2026-09-13 and OVERDUE from 10-06, forever, with no
                        action by anyone able to clear it.
  loop_control/counter_canon  counted GREP HITS for /DISTINCT agent_id/ and
                        called two of them drift. Its own note conceded "a
                        grep hit is not proof two counters DISAGREE" — an
                        accurate disclaimer on a check that could never pass.

Behavioural throughout: every assertion drives the real lane function.
"""
import ast
import datetime
import io
import os

import pytest

from routes.loop_flywheel_master_shell import _lane_infra
from routes.loop_control_master_shell import _lane_counter_canon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AWS = "postgresql://u:pw@ep-polished-breeze-af22mhng-pooler.c-2.us-west-2.aws.neon.tech/db"
AZURE = "postgresql://u:pw@ep-old-waterfall.westus3.azure.neon.tech/db"


def _neon(monkeypatch, dsn):
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    if dsn is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", dsn)
    return next(c for c in _lane_infra() if c["id"] == "neon_off_azure")


# ── loop_flywheel/infra ───────────────────────────────────────────────
def test_an_aws_host_passes(monkeypatch):
    assert _neon(monkeypatch, AWS)["pass"] is True


def test_an_azure_host_fails(monkeypatch):
    """★ THE GREEN DIRECTION, inverted: a check that always passes would be
    as useless as the countdown that always failed."""
    c = _neon(monkeypatch, AZURE)
    assert c["pass"] is False
    assert "still on Azure" in c["detail"]


def test_an_unreadable_dsn_is_unknown_never_a_pass(monkeypatch):
    """The countdown's failure was asserting a state it never observed.
    Inverting that into a fake green is the same defect facing the other way."""
    assert _neon(monkeypatch, None)["pass"] is None
    assert _neon(monkeypatch, "not-a-dsn")["pass"] is None


def test_the_dsn_password_never_reaches_the_note(monkeypatch):
    """★ /api/v1/ops/deadman is PUBLIC and publishes `detail` verbatim."""
    for dsn in (AWS, AZURE):
        assert "s3cr3t" not in _neon(monkeypatch, dsn)["detail"]


def test_the_verdict_does_not_depend_on_todays_date(monkeypatch):
    """★ THE REGRESSION THIS LANE EXISTS FOR. The old check was pure date
    arithmetic, so its verdict changed with the calendar while the system
    stood still. Same DSN, wildly different 'today' — same answer."""
    import routes.loop_flywheel_master_shell as m

    class _FarFuture(datetime.date):
        @classmethod
        def today(cls):
            return cls(2031, 1, 1)

    before = _neon(monkeypatch, AWS)["pass"]
    monkeypatch.setattr(m.datetime, "date", _FarFuture)
    after = _neon(monkeypatch, AWS)["pass"]
    assert before is True and after is True, (
        "the verdict moved with the calendar — a countdown has come back")


def test_no_migration_countdown_constant_survives():
    """Belt and braces on the above: the named constant is gone."""
    src = io.open(os.path.join(ROOT, "routes", "loop_flywheel_master_shell.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    names = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
    assert "_NEON_MIGRATION_DUE" not in names
    assert "_NEON_WARN_DAYS" not in names


# ── loop_control/counter_canon ────────────────────────────────────────
class _Cur:
    def __init__(self, outer): self.o = outer
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.o.last = " ".join(sql.split())
    def fetchone(self):
        s = self.o.last
        if "information_schema.tables" in s or "to_regclass" in s:
            return (True,)
        if "COUNT(DISTINCT agent_id)" in s:
            return self.o.counts
        return (1,)
    def fetchall(self):
        if "information_schema.columns" in self.o.last:
            return [("created_at",), ("agent_id",), ("ip_address",),
                    ("session_id",), ("is_public_ip",), ("is_real_external",)]
        return []


class _Conn:
    def __init__(self, counts=(44, 83, 210)):
        self.counts, self.last = counts, ""
    def cursor(self, *a, **k): return _Cur(self)
    def close(self): pass


def _lane(counts=(44, 83, 210)):
    return {c["id"]: c for c in _lane_counter_canon(_Conn(counts))}


def test_no_db_is_unknown_not_a_verdict():
    got = _lane_counter_canon(None)
    assert got[0]["pass"] is None
    assert "never a zero and never a pass" in got[0]["detail"]


def test_it_publishes_the_canonical_VALUE_not_a_file_count():
    """★ The lane's name promises canon over values. Prove a number appears."""
    c = _lane()["canon_value"]
    assert c["pass"] is True
    assert "44 distinct agents" in c["detail"]


def test_the_retired_bases_are_reported_and_deliberately_not_scored():
    """They SHOULD differ — that is why the canonical basis exists. Scoring
    them equal would red the lane forever, which is the failure being fixed."""
    c = _lane()["canon_spread"]
    assert c["pass"] is None
    assert "raw ip_address=83" in c["detail"] and "session_id=210" in c["detail"]
    assert "166 agents off" in c["detail"]        # |210 - 44|


def test_more_than_one_file_querying_agent_id_no_longer_fails_the_lane():
    """★ THE OLD BEHAVIOUR, pinned as gone. Many modules legitimately query
    agent_id; counting them was never evidence that two counters disagree."""
    ids = _lane()
    assert "agent_count_sites" not in ids, "the grep-count check is back"
    assert ids["canon_emitters"]["pass"] is True


def test_the_emitter_check_is_a_call_site_not_a_substring(tmp_path, monkeypatch):
    """★ A module that only MENTIONS the helper in a comment must fail. This
    is the whole difference between this guard and the one it replaces."""
    import routes.loop_control_master_shell as m
    fake = {"flask_mcp_endpoints.py": "x = 1  # canonical_external_activity_sql\n",
            "routes/ai_reach.py": "from x import canonical_external_activity_sql\n"
                                  "q = canonical_external_activity_sql(7)\n",
            "routes/weekly_series.py": "q = canonical_external_activity_sql(7)\n"}
    monkeypatch.setattr(m, "_read",
                        lambda p: fake.get(os.path.relpath(p, m._repo_root())))
    c = _lane()["canon_emitters"]
    assert c["pass"] is False
    assert "flask_mcp_endpoints.py" in c["detail"]


def test_an_unparseable_emitter_is_unknown_not_a_pass(monkeypatch):
    import routes.loop_control_master_shell as m
    monkeypatch.setattr(m, "_read", lambda p: "def (((")
    c = _lane()["canon_emitters"]
    assert c["pass"] is None
    assert "UNKNOWN, not a pass" in c["detail"]
