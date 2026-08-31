"""util/ddl_once — run idempotent schema DDL at most once per process.

WHY IT EXISTS. Measured on the primary, 2026-08-31 ~09:5x UTC:

    pid 29441  COPY public.fiber_kmz_routes_old_0822 ...   549s   (the nightly dump)
    pid 30160  ALTER TABLE auto_trial_keys ADD COLUMN ...  268s   waiting
    pid 30760  CREATE TABLE IF NOT EXISTS auto_trial_keys    7s   waiting on 30160
    -> 17 of 20 active backends blocked

`ADD COLUMN IF NOT EXISTS` reads as free and is not: once the column exists the
statement does nothing, but it still REQUESTS ACCESS EXCLUSIVE, and in
PostgreSQL a *pending* exclusive request blocks every lock request behind it. A
pg_dump holds ACCESS SHARE on every table for its whole run, so a "defensive"
per-call ALTER converts a routine backup into an application stall.

The properties below are the ones whose failure is silent — a latch set on a
failed pass leaves a half-applied schema recorded as done, which is worse than
the contention this removes.
"""

import sys
import pathlib
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from util import ddl_once  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DCHUB_DDL_ONCE_ALWAYS", raising=False)
    ddl_once.reset()
    yield
    ddl_once.reset()


class _Cur:
    def __init__(self, boom=False):
        self.stmts = []
        self.boom = boom

    def execute(self, sql, params=None):
        self.stmts.append(str(sql))
        if self.boom:
            raise RuntimeError("ddl failed")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, boom=False):
        self.cur = _Cur(boom)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


DDL = ("ALTER TABLE t ADD COLUMN IF NOT EXISTS x TEXT",)


# ── the point ────────────────────────────────────────────────────────

def test_runs_once_then_never_again():
    a, b, c = _Conn(), _Conn(), _Conn()
    assert ddl_once.ensure_once("k", a, DDL) is True
    assert ddl_once.ensure_once("k", b, DDL) is False
    assert ddl_once.ensure_once("k", c, DDL) is False
    assert a.cur.stmts == list(DDL)
    assert b.cur.stmts == [] and c.cur.stmts == []


def test_distinct_keys_are_independent():
    a, b = _Conn(), _Conn()
    ddl_once.ensure_once("one", a, DDL)
    ddl_once.ensure_once("two", b, DDL)
    assert a.cur.stmts and b.cur.stmts


def test_every_statement_runs_on_the_first_pass():
    a = _Conn()
    many = ("ALTER TABLE t ADD COLUMN IF NOT EXISTS a TEXT",
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS b TEXT",
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS c TEXT")
    ddl_once.ensure_once("m", a, many)
    assert a.cur.stmts == list(many)


# ── failure must not latch ───────────────────────────────────────────

def test_a_failed_pass_is_retried():
    bad = _Conn(boom=True)
    assert ddl_once.ensure_once("k", bad, DDL) is False
    assert ddl_once.already_done("k") is False, \
        "a failed pass must not be recorded as done"
    assert bad.rollbacks == 1
    good = _Conn()
    assert ddl_once.ensure_once("k", good, DDL) is True
    assert good.cur.stmts == list(DDL)


def test_partial_failure_does_not_latch():
    """Statement 2 of 3 fails — the schema is half-applied and must be retried,
    not recorded as complete."""
    class _Partial(_Cur):
        def execute(self, sql, params=None):
            self.stmts.append(str(sql))
            if len(self.stmts) == 2:
                raise RuntimeError("boom")

    conn = _Conn()
    conn.cur = _Partial()
    ddl_once.ensure_once("p", conn, ("A", "B", "C"))
    assert ddl_once.already_done("p") is False


# ── guards against misuse ────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    ("", _Conn(), DDL),          # no key
    ("k", None, DDL),            # no conn
    ("k", _Conn(), ()),          # no statements
])
def test_missing_inputs_are_a_no_op(args):
    assert ddl_once.ensure_once(*args) is False


def test_a_swallowed_failure_never_raises():
    """A schema probe must not break the request it was guarding."""
    class _Explode:
        def cursor(self):
            raise RuntimeError("connection gone")

        def rollback(self):
            raise RuntimeError("also gone")

    assert ddl_once.ensure_once("k", _Explode(), DDL) is False


# ── escape hatch ─────────────────────────────────────────────────────

@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "ON"])
def test_env_override_restores_per_call_execution(val, monkeypatch):
    monkeypatch.setenv("DCHUB_DDL_ONCE_ALWAYS", val)
    a, b = _Conn(), _Conn()
    ddl_once.ensure_once("k", a, DDL)
    ddl_once.ensure_once("k", b, DDL)
    assert b.cur.stmts == list(DDL), "override must re-run the DDL"


def test_reset_clears_one_key_or_all():
    a = _Conn()
    ddl_once.ensure_once("k", a, DDL)
    assert ddl_once.already_done("k")
    ddl_once.reset("k")
    assert not ddl_once.already_done("k")
    ddl_once.ensure_once("k", _Conn(), DDL)
    ddl_once.reset()
    assert not ddl_once.already_done("k")


# ── thread safety ────────────────────────────────────────────────────

def test_concurrent_callers_run_the_ddl_at_most_once_each_key():
    """gunicorn runs 8 threads per worker; two hitting a cold key at once must
    not both fire the ALTER."""
    conns, errors = [], []

    def worker():
        try:
            c = _Conn()
            conns.append(c)
            ddl_once.ensure_once("race", c, DDL)
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ran = [c for c in conns if c.cur.stmts]
    assert len(ran) == 1, f"{len(ran)} threads executed the DDL, expected 1"


# ── the callable variant ─────────────────────────────────────────────

def test_call_variant_runs_once():
    n = {"c": 0}

    def fn():
        n["c"] += 1

    assert ddl_once.ensure_once_call("k", fn) is True
    assert ddl_once.ensure_once_call("k", fn) is False
    assert n["c"] == 1


def test_call_variant_failure_is_retried():
    def boom():
        raise RuntimeError("x")

    n = {"c": 0}

    def ok():
        n["c"] += 1

    assert ddl_once.ensure_once_call("k", boom) is False
    assert ddl_once.already_done("k") is False
    assert ddl_once.ensure_once_call("k", ok) is True
    assert n["c"] == 1


# ── the call sites are actually wired ────────────────────────────────

def test_hot_paths_no_longer_issue_bare_per_call_alters():
    """A helper nothing calls fixes nothing."""
    root = pathlib.Path(__file__).resolve().parents[1]

    lp = (root / "linkedin_poster.py").read_text()
    assert lp.count("_ddl_once(") == 2, \
        "linkedin_poster's two defensive ALTERs must both be wrapped"
    assert "from util.ddl_once import" in lp

    me = (root / "routes" / "marketing_engine.py").read_text()
    assert 'ensure_once("social_media_posts.share_urn"' in me, \
        "_remember_share_urn must not ALTER before every UPDATE"
    assert "from util.ddl_once import ensure_once" in me
