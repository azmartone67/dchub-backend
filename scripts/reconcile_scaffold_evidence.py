#!/usr/bin/env python3
"""
reconcile_scaffold_evidence.py — run the merged-scaffold reconciliation.

    python3 scripts/reconcile_scaffold_evidence.py            # live context
    python3 scripts/reconcile_scaffold_evidence.py --ctx c.json
    python3 scripts/reconcile_scaffold_evidence.py --json

All judgement lives in routes/brain_scaffold_reconcile.reconcile(), which is
pure. This file only does I/O: walk routes/_proposed_*.py, obtain a context
(live via the planner's own gatherer, or from a JSON file), print the report.

Read the module docstring before acting on the output. In particular
UNRESOLVED is NOT a staleness verdict — 36% of the corpus cites sources that
never existed, so "does not resolve" and "went stale" are different claims
and this tool refuses to conflate them.

Exit status is 0 unless --strict is passed, in which case any scaffold whose
citations are ALL malformed exits 1. It is a reporter by default: nothing
here deletes a file or opens a PR.
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routes.brain_scaffold_reconcile import (          # noqa: E402
    V_ALL_MALFORMED, V_INDETERMINATE, V_RESOLVE,
    MALFORMED, RESOLVES, SOURCE_DOWN, UNRESOLVED,
    parse_scaffold, reconcile,
)


def _live_context():
    """The planner's own gatherer, so the schema can never drift from the
    one the evidence keys were written against. Returns (ctx, failed_roots)
    — a source that raised is reported, never silently treated as empty."""
    from routes.brain_strategic_planner import _gather_strategic_context
    ctx = _gather_strategic_context()
    failed = [k for k, v in (ctx or {}).items()
              if isinstance(v, dict) and v.get("_note") == "module_not_loaded"]
    return ctx or {}, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ctx", help="JSON file holding the context dict "
                                  "(offline; skips the live gather)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any scaffold's citations are all malformed")
    args = ap.parse_args()

    scaffolds = [parse_scaffold(p)
                 for p in sorted((ROOT / "routes").glob("_proposed_*.py"))]

    if args.ctx:
        ctx, failed = json.loads(pathlib.Path(args.ctx).read_text()), []
    else:
        try:
            ctx, failed = _live_context()
        except Exception as e:
            print(f"could not gather live context: {e}\n"
                  f"Re-run with --ctx <file> to reconcile offline.",
                  file=sys.stderr)
            return 2

    rep = reconcile(ctx, scaffolds, failed_sources=failed)

    if args.json:
        print(json.dumps(rep, indent=2, default=str))
        return 1 if (args.strict and rep["verdicts"][V_ALL_MALFORMED]) else 0

    c = rep["citations"]
    print(f"\n  {rep['scaffolds']} merged scaffold(s), "
          f"{rep['citations_total']} citation(s)\n")
    print(f"  citations   {RESOLVES:<12} {c[RESOLVES]:>4}")
    print(f"              {MALFORMED:<12} {c[MALFORMED]:>4}   "
          f"(root is not a context source — never resolved)")
    print(f"              {UNRESOLVED:<12} {c[UNRESOLVED]:>4}   "
          f"(INDETERMINATE — not a staleness verdict)")
    print(f"              {SOURCE_DOWN:<12} {c[SOURCE_DOWN]:>4}")
    if rep["sources_unavailable"]:
        print(f"  sources unavailable this run: "
              f"{', '.join(rep['sources_unavailable'])}")
    print(f"\n  verdicts    {V_ALL_MALFORMED:<24} "
          f"{rep['verdicts'][V_ALL_MALFORMED]:>4}")
    print(f"              {V_RESOLVE:<24} {rep['verdicts'][V_RESOLVE]:>4}")
    print(f"              {V_INDETERMINATE:<24} "
          f"{rep['verdicts'][V_INDETERMINATE]:>4}\n")

    for r in rep["rows"]:
        if r["verdict"] != V_ALL_MALFORMED:
            continue
        age = f"{r['age_days']}d" if r["age_days"] is not None else "age?"
        print(f"  [{V_ALL_MALFORMED}] {r['slug']}  ({r['week_of']}, {age})")
        for cit in r["citations"]:
            print(f"        {cit['state']:<12} {cit['key']}")
    print(f"\n  {rep['note']}\n")
    return 1 if (args.strict and rep["verdicts"][V_ALL_MALFORMED]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
