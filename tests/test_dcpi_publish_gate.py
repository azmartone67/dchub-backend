"""r-publish-gate (2026-08-08) — one owner for the `published` flag.

THE BUG: two automated repairs fought over `published` on market_power_scores
and the loser was a publicly-served row.

  * routes/dcpi.py's r-twin-unpublish runs on every DCPI recompute and sets
    published=false on a REDUNDANT_TWIN_SLUGS row whose canonical is live.
  * dchub_self_heal.py::fix_enforce_publish_gate runs on every self-heal cycle
    (PATTERNS dispatches "credibility_gate_tick" unconditionally) and decided
    `published` from quality_score.

The gate had TWO publish statements — one for curated rows, one for lite-pro
rows — and r-twin-unpublish's exclusion was hand-added to the first only.

The live table fingerprinted that exactly. Measured 2026-08-08 15:39 UTC, of
the seven twins retired on 07-28:

    six with tier_required='free'      -> published=false, and
                                          dcpi_daily_snapshots shows them
                                          leaving the index on 07-28 and
                                          never returning
    one with tier_required='lite-pro'  -> `washington`, published=true,
                                          computed_at frozen 07-19 21:50,
                                          method_version NULL, quality 100

Same fix, same day, seven rows. The only variable that predicted the outcome
was which of the two statements owned the row. `washington` flapped against
the recompute — in the published snapshot on 07-29, out 07-30 to 08-05, back
in on 08-06 and 08-07, out again 08-08 — and while it was in, /api/v1/markets
served both `dc` (Washington, DC, fresh) and `washington` (Washington, 19 days
stale): a duplicate market in the ranking, a 19-day-old row in an index whose
other 323 rows were minutes old, and a published score with no method_version,
which /api/v1/dcpi/methodology says does not exist.

WHY IT DRIFTED: the same reason as every other entry in this family. The rule
was correct and it was written twice — once. util/iso_taxonomy.py,
util/status_taxonomy.py, util/deals.py, util/capacity_pipeline.py and
util/dcpi_score_row.py are all the same lesson.

NOT COVERED BY r-provenance-writer, by design: that census keys on statements
that BIND a score (`constraint_score=%s`, or an INSERT naming it). A statement
that only flips `published` binds no score, so this mechanism sat outside its
scope by construction rather than by oversight — see its own scope note. This
file is that gap closed.

WHAT IS PINNED HERE:
  1. CENSUS — no UPDATE anywhere in the repo may set `published` to anything
     other than false without binding the shared fence. This is the test that
     fails if a THIRD publish statement appears, which is the whole defect.
  2. BEHAVIORAL — the shipped gate is pulled out of the source and executed
     against a stub cursor, and the parameters it binds are inspected, so the
     twin/canonical pairs are asserted as VALUES reaching the driver.
  3. SHAPE — the three rules the fence carries, each asserted precisely enough
     that inverting it or dropping a rule fails.

SCOPE NOTE, deliberately: an INSERT that creates a row with published=TRUE is
out of scope. util/dcpi_score_row.upsert_scored_market takes publish= at every
call site and its rows are fresh, stamped and drawn from MARKETS (which
excludes every twin), and a per-row SQL predicate cannot be evaluated against
a VALUES list anyway. Should such a row ever be wrong, the gate below corrects
it on the next self-heal cycle — the census keys on UPDATE for that reason,
not by oversight.
"""
import ast
import os
import re

import pytest

from util.dcpi_score_row import (MAY_PUBLISH, PUBLISH_STALE_AFTER,
                                 may_publish_params)
from util.market_aliases import DCPI_METRO_ALIASES, REDUNDANT_TWIN_SLUGS


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_SKIP_DIRS = {".git", "tests", "node_modules", "__pycache__", ".venv", "venv",
              ".pytest_cache", "site-packages", ".claude"}


def _candidate_files():
    """Every repo .py that mentions the table. Text-filtered first so an
    unrelated syntax error elsewhere cannot turn this guard into a blind
    pass."""
    root = _repo_root()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "market_power_scores" in text:
                yield path, text


# ---------------------------------------------------------------------------
# Rendering SQL out of the AST
# ---------------------------------------------------------------------------

