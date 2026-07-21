"""facility_dedup.py — cross-source facility de-duplication (2026-07-20).

WHY: `facilities` aggregates many sources (PeeringDB, OpenStreetMap, operator
directories, permits, curated seeds) for breadth, but nothing collapses the
source variants of the SAME physical site into one record. So one facility
appears many times — "Equinix SG1", "Equinix SG1 - Singapore", "Equinix SG1
Singapore", "Equinix Data Center SG1" are all one building. A paying customer
(Landry, landry2@) auditing APAC markets flagged it: ~15-58% redundancy.

DESIGN — conservative, NON-DESTRUCTIVE, reversible:
  * Signal is OPERATOR + SITE-TOKEN from the NAME, never coordinates: many rows
    use a country-centroid placeholder (SG -> 1.352,103.820) and true duplicates
    are geocoded km apart by different sources, so a coord-based merge would both
    false-merge (centroid collisions) and miss (geocode drift). Names are the
    reliable key here.
  * HIGH PRECISION over recall: only merge when brand AND site discriminator
    clearly match. Equinix SG1 vs SG2, STT Defu 1 vs 2, Keppel 1 vs 2 must stay
    separate. A missed duplicate is safe; a false merge hides a real distinct
    site and is not. When ambiguous, DON'T merge.
  * Non-destructive: never deletes. Marks duplicates via facilities.duplicate_of_id
    -> the canonical row's id (canonical rows keep it NULL). Fully reversible via
    /undo. Customer-facing read paths filter `duplicate_of_id IS NULL`.

Endpoints (admin-keyed):
  GET  /api/v1/admin/facility-dedup/analyze?country=SG   dry-run: proposed clusters
  POST /api/v1/admin/facility-dedup/apply?country=SG&confirm=1   mark duplicates
  POST /api/v1/admin/facility-dedup/undo?country=SG      clear the marks
  GET  /api/v1/admin/facility-dedup/export?country=SG    canonical-only list (CSV/JSON)
"""
from __future__ import annotations

import os
import re
import csv
import io
import math
import logging

from flask import Blueprint, request, jsonify, Response

logger = logging.getLogger("facility_dedup")
facility_dedup_bp = Blueprint("facility_dedup", __name__)


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _admin_ok():
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    return bool(expected) and provided == expected


# ── entity-resolution primitives ──────────────────────────────────────────

_CORP = re.compile(r'\b(pte|ltd|limited|inc|llc|gmbh|holdings?|group|'
                   r'corporation|corp|co|company|services|technologies|'
                   r'technology|solutions|division|operating|pty|sdn|bhd|'
                   r'international|communications?|telecom|telecommunications?)\b')
_GENERIC = {'', 'unknown', 'data center', 'data centre', 'datacenter',
            'n/a', 'none', 'null', 'data', 'pt', 'the', 'co'}

# Curated brand aliases for the operators that dominate the duplication — the
# name/provider strings that mean the SAME company. Maps a normalized token to
# a canonical brand key. High-value because hyperscalers + majors are listed
# under many names (AWS = Amazon Web Services = Amazon; STT = ST Telemedia).
_BRAND_ALIASES = {
    "aws": "aws", "amazon": "aws", "amazonwebservices": "aws",
    "microsoft": "microsoft", "azure": "microsoft", "msft": "microsoft",
    "google": "google", "gcp": "google", "googlecloud": "google",
    "meta": "meta", "facebook": "meta",
    "oracle": "oracle", "oraclecloud": "oracle", "oci": "oracle",
    "alibaba": "alibaba", "alibabacloud": "alibaba", "aliyun": "alibaba",
    "tencent": "tencent", "tencentcloud": "tencent",
    "huawei": "huawei", "huaweicloud": "huawei",
    "stt": "stt", "sttgdc": "stt", "sttelemedia": "stt",
    "sttelemediaglobaldatacentres": "stt", "sttelemediaglobal": "stt",
    "digitalrealty": "digitalrealty",
    "equinix": "equinix",
    "keppel": "keppel", "keppeldc": "keppel", "keppeldatacentres": "keppel",
    "globalswitch": "globalswitch",
    "ntt": "ntt", "nttcommunications": "ntt", "nttdata": "ntt", "nttglobal": "ntt",
    "colt": "colt", "coltdcs": "colt",
    "cyxtera": "cyxtera",
    "ironmountain": "ironmountain",
    "airtrunk": "airtrunk",
    "princetondigital": "pdg", "pdg": "pdg", "princetondg": "pdg",
    "gds": "gds",
    "telin": "telin", "telinsingapore": "telin",
    "telehouse": "telehouse",
    "1net": "1net", "onenet": "1net",
    "ascenix": "ascenix",
    "epsilon": "epsilon",
    "iseek": "iseek", "nextdc": "nextdc", "airtrunkoperating": "airtrunk",
    "vocus": "vocus", "megaport": "megaport", "canberra": "canberradc",
    "leaseweb": "leaseweb", "ovhcloud": "ovh", "ovh": "ovh",
    "spacedc": "spacedc", "hetzner": "hetzner", "phoenixnap": "phoenixnap",
    "singtel": "singtel", "starhub": "starhub", "m1": "m1",
    "chinamobile": "chinamobile", "chinaunicom": "chinaunicom",
}

