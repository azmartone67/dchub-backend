"""tool_copy claims — the per-platform description tuner, made accountable.

NO NETWORK, NO DB.

The tuner has rewritten tool descriptions per platform since 2026-06-26 and has
read a 30d adoption signal the whole time — but only as PROMPT INPUT. Nothing
judged a rewrite after it shipped, and mcp_tool_descriptions_per_platform is
UNIQUE(platform, tool_name) with no history, so a rewrite that REDUCED adoption
was both invisible and permanent.

These tests pin the four properties that make the loop closeable:
  1. the claim is judged over a window that does NOT overlap its baseline
  2. an unreadable instrument reads UNOBSERVED, never a confirming zero
  3. the prior description is captured, because nothing else keeps it
  4. a revert cannot register a claim of its own (no revert-the-revert loop)

★ Nothing at module scope (see CLAUDE.md — a module-scope failure aborts
collection and silently kills the whole suite).
"""
import pytest


# ── 1. window / horizon coherence ────────────────────────────────────
def test_the_judged_window_does_not_overlap_the_baseline_window():
    """Baseline is the N days BEFORE the rewrite; the verdict reads the trailing
    N days AT the horizon. Horizon must equal the window or the 'after' number
    still contains pre-rewrite traffic and every claim drifts to confirmed."""
    import routes.claim_ledger as cl
    assert cl.TOOL_COPY_HORIZON_HOURS == cl.TOOL_COPY_WINDOW_DAYS * 24


def test_the_metric_asks_for_the_same_window_the_horizon_waits_out():
    import routes.claim_ledger as cl
    captured = {}

    def fake_register(**kw):
        captured.update(kw)
        return {"ok": True, "id": 1}

    cl_register = cl.register_claim
    cl_open = cl._open_tool_copy_claim
    try:
        cl._open_tool_copy_claim = lambda p, t: False
        cl.register_claim = fake_register
        cl.register_tool_copy_claim("claude", "get_facility", "new copy",
                                    prior_description="old copy",
                                    baseline_calls=100)
    finally:
        cl.register_claim = cl_register
        cl._open_tool_copy_claim = cl_open
    assert f"days={cl.TOOL_COPY_WINDOW_DAYS}" in captured["expected_metric"]
    assert captured["horizon_hours"] == cl.TOOL_COPY_HORIZON_HOURS


# ── 2. an unreadable baseline must not become a confirming claim ─────
def test_an_unreadable_baseline_refuses_the_claim():
    """`>= 0` is satisfied by anything. A claim built on a baseline the
    instrument could not read would be confirmed by construction — the same
    trap the canon claim shipped with on 2026-08-23."""
    import routes.claim_ledger as cl
    assert cl.tool_copy_expectation(None) == -1
    assert cl.tool_copy_expectation("not a number") == -1
    assert cl.tool_copy_expectation(-3) == -1


def test_the_registrar_declines_rather_than_registering_a_free_pass():
    import routes.claim_ledger as cl
    calls = []
    cl_register = cl.register_claim
    cl_open = cl._open_tool_copy_claim
    try:
        cl._open_tool_copy_claim = lambda p, t: False
        cl.register_claim = lambda **kw: calls.append(kw) or {"ok": True, "id": 9}
        out = cl.register_tool_copy_claim("claude", "get_facility", "copy",
                                          baseline_calls=None)
    finally:
        cl.register_claim = cl_register
        cl._open_tool_copy_claim = cl_open
    assert out is None
    assert calls == [], "no claim may be registered on an unreadable baseline"


def test_the_bar_is_a_real_fall_not_any_wobble():
    import routes.claim_ledger as cl
    assert cl.tool_copy_expectation(100) == 60
    assert cl.tool_copy_expectation(10) == 6
    assert cl.tool_copy_expectation(0) == 0


def test_adoption_read_returns_none_not_zero_when_the_query_fails():
    """A zero from a broken instrument SATISFIES `>= floor(0.6 x baseline)` on
    any small cell. None resolves as unobserved instead."""
    import routes.ai_platform_tool_tuner as t

    class _Boom:
        def cursor(self):
            raise RuntimeError("connection reset")

        def rollback(self):
            pass

    assert t._adoption_calls(_Boom(), "claude", "get_facility", 14) is None


# ── 3. the prior description is captured ─────────────────────────────
def test_the_claim_carries_the_prior_description_and_says_it_is_revertible():
    import routes.claim_ledger as cl
    captured = {}
    cl_register = cl.register_claim
    cl_open = cl._open_tool_copy_claim
    try:
        cl._open_tool_copy_claim = lambda p, t: False
        cl.register_claim = lambda **kw: captured.update(kw) or {"ok": True, "id": 1}
        cl.register_tool_copy_claim("grok", "rank_markets", "new",
                                    prior_description="the old copy",
                                    baseline_calls=50)
    finally:
        cl.register_claim = cl_register
        cl._open_tool_copy_claim = cl_open
    assert captured["regime"]["prior_description"] == "the old copy"
    assert captured["regime"]["revertible"] is True