def _render(node):
    """Render a string-valued expression.

    Constants come through literally; anything else — an f-string hole, a
    concatenated Name — is rendered as its SOURCE text. That is what lets the
    census see a fence that reaches the SQL through a shared constant: the
    gate builds its statement as "..." + MAY_PUBLISH + ")", and the name is
    the only trace of the predicate in that file. Same technique as
    tests/test_dcpi_provenance_stamp.py, extended to `+` concatenation because
    this predicate cannot be interpolated — it carries %s placeholders, and an
    f-string around one is how a literal % reaches psycopg2.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        return "".join(_render(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return " " + ast.unparse(node.value) + " "
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _render(node.left) + _render(node.right)
    return " " + ast.unparse(node) + " "


def _sql_expressions(tree):
    """(lineno, sql) for every whole string expression in a module.

    Operands of a `+` chain and the parts of an f-string are skipped as roots
    so a concatenated statement is yielded once, entire, rather than once per
    fragment — a fragment-at-a-time census would see "SET published = (" with
    no fence attached and fail on the correct code.
    """
    inner = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            inner.add(id(node.left))
            inner.add(id(node.right))
        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                inner.add(id(v))
    out = []
    for node in ast.walk(tree):
        if id(node) in inner:
            continue
        is_str_const = (isinstance(node, ast.Constant)
                        and isinstance(node.value, str))
        is_concat = (isinstance(node, ast.BinOp)
                     and isinstance(node.op, ast.Add))
        if is_str_const or is_concat or isinstance(node, ast.JoinedStr):
            text = _render(node)
            if text.strip():
                out.append((getattr(node, "lineno", 0), text))
    return out


def _set_clause(sql):
    """The SET clause of an UPDATE, or "" if this is not one.

    Paren-aware. The fence itself contains a WHERE inside its NOT EXISTS
    subquery, so a naive split on the first WHERE would truncate the very text
    this census is looking for and pass a statement it should reject.
    """
    flat = " ".join(sql.split())
    if not re.search(r"update\s+market_power_scores\b", flat, re.I):
        return ""
    depth, set_at = 0, None
    for tok in re.finditer(r"\(|\)|\bset\b|\bwhere\b", flat, re.I):
        word = tok.group().lower()
        if word == "(":
            depth += 1
        elif word == ")":
            depth -= 1
        elif depth == 0 and word == "set" and set_at is None:
            set_at = tok.end()
        elif depth == 0 and word == "where" and set_at is not None:
            return flat[set_at:tok.start()]
    return flat[set_at:] if set_at is not None else ""


def _published_assignments():
    """(path, lineno, assigned_expression, fence_aliases) for every UPDATE in
    the repo that assigns `published`."""
    found = []
    for path, text in _candidate_files():
        tree = ast.parse(text)
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                    node.module or "").endswith("dcpi_score_row"):
                for alias in node.names:
                    if alias.name == "MAY_PUBLISH":
                        aliases.add(alias.asname or alias.name)
        for lineno, sql in _sql_expressions(tree):
            clause = _set_clause(sql)
            if not clause:
                continue
            m = re.search(r"\bpublished\s*=\s*(.+)", clause, re.I | re.S)
            if m:
                found.append((path, lineno, m.group(1), aliases))
    return found


# ---------------------------------------------------------------------------
# 1. CENSUS
# ---------------------------------------------------------------------------

def test_every_publishing_update_binds_the_shared_fence():
    """★ The guard. An UPDATE may set `published` to false freely — retiring a
    row is always safe — but setting it to anything else must go through the
    shared fence.

    A third publish statement fails here, which is the entire point: two
    statements and one hand-added rule is what published a 19-day-stale
    duplicate for eleven days.
    """
    assignments = _published_assignments()
    assert assignments, "census found no publish statements — it has gone blind"

    unfenced = []
    for path, lineno, value, aliases in assignments:
        if value.strip().lower().startswith("false"):
            continue                      # unpublishing is always allowed
        fenced = MAY_PUBLISH in value or any(a in value for a in aliases)
        if not fenced:
            unfenced.append(f"{os.path.relpath(path, _repo_root())}:{lineno}")

    assert not unfenced, (
        "UPDATE publishes a row without the shared fence: " + ", ".join(
            sorted(unfenced))
    )


def test_the_census_still_sees_the_two_known_statements():
    """Anti-blindness. Both the gate's assignment and r-twin-unpublish's
    retirement must be visible to the machinery above — if a refactor makes
    either invisible, the census passes while guarding nothing.
    """
    seen = {os.path.relpath(p, _repo_root()) for p, _, _, _ in
            _published_assignments()}
    assert "dchub_self_heal.py" in seen, (
        "the publish gate is no longer visible to the census")
    assert os.path.join("routes", "dcpi.py") in seen, (
        "r-twin-unpublish's retirement is no longer visible to the census")


def test_the_gate_has_exactly_one_publish_statement():
    """The defect was two statements sharing one rule. One rule, one
    statement — a second branch is somewhere for the next rule to be
    forgotten."""
    heal = [a for a in _published_assignments()
            if os.path.basename(a[0]) == "dchub_self_heal.py"]
    assert len(heal) == 1, (
        f"dchub_self_heal.py has {len(heal)} statements assigning `published` "
        "— it had two, and the twin rule was added to one of them"
    )


def test_the_fence_has_exactly_one_definition():
    """If a caller retypes the predicate instead of importing it, this whole
    file starts asserting on a copy — the original failure mode, one level
    up. Same guard as the lite fence's."""
    hits = []
    for path, text in _candidate_files():
        if path.endswith(os.path.join("util", "dcpi_score_row.py")):
            continue
        if "percentile_disc" in text or "AS twin(slug, canonical)" in text:
            hits.append(os.path.relpath(path, _repo_root()))
    assert not hits, f"publish fence retyped in: {hits}"


