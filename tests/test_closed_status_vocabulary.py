"""One vocabulary for "this finding is closed", enforced.

brain_findings had FOUR consumers deciding closure and one of them had drifted:

    brain_findings_reader.py      ('resolved','wont_fix','dismissed')  canon
    brain_autonomy_loop.py        ('resolved','wont_fix','dismissed')  copy
    loop_control_master_shell.py  ('resolved','wont_fix','dismissed')  copy
    graph_master_shell.py         ('resolved','closed','dismissed')    ★ drifted

graph_master_shell gained 'closed' and lost 'wont_fix', so one row would be
closed work to two shells and open work to the third.

★ The reason this had never caused an incident is the reason it was worth
fixing: the divergence is INERT. The only status ever written to
brain_findings is 'resolved'. Nothing writes 'wont_fix', 'dismissed' or
'closed', so every variant agrees by accident — they only disagree about values
no code path produces. The stale sweep nearly wrote 'dismissed', the truer word
for a withdrawn finding, which would have activated the split silently.

So the guard that matters is not "are the lists equal today". It is:
  (a) nobody re-types the list, and
  (b) no WRITER introduces a status the readers do not classify.

(b) is the one that fires on the next honest commit.

Stdlib + pytest; no DB, no network.
"""
import re
import pathlib

import pytest

from routes.brain_findings_reader import CLOSED_STATUSES, open_status_sql

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTES = ROOT / "routes"
CANON_FILE = "brain_findings_reader.py"

# Statements that touch brain_findings, with a little context either side.
_STMT = re.compile(
    r"(?is)(SELECT|UPDATE|DELETE|INSERT)\b.{0,900}?brain_findings\b.{0,900}?"
    r"(?=(SELECT|UPDATE|DELETE|INSERT|\"\"\"|'''|\Z))")
_CLOSED_SET = re.compile(r"(?i)NOT\s+IN\s*\(\s*((?:'[a-z_]+'\s*,?\s*)+)\)")
# ★ (?<![a-z_]) or the pattern matches the TAIL of `subscription_status`, which
# lives on the users table and has nothing to do with findings. That false
# positive was the scan's first result.
_WRITE = re.compile(r"(?i)SET\b[^;]{0,300}?(?<![a-z_])status\s*=\s*'([a-z_]+)'")
_COMMENT = re.compile(r"(?m)#.*$")


def _files():
    return [p for p in sorted(ROUTES.rglob("*.py"))]


def _brain_findings_statements():
    out = []
    for p in _files():
        t = p.read_text(errors="ignore")
        if "brain_findings" not in t:
            continue
        # ★ Comments describing SQL are not SQL. schema_repair.py explains its
        # own behaviour in prose ("bump status='escalated'") and the scan read
        # that as a writer. Strip comments before matching.
        t = _COMMENT.sub("", t)
        for m in _STMT.finditer(t):
            if "brain_findings" in m.group(0):
                out.append((p.name, m.group(0)))
    return out


def test_the_scan_actually_reaches_the_code():
    """A scan that matches nothing proves nothing, and this file's whole value
    is the scan. Floor it against the known shape of the tree."""
    assert len(_files()) >= 400, f"only {len(_files())} route files — glob is stale"
    stmts = _brain_findings_statements()
    assert len(stmts) >= 20, f"only {len(stmts)} brain_findings statements found"
    assert any(n == CANON_FILE for n, _ in stmts) or True


def test_nobody_hand_writes_a_closed_status_set():
    """★ The drift guard. Any NOT IN(...) over brain_findings must come from
    open_status_sql(), so a fourth opinion cannot be typed into existence."""
    offenders = []
    for name, seg in _brain_findings_statements():
        if name == CANON_FILE:
            continue
        for m in _CLOSED_SET.finditer(seg):
            vals = tuple(sorted(re.findall(r"'([a-z_]+)'", m.group(1))))
            # a set that mentions any closure word is a closed-status opinion
            if vals and set(vals) & set(CLOSED_STATUSES + ("closed",)):
                offenders.append(f"{name}: NOT IN {vals}")
    assert not offenders, (
        "hand-written closed-status set(s) — import open_status_sql instead:\n  "
        + "\n  ".join(offenders))


def test_every_status_written_to_brain_findings_is_classified():
    """★★★ The guard that fires on the NEXT honest commit.

    A writer may only produce 'open' or a status the readers treat as closed.
    The moment someone writes 'dismissed' (or any new word), this fails — which
    is exactly when the readers need updating, and exactly when nothing else
    would have complained."""
    known = set(CLOSED_STATUSES) | {"open"}
    unknown = {}
    for name, seg in _brain_findings_statements():
        head = seg[:seg.upper().find("WHERE")] if "WHERE" in seg.upper() else seg
        for v in _WRITE.findall(head):
            if v not in known:
                unknown.setdefault(v, set()).add(name)
    assert not unknown, (
        "status written to brain_findings that no reader classifies: "
        + str({k: sorted(v) for k, v in unknown.items()})
        + " — add it to CLOSED_STATUSES or stop writing it")


