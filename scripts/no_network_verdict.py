#!/usr/bin/env python3
"""Verdict for the unit-tests step's no-network rule (.github/workflows/pre-merge.yml).

Reads the log tests/_no_network/sitecustomize.py appended to during that step's
pytest run, and tests/no_network_register.json: the known debt, test files (or
modules) and the hosts they reached when the rule landed. Exits 1 when:

- a file reached a host the register does not list for it: a new file, or a
  registered file reaching a new host (one ::error per file, naming the hosts);
- no pytest process loaded the hook (log missing, empty, or only other
  processes): zero attempts would then mean nothing was watched;
- a log line does not parse, or the register cannot be read.

A registered host that nothing reached this run is a ::warning, not a failure.
Some attempts happen only under a condition, and one quiet run does not show a
file is fixed: retire an entry only when it was quiet in every run compared,
and compare at least three.

With DCHUB_NO_NETWORK_PARTIAL_RUN=1 (each `unit-tests shard N` job) the log
covers one shard's files only, so "nothing reached it" would be true of every
registered file that ran in another shard. A partial run still fails on every
error above but skips that warning. The `unit-tests` job judges every shard's
log together, without the flag, so each quiet entry is reported once per run.

Usage: python3 scripts/no_network_verdict.py <log> [<register>]
"""
import json
import os
import sys
from collections import defaultdict

REGISTER = "tests/no_network_register.json"


def _esc(value):
    """Workflow-command escaping, so a value cannot end the annotation early."""
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def verdict(log_path, register_path=REGISTER, partial=False):
    """Return (exit code, lines to print). partial: the log is one shard's; see the module docstring."""
    try:
        with open(register_path, encoding="utf-8") as f:
            known = {name: set(hosts) for name, hosts in json.load(f)["known"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return 1, [f"::error::no-network register {register_path} is unreadable ({_esc(e)}); the log cannot be judged without it."]

    errors = []
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        lines = []
        errors.append(f"::error::no-network log {log_path} is unreadable ({_esc(e.strerror)}): the hook never wrote to it.")

    entries, unparseable = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            unparseable += 1

    loaded = [e for e in entries if e.get("ev") == "loaded"]
    under_pytest = sum(1 for e in loaded if e.get("pytest") is True)
    attempts = [e for e in entries if e.get("ev") in ("dns", "connect")]
    reached = defaultdict(lambda: defaultdict(int))
    for a in attempts:
        reached[a.get("file") or ""][str(a.get("host"))] += 1

    out = [
        f"no-network: hook loaded in {len(loaded)} python process(es), {under_pytest} running pytest; "
        f"{len(attempts)} lookup/connect attempt(s) off loopback, all refused, from {len(reached)} file(s)"
    ]
    if unparseable:
        errors.append(f"::error::no-network log has {unparseable} unparseable line(s); a log this cannot read is not a clean log.")
    if not under_pytest:
        errors.append(
            "::error::the no-network hook loaded in no pytest process, so zero attempts proves nothing. "
            "The step must put tests/_no_network on PYTHONPATH for pytest."
        )

    for name, hosts in sorted(reached.items()):
        new = [h for h in sorted(hosts) if h not in known.get(name, ())]
        if not new:
            continue
        what = ", ".join(f"{h} ({hosts[h]}x)" for h in new)
        where = f" file={name}" if name else ""
        who = name or "a process with no test file or repo module on its stack"
        unlisted = "that host for it" if name in known else "it"
        errors.append(
            f"::error{where}::no network: {_esc(who)} reached {_esc(what)}, and {REGISTER} does not list {unlisted}. "
            "The hook refused it. Stub the fetch in the test; do not add it to the register to make it pass."
        )

    if partial:
        out.append("no-network: partial run (one unit-tests shard); quiet register entries are judged once, "
                   "over every shard's log, by the unit-tests job")
    for name, hosts in sorted(known.items()):
        quiet = [h for h in sorted(hosts) if h not in reached.get(name, {})]
        if quiet and not partial:
            out.append(
                f"::warning file={name}::no network: the register lists {_esc(', '.join(quiet))} for {_esc(name)}, and nothing "
                "reached it this run. Retire it only if it is quiet in every run compared (at least three)."
            )

    debt = sum(n for name, hosts in reached.items() for h, n in hosts.items() if h in known.get(name, ()))
    out.append(f"no-network: {debt} attempt(s) are registered debt, across {len(known)} registered file(s)")
    return (1 if errors else 0), out + errors


def main(argv):
    if len(argv) not in (2, 3):
        print("usage: python3 scripts/no_network_verdict.py <log> [<register>]", file=sys.stderr)
        return 2
    code, lines = verdict(*argv[1:], partial=os.environ.get("DCHUB_NO_NETWORK_PARTIAL_RUN") == "1")
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
