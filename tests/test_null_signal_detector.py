"""The detector must not become defect number nine.

Every defect in the 2026-09-07/08 brain sweep failed as SILENCE: a gate that
rejected 45 of 45, a heartbeat key simply absent, a counter pinned at a
confident 0, twelve workflow runs all `skipped` in neutral grey. A detector
that hunts that shape is subject to it -- a scanner that silently scans
nothing reports a clean bill of health forever, and it will be believed
precisely because it is the thing that was supposed to catch this.

So the self-test is the real subject here, not the scan. Its three legs are
each pinned against the failure they exist for:

    scan_floor      a broken regex finds zero probes -> "no dead probes"
    negative canary a comparison that always passes -> misses everything
    positive canary an empty relation list -> flags all 69 and buries the real

and `ok` must be False with findings WITHHELD whenever any leg fails, because
an unverified scanner's output is worse than no output.

Stdlib + pytest; no DB, no network.
"""
import os
import re
import textwrap

import pytest

from routes import brain_null_signal_detector as d


# ── the scan ──────────────────────────────────────────────────────────
def test_scan_finds_probes_in_the_real_tree():
    """Non-vacuous by construction: the repo genuinely contains these."""
    probes = d.scan_table_probes()
    assert len(probes) >= d._MIN_PROBES, (
        f"only {len(probes)} probes found; the scan or the regex is broken")


def test_scan_records_file_and_line(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent("""
        def f(cur):
            cur.execute("SELECT to_regclass('public.widgets')")
    """))
    probes = d.scan_table_probes(tmp_path)
    assert "public.widgets" in probes
    assert probes["public.widgets"][0].endswith("m.py:3")


