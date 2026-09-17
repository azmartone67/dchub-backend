#!/usr/bin/env python3
"""tests/test_thin_twin_canonical_repair.py — the empty twin points at the
populated one, and never the reverse.

NO NETWORK, NO PRODUCTION DB. The behavioural half runs the script's own SQL
constants, verbatim, against an in-memory SQLite fixture — the three SELECTs
carry no driver placeholders precisely so this is possible. That is the point:
a regex can tell you `power_mw > 0` is still in the file, it cannot tell you
which side of the join it is on. Five guards in this repo have asserted on
prose and passed while the behaviour was inverted.

WHAT IS BEING PROTECTED (measured 2026-08-14, production)
---------------------------------------------------------
533 live rows share LOWER(TRIM(name)) + LOWER(TRIM(city)) with a live row that
has capacity. 524 already carry duplicate_of_id at that twin, so
_canonical_twin_url already serves rel=canonical there — verified live as
Googlebot. Only 9 fail to consolidate, and a separate 24 rows fail the OTHER
way: a page WITH capacity canonicalising to a page WITHOUT.

★ THE FOUR WAYS THIS SCRIPT COULD DO REAL DAMAGE, one test each:

  1. Set is_duplicate. That is the 2026-07-27 change that left 57 of 58 slugs
     with no keeper and was reverted; repeating it here drops 527 canonical
     slugs and takes canonical_stats.facilities_verified 17,864 -> 17,337,
     through the advertised "17,500+".
  2. Invert direction. Clearing the pointer on the THIN side instead of the
     populated side would undo the 518 correct pointers — and the run would
     print a large, healthy-looking row count while doing it.
  3. Repair A before clearing B. 5 of A's targets currently point back at A's
     rows; A-first creates 5 canonical CYCLES, which Google reads as no
     canonical at all. Strictly worse than the self-canonical it replaces.
  4. Guess at an ambiguous row. Nothing is ambiguous today, but a future ingest
     landing two populated twins for one name+city would otherwise pick by scan
     order — the nondeterminism test_sitemap_thin_gate.py documents for Equinix
     PA2/PA3.

Run standalone:   python3 tests/test_thin_twin_canonical_repair.py
Run under pytest: pytest tests/test_thin_twin_canonical_repair.py
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "repair_thin_twin_canonical.py")
sys.path.insert(0, ROOT)

import repair_thin_twin_canonical as R  # noqa: E402


def _src():
    return open(SRC, encoding="utf-8").read()


def _code(text):
    """Source with comments and the module docstring stripped.

    The docstring quotes the is_duplicate change this script deliberately does
    NOT make. Matching prose would read that warning as the behaviour.
    """
    body = text.split('"""', 2)[2] if text.count('"""') >= 2 else text
    return "\n".join(l for l in body.splitlines()
                     if not l.lstrip().startswith("#"))


# ─────────────────────────────────────────────────────────────────────────────
# Fixture. Every row here is a real shape from the 2026-08-14 measurement.
# ─────────────────────────────────────────────────────────────────────────────
COLS = ("id", "name", "city", "power_mw", "is_duplicate", "duplicate_of_id",
        "canonical_slug")

ROWS = [
    # -- already correct: thin -> populated. Must be found but NOT repaired.
    (100, "Equinix DA6 Dallas", "Dallas", 0, 0, 101, "equinix-inc-da6"),
    (101, "Equinix DA6 Dallas", "Dallas", 18, 0, None, "equinix-da6"),

    # -- DEFECT A.1: no pointer at all -> self-canonical.
    (200, "Switch Las Vegas 8", "Las Vegas", 0, 0, None, "switch-lv8-thin"),
    # -- DEFECT B on the same pair: the 130 MW row points BACK at the empty one.
    (201, "Switch Las Vegas 8", "Las Vegas", 130, 0, 200, "switch-lv8"),

    # -- DEFECT A.2: pointer at a row that is itself flagged; the target is
    #    rejected by _canonical_twin_url, so the page self-canonicals anyway.
    (300, "QTS Chicago", "Chicago", 0, 0, 302, "qts-realty-chicago"),
    (301, "QTS Chicago", "Chicago", 95, 0, None, "qts-chicago"),
    (302, "QTS Chicago", "Chicago", 120, 1, None, "qts-chicago-flagged"),

    # -- DEFECT A.3: pointer at another THIN row instead of the populated twin.
    (400, "Telehouse Osaka", "Osaka", 0, 0, 402, "telehouse-osaka-thin-a"),
    (401, "Telehouse Osaka", "Osaka", 3, 0, None, "telehouse-osaka"),
    (402, "Telehouse Osaka", "Osaka", 0, 0, None, "telehouse-osaka-thin-b"),

    # -- AMBIGUOUS: two populated twins for one name+city. Must be reported and
    #    skipped, never guessed.
    (500, "Ambiguous Campus", "Reno", 0, 0, None, "ambiguous-thin"),
    (501, "Ambiguous Campus", "Reno", 10, 0, None, "ambiguous-pop-1"),
    (502, "Ambiguous Campus", "Reno", 20, 0, None, "ambiguous-pop-2"),

    # -- SHARED canonical_slug: one URL between them, nothing to consolidate.
    (600, "Equinix AM3", "Amsterdam", 0, 0, None, "equinix-am3"),
    (601, "Equinix AM3", "Amsterdam", 12, 0, None, "equinix-am3"),

    # -- NORMALISATION: differs only by case and surrounding whitespace.
    (700, "  cratis dc north ", "Copenhagen", 0, 0, None, "unknown-dc-north"),
    (701, "Cratis DC North", "  Copenhagen", 3, 0, None, "cratis-dc-north"),

    # -- A FLAGGED thin row. Not a candidate: it emits no URL.
    (800, "Iron Mountain WIR-1", "Manassas", 0, 1, None, "im-wir1-flagged"),
    (801, "Iron Mountain WIR-1", "Manassas", 40, 0, None, "im-wir1"),
]


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE discovered_facilities ("
                 "id INTEGER PRIMARY KEY, name TEXT, city TEXT, power_mw REAL, "
                 "is_duplicate INTEGER, duplicate_of_id INTEGER, "
                 "canonical_slug TEXT)")
    conn.executemany(
        "INSERT INTO discovered_facilities (%s) VALUES (%s) ON CONFLICT DO NOTHING"
        % (",".join(COLS), ",".join("?" * len(COLS))), ROWS)
    return conn


def _thin_twin(conn):
    """(thin_id, old_pointer, twin_id, n_twins) for every selected row."""
    return [(r[0], r[2], r[3], r[6])
            for r in conn.execute(R.THIN_TWIN_SQL).fetchall()]


def _plan(conn):
    """The script's OWN split, not the test's idea of it — R.plan is the
    function main() calls. Re-implementing it here would prove only that the
    test agrees with itself."""
    return R.plan(conn.execute(R.THIN_TWIN_SQL).fetchall())


# ─────────────────────────────────────────────────────────────────────────────
# Behaviour
# ─────────────────────────────────────────────────────────────────────────────
def test_it_finds_the_thin_side_and_targets_the_populated_one():
    """Direction. The row WITHOUT capacity is the one that gets a pointer."""
    got = {r[0]: r[2] for r in _thin_twin(_db())}
    assert got.get(200) == 201, "the empty Switch row must target the 130 MW row"
    assert got.get(300) == 301, "must target the LIVE 95 MW row, not the flagged 120 MW one"
    assert got.get(400) == 401, "must target the populated row, not the other thin one"
    assert got.get(700) == 701, "LOWER(TRIM(...)) must match across case and padding"
    # The populated rows are never candidates for a pointer of their own.
    for populated in (101, 201, 301, 401, 501, 601, 701, 801):
        assert populated not in got, (
            f"id {populated} has capacity and must never be given a "
            f"duplicate_of_id by this script — that is defect B, not a repair"
        )


def test_a_flagged_row_is_neither_candidate_nor_target():
    """_canonical_twin_url rejects a flagged target and returns None, which is
    exactly how three production rows ended up self-canonical. Pointing at one
    would reproduce the bug being fixed."""
    got = {r[0]: r[2] for r in _thin_twin(_db())}
    assert 800 not in got, "a flagged thin row emits no URL and is not a candidate"
    assert 302 not in set(got.values()), "a flagged row must never be a target"


def test_twins_sharing_one_canonical_slug_are_left_alone():
    """One URL between them: no duplicate to consolidate, and a pointer would
    only add a self-referential canonical."""
    assert 600 not in {r[0] for r in _thin_twin(_db())}


def test_an_ambiguous_row_is_reported_but_never_repaired():
    """★ The count is carried out of SQL so plan() can skip on it. If n_twins
    were dropped, the MIN(p.id) tiebreak would silently pick the 10 MW row over
    the 20 MW one on nothing but id order."""
    rows = _thin_twin(_db())
    amb = [r for r in rows if r[0] == 500]
    assert amb, "the ambiguous row must still be SELECTED, so it can be reported"
    assert amb[0][3] == 2, f"n_twins must expose the ambiguity; got {amb[0][3]}"

    repair, already, ambiguous = _plan(_db())
    assert {r[0] for r in ambiguous} == {500}, (
        f"expected exactly the ambiguous row; got {sorted(r[0] for r in ambiguous)}"
    )
    assert 500 not in {r[0] for r in repair}, (
        "an ambiguous row reached the repair set — the winner would be MIN(id), "
        "which is the 10 MW row over the 20 MW one, on nothing but id order"
    )
    assert 500 not in {r[0] for r in already}


def test_already_correct_rows_are_not_rewritten():
    """518 of the 527 are already right. A repair that rewrote them would log a
    large row count and mean nothing — and would mask a real regression."""
    rows = _thin_twin(_db())
    assert (100, 101, 101, 1) in [(r[0], r[1], r[2], r[3]) for r in rows], (
        "the already-correct row must be selected so its state is verifiable"
    )
    todo, already, _amb = _plan(_db())
    assert {r[0] for r in already} == {100}, "id 100 is already correct"
    assert 100 not in {r[0] for r in todo}, "already-correct rows must not be rewritten"
    # 402 is in the set on its own merits: it is thin, it has the populated
    # twin 401, and it carries no pointer. Two thin rows for one facility both
    # get one — many-to-one is the normal shape (production has 8430 and 18741
    # both pointing at 526).
    assert {r[0] for r in todo} == {200, 300, 400, 402, 700}, (
        f"unexpected repair set: {sorted(r[0] for r in todo)}"
    )
    assert {r[0] for r in todo} | {r[0] for r in already} | {r[0] for r in _amb} \
        == {r[0] for r in rows}, "plan() dropped a selected row on the floor"


def test_backwards_finds_capacity_pointing_at_empty_and_nothing_else():
    """★ DEFECT B, and the direction that matters most. If the two power_mw
    predicates were swapped this would return the THIN rows, and clearing their
    pointers would undo every correct canonical in the table."""
    conn = _db()
    got = {(r[0], r[3]) for r in conn.execute(R.BACKWARDS_SQL).fetchall()}
    assert got == {(201, 200)}, (
        f"expected exactly the 130 MW -> 0 MW pointer; got {sorted(got)}"
    )
    # 100 -> 101 is thin -> populated: correct, and must never be cleared.
    assert 100 not in {g[0] for g in got}, (
        "a correct thin->populated pointer was selected for CLEARING — the "
        "power_mw predicates are inverted"
    )


def test_a_without_b_creates_cycles_so_the_two_must_be_atomic():
    """★ The reason this is ONE transaction.

    A alone points the empty Switch row at the 130 MW row while the 130 MW row
    still points back: both pages name each other, neither consolidates, and
    Google reads that as no canonical at all.

    ★ This test also killed the claim it was written to prove. It first
    asserted that B had to run BEFORE A, and the both-orders run below showed
    that is false — the two id sets are disjoint (A writes only power_mw = 0
    rows, B only power_mw > 0), so either order lands the same state. The
    invariant is atomicity. The script's comment was corrected to match."""
    def cycles(conn):
        return conn.execute(R.CYCLE_CHECK_SQL).fetchall()

    def repair_a(conn):
        for r in R.plan(conn.execute(R.THIN_TWIN_SQL).fetchall())[0]:
            conn.execute("UPDATE discovered_facilities SET duplicate_of_id=? "
                         "WHERE id=?", (r[3], r[0]))

    def clear_b(conn):
        ids = [r[0] for r in conn.execute(R.BACKWARDS_SQL).fetchall()]
        conn.executemany("UPDATE discovered_facilities SET duplicate_of_id=NULL "
                         "WHERE id=?", [(i,) for i in ids])

    assert not cycles(_db()), "fixture must start clean, or nothing below means anything"

    partial = _db()
    repair_a(partial)
    assert cycles(partial), (
        "A applied alone must produce a cycle. If it does not, this fixture no "
        "longer reproduces the hazard and the atomicity requirement is unproven"
    )

    # Disjoint id sets — so both orders must reach the same clean state.
    a_ids = {r[0] for r in _plan(_db())[0]}
    b_ids = {r[0] for r in _db().execute(R.BACKWARDS_SQL).fetchall()}
    assert not (a_ids & b_ids), (
        f"A and B write overlapping rows ({sorted(a_ids & b_ids)}) — order now "
        f"changes the outcome and the script must impose one"
    )
    for order in ((clear_b, repair_a), (repair_a, clear_b)):
        conn = _db()
        order[0](conn)
        order[1](conn)
        assert not cycles(conn), f"{[f.__name__ for f in order]} left a cycle"
        assert conn.execute("SELECT duplicate_of_id FROM discovered_facilities "
                            "WHERE id=200").fetchone()[0] == 201


