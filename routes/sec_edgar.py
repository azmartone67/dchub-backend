"""
sec_edgar.py — SEC EDGAR filings extractor for DC-relevant companies.

Pulls submissions JSON (free, no auth) for ~16 tracked companies covering
hyperscalers, DC REITs, power utilities supplying hyperscalers, and
critical infrastructure plays.

Uses the standard SEC submissions JSON API:
    https://data.sec.gov/submissions/CIK<10-digit-zero-padded-cik>.json

Returns recent filings: 8-K (current report — capex, M&A, material events),
10-K (annual), 10-Q (quarterly), S-1 (IPO), 13F (institutional holdings).

Schema: sec_filings table (auto-created on first call).
Endpoints:
  POST /api/v1/sec/extract              run extraction (all companies)
  POST /api/v1/sec/extract/<ticker>     run for one company
  GET  /api/v1/sec/filings              list recent filings (limit, since, type, cik)
  GET  /api/v1/sec/filings/<cik>        per-company filings
  GET  /api/v1/sec/companies            list tracked companies + filing counts
  GET  /api/v1/sec/health               last extraction time
"""

import os
import json
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone, date

import psycopg2 as _pg
from flask import Blueprint, jsonify, request

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs): pass


sec_edgar_bp = Blueprint("sec_edgar_filings", __name__, url_prefix="/api/v1/sec")
SOURCE_ID = "sec-edgar-filings"

# DC-relevant company CIK registry. CIKs are SEC's canonical company IDs.
# Format: ticker -> (CIK, display_name, category)
TRACKED_COMPANIES = {
    # Hyperscalers (top buyers of DC capacity)
    "MSFT":  ("0000789019",  "Microsoft Corporation",          "hyperscaler"),
    "GOOGL": ("0001652044",  "Alphabet Inc.",                  "hyperscaler"),
    "AMZN":  ("0001018724",  "Amazon.com Inc.",                "hyperscaler"),
    "META":  ("0001326801",  "Meta Platforms Inc.",            "hyperscaler"),
    "AAPL":  ("0000320193",  "Apple Inc.",                     "hyperscaler"),
    "ORCL":  ("0001341439",  "Oracle Corporation",             "hyperscaler"),
    "TSLA":  ("0001318605",  "Tesla Inc.",                     "hyperscaler"),

    # Pure-play DC REITs / colos
    "EQIX":  ("0001101239",  "Equinix Inc.",                   "dc_reit"),
    "DLR":   ("0001297996",  "Digital Realty Trust Inc.",      "dc_reit"),
    "IRM":   ("0001020569",  "Iron Mountain Inc.",             "dc_reit"),

    # Power infrastructure (supplying hyperscalers)
    "CEG":   ("0001868275",  "Constellation Energy Corp.",     "power"),
    "VST":   ("0001692819",  "Vistra Corp.",                   "power"),
    "NRG":   ("0001013871",  "NRG Energy Inc.",                "power"),
    "TLN":   ("0001839526",  "Talen Energy Corp.",             "power"),

    # Critical infrastructure / silicon
    "VRT":   ("0001674101",  "Vertiv Holdings Co.",            "infra"),
    "NVDA":  ("0001045810",  "NVIDIA Corporation",             "silicon"),
    "AMD":   ("0000002488",  "Advanced Micro Devices Inc.",    "silicon"),
}


