"""tests/test_brain_lane_driver_effector_read.py — the lane driver reads the
effector registry it already had (2026-08-29).

Lane 3 of the wiring shell. The driver's catalog was EIGHT hardcoded verbs, six
of which run an existing orchestrator and none of which changes the product.
Live 2026-08-29 04:08Z it chose `stop` on revenue at confidence 0.75 — a
correct decision inside an action space with nothing in it.

routes/squasher_action_classes already held a real registry: granted /
reversible / verifier_url / bound_params / breaker_tripped / runs_ok /
consecutive_failed, an append-only brain_action_class_runs ledger, three
registered classes, facility_dedup_apply at 7 runs / 0 failures. It was ~70%
built. The only gap was that the driver never read it — which is the thesis of
this whole shell: the action space is disjoint from the problem space.

Ways this wiring could go wrong, one test each:
  (1) ENUM FROZEN — the schema enum is built at module scope, so no registry
      verb can ever be offered no matter what is granted.
  (2) UNGRANTED VERB OFFERED — the driver can name an action the registry
      would refuse, and the grant stops meaning anything.
  (3) GUARDS BYPASSED — a second execution path that does not re-check
      eligibility, so a tripped breaker or revoked grant is ignored.
  (4) ★ BLIND READ RENDERED AS "NOTHING GRANTED" — the registry is
      unreadable and the driver silently proceeds with eight verbs, exactly
      the failure this shell exists to remove.
  (5) TICK VERBS DROPPED TOO EARLY — leaving the driver with `stop` alone.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_brain_lane_driver_effector_read.py -v
"""
from __future__ import annotations

import pytest


def _drv():
    from routes import brain_lane_driver as d
    return d


# ── (1) the enum follows the action space ────────────────────────────────

def test_the_schema_enum_is_built_from_the_action_space():
    d = _drv()
    acts = dict(d._ACTIONS)
    acts["effector:facility_dedup_apply"] = None
    schema = d.decision_schema(acts)
    assert "effector:facility_dedup_apply" in schema["properties"]["action"]["enum"], \
        "a granted effector was not offered to the model"


def test_the_static_schema_alone_can_never_offer_an_effector():
    """★REGRESSION (1). This is the shape of the original bug: a module-scope
    enum cannot grow, so no amount of granting changes what the model may
    choose."""
    d = _drv()
    assert not [k for k in d._DECISION_SCHEMA["properties"]["action"]["enum"]
                if k.startswith(d._EFFECTOR_PREFIX)]
    assert d._DECISION_SCHEMA["properties"]["action"]["enum"] == sorted(d._ACTIONS)


def test_decision_schema_does_not_mutate_the_module_schema():
    d = _drv()
    before = list(d._DECISION_SCHEMA["properties"]["action"]["enum"])
    d.decision_schema({**d._ACTIONS, "effector:x": None})
    assert d._DECISION_SCHEMA["properties"]["action"]["enum"] == before


# ── (2) only granted + eligible classes become verbs ─────────────────────

def test_only_eligible_classes_become_verbs(monkeypatch):
    d = _drv()
    from routes import squasher_action_classes as ac

    rows = [
        {"class": "facility_dedup_apply", "granted": True, "breaker_tripped": False},
        {"class": "never_granted",        "granted": False, "breaker_tripped": False},
        {"class": "breaker_is_tripped",   "granted": True,  "breaker_tripped": True},
    ]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(ac, "enabled", lambda: True)
    monkeypatch.setattr(ac, "_conn", lambda: Conn())
    monkeypatch.setattr(ac, "class_rows", lambda cur: rows)
    monkeypatch.setattr(ac, "eligible",
                        lambda r: ((bool(r.get("granted")) and not r.get("breaker_tripped")),
                                   "ok"))

    got = d.registry_actions()
    assert "effector:facility_dedup_apply" in got
    assert "effector:never_granted" not in got, "an ungranted class became a verb"
    assert "effector:breaker_is_tripped" not in got, "a tripped breaker was ignored"


def test_the_global_kill_switch_yields_no_effectors(monkeypatch):
    d = _drv()
    from routes import squasher_action_classes as ac
    monkeypatch.setattr(ac, "enabled", lambda: False)
    got = d.registry_actions()
    assert "__disabled__" in got
    assert not [k for k in got if k.startswith(d._EFFECTOR_PREFIX)]


# ── (4) ★ a blind read must not read as "nothing granted" ────────────────

