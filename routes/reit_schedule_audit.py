"""
routes/reit_schedule_audit.py — REIT Schedule III coverage-FLOOR auditor (2026-07-28).

★ THIS IS NOT A FACILITY IMPORTER. Read this before extending it.

The plan was "REIT 10-Ks carry full property schedules — hundreds of facilities verify
per document". I fetched Digital Realty's 2025 10-K (CIK 1297996, acc
0001104659-26-015365) and that premise is WRONG: Schedule III is aggregated BY MARKET,
not by property. The real shape is

    North American Markets   Northern Virginia 16 ... Dallas 16 ... Chicago 7 ...
    EMEA Markets             London 13 ... Frankfurt 24 ... Paris 12 ...
    APAC Markets             Singapore 3 ... Sydney 4 ...

— a market name, a DATA-CENTER BUILDING COUNT, financials, and an acquisition-year
range. There are no facility names and no addresses anywhere in it. Nothing here can
create a facility row, and this module deliberately cannot write to
discovered_facilities.

What it IS worth: an AUDITED, SEC-filed per-market building count — a verification
ORACLE. Where our own count for that operator+market sits BELOW the filed owned-count,
we are provably missing coverage and we know exactly where and by how much.

★READ THE FLOOR CORRECTLY. Schedule III covers OWNED real estate only. These operators
also lease heavily, so our count being ABOVE the filed number is normal and not an
error — only BELOW is a provable gap. First run (DLR, 2025 10-K): 20 markets,
158 owned buildings, our 278 keepers across the same markets, and 4 markets under the
floor — New York 5/10, Sydney 1/4, Portland 1/3, Boston 1/2 = 11 buildings short.
London showed 70 against 13 owned, which is plausible leasing OR duplicate
attribution; the lane reports it, it does not assert which.

★SEC POLICY: EDGAR requires a descriptive User-Agent with contact info and asks for
<=10 req/s. Both are honoured below. Do not remove the User-Agent.

★VERIFIED SOURCES: Digital Realty parses (proven above). Equinix is configured but
UNVERIFIED — its Schedule III is a cross-reference into a separate financial-statement
exhibit, not inline where DLR's is, so its parse is expected to return nothing until
someone does that work. It is marked unverified rather than silently reporting zero.

Endpoints:
  GET/POST /api/v1/admin/reit-audit/run   JSON (fetches + compares; admin)
  GET      /admin/reit-audit               HTML
Kill: REIT_AUDIT_DISABLE=1
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)

reit_schedule_audit_bp = Blueprint("reit_schedule_audit", __name__)

_SEC_UA = "DC Hub Data Ingestion jonathan@dchub.cloud"
_TTL = 6 * 3600            # filings change quarterly at most
_cache: dict = {"ts": 0.0, "payload": None}
_lock = threading.Lock()

# provider = the exact discovered_facilities.provider string to compare against.
# verified = has this issuer's Schedule III actually been parsed successfully?
_ISSUERS = [
    {"cik": "1297996", "ticker": "DLR",  "provider": "Digital Realty", "verified": True},
    {"cik": "1101239", "ticker": "EQIX", "provider": "Equinix",        "verified": False},
]

# Region header words that bleed into the first market of each block.
_REGION_PREFIX = re.compile(
    r"^(?:North\s+)?American\s+Markets\s+|^EMEA\s+Markets\s+|^APAC\s+Markets\s+"
    r"|^Asia[\s-]*Pacific\s+Markets\s+|^Other\s+Markets\s+", re.I)


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def _disabled() -> bool:
    return (os.environ.get("REIT_AUDIT_DISABLE") or "").strip() == "1"


def _conn():
    try:
        import psycopg2 as _pg
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip())
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[reit-audit] db connect failed: %s", e)
        return None


def _get(url: str, timeout: int = 60) -> str | None:
    """EDGAR fetch with the policy-required descriptive User-Agent."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": _SEC_UA, "Accept-Encoding": "gzip, deflate"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if (r.headers.get("Content-Encoding") or "") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except Exception as e:
        logger.warning("[reit-audit] fetch %s failed: %s", url[:70], str(e)[:120])
        return None


