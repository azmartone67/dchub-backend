"""scripts/check_dangling_pointers.py — the guard's own guard (2026-09-08).

A pointer at a suppressed row is a DEAD pointer: `_canonical_twin_row`,
`_twin_pointer_url` and the sitemap's `_noncanon_slugs` all require
`COALESCE(is_duplicate,0)=0` on the target, so the page falls back to a
self-canonical while the alternate is excluded from every `duplicate_of_id IS
NULL` count and the target from every `is_duplicate = 0` one — the facility is
counted zero times and consolidated not at all.

Measured 2026-09-08: 1,350 such rows existed, all written by
`brand+site_token/v2` against rows suppressed afterwards. They were repaired
(67 repointed onward, 1,283 cleared). Nothing prevents the mechanism recurring,
which is what the checker is for and what this pins.

★★★ THE CHECKER'S PASS STATE IS ZERO, WHICH IS ALSO WHAT A BROKEN QUERY
    RETURNS. That is the whole reason the MIN_* floors exist, and the reason
    this file asserts them as LITERALS: a scan whose clean answer and whose
    blind answer are the same number is a green light wired to nothing.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "scripts", "check_dangling_pointers.py")
SRC = open(PATH, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _const(name):
    for n in ast.walk(TREE):
        if (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == name):
            return n.value.value
    raise AssertionError(f"{name} is gone — the floor it names is not enforced")


def _dict_keys(name):
    for n in ast.walk(TREE):
        if (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == name):
            return [k.value for k in n.value.keys]
    raise AssertionError(f"{name} is gone")


def test_the_budget_is_zero_with_no_headroom():
    """★ Before the repair this query returned 1,350. There is no acceptable
    number of dead pointers — one is a facility consolidated nowhere and
    counted nowhere — so any headroom is headroom for the defect itself.

    Pinned as a LITERAL so raising it is a code review, not a side effect."""
    budget = None
    for node in ast.walk(TREE):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "add_argument"
                and node.args
                and getattr(node.args[0], "value", None) == "--max-rows"):
            budget = next(k.value.value for k in node.keywords if k.arg == "default")
    assert budget == 0, f"--max-rows default is {budget}, not 0"


def test_the_floors_are_pinned_and_far_from_zero():
    """Measured live 2026-09-08: 2,020 pointers, 7,683 suppressed rows, 466
    twins. Each floor sits below the live value (so ordinary movement does not
    trip it) and far above zero (so a collapsed denominator does)."""
    assert _const("MIN_POINTERS") == 1000
    assert _const("MIN_SUPPRESSED") == 4000
    assert _const("MIN_TWINS") == 200
    for name, live in (("MIN_POINTERS", 2020), ("MIN_SUPPRESSED", 7683),
                       ("MIN_TWINS", 466)):
        floor = _const(name)
        assert 0 < floor < live, (name, floor, live)


def test_every_dangling_shape_is_covered():
    """★ The repair found ONE shape. A guard pinned only to the shape that
    happened to bite goes green while a pointer at a DELETED row does the
    identical damage."""
    shapes = _dict_keys("_DANGLING")
    for expected in ("pointer_to_suppressed_row", "pointer_to_missing_row",
                     "pointer_to_row_without_a_slug",
                     "legacy_twin_to_suppressed_keeper"):
        assert expected in shapes, f"{expected} is no longer checked"
    assert len(shapes) >= 4


def test_the_total_sums_every_shape_not_just_one():
    """A total that reads one key would report 0 while another shape ran away."""
    assert "sum(m[k] for k in _DANGLING)" in SRC, (
        "the total no longer sums over _DANGLING — a shape can now grow "
        "without moving the number the budget is compared against")


def test_could_not_measure_is_never_exit_zero():
    """★ The failure modes must not share an exit code with success. Three
    paths return 2: no DSN, a query that raised, and a collapsed floor."""
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    returns = {n.value.value for n in ast.walk(fn)
               if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)}
    assert 2 in returns, "main() never returns 2 — 'could not measure' is silent"
    assert SRC.count("return 2") >= 3, (
        "fewer than three 'could not measure' exits: no-DSN, query-raised and "
        "floor-collapsed must all be 2, never 0")
    # and the floor check must gate the RESULT, not merely be reported
    assert "if blind:" in SRC and "return 2" in SRC.split("if blind:")[1][:400], (
        "the floor check does not return 2 — a blind scan would report clean")


def test_the_checker_is_executable_and_self_contained():
    assert os.access(PATH, os.X_OK), "not executable"
    assert "psycopg2" in SRC and "DATABASE_URL" in SRC
    compile(SRC, PATH, "exec")
