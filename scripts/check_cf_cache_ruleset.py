#!/usr/bin/env python3
"""Guard: the Cloudflare cache ruleset must match the pinned canon.

Why this exists (2026-09-07)
----------------------------
On 2026-09-06 an anonymous caller was served a keyed 1,003,630-byte CSV from
`/api/v1/mcp/tools/export_facility_csv` on a `cf-cache-status: HIT`, eight
minutes after `#4038` gated it at the origin. The origin gate was correct the
whole time: **the Cloudflare cache sits IN FRONT of the Pages worker, so on a
HIT `_worker.js` never executes** and no worker-side change can stop it.

The fix was a cache rule. Which is the problem this script addresses:

    Nothing in this repository referenced the cache ruleset.
    `grep -rn 'fecada93'` over dchub-backend returned zero hits.

25 rules — including the credential-keyed bypass that closed the CSV leak —
lived only in the Cloudflare dashboard, editable by anyone with dashboard
access, with no review, no history in git, and no alarm. A silent edit to rule
ordering is enough to re-open the leak, because **Cache Rules are last-match-
wins**: rule 2 ("Cache Public API", `/api/v1/`, `override_origin`, 3600) beat
rule 1 ("No cache auth") for four months and that is exactly how the CSV
escaped.

This script does NOT change Cloudflare. It reads the live ruleset and diffs it
against `scripts/cf_cache_ruleset_canon.json`. Humans still make edits in the
dashboard or by API append; this makes an unreviewed edit visible within a day.

Two tiers, deliberately
-----------------------
  GUARD (exit 1) — rule set, rule ORDER, expressions, actions, action
        parameters, enabled flags. Any of these moving changes what the edge
        serves.
  GAUGE (exit 0, reported) — the ruleset `version` counter alone. Cloudflare
        bumps it on every write including no-op writes; failing on it would
        train people to re-pin reflexively, which defeats the guard.

★ CANNOT-CHECK IS NOT PASS. A missing token, an API error or an unparseable
response exits 3, never 0. `drift_detected` being a boolean is what let 11 of
16 registry listings report healthy while being unreadable (2026-07-27); a
guard that cannot say "I could not look" is a guard that lies.

★ FLOOR. Canon must carry at least MIN_CANON_RULES rules and a ruleset id, or
the script exits 2 without comparing. An empty canon file must never be able
to produce a green run.

Usage
-----
    python3 scripts/check_cf_cache_ruleset.py            # diff live vs canon
    python3 scripts/check_cf_cache_ruleset.py --probe    # + outside-in probe
    python3 scripts/check_cf_cache_ruleset.py --repin    # bless live as canon

`--repin` is the human step AFTER an intentional rule change: apply it in
Cloudflare, run `--repin`, commit the canon diff in a PR. The canon diff is
then the code review of an edge change that otherwise has none.

Env: CF_CACHE_RULES_TOKEN (or CLOUDFLARE_API_TOKEN) needs
Zone > Config Rules (or Zone > Cache Rules) READ on dchub.cloud. The existing
CLOUDFLARE_API_TOKEN CI secret is scoped to Cache Purge only and will NOT work.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

CANON_PATH = Path(__file__).with_name("cf_cache_ruleset_canon.json")
API_ROOT = "https://api.cloudflare.com/client/v4"

# ★ FLOOR: a canon smaller than this is a corrupt/emptied file, not a real
# config. 25 rules live on 2026-09-07; 20 leaves room for genuine pruning
# without letting an empty file through.
MIN_CANON_RULES = 20

# Fields whose change alters what the edge serves. `description` is included on
# purpose: it is where the WHY of each bypass lives, and a silent rewrite of it
# is how the reason for a rule gets lost.
COMPARED_FIELDS = ("expression", "action", "action_parameters", "enabled", "description")

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_SELF = 2       # the guard's own preconditions failed
EXIT_UNCHECKED = 3  # could not look — NOT a pass


# --------------------------------------------------------------------------
# pure normalisation + diff (no network; this is what the tests exercise)
# --------------------------------------------------------------------------
def normalize_rules(rules: list[dict]) -> list[dict]:
    """Project the CF rule objects down to the fields we pin, in live order."""
    out = []
    for position, rule in enumerate(rules or [], start=1):
        out.append(
            {
                "position": position,
                "id": rule.get("id", ""),
                "description": rule.get("description", ""),
                "expression": rule.get("expression", ""),
                "action": rule.get("action", ""),
                "action_parameters": rule.get("action_parameters", {}),
                "enabled": bool(rule.get("enabled", False)),
            }
        )
    return out


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def diff_ruleset(canon: dict, live_rules: list[dict]) -> list[str]:
    """Return a list of human-readable drift findings. Empty list == match.

    Compares by rule id, then separately compares the ORDER of ids, because
    last-match-wins means a pure reorder changes behaviour while every
    individual rule still compares equal.
    """
    findings: list[str] = []
    canon_rules = canon.get("rules", [])

    canon_by_id = {r["id"]: r for r in canon_rules}
    live_by_id = {r["id"]: r for r in live_rules}

    added = [r for r in live_rules if r["id"] not in canon_by_id]
    removed = [r for r in canon_rules if r["id"] not in live_by_id]

    for rule in added:
        findings.append(
            f"ADDED rule at position {rule['position']} [{rule['id'][:8]}] "
            f"{rule['action']} — {rule['description'][:80]!r}\n"
            f"       expr: {rule['expression'][:160]}"
        )
    for rule in removed:
        findings.append(
            f"REMOVED rule that canon pins at position {rule['position']} "
            f"[{rule['id'][:8]}] — {rule['description'][:80]!r}"
        )

    for rule_id, canon_rule in canon_by_id.items():
        live_rule = live_by_id.get(rule_id)
        if live_rule is None:
            continue
        for field in COMPARED_FIELDS:
            before, after = canon_rule.get(field), live_rule.get(field)
            if _canonical(before) != _canonical(after):
                findings.append(
                    f"CHANGED {field} on rule [{rule_id[:8]}] "
                    f"({canon_rule.get('description', '')[:60]!r})\n"
                    f"       canon: {_canonical(before)[:200]}\n"
                    f"       live : {_canonical(after)[:200]}"
                )

    # Order matters independently of content: LAST MATCHING RULE WINS.
    canon_order = [r["id"] for r in canon_rules if r["id"] in live_by_id]
    live_order = [r["id"] for r in live_rules if r["id"] in canon_by_id]
    if canon_order != live_order:
        findings.append(
            "REORDERED — the shared rules are in a different sequence.\n"
            "       Cache Rules are LAST-MATCH-WINS, so a reorder alone can\n"
            "       re-open a bypass that an earlier rule used to lose.\n"
            f"       canon: {[i[:8] for i in canon_order]}\n"
            f"       live : {[i[:8] for i in live_order]}"
        )

    return findings


def check_canon_floor(canon: dict) -> str | None:
    """Return an error string if canon itself is unusable, else None."""
    if not isinstance(canon, dict):
        return "canon is not a JSON object"
    if not canon.get("ruleset_id"):
        return "canon has no ruleset_id"
    rules = canon.get("rules")
    if not isinstance(rules, list):
        return "canon has no rules list"
    if len(rules) < MIN_CANON_RULES:
        return (
            f"canon carries only {len(rules)} rules, below the floor of "
            f"{MIN_CANON_RULES} — refusing to compare against a truncated or "
            "emptied canon file"
        )
    ids = [r.get("id") for r in rules]
    if len(set(ids)) != len(ids):
        return "canon contains duplicate rule ids"
    return None


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------
def _token() -> str | None:
    return os.environ.get("CF_CACHE_RULES_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN")


def fetch_live_ruleset(zone_id: str, ruleset_id: str) -> tuple[dict | None, str | None]:
    """Return (result, error). Exactly one is not None."""
    import requests  # imported late so the pure tests need no network stack

    token = _token()
    if not token:
        return None, (
            "CF_CACHE_RULES_TOKEN (or CLOUDFLARE_API_TOKEN) is not set. "
            "Needs Zone > Config Rules READ on dchub.cloud. NOT the Cache "
            "Purge token — that scope cannot read rulesets."
        )
    url = f"{API_ROOT}/zones/{zone_id}/rulesets/{ruleset_id}"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except Exception as exc:  # noqa: BLE001 - any transport failure is "could not look"
        return None, f"transport error talking to the Cloudflare API: {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return None, f"Cloudflare API returned HTTP {resp.status_code} (body starts: {resp.text[:200]!r})"
    try:
        payload = resp.json()
    except ValueError:
        return None, f"Cloudflare API returned non-JSON (body starts: {resp.text[:200]!r})"
    if not payload.get("success"):
        return None, f"Cloudflare API reported failure: {json.dumps(payload.get('errors'))[:300]}"
    result = payload.get("result")
    if not isinstance(result, dict) or "rules" not in result:
        return None, "Cloudflare API result carried no rules array"
    return result, None


# --------------------------------------------------------------------------
# outside-in probe
# --------------------------------------------------------------------------
# Each entry names the rule it exercises, so the probe reports what it does NOT
# cover instead of implying the whole ruleset is verified.
PROBE_TABLE = [
    {
        "path": "/api/v1/mcp/tools/export_facility_csv",
        "expect": "bypass",
        "rule": "99ac770b / b3ce82fb",
        "why": "the 2026-09-06 leak path — anon HIT served a keyed 1MB CSV",
    },
    {
        "path": "/api/v1/health",
        "expect": "bypass",
        "rule": "8a06794b",
        "why": "a cached liveness probe reports a dead origin as healthy",
    },
    {
        "path": "/grid",
        "expect": "bypass",
        "rule": "862142e5",
        "why": "tier-varying hub — gates 7 of 9 ISO cards for free callers",
    },
    {
        "path": "/api/v1/stats",
        "expect": "cached",
        "rule": "ef1b5109",
        "why": "POSITIVE CONTROL — proves this probe can still observe a HIT",
    },
]

BASE_URL = "https://dchub.cloud"


def _head_twice(path: str, calls: int = 2) -> list[dict]:
    """GET the SAME url N times with NO cache-buster.

    ★ A `?_=<timestamp>` query makes every request a distinct cache key, so a
    cache-busted probe can never observe the staleness it exists to detect.
    """
    import requests

    observations = []
    for _ in range(calls):
        try:
            resp = requests.get(BASE_URL + path, timeout=30, stream=True)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            observations.append(
                {
                    "status": resp.status_code,
                    "cf_cache_status": headers.get("cf-cache-status"),
                    "age": headers.get("age"),
                    "worker_stamp": headers.get("x-dc-response-time"),
                }
            )
            resp.close()
        except Exception as exc:  # noqa: BLE001
            observations.append({"error": f"{type(exc).__name__}: {exc}"})
        time.sleep(1)
    return observations


def evaluate_probe(entry: dict, observations: list[dict]) -> tuple[str, str]:
    """Return (verdict, message). verdict in {ok, fail, unchecked, control_weak}."""
    errors = [o["error"] for o in observations if "error" in o]
    if errors:
        return "unchecked", f"could not reach {entry['path']}: {errors[0]}"

    statuses = [o.get("cf_cache_status") for o in observations]
    if any(s is None for s in statuses):
        return "unchecked", (
            f"{entry['path']} returned no cf-cache-status header — the edge "
            "verdict is unreadable, which is not the same as uncached"
        )

    hits = [s for s in statuses if s and s.upper() == "HIT"]

    if entry["expect"] == "bypass":
        if hits:
            ages = [o.get("age") for o in observations]
            stamps = [o.get("worker_stamp") for o in observations]
            return "fail", (
                f"{entry['path']} was served from the edge cache "
                f"(cf-cache-status={statuses}, age={ages}, "
                f"x-dc-response-time={stamps}). On a HIT the Pages worker never "
                f"runs. Rule {entry['rule']} is not holding: {entry['why']}"
            )
        return "ok", f"{entry['path']} not cached (cf-cache-status={statuses})"

    # expect == "cached": the discrimination control.
    if hits:
        return "ok", f"{entry['path']} HIT as expected — probe is discriminating"
    return "control_weak", (
        f"CONTROL did not reproduce a HIT on {entry['path']} "
        f"(cf-cache-status={statuses}). The bypass results above are therefore "
        "weaker evidence: 'no HIT observed' by a probe that may not be able to "
        "observe a HIT at all."
    )


def run_probe() -> tuple[int, bool]:
    """Return (failures, control_ok)."""
    failures = 0
    control_ok = True
    print("\n─── outside-in edge probe (same URL twice, NO cache-buster) ───")
    for entry in PROBE_TABLE:
        observations = _head_twice(entry["path"])
        verdict, message = evaluate_probe(entry, observations)
        icon = {"ok": "✅", "fail": "❌", "unchecked": "⚠️ ", "control_weak": "⚠️ "}[verdict]
        print(f"{icon} [{entry['expect']:>6}] {message}")
        if verdict == "fail":
            failures += 1
        elif verdict == "unchecked":
            # An unreachable bypass probe proves nothing — treat as failure.
            if entry["expect"] == "bypass":
                failures += 1
        elif verdict == "control_weak":
            control_ok = False
    covered = ", ".join(e["rule"] for e in PROBE_TABLE if e["expect"] == "bypass")
    print(
        f"   probe coverage: {covered} — every OTHER rule in the ruleset is "
        "checked by the canon diff ONLY, never by observed edge behaviour."
    )
    return failures, control_ok


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="also run the outside-in edge probe")
    parser.add_argument("--repin", action="store_true", help="rewrite canon from the live ruleset")
    args = parser.parse_args()

    if not CANON_PATH.exists():
        print(f"❌ SELF-CHECK: canon file missing at {CANON_PATH}", file=sys.stderr)
        return EXIT_SELF
    try:
        canon = json.loads(CANON_PATH.read_text())
    except ValueError as exc:
        print(f"❌ SELF-CHECK: canon file is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_SELF

    zone_id = os.environ.get("CF_ZONE_ID") or canon.get("zone_id", "")
    ruleset_id = canon.get("ruleset_id", "")
    if not zone_id or not ruleset_id:
        print("❌ SELF-CHECK: canon has no zone_id/ruleset_id to query", file=sys.stderr)
        return EXIT_SELF

    live, error = fetch_live_ruleset(zone_id, ruleset_id)
    if error:
        print(
            "⚠️  COULD NOT CHECK — this is NOT a pass.\n"
            f"   {error}",
            file=sys.stderr,
        )
        return EXIT_UNCHECKED

    live_rules = normalize_rules(live.get("rules", []))

    if args.repin:
        canon_out = {
            "_comment": (
                "PINNED Cloudflare cache ruleset for dchub.cloud. Regenerate with "
                "`python3 scripts/check_cf_cache_ruleset.py --repin` AFTER an "
                "intentional rule change, and land the diff in a PR — this file is "
                "the only code review an edge change gets. Rule ORDER is "
                "significant: Cache Rules are last-match-wins."
            ),
            "zone_name": canon.get("zone_name", "dchub.cloud"),
            "zone_id": zone_id,
            "ruleset_id": ruleset_id,
            "phase": live.get("phase", ""),
            "pinned_version": str(live.get("version", "")),
            "pinned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rules": live_rules,
        }
        CANON_PATH.write_text(json.dumps(canon_out, indent=2, sort_keys=False) + "\n")
        print(
            f"✅ re-pinned {len(live_rules)} rules at ruleset version "
            f"{live.get('version')} → {CANON_PATH.name}\n"
            "   Review the git diff before committing: every line of it is a "
            "change to what the edge serves."
        )
        return EXIT_OK

    floor_error = check_canon_floor(canon)
    if floor_error:
        print(f"❌ SELF-CHECK: {floor_error}", file=sys.stderr)
        return EXIT_SELF

    findings = diff_ruleset(canon, live_rules)

    live_version = str(live.get("version", ""))
    pinned_version = str(canon.get("pinned_version", ""))
    if live_version != pinned_version and not findings:
        # GAUGE, not GUARD — see the module docstring.
        print(
            f"::notice::cache ruleset version moved {pinned_version} → "
            f"{live_version} with no rule change (no-op write or a change that "
            "was reverted). Not a failure."
        )

    if findings:
        print(
            f"❌ CACHE RULESET DRIFT — {len(findings)} finding(s). Live version "
            f"{live_version}, canon pinned at {pinned_version}.\n"
            "   Nothing in this repo other than this canon records what the "
            "edge is configured to do, so an unreviewed dashboard edit shows up "
            "here or nowhere.\n",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"   • {finding}", file=sys.stderr)
        print(
            "\n   If the change was intentional: run "
            "`python3 scripts/check_cf_cache_ruleset.py --repin` and land the "
            "canon diff in a PR.",
            file=sys.stderr,
        )
        return EXIT_DRIFT

    print(
        f"✅ cache ruleset matches canon: {len(live_rules)} rules, same order, "
        f"same expressions/actions (ruleset {ruleset_id[:8]}, version {live_version})."
    )

    if args.probe:
        failures, control_ok = run_probe()
        if failures:
            print(f"\n❌ {failures} probe failure(s) — see above.", file=sys.stderr)
            return EXIT_DRIFT
        if not control_ok:
            print("::warning::edge probe control was inconclusive this run.")
        print("✅ edge probe: no bypass path was served from cache.")

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