def test_an_unreadable_registry_is_reported_not_silently_empty(monkeypatch):
    """★REGRESSION (4). 'the registry says nothing is granted' and 'I could not
    read the registry' are different facts. Rendering them the same way is the
    failure this entire shell exists to remove — so the action space carries
    the reason, and it is not the string 'ok'."""
    d = _drv()
    from routes import squasher_action_classes as ac

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ac, "enabled", lambda: True)
    monkeypatch.setattr(ac, "_conn", boom)

    got = d.registry_actions()
    assert "__error__" in got, "an unreadable registry returned a plain empty dict"

    acts, basis = d.available_actions()
    assert basis["registry_state"] != "ok"
    assert "connection refused" in basis["registry_state"]
    assert basis["registry"] == []


def test_a_readable_empty_registry_is_a_legitimate_ok(monkeypatch):
    """THE PAIRED CONTROL for the guard above: failing loudly on a blind read
    must not make every empty registry look broken."""
    d = _drv()
    from routes import squasher_action_classes as ac

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(ac, "enabled", lambda: True)
    monkeypatch.setattr(ac, "_conn", lambda: Conn())
    monkeypatch.setattr(ac, "class_rows", lambda cur: [])
    acts, basis = d.available_actions()
    assert basis["registry_state"] == "ok"
    assert basis["registry"] == []


# ── (5) the tick verbs survive until a registry verb has a record ────────

def test_the_static_verbs_are_still_there():
    """★REGRESSION (5). The handoff's sequencing note: drop the six tick verbs
    before the registry is read and the driver is left with `stop` alone."""
    d = _drv()
    for verb in ("audience_master_tick", "media_master_tick",
                 "indexnow_recent_submit", "per_tool_conversion_run",
                 "deep_dive_rotate", "brain_self_direct_tick",
                 "propose_finding", "stop"):
        assert verb in d._ACTIONS, "%s was dropped too early" % verb


def test_the_action_space_is_a_superset_of_the_static_catalog(monkeypatch):
    d = _drv()
    from routes import squasher_action_classes as ac
    monkeypatch.setattr(ac, "enabled", lambda: False)
    acts, _ = d.available_actions()
    assert set(d._ACTIONS).issubset(set(acts))


# ── (3) dispatch re-checks the guards at run time ────────────────────────

def test_an_effector_that_became_ineligible_is_not_executed(monkeypatch):
    """★REGRESSION (3). The class was eligible when the action space was built.
    A breaker can trip between then and now, and the registry's own contract is
    that a row edited straight into the table gets no free pass."""
    d = _drv()
    from routes import squasher_action_classes as ac

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()
        def commit(self): pass

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    executed = []
    monkeypatch.setattr(d, "_act_disabled", lambda: False)
    monkeypatch.setattr(d, "available_actions",
                        lambda: ({**d._ACTIONS, "effector:x": None}, {}))
    monkeypatch.setattr(ac, "_conn", lambda: Conn())
    monkeypatch.setattr(ac, "class_row", lambda cur, cls: {"class": cls})
    monkeypatch.setattr(ac, "eligible", lambda r: (False, "breaker tripped"))
    monkeypatch.setattr(ac, "execute_one",
                        lambda *a, **k: executed.append(1) or {"executed": True})

    out = d._act("revenue", {"action": "effector:x"})
    assert out["dispatched"] is False
    assert "breaker tripped" in out["note"]
    assert not executed, "execute_one ran for an ineligible class"


def test_no_open_row_is_not_recorded_as_a_failed_execution(monkeypatch):
    """Nothing to act on is not a failure. Recording it as one is the same
    conflation this shell removes everywhere else."""
    d = _drv()
    from routes import squasher_action_classes as ac

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()
        def commit(self): pass

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(d, "_act_disabled", lambda: False)
    monkeypatch.setattr(d, "available_actions",
                        lambda: ({**d._ACTIONS, "effector:x": None}, {}))
    monkeypatch.setattr(ac, "_conn", lambda: Conn())
    monkeypatch.setattr(ac, "class_row", lambda cur, cls: {"class": cls})
    monkeypatch.setattr(ac, "eligible", lambda r: (True, "ok"))
    monkeypatch.setattr(ac, "oldest_open_row_of_class", lambda cur, cls: None)

    out = d._act("revenue", {"action": "effector:x"})
    assert out["dispatched"] is False
    assert "no open row" in out["note"]