# ─────────────────────────────────────────────────────────────────────────────
# Source contract — the things a fixture cannot observe
# ─────────────────────────────────────────────────────────────────────────────
def test_it_never_writes_is_duplicate():
    """★ THE REVERTED CHANGE. is_duplicate is a VISIBILITY flag: flagging the
    527 drops them from COUNT(DISTINCT canonical_slug) WHERE is_duplicate=0,
    which is canonical_stats.facilities_verified — 17,864 -> 17,337, through
    the "17,500+" the MCP instructions advertise."""
    code = _code(_src())
    for stmt in re.findall(r"UPDATE\s+discovered_facilities\s+SET\s+([^\"']+)", code, re.I):
        assert "is_duplicate" not in stmt, (
            f"this script must never write is_duplicate; found: {stmt.strip()[:70]!r}"
        )
        assert "power_mw" not in stmt, (
            "copying power_mw across would re-admit the empty row to the sitemap "
            "as a second URL for the same building"
        )


def test_the_advertised_count_is_asserted_not_assumed():
    code = _code(_src())
    assert "facilities_verified" in code, "the count guard is gone"
    seg = code.split("facilities_verified\"] != before")[-1][:400]
    assert "rollback()" in seg, (
        "a move in facilities_verified must roll the transaction back, not just log"
    )


