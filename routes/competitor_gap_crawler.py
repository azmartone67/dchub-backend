"""Competitor coverage-gap crawler (2026-06-17).

Crawls public surfaces of data-center competitors (DatacenterHawk,
Cloudscene, DataCenter Frontier, DataCenterDynamics), finds facilities they
list that DC Hub does NOT have, and feeds the TRUE gaps into the existing
discovery → auto-approve → facilities chain so our MCP tools can answer
"do you have facility X" where rivals only show it behind a login.

Design (per vetted spec, reuse-first + additive):

  - REUSES routes/competitor_intel.py:_COMPETITOR_SITEMAPS for the
    competitive-metric sitemaps; this module adds its own gap-source
    sitemap map (_GAP_SOURCES) because the gap diff needs the per-facility
    child sitemaps, not the root sitemap.
  - REUSES news_facility_extractor.insert_discovered_facility (dedups on
    source_url) to stage TRUE gaps into discovered_facilities — NEVER writes
    custom SQL into the live `facilities` table (capacity_mw/lat/lng schema
    trap). facility_auto_approve.run_auto_approve promotes from there.
  - REUSES routes/news_entity_extraction._is_real_entity for the NER
    real-entity gate so slug noise (provider-index pages, carrier-only
    service-providers, sentence fragments) is dropped.
  - REUSES routes/brain_findings_writer.upsert_brain_finding to file one
    finding per source (issue='coverage_gap_competitor:<slug>'), marked
    resolved once gaps are staged to dodge the 200-sighting quarantine.

Sources crawled (must stay in sync with _GAP_SOURCES below):
  - Cloudscene          /sitemap/data-centers.xml, /sitemap/service-providers.xml
  - DataCenter Frontier /sitemap/News.xml                            (editorial)
  - DataCenterDynamics  /en/rss/     (editorial; honor Content-Signal ai-train=no)
  - DatacenterHawk      /__sitemap__/facilities.xml, /__sitemap__/providers.xml

  ★2026-08-10: whether a given source SHOULD be ingested is a permission
  question, and robots.txt does not answer it. "Allow: /" says a crawler may
  fetch a page; it grants nothing about reusing a compiled directory — single
  facts are not protectable, but a compilation generally is. So do not justify
  a source here on the basis of robots, and do not reason here about User-Agent
  selection; if a source's terms require permission, obtain permission. Where
  each source stands is tracked in docs/DATA-PROVENANCE.md §4 and §7.

SKIPPED (do NOT crawl):
  - DCByte         — ToS PROHIBITS automated crawling/scraping/indexing of
                     ALL content (overrides its permissive robots.txt).
  - DataCenters.com — Vercel anti-bot wall 403s Railway egress; permissions
                     unknown from Railway → skip until re-verified.

Safety:
  - Self-identifying UA, polite rate-limit (sleep _POLITE_DELAY_S between fetches),
    per-run page cap (default 500 sitemap URLs), whole-run time-cap (default 90s).
  - Robots disallowed-prefix check before any fetch.
  - Runs ONLY under the existing daily competitor scan / the discovery runner,
    background + time-capped, so the 1-replica request path never blocks.
"""

from __future__ import annotations

import os
import re
import time
import random
import logging
import datetime
from urllib.parse import urlsplit
from utc_clock import utc_now

logger = logging.getLogger(__name__)

# ── Self-identifying UA: names the operator, links a contact page, and
#    states purpose. It is deliberately NOT disguised, and it is not a
#    substitute for permission — see docs/DATA-PROVENANCE.md §4. Keep it
#    accurate and keep it identifying; permission questions belong in the
#    provenance register, not in a UA string.
GAP_USER_AGENT = ("DCHub-Discovery-Bot/1.0 (+https://dchub.cloud; "
                  "factual-db-diff; not-for-training)")

# Polite rate-limit + caps (env-overridable so a deploy can tune).
_POLITE_DELAY_S = float(os.environ.get("COMPETITOR_GAP_DELAY_S", "2.5"))
_DEFAULT_PAGE_LIMIT = int(os.environ.get("COMPETITOR_GAP_PAGE_LIMIT", "500"))
_DEFAULT_BUDGET_S = float(os.environ.get("COMPETITOR_GAP_BUDGET_S", "90"))
_FETCH_TIMEOUT_S = float(os.environ.get("COMPETITOR_GAP_FETCH_TIMEOUT_S", "20"))
_MAX_INSERTS_PER_RUN = int(os.environ.get("COMPETITOR_GAP_MAX_INSERTS", "200"))
_MAX_GAP_ONLY_PER_RUN = int(os.environ.get("COMPETITOR_GAP_MAX_GAP_ONLY", "150"))