def _latest_10k(cik: str) -> dict | None:
    js = _get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", timeout=30)
    if not js:
        return None
    try:
        d = json.loads(js)
        r = d["filings"]["recent"]
        for form, acc, doc, fd in zip(r["form"], r["accessionNumber"],
                                      r["primaryDocument"], r["filingDate"]):
            if form == "10-K":
                a = acc.replace("-", "")
                return {"name": d.get("name"), "filed": fd, "accession": acc,
                        "url": ("https://www.sec.gov/Archives/edgar/data/"
                                f"{int(cik)}/{a}/{doc}")}
    except Exception as e:
        logger.warning("[reit-audit] submissions parse failed: %s", str(e)[:120])
    return None


def _flatten(html: str) -> str:
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"&#\d+;|&nbsp;|&amp;", " ", t)
    return re.sub(r"\s+", " ", t)


def _parse_schedule_iii(html: str) -> list:
    """Extract [(market, owned_building_count)] from Schedule III.

    Anchors on the ALL-CAPS 'SCHEDULE III' heading (the lowercase form appears in the
    exhibit index and table of contents, which carry no data). Each row is
    '<Market> <count> $<financials…> <yyyy> - <yyyy>'; the trailing acquisition-year
    range is what makes a data row distinguishable from prose.
    """
    t = _flatten(html)
    starts = [m.start() for m in re.finditer(r"SCHEDULE III", t)]
    if not starts:
        return []
    seg = t[starts[0]: starts[0] + 12000]
    rows = re.findall(
        r"([A-Z][A-Za-z .'/&-]{2,34}?)\s+(\d{1,3})\s+\$?[\s\d,().$-]{20,}?"
        r"(\d{4})\s*-\s*(\d{4})", seg)
    out, seen = [], set()
    for name, cnt, _y0, _y1 in rows:
        name = _REGION_PREFIX.sub("", name.strip()).strip()
        if len(name) < 3 or name.lower().endswith("markets"):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((name, int(cnt)))
    return out


def _our_counts(c, provider: str) -> dict:
    if c is None:
        return {}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT lower(COALESCE(NULLIF(market,''), COALESCE(city,''))), COUNT(*) "
                "FROM discovered_facilities WHERE provider = %s "
                "  AND COALESCE(is_duplicate,0)=0 GROUP BY 1", (provider,))
            return {k: int(v) for k, v in cur.fetchall() if k}
    except Exception as e:
        logger.debug("[reit-audit] our_counts failed: %s", e)
        return {}


def _match(market: str, ours: dict) -> int:
    m = market.lower()
    return sum(v for k, v in ours.items() if m in k or k in m)


def _run_audit() -> dict:
    c = _conn()
    issuers = []
    for spec in _ISSUERS:
        rec = {"ticker": spec["ticker"], "provider": spec["provider"],
               "parse_verified": spec["verified"]}
        f = _latest_10k(spec["cik"])
        time.sleep(0.2)                       # SEC asks for <=10 req/s
        if not f:
            rec["error"] = "could not resolve latest 10-K"
            issuers.append(rec)
            continue
        rec.update({"filed": f["filed"], "accession": f["accession"],
                    "filing_url": f["url"], "issuer": f["name"]})
        html = _get(f["url"], timeout=90)
        time.sleep(0.2)
        if not html:
            rec["error"] = "could not fetch the 10-K document"
            issuers.append(rec)
            continue
        markets = _parse_schedule_iii(html)
        if not markets:
            rec["error"] = ("Schedule III not parseable inline — for this issuer it is a "
                            "cross-reference into a separate financial-statement exhibit. "
                            "Expected while parse_verified=false; NOT a coverage finding.")
            issuers.append(rec)
            continue
        ours = _our_counts(c, spec["provider"])
        below, ok, over = [], [], []
        for name, filed in markets:
            got = _match(name, ours)
            row = {"market": name, "filed_owned": filed, "ours": got}
            if got < filed:
                row["short_by"] = filed - got
                below.append(row)
            elif got > filed * 3:
                over.append(row)          # plausible leasing OR duplicate attribution
            else:
                ok.append(row)
        rec.update({
            "markets_in_filing": len(markets),
            "filed_owned_total": sum(n for _, n in markets),
            "our_keepers_same_markets": sum(_match(n, ours) for n, _ in markets),
            "below_floor": sorted(below, key=lambda r: -r["short_by"]),
            "buildings_short": sum(r["short_by"] for r in below),
            "far_above_floor": over,
            "at_or_above": len(ok),
        })
        issuers.append(rec)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "what_this_is": ("COVERAGE-FLOOR AUDIT, not an importer. Schedule III is "
                         "aggregated BY MARKET with no facility names or addresses, so "
                         "nothing here can create a facility row. It yields an audited "
                         "per-market OWNED building count."),
        "how_to_read": ("Schedule III is OWNED real estate only and these operators lease "
                        "heavily, so ours ABOVE filed is normal. Only BELOW is a provable "
                        "gap. far_above_floor is plausible leasing OR duplicate "
                        "attribution — it flags, it does not assert."),
        "issuers": issuers,
        "source": "SEC EDGAR",
    }