def test_a_and_b_commit_together_or_not_at_all():
    """The fixture above proves A alone leaves cycles. This proves the script
    cannot ship A alone: one commit, after both writes, with the cycle re-check
    between."""
    body = _code(_src()).split("def main(")[1]
    # find(), not index(): index() raises ValueError, and under the standalone
    # runner that aborted the whole file before the later tests ran — a
    # mutation was "caught" by a traceback in the wrong test. Caught 2026-08-14
    # by the mutation harness.
    b_write = body.find("SET duplicate_of_id = NULL")
    a_write = body.find("SET duplicate_of_id = %s")
    assert b_write != -1, "defect B's write is gone"
    assert a_write != -1, "defect A's write is gone"
    commits = [m.start() for m in re.finditer(r"conn\.commit\(\)", body)]
    assert len(commits) == 1, (
        f"expected exactly one commit; found {len(commits)}. A second commit "
        f"between the two writes would publish A without B"
    )
    assert commits[0] > max(a_write, b_write), "the commit precedes a write"
    assert body.index("CYCLE_CHECK_SQL") < commits[0], (
        "the cycle re-check must run before the commit, or it re-checks a state "
        "already made permanent"
    )


def test_it_is_dry_run_by_default():
    code = _code(_src())
    body = code.split("def main(")[1]
    guard = body.index("if not apply:")
    for m in re.finditer(r"cur\.execute\(\s*\"UPDATE", body):
        assert m.start() > guard, (
            "an UPDATE runs before the --apply guard; the dry run would write"
        )