def test_dispatch_delegates_to_the_registry_executor(monkeypatch):
    """THE PAIRED CONTROL. The guards above are worthless if the happy path
    never runs — and delegation is the point: a second execution path would be
    a second thing to keep correct."""
    d = _drv()
    from routes import squasher_action_classes as ac

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()
        def commit(self): pass

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = {}

    def fake_execute(conn, cur, row, cls_row, **kw):
        seen["row"] = row
        seen["cls"] = cls_row.get("class")
        return {"executed": True, "verified": True, "outcome": "verified"}

    monkeypatch.setattr(d, "_act_disabled", lambda: False)
    monkeypatch.setattr(d, "available_actions",
                        lambda: ({**d._ACTIONS, "effector:facility_dedup_apply": None}, {}))
    monkeypatch.setattr(ac, "_conn", lambda: Conn())
    monkeypatch.setattr(ac, "class_row", lambda cur, cls: {"class": cls})
    monkeypatch.setattr(ac, "eligible", lambda r: (True, "ok"))
    monkeypatch.setattr(ac, "oldest_open_row_of_class",
                        lambda cur, cls: {"id": 42, "status": "awaiting_ops"})
    monkeypatch.setattr(ac, "execute_one", fake_execute)

    out = d._act("revenue", {"action": "effector:facility_dedup_apply"})
    assert out["dispatched"] is True
    assert seen["cls"] == "facility_dedup_apply"
    assert seen["row"]["id"] == 42
    assert out["effector"]["outcome"] == "verified"


def test_the_shadow_kill_still_stops_an_effector(monkeypatch):
    """BRAIN_LANE_DRIVER_ACT_DISABLED must gate the new path too, or the
    registry wiring quietly re-arms a driver the owner had parked."""
    d = _drv()
    monkeypatch.setattr(d, "_act_disabled", lambda: True)
    monkeypatch.setattr(d, "available_actions",
                        lambda: ({**d._ACTIONS, "effector:x": None}, {}))
    out = d._act("revenue", {"action": "effector:x"})
    assert out["dispatched"] is False
    assert "DISABLED" in out["note"]


# ── ★ THE CALL SITE ITSELF ───────────────────────────────────────────────
#
# The tests above verify decision_schema() in isolation, and a mutation
# reverting the CALL SITE back to the frozen module schema left every one of
# them green. A helper that is correct but unused is the same defect as a
# registry that is built but unread — this file's whole subject. These drive
# _reason() and read what it actually hands the model.

class _CaptureBody(Exception):
    """Carries the captured call out of _reason without an LLM round-trip."""

    def __init__(self, schema, system):
        super().__init__("captured")
        self.schema = schema
        self.system = system


def _drive_reason(monkeypatch, extra_actions):
    d = _drv()
    import routes.brain_llm_structured as bls

    def fake_build(model, system, messages, max_tokens=None, schema=None):
        raise _CaptureBody(schema, system)

    monkeypatch.setattr(bls, "build_messages_body", fake_build, raising=False)
    monkeypatch.setattr(d, "available_actions",
                        lambda: ({**d._ACTIONS, **extra_actions}, {"registry_state": "ok"}))
    # _reason returns {"error": "no_api_key"} before building anything unless a
    # key is present. A dummy is enough: fake_build raises before any request.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    try:
        out = d._reason("revenue", {"kpi_main": 1}, [], None, "kpi table")
    except _CaptureBody as c:
        return c
    raise AssertionError(
        "_reason never reached build_messages_body; it returned %r. These "
        "tests guard the call site, so a skip here would hide the very "
        "regression they exist to catch." % (out,))


def test_the_call_site_offers_registry_effectors_to_the_model(monkeypatch):
    """★REGRESSION. Reverting this call site to the frozen _DECISION_SCHEMA is
    a one-line change that re-breaks the lane completely, and every isolated
    test of decision_schema() stays green through it."""
    cap = _drive_reason(monkeypatch, {"effector:facility_dedup_apply": None})
    assert cap.schema is not None
    enum = cap.schema["properties"]["action"]["enum"]
    assert "effector:facility_dedup_apply" in enum, (
        "the model was handed the frozen eight-verb enum — a granted effector "
        "cannot be chosen no matter what the registry says")


def test_the_charter_describes_every_verb_it_offers(monkeypatch):
    """An enum option the charter never mentions is half-wired: selectable, and
    undescribed. The model is told what it does."""
    cap = _drive_reason(monkeypatch, {"effector:facility_dedup_apply": None})
    assert "effector:facility_dedup_apply" in (cap.system or ""), \
        "a verb was offered in the enum but never described in the charter"


def test_the_charter_stays_clean_when_nothing_is_granted(monkeypatch):
    """THE PAIRED CONTROL. With no granted class the charter must read exactly
    as before — no empty heading, no dangling bullet."""
    cap = _drive_reason(monkeypatch, {})
    assert _drv()._EFFECTOR_PREFIX not in (cap.system or "")