def _dsn(): return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS sec_filings_v2 (
    id                  BIGSERIAL PRIMARY KEY,
    accession_number    TEXT UNIQUE NOT NULL,
    cik                 TEXT NOT NULL,
    ticker              TEXT,
    company_name        TEXT,
    category            TEXT,
    form_type           TEXT NOT NULL,
    filing_date         DATE NOT NULL,
    accepted_at         TIMESTAMPTZ,
    primary_doc_url     TEXT,
    primary_doc_desc    TEXT,
    items               TEXT,
    metadata            JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_sec_filings_v2_cik_date    ON sec_filings_v2 (cik, filing_date DESC);
CREATE INDEX IF NOT EXISTS ix_sec_filings_v2_form_date   ON sec_filings_v2 (form_type, filing_date DESC);
CREATE INDEX IF NOT EXISTS ix_sec_filings_v2_filing_date ON sec_filings_v2 (filing_date DESC);
CREATE INDEX IF NOT EXISTS ix_sec_filings_v2_ticker      ON sec_filings_v2 (ticker);
"""


def _ensure_table():
    if getattr(_ensure_table, "_done", False): return
    with _conn() as c, c.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        c.commit()
    _ensure_table._done = True


def _fetch_company_submissions(cik):
    """Pull EDGAR submissions JSON for a CIK. SEC requires UA with contact info."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    req = urllib.request.Request(
        url,
        headers={
            # SEC requires a User-Agent identifying your application + contact email
            "User-Agent": "DC Hub Intelligence azmartone@gmail.com",
            "Accept": "application/json",
            "Host": "data.sec.gov",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"SEC returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


# Form types we care about for DC intelligence
RELEVANT_FORMS = {"8-K", "10-K", "10-Q", "S-1", "13F", "S-1/A", "8-K/A", "10-K/A", "10-Q/A", "DEF 14A"}


def _persist_filings(filings, cik, ticker, company_name, category):
    """Bulk insert filings, dedupe on accession_number."""
    if not filings: return 0
    rows_inserted = 0
    with _conn() as c, c.cursor() as cur:
        for f in filings:
            try:
                cur.execute(
                    """INSERT INTO sec_filings_v2
                          (accession_number, cik, ticker, company_name, category,
                           form_type, filing_date, accepted_at,
                           primary_doc_url, primary_doc_desc, items, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (accession_number) DO NOTHING""",
                    (
                        f["accession_number"],
                        cik,
                        ticker,
                        company_name,
                        category,
                        f["form_type"],
                        f["filing_date"],
                        f.get("accepted_at"),
                        f.get("primary_doc_url"),
                        f.get("primary_doc_desc"),
                        f.get("items"),
                        json.dumps(f.get("metadata") or {}),
                    ),
                )
                if cur.rowcount > 0:
                    rows_inserted += 1
            except Exception:
                pass
        c.commit()
    return rows_inserted


def _parse_recent_filings(submissions_json, cik):
    """Extract recent filings from EDGAR submissions JSON."""
    out = []
    recent = (submissions_json.get("filings") or {}).get("recent") or {}
    if not recent:
        return out

    accession_nums = recent.get("accessionNumber", [])
    forms          = recent.get("form", [])
    dates          = recent.get("filingDate", [])
    accepted       = recent.get("acceptanceDateTime", [])
    primary_docs   = recent.get("primaryDocument", [])
    primary_descs  = recent.get("primaryDocDescription", [])
    items          = recent.get("items", [])

    for i in range(len(accession_nums)):
        form_type = forms[i] if i < len(forms) else ""
        if form_type not in RELEVANT_FORMS:
            continue

        acc = accession_nums[i]
        # Build URL: https://www.sec.gov/Archives/edgar/data/<cik>/<acc-no-hyphens>/<primary_doc>
        acc_no_hyphens = acc.replace("-", "")
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no_hyphens}/{primary_doc}" if primary_doc else None

        out.append({
            "accession_number": acc,
            "form_type":        form_type,
            "filing_date":      dates[i] if i < len(dates) else None,
            "accepted_at":      accepted[i] if i < len(accepted) else None,
            "primary_doc_url":  url,
            "primary_doc_desc": primary_descs[i] if i < len(primary_descs) else None,
            "items":            items[i] if i < len(items) else None,
        })

    return out


def run_extraction_for_company(ticker):
    """Run extraction for one tracked company."""
    if ticker not in TRACKED_COMPANIES:
        return {"status": "error", "error": f"unknown ticker: {ticker}"}

    cik, name, category = TRACKED_COMPANIES[ticker]
    started = time.time()
    summary = {"ticker": ticker, "company": name, "cik": cik, "category": category}

    try:
        submissions = _fetch_company_submissions(cik)
        filings = _parse_recent_filings(submissions, cik)
        rows = _persist_filings(filings, cik, ticker, name, category)
        elapsed_ms = int((time.time() - started) * 1000)
        summary.update({
            "filings_seen":    len(filings),
            "rows_inserted":   rows,
            "duration_ms":     elapsed_ms,
            "status":          "ok",
        })
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        summary.update({
            "status":      "error",
            "error":       f"{type(e).__name__}: {e}",
            "duration_ms": elapsed_ms,
        })
    return summary


def run_extraction_all():
    """Run extraction across all tracked companies. Heartbeats once at end."""
    _ensure_table()
    started = time.time()
    results = []
    total_inserted = 0
    failed = []

    for ticker in TRACKED_COMPANIES:
        r = run_extraction_for_company(ticker)
        results.append(r)
        total_inserted += r.get("rows_inserted", 0)
        if r.get("status") != "ok":
            failed.append(ticker)
        # Polite rate limit — SEC says max 10 req/sec. We do ~3/sec.
        time.sleep(0.35)

    elapsed_ms = int((time.time() - started) * 1000)
    succeeded = len(TRACKED_COMPANIES) - len(failed)

    _heartbeat(
        SOURCE_ID,
        status=("success" if succeeded > 0 else "failure"),
        rows_affected=total_inserted,
        duration_ms=elapsed_ms,
        metadata={"companies_total": len(TRACKED_COMPANIES),
                  "succeeded": succeeded, "failed": failed[:5]},
        error=("; ".join(r.get("error", "") for r in results if r.get("status") != "ok")[:300] if not succeeded else None),
    )

    return {
        "iso": "SEC-EDGAR",  # for orchestrator compat — not actually an ISO
        "duration_ms": elapsed_ms,
        "companies_total": len(TRACKED_COMPANIES),
        "companies_succeeded": succeeded,
        "companies_failed": failed,
        "rows_inserted": total_inserted,
        "results": results,
        "status": "ok" if succeeded > 0 else "error",
    }


# Alias for orchestrator compatibility
def run_extraction():
    return run_extraction_all()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# AUTO-REPAIR: duplicate route '/extract' also in routes/iso_caiso.py:145 — review and remove one
@sec_edgar_bp.route("/extract", methods=["POST", "GET"])
def trigger_extract_all():
    s = run_extraction_all()
    return jsonify(s), (200 if s.get("status") == "ok" else 500)


@sec_edgar_bp.route("/extract/<string:ticker>", methods=["POST", "GET"])
def trigger_extract_one(ticker):
    _ensure_table()
    s = run_extraction_for_company(ticker.upper())
    return jsonify(s), (200 if s.get("status") == "ok" else 500)


@sec_edgar_bp.route("/filings", methods=["GET"])
def list_filings():
    _ensure_table()
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
    except ValueError:
        return jsonify(error="limit must be int"), 400

    args = {"limit": limit}
    where_parts = []

    form_filter = request.args.get("type")
    if form_filter:
        where_parts.append("form_type = %(form)s")
        args["form"] = form_filter

    cik_filter = request.args.get("cik")
    if cik_filter:
        where_parts.append("cik = %(cik)s")
        args["cik"] = cik_filter

    ticker_filter = request.args.get("ticker")
    if ticker_filter:
        where_parts.append("ticker = %(ticker)s")
        args["ticker"] = ticker_filter.upper()

    since_filter = request.args.get("since")
    if since_filter:
        try:
            args["since"] = date.fromisoformat(since_filter)
            where_parts.append("filing_date >= %(since)s")
        except ValueError:
            return jsonify(error="since must be YYYY-MM-DD"), 400

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = f"""
        SELECT id, accession_number, cik, ticker, company_name, category,
               form_type, filing_date, accepted_at,
               primary_doc_url, primary_doc_desc, items, created_at
        FROM sec_filings_v2
        {where}
        ORDER BY filing_date DESC, accepted_at DESC NULLS LAST, id DESC
        LIMIT %(limit)s
    """

    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for r in rows:
        for k in ("accepted_at", "created_at"):
            if isinstance(r.get(k), datetime):
                r[k] = r[k].isoformat()
        if isinstance(r.get("filing_date"), date):
            r["filing_date"] = r["filing_date"].isoformat()

    return jsonify(count=len(rows), filings=rows), 200


@sec_edgar_bp.route("/filings/<string:cik>", methods=["GET"])
def filings_by_cik(cik):
    return list_filings()


@sec_edgar_bp.route("/companies", methods=["GET"])
def list_companies():
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT ticker, cik, company_name, category,
                      COUNT(*) AS filings_count,
                      MAX(filing_date) AS most_recent_filing
               FROM sec_filings_v2
               GROUP BY ticker, cik, company_name, category
               ORDER BY filings_count DESC"""
        )
        rows = cur.fetchall()

    db_data = {}
    for ticker, cik, name, cat, count, mrf in rows:
        db_data[ticker] = {
            "ticker": ticker, "cik": cik, "company_name": name,
            "category": cat, "filings_count": int(count or 0),
            "most_recent_filing": mrf.isoformat() if mrf else None,
        }

    # Merge with tracked list to show 0-count companies too
    tracked = []
    for ticker, (cik, name, cat) in TRACKED_COMPANIES.items():
        info = db_data.get(ticker, {
            "ticker": ticker, "cik": cik, "company_name": name,
            "category": cat, "filings_count": 0, "most_recent_filing": None,
        })
        tracked.append(info)

    return jsonify(count=len(tracked), companies=tracked), 200

# AUTO-REPAIR: duplicate route '/health' also in main.py:3845 — review and remove one

@sec_edgar_bp.route("/health", methods=["GET"])
def health():
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT MAX(filing_date), MAX(created_at), COUNT(*),
                      COUNT(DISTINCT cik), COUNT(DISTINCT form_type)
               FROM sec_filings_v2"""
        )
        latest_filing, latest_extract, total, distinct_ciks, distinct_forms = cur.fetchone()
    return jsonify(
        status="ok",
        source_id=SOURCE_ID,
        latest_filing_date=latest_filing.isoformat() if latest_filing else None,
        latest_extraction_at=latest_extract.isoformat() if latest_extract else None,
        total_filings=int(total or 0),
        distinct_companies=int(distinct_ciks or 0),
        distinct_form_types=int(distinct_forms or 0),
        tracked_companies=len(TRACKED_COMPANIES),
    ), 200