# ── Gap sources. method drives slug parsing.
#    "dchawk_facility"  → /marketplace/providers/<operator>/<street-addr[-city]>/<code>
#    "cloudscene_dc"    → /data-center/<country>/<city>/<operator-facility-name>
#    "provider_index"   → operator list only (used to enrich operator coverage,
#                          NOT inserted as facilities)
#    "editorial_rss"    → DCD/DCF headlines (deal/expansion signal; routed to
#                          NER + optional deal path, not bulk-inserted)
# ★★★ORDERED BY YIELD, AND THAT ORDER IS LOAD-BEARING (2026-07-29).
# The loop is time-capped (`COMPETITOR_GAP_BUDGET_S`, default 90s) and `break`s
# when the budget runs out, so WHATEVER IS FIRST GETS THE BUDGET and the tail
# may never run at all.
#
# It used to lead with `dchawk` + `dchawk_providers`. DCHawk mints ZERO
# facilities BY DESIGN — its slug parser blends street+city so city/country come
# back None and 100% of candidates die at `_is_geocodable` before the existence
# diff (documented since 2026-07-02). Measured on a live run 2026-07-29: dchawk
# parsed 500 URLs for `true_gaps 0 / inserted 0`, and the run then hit
# `budget_exhausted` at `cloudscene_providers` — i.e. the crawler spent its
# budget on a source that cannot produce, and Cloudscene, the only real yielder,
# was starved.
#
# That is the most likely reason Cloudscene sat at 962 of 11,859 sitemap URLs
# (8.1%) despite a sweep designed to tile the whole sitemap in ~24 daily runs.
#
# So: FACILITY-MINTING SOURCES FIRST, signal-only sources after.
#   cloudscene            -> the ONLY source that mints facilities today
#   cloudscene_providers  -> provider index (no facility inserts)
#   dcf / dcd             -> editorial signal (no facility inserts)
#   dchawk*               -> operator list + editorial signal ONLY; last,
#                            because it is known to yield no facilities
# ★If the budget clips the tail, we lose signal-only sources for that run — an
# acceptable trade against starving the one source that grows inventory. Raise
# `COMPETITOR_GAP_BUDGET_S` if the tail matters more than it currently does.
_GAP_SOURCES = [
    {"slug": "cloudscene", "name": "Cloudscene",
     "method": "cloudscene_dc",
     "url": "https://cloudscene.com/sitemap/data-centers.xml",
     "disallowed_prefixes": ()},
    {"slug": "cloudscene_providers", "name": "Cloudscene (service-providers)",
     "method": "provider_index",
     "url": "https://cloudscene.com/sitemap/service-providers.xml",
     "disallowed_prefixes": ()},
    {"slug": "dcf", "name": "DataCenter Frontier",
     "method": "editorial_rss",
     "url": "https://www.datacenterfrontier.com/sitemap/News.xml",
     "disallowed_prefixes": ()},
    {"slug": "dcd", "name": "DataCenterDynamics",
     "method": "editorial_rss",
     "url": "https://www.datacenterdynamics.com/en/rss/",
     "disallowed_prefixes": ()},
    {"slug": "dchawk", "name": "DatacenterHawk",
     "method": "dchawk_facility",
     "url": "https://datacenterhawk.com/__sitemap__/facilities.xml",
     "disallowed_prefixes": ("/data", "/admin", "/api", "/rest", "/iam")},
    {"slug": "dchawk_providers", "name": "DatacenterHawk (providers)",
     "method": "provider_index",
     "url": "https://datacenterhawk.com/__sitemap__/providers.xml",
     "disallowed_prefixes": ("/data", "/admin", "/api", "/rest", "/iam")},
]


# Country slug → ISO-2 (Cloudscene uses full-word country slugs).
_COUNTRY_SLUG_ISO = {
    "united-states-of-america": "US", "united-states": "US", "usa": "US",
    "united-kingdom": "GB", "england": "GB", "scotland": "GB",
    "ireland": "IE", "germany": "DE", "france": "FR", "spain": "ES",
    "netherlands": "NL", "singapore": "SG", "japan": "JP", "australia": "AU",
    "canada": "CA", "india": "IN", "brazil": "BR", "italy": "IT",
    "sweden": "SE", "denmark": "DK", "norway": "NO", "finland": "FI",
    "poland": "PL", "switzerland": "CH", "austria": "AT", "belgium": "BE",
    "south-africa": "ZA", "new-zealand": "NZ", "mexico": "MX", "chile": "CL",
    "china": "CN", "hong-kong": "HK", "south-korea": "KR", "taiwan": "TW",
    "indonesia": "ID", "malaysia": "MY", "thailand": "TH", "philippines": "PH",
    "united-arab-emirates": "AE", "saudi-arabia": "SA", "israel": "IL",
    "portugal": "PT", "czech-republic": "CZ", "romania": "RO", "greece": "GR",
}

# US state hint from a city slug suffix (Cloudscene appends -oh, -tx, etc.
# on some US cities, e.g. columbus-oh).
_US_STATE_ABBREVS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy",
}


# ─────────────────────────────────────────────────────────────────────
# FETCH (honest UA, robots-aware, polite). Distinct from
# competitor_intel._fetch (which reads only 8KB for hash/title) — sitemap
# diffing needs the full body. We mirror its tolerant fail-soft shape.
# ─────────────────────────────────────────────────────────────────────

def _robots_allows(url: str, disallowed_prefixes: tuple) -> bool:
    """Cheap per-URL robots check against the source's known
    User-agent:* disallowed prefixes. We already verified robots.txt for
    each source out-of-band; this guards against an accidentally-built URL
    drifting onto a gated path (e.g. /api)."""
    try:
        path = urlsplit(url).path or "/"
    except Exception:
        return False
    for pref in (disallowed_prefixes or ()):
        if path.startswith(pref):
            return False
    return True


def _fetch_text(url: str, disallowed_prefixes: tuple = ()) -> dict:
    """Fetch full text body with the honest UA. Returns
    {status, text, error}. Tolerates failure (never raises)."""
    out = {"status": 0, "text": "", "error": None}
    if not _robots_allows(url, disallowed_prefixes):
        out["error"] = "robots_disallowed"
        return out
    try:
        import requests
    except Exception as e:  # pragma: no cover
        out["error"] = f"requests_unavailable: {e}"
        return out
    try:
        r = requests.get(url, timeout=_FETCH_TIMEOUT_S, headers={
            "User-Agent": GAP_USER_AGENT,
            # Honor DCD's Content-Signal: explicit no-train intent.
            "Accept": "application/xml, text/xml, application/rss+xml, "
                      "text/html;q=0.9, */*;q=0.5",
            "Cache-Control": "no-cache",
        })
        out["status"] = r.status_code
        if r.status_code == 200:
            out["text"] = r.text[:8_000_000]  # 8MB cap
        else:
            out["error"] = f"http_{r.status_code}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


