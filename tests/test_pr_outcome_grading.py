"""The PR outcome monitor must grade honestly — or not at all.

MEASURED 2026-09-21: 88 of 88 merged brain_pr_outcomes rows were
`unknown` / `no_baseline`. The cause was an accident: the monitor guesses a
page from a module name (`routes/claim_ledger.py` -> `/claim-ledger`) and 0 of
54 guesses exist as a sentinel page. Behind that sat a worse bug —
`sentinel_before` is snapshotted AFTER the merge, and `sentinel_after IS
sentinel_before` unless the run waits. Fix the join and every PR would grade
`success` against ITSELF, and L6 would learn that everything it ships works.

The replacement grades by RECURRENCE: a brain PR names its target finding
(`**Finding:** `<key>``); if a later PR re-targets that finding after this one
merged, the fix did not hold. Over all 14 merged L5 drafts: 4 name a finding,
1 recurred (#4202 cf_cache_rate_low, re-targeted by #4567 five days later).

Harness: CI installs pytest only and the module imports Flask, so the pure
pieces are AST-extracted and exec'd — the real functions.
"""
import os
import ast
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "brain_pr_outcome_monitor.py")
SRC = open(MOD, encoding="utf-8").read()


def _load():
    pieces = []
    for node in ast.parse(SRC).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_FINDING_LINE_RE"
                for t in node.targets):
            pieces.append(ast.get_source_segment(SRC, node))
        elif isinstance(node, ast.FunctionDef) and node.name in (
                "extract_finding", "recurrence_plan"):
            pieces.append(ast.get_source_segment(SRC, node))
    ns = {"re": re}
    exec(compile("\n\n".join(pieces), MOD, "exec"), ns)
    for n in ("_FINDING_LINE_RE", "extract_finding", "recurrence_plan"):
        assert n in ns, f"AST extraction missed {n}"
    return ns


NS = _load()
extract = NS["extract_finding"]
plan = NS["recurrence_plan"]


def _fn(name):
    return next(n for n in ast.parse(SRC).body
                if isinstance(n, ast.FunctionDef) and n.name == name)


# ── The sentinel must never grade again ──────────────────────────────────

def test_the_sentinel_path_never_assigns_success_or_regression():
    """★ The landmine. A success here would be a page compared with itself."""
    fn = _fn("monitor_recent_prs")
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and node.value.value in ("success", "regression")):
            tgt = ast.dump(node.targets[0])
            assert "outcome" not in tgt, (
                f"monitor_recent_prs assigns outcome={node.value.value!r} — the "
                "sentinel 'before' is taken AFTER the merge, so any such grade "
                "compares a page against itself")


def test_ungraded_rows_say_why():
    """`no_baseline` for everything hid the reason. The two real ones:"""
    seg = ast.get_source_segment(SRC, _fn("monitor_recent_prs"))
    assert "no_pre_merge_baseline" in seg and "no_page_touched" in seg


# ── extract_finding reads the producer's structured line only ───────────

REAL_L5_BODY = ("## Brain Layer-5 auto-proposed fix\n\n**Proposal:** #105516\n"
                "**Finding:** `cf_cache_rate_low`\n**Loop:** `cache_rate`\n")


def test_extracts_the_structured_finding_line():
    """Verbatim shape written by brain_backlog_admin (checked on #4567)."""
    assert extract(REAL_L5_BODY) == "cf_cache_rate_low"


def test_no_finding_line_means_no_finding():
    """#4222, #4738 and #3421 name none — they must not be graded."""
    assert extract("**Proposal:** #265\n**Loop:** `dchub://coverage/dcgi`\n") == ""
    assert extract("") == "" and extract(None) == ""


def test_prose_mentioning_a_finding_is_not_a_finding_line():
    assert extract("This fixes the Finding: cf_cache_rate_low issue.") == ""


# ── recurrence_plan: a genuine LATER re-target only ──────────────────────

