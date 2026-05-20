"""Phase FF+25-followup-r16 (2026-05-20) — DataCenterMap.com crawler.
==========================================================================

The user found DCHawk had facilities we didn't (Gryphon, Prairie Sky,
Bell Canada Regina) and asked for systematic coverage. After weighing
the ToS risk of scraping DCHawk, we chose DataCenterMap.com — a 100%
public, intentionally-crawlable global DC registry.

URLs walked (one per country, expanding as needed):
  https://www.datacentermap.com/<country>/
    e.g. /canada/, /united-kingdom/, /germany/, /singapore/...

Each country page lists facilities with a name + city + operator link.
We extract those three fields. For first-run coverage, that's enough to
seed the row — the existing discovery pipeline can backfill MW, status,
and address over subsequent nightly crawls.

ENDPOINTS:
  POST /api/v1/admin/dcm-crawl/run        admin: trigger one crawl now
                                            ?country=canada  scope to 1 country
                                            ?dry_run=1       no inserts
  GET  /api/v1/admin/dcm-crawl/status     last-run summary
  GET  /api/v1/admin/dcm-crawl/log        last 50 crawl runs

POLITENESS / SAFETY:
  · User-Agent: "DCHubCrawler/1.0 (+https://dchub.cloud/contact)"
  · 2-second sleep between requests (configurable via env)
  · Respects robots.txt — fetches and parses before each crawl
  · Caps at 250 facilities per run by default so a runaway can't blow
    up the DB
  · source='datacentermap' on every row so a single SQL can purge
    everything if needed:
      DELETE FROM facilities WHERE source = 'datacentermap';
  · Disabled by default via env var DCM_CRAWL_ENABLED — set to true
    in Railway to activate
"""
import os
import re
import time
import json
import logging
import datetime
import hashlib
from urllib.parse import urljoin, urlparse
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
datacentermap_crawler_bp = Blueprint("datacentermap_crawler", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ── Constants ────────────────────────────────────────────────────────
BASE     = "https://www.datacentermap.com"
USER_AGENT = "DCHubCrawler/1.0 (+https://dchub.cloud/contact)"
SLEEP_SEC = float(os.environ.get("DCM_CRAWL_SLEEP", "2.0"))
MAX_PER_RUN = int(os.environ.get("DCM_CRAWL_MAX", "250"))
ENABLED = (os.environ.get("DCM_CRAWL_ENABLED", "false")
            .lower() in ("1", "true", "yes"))

# Country slug list — extend as we want broader coverage. Starts with
# the regions where we know we're thin (per the brain coverage detector).
COUNTRIES = [
    "canada", "united-kingdom", "germany", "france", "ireland",
    "netherlands", "singapore", "japan", "australia", "brazil",
    "mexico", "india", "south-africa",
]


# ── robots.txt awareness ─────────────────────────────────────────────
_robots_cache: dict = {"fetched_at": 0, "allowed_prefixes": None,
                       "disallowed_prefixes": []}


def _check_robots() -> bool:
    """Read robots.txt and decide whether our UA can crawl the country
    pages. Returns True if allowed. Caches for 1 hour."""
    import urllib.request
    if time.time() - _robots_cache["fetched_at"] < 3600 \
       and _robots_cache["allowed_prefixes"] is not None:
        return True   # cached affirmative
    try:
        req = urllib.request.Request(
            f"{BASE}/robots.txt",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[dcm-crawl] robots fetch failed: {e}")
        return False
    # Simple parse: respect blanket Disallow lines under User-agent: *
    lines = text.splitlines()
    in_star = False
    disallows: list[str] = []
    for line in lines:
        s = line.split("#", 1)[0].strip()
        if not s: continue
        if s.lower().startswith("user-agent:"):
            in_star = s.split(":", 1)[1].strip() == "*"
            continue
        if in_star and s.lower().startswith("disallow:"):
            path = s.split(":", 1)[1].strip()
            if path: disallows.append(path)
    _robots_cache["fetched_at"] = time.time()
    _robots_cache["disallowed_prefixes"] = disallows
    _robots_cache["allowed_prefixes"] = []  # we don't need an allow list
    # Are country paths blocked?
    for d in disallows:
        for c in COUNTRIES:
            if f"/{c}".startswith(d):
                logger.warning(f"[dcm-crawl] /{c} blocked by robots.txt")
                return False
    return True


# ── Page fetch ───────────────────────────────────────────────────────
def _fetch(path: str) -> str | None:
    import urllib.request, urllib.error
    url = urljoin(BASE, path)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.info(f"[dcm-crawl] {path} → HTTP {e.code}")
        return None
    except Exception as e:
        logger.info(f"[dcm-crawl] {path} → {type(e).__name__}: {e}")
        return None


# ── Extraction ───────────────────────────────────────────────────────
# DataCenterMap country pages list facilities in a recognizable pattern.
# We use BOTH JSON-LD (if present) and a defensive HTML regex so the
# crawler degrades gracefully when the markup changes. Each row needs
# at minimum a name and a city — operator + address are nice-to-have.

_JSONLD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# Heuristic for facility links in DCM list pages
_FACILITY_LINK_RE = re.compile(
    r'<a\s+[^>]*href="(/[a-z\-]+/[a-z\-_]+/?)"[^>]*>([^<]{3,160})</a>',
    re.IGNORECASE,
)


def _extract_jsonld(html: str) -> list[dict]:
    """Pull schema.org rows from any JSON-LD blocks on the page."""
    out: list[dict] = []
    for m in _JSONLD_RE.finditer(html):
        blob = m.group(1).strip()
        try:
            data = json.loads(blob)
        except Exception:
            continue
        # Could be a single object, a list, or a graph
        candidates = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                candidates = data["@graph"]
            else:
                candidates = [data]
        for c in candidates:
            if not isinstance(c, dict): continue
            t = c.get("@type") or ""
            if isinstance(t, list): t = " ".join(t)
            if any(k in str(t) for k in
                    ("DataCenter", "Place", "LocalBusiness", "Organization")):
                out.append(c)
    return out


def _parse_country_page(html: str, country_slug: str) -> list[dict]:
    """Best-effort extraction of facility name + city + URL from a
    country index page. Returns list of {name, city, country, dcm_path}."""
    rows: list[dict] = []

    # First: prefer JSON-LD if present
    for blob in _extract_jsonld(html):
        name = (blob.get("name") or "").strip()
        if not name or len(name) > 200: continue
        addr = blob.get("address") or {}
        if isinstance(addr, dict):
            city = (addr.get("addressLocality")
                    or addr.get("addressRegion") or "").strip()
        else:
            city = ""
        rows.append({
            "name": name, "city": city,
            "country": country_slug.replace("-", " ").title(),
            "operator": (blob.get("brand") or blob.get("parentOrganization")
                         or {}).get("name") if isinstance(
                            blob.get("brand") or blob.get("parentOrganization"), dict) else None,
            "dcm_path": (blob.get("url") or "").replace(BASE, ""),
        })

    # Then: regex fallback for plain HTML list pages
    if not rows:
        seen = set()
        for m in _FACILITY_LINK_RE.finditer(html):
            path, name = m.group(1), m.group(2).strip()
            # Filter out menu links and aggregate pages
            if any(x in path for x in
                    ("/marketplace", "/articles", "/about",
                     "/contact", "/login", "#", "blog")):
                continue
            if name.lower() in ("home", "marketplace", "about", "contact"):
                continue
            if path in seen: continue
            seen.add(path)
            rows.append({
                "name": name, "city": "",
                "country": country_slug.replace("-", " ").title(),
                "operator": None,
                "dcm_path": path,
            })

    return rows


# ── Storage ──────────────────────────────────────────────────────────
def _ensure_log_table():
    c = _get_db()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dcm_crawl_log (
                    id              SERIAL PRIMARY KEY,
                    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at     TIMESTAMPTZ,
                    countries       TEXT[],
                    pages_fetched   INT NOT NULL DEFAULT 0,
                    facilities_seen INT NOT NULL DEFAULT 0,
                    facilities_new  INT NOT NULL DEFAULT 0,
                    facilities_dup  INT NOT NULL DEFAULT 0,
                    errors          INT NOT NULL DEFAULT 0,
                    dry_run         BOOLEAN NOT NULL DEFAULT FALSE,
                    notes           TEXT
                )
            """)
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning(f"[dcm-crawl] log table create failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


def _insert_facility(cur, f: dict) -> tuple[bool, str]:
    name = (f.get("name") or "").strip()
    if not name: return False, ""
    sid = "dcm_" + hashlib.sha256(name.encode()).hexdigest()[:16]
    # Already in DB?
    cur.execute(
        "SELECT 1 FROM facilities WHERE source_id = %s LIMIT 1",
        (sid,),
    )
    if cur.fetchone():
        return False, sid
    # Also check by name fuzzy — don't duplicate facilities we have under
    # a different source (e.g. from the existing crawler).
    cur.execute(
        "SELECT 1 FROM facilities WHERE LOWER(name) = LOWER(%s) LIMIT 1",
        (name,),
    )
    if cur.fetchone():
        return False, sid
    cur.execute("""
        INSERT INTO facilities
          (id, name, provider, city, state, country, power_mw,
           status, address, source, source_id)
        VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, 'datacentermap', %s)
    """, (
        sid, name, f.get("operator"),
        f.get("city"), f.get("state"), f.get("country", ""),
        "unknown", f.get("dcm_path") or None, sid,
    ))
    return True, sid


def _crawl(scope_country: str | None, dry_run: bool) -> dict:
    """Run one crawl pass. Best-effort, per-country error-isolated."""
    if not ENABLED and not dry_run:
        return {"ok": False,
                "error": "DCM_CRAWL_ENABLED env var not set to true",
                "hint": "Set DCM_CRAWL_ENABLED=true in Railway, or "
                        "pass ?dry_run=1 to scan without inserting."}

    if not _check_robots():
        return {"ok": False,
                "error": "robots.txt disallows country path crawl",
                "hint": "Inspect "
                        "https://www.datacentermap.com/robots.txt manually."}

    countries = [scope_country] if scope_country else COUNTRIES
    countries = [c for c in countries if c]
    summary = {
        "countries":       countries,
        "pages_fetched":   0,
        "facilities_seen": 0,
        "facilities_new":  0,
        "facilities_dup":  0,
        "errors":          0,
        "dry_run":         dry_run,
        "examples":        [],
        "started_at":      datetime.datetime.utcnow().isoformat() + "Z",
    }

    c = _get_db()
    _ensure_log_table()
    cap_hit = False

    try:
        for country in countries:
            if cap_hit: break
            html = _fetch(f"/{country}/")
            summary["pages_fetched"] += 1
            time.sleep(SLEEP_SEC)
            if not html:
                summary["errors"] += 1
                continue
            rows = _parse_country_page(html, country)
            summary["facilities_seen"] += len(rows)

            for f in rows:
                if summary["facilities_new"] >= MAX_PER_RUN:
                    cap_hit = True
                    break
                if dry_run or c is None:
                    summary["examples"].append({
                        "name": f.get("name"), "country": f.get("country"),
                        "city": f.get("city"), "operator": f.get("operator"),
                    })
                    summary["facilities_new"] += 1
                    continue
                try:
                    with c.cursor() as cur:
                        added, sid = _insert_facility(cur, f)
                    try: c.commit()
                    except Exception: pass
                    if added:
                        summary["facilities_new"] += 1
                        if len(summary["examples"]) < 25:
                            summary["examples"].append({
                                "name": f.get("name"),
                                "country": f.get("country"),
                                "source_id": sid,
                            })
                    else:
                        summary["facilities_dup"] += 1
                except Exception as e:
                    try: c.rollback()
                    except Exception: pass
                    summary["errors"] += 1
                    logger.info(f"[dcm-crawl] insert err: {str(e)[:100]}")

        summary["finished_at"] = datetime.datetime.utcnow().isoformat() + "Z"
        summary["ok"] = True

        # Persist a run record
        if c is not None:
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO dcm_crawl_log
                          (countries, pages_fetched, facilities_seen,
                           facilities_new, facilities_dup, errors,
                           dry_run, finished_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    """, (
                        countries, summary["pages_fetched"],
                        summary["facilities_seen"],
                        summary["facilities_new"],
                        summary["facilities_dup"],
                        summary["errors"], dry_run,
                    ))
                try: c.commit()
                except Exception: pass
            except Exception:
                try: c.rollback()
                except Exception: pass
    finally:
        try:
            if c is not None: c.close()
        except Exception: pass

    return summary