def test_the_hand_rolled_twin_exclusion_is_gone():
    """The old `market_slug <> ALL(%s)` exclusion was the second mechanism.
    Leaving it beside the fence would mean two places to update again."""
    heal = open(os.path.join(_repo_root(), "dchub_self_heal.py"),
                encoding="utf-8").read()
    assert "market_slug <> ALL" not in heal, (
        "the hand-rolled twin exclusion is back alongside the shared fence")
    assert "'cheyenne-wy'" not in heal and '"cheyenne-wy"' not in heal, (
        "self-heal hand-copied the twin list instead of importing it")


# ---------------------------------------------------------------------------
# 2. SHAPE — the three rules
# ---------------------------------------------------------------------------

def test_the_fence_refuses_a_twin_whose_canonical_is_published():
    """Rule 1, the load-bearing one. It is the only rule that still holds if
    something re-scores a retired twin — recompute_stale_markets selects on
    age alone, with no published filter and no twin filter, and deliberately
    does not require the slug to be in MARKETS.

    Asserted on the clause's shape, because each of these is a way to write a
    predicate that looks right and fences nothing: EXISTS instead of NOT
    EXISTS publishes exactly the rows it should hide; dropping the
    canonical-published requirement retires a twin whose target is itself
    unpublished, orphaning a live URL.
    """
    flat = " ".join(MAY_PUBLISH.split())
    assert "NOT EXISTS" in flat, (
        "the twin clause is not a NOT EXISTS — an EXISTS here inverts the "
        "rule and publishes exactly the rows it should hide")
    assert "twin.slug = market_power_scores.market_slug" in flat, (
        "the twin clause is not correlated to the row being gated")
    assert "canon.market_slug = twin.canonical" in flat, (
        "the twin clause no longer looks up the canonical target")
    assert "COALESCE(canon.published, true) = true" in flat, (
        "the fence retires a twin without checking its canonical is "
        "published — that orphans the market it redirects to")


def test_the_fence_refuses_a_row_far_behind_the_index():
    """Rule 2. Every quality component is satisfied just as well by a stale
    row as a live one; `washington` scored 100 while 19 days frozen.

    Pinned as RELATIVE to the index median. Against NOW() a stalled pipeline
    would blank the whole index; against MAX a single future-dated row would
    drag every other row past the threshold and do the same.
    """
    flat = " ".join(MAY_PUBLISH.split())
    assert "percentile_disc(0.5) WITHIN GROUP (ORDER BY computed_at)" in flat, (
        "the freshness rule no longer measures against the index median — "
        "MAX lets one future timestamp unpublish the entire index, and NOW() "
        "lets a stalled pipeline do it")
    assert "NOW()" not in flat, (
        "the freshness rule measures against wall-clock time, so a stalled "
        "pipeline unpublishes every market at once")
    assert "INTERVAL '" + PUBLISH_STALE_AFTER + "'" in flat
    assert "market_power_scores.computed_at IS NOT NULL" in flat, (
        "a NULL computed_at would make the comparison NULL, not false")