def _pr(n, f, created, merged=None):
    return {"number": n, "finding": f, "created_at": created, "merged_at": merged}


def test_the_measured_case_recurs():
    """#4202 merged 09-08T04:54; #4567 opened 09-13T12:34 on the same finding."""
    out = plan([_pr(4202, "cf_cache_rate_low", "2026-09-08T04:31:29Z", "2026-09-08T04:54:26Z"),
                _pr(4567, "cf_cache_rate_low", "2026-09-13T12:34:07Z", "2026-09-13T21:35:52Z")])
    assert out == {4202: 4567}


def test_a_pr_opened_before_the_merge_is_not_a_recurrence():
    """Concurrent PRs on one finding are not evidence the first one failed."""
    out = plan([_pr(1, "f", "2026-09-01T00:00:00Z", "2026-09-05T00:00:00Z"),
                _pr(2, "f", "2026-09-03T00:00:00Z", "2026-09-06T00:00:00Z")])
    assert 1 not in out


def test_different_findings_never_recur_each_other():
    out = plan([_pr(1, "a", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"),
                _pr(2, "b", "2026-09-10T00:00:00Z", "2026-09-11T00:00:00Z")])
    assert out == {}


def test_an_unmerged_pr_is_never_graded():
    out = plan([_pr(1, "f", "2026-09-01T00:00:00Z", None),
                _pr(2, "f", "2026-09-10T00:00:00Z", "2026-09-11T00:00:00Z")])
    assert 1 not in out


def test_an_open_later_pr_still_counts_as_the_finding_coming_back():
    out = plan([_pr(1, "f", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"),
                _pr(2, "f", "2026-09-10T00:00:00Z", None)])
    assert out == {1: 2}


def test_no_finding_is_skipped():
    out = plan([_pr(1, "", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"),
                _pr(2, "", "2026-09-10T00:00:00Z", "2026-09-11T00:00:00Z")])
    assert out == {}


# ── The grader's write discipline ────────────────────────────────────────

def test_grader_only_upgrades_unknown():
    """A real grade must never be overwritten by recurrence."""
    seg = ast.get_source_segment(SRC, _fn("grade_recurrences"))
    assert "AND outcome = 'unknown'" in seg
    assert "stored.get(n) == \"unknown\"" in seg


def test_grader_never_writes_success():
    """Absence of a re-target is absence of evidence, not success."""
    seg = ast.get_source_segment(SRC, _fn("grade_recurrences"))
    assert "'success'" not in seg and '"success"' not in seg


def test_summary_counts_recurred_as_merged():
    """Otherwise a recurred PR silently drops out of L6's denominator."""
    seg = ast.get_source_segment(SRC, _fn("summary"))
    assert '"recurred"' in seg


# ── The fetch must stay narrow (2026-09-21) ──────────────────────────────

def _grade_select():
    seg = ast.get_source_segment(SRC, _fn("grade_recurrences"))
    m = re.search(r'"""(SELECT pr_number, outcome FROM brain_pr_outcomes.*?)"""',
                  seg, re.S)
    assert m, "grade_recurrences' SELECT not found"
    return m.group(1)


def test_grader_fetches_only_l5_drafts():
    """★ The first version fetched every brain_authored row — 150 GitHub calls
    per run. It took 17s (the edge 503'd at ~15s), nearly every fetch failed,
    it graded nothing, and it spent the monitor's own GitHub budget. Only
    brain_backlog_admin writes a `**Finding:**` line, and it titles every PR
    `[brain-l5 draft]`, so nothing else can be graded or act as a re-target."""
    assert "[brain-l5" in _grade_select(), (
        "grade_recurrences no longer narrows to [brain-l5 draft] PRs — it will "
        "go back to one GitHub call per brain_authored row")


def test_the_like_pattern_is_psycopg2_safe():
    """The query also takes a %s parameter, so a literal % must be %%.
    Verified on Postgres 18.6 via psycopg2: a single % raises IndexError,
    grade_recurrences catches it, returns ok:False — and grades nothing,
    silently. That is the inert state this fix exists to end."""
    sel = _grade_select()
    assert "'[brain-l5%%'" in sel, "LIKE pattern is not %%-escaped"


# ── No DB connection may be held across GitHub I/O (2026-09-21) ──────────
#
# main.get_pg_connection() force-closes any checkout held > ~60s. reclassify
# held one while fetching 150 PRs; production logged
#   FORCED RECLAIM: Connection ... held 61s ... Checkout stack: ... reclassify
# and the UPDATE then ran on the dead connection -> HTTP 500. These tests pin
# the invariant BEHAVIOURALLY: a fake pool counts open checkouts and the fake
# GitHub call fails if any is open when it runs.

class _Pool:
    def __init__(self):
        self.open = 0
        self.checkouts = 0
        self.writes = []

    def get(self):
        pool = self
        pool.open += 1
        pool.checkouts += 1

        class _Cur:
            rowcount = 1
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                if sql.lstrip().upper().startswith("UPDATE"):
                    pool.writes.append(params)
            def fetchall(self):
                return [(4202, "unknown"), (4567, "unknown")]

        class _Conn:
            def cursor(self): return _Cur()
            def commit(self): pass
            def close(self): pool.open -= 1
        return _Conn()


def _load_grader(pool, gh_bodies):
    names = ("_NoDatabase", "_db_read", "_db_write", "extract_finding",
             "recurrence_plan", "grade_recurrences")
    pieces = []
    for node in ast.parse(SRC).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            pieces.append(ast.get_source_segment(SRC, node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in ("_FINDING_LINE_RE", "_GRADE_MAX")
                for t in node.targets):
            pieces.append(ast.get_source_segment(SRC, node))

    def _gh_api(path):
        assert pool.open == 0, (
            f"{pool.open} DB connection(s) checked out during a GitHub call — "
            "the pool reaper force-closes connections held > ~60s")
        return gh_bodies[int(path.rsplit("/", 1)[1])]

    import logging
    ns = {"re": re, "_get_db": pool.get, "_gh_api": _gh_api,
          "_GITHUB_REPO": "o/r", "logger": logging.getLogger("t")}
    exec(compile("\n\n".join(pieces), MOD, "exec"), ns)
    for n in names:
        assert n in ns, f"AST extraction missed {n}"
    return ns["grade_recurrences"]


_BODIES = {
    4202: {"body": "**Finding:** `cf_cache_rate_low`\n",
           "created_at": "2026-09-08T04:31:29Z", "merged_at": "2026-09-08T04:54:26Z"},
    4567: {"body": "**Finding:** `cf_cache_rate_low`\n",
           "created_at": "2026-09-13T12:34:07Z", "merged_at": "2026-09-13T21:35:52Z"},
}


def test_no_connection_is_held_while_github_is_called():
    pool = _Pool()
    out = _load_grader(pool, _BODIES)(apply=True)
    assert out["ok"], out
    assert pool.open == 0, "a connection was never released"


def test_the_write_uses_a_fresh_checkout_after_the_fetches():
    """The measured failure: the write path reused the connection taken
    before the fetches. It must check out a new one afterwards."""
    pool = _Pool()
    out = _load_grader(pool, _BODIES)(apply=True)
    assert out["applied"] == 1 and len(pool.writes) == 1, out
    assert pool.checkouts == 2, (
        f"{pool.checkouts} checkout(s) — expected one to read and a separate "
        "one to write, so no connection spans the GitHub fetches")


def test_reclassify_never_checks_out_directly():
    """reclassify is a Flask route, so it is pinned structurally: it must go
    through _db_read/_db_write, and write only after it has fetched."""
    seg = ast.get_source_segment(SRC, _fn("reclassify"))
    assert "_get_db()" not in seg, "reclassify checks out a connection directly again"
    assert seg.index("_db_write(") > seg.index("_gh_api("), (
        "reclassify writes before it fetches — the checkout would span the fetch")
