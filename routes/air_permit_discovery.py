"""
routes/air_permit_discovery.py — EPA air-permit facility discovery (2026-07-28).

★ THE POINT: this is the first acquisition source that yields NAMED facilities WITH
STREET ADDRESSES. A data centre of any size must permit its backup generators, and
that permit is filed under the facility's own name and street address. Nothing else
we ingest carries addresses at that rate — the first clean run returned 239 gated
candidates, 100% with a street address, of which 139 were NOT in our data.

★WRONG CREDENTIAL WARNING: this does NOT use EPA_AQS_API_KEY. AQS is the Air Quality
System — ambient pollutant MONITORING (air_permitting_data.py holds 23,028 AQS
monitors, 113 nonattainment areas, 83 Class I areas). That is siting CONTEXT ("is
this parcel in nonattainment, so permitting is harder"), NOT a facility registry, and
it cannot discover anything. The right source is EPA ECHO, which needs no key.

★ECHO IS A TWO-STEP API: air_rest_services.get_facilities returns a QueryID; you then
fetch rows with get_qid?qid=…&pageno=1. The first call returns NO facility rows — a
single-call implementation looks like "0 results" and is wrong. Field names are
AIRName / AIRStreet / AIRCity / AIRState / AIRZip / RegistryID (NOT FacName).
★p_naics=518210 does NOT filter — it returned 279,665 rows nationally and no NAICS
column comes back. Search by facility NAME (p_fn) instead.

★★THE PRECISION GATE IS THE WHOLE DESIGN. A loose name match writes garbage: querying
"SWITCH LTD" returned 76 rows of switching stations, switchgear manufacturers,
telecom switch offices and literally SWITCHGRASS. That is exactly how dchub_pipeline
came to write "Dominion None" as a facility. So a candidate is kept only if its name
self-identifies as a data centre OR matches a tight operator allowlist. The gate is
deliberately conservative and DOES reject true positives — e.g. "APPLE INC." (Maiden,
NC) is really Apple's data centre but does not say so in its name. Under-collecting is
the safe direction; switchgrass in the facility table is not.

★CLASSIFICATION, NOT A DECISION: many gated hits are ENTERPRISE data centres (Walmart,
Allstate, Honda, Barclays, Bloomberg, Albuquerque Public Schools) — genuinely data
centres, but corporate IT rather than colo/hyperscale. Whether they belong in DC Hub's
inventory is a PRODUCT call, so every row is tagged facility_type='colo_operator' or
'enterprise' and the operator decides. This module does not decide for you.

★IDEMPOTENT BY CONSTRUCTION: source='epa_echo_air' with source_id=EPA RegistryID (a
stable federal identifier) against the (source, source_id) unique index, via
ON CONFLICT DO UPDATE. Re-running cannot duplicate — the failure that turned 15k
facilities into "21.7k" (see the 2026-07-28 source-normalization work).

★DISARMED by default (AIR_PERMIT_INGEST_ARM). Unarmed it reports exactly what it WOULD
write and writes nothing.

Endpoints:
  GET/POST /api/v1/admin/air-permits/discover   dry-run report (admin)
      POST ?arm=1  writes, only if AIR_PERMIT_INGEST_ARM=1 is also set
Kill: AIR_PERMIT_DISABLE=1
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

air_permit_discovery_bp = Blueprint("air_permit_discovery", __name__)

_UA = "DC Hub Data Ingestion jonathan@dchub.cloud"
_ECHO = "https://echodata.epa.gov/echo/air_rest_services"

# High-precision name queries only. Anything looser (e.g. a bare "SWITCH") drags in
# switchgear and switchgrass — measured, not hypothetical.
_PATTERNS = ("DATA CENTER", "DATACENTER", "DATA CENTRE", "EQUINIX", "DIGITAL REALTY",
             "QTS", "CYRUSONE", "VANTAGE DATA", "STACK INFRA", "ALIGNED ENERGY",
             "ALIGNED DATA", "CORESITE", "EDGECONNEX", "FLEXENTIAL", "TIERPOINT",
             "COLOGIX", "DATABANK", "365 DATA", "PRIME DATA", "NTT DATA CENTER")

_DC_RE = re.compile(r"DATA\s*CENT(?:ER|RE)|DATACENTER", re.I)
_OPERATORS = ("EQUINIX", "DIGITAL REALTY", "QTS ", "CYRUSONE", "VANTAGE DATA",
              "STACK INFRA", "ALIGNED ENERGY", "ALIGNED DATA", "CORESITE",
              "EDGECONNEX", "FLEXENTIAL", "TIERPOINT", "COLOGIX", "DATABANK",
              "365 DATA", "PRIME DATA")
_MAX_ROWS_PER_PATTERN = 3000     # a pattern this broad is not a data-centre query


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def _disabled() -> bool:
    return (os.environ.get("AIR_PERMIT_DISABLE") or "").strip() == "1"


def _armed() -> bool:
    return (os.environ.get("AIR_PERMIT_INGEST_ARM") or "").strip() == "1"


def _api(url: str, timeout: int = 90):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _conn(primary: bool = False):
    try:
        import psycopg2 as _pg
        if primary:
            url = ((os.environ.get("DATABASE_URL") or "").strip()
                   or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        else:
            url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
                   or (os.environ.get("DATABASE_URL") or "").strip())
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[air-permit] db connect failed: %s", e)
        return None


def _classify(name: str) -> str:
    n = (name or "").upper()
    return "colo_operator" if any(o in n for o in _OPERATORS) else "enterprise"


def _gate(name: str) -> bool:
    n = (name or "").upper()
    return bool(_DC_RE.search(n)) or any(o in n for o in _OPERATORS)


def _harvest() -> tuple[dict, list]:
    """Returns ({registry_id: row}, [notes]). Gated, deduped by EPA RegistryID."""
    kept: dict = {}
    notes: list = []
    for p in _PATTERNS:
        try:
            q = _api(f"{_ECHO}.get_facilities?output=JSON&p_fn="
                     + urllib.parse.quote(p), timeout=60)
            r = q.get("Results", {}) or {}
            qid, rows = r.get("QueryID"), int(r.get("QueryRows") or 0)
            if not qid or rows == 0:
                notes.append(f"{p}: 0 rows")
                continue
            if rows > _MAX_ROWS_PER_PATTERN:
                notes.append(f"{p}: {rows} rows — too broad, skipped")
                continue
            time.sleep(0.3)                      # ECHO courtesy
            d = _api(f"{_ECHO}.get_qid?output=JSON&qid={qid}&pageno=1")
            fac = (d.get("Results", {}) or {}).get("Facilities") or []
            added = 0
            for f in fac:
                rid = (f.get("RegistryID") or "").strip()
                nm = (f.get("AIRName") or "").strip()
                if not rid or not nm or not _gate(nm) or rid in kept:
                    continue
                kept[rid] = {
                    "registry_id": rid,
                    "name": nm,
                    "street": (f.get("AIRStreet") or "").strip() or None,
                    "city": (f.get("AIRCity") or "").strip() or None,
                    "state": (f.get("AIRState") or "").strip() or None,
                    "zip": (f.get("AIRZip") or "").strip() or None,
                    "facility_type": _classify(nm),
                    "source_url": ("https://echo.epa.gov/detailed-facility-report"
                                   f"?fid={urllib.parse.quote(rid)}"),
                }
                added += 1
            notes.append(f"{p}: {rows} rows -> +{added} gated")
            time.sleep(0.3)
        except Exception as e:
            notes.append(f"{p}: ERROR {str(e)[:70]}")
    return kept, notes


def _split_new(c, rows: dict) -> tuple[list, list]:
    """Split into (already-known, new) by normalised name within the same city."""
    if c is None:
        return [], list(rows.values())
    bycity: dict = {}
    try:
        with c.cursor() as cur:
            cur.execute("SELECT lower(regexp_replace(COALESCE(name,''),"
                        "'[^a-z0-9]','','gi')), lower(COALESCE(city,'')) "
                        "FROM discovered_facilities WHERE COALESCE(is_duplicate,0)=0")
            for n, city in cur.fetchall():
                if n:
                    bycity.setdefault(city or "", set()).add(n)
    except Exception as e:
        logger.debug("[air-permit] existing-load failed: %s", e)
        return [], list(rows.values())
    known, new = [], []
    for v in rows.values():
        n = re.sub(r"[^a-z0-9]", "", (v["name"] or "").lower())
        city = (v["city"] or "").lower()
        hit = any(o and n and (o in n or n in o or (len(o) > 8 and o[:10] in n))
                  for o in bycity.get(city, ()))
        (known if hit else new).append(v)
    return known, new


def _write(rows: list) -> dict:
    """ON CONFLICT (source, source_id) — re-running cannot duplicate."""
    pc = _conn(primary=True)
    if pc is None:
        return {"written": 0, "error": "no primary db"}
    n = 0
    try:
        with pc.cursor() as cur:
            for v in rows:
                cur.execute("""
                    INSERT INTO discovered_facilities
                      (source, source_id, name, city, state, address, facility_type,
                       source_url, is_duplicate, discovered_at, first_seen, last_updated)
                    VALUES ('epa_echo_air', %s, %s, %s, %s, %s, %s, %s, 0,
                            NOW() ON CONFLICT DO NOTHING, NOW(), NOW())
                    ON CONFLICT (source, source_id) DO UPDATE
                       SET name = EXCLUDED.name,
                           address = COALESCE(EXCLUDED.address, discovered_facilities.address),
                           last_updated = NOW()
                """, (v["registry_id"], v["name"], v["city"], v["state"],
                      v["street"], v["facility_type"], v["source_url"]))
                n += 1
    except Exception as e:
        return {"written": n, "error": str(e)[:200]}
    finally:
        try:
            pc.close()
        except Exception:
            pass
    return {"written": n}


@air_permit_discovery_bp.route("/api/v1/admin/air-permits/discover",
                              methods=["GET", "POST"])
def air_permits_discover():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403

    rows, notes = _harvest()
    c = _conn()
    known, new = _split_new(c, rows)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass

    want_write = (request.method == "POST"
                  and (request.args.get("arm") or "") == "1")
    out = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "EPA ECHO air_rest_services (no API key required)",
        "gated_candidates": len(rows),
        "with_street_address": sum(1 for v in rows.values() if v["street"]),
        "already_known": len(known),
        "new_to_us": len(new),
        "by_type": {
            "colo_operator": sum(1 for v in new if v["facility_type"] == "colo_operator"),
            "enterprise": sum(1 for v in new if v["facility_type"] == "enterprise"),
        },
        "product_call": ("A large share of gated hits are ENTERPRISE data centres "
                         "(corporate IT), not colo/hyperscale. Every row is tagged "
                         "facility_type so you decide what counts — this module does "
                         "not decide for you."),
        "gate_note": ("Conservative by design: it rejects true positives whose name "
                      "does not say 'data center' (e.g. APPLE INC. at Maiden NC). "
                      "Under-collecting beats writing switchgear and switchgrass — a "
                      "bare 'SWITCH' query returned 76 such rows."),
        "armed": _armed(),
        "patterns": notes,
        "sample_new": new[:15],
    }
    if want_write and not _armed():
        out["write"] = {"written": 0,
                        "skipped": "AIR_PERMIT_INGEST_ARM=1 is not set — refusing to write"}
    elif want_write:
        out["write"] = _write(new)
    else:
        out["write"] = {"written": 0, "skipped": "dry run — POST ?arm=1 to write"}
    return jsonify(out)