def test_the_fence_refuses_a_row_with_no_method_version():
    """Rule 3. /api/v1/dcpi/methodology commits to a stamp on every score
    row; r-provenance-writer made that true of the write paths, and this is
    the same commitment at the serving decision."""
    flat = " ".join(MAY_PUBLISH.split())
    assert "market_power_scores.method_version IS NOT NULL" in flat


def test_the_fence_is_placeholder_safe():
    """It carries %s for psycopg2 and is assembled with .replace(), never an
    f-string. A stray Python % here is how a literal % reaches the driver."""
    assert MAY_PUBLISH.count("%s") == 2
    assert "{" not in MAY_PUBLISH and "}" not in MAY_PUBLISH, (
        "an unsubstituted template hole survived into the predicate")
    # one array per placeholder, zipped by unnest
    assert len(may_publish_params()) == MAY_PUBLISH.count("%s")


# ---------------------------------------------------------------------------
# 3. PARAMETERS — the twin pairs as values
# ---------------------------------------------------------------------------

def test_params_carry_every_twin_paired_with_its_canonical():
    """unnest zips the two arrays positionally, so a pair that drifts out of
    alignment fences the wrong market — silently, since both arrays stay the
    right length."""
    slugs, canons = may_publish_params()
    assert len(slugs) == len(canons), "twin arrays are not parallel"
    assert slugs == sorted(slugs), "unnest zips positionally; keep it ordered"

    pairs = dict(zip(slugs, canons))
    expected = {t: DCPI_METRO_ALIASES[t] for t in REDUNDANT_TWIN_SLUGS
                if DCPI_METRO_ALIASES.get(t)}
    assert pairs == expected, "bound twin->canonical pairs do not match the map"

    # the row this was built for, spelled out
    assert pairs["washington"] == "dc"


def test_params_are_read_from_the_shared_map_not_a_copy():
    """A hand-listed set here would pass every other test in this file while
    silently missing the next twin added to util.market_aliases."""
    import util.market_aliases as aliases

    original = aliases.REDUNDANT_TWIN_SLUGS
    original_map = dict(aliases.DCPI_METRO_ALIASES)
    try:
        aliases.REDUNDANT_TWIN_SLUGS = frozenset(original | {"zzz-test-twin"})
        aliases.DCPI_METRO_ALIASES["zzz-test-twin"] = "dallas"
        slugs, canons = may_publish_params()
        assert "zzz-test-twin" in slugs, (
            "may_publish_params does not read util.market_aliases — it is a "
            "copy, and a new twin will never reach the fence")
        assert canons[slugs.index("zzz-test-twin")] == "dallas"
    finally:
        aliases.REDUNDANT_TWIN_SLUGS = original
        aliases.DCPI_METRO_ALIASES.clear()
        aliases.DCPI_METRO_ALIASES.update(original_map)


# ---------------------------------------------------------------------------
# 4. BEHAVIORAL — execute the shipped gate
# ---------------------------------------------------------------------------

