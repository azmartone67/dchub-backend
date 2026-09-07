#!/usr/bin/env python3
"""Guard: a tier-varying route must not be cacheable for anonymous callers.

Why this exists (2026-09-07)
----------------------------
Rule 24 (`b3ce82fb`) bypasses the cache when a request carries `x-api-key` or
`?api_key=`. That ended the leak class for API-key callers. It did NOT end the
class, because **tier is not only resolved from `x-api-key`**: across
`routes/`, `Authorization` is read 27 times and the `dchub_token` cookie 8
times. Rule 24 keys on NEITHER.

So a PRO caller authenticating by Bearer token or session cookie on an
`/api/v1/*` route matches rule 2 instead:

    "Cache Public API"  cache: true, edge_ttl override_origin 3600,
                        NO custom cache key -> the key is the URL alone

Their tier-specific response is stored under a URL-only key for an hour and
served to whoever asks next, including an anonymous caller. That is the same
mechanism as the 2026-09-06 `export_facility_csv` incident, on a different
credential channel.

Each of rules 9/11/12/13/17/19/20/21/22/23 is one endpoint prefix patched into
the bypass list by hand after someone noticed. Nothing has ever checked that a
NEW tier-gated route gets the same treatment. This does.

★ AN ANON 401 DOES NOT DISPROVE A FINDING. Rule 2 carries
`status_code_ttl: {400-599: -1}`, so an anonymous error response is not stored
and Cloudflare reports `cf-cache-status: BYPASS`. Measured 2026-09-07:

    /api/v1/brief/buyer            200  MISS -> HIT     <- cached, confirmed
    /api/v1/account/entitlements   401  BYPASS, BYPASS  <- error, not stored
    /api/v1/alerts                 404  BYPASS, BYPASS  <- error, not stored

The BYPASS on the last two is the STATUS CODE, not a bypass rule. Their
exposure lands the moment a cookie- or Bearer-authenticated caller produces a
200 on that URL and it is stored under the URL-only key. Curling a path
anonymously, seeing BYPASS and concluding "covered" is the false refutation
this paragraph exists to prevent — it is exactly how the CSV incident looked
right up until it did not.

★ WHAT THIS IS NOT. It is a STATIC finding: the ruleset says these paths are
anon-cacheable and the code says their responses vary by tier. Nothing here
observes a leak, and this script deliberately does not try to produce one —
doing so would mean caching real PRO data at the edge for anonymous callers.
Do not report its output as "observed".

★ RATCHET, NOT A WALL. 45 gaps existed the day this shipped. A guard that fails
on all of them blocks every PR and gets switched off within a week, so known
gaps live in a baseline file and only NEW ones fail. Fixing one and removing it
from the baseline is the intended direction; `--update-baseline` rewrites it.

★ SELF-CHECK BEFORE VERDICT. The evaluator must reproduce four dispositions
that were MEASURED against the live edge (see ORACLE). If it cannot, the script
exits 2 without reporting anything, because an evaluator that disagrees with
observed behaviour cannot be trusted to find gaps in it.

Usage
-----
    python3 scripts/check_anon_tier_cache_coverage.py
    python3 scripts/check_anon_tier_cache_coverage.py --update-baseline
    python3 scripts/check_anon_tier_cache_coverage.py --list   # every verdict
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CANON = ROOT / "scripts" / "cf_cache_ruleset_canon.json"
BASELINE = ROOT / "scripts" / "anon_tier_cache_baseline.json"

sys.path.insert(0, str(ROOT / "scripts"))
import cf_expression as cfx  # noqa: E402

EXIT_OK, EXIT_NEW_GAP, EXIT_SELF = 0, 1, 2

# ★ MEASURED at the edge on 2026-09-07 with two consecutive un-cache-busted
# GETs each. /api/v1/stats returned `HIT age=1148` with a frozen
# x-dc-response-time; the other three returned DYNAMIC twice.
ORACLE = {
    "/api/v1/stats": "cached",
    "/api/v1/health": "bypass",
    "/grid": "bypass",
    "/api/v1/mcp/tools/export_facility_csv": "bypass",
}

# Gating expressed as a decorator on the view.
GATE_DECORATORS = {
    "require_plan", "_require_plan", "_lazy_require_plan", "mcp_tier",
    "soft_gate", "_lazy_protect_data", "require_auth",
}
# Gating expressed INLINE in the body. Rule 13's own description records why
# this set is needed: "inline-gated routes invisible to decorator ratchet".
GATE_CALLS = {
    "_resolve_caller_tier", "_caller_tier", "_detect_caller_tier", "resolve_tier",
    "_resolve_tier", "_detect_tier", "_is_pro", "_gate", "_gate_response",
    "jsonify_gated_snapshot", "_dcpi_is_paid", "_dcpi_gated_meta", "_tier",
    "require_plan", "_require_plan",
}

# ★ FLOORS. A scan that can silently find nothing is a scan that always passes.
MIN_ROUTES_SCANNED = 2000
MIN_GATED_ROUTES = 100


def _load_route_extractor():
    """Reuse the route-table checker's blueprint-prefix resolution.

    Prefix semantics (register_blueprint's url_prefix WINS over the ctor's,
    unregistered blueprints serve nothing) are subtle and already solved there.
    A second implementation would drift; tests pin the two to agree.
    """
    path = ROOT / "scripts" / "check_route_table_coherence.py"
    spec = importlib.util.spec_from_file_location("_rtc", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decorator_names(node) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
            names.add(dec.func.id)
        elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            names.add(dec.func.attr)
        elif isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.add(dec.attr)
    return names


def _get_capable(dec: ast.Call, rtc) -> bool:
    """Only GET/HEAD responses are cacheable, so a POST-only route is not exposed."""
    for kw in dec.keywords:
        if kw.arg != "methods":
            continue
        if not isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
            return True  # computed methods — assume exposed rather than assume safe
        methods = {rtc._const_str(e) for e in kw.value.elts}
        return bool(methods & {"GET", "HEAD", None})
    return True  # Flask's default is GET


def scan_routes(root: pathlib.Path = ROOT):
    """Return (gated, total_routes, unparsed_files)."""
    rtc = _load_route_extractor()
    trees, unparsed = {}, []
    for path in rtc._python_files(root):
        try:
            trees[path] = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            unparsed.append(str(path.relative_to(root)))
    registered, reg_prefix = rtc._register_blueprint_facts(trees)

    gated: dict[str, dict] = {}
    total = 0
    for file_path, tree in trees.items():
        ctor_prefix: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                    and rtc._is_blueprint_call(node.value):
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg == "url_prefix":
                        prefix = rtc._const_str(kw.value) or ""
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        ctor_prefix[target.id] = prefix

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                if dec.func.attr not in rtc.ROUTE_DECORATORS or not dec.args:
                    continue
                rel = rtc._const_str(dec.args[0])
                if not rel or not rel.startswith("/"):
                    continue
                owner = dec.func.value.id if isinstance(dec.func.value, ast.Name) else None
                if owner in ctor_prefix and owner not in registered:
                    continue
                full = rtc._join(reg_prefix.get(owner) or ctor_prefix.get(owner, ""), rel)
                total += 1
                if not _get_capable(dec, rtc):
                    continue
                calls = {
                    n.func.id for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                evidence = sorted(
                    (_decorator_names(node) & GATE_DECORATORS) | (calls & GATE_CALLS)
                )
                if evidence and full not in gated:
                    gated[full] = {
                        "path": full,
                        "file": str(file_path.relative_to(root)),
                        "gated_by": evidence,
                    }
    return gated, total, unparsed


def concrete_path(path: str) -> str:
    """Flask params -> a concrete sample, so prefix rules evaluate honestly."""
    return re.sub(r"<[^>]+>", "sample", path)


def self_check(rules: list[dict]) -> list[str]:
    problems = []
    for rule in rules:
        try:
            cfx.parse(rule["expression"])
        except cfx.ParseError as exc:
            problems.append(f"rule {rule['position']} [{rule['id'][:8]}] will not parse: {exc}")
    for path, expected in ORACLE.items():
        verdict, _, _ = cfx.disposition(rules, path)
        if verdict != expected:
            problems.append(
                f"ORACLE MISMATCH on {path}: measured at the edge as {expected!r}, "
                f"evaluator says {verdict!r}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--list", action="store_true", help="print every verdict")
    args = parser.parse_args()

    canon = json.loads(CANON.read_text())
    rules = canon["rules"]

    problems = self_check(rules)
    if problems:
        print("❌ SELF-CHECK FAILED — refusing to report gaps:", file=sys.stderr)
        for problem in problems:
            print(f"   • {problem}", file=sys.stderr)
        return EXIT_SELF

    gated, total, unparsed = scan_routes()
    if unparsed:
        print(f"::warning::route scanner could not parse {len(unparsed)} file(s): "
              f"{', '.join(unparsed[:5])}")
    if total < MIN_ROUTES_SCANNED or len(gated) < MIN_GATED_ROUTES:
        print(
            f"❌ SELF-CHECK FAILED — scan found {total} routes and {len(gated)} "
            f"tier-gated (floors: {MIN_ROUTES_SCANNED}/{MIN_GATED_ROUTES}). A scan "
            "that finds nothing must not report a clean bill.",
            file=sys.stderr,
        )
        return EXIT_SELF

    findings: dict[str, dict] = {}
    counts = {"bypass": 0, "cached": 0, "no-rule": 0, "unknown": 0}
    for path in sorted(gated):
        verdict, winner, _ = cfx.disposition(rules, concrete_path(path))
        counts[verdict] += 1
        if args.list:
            print(f"  {verdict:<8} {path}")
        # `no-rule` means Cloudflare's defaults apply and the origin's own
        # cache headers are respected — a weaker exposure, reported not failed.
        if verdict in ("cached", "unknown"):
            findings[path] = {
                **gated[path],
                "verdict": verdict,
                "winning_rule": winner["position"] if winner else None,
                "winning_rule_id": winner["id"][:8] if winner else None,
            }

    print(
        f"scanned {total} routes; {len(gated)} are tier-gated and GET-capable — "
        f"bypass {counts['bypass']}, cached {counts['cached']}, "
        f"no-rule {counts['no-rule']}, unknown {counts['unknown']}"
    )

    if args.update_baseline:
        BASELINE.write_text(json.dumps({
            "_comment": (
                "Tier-gated, GET-capable routes that the cache ruleset leaves "
                "ANON-CACHEABLE. These are known gaps, not approved ones — the "
                "guard fails on NEW entries only. Removing a line (by adding a "
                "bypass rule) is the intended direction. Regenerate with "
                "scripts/check_anon_tier_cache_coverage.py --update-baseline."
            ),
            "generated_against_ruleset_version": canon.get("pinned_version"),
            "known_gaps": sorted(findings),
        }, indent=2) + "\n")
        print(f"✅ baseline rewritten with {len(findings)} known gap(s)")
        return EXIT_OK

    known = set(json.loads(BASELINE.read_text())["known_gaps"]) if BASELINE.exists() else set()
    new_gaps = sorted(set(findings) - known)
    fixed = sorted(known - set(findings))

    if fixed:
        print(f"\n✅ {len(fixed)} baselined gap(s) are now covered — drop them from "
              f"the baseline: {', '.join(fixed[:6])}")

    if new_gaps:
        print(
            f"\n❌ {len(new_gaps)} NEW anon-cacheable tier-gated route(s).\n"
            "   Their responses vary by caller tier, and the edge will cache an\n"
            "   anonymous or cookie/Bearer-authenticated response under a "
            "URL-only key.\n"
            "   Fix: append a bypass rule in Cloudflare covering the path, then\n"
            "   re-pin with scripts/check_cf_cache_ruleset.py --repin.\n",
            file=sys.stderr,
        )
        for path in new_gaps:
            item = findings[path]
            print(
                f"   • {path}\n"
                f"       cached by rule {item['winning_rule']} "
                f"[{item['winning_rule_id']}] — gated via {', '.join(item['gated_by'])} "
                f"in {item['file']}",
                file=sys.stderr,
            )
        return EXIT_NEW_GAP

    print(f"✅ no NEW anon-cacheable tier-gated routes ({len(known)} known gap(s) baselined).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