def _cached() -> dict:
    with _lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TTL:
            return _cache["payload"]
    p = _run_audit()
    with _lock:
        _cache["ts"] = time.time()
        _cache["payload"] = p
    return p


@reit_schedule_audit_bp.route("/api/v1/admin/reit-audit/run", methods=["GET", "POST"])
def reit_audit_run():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _lock:
            _cache["payload"] = None
    return jsonify(_cached())


@reit_schedule_audit_bp.route("/admin/reit-audit", methods=["GET"])
def reit_audit_dashboard():
    if _disabled():
        return Response("reit audit disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _cached()
    blocks = []
    for it in p["issuers"]:
        if it.get("error"):
            blocks.append(
                f"<div style='background:#0f172a;border:1px solid #eab308;border-radius:12px;"
                f"padding:14px;margin:12px 0'><b>{_esc(it['ticker'])}</b> "
                f"<span style='color:#94a3b8'>{_esc(it.get('provider',''))}</span><br>"
                f"<span style='color:#eab308;font-size:13px'>{_esc(it['error'])}</span></div>")
            continue
        rows = "".join(
            f"<tr><td style='padding:3px 10px'>{_esc(r['market'])}</td>"
            f"<td style='padding:3px 10px;text-align:right'>{r['filed_owned']}</td>"
            f"<td style='padding:3px 10px;text-align:right'>{r['ours']}</td>"
            f"<td style='padding:3px 10px;color:#ef4444'>short {r['short_by']}</td></tr>"
            for r in it.get("below_floor", []))
        blocks.append(
            f"<div style='background:#0f172a;border:1px solid "
            f"{'#ef4444' if it.get('buildings_short') else '#22c55e'};border-radius:12px;"
            f"padding:14px;margin:12px 0'>"
            f"<b>{_esc(it['ticker'])}</b> — {_esc(it.get('issuer',''))} · 10-K "
            f"{_esc(it.get('filed',''))} · <a href='{_esc(it.get('filing_url',''))}' "
            f"style='color:#60a5fa'>filing</a><br>"
            f"<span style='color:#94a3b8;font-size:13px'>"
            f"{it['markets_in_filing']} markets · {it['filed_owned_total']} owned "
            f"buildings filed · {it['our_keepers_same_markets']} ours · "
            f"<b style='color:#ef4444'>{it['buildings_short']} short</b> across "
            f"{len(it.get('below_floor',[]))} market(s)</span>"
            + (f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>"
               f"<tr style='color:#64748b'><td style='padding:3px 10px'>market</td>"
               f"<td style='padding:3px 10px'>filed</td><td style='padding:3px 10px'>ours</td>"
               f"<td></td></tr>{rows}</table>" if rows else "")
            + "</div>")
    html = ("<!doctype html><meta charset='utf-8'>"
            "<title>REIT Schedule III coverage floor · DC Hub</title>"
            "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,"
            "Segoe UI,Roboto,sans-serif;max-width:900px;margin:24px auto;padding:0 16px'>"
            "<h2 style='margin:0 0 4px'>REIT Schedule III — coverage floor</h2>"
            f"<div style='color:#64748b;font-size:12px;line-height:1.5'>"
            f"{_esc(p['what_this_is'])}<br><br>{_esc(p['how_to_read'])}<br><br>"
            f"generated {_esc(p['generated_at'])} · JSON "
            f"/api/v1/admin/reit-audit/run</div>" + "".join(blocks) + "</body>")
    return Response(html, mimetype="text/html")