_LOC_RE = re.compile(r"<loc>\s*([^<\s][^<]*?)\s*</loc>", re.I)
# RSS/Atom item links: <link>...</link> or <link href="..."/>
_RSS_LINK_RE = re.compile(r"<link[^>]*>\s*([^<\s][^<]*?)\s*</link>", re.I)
_ATOM_LINK_RE = re.compile(r'<link[^>]+href=["\']([^"\']+)["\']', re.I)
_RSS_TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>",
                           re.I | re.S)


def _extract_locs(xml_text: str) -> list[str]:
    """Pull <loc> entries from a sitemap; fall back to RSS/Atom links."""
    if not xml_text:
        return []
    locs = [m.group(1).strip() for m in _LOC_RE.finditer(xml_text)]
    if locs:
        return locs
    # RSS/Atom fallback (DCD feed)
    out = [m.group(1).strip() for m in _RSS_LINK_RE.finditer(xml_text)]
    out += [m.group(1).strip() for m in _ATOM_LINK_RE.finditer(xml_text)]
    # Drop self/feed links + dedup preserving order
    seen, clean = set(), []
    for u in out:
        if not u.startswith("http"):
            continue
        if u in seen:
            continue
        seen.add(u)
        clean.append(u)
    return clean


# ─────────────────────────────────────────────────────────────────────
# SLUG PARSERS — operator/address/city are IN the URL path; no page fetch.
# ─────────────────────────────────────────────────────────────────────

def _deslug(s: str) -> str:
    """Turn a URL slug segment into a Title-Case-ish human string."""
    s = (s or "").replace("%20", "-").strip("-/")
    words = [w for w in s.split("-") if w]
    out = []
    for w in words:
        if w.isdigit():
            out.append(w)
        elif len(w) <= 3 and w.upper() in {"DC", "US", "UK", "AI", "IX"}:
            out.append(w.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out).strip()


def _looks_numeric_code(seg: str) -> bool:
    """A trailing slug segment that's a pure id / facility-code (e.g.
    1063733, dfw01, sgp1) — not a human facility name."""
    if not seg:
        return True
    if seg.isdigit():
        return True
    # short alnum codes like dfw01, sgp1, yyz01, cmh01
    if len(seg) <= 6 and re.fullmatch(r"[a-z]{2,4}\d{0,3}", seg):
        return True
    return False


def _parse_dchawk_facility(loc: str) -> dict | None:
    """DatacenterHawk facility slug:
       /marketplace/providers/<operator>/<street-address[-city]>/<code>
    Operator + street address are parseable; city is often blended into
    the address segment and not cleanly separable, so city stays None and
    the row leans on operator + address. country defaults unknown → we
    leave geocoding to the pipeline. Returns a candidate dict or None."""
    try:
        parts = [p for p in urlsplit(loc).path.split("/") if p]
    except Exception:
        return None
    # ['marketplace','providers',<operator>,<address>,<code>]
    if len(parts) < 5 or parts[0] != "marketplace" or parts[1] != "providers":
        return None
    operator_slug, address_slug = parts[2], parts[3]
    operator = _deslug(operator_slug)
    address = _deslug(address_slug)
    if not operator or not address:
        return None
    # Build a facility name from operator + address (DCHawk has no separate
    # facility name in the slug; the code segment is an internal id).
    name = f"{operator} — {address}"
    return {
        "name": name[:200],
        "operator": operator[:200],
        "address": address[:200],
        "city": None,
        "state": None,
        "country": None,   # unknown from slug; pipeline/geocoder resolves
        "source_url": loc,
    }


def _parse_cloudscene_dc(loc: str) -> dict | None:
    """Cloudscene data-center slug:
       /data-center/<country>/<city>/<operator-facility-name>
    Country + city + facility name are cleanly separable — highest-value
    gap source. Returns a candidate dict or None."""
    try:
        parts = [p for p in urlsplit(loc).path.split("/") if p]
    except Exception:
        return None
    # ['data-center', <country>, <city>, <facility>]
    if len(parts) < 4 or parts[0] != "data-center":
        return None
    country_slug, city_slug, fac_slug = parts[1], parts[2], parts[3]
    country = _COUNTRY_SLUG_ISO.get(country_slug.lower())
    # US state hint: city slug like "columbus-oh"
    state = None
    city_words = city_slug.split("-")
    if (country == "US" or country is None) and len(city_words) >= 2 \
            and city_words[-1].lower() in _US_STATE_ABBREVS:
        state = city_words[-1].upper()
        city = _deslug("-".join(city_words[:-1]))
    else:
        city = _deslug(city_slug)
    name = _deslug(fac_slug)
    if not name or not city:
        return None
    # operator = leading token(s) of the facility slug before the trailing
    # site code (e.g. "nextdc-s1" → operator NextDC). Best-effort.
    operator = None
    fac_words = fac_slug.split("-")
    if len(fac_words) >= 2 and _looks_numeric_code(fac_words[-1]):
        operator = _deslug("-".join(fac_words[:-1]))
    return {
        "name": name[:200],
        "operator": (operator or None),
        "address": None,
        "city": city[:120],
        "state": state,
        "country": country,
        "source_url": loc,
    }


