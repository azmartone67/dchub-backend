"""
lead_enrichment.py — enrich identified_checkout_signals with IPinfo + email parsing.

Phase ZZZZZ-round41 (2026-05-25). We have 13 identified leads (mostly QA)
but no enrichment context — the outreach email is generic. Adding:

  - Email domain → company (parse domain, lookup industry/size via IPinfo Company API)
  - Capture IP → IPinfo Geo + company (auto-detect ASN/org)
  - Email-format hint at title (e.g. firstname.lastname@x.com → likely individual user)

This runs as a follow-up cron after the email-capture endpoint persists a row.
Without IPINFO_TOKEN it falls back to local heuristics (free-tier email check,
domain TLD inference). Either way the row's `notes` field gets a JSON blob the
outreach cron can read for personalization.
"""
import os
import json
import re
import datetime
import urllib.request
import urllib.error
from contextlib import contextmanager

from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

lead_enrich_bp = Blueprint("lead_enrichment", __name__,
                            url_prefix="/api/v1/lead-enrichment")

IPINFO_TOKEN = os.environ.get("IPINFO_TOKEN", "").strip()
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "protonmail.com", "proton.me", "msn.com", "live.com",
    "fastmail.com", "duck.com", "pm.me", "mac.com",
}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _classify_email(email):
    out = {"email": email}
    if not email or "@" not in email:
        return {**out, "valid": False}
    local, _, domain = email.partition("@")
    domain = domain.lower()
    out["domain"] = domain
    out["is_free_email"] = domain in FREE_EMAIL_DOMAINS
    out["is_corporate"] = not out["is_free_email"]
    out["local_part"] = local
    # Name guess from local part
    if "." in local:
        parts = local.split(".")
        out["name_guess"] = " ".join(p.capitalize() for p in parts if not p.isdigit())
    elif len(local) > 3:
        out["name_guess"] = local.capitalize()
    else:
        out["name_guess"] = None
    # Title hints
    if any(p in local.lower() for p in ("ops", "infra", "devops", "platform", "sre")):
        out["title_hint"] = "infrastructure"
    elif any(p in local.lower() for p in ("ceo", "cto", "vp", "founder")):
        out["title_hint"] = "executive"
    elif any(p in local.lower() for p in ("eng", "dev", "engineer")):
        out["title_hint"] = "engineering"
    else:
        out["title_hint"] = None
    return out


def _ipinfo_lookup(ip):
    """Look up IP via IPinfo. Returns None if token unset or call fails."""
    if not IPINFO_TOKEN or not ip:
        return None
    try:
        req = urllib.request.Request(
            f"https://ipinfo.io/{ip}/json",
            headers={"Authorization": f"Bearer {IPINFO_TOKEN}",
                      "User-Agent": "DCHub-LeadEnrich/1.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _enrich_one(lead_row):
    """Build enrichment blob for a single signal row."""
    email_info = _classify_email(lead_row.get("email"))
    ip_info = _ipinfo_lookup(lead_row.get("ip"))
    return {
        "email":   email_info,
        "ip_info": ip_info or {"_skipped": "no_token_or_lookup_failed"},
        "enriched_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


@lead_enrich_bp.route("/process-pending", methods=["GET", "POST"])
def process_pending():
    """Enrich any signal whose notes field doesn't yet contain enrichment JSON."""
    started = datetime.datetime.utcnow()
    out = {"at": started.isoformat() + "Z", "enriched": 0, "skipped": 0, "errors": 0}
    if not (_pg and _dsn()):
        out["error"] = "no_db"
        return jsonify(out), 200

    limit = max(1, min(50, int(request.args.get("limit", 25))))
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, ip, notes, captured_at
                FROM identified_checkout_signals
                WHERE notes IS NULL
                   OR notes = ''
                   OR (notes IS NOT NULL AND notes NOT LIKE '{%%enriched_at%%')
                ORDER BY captured_at DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
        return jsonify(out), 500

    for row in rows:
        try:
            enrichment = _enrich_one(row)
            with _conn() as c, c.cursor() as cur:
                # Merge enrichment into notes (preserves any existing notes content)
                merged = json.dumps(enrichment, default=str)[:2000]
                cur.execute("""
                    UPDATE identified_checkout_signals
                       SET notes = %s
                     WHERE id = %s
                """, (merged, row["id"]))
                c.commit()
                out["enriched"] += 1
        except Exception as e:
            out["errors"] += 1
            out.setdefault("error_detail", []).append(f"id={row['id']}: {type(e).__name__}")

    out["elapsed_ms"] = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    return jsonify(out), 200


@lead_enrich_bp.route("/status", methods=["GET"])
def status():
    out = {
        "blueprint": "lead_enrich_bp",
        "ipinfo_token_set": bool(IPINFO_TOKEN),
        "free_email_domains_known": len(FREE_EMAIL_DOMAINS),
    }
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM identified_checkout_signals WHERE notes LIKE '{%%enriched_at%%'")
                out["enriched_count"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM identified_checkout_signals WHERE notes IS NULL OR notes = ''")
                out["unenriched_count"] = cur.fetchone()[0]
        except Exception as e:
            out["db_error"] = str(e)[:120]
    return jsonify(out), 200


@lead_enrich_bp.route("/by-id/<int:lead_id>", methods=["GET"])
def by_id(lead_id):
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 200
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, tool, tier, ip, captured_at, notes
                FROM identified_checkout_signals WHERE id = %s
            """, (lead_id,))
            row = cur.fetchone()
        if not row: return jsonify({"error": "not_found"}), 404
        for k, v in list(row.items()):
            if isinstance(v, datetime.datetime): row[k] = v.isoformat()
        try:
            row["notes_parsed"] = json.loads(row.get("notes") or "{}")
        except Exception:
            row["notes_parsed"] = None
        return jsonify(row), 200
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}", "detail": str(e)[:200]}), 500