# Distinctive site-locality tokens (Singapore/APAC) that discriminate sites of
# the same operator when there's no site number.
_LOCALITY = ("defu", "taiseng", "loyang", "jurong", "onenorth", "serangoon",
             "woodlands", "bedok", "katong", "tuas", "changi", "sciencepark",
             "kimchuan", "paya", "ulu", "mediahub", "eqix")


def _norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _brand_key(provider, name):
    """Canonical operator key. Prefer provider; fall back to name. Resolve
    through the curated alias map; else first 1-2 significant tokens."""
    for raw in (provider, name):
        base = _norm(raw)
        if not base or base in _GENERIC:
            continue
        base = _CORP.sub(' ', base)
        base = re.sub(r'\s+', ' ', base).strip()
        toks = [t for t in base.split() if t and t not in _GENERIC]
        if not toks:
            continue
        # try alias on collapsed first token, first-two, and de-spaced whole
        cand = [toks[0], ''.join(toks[:2]), ''.join(toks)]
        for k in cand:
            if k in _BRAND_ALIASES:
                return _BRAND_ALIASES[k]
        # not a known brand: use first token unless it's too generic/short
        t0 = toks[0]
        # a 2-word brand (digital realty, global switch, iron mountain) collapses
        two = ''.join(toks[:2])
        if two in _BRAND_ALIASES:
            return _BRAND_ALIASES[two]
        return t0
    return ''


_GEO_STOP = re.compile(
    r'\b(data ?cent(er|re)s?|dc|campus|colocation|colo|the|'
    r'singapore|singaporean|australia|australian|thailand|thai|'
    r'new zealand|zealand|aotearoa|nz|sg|au|th)\b')
_DIRECTION = ("north", "south", "east", "west", "central")


def _site_token(name, provider, brand):
    """The site discriminator that MUST match for a merge. Empty => no reliable
    discriminator (the caller then requires an identical name-core, which keeps
    "1-Net East" vs "1-Net North" and "North East Valley" vs "Palmerston North"
    apart on their own).

    Conservative by construction:
      * digit-glued brand tokens (5GN, 2degrees, 365main) are removed first so a
        BRAND digit is never read as a site number;
      * site codes accept a SINGLE leading letter (NEXTDC S1/M1/D1/A1/B1 are
        distinct cities) — the earlier 2-letter floor collapsed them to "1";
      * localities count only WITH an adjacent number (Tai Seng 1 vs 2 stay
        apart); bare directionals/localities are NOT tokens (too coarse — they
        wrongly merged Palmerston North with North East Valley)."""
    w = _norm(name) + " " + _norm(provider)
    w = _GEO_STOP.sub(' ', w)
    w = re.sub(r'\b\d{1,2}[a-z]{2,}\b', ' ', w)   # drop 5gn / 2degrees / 365main
    toks = [t for t in w.split() if t]
    codes = []        # glued letter+digit site codes (+ any hall suffix)
    prefixed = []     # "<short-alpha><number>" — city/hall abbrev + number
    bares = []        # a lone 1-2 digit number
    for i, t in enumerate(toks):
        if re.fullmatch(r'[a-z]{1,4}\d{1,2}', t) and t != brand:
            # bind a following hall number: DC6 2 -> dc62, FR2 6 -> fr26 (keeps
            # DC6-2 vs DC6-5 apart); a 3+ digit street number is NOT bound.
            if i + 1 < len(toks) and re.fullmatch(r'\d{1,2}', toks[i + 1]):
                codes.append(t + toks[i + 1])
            else:
                codes.append(t)
        elif re.fullmatch(r'\d{1,2}', t):
            # bind a preceding short non-brand token: "Col 1" -> col1, "Dal 1" ->
            # dal1, "SE 2" -> se2 (keeps different cities/regions apart even when
            # only the abbreviation, not the full city, is present).
            prec = toks[i - 1] if (i > 0 and re.fullmatch(r'[a-z]{1,4}', toks[i - 1])
                                   and toks[i - 1] != brand) else ""
            (prefixed if prec else bares).append(prec + t)
    if codes:
        return codes[-1]
    wns = "".join(toks)
    # locality ONLY when it carries a number (defu1, taiseng2)
    for loc in _LOCALITY:
        mnum = re.search(loc + r'(\d{1,2})', wns)
        if mnum:
            return loc + mnum.group(1)
    if prefixed:
        return prefixed[-1]
    if bares:
        return bares[-1]
    return ""


