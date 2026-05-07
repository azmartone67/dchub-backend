"""Phase 75 -- NEPA filings scraper.

Hits regulations.gov v4 API to find federal environmental filings related
to data center / AI infrastructure / hyperscale projects. Stores results
in the nepa_filings table for downstream querying.

API key: get a free key from https://api.data.gov/signup/ and set the
NEPA_API_KEY env var. The DEMO_KEY works for low-volume testing
(rate-limited to 30 requests/hour).

Usage:
  from services.nepa_scraper import scrape_recent_filings
  count = scrape_recent_filings(max_pages=3)
  print(f"Stored {count} new filings")
"""
import os
import json
import urllib.request
import urllib.parse
import urllib.error
import datetime
from typing import List, Dict, Optional

API_BASE = "https://api.regulations.gov/v4"
KEYWORDS = [
    "data center",
    "hyperscale",
    "AI infrastructure",
    "computing facility",
    "server farm",
]


def _api_key() -> str:
    return os.environ.get("NEPA_API_KEY") or "DEMO_KEY"


def _request(path: str, params: Dict) -> Optional[dict]:
    """Hit regulations.gov v4 API; return parsed JSON or None on error."""
    params = dict(params)
    params["api_key"] = _api_key()
    qs = urllib.parse.urlencode(params)
    url = API_BASE + path + "?" + qs
    req = urllib.request.Request(url, headers={"User-Agent": "DCHub-NEPA-Scraper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[nepa_scraper] HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"[nepa_scraper] request failed: {type(e).__name__}: {e}")
        return None


def search_documents(keyword: str, page_number: int = 1, page_size: int = 25) -> Optional[dict]:
    """Search regulations.gov documents for a keyword."""
    return _request(
        "/documents",
        {
            "filter[searchTerm]": keyword,
            "page[size]": page_size,
            "page[number]": page_number,
            "sort": "-postedDate",
        }
    )


def parse_document(doc: dict) -> Dict:
    """Parse one regulations.gov document into a flat dict for our table."""
    attrs = doc.get("attributes", {})
    return {
        "document_id":    doc.get("id", ""),
        "docket_id":      attrs.get("docketId") or "",
        "agency":         (attrs.get("agencyId") or "").upper(),
        "title":          (attrs.get("title") or "")[:500],
        "summary":        (attrs.get("summary") or attrs.get("abstract") or "")[:2000],
        "posted_date":    attrs.get("postedDate"),
        "received_date":  attrs.get("receivedDate"),
        "document_type":  attrs.get("documentType"),
        "url":            f"https://www.regulations.gov/document/{doc.get('id', '')}",
    }


def ensure_table(conn) -> None:
    """Create nepa_filings table if it doesn't already exist."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nepa_filings (
            id              SERIAL PRIMARY KEY,
            document_id     TEXT UNIQUE,
            docket_id       TEXT,
            agency          TEXT,
            title           TEXT,
            summary         TEXT,
            posted_date     TIMESTAMP,
            received_date   TIMESTAMP,
            document_type   TEXT,
            url             TEXT,
            keyword_matched TEXT,
            source          TEXT DEFAULT 'regulations_gov_nepa',
            created_at      TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nepa_filings_posted ON nepa_filings(posted_date DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nepa_filings_agency ON nepa_filings(agency);")
    conn.commit()


def upsert_filing(conn, parsed: Dict, keyword: str) -> bool:
    """Insert a filing if not already present. Returns True if inserted."""
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO nepa_filings
                (document_id, docket_id, agency, title, summary,
                 posted_date, received_date, document_type, url, keyword_matched)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (document_id) DO NOTHING
            RETURNING id
        """, (
            parsed.get("document_id"),
            parsed.get("docket_id"),
            parsed.get("agency"),
            parsed.get("title"),
            parsed.get("summary"),
            parsed.get("posted_date"),
            parsed.get("received_date"),
            parsed.get("document_type"),
            parsed.get("url"),
            keyword,
        ))
        row = cur.fetchone()
        conn.commit()
        return bool(row)
    except Exception as e:
        conn.rollback()
        print(f"[nepa_scraper] upsert failed: {e}")
        return False


def scrape_recent_filings(max_pages: int = 3, page_size: int = 25) -> int:
    """Scrape recent NEPA filings for all keywords and store new ones.
    Returns the number of NEW filings inserted (not counting duplicates).
    """
    neon = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not neon:
        print("[nepa_scraper] no DB url configured")
        return 0

    conn = None
    for modname in ("psycopg", "psycopg2"):
        try:
            mod = __import__(modname)
            conn = mod.connect(neon)
            break
        except Exception:
            continue
    if not conn:
        print("[nepa_scraper] no postgres driver available")
        return 0

    inserted = 0
    try:
        ensure_table(conn)
        for kw in KEYWORDS:
            for page in range(1, max_pages + 1):
                resp = search_documents(kw, page_number=page, page_size=page_size)
                if not resp or "data" not in resp:
                    break
                docs = resp.get("data", [])
                if not docs:
                    break
                for doc in docs:
                    parsed = parse_document(doc)
                    if not parsed.get("document_id"):
                        continue
                    if upsert_filing(conn, parsed, kw):
                        inserted += 1
                # Stop if there are no more pages
                meta = resp.get("meta", {})
                if not meta.get("hasNextPage"):
                    break
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return inserted


if __name__ == "__main__":
    n = scrape_recent_filings(max_pages=2)
    print(f"Inserted {n} new NEPA filings")
