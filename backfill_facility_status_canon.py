#!/usr/bin/env python3
"""Backfill the legacy lowercase `discovered_facilities.status` cohort onto the
canonical Title-Case vocabulary (util/facility_status.py::canon_status).

PR #2047 fixed every ingest WRITER but deliberately left the legacy rows:
10,435 `'active'` + 12 lowercase `'operational'` (measured 2026-07-31, all
power_mw=0). This script finishes that job.

    python3 backfill_facility_status_canon.py                # dry run (default)
    python3 backfill_facility_status_canon.py --apply
    python3 backfill_facility_status_canon.py --rollback <file.json>

★ WHY THIS SELF-GATES ★
The backfill is NOT a contained rename. `status` is read by call sites that key
on the exact literal, and flipping the data moves them:

  * `COALESCE(status,'') <> 'active'` exclusions  -> ~10.4k zero-MW shells flood
    IN, inflating facility/operator counts with ZERO MW change.
  * `LOWER(status) = 'operational'`               -> the inverse trap: rows flood
    in and drag AVG(power_mw) down.
  * `status = 'operational'` (lowercase)          -> matches 12 rows today,
    matches NOTHING after: the counter goes to zero.

So --apply refuses while any known-blocking predicate is still in the tree.
Fix the readers first; then this becomes the no-op it should be.

★ SCOPE GUARD ★
`discovered_facilities.status` carries TWO DISJOINT VOCABULARIES: the lifecycle
one (Operational / Under Construction / Planned / ...) and a moderation-workflow
one ('pending' -> 'approved'/'rejected', written by facility_auto_approve.py and
gauged by routes/flywheel_master_shell.py:326). This script matches the two
lifecycle literals EXACTLY. Never widen it to "anything not Title-Case" — that
would eat the auto-approve queue.
"""
import argparse
import datetime as dt
import json
import os
import sys

# Exactly the two legacy lifecycle literals. Not a pattern. See SCOPE GUARD.
LEGACY = ("active", "operational")
TARGET = "Operational"          # canon_status()'s mapping for both

# Predicates that MUST be gone from the tree before this may run. Each entry is
# (literal, why it blocks).
BLOCKERS = [
    ("<> 'active'",
     "count-based exclusion: the shells flood IN, inflating counts at 0 MW"),
    ("LOWER(status) = 'operational'",
     "inverse trap: the shells flood IN and drag AVG(power_mw) down"),
    ("status = 'operational'",
     "lowercase exact-match counter: drops from 12 rows to 0"),
]

REPO = os.path.dirname(os.path.abspath(__file__))


def _primary_url() -> str:
    """Writes go to the PRIMARY. NEON_REPLICA_URL is read-only by design."""
    for var in ("DATABASE_URL", "NEON_DATABASE_URL"):
        if os.environ.get(var):
            return os.environ[var]
    sys.exit("no DATABASE_URL / NEON_DATABASE_URL in the environment")


def _sql_strings(tree):
    """Every string constant in the module EXCEPT docstrings.

    Docstrings are excluded on purpose: a file that documents the predicate it
    removed would otherwise block this backfill forever, and — the direction
    that actually bites — a real regression could hide behind a docstring
    mention of the old literal. Same lesson as the radar guard in
    tests/test_radar_freshness.py.
    """
    import ast
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [(n.value, n.lineno) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def scan_blockers() -> list:
    """Scan the shipped source for predicates this backfill would break.

    Source-scanning rather than trusting a checklist: the reason the legacy
    cohort is still here is that readers keyed on a literal nobody had
    inventoried. Conservative by design — it matches the literal wherever it
    appears in a file that touches discovered_facilities, so it can over-report
    (a file querying both tables). Over-reporting refuses the run; under-
    reporting corrupts published figures.
    """
    import ast
    found = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "tests", "node_modules", "__pycache__",
                                "dchub-frontend", ".claude", "scripts")]
        for fn in files:
            if not fn.endswith(".py") or fn == os.path.basename(__file__):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                tree = ast.parse(src)
            except Exception:
                continue
            # Only files that actually read the table in question.
            if "discovered_facilities" not in src:
                continue
            rel = os.path.relpath(path, REPO)
            for text, lineno in _sql_strings(tree):
                for lit, why in BLOCKERS:
                    if lit in text:
                        found.append((f"{rel}:{lineno}", lit, why))
    return sorted(set(found))


