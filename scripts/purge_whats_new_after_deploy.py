#!/usr/bin/env python3
"""purge_whats_new_after_deploy.py — after a deploy that changes What's New, make
the URL the page fetches serve the new cards, or fail loudly (2026-09-11).

WHY THIS EXISTS
===============
dchub-backend#4405 added seven cards to data/platform_updates.json and merged at
18:50:34Z. The origin served them and a cache-busted read of the feed served
them, but https://dchub.cloud/whats-new did not. The page fetches
/api/v1/whats-new?d=<UTC date>, and that exact URL kept returning the previous
fourteen cards:

    18:51:31Z  the OLD build answers a request during the deploy swap
    ~18:52:53Z the Pages worker stores that body in KV (warm tier, fresh 300s)
    18:55:42Z  a zone purge of the URL runs; the next request is answered from
               the worker's still-fresh KV copy, and the zone stores it again
    ~19:00Z    the KV copy has expired; a second zone purge finally sticks

There are two copies in front of the origin, in order: the worker's KV copy,
then the zone's copy of the worker's answer. Purging the zone helps only once
the KV copy is the new one. Purged earlier, the zone re-stores the old body.

WHAT IT DOES
============
1. Reads the ORIGIN's cards for each URL until consecutive reads agree. The
   service runs 2 replicas, and a swap can leave one on the old build.
2. Watches the worker's KV copy of the SAME URLs, skipping only the zone. A
   cookie whose name contains "sid" matches zone rule 24's cookie family on
   /api/, and the worker's credential check (auth_token|token|dchub_token)
   does not match it, so the worker treats the probe as anonymous and answers
   from its KV lane. It waits until every copy carries the origin's cards. If
   the zone answers a probe anyway (cf-cache-status HIT), the probe cannot see
   the KV lane, so it waits out the tier's freshness instead of trusting it.
3. Purges the zone's copy of each URL (Cloudflare purge_cache with files=; the
   cache key includes the query string, so the exact ?d= URL is listed).
4. Reads each URL the way the page does, with no cookie and no cache-buster,
   and requires the origin's cards on every read. A stale read is a straggler
   from the swap; the script repeats from step 2, up to --rounds times, inside
   --budget-s seconds in all.

Scope: --edge (https://dchub.cloud), the host the page loads from.

Exit codes: 0 every URL serves the origin's cards · 1 still stale after
--rounds, or the budget ran out · 2 cannot purge (no token or zone id, or
Cloudflare refused) · 3 cannot observe (the origin never answered consistently).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANON = ROOT / "scripts" / "cf_cache_ruleset_canon.json"

EDGE = "https://dchub.cloud"
ORIGIN = "https://dchub-backend-production.up.railway.app"
FEED = "/api/v1/whats-new"
USER_AGENT = "dchub-whats-new-post-deploy-purge/1.0 (+https://github.com/azmartone67/dchub-backend)"

# dchub-frontend/_worker.js getRouteTier(): /api/v1/whats-new matches no
# ROUTE_CACHE_MAP entry and falls through to CACHE_TIERS.warm (kvFreshTtl 300).
KV_FRESH_TTL_S = 300
ZONE_BYPASS_COOKIE = "qa_probe_sid=1"

ORIGIN_CONFIRM = 3
ORIGIN_DEADLINE_S = 180
KV_WAIT_MARGIN_S = 120
PROBE_INTERVAL_S = 15
VERIFY_READS = 3
VERIFY_INTERVAL_S = 5
# The whole run. .github/workflows/whats-new-post-deploy-purge.yml gives the job
# timeout-minutes that covers the 600s deploy wait plus this, so the script ends
# with its own verdict instead of being killed without one.
BUDGET_S = 1500


def page_paths(now: datetime.datetime) -> list[str]:
    """The URLs to repair. whats-new.html builds its key with
    new Date().toISOString().slice(0, 10), which is the UTC date. The bare feed
    is the one agents call."""
    day = now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d")
    return [f"{FEED}?d={day}", FEED]


def card_ids(body) -> list | None:
    if not isinstance(body, dict) or not isinstance(body.get("platform"), list):
        return None
    return [c.get("id") if isinstance(c, dict) else None for c in body["platform"]]


def zone_id_from_canon(path: pathlib.Path = CANON) -> str:
    try:
        return str(json.loads(path.read_text()).get("zone_id") or "").strip()
    except (OSError, ValueError):
        return ""


class Http:
    """The two network seams. Tests replace this whole object."""

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def get_json(self, url: str, cookie: str | None = None):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        r = self.session.get(url, headers=headers, timeout=30, allow_redirects=False)
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, body

    def purge(self, zone: str, token: str, urls: list[str]):
        r = self.session.post(
            f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"files": urls}, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = {}
        ok = r.status_code == 200 and bool(body.get("success"))
        return ok, f"HTTP {r.status_code}, errors {json.dumps(body.get('errors'))[:200]}"


def origin_cards(http, url, *, sleep, monotonic, deadline):
    """The origin's card ids at `url` once ORIGIN_CONFIRM consecutive reads agree."""
    last, streak = None, 0
    while monotonic() < deadline:
        try:
            status, _, body = http.get_json(url)
            ids = card_ids(body) if status == 200 else None
        except requests.RequestException:
            ids = None
        if ids is None:
            last, streak = None, 0
        elif ids == last:
            streak += 1
        else:
            last, streak = ids, 1
        if streak >= ORIGIN_CONFIRM:
            return last
        sleep(PROBE_INTERVAL_S if ids is None else VERIFY_INTERVAL_S)
    return None