def test_scan_skips_tests_dir(tmp_path):
    """A test may probe a fixture table that production does not have;
    flagging it would train people to ignore this detector."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("to_regclass('fixture_only')\n")
    assert "fixture_only" not in d.scan_table_probes(tmp_path)


@pytest.mark.parametrize("src,expected", [
    ("to_regclass('public.a')", "public.a"),
    ('to_regclass("b")', "b"),
    ("to_regclass( 'public.c' )", "public.c"),
])
def test_probe_regex_shapes(tmp_path, src, expected):
    (tmp_path / "m.py").write_text(src + "\n")
    assert expected in d.scan_table_probes(tmp_path)


# ── the comparison ────────────────────────────────────────────────────
def test_schema_qualified_and_bare_names_both_resolve():
    """to_regclass resolves through search_path, so `public.x` and `x` are
    the same relation. Treating them differently would report false deaths."""
    live = {"widgets"}
    assert d.dead_probes({"public.widgets": ["a:1"]}, live) == []
    assert d.dead_probes({"widgets": ["a:1"]}, live) == []


def test_a_missing_table_is_reported_with_its_sites():
    out = d.dead_probes({"public.gone": ["r/x.py:9", "r/y.py:2"]}, {"kept"})
    assert len(out) == 1
    assert out[0]["probed"] == "public.gone"
    assert out[0]["site_count"] == 2


# ── the self-test: one leg per failure mode ───────────────────────────
def test_scan_floor_fails_when_the_scan_finds_nothing():
    """THE dangerous green: a broken scan reports no dead probes."""
    st = d._self_test({}, {"anything"})
    assert st["passed"] is False
    assert st["legs"]["scan_floor"]["passed"] is False


def test_positive_canary_fails_on_an_empty_relation_list():
    """A failed/empty table query would mark all 69 probes dead."""
    st = d._self_test({f"p{i}": ["x:1"] for i in range(d._MIN_PROBES)}, set())
    assert st["passed"] is False
    assert st["legs"]["positive_canary"]["passed"] is False


def test_negative_canary_fails_if_nothing_can_be_reported_dead(monkeypatch):
    """A comparison that always passes misses every real defect."""
    monkeypatch.setattr(d, "dead_probes", lambda probes, live: [])
    st = d._self_test({f"p{i}": ["x:1"] for i in range(d._MIN_PROBES)},
                      {d._CANARY_PRESENT})
    assert st["passed"] is False
    assert st["legs"]["negative_canary"]["passed"] is False


def test_all_three_legs_pass_on_a_healthy_run():
    st = d._self_test({f"p{i}": ["x:1"] for i in range(d._MIN_PROBES)},
                      {d._CANARY_PRESENT})
    assert st["passed"] is True
    assert all(l["passed"] for l in st["legs"].values())


def test_the_present_canary_is_a_table_that_really_exists():
    """The positive canary must name a relation the codebase actually uses,
    or the leg is decorative. This one is the table defect #2 should have
    named -- brain_proposed_code_fixes, not brain_proposed_code."""
    assert d._CANARY_PRESENT == "brain_proposed_code_fixes"
    probes = d.scan_table_probes()
    assert any(p.split(".")[-1] == d._CANARY_PRESENT for p in probes), (
        "the positive canary is no longer probed anywhere in the tree")


def test_the_absent_canary_cannot_collide_with_a_real_table():
    assert d._CANARY_MISSING.split(".")[-1].startswith("__")


# ── it must not read its own prose as evidence (2026-09-08) ───────────
# Its FIRST live run reported three findings -- `public.X`, `public.foo`
# and `foo` -- every one from its own docstring and the comment above
# _PROBE_RE, where the idiom is spelled out to explain it. 3 of 12
# findings were noise it manufactured about itself. A scanner that cites
# its own documentation is the same class of defect it exists to hunt.

def test_it_does_not_scan_its_own_module():
    """The three placeholder names from its docstring must not appear."""
    probes = d.scan_table_probes()
    for placeholder in ("foo", "public.foo", "public.X"):
        assert placeholder not in probes, (
            f"{placeholder!r} came from this detector's own prose — it is "
            f"reading its documentation as evidence")


def test_comment_lines_are_not_probes(tmp_path):
    (tmp_path / "m.py").write_text(
        "# to_regclass('public.explained_in_a_comment')\n"
        "cur.execute(\"SELECT to_regclass('public.real_one')\")\n")
    probes = d.scan_table_probes(tmp_path)
    assert "public.real_one" in probes
    assert "public.explained_in_a_comment" not in probes


def test_an_indented_comment_is_also_skipped(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f():\n    # to_regclass('public.indented_comment')\n    pass\n")
    assert "public.indented_comment" not in d.scan_table_probes(tmp_path)


def test_skipping_itself_does_not_collapse_the_scan():
    """Excluding one file must not take the scan under its floor — that
    would trade a noise bug for a silent one."""
    probes = d.scan_table_probes()
    assert len(probes) >= d._MIN_PROBES, (
        f"{len(probes)} probes after self-exclusion, floor {d._MIN_PROBES}")


# ══════════════════════════════════════════════════════════════════════
#  CHECK 2 — CAN'T-FAIL SIGNATURES (2026-09-18)
#
#  Same subject as above: the self-test, not the scan. A check that hunts
#  "this metric can only ever produce one value" has that exact failure
#  mode itself, so every run plants a known defect and must find it.
# ══════════════════════════════════════════════════════════════════════

class _SigCursor:
    """Cursor over a scripted (to_regclass, row) script keyed by table."""

    def __init__(self, tables, rows, raise_on=()):
        self._tables = set(tables)     # which tables "exist"
        self._rows = rows              # table -> (hits, total)
        self._raise_on = set(raise_on)
        self._pending = None

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            name = params[0]
            self._pending = ("regclass", name)
            return
        # the signal query — find which table it names
        for t in self._rows:
            if t in sql:
                if t in self._raise_on:
                    raise RuntimeError("relation exploded")
                self._pending = ("row", self._rows[t])
                return
        self._pending = ("row", (0, 0))

    def fetchone(self):
        kind, val = self._pending
        if kind == "regclass":
            return (val if val in self._tables else None,)
        return val


def _reg(name, table, boundary="low"):
    return {"name": name, "table": table, "boundary": boundary,
            "sql": f"SELECT a, b FROM {table}", "why": "test"}


# ── the evaluator ─────────────────────────────────────────────────────

def test_pinned_low_is_flagged():
    out = d.evaluate_bounded_signal(0, 500, "low")
    assert out["pinned"] is True and "never once" in out["reason"]


def test_both_values_present_is_not_flagged():
    assert d.evaluate_bounded_signal(250, 500, "low")["pinned"] is False


def test_small_sample_is_never_flagged():
    """★ Crying wolf on 3 rows gets the whole check muted."""
    out = d.evaluate_bounded_signal(0, 3, "low")
    assert out["pinned"] is False and out["sample_ok"] is False


def test_zero_rows_is_unmeasured_not_clean():
    """★ The distinction the sentinel lane lost for a month."""
    out = d.evaluate_bounded_signal(0, 0, "low")
    assert out["pinned"] is False
    assert "UNMEASURED" in out["reason"]


def test_boundary_direction_is_respected():
    """A signal declared `low` that sits at its HIGH boundary is a bad month,
    not a null signal — flagging it would bury the real findings."""
    assert d.evaluate_bounded_signal(500, 500, "high")["pinned"] is True
    assert d.evaluate_bounded_signal(500, 500, "low")["pinned"] is False


# ── the scan ──────────────────────────────────────────────────────────

def test_absent_table_is_unmeasured_never_clean():
    cur = _SigCursor(tables=set(), rows={"t_absent": (0, 900)})
    scan = d.scan_bounded_signals(cur, [_reg("s", "t_absent")])
    assert scan["measured"] == []
    assert scan["unmeasured"][0]["why_unmeasured"] == "table absent"


def test_raising_query_is_unmeasured_never_clean():
    cur = _SigCursor(tables={"t_boom"}, rows={"t_boom": (0, 900)},
                     raise_on={"t_boom"})
    scan = d.scan_bounded_signals(cur, [_reg("s", "t_boom")])
    assert scan["measured"] == []
    assert "RuntimeError" in scan["unmeasured"][0]["why_unmeasured"]


def test_scan_flags_a_pinned_signal():
    cur = _SigCursor(tables={"t_live"}, rows={"t_live": (0, 900)})
    scan = d.scan_bounded_signals(cur, [_reg("s", "t_live")])
    assert scan["measured"][0]["pinned"] is True


# ── the self-test: the real subject ───────────────────────────────────

def test_self_test_passes_on_a_healthy_scan():
    scan = {"measured": [{"pinned": False}] * d._MIN_MEASURED_SIGNALS,
            "unmeasured": []}
    assert d._self_test_bounded(scan)["passed"] is True


def test_self_test_fails_when_nothing_was_measured():
    """★ THE FLOOR. Every signal unmeasured means the check found nothing —
    reporting that as a clean bill of health is the defect it hunts."""
    st = d._self_test_bounded({"measured": [], "unmeasured": [{"name": "x"}]})
    assert st["passed"] is False
    assert st["legs"]["measured_floor"]["passed"] is False


def test_self_test_fails_when_the_planted_defect_is_missed(monkeypatch):
    """★ THE PLANTED DEFECT. If the evaluator stops detecting a pin, the
    self-test must catch it — structurally, on every run, not in a unit
    test that someone might delete."""
    monkeypatch.setattr(d, "evaluate_bounded_signal",
                        lambda *a, **k: {"pinned": False})
    st = d._self_test_bounded(
        {"measured": [{"pinned": False}] * 5, "unmeasured": []})
    assert st["passed"] is False
    assert st["legs"]["planted_defect"]["passed"] is False


def test_self_test_fails_when_the_check_flags_everything(monkeypatch):
    monkeypatch.setattr(d, "evaluate_bounded_signal",
                        lambda *a, **k: {"pinned": True})
    st = d._self_test_bounded(
        {"measured": [{"pinned": True}] * 5, "unmeasured": []})
    assert st["passed"] is False
    assert st["legs"]["healthy_canary"]["passed"] is False
    assert st["legs"]["small_sample_canary"]["passed"] is False


def test_registry_entries_are_well_formed():
    """Every entry must carry the keys the scanner reads — a typo here would
    make that signal permanently unmeasured and silently so."""
    assert len(d._BOUNDED_SIGNALS) >= d._MIN_MEASURED_SIGNALS
    for sig in d._BOUNDED_SIGNALS:
        assert set(sig) >= {"name", "table", "boundary", "sql", "why"}
        assert sig["boundary"] in ("low", "high")
        assert sig["table"] in sig["sql"], (
            f"{sig['name']}: sql does not name its own table, so the "
            f"to_regclass guard checks a different relation than it queries")


# ── 2026-09-18: the signal that emitted a true finding under a wrong cause ──

def test_registry_does_not_count_proposal_status_rejected():
    """★ REGRESSION PIN. The first live run of check 2 reported
    l5_proposal_rejections "0 of 194 — never once produced" off
    brain_proposed_code_fixes.status='rejected'. True, and a misleading cause:

      · Layer 5's automatic rejections (SQLite-stack guard, compile guard)
        `return` BEFORE the INSERT — a rejected proposal never becomes a row
        that could carry status='rejected';
      · that column's only writer is the admin-only, manually-invoked
        POST /api/v1/brain/proposed-code/neutralize "r67 one-off cleanup".

    So the count measured "did an admin hand-neutralize anything", not "does
    the brain reject bad proposals" — and the finding's `why` pointed the
    reader at a confidence threshold that has nothing to do with it.

    A detector that emits a true finding under a wrong cause sends the next
    reader to the wrong file. That is worse than emitting nothing, and it is
    the exact failure class this module exists to hunt."""
    for sig in d._BOUNDED_SIGNALS:
        sql = " ".join(sig["sql"].split())
        assert not ("brain_proposed_code_fixes" in sql
                    and "status = 'rejected'" in sql), (
            f"{sig['name']} counts brain_proposed_code_fixes.status='rejected'"
            " — rejections never reach that column; watch "
            "brain_issue_persistence.last_outcome instead")


def test_permafail_signal_watches_the_column_that_records_rejections():
    """The replacement must read brain_issue_persistence.last_outcome, which is
    what brain_v2_store.last_outcomes_map reads to skip permafail issues."""
    sig = next((x for x in d._BOUNDED_SIGNALS
                if x["name"] == "l5_permafail_rejections"), None)
    assert sig is not None, "the repointed signal is gone"
    assert sig["table"] == "brain_issue_persistence"
    sql = " ".join(sig["sql"].split())
    assert "last_outcome" in sql
    # the three outcomes brain_v2_layer5._PERMAFAIL actually emits
    for outcome in ("refused", "rejected_false_syntax_claim",
                    "rejected_sqlite_hallucination"):
        assert outcome in sql, f"{outcome} missing from the permafail set"


def test_permafail_set_matches_layer5(tmp_path):
    """★ WRITER/READER PIN. If Layer 5 adds or renames a permafail outcome and
    this registry is not updated, the signal silently stops seeing it — the
    disagreement has no runtime error, which is the whole shape check 2 hunts."""
    import os
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(d.__file__))), "routes", "brain_v2_layer5.py"),
        encoding="utf-8").read()
    m = re.search(r"_PERMAFAIL\s*=\s*\{(.*?)\}", src, re.S)
    assert m, "could not find _PERMAFAIL in brain_v2_layer5"
    layer5 = set(re.findall(r"[\"']([a-z_]+)[\"']", m.group(1)))
    sig = next(x for x in d._BOUNDED_SIGNALS
               if x["name"] == "l5_permafail_rejections")
    sql = " ".join(sig["sql"].split())
    missing = {o for o in layer5 if o not in sql}
    assert not missing, (
        f"brain_v2_layer5._PERMAFAIL emits {sorted(missing)} but the "
        f"l5_permafail_rejections signal does not count them")


# ══════════════════════════════════════════════════════════════════════
#  THE RATIO FLOOR (2026-09-18)
#
#  A zero floor cannot see a signal that produces both values while
#  carrying almost no information. autopilot verification read
#  "192 of 8531 — both values occur": true, and useless.
# ══════════════════════════════════════════════════════════════════════

def test_ratio_floor_is_opt_in():
    """★ THE LOAD-BEARING PROPERTY. Identical numbers; only the declared floor
    differs. A signal that declares none must NEVER be starved — otherwise an
    occurrence signal like l5_permafail_rejections (66 of 337 = 20%, perfectly
    healthy) would be flagged forever and the check would get muted."""
    assert d.evaluate_bounded_signal(10, 500, "low")["starved"] is False
    assert d.evaluate_bounded_signal(
        10, 500, "low", min_ratio=None)["starved"] is False
    assert d.evaluate_bounded_signal(
        10, 500, "low", min_ratio=0.50)["starved"] is True


def test_signal_under_its_declared_floor_is_starved():
    out = d.evaluate_bounded_signal(192, 8531, "low", min_ratio=0.50)
    assert out["starved"] is True and out["pinned"] is False
    assert out["ratio"] == round(192 / 8531, 4)
    assert "carries almost no information" in out["reason"]


def test_signal_above_its_floor_is_healthy():
    out = d.evaluate_bounded_signal(300, 400, "low", min_ratio=0.50)
    assert out["starved"] is False and out["pinned"] is False


def test_pinned_takes_precedence_over_starved():
    """0-of-N is the stronger statement; reporting both would double-count."""
    out = d.evaluate_bounded_signal(0, 500, "low", min_ratio=0.50)
    assert out["pinned"] is True and out["starved"] is False


def test_small_sample_is_neither_pinned_nor_starved():
    out = d.evaluate_bounded_signal(0, 3, "low", min_ratio=0.50)
    assert out["pinned"] is False and out["starved"] is False


def test_ratio_is_reported_even_without_a_floor():
    """Reporting the ratio on every signal is what lets a floor be CALIBRATED
    from a live run instead of guessed."""
    assert d.evaluate_bounded_signal(66, 337, "low")["ratio"] == round(66 / 337, 4)
    assert d.evaluate_bounded_signal(0, 0, "low")["ratio"] is None


# ── the self-test's two new legs ──────────────────────────────────────

def test_self_test_fails_if_the_floor_stops_flagging(monkeypatch):
    real = d.evaluate_bounded_signal
    monkeypatch.setattr(
        d, "evaluate_bounded_signal",
        lambda *a, **k: {**real(*a, **k), "starved": False})
    st = d._self_test_bounded({"measured": [{"pinned": False}] * 6,
                               "unmeasured": []})
    assert st["passed"] is False
    assert st["legs"]["starved_canary"]["passed"] is False


def test_self_test_fails_if_an_unfloored_signal_gets_starved(monkeypatch):
    """★ Guards the opt-in property structurally, every run."""
    real = d.evaluate_bounded_signal
    monkeypatch.setattr(
        d, "evaluate_bounded_signal",
        lambda *a, **k: {**real(*a, **k), "starved": True})
    st = d._self_test_bounded({"measured": [{"pinned": False}] * 6,
                               "unmeasured": []})
    assert st["passed"] is False
    assert st["legs"]["unfloored_canary"]["passed"] is False


# ── the corrected denominator ─────────────────────────────────────────

def test_autopilot_signal_counts_only_verifiable_rows():
    """★ The old entry counted verified rows against ALL rows and reported
    192 of 8531 (2.2%) — a coverage emergency that was ~98% bookkeeping."""
    sig = next(x for x in d._BOUNDED_SIGNALS
               if x["name"] == "autopilot_action_verification")
    sql = " ".join(sig["sql"].split())
    # denominator must be filtered, not a bare COUNT(*)
    assert "COUNT(*) FILTER (WHERE outcome = 'executed_ok')" in sql, (
        "denominator is not restricted to verifiable rows")
    assert sql.count("executed_ok") >= 2, (
        "numerator and denominator must BOTH be restricted to executed_ok")


def test_autopilot_denominator_matches_what_the_verifier_selects():
    """★ WRITER/READER PIN. The verifier only ever considers
    outcome='executed_ok' — encoded in the ix_autopilot_unverified partial
    index. If that predicate ever changes, this signal's denominator becomes
    wrong again silently, which is the exact shape check 2 hunts."""
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(d.__file__)))
    src = open(os.path.join(root, "routes", "brain_autopilot.py"),
               encoding="utf-8").read()
    m = re.search(r"ix_autopilot_unverified[^\"']*WHERE\s+(.+?)\"", src)
    assert m, "ix_autopilot_unverified partial index not found"
    pred = m.group(1)
    assert "outcome = 'executed_ok'" in pred, (
        f"the verifier's candidate predicate changed to: {pred} — "
        f"autopilot_action_verification's denominator must be updated to match")


def test_only_coverage_signals_declare_a_floor():
    """An occurrence signal with a floor would cry wolf forever. Today exactly
    one entry is a coverage signal; this pins that deliberateness."""
    floored = [x["name"] for x in d._BOUNDED_SIGNALS if "min_ratio" in x]
    assert floored == ["autopilot_action_verification"], (
        f"unexpected floored signals {floored} — a floor on an occurrence "
        f"signal (e.g. l5_permafail_rejections at 20%) fires forever")
    for x in d._BOUNDED_SIGNALS:
        if "min_ratio" in x:
            assert 0.0 < x["min_ratio"] < 1.0
#  THE `why` CONSUMER TEST (2026-09-18)
#
#  A finding's `why` is the first thing the next reader acts on, and
#  nothing type-checks prose. autopilot_action_verification shipped
#  claiming it gates class_success_weight; it does not, and the claim was
#  one grep from being disproved. It took THREE passes to land on the
#  right consumer — class_success_weight, then effect_ratio, then finally
#  the runaway quarantine.
#
#  You cannot test English. So each entry DECLARES its consumers as data,
#  the declaration is verified against the source, and the prose is
#  required to name a declared symbol — which is what stops the two
#  drifting apart.
# ══════════════════════════════════════════════════════════════════════

import ast as _ast


def _verify_claim(root, rel_file, symbol, column, role="consumer"):
    """Does `column` appear inside a real def/class scope that carries
    `symbol`? Returns (ok, detail).

    Module scope is NEVER accepted. A symbol that appears only at module
    level (a docstring, a DDL list) would make the scope the whole file and
    the check vacuous — the exact shape this module exists to hunt."""
    path = os.path.join(root, rel_file)
    if not os.path.exists(path):
        return False, f"{rel_file} does not exist"
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    try:
        tree = _ast.parse(src)
    except SyntaxError as e:
        return False, f"{rel_file} does not parse: {e}"
    defs = [n for n in _ast.walk(tree)
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                              _ast.ClassDef))]
    exact = [n for n in defs if n.name == symbol]
    cands = exact or [
        n for n in defs
        if any(symbol in l
               for l in lines[n.lineno - 1:getattr(n, "end_lineno", n.lineno)])]
    if not cands:
        return False, f"no def/class scope in {rel_file} contains '{symbol}'"
    for n in cands:
        lo, hi = n.lineno, getattr(n, "end_lineno", n.lineno)
        body = lines[lo - 1:hi]
        if not any(column in l for l in body):
            continue
        # The ROLE must be load-bearing, or it is decoration. A `consumer`
        # scope has to actually READ (a SELECT); a `producer` has to WRITE.
        # Without this, mislabelling _persist() — which only ever INSERTs
        # action_taken — as a consumer would pass silently.
        up = "\n".join(body).upper()
        if role == "consumer" and "SELECT" not in up:
            return False, (f"{rel_file}::{n.name}() writes '{column}' but never "
                           f"SELECTs it — declared role 'consumer' is wrong")
        if role == "producer" and not ("INSERT" in up or "UPDATE" in up):
            return False, (f"{rel_file}::{n.name}() does not INSERT/UPDATE "
                           f"'{column}' — declared role 'producer' is wrong")
        return True, f"{rel_file}::{n.name}():{lo}-{hi}"
    return False, (f"{len(cands)} scope(s) in {rel_file} carry '{symbol}' but "
                   f"none reads '{column}'")


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(d.__file__)))


def test_every_signal_declares_its_consumers():
    for sig in d._BOUNDED_SIGNALS:
        cons = sig.get("consumers")
        assert cons, (
            f"{sig['name']} declares no consumers — an undeclared `why` is "
            f"prose nothing can check, which is how "
            f"autopilot_action_verification shipped naming the wrong one")
        for c in cons:
            assert set(c) >= {"file", "symbol", "reads", "role"}, (
                f"{sig['name']}: malformed consumer {c}")
            assert c["role"] in ("consumer", "producer"), (
                f"{sig['name']}: role must be consumer|producer, got "
                f"{c['role']!r} — a producer must not be dressed up as a "
                f"consumer")


def test_every_declared_consumer_actually_reads_the_column():
    """★ THE POINT. Each declaration is checked against the source."""
    root = _repo_root()
    failures = []
    for sig in d._BOUNDED_SIGNALS:
        for c in sig.get("consumers", []):
            ok, detail = _verify_claim(root, c["file"], c["symbol"],
                                       c["reads"], c["role"])
            if not ok:
                failures.append(f"{sig['name']} -> {detail}")
    assert not failures, (
        "declared consumers that the source does not support:\n  "
        + "\n  ".join(failures))


def test_why_names_a_declared_consumer():
    """Binds the PROSE to the DATA. Without this the declaration could say one
    thing and the sentence a reader acts on say another — which is exactly the
    bug: the `why` claimed class_success_weight while nothing backed it."""
    for sig in d._BOUNDED_SIGNALS:
        syms = [c["symbol"] for c in sig.get("consumers", [])]
        assert any(s in sig["why"] for s in syms), (
            f"{sig['name']}: why names none of its declared consumers {syms} "
            f"— prose and declaration have drifted")


def test_the_verifier_rejects_the_claim_that_shipped():
    """★ MUST-FAIL CONTROL, pinned to the real defect. The original entry
    claimed class_success_weight consumed outcome_verified. It does not —
    _read_class_rate reads brain_fix_outcomes / autopilot_outcomes /
    brain_action_class_runs, and outcome_verified appears NOWHERE in
    brain_work_selector.py. If this ever passes, the verifier has gone
    vacuous and every other assertion above is worthless."""
    root = _repo_root()
    ok, detail = _verify_claim(root, "routes/brain_work_selector.py",
                               "class_success_weight", "outcome_verified")
    assert ok is False, (
        f"the verifier accepted a claim known to be false: {detail}")


def test_the_verifier_accepts_a_claim_known_to_be_true():
    """The other half of the control: a verifier that rejects everything would
    also pass the test above."""
    root = _repo_root()
    ok, detail = _verify_claim(root, "routes/brain_autopilot.py",
                               "autopilot_verify", "outcome_verified")
    assert ok is True, f"the verifier rejected a true claim: {detail}"


def test_module_scope_alone_is_never_enough():
    """A symbol that appears only at module level must not satisfy a claim —
    the scope would be the whole file and the check would be vacuous."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(os.path.join(t, "routes"))
        open(os.path.join(t, "routes", "m.py"), "w").write(
            "# my_symbol mentioned only in a comment\n"
            "SQL = 'SELECT the_column FROM x'\n"
            "def unrelated():\n    return 1\n")
        ok, _ = _verify_claim(t, "routes/m.py", "my_symbol", "the_column")
        assert ok is False


