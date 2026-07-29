#!/usr/bin/env python3
"""
check_prewall_offers.py — is the pre-wall pay offer reaching REAL agents?

★WHY THIS EXISTS. The pre-wall offer shipped 2026-07-27 (gateway dfc3f42) wired
ONLY into the KEYED `gate.trial_taste` branch. The anon/auto-mint cascade returns
before ever reaching it, and that cascade carries ~95% of real flagship traffic —
so the surface was a total no-op for real callers while looking healthy in
probes. The 07-27 verification passed because the probe held a minted key and
took the keyed route. ★The generalised lesson: verify PATH overlap, not just SET
overlap; a probe that mints a key is not a probe of anonymous traffic.

Fixed 2026-07-28: gateway `67913dc6` (offer also attaches in the anon
`trial_taste_inline` under-cap branch) + `MPP_PREWALL_DISABLE` set to `0` — the
merge alone was inert without that. Admin visibility landed in backend
`71e6948f`.

★BASELINE, pinned 2026-07-28 ~19:30 local, AFTER both deploys:
    master-tick split.real.prewall      = 1
    master-tick split.all.prewall       = 35   (34 of them internal probes)
    master-tick funnel.real_prewall     = 1
    shell #34 rc_prewall                = 1 offer / 2,973 granted calls (30d)
    mpp_paid (all time)                 = 0    ← the number that ultimately matters

★★THE ONE REAL OFFER PREDATES THE FIX. It fired 2026-07-28 06:22:48Z on
get_market_intel (platform `mcp`, tier free) during the window when the same fix
briefly sat on main as 9653066 before being reverted. It proves the anon wiring
works for genuine callers, but it is NOT evidence for the current deploy. So:

    real prewall offers > 1  =  the FIRST genuine post-fix evidence.
    real prewall offers == 1 =  no real caller has hit the window since.

★HOW TO READ THE RESULT — this matters more than the number.
  · real offers GREW, mpp_paid still 0 → the surface works and agents are
    declining to pay. That is a COMMERCIAL question (tighten the flagship trial),
    NOT a bug. Do not "fix" the offer further.
  · real offers FLAT at 1 → the offer is not reaching real traffic. Work the
    rc_prewall 0-case checklist: MPP_PREWALL_DISABLE must not be '1',
    MPP_PREWALL_AT (2) vs DCHUB_TRIAL_TOOL_DAILY_FULL (4), anon-cascade wiring
    still present. Also note 39% of callers make exactly ONE call/day and can
    never reach an offer that fires at remaining<=2 — a flat number may be cohort
    shape, not breakage.
  · internal-only growth → only probes fired; ignore it, that is not traffic.

★OPEN ANOMALY worth a look: on 2026-07-28 a fresh-session anon probe of
get_market_intel jumped remaining 3 -> walled 0, skipping the offer window
entirely, while get_fiber_intel stepped 3/2/1/0 correctly and fired at each of
the last three. get_market_intel also shows `trial_taste_bounded`, so it may take
a different branch. If real offers stay flat, start here.

★Reads the ADMIN ENDPOINTS, not the DB — no DSN needed, just the 64-hex admin key
in ~/.dchub_admin_key. Hits the RAILWAY ORIGIN: admin GETs are edge-cached by CF
Cache Rules (measured age 2,296s on /api/v1/health) and the zone route has a 15s
admin timeout.

Usage:  python3 check_prewall_offers.py
"""
from __future__ import annotations

import os
import sys

import requests

ORIGIN = "https://dchub-backend-production.up.railway.app"
TICK = "/api/v1/admin/agent-pay/master-tick"
SHELL = "/api/v1/admin/agent-pay-shell/master-tick?fresh=1"
KEYFILE = os.path.expanduser("~/.dchub_admin_key")

BASE_REAL_PREWALL = 1
BASE_ALL_PREWALL = 35
BASE_PAID = 0
BASE_DATE = "2026-07-28"