def worker_copies(http, edge, paths, want, *, sleep, monotonic, deadline, log) -> dict:
    """{path: 'current' | 'blind' | 'timeout'}. Every unresolved URL is probed on
    each pass, so two URLs cost one wait, not two in a row."""
    states = {}
    while True:
        for path in paths:
            if path in states:
                continue
            try:
                status, headers, body = http.get_json(edge + path, cookie=ZONE_BYPASS_COOKIE)
            except requests.RequestException as e:
                log(f"  worker copy {path}: {type(e).__name__}")
                continue
            zone = (headers.get("cf-cache-status") or "").upper()
            if zone == "HIT":
                states[path] = "blind"
                continue
            ids = card_ids(body) if status == 200 else None
            current = ids == want[path]
            log(f"  worker copy {path}: HTTP {status}, zone {zone or '-'}, kv "
                f"{headers.get('x-cache-kv', '-')} age {headers.get('x-cache-kv-age', '-')}: "
                f"{'current' if current else 'stale'}")
            if current:
                states[path] = "current"
        if len(states) == len(paths):
            return states
        if monotonic() + PROBE_INTERVAL_S > deadline:
            return {p: states.get(p, "timeout") for p in paths}
        sleep(PROBE_INTERVAL_S)


def page_serves(http, page_url, want, *, sleep) -> bool:
    """Every one of VERIFY_READS plain reads, the way the page reads, carries `want`."""
    for i in range(VERIFY_READS):
        try:
            status, _, body = http.get_json(page_url)
        except requests.RequestException:
            return False
        if status != 200 or card_ids(body) != want:
            return False
        if i < VERIFY_READS - 1:
            sleep(VERIFY_INTERVAL_S)
    return True


def run(http, token, zone, paths, *, edge=EDGE, origin=ORIGIN, rounds=3, budget_s=BUDGET_S,
        sleep=time.sleep, monotonic=time.monotonic, log=print) -> int:
    if not token:
        log("::error::CLOUDFLARE_API_TOKEN is not set, so nothing was purged. A purge "
            "step that skips quietly is how the page kept showing the old cards.")
        return 2
    if not zone:
        log("::error::no Cloudflare zone id (CLOUDFLARE_ZONE_ID, or zone_id in "
            "scripts/cf_cache_ruleset_canon.json), so nothing was purged.")
        return 2

    start = monotonic()
    hard = start + budget_s
    want = {}
    for path in paths:
        ids = origin_cards(http, origin + path, sleep=sleep, monotonic=monotonic,
                           deadline=min(start + ORIGIN_DEADLINE_S, hard))
        if ids is None:
            log(f"::error::the origin never answered {path} consistently, so there is no "
                "way to tell which cards are current; nothing was purged.")
            return 3
        want[path] = ids
        log(f"origin {path}: {len(ids)} cards, first {ids[0] if ids else None}")

    urls = [edge + p for p in paths]
    for rnd in range(1, rounds + 1):
        if monotonic() >= hard:
            log(f"::error::the {budget_s}s budget ran out before round {rnd}; the page's "
                "URLs may still serve old cards")
            return 1
        states = worker_copies(http, edge, paths, want, sleep=sleep, monotonic=monotonic,
                               deadline=min(monotonic() + KV_FRESH_TTL_S + KV_WAIT_MARGIN_S, hard),
                               log=log)
        if "blind" in states.values():
            log(f"::warning::the zone answered the worker-copy probe, so it cannot see the KV "
                f"lane; waiting out the KV freshness ({KV_FRESH_TTL_S}s) before purging")
            sleep(min(KV_FRESH_TTL_S + 30, max(0.0, hard - monotonic())))
        for path, state in states.items():
            if state == "timeout":
                log(f"::warning::the worker's copy of {path} never matched the origin; purging "
                    "anyway and letting the page read decide")
        ok, detail = http.purge(zone, token, urls)
        if not ok:
            log(f"::error::Cloudflare refused the purge: {detail}")
            return 2
        log(f"round {rnd}: purged {len(urls)} URL(s)")
        sleep(VERIFY_INTERVAL_S)
        stale = [p for p in paths if not page_serves(http, edge + p, want[p], sleep=sleep)]
        if not stale:
            log(f"✅ the page's URLs serve the origin's cards (round {rnd})")
            return 0
        log(f"round {rnd}: still stale on {', '.join(stale)}; a request answered by the old "
            "build during the swap re-stored it")
    log(f"::error::the page's URLs still serve old cards after {rounds} round(s)")
    return 1


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: argparse otherwise accepts any unambiguous prefix
    # (--edg for --edge), which hides a typo in the workflow command from the
    # command-line test.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--edge", default=EDGE, help="the public host the page loads from")
    ap.add_argument("--origin", default=ORIGIN, help="the Railway origin, read as the truth")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--budget-s", type=float, default=BUDGET_S)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    zone = (os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip() or zone_id_from_canon()
    paths = page_paths(datetime.datetime.now(datetime.timezone.utc))
    return run(Http(), token, zone, paths, edge=args.edge.rstrip("/"),
               origin=args.origin.rstrip("/"), rounds=args.rounds, budget_s=args.budget_s)


if __name__ == "__main__":
    sys.exit(main())