def test_a_rollback_file_is_written_before_the_first_write():
    body = _code(_src()).split("def main(")[1]
    dump = body.find("json.dump(")
    writes = [m.start() for m in re.finditer(r"UPDATE discovered_facilities SET", body)]
    assert dump != -1, "no rollback file is written at all"
    assert writes, "no writes found — this test would pass vacuously"
    assert dump < min(writes), (
        "the rollback file must exist before the first mutation, or a failed "
        "run leaves no way back"
    )
    assert "--rollback" in _src() and "def rollback(" in _src()


def test_every_query_filters_flagged_rows_on_both_sides():
    """A flagged row is neither candidate nor target. Missing this on the target
    side is exactly how three production rows ended up self-canonical."""
    for name in ("BACKWARDS_SQL", "THIN_TWIN_SQL", "CYCLE_CHECK_SQL"):
        sql = getattr(R, name)
        assert sql.upper().count("IS_DUPLICATE") >= 1, f"{name} does not filter flagged rows"
    assert R.BACKWARDS_SQL.upper().count("COALESCE(D.IS_DUPLICATE, 0) = 0") == 1
    assert R.BACKWARDS_SQL.upper().count("COALESCE(T.IS_DUPLICATE, 0) = 0") == 1
    assert R.CYCLE_CHECK_SQL.upper().count("IS_DUPLICATE, 0) = 0") == 2, (
        "the cycle check must be live-to-live; the table holds an inert "
        "cross-flag pair (17370 <-> 7579) that would abort every run"
    )


