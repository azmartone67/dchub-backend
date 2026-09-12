#!/usr/bin/env python3
"""check_sitemap_selfcanon.py — does the PUBLISHED sitemap advertise two
self-canonical URLs for one facility?

tests/test_sitemap_no_duplicate_selfcanon.py proves the emit rules behave, on
fixtures, in CI. This asks the deployed artefact. They are different questions:
the sitemap is served from a SNAPSHOT, so shipping the rule and publishing it
are separated by a rebuild.

    DATABASE_URL=... python3 scripts/check_sitemap_selfcanon.py
    ... --json           machine-readable
    ... --max-groups 350 fail above this many residual groups

Exit 0 clean · 1 over budget · 2 could not measure. ★ "Could not measure" is
NEVER exit 0: a scan that fetched nothing has no duplicates either, and that is
the shape of a green run that checked nothing.

Identity is the RENDERED <h1> + <title> (util/facility_headline) — the only
honest key. The slug is not: it hashes provider|name and the classic duplicate
pair disagrees about `provider`, which is exactly why it has two slugs.

★★★ THE BUDGET IS A CEILING, AND A CEILING WITH SLACK IN IT IS THE SAME BUG AS
A SCAN WITH NO FLOOR. It defaulted to 400 against a live 303 — 97 groups of
headroom, which meant this guard would have gone on exiting 0 all the way back
up to the population it exists to catch. Set it just above what is actually
measured, and re-measure it whenever the residual moves.

MEASURED 2026-09-07, live:

    before r-drain-fork (#4101)        3,930 groups
    after  r-drain-fork                  310 groups   (the 303 reported at
                                                       08:45Z was measured with
                                                       26 pointers that were
                                                       later reverted as false
                                                       merges; 310 is the
                                                       honest self-canonical
                                                       number without them)
    after  r-twin-pointer (#4110)         103 groups   <- the budget's basis
    --max-groups default                  120          17 groups of headroom

★★★ NOTHING RAN THIS UNTIL 2026-09-11. The 103 below was a HAND-RUN. No
workflow and no cron referenced this file, so the 96% drop it recorded was
completely unmonitored — the residual could have climbed back to 3,930 in
silence. .github/workflows/sitemap-selfcanon-daily.yml now runs it daily at
10:41 UTC on the `DATABASE_URL` repo secret and fails on exit 1, warns on
exit 2, and fails DIFFERENTLY when nothing has measured for 3 days.

RE-MEASURED 2026-09-11 FROM THE OUTSIDE (no DB), as a corroboration of the
budget rather than a replacement for its basis:

    18,809  distinct /facilities/<slug> in the published sitemap
       102  groups of >=2 on util.facility_headline.identity_key
       108  surplus URLs
        40  of those 102 still have >=2 members serving rel=canonical at
            THEMSELVES (the rest already canonicalise at a twin — this
            checker counts them anyway, by design: see the 37 drain-fork and
            13 twin-pointer lines below)

The five identity fields came from the public /api/v1/exports/facilities
snapshot plus /api/v1/facilities/<slug> for the 1,858 published slugs that
snapshot does not carry. ★ It is an UPPER BOUND on what this script reports:
the public surfaces do not expose `duplicate_of_id`, so the outside reading
skips nothing and this script skips the twins. 102 against a 103 measured four
days earlier says the residual has not moved, and 120 still sits 18 above it.

★ 103 is MEASURED, live, after the apply. #4110 predicted 80 by simulation and
  shipped a budget of 100 on that prediction, so the guard failed on its first
  real run. The simulation was wrong twice over: it anchored on a BEFORE of 310
  when the checker's own number at apply time was 322, and it assumed 231 URLs
  would drop when 220 did (11 were held back by guards it did not model). A
  budget set from a prediction is a guess wearing a measurement's clothes —
  this one is set from the artefact.

EVERY ONE OF THE 103 IS ACCOUNTED FOR, and none of them is a defect:

    37  drain fork — consolidated by #4101's pointer; the URL is still emitted
        because its keeper is absent from the sitemap or the slug is shared
    36  refused: name mismatch (see below) — the deliberate refusal
    13  twin pointer IS set; the URL stays because a LIVE discovered row also
        wears that legacy slug (the NOT EXISTS guard — dropping it would take a
        live page's only URL, the 2026-07-28 lesson)
    11  coordinate veto fired — genuinely distant rows under one <h1>
     5  group larger than MAX_GROUP — a generic-name collision, not a facility
     1  no discovered keeper — duplicate_of_id addresses no other id space

WHAT THE ~80 RESIDUAL IS, AND WHY IT IS NOT GOING TO ZERO. It is the
name_mismatch class routes/facility_dedup_v4.plan_group deliberately REFUSES:
two rows that render one <h1> from DIFFERENT names, because
util.facility_site_code.site_code_headline rewrites the <h1> down to
"<Operator> <CODE> — <City> Data Center" and drops the tail. 197 of the 202
cross-table pairs are that shape, and it is lossy — "SecureIT DCB1.1"/"DCB1.2"
and "noris network AG ING1 ITA"/"ITB" are DISTINCT halls 0.00-0.03 km apart
under one heading. Consolidating them would hide a real facility, so the lane
refuses and this checker counts them. ★ The real fix is in the HEADLINE, not
here: until site_code_headline stops collapsing those names, two genuinely
different buildings publish one <h1> and this number stays around 80.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SITEMAP = "https://dchub.cloud/sitemap.xml"
UA = "DCHub-SelfCanonCheck/1.0"
# A sitemap this small means the fetch or the shard parse broke, not that the
# site shrank. Same contract as _SITEMAP_THIN_GATE_FLOOR in main.py.
MIN_URLS = 2000

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_FAC = re.compile(r"https://dchub\.cloud/facilities/(.+)$")


def _get(url, timeout=60):
    import requests
    # cache-busted: the sitemap is served through Cloudflare and a "verified
    # live" read off a HIT is not one.
    sep = "&" if "?" in url else "?"
    r = requests.get(f"{url}{sep}_={int(time.time())}",
                     headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    raw = r.content
    # requests decodes Content-Encoding, but a shard that is a .gz FILE arrives
    # as bytes — the magic number is the only reliable test.
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw.decode("utf-8", "replace")


def published_facility_slugs(index_url=SITEMAP):
    """Every /facilities/<slug> in every shard of the index, deduplicated."""
    shards = _LOC.findall(_get(index_url))
    if not shards:
        raise RuntimeError(f"{index_url} listed no shards")
    slugs, fetched = set(), 0
    for s in shards:
        if "facilit" not in s:
            continue
        for loc in _LOC.findall(_get(s)):
            m = _FAC.match(loc)
            if m:
                slugs.add(m.group(1))
        fetched += 1
    if not fetched:
        raise RuntimeError("index carried no facility shards")
    return slugs


def _rows(cur, sql):
    cur.execute(sql)
    return cur.fetchall() or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-groups", type=int, default=120)
    ap.add_argument("--sitemap", default=SITEMAP)
    a = ap.parse_args()

    try:
        import psycopg2
        from util.facility_headline import identity_key
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        slugs = published_facility_slugs(a.sitemap)
        if len(slugs) < MIN_URLS:
            raise RuntimeError(
                f"only {len(slugs)} facility URLs published (floor {MIN_URLS}) "
                "— treating as a broken fetch, not a clean sitemap")
        conn = psycopg2.connect(dsn, connect_timeout=30)
        cur = conn.cursor()
        # /facilities/<slug> resolves discovered_facilities FIRST, then the
        # legacy table — so load legacy first and let discovered overwrite.
        row = {}
        for sql in (
            "SELECT canonical_slug, name, provider, city, state, country, "
            "       NULL::int AS dup, COALESCE(power_mw,0), id::text "
            "  FROM facilities "
            " WHERE canonical_slug IS NOT NULL AND canonical_slug <> ''",
            "SELECT canonical_slug, name, provider, city, state, country, "
            "       duplicate_of_id, COALESCE(power_mw,0), id::text "
            "  FROM discovered_facilities "
            " WHERE canonical_slug IS NOT NULL AND canonical_slug <> '' "
            "   AND COALESCE(is_duplicate,0) = 0",
        ):
            for r in _rows(cur, sql):
                # the lookup's own tiebreak: highest power, then lowest id
                prev = row.get(r[0])
                if prev is None or (r[7], str(prev[8])) > (prev[7], str(r[8])):
                    row[r[0]] = r
        conn.close()
    except Exception as e:
        out = {"ok": False, "measured": False, "error": str(e)[:300]}
        print(json.dumps(out) if a.json else f"COULD NOT MEASURE: {e}")
        return 2

    groups, unresolved = collections.defaultdict(list), []
    for s in sorted(slugs):
        r = row.get(s)
        if r is None:
            unresolved.append(s)
            continue
        if r[6] is not None:
            continue          # declares a twin canonical — not self-canonical
        groups[identity_key(r[1], r[2], r[3], r[4], r[5])].append(s)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    surplus = sum(len(v) - 1 for v in dupes.values())
    ok = len(dupes) <= a.max_groups
    out = {
        "ok": ok, "measured": True,
        "published_facility_urls": len(slugs),
        "self_canonical": sum(len(v) for v in groups.values()),
        "unresolved_slugs": len(unresolved),
        "duplicate_groups": len(dupes),
        "surplus_urls": surplus,
        "budget": a.max_groups,
        "sizes": dict(sorted(collections.Counter(
            len(v) for v in dupes.values()).items())),
        "sample": [{"h1": k[0], "urls": v} for k, v in list(dupes.items())[:10]],
    }
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"published facility URLs : {out['published_facility_urls']}")
        print(f"self-canonical          : {out['self_canonical']}")
        print(f"duplicate groups        : {out['duplicate_groups']} "
              f"(budget {a.max_groups})  surplus URLs {surplus}")
        print(f"group sizes             : {out['sizes']}")
        for g in out["sample"][:5]:
            print(f"  {g['h1'][:56]!r} -> {g['urls']}")
        print("OK" if ok else "OVER BUDGET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
