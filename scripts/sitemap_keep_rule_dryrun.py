#!/usr/bin/env python3
"""Dry-run the Step 2 keep-or-drop rule against the PUBLISHED sitemaps.

Owner rule (approved 2026-09-15), for every facility URL in any sitemap,
including the AI shards:

    KEEP when the URL earned >=1 GSC impression in the last 90 days,
    OR it is not thin AND not an undetected duplicate.

This script MEASURES that rule and writes nothing. It is also the instrument the
4-6 week re-measure reads, so every number it prints names its own basis.

★★★ MEMBERSHIP IS READ OFF THE PUBLISHED ARTEFACT, NEVER RE-DERIVED.
The gated / AI-only split comes from the live shards themselves. A second copy
of main.py's capacity predicate (`_thin_excl`) would drift from it the first
time either moved, and a dry-run that disagrees with the artefact it describes
is worse than none. Same reason the floor and the fetch come from
check_sitemap_selfcanon rather than a second implementation.

★★★ THE IMPRESSION SIDE IS `seo_proven_pages` — THE TABLE THE SITEMAP ITSELF
READS (main.py, the r-proven-exempt readmission). google_search_console.
refresh_proven_pages pulls dimensions=['page'] over 90 days, PAGED with
startRow, and upserts every facility URL with impressions > 0, stamping
`last_seen = CURRENT_DATE`. So the rows carrying MAX(last_seen) ARE the current
in-window set, which is exactly the rule's "earned >=1 impression in the last
90 days".
★ `impressions` is a GREATEST() ratchet across refreshes. It is a lifetime high
  water mark, NOT a 90-day figure — read MEMBERSHIP from this table, never the
  magnitude. This script prints the ratchet only beside residual duplicate
  members, where it is used to rank, never to claim a 90-day total.
★ The public /api/v1/seo/performance page grain CANNOT answer this. Its daily
  ingest stores roughly the top 500 pages per day (routes/gsc_performance.
  DEFAULT_ROW_LIMIT), so the 1-impression tail this rule turns on is simply not
  in `gsc_daily_performance`, and the stored window has a known 08-01..08-17
  hole on top of that.

★★★ FAIL CLOSED, LOUDLY. A missing, empty or STALE proven table marks every URL
zero-impression and turns this into a 19,000-URL removal plan. Every one of
those conditions exits 2 (COULD NOT MEASURE) and prints no drop count. Exit 2
is also what the workflow distinguishes from a real over-budget answer.

Two thin bars are reported, because this repo holds two and they disagree:

    contentless  util/thin_content.is_contentless — the rule's literal wording.
                 These pages serve robots=noindex and have been excluded from
                 BOTH families since r-noindex-coherence (2026-09-07), so this
                 branch is expected to be 0 published URLs. Reported anyway: a
                 non-zero here means the coherence invariant regressed.
    capacity     the r-thin-sitemap gate — operationally, the URLs the ungated
                 AI family publishes that the capacity-gated shard does not.
                 This is the bar the owner approved for the removal PR
                 (2026-09-15), and `drop_candidates` below counts it.

Usage:
    DATABASE_URL=... python3 scripts/sitemap_keep_rule_dryrun.py [--json]

Exit codes:
    0  measured
    2  could not measure (no DSN, missing/empty/stale proven table, short
       sitemap, or a drop share so large the inputs are not trustworthy)
"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# One owner for the fetch, the parse and the floor: the daily self-canon
# checker. Importing it is deliberate — see the membership note above.
import check_sitemap_selfcanon as csc  # noqa: E402

# The proven refresh runs inside the daily GSC ingest. Three days of slack
# covers a missed cron and GSC's own 2-3 day reporting lag; beyond that the
# table is stale and this script refuses to classify rather than reporting a
# removal plan built on absence.
PROVEN_MAX_AGE_DAYS = 3

# ★ Blast-radius refusal, not a policy. At the 2026-08-19 measurement the
#   capacity-less pool was 13,279 slugs of which 4,567 had any impression, i.e.
#   the zero-impression share of a ~19k corpus sat near 46%. A dry-run claiming
#   more than 60% of the published sitemap should be dropped has lost an input
#   (an empty proven read, a half-fetched sitemap), not discovered a collapse.
MAX_DROP_SHARE = 0.60


def _proven_current(cur):
    """(slugs touched by the newest refresh, as_of date, total rows).

    Raises RuntimeError when the table is missing, empty or stale — each of
    which would otherwise read as "nothing has impressions"."""
    cur.execute("SELECT to_regclass('public.seo_proven_pages')")
    reg = cur.fetchone()
    if not (reg and reg[0]):
        raise RuntimeError(
            "seo_proven_pages does not exist — the impression side of the rule "
            "is unmeasurable, and treating that as 'no page has impressions' "
            "would drop the whole sitemap")
    cur.execute("SELECT slug, last_seen, impressions FROM seo_proven_pages")
    rows = cur.fetchall() or []
    if not rows:
        raise RuntimeError("seo_proven_pages is EMPTY — refusing to read that "
                           "as zero impressions site-wide")
    as_of = max(r[1] for r in rows if r[1] is not None)
    age = (datetime.date.today() - as_of).days
    if age > PROVEN_MAX_AGE_DAYS:
        raise RuntimeError(
            f"seo_proven_pages last_seen is {as_of} ({age} days old, limit "
            f"{PROVEN_MAX_AGE_DAYS}) — the daily GSC refresh is not running, so "
            "the in-window set cannot be read")
    current = {r[0] for r in rows if r[1] == as_of}
    ratchet = {r[0]: r[2] for r in rows}
    return current, as_of, len(rows), ratchet


def _shard_membership(index_url):
    """{'gated': set, 'ai': set, 'other': set} of facility slugs, per shard family.

    The AI family is the ungated build and the gated shard is a strict subset of
    it (#4459 pins that as a SET comparison, not a length one). Both facts are
    re-derived here from the artefact rather than assumed."""
    fams = {"gated": set(), "ai": set(), "other": set()}
    for shard in csc._LOC.findall(csc._get(index_url)):
        if "facilit" not in shard:
            fam = "other"
        elif "ai-facilities" in shard:
            fam = "ai"
        else:
            fam = "gated"
        for loc in csc._LOC.findall(csc._get(shard)):
            m = csc._FAC.match(loc)
            if m:
                fams[fam].add(m.group(1))
    return fams


# Columns that carry the consolidation evidence but are NOT guaranteed to exist
# on both tables. `facilities.discovered_twin_id` is created by v4's
# ensure_twin_schema on an admin hit, so an environment that never took one does
# not have it; `address` is absent from v4's own legacy SELECT. Each is PROBED
# and reported as absent rather than assumed — an unprobed name here fails the
# whole query and costs every group its evidence.
_OPTIONAL_COLS = {
    "discovered_facilities": ("address", "merged_facility_id"),
    "facilities": ("address", "discovered_twin_id"),
}
_BASE_COLS = ("id", "canonical_slug", "name", "provider", "city", "state",
              "country", "latitude", "longitude", "power_mw",
              "duplicate_of_id")


def _present_columns(cur, table, wanted):
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "  AND column_name = ANY(%s)", (table, list(wanted)))
    return {r[0] for r in cur.fetchall() or []}


def _group_detail(cur, slugs):
    """Every row behind the residual duplicate URLs, both tables.

    ★ Scoped to the residual slugs ONLY (an ANY() list of ~dozens), because the
      consolidation PR needs the evidence per member — table, name, provider,
      coordinates, address, and which pointer column could name a keeper — and
      that evidence is what separates one building spelt twice (cologix-dal1,
      identical coordinates) from six halls of one campus (DATA4 MIL01
      DC01..DC10, 50-300 m apart), which must NEVER be merged.
    ★ A failure here is REPORTED by the caller, never swallowed into a
      column-poor fallback: the legacy union in main.py has a documented history
      of exactly that. Absent OPTIONAL columns are named in `missing_columns`.
    """
    out, missing = {}, {}
    for table in ("discovered_facilities", "facilities"):
        have = _present_columns(cur, table, _OPTIONAL_COLS[table])
        missing[table] = sorted(set(_OPTIONAL_COLS[table]) - have)
        sel = list(_BASE_COLS) + [
            c if c in have else f"NULL AS {c}" for c in _OPTIONAL_COLS[table]]
        # duplicate_of_id is INTEGER on discovered_facilities and TEXT on
        # facilities — two id spaces, so both are read as text and neither is
        # ever resolved against the other's ids.
        sel = [f"{c}::text" if c in ("id", "duplicate_of_id") else c for c in sel]
        cur.execute(f"SELECT '{table}' AS tbl, {', '.join(sel)} FROM {table} "
                    "WHERE canonical_slug = ANY(%s)", (list(slugs),))
        cols = ["table"] + list(_BASE_COLS) + list(_OPTIONAL_COLS[table])
        for r in cur.fetchall() or []:
            d = dict(zip(cols, r))
            for k in ("latitude", "longitude", "power_mw"):
                d[k] = float(d[k]) if d.get(k) is not None else None
            out.setdefault(d["canonical_slug"], []).append(d)
    return out, missing


def classify(fams, proven):
    """PURE — no I/O, unit-tested. The rule's branches, as SETS.

    Both refusals live here so there is one place to pin them:
      * the artefact floor (a short sitemap is a broken fetch, not a small site);
      * the drop-share ceiling (see MAX_DROP_SHARE).
    """
    published = fams["gated"] | fams["ai"] | fams["other"]
    if len(published) < csc.MIN_URLS:
        raise RuntimeError(
            f"only {len(published)} facility URLs published (floor "
            f"{csc.MIN_URLS}) — treating as a broken fetch, not a small sitemap")
    # AI-only IS the capacity-thin set, read off the artefact: a URL the ungated
    # family publishes and the capacity-gated shard does not.
    ai_only = fams["ai"] - fams["gated"] - fams["other"]
    drop = ai_only - proven
    share = len(drop) / float(len(published))
    if share > MAX_DROP_SHARE:
        raise RuntimeError(
            f"{len(drop)} of {len(published)} published URLs ({share:.0%}) "
            f"classify as drop — above the {MAX_DROP_SHARE:.0%} refusal line. "
            "An input is wrong (proven read, sitemap fetch); this is not a "
            "measurement")
    return {"published": published, "ai_only": ai_only,
            "keep_impression": published & proven, "drop": drop, "share": share}


def measure(dsn, index_url, grace_days=PROVEN_MAX_AGE_DAYS):
    import psycopg2
    from util.thin_content import contentless_slug_set

    fams = _shard_membership(index_url)
    published = fams["gated"] | fams["ai"] | fams["other"]

    conn = psycopg2.connect(dsn, connect_timeout=30)
    try:
        cur = conn.cursor()
        proven, as_of, proven_rows, ratchet = _proven_current(cur)

        # The rule's literal thin bar. Expected to be 0 against `published`:
        # these slugs serve robots=noindex and the emit loop already refuses
        # them. A non-zero count is a regression of r-noindex-coherence, not a
        # removal opportunity.
        contentless = contentless_slug_set(cur)

        # Undetected duplicates, by the ONE honest identity (rendered h1+title),
        # through the same helpers the daily checker publishes its number from.
        row_by_slug = csc.serving_row_by_slug(cur)
        groups, unresolved = csc.group_by_identity(published, row_by_slug)
        dupes = {k: v for k, v in groups.items() if len(v) > 1}

        detail, detail_missing, detail_error = {}, {}, None
        if dupes:
            members = sorted({s for v in dupes.values() for s in v})
            try:
                detail, detail_missing = _group_detail(cur, members)
            except Exception as ex:  # noqa: BLE001 — reported, never swallowed
                conn.rollback()
                detail_error = f"{type(ex).__name__}: {str(ex)[:200]}"
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # The owner-approved removal bar (2026-09-15): capacity-thin AND no
    # impression in the window. Duplicates are NOT dropped here — they are
    # consolidated with rel=canonical by their own PR.
    # ★ Both refusals live in classify(), which is why they fire AFTER the read
    #   rather than before it: one owner for the thresholds is worth one wasted
    #   connection on a broken fetch.
    plan = classify(fams, proven)
    ai_only, keep_impression = plan["ai_only"], plan["keep_impression"]
    drop_candidates = plan["drop"]

    # Surplus counts the members a group would shed, keeper excluded. The keeper
    # is NOT elected here: election is the consolidation PR's job and it needs
    # the per-member evidence below, not a count.
    surplus = sum(len(v) - 1 for v in dupes.values())
    return {
        "ok": True,
        "measured": True,
        "as_of_utc": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_facility_urls": len(published),
        "families": {"gated": len(fams["gated"]), "ai": len(fams["ai"]),
                     "other_shards": len(fams["other"]),
                     "ai_only": len(ai_only),
                     "gated_outside_ai": len(fams["gated"] - fams["ai"])},
        "impressions": {
            "source": "seo_proven_pages",
            "as_of": str(as_of),
            "rows_in_table": proven_rows,
            "in_window_slugs": len(proven),
            "published_with_impression": len(keep_impression),
            "ai_only_with_impression": len(ai_only & proven),
            "note": "membership only — `impressions` is a GREATEST() ratchet, "
                    "never a 90-day total",
        },
        "thin": {
            "contentless_slugs_total": len(contentless),
            "contentless_published": len(published & contentless),
            "note": "util/thin_content.is_contentless — the rule as written. "
                    "Expected 0 published since r-noindex-coherence "
                    "(2026-09-07); non-zero means that invariant regressed",
        },
        "duplicates": {
            "groups": len(dupes),
            "surplus_urls": surplus,
            "unresolved_slugs": len(unresolved),
            "detail_error": detail_error,
            "missing_columns": detail_missing,
            "groups_detail": [
                {"identity": k[0] if isinstance(k, tuple) else str(k),
                 "urls": v,
                 "members": [m for s in v for m in detail.get(s, [])],
                 "impressions_ratchet": {s: ratchet.get(s, 0) for s in v}}
                for k, v in sorted(dupes.items(), key=lambda kv: -len(kv[1]))
            ],
        },
        "rule": {
            "keep_by_impression": len(keep_impression),
            "drop_candidates_capacity_thin_zero_impression": len(drop_candidates),
            "keep_total_after_drop": len(published) - len(drop_candidates),
            "bar": "capacity gate (AI-only) AND no impression in the window, "
                   "per the owner decision of 2026-09-15",
        },
    }


def markdown_summary(out):
    """The step summary. Public because the workflow renders it from the JSON
    artefact rather than re-running the measurement — one owner for the wording,
    and a summary that cannot disagree with the file it was built from."""
    r, f, i, d = out["rule"], out["families"], out["impressions"], out["duplicates"]
    L = ["### Step 2 keep-or-drop dry-run",
         f"* published facility URLs: **{out['published_facility_urls']}** "
         f"(gated {f['gated']}, AI {f['ai']}, AI-only {f['ai_only']})",
         f"* impressions source: `seo_proven_pages` as of {i['as_of']} "
         f"({i['in_window_slugs']} in-window slugs, {i['rows_in_table']} rows)",
         f"* KEEP, earned an impression: **{r['keep_by_impression']}**",
         f"* DROP candidates (capacity-thin, 0 impressions): "
         f"**{r['drop_candidates_capacity_thin_zero_impression']}**",
         f"* sitemap after the drop: {r['keep_total_after_drop']}",
         f"* thin per util/thin_content published: "
         f"{out['thin']['contentless_published']} "
         f"(of {out['thin']['contentless_slugs_total']} contentless slugs)",
         f"* residual duplicate groups: {d['groups']} "
         f"({d['surplus_urls']} surplus URLs, {d['unresolved_slugs']} unresolved)"]
    if d["detail_error"]:
        L.append(f"* ⚠ group detail failed: `{d['detail_error']}`")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sitemap", default=csc.SITEMAP)
    ap.add_argument("--grace-days", type=int, default=PROVEN_MAX_AGE_DAYS)
    a = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        print(json.dumps({"ok": False, "measured": False,
                          "error": "DATABASE_URL is not set"})
              if a.json else "COULD NOT MEASURE: DATABASE_URL is not set")
        return 2
    try:
        out = measure(dsn, a.sitemap, a.grace_days)
    except Exception as e:  # noqa: BLE001 — every failure is "cannot measure"
        msg = str(e)[:400]
        print(json.dumps({"ok": False, "measured": False, "error": msg})
              if a.json else f"COULD NOT MEASURE: {msg}")
        return 2

    print(json.dumps(out, indent=2, default=str)
          if a.json else markdown_summary(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