def test_the_cycle_check_is_a_delta_not_a_zero_assertion():
    body = _code(_src()).split("def main(")[1]
    assert re.search(r"len\(cycles\)\s*>\s*before\[[\"']pointer_cycles[\"']\]", body), (
        "asserting zero cycles aborts on the pre-existing inert pair; the "
        "assertion must be on cycles this run introduced"
    )


def test_the_normalised_keys_stay_materialised():
    """Not style. Joining on LOWER(TRIM(name)) directly is >30s against
    production (timed 2026-08-14) because no index covers the expression;
    materialised in the CTE it is under a second. The brief that opened this
    work reported a 400s timeout with the same cause."""
    sql = R.THIN_TWIN_SQL
    assert "k_name" in sql and "k_city" in sql, "the normalised keys are gone"
    join = sql[sql.index("JOIN pop p"):sql.index("GROUP BY")]
    assert "LOWER" not in join.upper() and "TRIM" not in join.upper(), (
        "the join re-evaluates LOWER/TRIM per probe — this is the 400s query"
    )


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            # ★ Exception, not AssertionError. pytest isolates each test; this
            # runner does not, so one test raising ValueError used to abort the
            # file and silently skip every test after it — reported by the
            # mutation harness as a mutation the guard "did not catch".
            except Exception as _e:
                _failed += 1
                print(f"✗ {_name}: {type(_e).__name__}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