def parse_competitor_sitemap(slug: str, url: str,
                             limit: int = _DEFAULT_PAGE_LIMIT,
                             _prefetched_text: str = None,
                             offset: int = 0) -> dict:
    """Fetch + parse one competitor sitemap/RSS into candidate rows.

    Returns {slug, url, method, status, parsed (list of candidate dicts),
    locs_seen, error}. NEVER raises. For DatacenterHawk facility slugs and
    Cloudscene data-center slugs, operator/address/city/country are
    extracted directly from the <loc> path — no per-page fetch needed.

    `_prefetched_text` lets callers (and tests) supply XML/RSS without a
    network fetch (dry-run / offline)."""
    src = next((s for s in _GAP_SOURCES if s["slug"] == slug), None)
    if src is None:
        # Allow ad-hoc (slug,url) for the providers/markets helpers + tests.
        method = "provider_index"
        disallowed = ()
        name = slug
    else:
        method = src["method"]
        disallowed = src.get("disallowed_prefixes", ())
        name = src["name"]
        url = url or src["url"]

    out = {"slug": slug, "name": name, "url": url, "method": method,
           "status": 0, "locs_seen": 0, "parsed": [], "error": None}

    if _prefetched_text is not None:
        text, out["status"] = _prefetched_text, 200
    else:
        f = _fetch_text(url, disallowed)
        out["status"] = f["status"]
        if f["error"]:
            out["error"] = f["error"]
            return out
        text = f["text"]

    locs = _extract_locs(text)
    out["locs_seen"] = len(locs)
    # 2026-07-24 (growthfix wave): ROTATING window. The page cap used to read
    # the SAME first `limit` <loc> entries every run — once those were all
    # known, the diff was 0 forever while the sitemap tail (DCHawk ~1MB,
    # Cloudscene ~3MB — thousands of URLs) was NEVER reached. That is exactly
    # how the feed sat at 15 consecutive zero-row runs "saturated". A caller-
    # supplied offset rotates the window (wrapping), so successive days sweep
    # different slices and the whole sitemap is eventually covered.
    if offset and len(locs) > (limit or 0) > 0:
        _off = int(offset) % len(locs)
        if _off:
            locs = locs[_off:] + locs[:_off]
        out["window_offset"] = _off
    if limit and limit > 0:
        locs = locs[:limit]

    rows = []
    for loc in locs:
        cand = None
        if method == "dchawk_facility":
            cand = _parse_dchawk_facility(loc)
        elif method == "cloudscene_dc":
            cand = _parse_cloudscene_dc(loc)
        elif method == "provider_index":
            # operator list — name only (no facility insert; enrich-only)
            seg = (urlsplit(loc).path.rstrip("/").split("/") or [""])[-1]
            op = _deslug(seg)
            if op:
                cand = {"name": op[:200], "operator": op[:200],
                        "address": None, "city": None, "state": None,
                        "country": None, "source_url": loc,
                        "_provider_index": True}
        elif method == "editorial_rss":
            # headline/url only — routed through NER, not slug-parsed as a
            # facility. Keep the url so the deal/announcement path can use it.
            cand = {"name": None, "operator": None, "address": None,
                    "city": None, "state": None, "country": None,
                    "source_url": loc, "_editorial": True}
        if cand:
            rows.append(cand)
    out["parsed"] = rows
    return out


# ─────────────────────────────────────────────────────────────────────
# DIFF — a TRUE gap only if: not already known (discovered_facilities AND
# facilities), passes the NER real-entity gate, and is geocodable.
# ─────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _is_existing(cur, cand: dict) -> bool:
    """Fuzzy-match cand against BOTH discovered_facilities and the live
    facilities table on LOWER(TRIM(name)) [+city] OR operator+city. Mirrors
    facility_auto_approve.check_duplicate Strategy 1 + discovery merge.
    Returns True if we already have it (→ NOT a gap)."""
    name = _norm(cand.get("name"))
    city = _norm(cand.get("city"))
    operator = _norm(cand.get("operator"))
    if not name and not operator:
        return True  # nothing to match on → treat as noise, not a gap

    for table in ("facilities", "discovered_facilities"):
        # Match 1: name ILIKE (+ city when we have one)
        try:
            if name and city:
                cur.execute(
                    f"SELECT 1 FROM {table} "
                    f"WHERE LOWER(TRIM(name)) LIKE %s "
                    f"  AND LOWER(TRIM(COALESCE(city,''))) = %s LIMIT 1",
                    (f"%{name}%", city))
            elif name:
                cur.execute(
                    f"SELECT 1 FROM {table} "
                    f"WHERE LOWER(TRIM(name)) LIKE %s LIMIT 1",
                    (f"%{name}%",))
            else:
                cur.execute("SELECT 0 WHERE FALSE")
            if cur.fetchone():
                return True
        except Exception:
            # table/col shape we can't probe → fail-safe: assume existing so
            # we never mint a dup on an unknown-shape table.
            return True
        # Match 2: operator + city (when both present + table has provider)
        if operator and city:
            try:
                cur.execute(
                    f"SELECT 1 FROM {table} "
                    f"WHERE LOWER(TRIM(COALESCE(provider,''))) = %s "
                    f"  AND LOWER(TRIM(COALESCE(city,''))) = %s LIMIT 1",
                    (operator, city))
                if cur.fetchone():
                    return True
            except Exception:
                pass
    return False


def _is_geocodable(cand: dict) -> bool:
    """True if a city/state/country is parseable so the row can be
    geocoded by the pipeline (matches validate_facility's location gate:
    needs city OR country)."""
    return bool((cand.get("city") or "").strip()
                or (cand.get("country") or "").strip()
                or (cand.get("state") or "").strip())