class _FakeCursor:
    """Records what would reach psycopg2."""

    def __init__(self):
        self.calls = []
        self.rowcount = 3
        self._last = ""

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = " ".join(sql.split()).lower()

    def fetchall(self):
        return [("washington",)] if "select market_slug" in self._last else []

    def fetchone(self):
        return (323, 7, 91.4, 300)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, *a, **k):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_shipped_gate():
    """Pull fix_enforce_publish_gate out of the source and run it against
    stubs. Extracted rather than imported: nothing under tests/ may pay for a
    module that reads DATABASE_URL and registers healing patterns at import,
    and an extracted function cannot drift from the shipped one."""
    src = open(os.path.join(_repo_root(), "dchub_self_heal.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef)
              and n.name == "fix_enforce_publish_gate")
    module = ast.Module(body=[fn], type_ignores=[])
    cur = _FakeCursor()
    ns = {"DATABASE_URL": "postgresql://stub",
          "_conn": lambda: _FakeConn(cur)}
    exec(compile(ast.fix_missing_locations(module), "dchub_self_heal", "exec"),
         ns)
    ok, note = ns["fix_enforce_publish_gate"]()
    return ok, note, cur


def _publish_call(cur):
    """The one recorded statement that assigns `published`."""
    hits = [(sql, params) for sql, params in cur.calls
            if re.search(r"\bpublished\s*=\s*(?!false)",
                         _set_clause(sql), re.I)]
    assert len(hits) == 1, f"expected one publish statement, got {len(hits)}"
    return hits[0]


def test_the_shipped_gate_cannot_publish_a_twin_of_a_published_canonical():
    """★ The behavioral guard, asserted on what reaches the driver.

    The statement the shipped gate issues must carry the fence, and must bind
    every twin alongside the canonical it defers to — `washington` -> `dc`
    among them. Asserted as bound VALUES rather than as a substring, so a
    statement that merely mentions the fence while binding something else
    still fails.
    """
    ok, note, cur = _run_shipped_gate()
    assert ok, note

    sql, params = _publish_call(cur)
    assert MAY_PUBLISH in sql, (
        "the gate's publish statement does not carry the shared fence")

    slugs, canons = params
    assert dict(zip(slugs, canons))["washington"] == "dc"
    for twin in REDUNDANT_TWIN_SLUGS:
        canonical = DCPI_METRO_ALIASES.get(twin)
        if canonical:
            assert twin in slugs, f"{twin} is not fenced by the shipped gate"
            assert canons[slugs.index(twin)] == canonical


def test_the_shipped_gate_keeps_the_quality_threshold():
    """The fence is an AND, never a replacement — a row still has to clear
    quality 60 to be served."""
    _, _, cur = _run_shipped_gate()
    sql, _ = _publish_call(cur)
    assert "quality_score >= 60" in sql


def test_the_shipped_gate_reports_what_it_refused():
    """This job runs unattended every cycle and decides what the public index
    contains. A rule that quietly starts refusing rows is the green-but-blind
    failure mode; the names in the heal note are what make it visible."""
    ok, note, cur = _run_shipped_gate()
    assert ok
    assert "refused" in note.lower(), f"gate note hides its refusals: {note}"
    assert "washington" in note

    refusal_queries = [sql for sql, _ in cur.calls
                       if "select market_slug" in " ".join(sql.split()).lower()]
    assert refusal_queries, "the gate never asks what it refused"
    assert all(MAY_PUBLISH in sql for sql in refusal_queries), (
        "the refusal report is built from a second copy of the rule, so it "
        "can disagree with the action it claims to describe")


def test_the_gate_is_still_dispatched_every_cycle():
    """A fence in a job nothing runs is a dead scoreboard. The gate is the
    thing that makes retirement stick, so it has to keep running."""
    heal = open(os.path.join(_repo_root(), "dchub_self_heal.py"),
                encoding="utf-8").read()
    assert '"fix": "enforce_publish_gate"' in heal
    assert 'FIXES["enforce_publish_gate"] = fix_enforce_publish_gate' in heal


# ---------------------------------------------------------------------------
# 5. The public description must match the gate
# ---------------------------------------------------------------------------

def test_the_public_gate_policy_string_is_not_hand_copied():
    """/api/v1/dcpi/quality calls itself "public proof-of-filter" and prints
    the policy. It said "publish if quality_score >= 60 OR tier != lite-pro"
    — a description of the two-branch shape, and never a mention of the rules
    that actually keep a stale duplicate out."""
    main = open(os.path.join(_repo_root(), "main.py"), encoding="utf-8").read()
    assert "publish if quality_score >= 60 OR tier != lite-pro" not in main, (
        "the public gate description still describes the two-branch gate")
    assert "PUBLISH_STALE_AFTER" in main, (
        "the staleness window is retyped in the public description instead of "
        "bound from the constant the gate uses")


@pytest.mark.parametrize("twin", sorted(REDUNDANT_TWIN_SLUGS))
def test_no_twin_is_left_unfenced(twin):
    """Every member of the set reaches the fence. `portland` is in the set as
    belt-and-braces — r-portland-canon renamed the Maine row to portland-me
    and no bare-'portland' row exists — and it still has to be bound, since
    the whole point of the set is that a resurrected row is junk by
    definition."""
    slugs, _ = may_publish_params()
    assert twin in slugs, f"{twin} is in REDUNDANT_TWIN_SLUGS but never fenced"
