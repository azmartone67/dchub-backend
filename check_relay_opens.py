#!/usr/bin/env python3
"""
check_relay_opens.py — did promoting the relay to top level move real human opens?

★WHY THIS EXISTS. `relay_opens` had exactly 2 rows ever and BOTH were our own
probes — zero real humans had ever opened a handoff link. Two causes were found
and both are now fixed (issue #1790):

  1. MCP f9c965d — `for_your_human` was built only in buildPaywallExtras (the
     KEYED gated path). The auto-trial path (buildAutoMintBlock — a keyless agent
     hitting a paid tool, i.e. the COMMON first contact) emitted a trial key and
     no relay at all. The most-travelled path produced nothing to measure.
  2. MCP 0aab503 — `_collapseEnvelope` listed for_your_human in _ENV_MOVE, nesting
     it under `upgrade` beside 14 other URL-ish keys, against its own stated
     premise ("ONE stable object platforms surface verbatim"). Now top level.

★THIS IS AN A/B, NOT A VICTORY LAP. The fixes make the handoff MEASURABLE on the
common path; they do not make humans click. The whole value of this script is
that it can return a clean negative:

    if real opens are STILL 0 with the block present AND top-level, the
    constraint is agent BEHAVIOUR, not envelope shape — and the answer is a
    human-present channel (the bind-time receipt, already live and delivering)
    rather than more envelope tuning.

Do not read a still-zero result as "the fix failed". Read it as the experiment
returning its other answer, which is just as useful and should STOP further
envelope work.

★BASELINE (captured live 2026-07-28, immediately after 0aab503 deployed):
    real opens  = 0
    total rows  = 2   (both ours: human-simulated / dchub-ops-verify)
    write path  = PROVEN (1 row valid=t) — so a zero is delivery/appeal, never
                  an attribution bug. That half of brain investigation #14 is
                  already retired; don't re-litigate it.

★Probe exclusion is by USER-AGENT, not by platform/session — the server
overwrites platform, so a platform-based filter would silently count our own
traffic as human. The shell already does this correctly; we just read it.

★Hits the RAILWAY ORIGIN, not dchub.cloud: admin GETs are cached by CF Cache
Rules, and the CF zone route has a 15s timeout. ?fresh=1 busts the shell's own
in-process cache.

Usage:  python3 check_relay_opens.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

ORIGIN = "https://dchub-backend-production.up.railway.app"
PATH = "/api/v1/admin/actuation/master-tick?fresh=1"
KEYFILE = os.path.expanduser("~/.dchub_admin_key")

BASE_REAL, BASE_TOTAL = 0, 2
BASE_DATE = "2026-07-28"


def _int_before(detail: str, word: str) -> int | None:
    """Pull the integer immediately preceding `word` out of the shell's detail
    string, e.g. '0 real of 2 total relay_opens' -> real=0, total=2."""
    toks = detail.split()
    for i, t in enumerate(toks):
        if t == word and i:
            try:
                return int(toks[i - 1].replace(",", ""))
            except ValueError:
                return None
    return None


def main() -> int:
    try:
        key = open(KEYFILE).read().strip()
    except OSError as e:
        print(f"cannot read {KEYFILE}: {e}", file=sys.stderr)
        return 2
    if len(key) != 64:
        print(f"{KEYFILE} does not look like the 64-hex admin key "
              f"(len={len(key)}) — it was once a mashed-up railway command; "
              f"re-check it", file=sys.stderr)
        return 2

    req = urllib.request.Request(ORIGIN + PATH, headers={"X-Admin-Key": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)

    checks = {c.get("id"): c
              for lane in (payload.get("lanes") or [])
              for c in (lane.get("checks") or [])}
    # ★The shell calls this field `id`, NOT `key` — a parser looking for `key`
    # silently finds nothing and prints a clean, empty, WRONG all-good report.
    got = checks.get("relay_real_opens")
    if not got:
        print("relay_real_opens check ABSENT from the tick — the shell changed "
              "shape or lane 1 errored. NOT a pass.", file=sys.stderr)
        return 3

    detail = got.get("detail") or ""
    real = _int_before(detail, "real")
    total = _int_before(detail, "total")
    wrote = checks.get("relay_write_path", {})

    print(f"=== relay_opens · {payload.get('generated_at', '?')}")
    print(f"baseline ({BASE_DATE}): real={BASE_REAL} total={BASE_TOTAL}")
    print(f"now                   : real={real} total={total}")
    print(f"write path proven     : {wrote.get('pass')}")
    print(f"detail: {detail}")
    print()

    if real is None:
        print("VERDICT: could not parse the count — read `detail` above by hand.")
        return 3
    if real > BASE_REAL:
        print(f"VERDICT: MOVED. {real - BASE_REAL} real human open(s) since the "
              f"fix. Envelope shape mattered. Keep for_your_human top-level and "
              f"start attributing opens to tools/tiers.")
        return 0
    print("VERDICT: STILL ZERO real human opens.")
    print("  This is the experiment's OTHER answer, not a failure to fix "
          "anything. The block is present on the common path and top-level, so")
    print("  envelope shape is now RULED OUT. Stop tuning the envelope.")
    print("  Next lever is the human-present channel — the bind-time receipt "
          "(DCHUB_BIND_RECEIPT_ARM=1, verified delivering) — not more MCP fields.")
    if total is not None and total > BASE_TOTAL:
        print(f"  NOTE: total rows grew {BASE_TOTAL} -> {total}, so links ARE "
              f"being generated and opened by non-human traffic. Delivery works; "
              f"appeal does not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