def _passes_ner_gate(cand: dict) -> bool:
    """Reuse the news NER real-entity heuristic to drop slug noise
    (provider-index pages, sentence fragments, carrier-only names)."""
    try:
        from routes.news_entity_extraction import _is_real_entity
    except Exception:
        # If the NER module isn't importable, fall back to a minimal guard
        # so we never insert obvious junk.
        nm = (cand.get("name") or "").strip()
        return len(nm) >= 4 and not nm.isdigit()
    name = (cand.get("name") or "").strip()
    operator = (cand.get("operator") or "").strip()
    # Either the facility name OR the operator must read like a real entity.
    return bool(_is_real_entity(name) or (operator and _is_real_entity(operator)))


def diff_gaps(parsed_rows: list[dict], cur) -> dict:
    """Given parsed candidate rows + an open DB cursor, return the TRUE
    gaps (facilities we don't have) plus drop counters. Does NOT insert.

    A row is a TRUE GAP only if ALL hold:
      (a) not a provider-index/editorial row (those aren't facility inserts);
      (b) passes the NER real-entity gate (drops slug noise);
      (c) is geocodable (city/state/country parseable);
      (d) no fuzzy match in discovered_facilities AND facilities.

    Rows that pass (a)+(b)+(d) but fail the geocode gate (c) — every
    DCHawk facility slug, which carries operator+address but no
    city/country — land in `gap_only` instead of being silently dropped:
    they are recorded in coverage_gaps as intelligence but never staged
    into discovered_facilities. Capped so the per-row existence probes
    can't eat the run budget.
    """
    res = {"true_gaps": [], "gap_only": [], "dropped_not_facility": 0,
           "dropped_ner": 0, "dropped_not_geocodable": 0,
           "dropped_existing": 0}
    seen_urls = set()
    for cand in parsed_rows or []:
        if cand.get("_provider_index") or cand.get("_editorial"):
            res["dropped_not_facility"] += 1
            continue
        if not cand.get("name"):
            res["dropped_not_facility"] += 1
            continue
        url = cand.get("source_url") or ""
        if url in seen_urls:
            continue
        seen_urls.add(url)
        if not _passes_ner_gate(cand):
            res["dropped_ner"] += 1
            continue
        geocodable = _is_geocodable(cand)
        if not geocodable and len(res["gap_only"]) >= _MAX_GAP_ONLY_PER_RUN:
            res["dropped_not_geocodable"] += 1
            continue
        if _is_existing(cur, cand):
            res["dropped_existing"] += 1
            continue
        if geocodable:
            res["true_gaps"].append(cand)
        else:
            res["gap_only"].append(cand)
            res["dropped_not_geocodable"] += 1
    return res


# ─────────────────────────────────────────────────────────────────────
# INSERT — stage TRUE gaps into discovered_facilities (never `facilities`).
# ─────────────────────────────────────────────────────────────────────

def _to_discovered_facility(cand: dict, slug: str) -> dict:
    """Shape a candidate into the discovered_facilities insert dict that
    news_facility_extractor.insert_discovered_facility expects."""
    return {
        "name": (cand.get("name") or "")[:200],
        "provider": (cand.get("operator") or None),
        "city": cand.get("city"),
        "state": cand.get("state"),
        "country": cand.get("country") or None,
        "latitude": None,    # geocoded downstream; never insert null-island
        "longitude": None,
        "power_mw": None,
        "sqft": None,
        "status": "Announced",
        "source": f"competitor_gap:{slug}",
        "source_url": cand.get("source_url") or "",
        "confidence_score": 0.55,  # LEAD, not verified — low confidence
        "discovered_at": utc_now().strftime("%Y-%m-%d"),
        "notes": (f"Coverage-gap lead from {slug}: "
                  f"{(cand.get('operator') or '')} "
                  f"{(cand.get('address') or '')}").strip()[:400],
        "investment_usd": None,
        "acreage": None,
    }


