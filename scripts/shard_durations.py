"""Rebuild tests/shard_durations.json, the per-file seconds tests/_shard.py balances on.

A stale table costs balance, never coverage: every file is still assigned. So
refresh it when the `unit-tests shard N` jobs' wall times drift apart, not on a
schedule. Fetch the unit-tests job logs of one green run on main (every shard
job's log, or the old single job's), then run this on them:

    gh api --allow-escape-sequences repos/azmartone67/dchub-backend/actions/jobs/<job-id>/logs > s0.log
    python3 scripts/shard_durations.py --source "run <run-id> on main <sha>" s*.log

The Actions log stamps every line. With `pytest -v`, a file's time is the gap
between consecutive result lines, charged to the file of the later line; the
clock starts at the `collected N items` line. A file in several logs (the tail
guards run in every shard) keeps its largest time. Files under MIN_SECONDS are
left out and weigh `default_seconds`, their mean, which keeps the table short.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys

OUT = "tests/shard_durations.json"
MIN_SECONDS = 1.0
_STAMP = r"^\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z "
_COLLECTED = re.compile(_STAMP + r".*\bcollected \d+ items")
_RESULT = re.compile(_STAMP + r"(\S+?\.py)::\S.* (PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _when(whole, frac):
    return dt.datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S") + dt.timedelta(
        microseconds=int((frac or "0")[:6].ljust(6, "0")))


def file_seconds(lines):
    """Per-file seconds from one job log's lines."""
    per, prev = {}, None
    for raw in lines:
        line = _ANSI.sub("", raw)
        if prev is None:
            m = _COLLECTED.match(line)
            if m:
                prev = _when(m.group(1), m.group(2))
            continue
        m = _RESULT.match(line)
        if m:
            now = _when(m.group(1), m.group(2))
            per[m.group(3)] = per.get(m.group(3), 0.0) + (now - prev).total_seconds()
            prev = now
    return per


def table(logs, source):
    merged = {}
    for per in logs:
        for f, s in per.items():
            merged[f] = max(merged.get(f, 0.0), s)
    if not merged:
        raise SystemExit("no `pytest -v` result lines after a `collected N items` line; wrong log?")
    kept = {f: round(s, 1) for f, s in sorted(merged.items()) if s >= MIN_SECONDS}
    rest = [s for f, s in merged.items() if s < MIN_SECONDS]
    return {
        "why": "Per-file seconds that tests/_shard.py balances the unit-tests shards on. "
               "Rebuild with scripts/shard_durations.py; do not hand-edit.",
        "source": source,
        "files_measured": len(merged),
        "default_seconds": round(sum(rest) / len(rest), 2) if rest else MIN_SECONDS,
        "seconds": kept,
    }


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--source", required=True, help="which run the logs came from, for the record")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv[1:])
    per_log = []
    for path in a.logs:
        with open(path, encoding="utf-8", errors="replace") as fh:
            per_log.append(file_seconds(fh))
    data = table(per_log, a.source)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"{a.out}: {len(data['seconds'])} of {data['files_measured']} files over {MIN_SECONDS}s, "
          f"default {data['default_seconds']}s, total {sum(data['seconds'].values()) / 60:.1f}m listed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
