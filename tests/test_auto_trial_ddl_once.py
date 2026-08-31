"""auto_trial's schema DDL must not run on every request.

A MEASURED PRODUCTION STALL, not a theory. Observed live 2026-08-31 ~09:5x UTC
on the primary:

    pid 29441  COPY public.fiber_kmz_routes_old_0822 ...      549s   (the dump)
    pid 30160  ALTER TABLE auto_trial_keys ADD COLUMN ...     268s   waiting
    pid 30760  CREATE TABLE IF NOT EXISTS auto_trial_keys       7s   waiting on 30160
    -> 17 of 20 active backends blocked

The chain, and why our own code is the cause rather than the victim:

  1. a pg_dump holds ACCESS SHARE on EVERY table for the whole dump,
  2. `_ensure_schema` fires `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` on every
     request. That is a no-op after the first run — but a no-op ALTER still
     REQUESTS ACCESS EXCLUSIVE, so it queues behind the dump,
  3. in PostgreSQL a PENDING exclusive request blocks every lock request that
     arrives after it. Ordinary reads of auto_trial_keys — which coexist with a
     dump perfectly happily — therefore stacked up behind our own pointless DDL.

The backup did not break trial minting. Our per-request DDL let it.

Same class as #3366 ("a no-op ALTER still takes ACCESS EXCLUSIVE — stop running
it per request"), which fixed one site. This is another one, on the revenue path.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "auto_trial.py"
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)


def _fn(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


class _Cur:
    def __init__(self, boom=False):
        self.executed = []
        self.boom = boom

    def execute(self, sql, params=None):
        # Keep the FULL statement: truncating here hid the column names
        # and made the migration assertions fail against correct code.
        self.executed.append(" ".join(str(sql).split()))
        if self.boom:
            raise RuntimeError("ddl failed")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, boom=False):
        self._cur = _Cur(boom)
        self.rollbacks = 0

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rollbacks += 1


def _load(env=None):
    """Execute the real _ensure_schema against stubs, fresh module namespace."""
    import types
    ns = {
        "os": types.SimpleNamespace(environ=dict(env or {})),
        "_SCHEMA": "CREATE TABLE IF NOT EXISTS auto_trial_keys (api_key TEXT)",
        "_SCHEMA_READY": False,
    }
    mod = ast.Module(body=[_fn("_ensure_schema")], type_ignores=[])
    exec(compile(mod, str(SRC), "exec"), ns)          # noqa: S102 — the point
    return ns


# ── the fix ──────────────────────────────────────────────────────────

def test_ddl_runs_once_not_on_every_request():
    """The whole point. Second and third calls must issue NOTHING."""
    ns = _load()
    c1, c2, c3 = _Conn(), _Conn(), _Conn()
    ns["_ensure_schema"](c1)
    ns["_ensure_schema"](c2)
    ns["_ensure_schema"](c3)
    assert len(c1._cur.executed) >= 5, "first call must actually ensure the schema"
    assert c2._cur.executed == [], "second call issued DDL — the lock request is back"
    assert c3._cur.executed == []


def test_the_first_call_still_creates_and_migrates():
    """Once-per-process must not become never: a fresh worker still needs the
    table and the added columns."""
    ns = _load()
    c = _Conn()
    ns["_ensure_schema"](c)
    joined = " | ".join(c._cur.executed)
    assert "CREATE TABLE IF NOT EXISTS auto_trial_keys" in joined
    for col in ("operator_email", "operator_name", "client_name",
                "daily_count", "daily_date"):
        assert col in joined, f"{col} migration lost"


# ── failure must not latch ───────────────────────────────────────────

def test_a_failed_pass_is_retried_not_latched():
    """A half-applied schema latched as done would leave the table permanently
    wrong and silently so — worse than the lock contention this fixes."""
    ns = _load()
    bad = _Conn(boom=True)
    ns["_ensure_schema"](bad)
    assert ns["_SCHEMA_READY"] is False, "a failed pass must not set the flag"
    assert bad.rollbacks == 1

    good = _Conn()
    ns["_ensure_schema"](good)
    assert good._cur.executed, "the retry must actually run"


def test_flag_is_only_set_inside_the_try():
    """Set after the last statement, on the clean path — never in a finally,
    and never before the DDL it certifies."""
    src = ast.get_source_segment(TEXT, _fn("_ensure_schema"))
    assert "_SCHEMA_READY = True" in src
    assert src.index("daily_date") < src.index("_SCHEMA_READY = True"), \
        "the flag must be set AFTER the final migration, not before"
    assert "finally" not in src


# ── the escape hatch ─────────────────────────────────────────────────

@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "ON"])
def test_env_override_restores_per_call_ddl(val):
    ns = _load({"DCHUB_AUTO_TRIAL_DDL_ALWAYS": val})
    c1, c2 = _Conn(), _Conn()
    ns["_ensure_schema"](c1)
    ns["_ensure_schema"](c2)
    assert c2._cur.executed, "override must restore the old per-call behaviour"


def test_override_is_off_by_default():
    ns = _load({})
    c1, c2 = _Conn(), _Conn()
    ns["_ensure_schema"](c1)
    ns["_ensure_schema"](c2)
    assert c2._cur.executed == []


# ── nobody re-introduces per-request DDL here ────────────────────────

def test_no_other_function_issues_alter_table():
    """If a new code path adds its own ALTER, the stall comes straight back."""
    offenders = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.FunctionDef) or node.name == "_ensure_schema":
            continue
        src = ast.get_source_segment(TEXT, node) or ""
        if "ALTER TABLE" in src.upper():
            offenders.append(node.name)
    assert not offenders, (
        f"these functions issue ALTER TABLE outside the once-per-process "
        f"guard: {offenders}. A no-op ALTER still requests ACCESS EXCLUSIVE.")