def test_a_claim_with_no_prior_names_itself_unrevertible():
    import routes.claim_ledger as cl
    captured = {}
    cl_register = cl.register_claim
    cl_open = cl._open_tool_copy_claim
    try:
        cl._open_tool_copy_claim = lambda p, t: False
        cl.register_claim = lambda **kw: captured.update(kw) or {"ok": True, "id": 1}
        cl.register_tool_copy_claim("grok", "rank_markets", "new",
                                    prior_description=None, baseline_calls=50)
    finally:
        cl.register_claim = cl_register
        cl._open_tool_copy_claim = cl_open
    assert captured["regime"]["revertible"] is False


# ── 4. the revert must not register a claim of its own ───────────────
def _fake_conn(prior=None, calls=7):
    """Minimal conn double: _prior_description and _adoption_calls both read
    through c.cursor()."""
    class _Cur:
        def __init__(self, outer):
            self.outer = outer

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.outer.executed.append(sql)
            self._sql = sql

        def fetchone(self):
            return (prior,) if prior is not None else None

        def fetchall(self):
            return [("claude", "", calls)]

    class _C:
        def __init__(self):
            self.executed = []

        def cursor(self):
            return _Cur(self)

        def commit(self):
            pass

        def rollback(self):
            pass

    return _C()


@pytest.fixture()
def armed(monkeypatch):
    """Claims are DARK by default; arm the flag and reset the per-run cap."""
    import routes.ai_platform_tool_tuner as t
    monkeypatch.setenv("TOOL_COPY_CLAIMS_ENABLED", "1")
    t.reset_claim_run()
    yield t
    t.reset_claim_run()


def test_a_normal_rewrite_registers_a_claim(monkeypatch, armed):
    t = armed
    seen = []
    import routes.claim_ledger as cl
    monkeypatch.setattr(cl, "register_tool_copy_claim",
                        lambda *a, **k: seen.append((a, k)) or 1)
    t._upsert(_fake_conn(prior="old copy"), "claude", "get_facility",
              "brand new copy", "tuner:reseed")
    assert len(seen) == 1, "a shipped rewrite must pre-register its claim"
    assert seen[0][1]["prior_description"] == "old copy"


