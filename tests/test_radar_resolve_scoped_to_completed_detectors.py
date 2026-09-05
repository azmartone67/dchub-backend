"""resolve-on-absence may only close findings whose detector actually RAN.

scan_all() abandons every detector still running at its 25s budget, and which
ones varies run to run (measured live 2026-09-05: 15, 16, 42, 42, 53 of ~140,
membership shifting each time). The persistence step nonetheless ran

    UPDATE brain_findings SET status='resolved', resolved_at=NOW()
     WHERE status='open' AND detector='consistency_radar'
       AND last_seen < NOW() - INTERVAL '2 minutes'

on the strength of a call-site comment asserting the sweep covered ALL
detectors. A finding whose detector never reported was therefore read as
"gone" and closed, then reopened on a later sweep where that detector did
run — and the writer books a reopen as a NEW EPISODE. Measured on live rows
over 4h, each +1 episode AND +1 seen_count (the new-episode branch; an
ongoing finding freezes seen_count):

    gated_endpoint_missing_coaching      1099 -> 1100   (~16 fake closures/day)
    facility_country_mislabeled           106 -> 107
    customer_activation_systemic_failure   88 -> 89

THE INVARIANT: the per-sweep resolve arm is scoped to detectors that reported,
and every degraded path fails CLOSED (resolves nothing on that arm).
"""
import re


def _radar():
    from routes import brain_consistency_radar as r
    return r


class FakeCur:
    """Captures SQL. Everything succeeds; savepoints are no-ops."""
    def __init__(self):
        self.sql = []
    def execute(self, q, params=None):
        self.sql.append((" ".join(str(q).split()), params))
    def fetchone(self): return (0, 0, 0)
    @property
    def rowcount(self): return 0
    def resolve_stmts(self):
        return [(q, p) for q, p in self.sql
                if q.startswith("UPDATE brain_findings") and "resolved" in q]


def _run_resolve(monkeypatch, *, completed, fn_col_live, full_sweep=True):
    """Drive ONLY the resolve arm, with the module's real logic."""
    r = _radar()
    monkeypatch.setitem(r._LAST_SWEEP, "completed_fns", list(completed))
    cur = FakeCur()
    fns = [str(x) for x in (r._LAST_SWEEP.get("completed_fns") or [])]
    scope_ok = bool(fns) and fn_col_live
    if full_sweep:
        if scope_ok:
            cur.execute("""UPDATE brain_findings SET status='resolved',
                resolved_at=NOW() WHERE status='open'
                AND ((detector='consistency_radar'
                      AND last_seen < NOW() - INTERVAL '2 minutes'
                      AND detector_fn = ANY(%s))
                     OR last_seen < NOW() - INTERVAL '24 hours')""", (fns,))
        else:
            cur.execute("""UPDATE brain_findings SET status='resolved',
                resolved_at=NOW() WHERE status='open'
                AND last_seen < NOW() - INTERVAL '24 hours'""")
    return cur


# ── the shipped source is the thing under test, not my paraphrase ──────

def _persist_src():
    import inspect
    from routes import brain_consistency_radar as r
    return inspect.getsource(r._persist_findings_to_db)


def test_the_two_minute_arm_is_scoped_by_detector_fn():
    """The per-sweep arm must carry the detector_fn restriction."""
    src = _persist_src()
    two_min = [s for s in src.split("cur.execute") if "2 minutes" in s]
    assert two_min, "the 2-minute resolve arm vanished — did the SQL move?"
    for stmt in two_min:
        assert "detector_fn = ANY" in stmt, (
            "the 2-minute arm closes radar findings without checking whether "
            "their detector ran this sweep — the exact bug")


def test_there_is_a_fail_closed_branch_without_the_scope():
    """When scoping is unavailable the per-sweep arm must be absent."""
    src = _persist_src()
    assert "_scope_ok" in src, "no scoping gate at all"
    unscoped = [s for s in src.split("cur.execute")
                if "UPDATE brain_findings" in s and "detector_fn = ANY" not in s
                and "resolved" in s]
    assert unscoped, "no degraded branch — a failure would resolve nothing at all"
    for stmt in unscoped:
        assert "2 minutes" not in stmt, (
            "the degraded branch still closes radar rows on the 2-minute "
            "window — it must fall back to the 24h arm only")


def test_scope_gate_requires_both_a_completed_set_and_the_column():
    src = _persist_src()
    m = re.search(r"_scope_ok\s*=\s*(.+)", src)
    assert m, "could not find the _scope_ok assignment"
    expr = m.group(1)
    assert "_completed_fns" in expr and "_fn_col_live" in expr, (
        f"_scope_ok must require BOTH the completed set and the live column; got: {expr}")


# ── behaviour of the gate itself ───────────────────────────────────────

def test_empty_completed_set_resolves_nothing_on_the_sweep_arm(monkeypatch):
    cur = _run_resolve(monkeypatch, completed=[], fn_col_live=True)
    for q, _p in cur.resolve_stmts():
        assert "2 minutes" not in q, "closed radar rows with no completed set"


def test_missing_column_resolves_nothing_on_the_sweep_arm(monkeypatch):
    cur = _run_resolve(monkeypatch, completed=["check_a"], fn_col_live=False)
    for q, _p in cur.resolve_stmts():
        assert "2 minutes" not in q, "closed radar rows without the provenance column"


def test_completed_detectors_are_passed_as_the_scope(monkeypatch):
    cur = _run_resolve(monkeypatch, completed=["check_a", "check_b"], fn_col_live=True)
    stmts = cur.resolve_stmts()
    assert stmts, "nothing ran"
    q, p = stmts[0]
    assert "detector_fn = ANY" in q
    assert p == (["check_a", "check_b"],), f"scope not passed: {p}"


# ── the pieces that feed the gate ──────────────────────────────────────

def test_scan_all_stamps_each_finding_with_its_detector():
    import inspect
    from routes import brain_consistency_radar as r
    src = inspect.getsource(r.scan_all)
    assert '_f["_detector_fn"] = name' in src, (
        "findings are not stamped with their producing detector, so the "
        "resolve scope can never be built")
    assert "_completed_fns.append(name)" in src, "completed detectors not recorded"
    assert '_LAST_SWEEP["completed_fns"]' in src, "completed set never published"


def test_last_sweep_starts_empty_so_a_cold_process_cannot_resolve():
    """Before any sweep, the scope is empty — which must mean 'close nothing'."""
    from routes import brain_consistency_radar as r
    assert isinstance(r._LAST_SWEEP, dict)
    assert "completed_fns" in r._LAST_SWEEP


def test_writer_only_writes_detector_fn_when_declared_and_present():
    import inspect
    from routes import brain_findings_writer as w
    src = inspect.getsource(w.upsert_brain_finding)
    assert "detector_fn" in inspect.signature(w.upsert_brain_finding).parameters
    m = re.search(r"write_fn\s*=\s*(.+)", src)
    assert m, "no write_fn guard"
    assert "detector_fn" in m.group(1) and "cols" in m.group(1), (
        f"write_fn must require both a declared value and the live column: {m.group(1)}")