def measure(cur) -> dict:
    cur.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE COALESCE(power_mw,0) > 0),
               COALESCE(SUM(power_mw),0)::float
        FROM discovered_facilities WHERE status = ANY(%s)""", (list(LEGACY),))
    n, with_mw, mw = cur.fetchone()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(power_mw),0)::float "
                "FROM discovered_facilities")
    total, total_mw = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                "WHERE status IN ('pending','approved','rejected','duplicate')")
    (moderation,) = cur.fetchone()
    return {"legacy_rows": n, "legacy_with_mw": with_mw, "legacy_mw": mw,
            "table_rows": total, "table_mw": total_mw,
            "moderation_rows": moderation}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="FILE")
    ap.add_argument("--force", action="store_true",
                    help="run --apply despite blocking predicates (do not)")
    args = ap.parse_args()

    import psycopg2
    conn = psycopg2.connect(_primary_url(), connect_timeout=20)
    conn.autocommit = False
    cur = conn.cursor()

    if args.rollback:
        with open(args.rollback) as f:
            payload = json.load(f)
        rows = payload["rows"]
        print(f"restoring {len(rows):,} rows from {args.rollback}")
        for rid, old in rows:
            cur.execute("UPDATE discovered_facilities SET status=%s WHERE id=%s",
                        (old, rid))
        conn.commit()
        print("rollback committed.")
        return 0

    before = measure(cur)
    print("BEFORE")
    for k, v in before.items():
        print(f"    {k:<20}{v:>14,.0f}" if isinstance(v, float)
              else f"    {k:<20}{v:>14,}")

    if before["legacy_with_mw"]:
        # The cohort is defined as zero-MW shells. If that stops being true the
        # premise of every downstream impact estimate is void.
        sys.exit(f"ABORT: {before['legacy_with_mw']} legacy rows now carry "
                 "power_mw > 0 — re-measure before backfilling")

    blockers = scan_blockers()
    if blockers:
        print(f"\n{len(blockers)} BLOCKING predicate(s) still in the tree:")
        for where, lit, why in blockers:
            print(f"    {where:<44}{lit}\n        -> {why}")
    else:
        print("\nno blocking predicates found in the tree.")

    if not args.apply:
        print(f"\nDRY RUN — would set status='{TARGET}' on "
              f"{before['legacy_rows']:,} rows. Re-run with --apply.")
        return 0

    if blockers and not args.force:
        sys.exit("\nREFUSING to apply: fix the predicates above first. They "
                 "would silently move published figures. (--force overrides; "
                 "do not use it to make your own change land.)")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.expanduser(f"~/Downloads/facility_status_backfill_rollback_{stamp}.json")
    cur.execute("SELECT id, status FROM discovered_facilities "
                "WHERE status = ANY(%s) ORDER BY id", (list(LEGACY),))
    captured = cur.fetchall()
    with open(out, "w") as f:
        json.dump({"generated_at": stamp, "target": TARGET,
                   "rows": [[r[0], r[1]] for r in captured]}, f)
    print(f"\nid-level rollback written first: {out} ({len(captured):,} rows)")

    cur.execute("UPDATE discovered_facilities SET status=%s "
                "WHERE status = ANY(%s)", (TARGET, list(LEGACY)))
    changed = cur.rowcount

    after = measure(cur)
    ok = (after["table_rows"] == before["table_rows"]
          and round(after["table_mw"], 3) == round(before["table_mw"], 3)
          and after["moderation_rows"] == before["moderation_rows"]
          and after["legacy_rows"] == 0)
    if not ok:
        conn.rollback()
        sys.exit(f"ABORT (rolled back): invariants broke\n  before={before}\n  after={after}")

    conn.commit()
    print(f"committed: {changed:,} rows -> '{TARGET}'")
    print("invariants held: total rows unchanged, SUM(power_mw) unchanged, "
          "moderation-vocabulary rows untouched, legacy cohort now empty.")
    print(f"\nrollback:  python3 {os.path.basename(__file__)} --rollback {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
