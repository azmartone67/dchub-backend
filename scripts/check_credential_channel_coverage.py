#!/usr/bin/env python3
"""Does every credential channel the CODE READS bypass the edge cache?

The question, and why this one
------------------------------
`check_anon_tier_cache_coverage.py` asks "is the ANONYMOUS response on this
route cacheable?". That was the right question before rule 24 named every
credential channel; it is a proxy now, because an anonymous response on a
tier-gated route is the *public* response and caching it is usually correct.

This asks the sharper one, from the other end. Rule 24 is the single
credential-keyed bypass. So: enumerate every header, cookie and query arg the
route files actually read as a CREDENTIAL, and require that a request carrying
it does not get cached — by EVALUATING the whole 25-rule chain, not by grepping
rule 24 for the name.

★ WHY EVALUATION AND NOT A SUBSTRING SEARCH. Cache Rules are last-match-wins.
"the name appears in rule 24" is not the claim that matters; "a request carrying
this channel ends up cache:false" is. A later rule can re-cache what rule 24
bypassed, and a name can appear inside a rule that never fires for the path.
Substring presence would have been green for all 22 channels that were measured
HIT at the live edge on 2026-09-07.

★ WHY OFFLINE. This reads the PINNED canon, never Cloudflare, so it needs no
token and runs on every PR. `check_cf_cache_ruleset.py` is the half that proves
canon still equals live; without that daily run this guard is checking a file.

★ 2026-09-07. Rule 24 named x-api-key, ?api_key=, authorization, x-admin-key,
x-internal-key and the dchub_token/dchub_refresh cookies. Measured on
/api/v1/stats, 6 further headers, 10 further cookies and 6 further query args
each returned `cf-cache-status: HIT age=3044` — rule 2 (override_origin 3600,
URL-only cache key) was storing them. Headers and cookies are NOT part of the
cache key, so those were being served to anonymous callers; query args ARE, so
those were stale-serving rather than cross-caller leaking. Both are now bypassed
and both are checked here.

Exit codes
----------
  0  every credential channel is bypassed
  1  a channel is cacheable, or a new channel needs classifying
  2  SELF-CHECK FAILED — the checker cannot be trusted, nothing is reported
  3  could not check (canon missing/unreadable) — never a pass
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANON = ROOT / "scripts" / "cf_cache_ruleset_canon.json"

# ── what counts as a credential ──────────────────────────────────────────────
# A channel is a credential if the response can VARY BY IT. That is the only
# property that matters: a varying response under a key that does not include
# the channel is a cross-caller leak.
CREDENTIAL_SUBSTRINGS = (
    "key", "token", "auth", "secret", "session", "sid", "cron",
    "admin", "password", "passwd", "credential", "bearer",
)

# Explicitly NOT credentials: transport, routing and telemetry. Listed by name
# so that a genuinely new channel cannot slip through as "probably fine".
NON_CREDENTIAL = {
    "accept", "accept-encoding", "accept-language", "cache-control",
    "cf-connecting-ip", "cf-ipcountry", "cf-ray", "connection", "content-encoding",
    "content-length", "content-type", "host", "if-none-match", "if-modified-since",
    "origin", "range", "referer", "referrer", "user-agent", "x-agent-name",
    "x-client-name", "x-content-gzip", "x-country", "x-dc-probe", "x-forwarded-for",
    "x-forwarded-proto", "x-real-ip", "x-request-id", "sec-fetch-user",
    "sec-fetch-mode", "sec-fetch-site", "sec-ch-ua",
    # matches CREDENTIAL_SUBSTRINGS by accident, is not a credential:
    "max_tokens",
    # webhook SIGNATURES: POST-only, verified against a body, never GET-cacheable
    "stripe-signature", "svix-id", "svix-timestamp", "svix-signature",
}

# Channels that are credential-shaped but are NOT a caching risk, each with the
# reason. Anything here is still reported, just not failed.
ACCEPTED: dict[str, str] = {}

# ★ FLOORS. A scan that can find nothing always passes.
MIN_FILES_SCANNED = 300
MIN_CHANNELS_FOUND = 15

# Representative paths on the cache:true surface (rule 2's three prefixes).
# A credentialed request to any of these must not be cached.
PROBE_PATHS = ("/api/v1/stats", "/api/v1/facilities",
               "/api/rankings/markets", "/api/news/latest",
               # ★ 2026-09-11: the tier-varying HTML surface. Every path above is
               # /api/, so this checker could not see rule 15 storing /markets/* for
               # 86400s with override_origin while rule 24 bypassed header
               # credentials only under /api/. Measured live that day: the render
               # for an X-API-Key request to a market brief was served to the next
               # anonymous request (cf-cache-status HIT, age 1). The brief and its
               # PDF resolve the caller tier; the rest sample rule 18's HTML set.
               "/markets/dallas/brief", "/markets/dallas/brief.pdf",
               "/dcpi/dallas", "/facility/example", "/grid/pjm", "/")

# ── ORACLE: dispositions MEASURED at the live edge on 2026-09-07 ─────────────
# Two consecutive un-cache-busted GETs of https://dchub.cloud/api/v1/stats.
# If the evaluator stops reproducing these, it has stopped modelling the edge
# and this checker reports nothing.
ORACLE = [
    # (kind, name, expected disposition against the CURRENT canon)
    ("header", "x-api-key", "bypass"),          # DYNAMIC before and after
    ("header", "x-admin-token", "bypass"),      # was HIT age=3044, now DYNAMIC
    ("cookie", "dchub_session", "bypass"),      # was HIT age=3044, now DYNAMIC
    ("arg", "admin_key", "bypass"),             # was MISS->HIT, now DYNAMIC
    ("anon", None, "cached"),                   # /api/v1/stats HIT — POSITIVE CONTROL
]
# ★ NEGATIVE CONTROL. A channel nobody authenticates with must still be CACHED.
# Without it, a rule 24 degraded to a bare path match ("/api/" with no credential
# clause) would bypass everything and every assertion below would pass vacuously.
NEGATIVE_CONTROL = ("header", "x-definitely-not-a-credential", "cached")

# ★ 2026-09-11 — the same kind of measurement on the tier-varying HTML surface:
# two reads of one fresh /markets/dallas/brief URL, one second apart. Anonymous
# read HIT (rule 15 caches it); a dchub_token cookie read DYNAMIC (rule 18).
HTML_ORACLE_PATH = "/markets/dallas/brief"
HTML_ORACLE = [
    ("cookie", "dchub_token", "bypass"),
    ("anon", None, "cached"),                   # POSITIVE CONTROL on the HTML surface
]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module          # so dataclasses/typing in the target resolve
    spec.loader.exec_module(module)
    return module


def _is_credential(name: str) -> bool:
    low = name.lower()
    if low in NON_CREDENTIAL:
        return False
    return any(s in low for s in CREDENTIAL_SUBSTRINGS)


def scan_channels(root: pathlib.Path, rtc):
    """AST-enumerate request.headers/cookies/args .get("name") across the tree.

    AST, not grep: a name inside a comment or a docstring is not a read, and
    `.get(VAR)` is not a literal name. Both would be noise in a regex sweep.
    """
    found: dict[tuple[str, str], set[str]] = {}
    files = 0
    dynamic: list[str] = []
    for path in rtc._python_files(root):
        files += 1
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(root))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Attribute)):
                continue
            container = node.func.value.attr          # headers | cookies | args
            kind = {"headers": "header", "cookies": "cookie", "args": "arg"}.get(container)
            if kind is None or not node.args:
                continue
            # ★ the RECEIVER must be a request. `response.headers.get("x-trial-key")`
            # in the client helper package reads a header the server SENDS, which is
            # not a channel anyone authenticates to us with. Excluding by name is
            # deliberately narrow: an unrecognised receiver is still scanned, because
            # a missed channel is a silent hole and a spurious one is only noise.
            base = node.func.value.value
            base_name = (base.id if isinstance(base, ast.Name)
                         else base.attr if isinstance(base, ast.Attribute) else "")
            # "resp" as a substring catches response / resp / mint_resp / http_resp.
            # "request" and "req" do not contain it, so request reads still count.
            if "resp" in base_name.lower() or base_name.lower() == "res":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault((kind, first.value.lower()), set()).add(rel)
            else:
                dynamic.append(rel)
    return found, files, dynamic


def _request_for(cx, kind: str, name: str, path: str):
    if kind == "header":
        return cx.Request(path, headers=[name])
    if kind == "cookie":
        return cx.Request(path, cookies=[name])
    if kind == "arg":
        return cx.Request(path, args=[name])
    return cx.Request(path)


def self_check(cx, rules) -> list[str]:
    """Reproduce the measured edge, or report nothing at all."""
    failures = []
    checks = [(k, n, e, "/api/v1/stats") for k, n, e in [*ORACLE, NEGATIVE_CONTROL]]
    checks += [(k, n, e, HTML_ORACLE_PATH) for k, n, e in HTML_ORACLE]
    for kind, name, expected, path in checks:
        req = _request_for(cx, kind, name, path)
        got, _, err = cx.disposition(rules, req)
        if got != expected:
            label = "anonymous" if kind == "anon" else f"{kind} {name}"
            failures.append(f"{label}: model says {got!r}, edge measured {expected!r}"
                            + (f" ({err})" if err else ""))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true",
                        help="print every channel and its disposition, then exit 0")
    opts = parser.parse_args()

    try:
        canon = json.loads(CANON.read_text())
        rules = canon["rules"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"❌ cannot read pinned canon {CANON}: {exc}", file=sys.stderr)
        return 3
    try:
        cx = _load("_cfexpr", "cf_expression.py")
        rtc = _load("_rtc_cc", "check_route_table_coherence.py")
    except Exception as exc:                              # noqa: BLE001
        print(f"❌ cannot load helper modules: {exc}", file=sys.stderr)
        return 3

    problems = self_check(cx, rules)
    if problems:
        print("❌ SELF-CHECK FAILED — the evaluator no longer reproduces the edge.",
              file=sys.stderr)
        for line in problems:
            print(f"   {line}", file=sys.stderr)
        print("   Reporting nothing: a model that cannot reproduce four measured\n"
              "   dispositions cannot be trusted to find a fifth.", file=sys.stderr)
        return 2

    found, files, dynamic = scan_channels(ROOT, rtc)
    if files < MIN_FILES_SCANNED:
        print(f"❌ scanned only {files} files (floor {MIN_FILES_SCANNED}) — "
              "the scan broke; refusing to report a pass.", file=sys.stderr)
        return 2

    credentials = sorted(k for k in found if _is_credential(k[1]))
    if len(credentials) < MIN_CHANNELS_FOUND:
        print(f"❌ found only {len(credentials)} credential channels "
              f"(floor {MIN_CHANNELS_FOUND}) — the classifier or the scan broke.",
              file=sys.stderr)
        return 2

    gaps, covered = [], []
    for kind, name in credentials:
        worst = None
        for path in PROBE_PATHS:
            verdict, rule, err = cx.disposition(rules, _request_for(cx, kind, name, path))
            if verdict != "bypass":
                worst = (path, verdict, rule, err)
                break
        (covered if worst is None else gaps).append((kind, name, worst))

    if opts.list:
        print(f"scanned {files} files; {len(found)} distinct channels, "
              f"{len(credentials)} classified as credentials\n")
        for kind, name, worst in sorted(covered) + sorted(gaps, key=lambda x: x[:2]):
            mark = "bypass " if worst is None else f"{worst[1].upper():<7}"
            print(f"  {mark} {kind:<7} {name:<24} "
                  f"{len(found[(kind, name)])} file(s)")
        return 0

    print(f"scanned {files} files; {len(found)} distinct request channels, "
          f"{len(credentials)} classified as credentials "
          f"({len(covered)} bypassed, {len(gaps)} cacheable)")
    if dynamic:
        print(f"   note: {len(dynamic)} non-literal .get(<var>) call(s) are invisible "
              "to this scan — a channel named by a variable is not enumerable.")

    real = [g for g in gaps if g[1] not in ACCEPTED]
    if not real:
        print("✅ every credential channel the code reads is bypassed at the edge "
              f"on all {len(PROBE_PATHS)} cache:true probe paths.")
        return 0

    print("\n❌ CREDENTIAL CHANNELS THAT ARE STILL CACHED AT THE EDGE:", file=sys.stderr)
    for kind, name, (path, verdict, rule, err) in sorted(real, key=lambda x: x[:2]):
        where = ", ".join(sorted(found[(kind, name)])[:3])
        rid = (rule or {}).get("id", "?")[:8]
        print(f"   {kind:<7} {name:<24} {verdict} on {path} "
              f"(rule {rid}){' — ' + err if err else ''}", file=sys.stderr)
        print(f"           read in: {where}", file=sys.stderr)
    print("\n   A credentialed response stored under a URL-only cache key is served\n"
          "   to the next caller. Add the channel to rule 24 — the phase is capped\n"
          "   at 25 rules, so EXTEND it, never append #26.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
