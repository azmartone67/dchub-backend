#!/usr/bin/env python3
"""Push the pinned cache-ruleset canon TO Cloudflare — the mirror of `--repin`.

Why this exists (2026-09-11)
----------------------------
`check_cf_cache_ruleset.py --repin` writes the LIVE zone into canon, which is
the right direction when a human edited the dashboard. There was no tool for
the other direction, and that gap cost a live paywall leak:

    #4407 landed the canon change for rule 24 (the every-channel credential
    bypass, previously scoped to `/api/`) so that it also covers rule 18's
    tier-varying HTML set. The RULE ITSELF was never applied, because applying
    it meant hand-editing a 25-rule last-match-wins set in the dashboard.
    Measured 22:00Z that day, after the merge: an anonymous request was still
    served the PAID render of a market brief, and a paid request was still
    served the anonymous "Unlock with PRO" render.

So the canon said one thing, the zone did another, and the only way to close
the gap was a careful manual edit nobody wanted to make at 10pm. This script
makes it a reviewed, verified, repeatable operation.

What it will and will not do
----------------------------
It PATCHES individual rules so that the live zone matches canon. It REFUSES —
without writing anything — when the difference is anything other than field
edits on rules both sides already know about:

  * a rule the zone has and canon does not (someone added one in the dashboard)
  * a rule canon pins and the zone does not have (canon is from another zone,
    or a rule was deleted)
  * a different ORDER (Cache Rules are LAST-MATCH-WINS, so a reorder changes
    behaviour even when every rule compares equal — and re-ordering is not
    something this script is allowed to do by accident)
  * more than `--cap` rules differing (default 3): a canon that has drifted far
    from the zone is a review problem, not a one-click problem
  * any rule outside `--expect <id prefix>`, when that flag is given. The
    workflow passes it, so an unrelated dashboard edit made since the canon was
    pinned REFUSES the run instead of being silently reverted.

★ VERIFY BY READING BACK, NOT BY READING THE WRITE. The PATCH response carries
the updated ruleset and it would be very easy to declare success from it. This
script throws that body away and issues a FRESH GET, then re-runs the same
comparison. A write that reports 200 and did not take is exactly the failure
this whole guard family exists to catch.

★ CANNOT-WRITE IS NOT A PASS. No credential, or every credential refused, exits
3 with the measured evidence (which variable, which HTTP status) — never 0.
`cf-purge.yml` exiting 0 on a missing secret is how a purge job ran as a no-op
in 6 of 8 cases while reporting success.

★ EVERY CONFIGURED TOKEN IS TRIED. The read guard documents a fallback chain;
this one exercises it, because a token that can READ cache rules may not be
allowed to EDIT them, and which of the three repo secrets can write is not
knowable from here without asking Cloudflare.

Usage
-----
    python3 scripts/apply_cf_cache_ruleset.py                   # plan only
    python3 scripts/apply_cf_cache_ruleset.py --apply           # write it
    python3 scripts/apply_cf_cache_ruleset.py --apply --expect b3ce82fb

Env: the first of CF_CACHE_RULES_TOKEN, CLOUDFLARE_API_TOKEN, CF_TOKEN that can
perform the write is used, and the run reports WHICH one did. Token VALUES are
never printed.

Exit codes
----------
    0  the zone matches canon (already did, or was applied and verified)
    1  applied, but the read-back still does not match canon
    2  refused — canon floor failed, or the difference is not one this may apply
    3  could not look, or could not write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # importable both as a script and via spec_from_file_location
    sys.path.insert(0, str(_HERE))

from check_cf_cache_ruleset import (  # noqa: E402  - path set above
    API_ROOT,
    CANON_PATH,
    TOKEN_ENV_VARS,
    _canonical,
    check_canon_floor,
    normalize_rules,
)

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_REFUSED = 2
EXIT_CANNOT = 3

# Fields this script is willing to PATCH. `id` and position are deliberately
# absent: changing either is a structural edit, and structural edits refuse.
PATCHED_FIELDS = ("expression", "action", "action_parameters", "description", "enabled")

# ★ A ceiling on blast radius. One click may fix a rule; it may not rewrite the
# ruleset. A canon that differs from the zone in more places than this wants a
# human reading the diff, not a button.
DEFAULT_CAP = 3


# --------------------------------------------------------------------------
# pure planning (no network; this is what the tests exercise)
# --------------------------------------------------------------------------
def plan_changes(canon, live_rules, *, expect=None, cap=DEFAULT_CAP):
    """Return (changes, refusals).

    `changes` is the ordered list of rules to PATCH, each
    `{id, position, description, fields: {name: (live_value, canon_value)}}`.
    `refusals` is non-empty when the caller must not write at all — in which
    case `changes` is advisory output only and MUST NOT be applied.
    """
    canon_rules = canon.get("rules", []) or []
    canon_by_id = {r.get("id"): r for r in canon_rules}
    live_by_id = {r.get("id"): r for r in live_rules}
    refusals: list[str] = []

    for rule in live_rules:
        if rule.get("id") not in canon_by_id:
            refusals.append(
                f"the zone carries a rule canon does not know: position "
                f"{rule.get('position')} [{str(rule.get('id'))[:8]}] "
                f"{rule.get('action')} — {str(rule.get('description'))[:70]!r}. "
                f"Someone added it outside a PR. Re-pin and review it before "
                f"pushing canon over the top of it."
            )
    for rule in canon_rules:
        if rule.get("id") not in live_by_id:
            refusals.append(
                f"canon pins a rule the zone does not have: position "
                f"{rule.get('position')} [{str(rule.get('id'))[:8]}] — "
                f"{str(rule.get('description'))[:70]!r}. This script only edits "
                f"rules that already exist; creating one changes rule ORDER, "
                f"and order decides which rule wins."
            )

    canon_order = [r.get("id") for r in canon_rules if r.get("id") in live_by_id]
    live_order = [r.get("id") for r in live_rules if r.get("id") in canon_by_id]
    if canon_order != live_order:
        refusals.append(
            "the shared rules are in a different ORDER in canon than in the "
            "zone. Cache Rules are last-match-wins, so a reorder alone can "
            "re-open a bypass. Reordering is not something this script does.\n"
            f"       canon: {[str(i)[:8] for i in canon_order]}\n"
            f"       zone : {[str(i)[:8] for i in live_order]}"
        )

    changes = []
    for rule_id, canon_rule in canon_by_id.items():
        live_rule = live_by_id.get(rule_id)
        if live_rule is None:
            continue
        fields = {}
        for field in PATCHED_FIELDS:
            if _canonical(canon_rule.get(field)) != _canonical(live_rule.get(field)):
                fields[field] = (live_rule.get(field), canon_rule.get(field))
        if fields:
            changes.append(
                {
                    "id": rule_id,
                    "position": canon_rule.get("position") or 0,
                    "description": canon_rule.get("description", ""),
                    "fields": fields,
                }
            )
    changes.sort(key=lambda c: c["position"])

    if expect:
        stray = [c["id"] for c in changes if not str(c["id"]).startswith(expect)]
        if stray:
            refusals.append(
                f"--expect {expect} names the only rule this run may touch, but "
                f"these also differ from canon: {[str(i)[:8] for i in stray]}. "
                f"That is an edit made outside this repo since canon was pinned; "
                f"applying canon would silently revert it. Nothing was written."
            )

    if len(changes) > cap:
        refusals.append(
            f"{len(changes)} rules differ from canon, above the cap of {cap}. A "
            f"canon this far from the zone is a review, not a button. Read the "
            f"plan above, then re-run with --cap {len(changes)} if every line of "
            f"it is intended."
        )

    return changes, refusals


def describe_change(change, *, width=220):
    """Render one planned change for the log, live value then canon value."""
    lines = [
        f"rule position {change['position']} [{str(change['id'])[:8]}] — "
        f"{change['description'][:90]!r}"
    ]
    for field, (before, after) in sorted(change["fields"].items()):
        before_s, after_s = _canonical(before), _canonical(after)
        lines.append(f"    {field}:")
        lines.append(f"      zone  ({len(before_s)}b): {before_s[:width]}")
        lines.append(f"      canon ({len(after_s)}b): {after_s[:width]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------
def resolve_tokens(environ=None):
    """Every configured (env var name, token) pair, in intent order.

    De-duplicated by VALUE so a secret set under two names is tried once and
    reported once. Token values are never returned to a caller that logs.
    """
    env = os.environ if environ is None else environ
    out, seen = [], set()
    for name in TOKEN_ENV_VARS:
        value = (env.get(name) or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append((name, value))
    return out


def _json_body(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is just "no payload"
        return {}


def read_ruleset(http, zone_id, ruleset_id, tokens):
    """GET the live ruleset. Return (result, error). Exactly one is not None."""
    if not tokens:
        return None, (
            "NO CREDENTIAL PRESENT — none of "
            + ", ".join(TOKEN_ENV_VARS)
            + " is set in this environment. In CI that means the repo secret "
            "does not exist, or the workflow does not export it to this step."
        )
    url = f"{API_ROOT}/zones/{zone_id}/rulesets/{ruleset_id}"
    attempts = []
    for name, token in tokens:
        try:
            resp = http.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        except Exception as exc:  # noqa: BLE001 - any transport failure is "could not look"
            attempts.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if resp.status_code == 200:
            payload = _json_body(resp)
            result = payload.get("result")
            if not payload.get("success") or not isinstance(result, dict) or "rules" not in result:
                return None, (
                    f"{name} got HTTP 200 but the body carried no rules array "
                    f"(starts: {str(payload)[:200]!r})"
                )
            return result, None
        attempts.append(f"{name}: HTTP {resp.status_code}")
    return None, (
        "COULD NOT READ the cache ruleset. Tried " + "; ".join(attempts) + ". "
        "Read access needs the zone permission group that grants READ on CACHE "
        "RULES for dchub.cloud — test a candidate with "
        "`CF_CACHE_RULES_TOKEN='<token>' python3 scripts/check_cf_cache_ruleset.py` "
        "before putting it in CI."
    )


def _patch_rule(http, zone_id, ruleset_id, rule_id, body, token):
    """Return (status_code_or_None, error_string_or_None)."""
    url = f"{API_ROOT}/zones/{zone_id}/rulesets/{ruleset_id}/rules/{rule_id}"
    try:
        resp = http.patch(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code == 200 and _json_body(resp).get("success"):
        return 200, None
    detail = _json_body(resp).get("errors") or getattr(resp, "text", "")
    return resp.status_code, f"HTTP {resp.status_code}: {str(detail)[:220]}"


def apply_changes(http, zone_id, ruleset_id, canon_by_id, changes, tokens, *, log=print):
    """PATCH each planned change. Return (ok, writer_name, error).

    The first change decides which credential can write; the rest reuse it. A
    401/403 moves on to the next token, any other failure stops immediately —
    a 400 means the body was rejected and retrying with another key would only
    obscure that.
    """
    if not tokens:
        return False, None, (
            "NO CREDENTIAL PRESENT — none of " + ", ".join(TOKEN_ENV_VARS) + " is set."
        )

    writer = None
    attempts = []
    for change in changes:
        rule_id = change["id"]
        canon_rule = canon_by_id[rule_id]
        body = {field: canon_rule.get(field) for field in PATCHED_FIELDS}

        if writer is None:
            for name, token in tokens:
                status, error = _patch_rule(http, zone_id, ruleset_id, rule_id, body, token)
                if status == 200:
                    writer = (name, token)
                    log(f"   wrote rule [{str(rule_id)[:8]}] with {name}")
                    break
                attempts.append(f"{name}: {error}")
                if status not in (401, 403):
                    return False, None, (
                        f"{name} was accepted as a credential but the write "
                        f"failed on rule [{str(rule_id)[:8]}] — {error}. That is "
                        f"a rejected request body, not a permission problem, so "
                        f"no other token was tried."
                    )
            if writer is None:
                return False, None, (
                    "CANNOT WRITE — every configured credential was refused on "
                    f"rule [{str(rule_id)[:8]}]. Measured: " + "; ".join(attempts) + ".\n"
                    "   Reading this ruleset already works in CI, so the missing "
                    "piece is EDIT rather than READ on Cache Rules for "
                    "dchub.cloud. Do not reason about the permission dropdown — "
                    "mint a candidate, then TEST it before it goes near CI:\n"
                    "     CF_CACHE_RULES_TOKEN='<token>' python3 "
                    "scripts/apply_cf_cache_ruleset.py\n"
                    "   (that prints the plan and writes nothing), and when the "
                    "plan looks right, add it as the repo secret and press Run "
                    "again."
                )
            continue

        name, token = writer
        status, error = _patch_rule(http, zone_id, ruleset_id, rule_id, body, token)
        if status != 200:
            return False, name, (
                f"{name} wrote an earlier rule but failed on "
                f"[{str(rule_id)[:8]}] — {error}. The zone is now PARTIALLY "
                f"applied; the read-back below reports exactly what stuck."
            )
        log(f"   wrote rule [{str(rule_id)[:8]}] with {name}")

    return True, (writer[0] if writer else None), None


# --------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--apply", action="store_true",
        help="write the plan to Cloudflare (without this, nothing is written)")
    parser.add_argument(
        "--expect", default="",
        help="rule id prefix this run may touch; any other differing rule refuses")
    parser.add_argument(
        "--cap", type=int, default=DEFAULT_CAP,
        help=f"refuse when more than this many rules differ (default {DEFAULT_CAP})")
    return parser


def run(http, args, *, log=print, environ=None):
    if not CANON_PATH.exists():
        log(f"❌ REFUSED: canon file missing at {CANON_PATH}")
        return EXIT_REFUSED
    try:
        canon = json.loads(CANON_PATH.read_text())
    except ValueError as exc:
        log(f"❌ REFUSED: canon file is not valid JSON: {exc}")
        return EXIT_REFUSED

    floor_error = check_canon_floor(canon)
    if floor_error:
        log(f"❌ REFUSED: {floor_error}")
        return EXIT_REFUSED

    env = os.environ if environ is None else environ
    zone_id = env.get("CF_ZONE_ID") or canon.get("zone_id", "")
    ruleset_id = canon.get("ruleset_id", "")
    if not zone_id or not ruleset_id:
        log("❌ REFUSED: canon has no zone_id/ruleset_id to address")
        return EXIT_REFUSED

    tokens = resolve_tokens(env)
    live, error = read_ruleset(http, zone_id, ruleset_id, tokens)
    if error:
        log(f"⚠️  COULD NOT LOOK — this is NOT a pass.\n   {error}")
        return EXIT_CANNOT

    live_rules = normalize_rules(live.get("rules", []))
    changes, refusals = plan_changes(
        canon, live_rules, expect=args.expect or None, cap=args.cap)

    log(
        f"zone {zone_id[:8]} ruleset {ruleset_id[:8]}: {len(live_rules)} live rules, "
        f"version {live.get('version')}; canon pinned at {canon.get('pinned_version')} "
        f"({canon.get('pinned_at')})"
    )

    if changes:
        log(f"\n─── plan: {len(changes)} rule(s) would change, zone → canon ───")
        for change in changes:
            log(describe_change(change))

    if refusals:
        log(f"\n❌ REFUSED — {len(refusals)} reason(s). NOTHING was written.")
        for refusal in refusals:
            log(f"   • {refusal}")
        return EXIT_REFUSED

    if not changes:
        log("\n✅ the zone already matches canon — nothing to apply.")
        return EXIT_OK

    if not args.apply:
        log(
            "\n📋 plan only: --apply was not given, so Cloudflare was NOT "
            "written to. Re-run with --apply to push this plan to the zone."
        )
        return EXIT_OK

    log(f"\n─── applying {len(changes)} rule(s) ───")
    canon_by_id = {r.get("id"): r for r in canon.get("rules", [])}
    ok, writer, error = apply_changes(
        http, zone_id, ruleset_id, canon_by_id, changes, tokens, log=log)
    if not ok and writer is None:
        log(f"\n⚠️  {error}")
        return EXIT_CANNOT
    if not ok:
        log(f"\n❌ {error}")

    # ★ Read back on a FRESH request. The PATCH response carries the updated
    # ruleset and is thrown away on purpose: a write that reports 200 and did
    # not take is the failure this verification exists to catch.
    after, error = read_ruleset(http, zone_id, ruleset_id, tokens)
    if error:
        log(
            f"\n⚠️  COULD NOT VERIFY — the write was attempted and the read-back "
            f"failed: {error}. Treat the zone as UNKNOWN until "
            f"`python3 scripts/check_cf_cache_ruleset.py` runs clean."
        )
        return EXIT_CANNOT

    after_rules = normalize_rules(after.get("rules", []))
    remaining, still_refusing = plan_changes(canon, after_rules, cap=len(live_rules) + 1)
    if not ok:
        # ★ A write that errored does not get to report success because the
        # read-back happened to look right. The operator needs the error to be
        # the outcome, not a line scrolled past above a green tick.
        log(
            f"\n❌ the write reported an error above; {len(remaining)} rule(s) "
            f"still differ from canon. Re-run once the cause is understood."
        )
        return EXIT_VERIFY_FAILED
    if remaining or still_refusing:
        log(
            f"\n❌ VERIFICATION FAILED — after the write, the zone still differs "
            f"from canon in {len(remaining)} rule(s)."
        )
        for change in remaining:
            log(describe_change(change))
        for refusal in still_refusing:
            log(f"   • {refusal}")
        return EXIT_VERIFY_FAILED

    log(
        f"\n✅ applied and verified by a fresh read: {len(changes)} rule(s) written "
        f"with {writer}, ruleset version {live.get('version')} → "
        f"{after.get('version')}, all {len(after_rules)} rules now match canon."
    )
    return EXIT_OK


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    import requests  # imported late so the pure tests need no network stack

    return run(requests, args)


if __name__ == "__main__":
    sys.exit(main())
