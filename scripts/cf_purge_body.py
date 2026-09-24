#!/usr/bin/env python3
"""Build the Cloudflare purge_cache body from cf-purge.yml's dispatch inputs.

Usage (the workflow passes inputs through env, never inlined into `run:`):
    URLS=... PREFIXES=... HOSTS=... python3 scripts/cf_purge_body.py OUT.json

Cloudflare takes ONE kind of target per purge request, so exactly one of the
three inputs may be set:

    urls      single-file purge; absolute URLs        https://dchub.cloud/pricing
    prefixes  host + path prefix, NO scheme           dchub-backend-production.up.railway.app/api/v1/stats
    hosts     bare hostnames; evicts EVERYTHING       dchub-backend-production.up.railway.app
              cached under each one

★ WHY prefixes/hosts exist (2026-09-24). api.dchub.cloud/api/v1/stats is cached
under the zone worker's ORIGIN subrequest URL on the Railway host (Railway is
not on Cloudflare, so the object lives in this zone), pinned 3600s by Cache
Rule #1. Single-file purge of both the public URL and that Railway URL
returned success=true and did NOT evict it — measured twice, once on a stuck
boot-degraded copy and once on a healthy one. Only purge_everything did. These
inputs let a narrower purge be tried before reaching for the whole zone.

Exits 1 with a ::error:: line (on stdout, where the runner reads workflow
commands) when the inputs are not exactly one well-formed kind.
"""
from __future__ import annotations

import json
import os
import sys

# (dispatch input name, Cloudflare body key)
KINDS = (("urls", "files"), ("prefixes", "prefixes"), ("hosts", "hosts"))


def build(urls: str = "", prefixes: str = "", hosts: str = "") -> dict:
    raw = dict(zip((k for k, _ in KINDS), (urls, prefixes, hosts)))
    given = {name: raw[name].split() for name, _ in KINDS if raw[name].strip()}
    if len(given) != 1:
        raise ValueError(
            "set exactly ONE of urls / prefixes / hosts (got %s) — Cloudflare "
            "takes one kind of target per purge request"
            % (", ".join(sorted(given)) or "none"))
    (name, items), = given.items()
    if name == "urls":
        bad = [u for u in items if not u.startswith(("https://", "http://"))]
        why = "urls must be absolute (https://…)"
    elif name == "prefixes":
        bad = [p for p in items if "://" in p or "/" not in p]
        why = "prefixes are host/path with NO scheme (host.example/path)"
    else:
        bad = [h for h in items if "://" in h or "/" in h]
        why = "hosts are bare hostnames, no scheme and no path"
    if bad:
        raise ValueError(f"{why}: {bad}")
    return {dict(KINDS)[name]: items}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: cf_purge_body.py OUT.json")
        return 1
    try:
        body = build(os.environ.get("URLS", ""), os.environ.get("PREFIXES", ""),
                     os.environ.get("HOSTS", ""))
    except ValueError as e:
        print(f"::error::{e}")
        return 1
    with open(argv[0], "w") as fh:
        json.dump(body, fh)
    (kind, items), = body.items()
    print(f"Purging {len(items)} {kind}: {' '.join(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