def test_claims_are_dark_unless_the_flag_is_exactly_one(monkeypatch):
    """★ DARK BY DEFAULT — the ACTION_CLASSES_ENABLED convention. Anything but
    the exact string "1" leaves the tuner behaving as it did before."""
    import routes.ai_platform_tool_tuner as t
    import routes.claim_ledger as cl
    seen = []
    monkeypatch.setattr(cl, "register_tool_copy_claim",
                        lambda *a, **k: seen.append(1) or 1)
    for val in (None, "", "0", "true", "yes", "TRUE", " 1 x"):
        if val is None:
            monkeypatch.delenv("TOOL_COPY_CLAIMS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("TOOL_COPY_CLAIMS_ENABLED", val)
        t.reset_claim_run()
        t._upsert(_fake_conn(prior="old"), "claude", "get_facility", "new",
                  "tuner:reseed")
    assert seen == [], "only the exact string '1' may arm claim registration"


def test_the_per_run_cap_bounds_connections_and_counts_what_it_dropped(monkeypatch, armed):
    """A forced reseed is up to 120 upserts and register_claim opens its OWN
    connection each time. The cap must hold AND report — a silent cap reads as
    full coverage."""
    t = armed
    import routes.claim_ledger as cl
    monkeypatch.setattr(t, "_CLAIM_CAP_PER_RUN", 3)
    monkeypatch.setattr(cl, "register_tool_copy_claim", lambda *a, **k: 1)
    for i in range(10):
        t._upsert(_fake_conn(prior="old"), "claude", f"tool_{i}", "new",
                  "tuner:reseed")
    assert t._CLAIM_RUN["registered"] == 3
    assert t._CLAIM_RUN["capped"] == 7


def test_reset_clears_the_cap_between_runs(armed):
    t = armed
    t._CLAIM_RUN["registered"] = 99
    t.reset_claim_run()
    assert t._CLAIM_RUN == {"registered": 0, "capped": 0}


def test_a_revert_write_registers_NOTHING(monkeypatch, armed):
    """★ The loop guard. If the restoration registered its own claim, that claim
    would be judged in turn, could be refuted, and would revert the revert."""
    t = armed
    import routes.claim_ledger as cl
    seen = []
    monkeypatch.setattr(cl, "register_tool_copy_claim",
                        lambda *a, **k: seen.append(1) or 1)
    t._upsert(_fake_conn(prior="old copy"), "claude", "get_facility",
              "old copy", t._REVERT_GENERATED_BY_PREFIX + "revert")
    assert seen == [], "a revert must not pre-register a claim"


def test_a_rewrite_with_an_unreadable_baseline_registers_nothing(monkeypatch, armed):
    t = armed
    import routes.claim_ledger as cl
    seen = []
    monkeypatch.setattr(cl, "register_tool_copy_claim",
                        lambda *a, **k: seen.append(1) or 1)
    monkeypatch.setattr(t, "_adoption_calls", lambda *a, **k: None)
    t._upsert(_fake_conn(prior="old"), "claude", "get_facility", "new",
              "tuner:reseed")
    assert seen == []


def test_the_revert_prefix_is_what_the_ledger_actually_writes():
    """The guard is a string compare across two modules — pin them together, or
    a rename on one side silently re-arms the loop."""
    import routes.ai_platform_tool_tuner as t
    assert "claim_ledger:revert".startswith(t._REVERT_GENERATED_BY_PREFIX)


# ── 5. the revert itself ─────────────────────────────────────────────
def _ledger_conn(rows):
    """Stands in for the ledger connection. `rows` is [(id, regime_dict)]."""
    class _Cur:
        def __init__(self):
            self.updates = []

        def execute(self, sql, params=None):
            self._sel = sql.strip().upper().startswith("SELECT")
            if not self._sel:
                self.updates.append(params)

        def fetchall(self):
            return list(rows)

    class _C:
        def __init__(self):
            self.autocommit = False
            self._cur = _Cur()

        def cursor(self):
            return self._cur

        def close(self):
            pass

    return _C()


@pytest.fixture()
def ledger(monkeypatch):
    import routes.claim_ledger as cl
    monkeypatch.setattr(cl, "_db_url", lambda: "postgres://stub")
    monkeypatch.setattr(cl, "ensure_schema", lambda: True)
    return cl


def test_a_refuted_claim_restores_the_prior_description(ledger, monkeypatch):
    """★ The whole point of pre-registering. Refuted means the rewrite measurably
    reduced adoption — put the old copy back."""
    conn = _ledger_conn([(11, {"platform": "claude", "tool": "get_facility",
                               "prior_description": "the old copy"})])
    monkeypatch.setattr(ledger, "_conn", lambda: conn)
    wrote = []
    out = ledger.revert_refuted_tool_copy(
        upsert=lambda c, p, t, d, by: wrote.append((p, t, d, by)))
    assert out["reverted"] == 1
    assert wrote == [("claude", "get_facility", "the old copy",
                      "claim_ledger:revert")]


def test_the_revert_writes_under_the_prefix_the_tuner_guards_on(ledger, monkeypatch):
    """If the revert wrote under any other generated_by, the tuner would treat
    the restoration as a fresh rewrite and register a claim for it."""
    import routes.ai_platform_tool_tuner as t
    conn = _ledger_conn([(12, {"platform": "grok", "tool": "rank_markets",
                               "prior_description": "old"})])
    monkeypatch.setattr(ledger, "_conn", lambda: conn)
    wrote = []
    ledger.revert_refuted_tool_copy(
        upsert=lambda c, p, tl, d, by: wrote.append(by))
    assert wrote[0].startswith(t._REVERT_GENERATED_BY_PREFIX)


def test_an_already_reverted_claim_is_not_reapplied(ledger, monkeypatch):
    conn = _ledger_conn([(13, {"platform": "claude", "tool": "x",
                               "prior_description": "old",
                               "reverted_at": "2026-08-25T00:00:00+00:00"})])
    monkeypatch.setattr(ledger, "_conn", lambda: conn)
    wrote = []
    out = ledger.revert_refuted_tool_copy(upsert=lambda *a: wrote.append(a))
    assert out["reverted"] == 0 and wrote == []
    assert out["skipped"][0]["why"] == "already reverted"


def test_a_claim_with_no_prior_is_skipped_not_blanked(ledger, monkeypatch):
    """Reverting to None would erase the override entirely — a different change,
    not a restoration."""
    conn = _ledger_conn([(14, {"platform": "claude", "tool": "x",
                               "prior_description": None})])
    monkeypatch.setattr(ledger, "_conn", lambda: conn)
    wrote = []
    out = ledger.revert_refuted_tool_copy(upsert=lambda *a: wrote.append(a))
    assert out["reverted"] == 0 and wrote == []


def test_one_failing_revert_does_not_block_the_rest(ledger, monkeypatch):
    conn = _ledger_conn([
        (15, {"platform": "claude", "tool": "a", "prior_description": "old_a"}),
        (16, {"platform": "claude", "tool": "b", "prior_description": "old_b"}),
    ])
    monkeypatch.setattr(ledger, "_conn", lambda: conn)
    done = []

    def _upsert(c, p, t, d, by):
        if t == "a":
            raise RuntimeError("deadlock")
        done.append(t)

    out = ledger.revert_refuted_tool_copy(upsert=_upsert)
    assert done == ["b"] and out["reverted"] == 1
    assert any("upsert failed" in str(s.get("why")) for s in out["skipped"])


def test_verify_reports_the_reverts_it_ran(ledger, monkeypatch):
    """The revert must be WIRED, not a function nobody calls."""
    monkeypatch.setattr(ledger, "verify_due_claims", lambda limit=None: {"ok": True})
    monkeypatch.setattr(ledger, "revert_refuted_tool_copy",
                        lambda *a, **k: {"ok": True, "reverted": 2})
    monkeypatch.setattr(ledger, "_authed", lambda: True)
    monkeypatch.setattr(ledger, "_limit", lambda: 25)
    monkeypatch.setattr(ledger, "_no_store", lambda r: r)
    import flask
    app = flask.Flask(__name__)
    with app.test_request_context("/api/v1/brain/claims/verify", method="POST"):
        body = ledger.claims_verify().get_json()
    assert body["tool_copy_reverts"]["reverted"] == 2


# ── 6. one bet in flight per cell ────────────────────────────────────
def test_a_cell_already_under_test_does_not_get_a_second_claim(monkeypatch):
    """★ Two open claims on one (platform, tool) means a refuted FIRST claim
    reverts to v1 and clobbers the v3 the second claim is measuring."""
    import routes.claim_ledger as cl
    calls = []
    monkeypatch.setattr(cl, "_open_tool_copy_claim", lambda p, t: True)
    monkeypatch.setattr(cl, "register_claim",
                        lambda **kw: calls.append(kw) or {"ok": True, "id": 1})
    out = cl.register_tool_copy_claim("claude", "get_facility", "v3",
                                      prior_description="v2", baseline_calls=40)
    assert out is None and calls == []


def test_an_unreadable_ledger_refuses_the_second_bet(monkeypatch):
    """Fail-soft in the SAFE direction: a probe that cannot read must not let a
    second claim through."""
    import routes.claim_ledger as cl

    def _boom():
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(cl, "_db_url", lambda: "postgres://stub")
    monkeypatch.setattr(cl, "_conn", _boom)
    assert cl._open_tool_copy_claim("claude", "get_facility") is True


def test_no_database_at_all_also_refuses(monkeypatch):
    import routes.claim_ledger as cl
    monkeypatch.setattr(cl, "_db_url", lambda: None)
    assert cl._open_tool_copy_claim("claude", "get_facility") is True


# ── 7. the metric must address a route that EXISTS ───────────────────
def test_the_claim_metric_path_is_a_registered_route():
    """★ The highest-value guard here. claim_ledger names the instrument as a
    STRING; nothing else checks it resolves. A typo, or a route renamed on the
    tuner side, would make every tool_copy claim read `unobserved` forever —
    the instrument gap would look exactly like "no data yet" and the loop would
    silently never close. Assert the two halves against each other."""
    import flask
    import routes.ai_platform_tool_tuner as t
    import routes.claim_ledger as cl
    app = flask.Flask(__name__)
    app.register_blueprint(t.ai_platform_tool_tuner_bp)
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert cl.TOOL_COPY_ADOPTION_PATH in rules, (
        f"{cl.TOOL_COPY_ADOPTION_PATH} is not a route on the tuner blueprint; "
        f"adoption-ish routes present: "
        f"{sorted(r for r in rules if 'adoption' in r)}")


def test_the_registered_metric_parses_back_to_that_path():
    """The path goes into the metric string with a query appended — the ledger's
    own parser must recover exactly the registered route, or resolve_metric
    fetches something else."""
    import routes.claim_ledger as cl
    captured = {}
    cl_register, cl_open = cl.register_claim, cl._open_tool_copy_claim
    try:
        cl._open_tool_copy_claim = lambda p, t: False
        cl.register_claim = lambda **kw: captured.update(kw) or {"ok": True, "id": 1}
        cl.register_tool_copy_claim("claude", "get_facility", "copy",
                                    prior_description="old", baseline_calls=10)
    finally:
        cl.register_claim, cl._open_tool_copy_claim = cl_register, cl_open
    scheme, target, field = cl.parse_metric(captured["expected_metric"])
    assert scheme == "get"
    assert target.split("?", 1)[0] == cl.TOOL_COPY_ADOPTION_PATH
    assert field == "calls"
