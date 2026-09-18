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