def _repo_function_names(root):
    """Every function/class name defined under routes/. Cached per run."""
    if getattr(_repo_function_names, "_cache", None) is not None:
        return _repo_function_names._cache
    names = set()
    rdir = os.path.join(root, "routes")
    for fn in os.listdir(rdir):
        if not fn.endswith(".py"):
            continue
        try:
            tree = _ast.parse(open(os.path.join(rdir, fn),
                                   encoding="utf-8").read())
        except Exception:
            continue
        for n in _ast.walk(tree):
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                              _ast.ClassDef)) and len(n.name) > 6:
                names.add(n.name)
    _repo_function_names._cache = names
    return names


def test_why_never_names_an_undeclared_repo_function():
    """★ THE REAL CHECK, and the one the first mutation run proved was missing.
    Requiring the why to name *a* declared symbol still allowed it to ALSO name
    a false one — exactly the original bug, where the sentence said
    class_success_weight and nothing backed it.

    So: any name in a `why` that is a REAL function defined under routes/ must
    be declared as a consumer of that signal. You may not name code you have
    not verified consumes this column."""
    root = _repo_root()
    repo_names = _repo_function_names(root)
    for sig in d._BOUNDED_SIGNALS:
        mine = {c["symbol"] for c in sig.get("consumers", [])}
        why = sig["why"]
        # A name counts as a CODE REFERENCE only when it is unambiguous:
        # written as `name()`, or carrying >= 2 underscores. Plain English
        # collides constantly otherwise — "suppresses" contains the real
        # function `suppress`, and `proposals` / `verdicts` are both ordinary
        # words and real defs. A snake_case name with two underscores is not
        # something prose produces by accident.
        named = set()
        for n in repo_names:
            called = re.search(r"\b" + re.escape(n) + r"\(\)", why)
            snake = n.count("_") >= 2 and re.search(
                r"\b" + re.escape(n) + r"\b", why)
            if called or snake:
                named.add(n)
        undeclared = named - mine
        assert not undeclared, (
            f"{sig['name']}: why names real repo function(s) "
            f"{sorted(undeclared)} that are NOT declared consumers of this "
            f"signal — declare them (and let the verifier check them) or stop "
            f"claiming them")