def _name_core(name):
    n = _norm(name)
    n = _GEO_STOP.sub(' ', n)
    return re.sub(r'\s+', '', n)


def _signature(country, name, provider):
    """(country, brand, site_token) — rows with identical signature AND a
    non-empty site_token are the same facility. When site_token is empty we
    fall back to identical name-core, but ONLY if the core carries a distinctive
    remainder beyond the brand (>=4 chars) — otherwise a generic "Keppel
    Singapore" placeholder would wrongly absorb distinct sites."""
    brand = _brand_key(provider, name)
    if not brand:
        return None  # unclusterable → never auto-merged
    # Ambiguous multi-hall listings ("Equinix ME1/ME2") carry two distinct site
    # codes — keep them standalone rather than fold into one of the halls.
    _w = _GEO_STOP.sub(' ', _norm(name) + ' ' + _norm(provider))
    _codes = set(x for x in re.findall(r'\b([a-z]{1,4}\d{1,2})\b', _w) if x != brand)
    if len(_codes) >= 2:
        return None
    tok = _site_token(name, provider, brand)
    if tok:
        return (country, brand, tok)
    core = _name_core(name)
    remainder = core.replace(brand, "", 1)
    if len(remainder) < 4:
        return None
    return (country, brand, "core:" + core)


# Source trust for canonical selection (higher = preferred as the survivor).
_SOURCE_RANK = {
    "provider_directory": 9, "curated_hyperscaler": 9, "curated_global": 8,
    "manual_seed": 8, "global_directory": 7, "peeringdb": 7, "PeeringDB": 7,
    "dchub_pipeline": 5, "seed": 4, "datacentermap": 6,
    "OpenStreetMap": 3, "openstreetmap": 3,
    "discovered_facilities_drain": 2,
}
_SG_CENTROIDS = {(1.352, 103.82), (1.35, 103.82)}


def _canon_score(r):
    """Higher = better survivor. Prefer real provider, real (non-centroid)
    coords, capacity, trusted source, and an informative (not junk) name."""
    name, provider, source, la, lo, power = (
        r["name"], r["provider"], r["source"], r["lat"], r["lon"], r["power_mw"])
    s = 0.0
    s += _SOURCE_RANK.get(source or "", 1)
    if provider and _norm(provider) not in _GENERIC:
        s += 3
    if power not in (None, 0):
        s += 3
    if la is not None and lo is not None:
        s += 1
        if (round(float(la), 2), round(float(lo), 2)) not in _SG_CENTROIDS:
            s += 2  # real coords, not the placeholder centroid
    nm = (name or "")
    # penalise junk names like "Singapore Singapore" / "SG6" / provider==name
    if provider and _norm(provider) == _norm(name):
        s -= 2
    if len(nm) >= 10:
        s += 1
    return s