def _get(path: str, key: str):
    r = requests.get(ORIGIN + path, headers={"X-Admin-Key": key}, timeout=90)
    r.raise_for_status()
    return r.json()


def main() -> int:
    try:
        key = open(KEYFILE).read().strip()
    except OSError as e:
        print(f"cannot read {KEYFILE}: {e}", file=sys.stderr)
        return 2
    if len(key) != 64:
        print(f"{KEYFILE} is not the 64-hex admin key (len={len(key)})",
              file=sys.stderr)
        return 2

    tick = _get(TICK, key)
    split = tick.get("split") or {}
    real = (split.get("real") or {})
    alls = (split.get("all") or {})
    funnel = tick.get("funnel") or {}

    # ★`prewall` is absent, not 0, if the backend predates 71e6948f. Absent must
    # NOT be reported as zero — that is the exact false-zero this whole thread
    # was about.
    if "prewall" not in real:
        print("split.real has no `prewall` key — backend predates 71e6948f or "
              "the surface changed shape. UNMEASURED, not zero.", file=sys.stderr)
        return 3

    real_pw = int(real.get("prewall") or 0)
    all_pw = int(alls.get("prewall") or 0)
    paid = int(real.get("paid") or 0)
    chal = int(real.get("challenges") or 0)

    print(f"=== pre-wall offer · baseline {BASE_DATE} -> now")
    print(f"  real prewall offers : {BASE_REAL_PREWALL} -> {real_pw}")
    print(f"  all  prewall offers : {BASE_ALL_PREWALL} -> {all_pw}  "
          f"(internal probes included)")
    print(f"  real challenges     : {chal}   (agents that ASKED to pay)")
    print(f"  real paid           : {BASE_PAID} -> {paid}   <-- the one that matters")
    for t in (tick.get("by_tool") or []):
        if t.get("real_prewall_offers"):
            print(f"    {t.get('tool')}: {t['real_prewall_offers']} offer(s)")

    try:
        shell = _get(SHELL, key)
        for lane in (shell.get("lanes") or []):
            for ck in (lane.get("checks") or []):
                if ck.get("id") in ("rc_prewall", "rc_share"):
                    print(f"  shell {ck['id']}: pass={ck.get('pass')} — "
                          f"{(ck.get('detail') or '')[:120]}")
    except Exception as e:
        print(f"  (shell #34 unreadable: {str(e)[:80]})")

    print()
    if paid > BASE_PAID:
        print(f"VERDICT: FIRST REAL PAYMENT — mpp_paid {BASE_PAID} -> {paid}. "
              f"The rail closed end to end. Everything else is secondary.")
        return 0
    if real_pw > BASE_REAL_PREWALL:
        print(f"VERDICT: REACHING REAL AGENTS. {real_pw - BASE_REAL_PREWALL} new "
              f"real offer(s) since the fix — the first genuine post-fix evidence "
              f"(the baseline 1 predates the merge).")
        print("  mpp_paid is still 0, so agents SEE the offer and decline. That is "
              "a COMMERCIAL question — tighten the flagship trial — not a bug.")
        print("  Do NOT tune the offer further on this evidence.")
        return 0
    print("VERDICT: NO NEW REAL OFFERS (still at the pre-fix baseline of 1).")
    print("  Check, in order: MPP_PREWALL_DISABLE != '1'; MPP_PREWALL_AT (2) vs "
          "DCHUB_TRIAL_TOOL_DAILY_FULL (4); anon-cascade wiring intact.")
    print("  ★But 39% of callers make exactly ONE call/day and can never reach an "
          "offer that fires at remaining<=2 — flat may be cohort shape, not "
          "breakage. Compare against granted-call volume before concluding.")
    if all_pw > BASE_ALL_PREWALL:
        print(f"  NOTE: total offers grew {BASE_ALL_PREWALL} -> {all_pw} but real "
              f"stayed flat — that is probe traffic only, not demand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