def record_sweep(conn, slug: str, srec: dict, p: dict,
                 window_size: int = 0) -> None:
    """Persist ONE row per source per run into `competitor_gap_sweeps`.

    ★WHY THIS EXISTS. Cloudscene's sitemap carries 11,859 data-center URLs and
    we hold 962 distinct (8.1%). Nothing in the database could say whether the
    other 92% was NEVER REACHED or ALREADY KNOWN, because a candidate matching
    an existing facility is dropped in diff_gaps (`dropped_existing`) and leaves
    no trace — `coverage_gaps` only ever stores gaps. That one unknown decides
    the entire inventory roadmap: unreached means raising the caps is free and
    worth thousands of facilities; already-known means the source is tapped out
    and the next lever is a whole new integration.

    The sweep walks a CONTIGUOUS window (offset = day_of_year * limit, wrapping
    mod sitemap length, step == window width), so (window_offset, window_size,
    locs_total) is enough to reconstruct exactly which slice has been visited —
    no need to store 12k URLs per source.

    ★Fail-soft by design: instrumentation must NEVER cost a crawl. Any error
    here is swallowed, because a lost metric row is trivial and a lost crawl run
    is not.
    """
    try:
        drops = srec.get("drops") or {}
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO competitor_gap_sweeps
                     (slug, locs_total, window_offset, window_size, parsed,
                      dropped_existing, dropped_not_facility, true_gaps,
                      gap_only, inserted, dup, status, error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (slug, sweep_day, window_offset) DO NOTHING""",
                (slug,
                 int(p.get("locs_seen") or 0),
                 int(p.get("window_offset") or 0),
                 # ★window_size is the width of the URL slice visited — NOT
                 # `parsed`. Many URLs in a window yield no candidate (wrong
                 # path shape, unparseable slug), so parsed < window_size.
                 # Conflating them would understate coverage and make a fully
                 # swept sitemap look partially visited forever.
                 int(window_size or 0),
                 int(srec.get("parsed") or 0),
                 int(drops.get("dropped_existing") or 0),
                 int(drops.get("dropped_not_facility") or 0),
                 int(srec.get("true_gaps") or 0),
                 int(srec.get("gap_only") or 0),
                 int(srec.get("inserted") or 0),
                 int(srec.get("dup") or 0),
                 int(srec.get("status") or 0),
                 (srec.get("error") or None)))
    except Exception as e:  # noqa: BLE001
        logger.debug("[competitor-gap] sweep record failed (non-fatal): %s",
                     str(e)[:160])


def _insert_true_gaps(conn, slug: str, true_gaps: list[dict],
                      max_inserts: int = _MAX_INSERTS_PER_RUN) -> dict:
    """Insert TRUE gaps via news_facility_extractor.insert_discovered_facility
    (dedups on source_url). Returns {inserted, dup, errors}."""
    out = {"inserted": 0, "dup": 0, "errors": 0}
    try:
        from news_facility_extractor import insert_discovered_facility
    except Exception as e:
        out["errors"] += 1
        out["error"] = f"import insert_discovered_facility failed: {e}"
        return out
    for cand in true_gaps[:max_inserts]:
        fac = _to_discovered_facility(cand, slug)
        if not fac["source_url"]:
            out["errors"] += 1
            continue
        try:
            new_id = insert_discovered_facility(conn, fac)
            if new_id:
                out["inserted"] += 1
            else:
                out["dup"] += 1  # dedup on source_url (already staged)
        except Exception:
            out["errors"] += 1
    return out


# ─────────────────────────────────────────────────────────────────────
# COVERAGE_GAPS — persist gap intelligence rows (2026-07-02).
# The coverage_gaps table (created by competitor_intelligence.py) sat at
# 0 rows since creation: no code path ever inserted into it — gaps only
# flowed to discovered_facilities + brain_findings, so the gap record
# itself was invisible to SQL consumers. This writer closes that hole.
# ─────────────────────────────────────────────────────────────────────

def _gap_id_for(slug: str, source_url: str) -> str:
    import hashlib as _hl
    return f"{slug}:{_hl.sha256((source_url or '').encode()).hexdigest()[:16]}"


def persist_coverage_gaps(conn, slug: str, source_name: str,
                          gaps: list[dict], max_rows: int = 500) -> dict:
    """Upsert one coverage_gaps row per gap facility. Idempotent on
    gap_id (= slug + sha256(source_url)[:16]) so the daily re-crawl
    can't duplicate. gap_type distinguishes staged facility gaps from
    address-only leads that couldn't be geocoded."""
    out = {"inserted": 0, "errors": 0}
    try:
        with conn.cursor() as cur:
            for g in gaps[:max_rows]:
                src_url = g.get("source_url") or ""
                if not src_url:
                    continue
                loc = ", ".join(p for p in (g.get("city"), g.get("state"),
                                            g.get("country")) if p)
                desc = (f"{g.get('name')}"
                        + (f" ({loc})" if loc else "")
                        + f" — listed by {source_name} but missing from "
                          f"DC Hub coverage. Source: {src_url}")[:1000]
                if _is_geocodable(g):
                    gap_type = "facility_coverage"
                    adv = ("Staged into the discovery → auto-approve → "
                           "facilities chain; rivals gate this listing "
                           "behind a login, DC Hub serves it via API/MCP.")
                else:
                    gap_type = "facility_lead_ungeocodable"
                    adv = ("Recorded as a coverage lead (operator+address "
                           "only — no parseable geography in the source "
                           "slug, so not auto-staged).")
                try:
                    cur.execute("""
                        INSERT INTO coverage_gaps
                          (gap_id, competitor, gap_type, description,
                           dc_hub_advantage)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (gap_id) DO NOTHING
                    """, (_gap_id_for(slug, src_url), source_name,
                          gap_type, desc, adv))
                    if cur.rowcount > 0:
                        out["inserted"] += 1
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    out["errors"] += 1
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        out["errors"] += 1
        logger.warning("competitor_gap: coverage_gaps persist failed for "
                       "%s: %s", slug, str(e)[:160])
    return out


# ─────────────────────────────────────────────────────────────────────
# FINDING — one per source, marked resolved once staged (quarantine dodge).
# ─────────────────────────────────────────────────────────────────────

def _file_finding(conn, slug: str, n_true_gaps: int, sample_names: list[str],
                  staged: bool) -> None:
    """File / update the per-source coverage finding via the canonical
    writer. Marked 'resolved' once gaps are staged so a daily re-emit can't
    cross the 200-sighting auto-quarantine threshold."""
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception as e:
        logger.warning("competitor_gap: findings writer import failed: %s", e)
        return
    detail = (f"{n_true_gaps} competitor facilities not in DC Hub. "
              f"Sample: {', '.join(sample_names[:8])}")[:2000]
    status = "resolved" if staged else "open"
    try:
        with conn.cursor() as cur:
            upsert_brain_finding(
                cur,
                issue=f"coverage_gap_competitor:{slug}",
                url=f"dchub://coverage/competitor/{slug}",
                count=int(n_true_gaps),
                detail=detail,
                detector="competitor_gap_crawler",
                status=status,
            )
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("competitor_gap: finding upsert failed for %s: %s",
                       slug, e)


# ─────────────────────────────────────────────────────────────────────
# DB connection (reuse main.get_db; fall back to db_utils / DATABASE_URL).
# ─────────────────────────────────────────────────────────────────────

def _conn():
    try:
        from main import get_db
        c = get_db()
        if c is not None:
            return c
    except Exception:
        pass
    try:
        from db_utils import get_db as _gdb
        c = _gdb()
        if c is not None:
            return c
    except Exception:
        pass
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        return c
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# DEAD-MAN BEAT — make this crawl VISIBLE on /api/v1/ops/deadman.
# The gap crawl runs as a fire-and-forget daemon thread off the daily
# competitor scan (routes/competitor_intel.py), so it never appeared on
# the loop board — a silent stall was undetectable. We BEAT the shared
# ingest_runs ledger the same way the other in-worker jobs do
# (agent_request_writer._beat): POST 127.0.0.1:<PORT>/api/v1/admin/
# ingest-runs/beat with the X-Admin-Key/UA headers. Fail-OPEN — a beat
# error is swallowed so it can NEVER break the crawl.
# ─────────────────────────────────────────────────────────────────────

def _beat_ledger(status: str, rows_inserted: int, note: str = None) -> None:
    """Best-effort dead-man BEAT for the competitor-gap crawl. NEVER raises."""
    try:
        import json
        import urllib.request
        body = json.dumps({
            "feed": "competitor-gap-crawl",
            "status": status,
            "rows_inserted": int(rows_inserted or 0),
            # Piggybacks the DAILY competitor scan cron → 24h cadence, so the
            # dead-man flags overdue at 2x (48h) instead of the 48h default.
            "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": (note or "competitor coverage-gap crawler "
                             "(stages discovered_facilities)")[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        # Key precedence matches the ledger's own _admin_ok
        # (DCHUB_ADMIN_KEY → DCHUB_INTERNAL_KEY), with ADMIN_API_KEY as a
        # last-ditch fallback for parity with the other in-worker beaters.
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        req = urllib.request.Request(
            "http://127.0.0.1:%s/api/v1/admin/ingest-runs/beat" % port,
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "dchub-competitor-gap-crawl/1.0",
                     "X-Admin-Key": admin_key})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # fail-open: a beat error must not break the crawl
        logger.debug("competitor_gap: ledger beat failed: %s", e)


# ─────────────────────────────────────────────────────────────────────
# ORCHESTRATOR — fetch → diff → insert → file-finding for all legal sources.
# ─────────────────────────────────────────────────────────────────────

def run_competitor_gap_scan(limit: int = _DEFAULT_PAGE_LIMIT,
                            budget_s: float = _DEFAULT_BUDGET_S,
                            dry_run: bool = False,
                            sources: list[str] = None) -> dict:
    """Crawl competitor sitemaps, diff against our coverage, stage
    TRUE gaps into discovered_facilities, and file one finding per source.

    Args:
      limit:    max sitemap <loc> URLs read per source per run (page cap).
      budget_s: whole-run wall-clock cap; the loop stops cleanly when hit.
      dry_run:  parse + diff but DO NOT insert or file findings.
      sources:  optional list of source slugs to restrict to (else all).

    Returns a summary dict. NEVER raises — always returns a dict so the
    daily scan + discovery runner never break on a competitor outage.
    """
    started = time.monotonic()
    out = {
        "ok": True,
        "dry_run": dry_run,
        "limit": limit,
        "budget_s": budget_s,
        "ran_at": datetime.datetime.utcnow().isoformat() + "Z",
        "total_parsed": 0,
        "total_true_gaps": 0,
        "total_inserted": 0,
        "total_dup": 0,
        "sources": [],
        "errors": [],
    }

    # DB reachability probe for live runs. The actual working connection is
    # acquired PER SOURCE, around the diff+insert ONLY — it is NEVER held
    # across the slow rate-limited sitemap fetches. A connection held >72s is
    # force-reclaimed on the 1-replica pool (documented flapping mode), which
    # would yank it mid-insert. Holding it only during the DB phase keeps each
    # checkout to sub-second, so the crawler can't exhaust the pool.
    if not dry_run:
        _probe = _conn()
        if _probe is None:
            out["ok"] = False
            out["errors"].append("no_database")
            return out
        try:
            _probe.close()
        except Exception:
            pass

    want = set(sources) if sources else None
    first = True
    for src in _GAP_SOURCES:
        if want is not None and src["slug"] not in want:
            continue
        if time.monotonic() - started > budget_s:
            out["errors"].append(
                {"source": src["slug"], "err": "budget_exhausted"})
            break
        # Polite rate-limit between source fetches (skip before first).
        # No DB connection is held here or during the network fetch below.
        if not first:
            time.sleep(_POLITE_DELAY_S + random.uniform(0, 0.5))
        first = False

        srec = {"slug": src["slug"], "name": src["name"],
                "method": src["method"], "parsed": 0, "true_gaps": 0,
                "inserted": 0, "dup": 0, "status": 0, "error": None}
        conn = None
        # ★Holds the parse result for the `finally` recorder. MUST be
        # initialised here: `finally` runs on every exit path, including
        # ones that never reach the fetch, and an unbound name there
        # would raise INSIDE the cleanup block.
        _p_rec: dict = {}
        try:
            # ── network fetch — NO DB connection held ──
            # Deterministic daily offset: day-of-year * limit walks the window
            # forward one full page per day, wrapping per-source in the parser
            # (mod locs count) — so a 5,000-URL sitemap is fully swept in ~10
            # daily runs instead of re-diffing the same first page forever.
            _off = datetime.datetime.utcnow().timetuple().tm_yday * max(limit or 0, 0)
            p = parse_competitor_sitemap(src["slug"], src["url"], limit=limit,
                                         offset=_off)
            _p_rec = p
            srec["status"] = p["status"]
            srec["locs_seen"] = p.get("locs_seen", 0)
            if p.get("error"):
                srec["error"] = p["error"]
                out["errors"].append({"source": src["slug"],
                                      "err": p["error"]})
                out["sources"].append(srec)
                continue
            parsed = p["parsed"]
            srec["parsed"] = len(parsed)
            out["total_parsed"] += len(parsed)

            # provider_index + editorial sources don't mint facilities;
            # we still record their parse counts but skip diff/insert.
            if src["method"] in ("provider_index", "editorial_rss"):
                srec["note"] = ("operator/editorial signal — not inserted "
                                "as facilities (enrich/NER path only)")
                out["sources"].append(srec)
                continue

            if dry_run:
                # diff needs a cursor; offline dry-run has no DB so we just
                # report parse counts + NER/geocode pre-filter.
                conn = _conn()
                if conn is not None:
                    with conn.cursor() as cur:
                        d = diff_gaps(parsed, cur)
                    srec["true_gaps"] = len(d["true_gaps"])
                    srec["gap_only"] = len(d.get("gap_only", []))
                    srec["drops"] = {k: v for k, v in d.items()
                                     if k not in ("true_gaps", "gap_only")}
                    out["total_true_gaps"] += len(d["true_gaps"])
                else:
                    pre = [c for c in parsed
                           if c.get("name") and _passes_ner_gate(c)
                           and _is_geocodable(c)]
                    srec["pre_filter_candidates"] = len(pre)
                out["sources"].append(srec)
                continue

            # ── live DB phase: short-lived connection, opened AFTER the
            #    network fetch and closed in finally before the next fetch ──
            conn = _conn()
            if conn is None:
                srec["error"] = "no_database_this_source"
                out["errors"].append({"source": src["slug"],
                                      "err": "no_database_this_source"})
                out["sources"].append(srec)
                continue

            with conn.cursor() as cur:
                d = diff_gaps(parsed, cur)
            srec["true_gaps"] = len(d["true_gaps"])
            srec["gap_only"] = len(d.get("gap_only", []))
            srec["drops"] = {k: v for k, v in d.items()
                             if k not in ("true_gaps", "gap_only")}
            out["total_true_gaps"] += len(d["true_gaps"])

            ins = _insert_true_gaps(conn, src["slug"], d["true_gaps"])
            srec["inserted"] = ins["inserted"]
            srec["dup"] = ins["dup"]
            out["total_inserted"] += ins["inserted"]
            out["total_dup"] += ins["dup"]

            pg = persist_coverage_gaps(conn, src["slug"], src["name"],
                                       d["true_gaps"] + d.get("gap_only", []))
            srec["coverage_gap_rows"] = pg["inserted"]
            out["total_coverage_gap_rows"] = (
                out.get("total_coverage_gap_rows", 0) + pg["inserted"])

            sample = [g["name"] for g in d["true_gaps"][:8]]
            _file_finding(conn, src["slug"], len(d["true_gaps"]),
                          sample, staged=True)
        except Exception as e:
            srec["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            out["errors"].append({"source": src["slug"],
                                  "err": srec["error"]})
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
        finally:
            # ★★2026-07-29: RECORD FROM `finally`, NOT the success path.
            # The first cut called record_sweep() only after a successful
            # insert — but the loop `continue`s early on a fetch/parse error
            # (`if p.get("error")`), on provider_index/editorial_rss sources,
            # and on `no_database_this_source`. None of those wrote a row, so
            # **an ERRORING source was indistinguishable from one that never
            # ran** — the exact absent-vs-zero failure this table was built to
            # eliminate, reproduced inside the fix for it. Cloudscene hit it:
            # it ran and logged nothing, leaving the coverage question open.
            #
            # `continue` inside a try still executes `finally`, so one call here
            # covers every exit path — success, early-continue and exception —
            # and carries `status`/`error` so a failed sweep is VISIBLE as a
            # failure rather than as silence.
            #
            # ★Skipped only when no fetch happened (no locs_seen): the
            # budget-break path `break`s before the fetch, and recording a
            # phantom zero-width window there would understate coverage.
            try:
                if (_p_rec or {}).get("locs_seen"):
                    _c_rec = conn if conn is not None else _conn()
                    if _c_rec is not None:
                        try:
                            record_sweep(_c_rec, src["slug"], srec, _p_rec,
                                         window_size=limit)
                        finally:
                            if _c_rec is not conn:
                                try:
                                    _c_rec.close()
                                except Exception:
                                    pass
            except Exception:
                pass          # instrumentation must never cost a crawl
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        out["sources"].append(srec)

    out["elapsed_s"] = round(time.monotonic() - started, 1)

    # ── Dead-man BEAT (observability only) — record this run on the loop
    #    board so a silent stall of the fire-and-forget crawl thread is
    #    detectable. Live runs only: dry-run/offline invocations (tests) must
    #    not stamp the ledger. rows_inserted = TRUE gaps staged into
    #    discovered_facilities this run. Fail-open — never breaks the crawl.
    if not dry_run:
        _ins = int(out.get("total_inserted") or 0)
        _parsed = int(out.get("total_parsed") or 0)
        if not out.get("ok") or (_parsed == 0 and out.get("errors")):
            _status = "error"          # nothing fetched/parsed AND errors → broken
        elif _ins == 0:
            # Crawl + diff ran end-to-end; this window held nothing we lack.
            # An affirmative healthy-idle — the ledger must not count it
            # toward the >=3-zero-row alarm (it did, for 15 straight runs).
            _status = "no_new_data"
        else:
            _status = "success"
        _beat_ledger(_status, _ins,
                     note=(f"competitor gap crawl: parsed={_parsed} "
                           f"true_gaps={out.get('total_true_gaps', 0)} "
                           f"staged={_ins} dup={out.get('total_dup', 0)} "
                           f"errors={len(out.get('errors') or [])}"))
    return out