def test_why_does_not_name_a_symbol_it_has_not_declared():
    """★ Closes the hole the first mutation run found. Requiring the why to
    name *a* declared symbol still let it ALSO name a false one — which is
    precisely the original bug, where the sentence said class_success_weight
    and nothing backed it. Any symbol known to this registry that appears in a
    why must be declared BY THAT ENTRY."""
    known = {c["symbol"]
             for sig in d._BOUNDED_SIGNALS
             for c in sig.get("consumers", [])}
    for sig in d._BOUNDED_SIGNALS:
        mine = {c["symbol"] for c in sig.get("consumers", [])}
        for sym in known - mine:
            assert sym not in sig["why"], (
                f"{sig['name']}: why names '{sym}', which is a consumer of a "
                f"DIFFERENT signal and is not declared here — declare it or "
                f"stop claiming it")


def test_verifier_is_not_vacuous():
    """★ If _verify_claim ever returns True unconditionally, every consumer
    assertion above becomes decoration. Pin it on a claim that cannot hold:
    a column that appears nowhere in the named file."""
    root = _repo_root()
    ok, _ = _verify_claim(root, "routes/brain_work_selector.py",
                          "_read_class_rate", "outcome_verified", "consumer")
    assert ok is False
    ok, _ = _verify_claim(root, "routes/rag_master_shell.py",
                          "_persist", "action_taken", "consumer")
    assert ok is False, ("_persist only INSERTs action_taken; accepting it as "
                         "a 'consumer' means the role is not checked")