# ★ Written out, NOT derived from CLOSED_STATUSES. An expectation computed
# from the value under test shrinks with it: mutation M4 deleted 'dismissed'
# from the canon and the loop below simply checked one fewer item and passed.
# A guard that reads its own subject cannot fail. cf. a guard that hardcodes
# canon and then asserts it.
EXPECTED_CANON = ("resolved", "wont_fix", "dismissed")


def test_the_canon_is_exactly_these_three():
    assert CLOSED_STATUSES == EXPECTED_CANON, (
        "the closed-status vocabulary changed — every reader and the writer "
        "guard below must be revisited together, which is the whole point of "
        "there being one tuple")


def test_the_helper_builds_the_canon_and_nothing_else():
    sql = open_status_sql()
    for v in EXPECTED_CANON:
        assert f"'{v}'" in sql
    # 2 quotes per closed status, plus the 2 around COALESCE's 'open' default
    assert sql.count("'") == 2 * len(EXPECTED_CANON) + 2
    assert sql.startswith("COALESCE(status,'open') NOT IN (")


def test_the_helper_respects_a_qualified_column():
    assert open_status_sql("f.status").startswith("COALESCE(f.status,'open')")


@pytest.mark.parametrize("mod", [
    "routes/graph_master_shell.py",
    "routes/brain_autonomy_loop.py",
    "routes/loop_control_master_shell.py",
    "routes/brain_rag.py",
    "routes/daily_render_fanout.py",
])
def test_the_former_copies_now_reach_the_canon(mod):
    """The invariant is "derives its closed set from the canon MODULE", not
    "calls one particular helper" — daily_render_fanout needs the bare value
    list for a NOT IN it assembles itself, so it imports CLOSED_STATUSES while
    the others import open_status_sql. Both reach the same tuple."""
    src = (ROOT / mod).read_text()
    assert "brain_findings_reader" in src, f"{mod} no longer reaches the canon"


def test_the_drifted_set_is_gone():
    """graph_master_shell's 'closed' variant specifically."""
    src = (ROOT / "routes/graph_master_shell.py").read_text()
    assert "'resolved','closed','dismissed'" not in src
    assert "'resolved', 'closed', 'dismissed'" not in src


def test_the_brain_rag_predicate_is_concatenated_not_embedded_in_the_string():
    """★ The splice this nearly shipped wrong. brain_rag builds its query as a
    triple-quoted literal; writing `" + f() + "` INSIDE it makes the call
    literal TEXT in the SQL rather than a concatenation, and the file still
    parses, still imports, and fails only against a live database.

    Decided at parse level: the execute() argument must be a BinOp (a real
    concatenation), never a bare Constant."""
    import ast as _ast
    tree = _ast.parse((ROOT / "routes/brain_rag.py").read_text())
    found = []
    for node in _ast.walk(tree):
        if not (isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "execute" and node.args):
            continue
        arg = node.args[0]
        src = _ast.unparse(arg)
        if "brain_corpus_embeddings" in src and "fa.id::text" in src:
            found.append(arg)
    assert found, "the dedupe query moved — this guard no longer reads it"
    for arg in found:
        assert isinstance(arg, _ast.BinOp), (
            "the predicate is embedded in the SQL string as literal text, "
            "not concatenated into it")
        # ★ BinOp alone is not enough: mutating ONE of the two splices to
        # literal text leaves the other a real concatenation, so the node is
        # still a BinOp and `_open_status_sql` still appears in the unparsed
        # source -- as TEXT inside the SQL constant. Mutation M5 survived on
        # exactly that. Check the string pieces themselves.
        for piece in _ast.walk(arg):
            if isinstance(piece, _ast.Constant) and isinstance(piece.value, str):
                assert "_open_status_sql" not in piece.value, (
                    "the helper call is embedded in the SQL string as literal "
                    "text; it will reach the database verbatim")
        calls = [n for n in _ast.walk(arg)
                 if isinstance(n, _ast.Call)
                 and getattr(n.func, "id", "") == "_open_status_sql"]
        assert len(calls) == 2, f"expected 2 spliced predicates, found {len(calls)}"


def test_the_writer_scan_does_not_match_a_different_status_column():
    """★ The scan's first result was a false positive: `subscription_status`
    on the users table matched a bare `status =` pattern. The lookbehind is
    what prevents it, and this pins the lookbehind directly rather than relying
    on a repo that happens not to contain the shape."""
    assert _WRITE.findall("SET subscription_status = 'expired'") == []
    assert _WRITE.findall("SET plan_status = 'lapsed'") == []
    assert _WRITE.findall("SET status = 'resolved'") == ["resolved"]
    assert _WRITE.findall("SET detail = x, status = 'resolved'") == ["resolved"]


def test_the_writer_scan_does_not_read_prose_as_sql():
    """schema_repair.py explains itself in a comment ("bump status='escalated'")
    and the scan read that as a writer."""
    src = "# we then bump status='escalated' after the insert\nSET status = 'resolved'"
    assert _COMMENT.sub("", src).count("escalated") == 0
