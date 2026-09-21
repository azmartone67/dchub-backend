"""The unit-tests verdict: prove that the shards, together, ran every test file.

Each `unit-tests shard N` job (tests/_shard.py) collects the whole suite, keeps
its own files, and writes a report. This reads every report and fails unless
all of these hold:
  - there is a report from every shard 0..total-1, and all agree on the total;
  - every shard collected the SAME set of files;
  - every file is kept by exactly one shard, except the tail guards, which
    every shard keeps;
  - the kept files, together, are exactly the collected set;
  - every shard's own log shows tests that RAN (a pytest summary with passed
    or failed). The job's beat sums the counts over all shards, and a single
    shard that ran zero would disappear inside that sum — the #1797 shape, a
    gate reporting a count while part of it examined nothing.

A shard that died before writing its report is a shard whose files nobody can
prove ran. That is UNMEASURED or incomplete, never clean.

Usage: python3 scripts/unit_test_shards_verdict.py <dir>
  <dir> holds one sub-directory per shard artifact, each with unit-tests-shard.json
  and the shard's pytest log, unit-tests.log.
Exit: 0 complete; 1 incomplete, overlapping or disagreeing; 2 UNMEASURED (no
report at all, or one that cannot be read).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
from collections import Counter

REPORT, LOG = "unit-tests-shard.json", "unit-tests.log"
_KEYS = ("index", "total", "collected", "selected", "n_files", "files_sha256", "kept_files", "replicated")
# The same count the job's beat parses: tests that RAN.
_RAN = re.compile(r"([0-9]+) (?:passed|failed)")


def _digest(files):
    # Same digest as tests/_shard.files_digest. Kept local so this stays stdlib-only.
    return hashlib.sha256("\n".join(sorted(files)).encode()).hexdigest()


def load(directory):
    reports, errors = [], []
    for path in sorted(glob.glob(os.path.join(directory, "*", REPORT))):
        try:
            with open(path, encoding="utf-8") as fh:
                r = json.load(fh)
            missing = [k for k in _KEYS if k not in r]
            if missing:
                raise ValueError(f"missing {missing}")
            # ran: tests this shard's own log says ran; None when there is no log.
            log = os.path.join(os.path.dirname(path), LOG)
            r["ran"] = None
            if os.path.exists(log):
                with open(log, encoding="utf-8", errors="replace") as fh:
                    r["ran"] = sum(int(n) for n in _RAN.findall(fh.read()))
            reports.append(r)
        except (OSError, ValueError) as e:
            errors.append(f"{path}: {e}")
    return reports, errors


def verdict(reports, errors=()):
    if errors:
        return 2, [f"::error::UNMEASURED — unreadable shard report {e}" for e in errors]
    if not reports:
        return 2, ["::error::UNMEASURED — no shard wrote a report, so nothing proves any test file ran."]
    problems = []
    totals = sorted({r["total"] for r in reports})
    total = totals[-1]
    if len(totals) != 1:
        problems.append(f"shards disagree on the shard count: {totals}")
    seen = Counter(r["index"] for r in reports)
    missing = sorted(set(range(total)) - set(seen))
    if missing:
        problems.append(f"no report from shard(s) {missing} of 0..{total - 1}; their files ran nowhere this can prove")
    extra = sorted(i for i, n in seen.items() if n > 1 or not 0 <= i < total)
    if extra:
        problems.append(f"duplicate or out-of-range shard index(es): {extra}")
    if len({r["files_sha256"] for r in reports}) != 1:
        problems.append("shards collected different sets of test files, so no single plan covers them all")
    replicated = set(reports[0]["replicated"])
    if any(set(r["replicated"]) != replicated for r in reports):
        problems.append("shards disagree on which tail guard files run everywhere")
    kept = Counter(f for r in reports for f in r["kept_files"])
    absent = sorted(f for f in replicated if kept[f] != len(reports))
    if absent:
        problems.append(f"tail guard file(s) not run in every shard: {absent}")
    twice = sorted(f for f, n in kept.items() if n > 1 and f not in replicated)
    if twice:
        problems.append(f"{len(twice)} file(s) ran in more than one shard, e.g. {twice[:5]}")
    idle = sorted(r["index"] for r in reports if not r.get("ran"))
    if idle:
        problems.append(
            f"shard(s) {idle} ran zero tests: no `N passed` / `N failed` in their log (a collection "
            "abort, a run cut short, or no log at all), which the summed count would hide")
    n_files = reports[0]["n_files"]
    if _digest(kept) != reports[0]["files_sha256"]:
        problems.append(
            f"the shards kept {len(kept)} distinct file(s), but {n_files} were collected; "
            "some file ran in no shard, or a shard kept a file nobody collected")
    lines = [
        f"unit-tests shards: {len(reports)} report(s) for {total} shard(s); {n_files} files collected, "
        f"{len(kept)} kept ({len(replicated)} tail guard file(s) in every shard); "
        f"{sum(r['selected'] for r in reports)} tests kept across shards "
        f"(tail guard tests once per shard) of {reports[0]['collected']} collected; "
        f"ran per shard: {[r.get('ran') for r in sorted(reports, key=lambda r: r['index'])]}"
    ]
    lines += [f"::error::unit-tests shards: {p}" for p in problems]
    return (1 if problems else 0), lines


def main(argv):
    if len(argv) != 2:
        print("usage: python3 scripts/unit_test_shards_verdict.py <dir>", file=sys.stderr)
        return 2
    code, lines = verdict(*load(argv[1]))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