# ── Endpoints ────────────────────────────────────────────────────────
@datacentermap_crawler_bp.route("/api/v1/admin/dcm-crawl/run",
                                 methods=["POST"])
def crawl_run():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    country = (request.args.get("country") or "").strip().lower() or None
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    out = _crawl(country, dry_run)
    return jsonify(out)


@datacentermap_crawler_bp.route("/api/v1/admin/dcm-crawl/status",
                                 methods=["GET"])
def crawl_status():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, started_at, finished_at, countries,
                       pages_fetched, facilities_seen, facilities_new,
                       facilities_dup, errors, dry_run
                  FROM dcm_crawl_log
                 ORDER BY started_at DESC LIMIT 1
            """)
            r = cur.fetchone()
            if not r:
                return jsonify(ok=True, last_run=None,
                               enabled=ENABLED,
                               hint="No crawl runs yet. POST /run.")
            return jsonify(
                ok=True, enabled=ENABLED,
                last_run={
                    "id": r[0],
                    "started_at": str(r[1]) if r[1] else None,
                    "finished_at": str(r[2]) if r[2] else None,
                    "countries": r[3],
                    "pages_fetched": r[4],
                    "facilities_seen": r[5],
                    "facilities_new": r[6],
                    "facilities_dup": r[7],
                    "errors": r[8],
                    "dry_run": r[9],
                },
            )
    finally:
        try: c.close()
        except Exception: pass


@datacentermap_crawler_bp.route("/api/v1/admin/dcm-crawl/log",
                                 methods=["GET"])
def crawl_log():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, started_at, finished_at, countries,
                       facilities_seen, facilities_new, facilities_dup,
                       errors, dry_run
                  FROM dcm_crawl_log
                 ORDER BY started_at DESC LIMIT 50
            """)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "id": r[0],
                    "started_at": str(r[1]) if r[1] else None,
                    "finished_at": str(r[2]) if r[2] else None,
                    "countries": r[3],
                    "seen": r[4], "new": r[5], "dup": r[6],
                    "errors": r[7], "dry_run": r[8],
                })
        return jsonify(ok=True, count=len(rows), runs=rows)
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info(f"[dcm-crawl] ready · enabled={ENABLED} · "
                 f"sleep={SLEEP_SEC}s · max={MAX_PER_RUN}/run · "
                 f"{len(COUNTRIES)} countries")

_smoke()