def _load(country):
    c = _conn()
    if c is None:
        return None
    import psycopg2.extras
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Cluster only canonical-eligible rows — rows already marked as a
            # duplicate (e.g. by the geo cross-country pass) are resolved and must
            # not be re-clustered or overwritten.
            cur.execute("""
                SELECT id, name, provider, city, state, address, region,
                       source, source_url, status, power_mw,
                       COALESCE(latitude, lat) AS lat,
                       COALESCE(longitude, lon) AS lon, slug, canonical_slug
                FROM facilities WHERE country = %s AND duplicate_of_id IS NULL
            """, (country,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        try: c.close()
        except Exception: pass


# City extraction — the last-line safety net. If two rows in a signature group
# name DIFFERENT cities, they are different facilities (e.g. NTT Sydney DC1 vs
# NTT Melbourne DC1 both carry the generic code "dc1"), so the group is dropped.
_CITY_NORM = {"sidney": "sydney"}
_CITIES = (
    # APAC
    "sydney", "sidney", "melbourne", "brisbane", "perth", "adelaide", "darwin",
    "canberra", "hobart", "newcastle", "geelong", "townsville", "cairns",
    "auckland", "wellington", "christchurch", "dunedin", "hamilton",
    "palmerston", "tauranga", "bangkok", "chonburi", "singapore",
    "tokyo", "osaka", "seoul", "mumbai", "chennai", "bangalore", "hyderabad",
    "delhi", "noida", "jakarta", "hong kong", "taipei", "kuala lumpur", "manila",
    # North America metros
    "ashburn", "sterling", "manassas", "reston", "richmond", "dallas", "fort worth",
    "houston", "san antonio", "austin", "chicago", "elk grove", "phoenix", "mesa",
    "chandler", "atlanta", "santa clara", "san jose", "san francisco", "sunnyvale",
    "fremont", "los angeles", "el segundo", "portland", "hillsboro", "seattle",
    "quincy", "council bluffs", "omaha", "new albany", "columbus", "denver",
    "reno", "las vegas", "salt lake city", "new york", "newark", "secaucus",
    "piscataway", "boston", "miami", "toronto", "montreal", "vancouver",
    "boydton", "the dalles", "prineville", "cheyenne", "kansas city",
    # Europe metros
    "london", "slough", "frankfurt", "amsterdam", "paris", "marseille", "dublin",
    "madrid", "milan", "zurich", "geneva", "vienna", "warsaw", "oslo", "stockholm",
    "copenhagen", "helsinki", "brussels", "munich", "berlin", "hamburg",
    "sao paulo", "rio de janeiro", "santiago", "queretaro",
)


def _cities(name):
    n = _norm(name)
    out = set()
    for cn in _CITIES:
        if re.search(r'\b' + cn.replace(' ', r'\s') + r'\b', n):
            out.add(_CITY_NORM.get(cn, cn))
    return out


def _street_num(addr):
    """Leading street number of an address (a strong identity signal — two
    rows with different street numbers are different buildings)."""
    m = re.match(r'\s*(\d{1,6})\b', addr or "")
    return m.group(1) if m else None


def _real_coord(m):
    """(lat,lon) if the row has usable coordinates, else None (null / 0,0)."""
    la, lo = m.get("lat"), m.get("lon")
    if la is None or lo is None:
        return None
    try:
        la, lo = float(la), float(lo)
    except Exception:
        return None
    if la == 0 and lo == 0:
        return None
    return (la, lo)


def _haversine(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _weak_token(tok):
    """A token that carries NO location identity on its own: a bare ordinal
    ("1","2"), a generic per-site code an operator reuses in every city
    ("dc1","pop2","az1"), or a brand-only core match. Merging on a weak token
    across cities is the false-merge class the adversarial review found
    (NorthC 1 = Munich+Hamburg+Dusseldorf; STT DC2 = Bengaluru+Mumbai)."""
    return bool(re.fullmatch(r'\d{1,2}', tok)
                or tok.startswith("core:")
                or re.fullmatch(r'(dc|pop|poi|pa|mmr|mdf|idf|ix|az|dr)\d{1,2}', tok))


def _location_confirmed(members):
    """Positive evidence that EVERY member is in the same place — required
    before a weak token may merge. A single located member is NOT enough: that
    is exactly how "Meta Clonee 6" (Ireland, has a city) absorbed "Meta Hyperion
    Building 6" (Louisiana, no city / no coords) on the bare ordinal "6". So all
    members must independently confirm the SAME location, by one shared channel:
      * every member names the same structured city, OR
      * every member has real coordinates and they cluster within 25 km, OR
      * every member's NAME resolves to the same single city.
    If any member is unlocatable in the chosen channel, we cannot confirm — and
    do not merge."""
    cities = [_norm(m.get("city")) for m in members]
    if all(cities) and len(set(cities)) == 1:
        return True
    pts = [_real_coord(m) for m in members]
    if all(pts) and all(_haversine(pts[0], p) <= 25 for p in pts[1:]):
        return True
    ncities = [_cities(m["name"]) for m in members]
    if all(len(nc) == 1 for nc in ncities) and len({frozenset(nc) for nc in ncities}) == 1:
        return True
    return False


def _incompatible(members, tok):
    """True if members clearly belong to DIFFERENT physical sites. HARD blocks
    apply to every cluster (a unique operator site code like DFW18 is trusted
    across city-string variants such as Richardson vs Dallas):
      * 2+ distinct non-empty STATE codes;
      * 2+ distinct street numbers;
      * 2+ distinct name-parsed cities.
    WEAK tokens additionally require location agreement, because the token alone
    doesn't identify a site:
      * 2+ distinct structured cities  -> different sites;
      * no positive same-location evidence at all -> can't confirm, don't merge."""
    # Identical (specific) name in the same country = the SAME facility, even if
    # sources disagree on the city string (a site's suburb "Truganina" vs its
    # metro "Melbourne"). Only clusters that already share a real brand+token
    # signature reach here — generic brand-only names never do (their signature
    # is None), so this can't merge two distinct "Digital Realty" stubs.
    if len({_norm(m["name"]) for m in members}) == 1:
        return False
    # Coordinates within ~5 km CONFIRM the same site for a strong (non-weak)
    # token, overriding noisy location strings ("Derrimut" vs "Melbourne",
    # state "SAU" vs "SA"). Weak tokens (bare ordinals / DC1) are NOT trusted
    # this way — that's how ITB2 Deventer vs Apeldoorn (~13 km) stays split.
    if not _weak_token(tok):
        pts = [p for p in (_real_coord(m) for m in members) if p]
        if len(pts) >= 2 and all(_haversine(pts[0], p) <= 5 for p in pts[1:]):
            return False
    states = {_norm(m.get("state")) for m in members}
    states.discard("")
    if len(states) > 1:
        return True
    nums = {_street_num(m.get("address")) for m in members}
    nums.discard(None)
    if len(nums) > 1:
        return True
    ncities = set()
    for m in members:
        ncities |= _cities(m["name"])
    if len(ncities) > 1:
        return True
    # Structured-city conflict blocks EVERY token, not just weak ones: a code
    # like "ITB2" is an operator ordinal reused per city (Deventer vs Apeldoorn),
    # indistinguishable from a metro code without this. We accept losing the odd
    # metro/suburb merge (DFW18 Richardson vs Dallas stays two rows) to never
    # hide a real distinct site.
    cities = {_norm(m.get("city")) for m in members}
    cities.discard("")
    if len(cities) > 1:
        return True
    # Weak tokens carry no site identity — require positive same-location evidence.
    if _weak_token(tok) and not _location_confirmed(members):
        return True
    return False


def _cluster(rows):
    """Group by signature; return {signature: [rows]} for groups with >1 row.
    Singletons and unclusterable rows stay canonical. A group whose members
    resolve to different states/streets/cities (or a weak token with no
    same-location evidence) is dropped — safety over recall."""
    groups = {}
    exact = {}
    for r in rows:
        nm = _norm(r["name"])
        if not nm:
            continue
        sig = _signature("_", r["name"], r["provider"])
        if sig is not None:
            groups.setdefault(sig, []).append(r)
        # exact-duplicate key: identical name + city (catches literal repeat
        # ingestions of generic-named rows like "Digital Realty" x3 at one site
        # that carry no distinctive site token, so never get a signature).
        exact.setdefault((nm, _norm(r.get("city"))), []).append(r)
    out = {}
    for k, v in groups.items():
        if len(v) >= 2 and not _incompatible(v, k[2]):
            out[k] = v
    used = {id(m) for cl in out.values() for m in cl}
    for (nm, city), v in exact.items():
        if not city or len(v) < 2:
            continue  # need a city to be sure it's the same place
        rem = [m for m in v if id(m) not in used]
        if len(rem) >= 2:
            out[("exact", nm, city)] = rem
    return out


def _plan(country):
    """Return the merge plan: list of clusters, each with a chosen canonical and
    the duplicate ids that would point to it. Pure/no-write."""
    rows = _load(country)
    if rows is None:
        return None
    clusters = _cluster(rows)
    plan = []
    dup_total = 0
    for sig, members in clusters.items():
        members_sorted = sorted(members, key=_canon_score, reverse=True)
        canon = members_sorted[0]
        dups = members_sorted[1:]
        dup_total += len(dups)
        plan.append({
            "brand": sig[1], "site_token": sig[2],
            "canonical": {"id": canon["id"], "name": canon["name"],
                          "provider": canon["provider"], "source": canon["source"]},
            "duplicates": [{"id": d["id"], "name": d["name"],
                            "provider": d["provider"], "source": d["source"]}
                           for d in dups],
        })
    plan.sort(key=lambda p: len(p["duplicates"]), reverse=True)
    return {"country": country, "total_rows": len(rows),
            "clusters": len(plan), "duplicate_rows": dup_total,
            "canonical_rows": len(rows) - dup_total, "plan": plan}


def _ensure_columns():
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE facilities ADD COLUMN IF NOT EXISTS duplicate_of_id TEXT")
            cur.execute("ALTER TABLE facilities ADD COLUMN IF NOT EXISTS dedup_method TEXT")
            cur.execute("ALTER TABLE facilities ADD COLUMN IF NOT EXISTS dedup_at TIMESTAMPTZ")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_facilities_dupof "
                        "ON facilities(duplicate_of_id)")
    except Exception as e:
        logger.warning("[dedup] ensure columns failed: %s", str(e)[:140])
    finally:
        try: c.close()
        except Exception: pass


# ── endpoints ──────────────────────────────────────────────────────────────

@facility_dedup_bp.route("/api/v1/admin/facility-dedup/analyze")
def dedup_analyze():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    country = (request.args.get("country") or "").strip()
    if not country:
        return jsonify(ok=False, error="country required (ISO-2, e.g. SG)"), 400
    res = _plan(country)
    if res is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    verbose = request.args.get("verbose") == "1"
    if not verbose:
        res = {**res, "plan": res["plan"][:40]}
    return jsonify(ok=True, dry_run=True, **res), 200


@facility_dedup_bp.route("/api/v1/admin/facility-dedup/apply", methods=["POST"])
def dedup_apply():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    country = (request.args.get("country") or "").strip()
    if not country:
        return jsonify(ok=False, error="country required"), 400
    res = _plan(country)
    if res is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    if request.args.get("confirm") != "1":
        return jsonify(ok=True, dry_run=True, note="add ?confirm=1 to apply",
                       country=country, would_mark=res["duplicate_rows"],
                       clusters=res["clusters"]), 200
    _ensure_columns()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    marked = 0
    try:
        with c.cursor() as cur:
            for cl in res["plan"]:
                cid = cl["canonical"]["id"]
                for d in cl["duplicates"]:
                    try:
                        cur.execute(
                            "UPDATE facilities SET duplicate_of_id=%s, "
                            "dedup_method='brand+site_token/v1', dedup_at=NOW() "
                            "WHERE id=%s AND (duplicate_of_id IS NULL OR duplicate_of_id<>%s)",
                            (cid, d["id"], cid))
                        marked += cur.rowcount
                    except Exception:
                        pass
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, country=country, marked_duplicates=marked,
                   clusters=res["clusters"], canonical_rows=res["canonical_rows"]), 200


@facility_dedup_bp.route("/api/v1/admin/facility-dedup/undo", methods=["POST"])
def dedup_undo():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    country = (request.args.get("country") or "").strip()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    n = 0
    try:
        with c.cursor() as cur:
            if country:
                cur.execute("UPDATE facilities SET duplicate_of_id=NULL, dedup_method=NULL, "
                            "dedup_at=NULL WHERE country=%s AND duplicate_of_id IS NOT NULL",
                            (country,))
            else:
                cur.execute("UPDATE facilities SET duplicate_of_id=NULL, dedup_method=NULL, "
                            "dedup_at=NULL WHERE duplicate_of_id IS NOT NULL")
            n = cur.rowcount
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, cleared=n, country=country or "ALL"), 200


@facility_dedup_bp.route("/api/v1/admin/facility-dedup/export")
def dedup_export():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    country = (request.args.get("country") or "").strip()
    if not country:
        return jsonify(ok=False, error="country required"), 400
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 500
    import psycopg2.extras
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT name, provider, city, status, power_mw,
                       COALESCE(latitude, lat) AS lat, COALESCE(longitude, lon) AS lon,
                       source, source_url
                FROM facilities
                WHERE country=%s AND duplicate_of_id IS NULL
                ORDER BY provider NULLS LAST, name
            """, (country,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        try: c.close()
        except Exception: pass
    if request.args.get("format") == "json":
        return jsonify(ok=True, country=country, canonical=len(rows), facilities=rows), 200
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "provider", "city", "status", "power_mw", "lat", "lon", "source"])
    for r in rows:
        w.writerow([r["name"], r["provider"], r["city"], r["status"],
                    r["power_mw"], r["lat"], r["lon"], r["source"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=dchub_{country}_canonical.csv"})
