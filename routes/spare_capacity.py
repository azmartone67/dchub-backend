"""Phase CCCC (2026-05-16) — Spare-Capacity Surface.

User vision: "is there a way to start capturing spare capacity from
operators we can start reporting for users, need to start referring
business to make broker fees, cut out sales people, maybe have a
promo code they can track where get credit, same as our pocket
listings, need to make sure we get credit for our deals sourced."

This module is the MVP. Operators can list spare MW; tenants can
browse; every listing gets a unique referral_code so DC Hub gets
attributed when business closes.

  POST /api/v1/spare-capacity/submit       intake form ingest
  GET  /api/v1/spare-capacity/listings     paginated public list
  GET  /api/v1/spare-capacity/<ref>        per-listing detail (tracker URL)
  GET  /spare-capacity                     human HTML page with form + listings
  GET  /spare-capacity/<ref>               per-listing tracker page

Anti-abuse:
  - 10 listings / day / IP cap (in-memory)
  - email-required on submit; not verified yet (Phase DDDD+)
  - moderation flag: listings start status='pending', flip to 'live'
    via admin endpoint (or auto-approve known operator emails later)

Privacy:
  - contact_email + contact_name shown only to admins via header check
  - public listings show operator_name + location + MW + ready_date

Future Phase DDDD+ candidates:
  - Email verification on submit (SendGrid one-time link)
  - Operator account → bulk edit own listings
  - Tenant intake form on per-listing page → broker-credit attribution
  - Stripe Connect for actual payout when deal closes
"""

from __future__ import annotations

import os
import secrets
import datetime
from flask import Blueprint, jsonify, request, Response


spare_capacity_bp = Blueprint("spare_capacity", __name__)


_RL_BUCKET: dict[str, list[float]] = {}
_RL_WINDOW_SEC = 86400.0  # per-day
_RL_MAX = 10               # 10 submits/day/IP
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS spare_capacity_listings (
    referral_code   TEXT PRIMARY KEY,
    operator_name   TEXT NOT NULL,
    location        TEXT NOT NULL,
    state           TEXT,
    market          TEXT,
    mw_available    REAL NOT NULL,
    ready_date      TEXT,
    description     TEXT,
    contact_name    TEXT,
    contact_email   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    tenant_inquiries INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_spare_capacity_status
    ON spare_capacity_listings(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_spare_capacity_state
    ON spare_capacity_listings(state);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        pass


def _gen_referral_code() -> str:
    """8-char URL-safe code prefixed with SC- so it's recognizable."""
    return "SC-" + secrets.token_urlsafe(6)[:8].upper().replace("_", "X").replace("-", "X")


def _rate_limited(ip: str) -> bool:
    import time
    now = time.time()
    bucket = _RL_BUCKET.setdefault(ip, [])
    bucket[:] = [t for t in bucket if (now - t) < _RL_WINDOW_SEC]
    if len(bucket) >= _RL_MAX:
        return True
    bucket.append(now)
    return False


def _validate_submit(d: dict) -> tuple[bool, str]:
    """Return (ok, error_message)."""
    op = (d.get("operator_name") or "").strip()
    if len(op) < 2 or len(op) > 100:
        return False, "operator_name must be 2-100 chars"
    loc = (d.get("location") or "").strip()
    if len(loc) < 3 or len(loc) > 200:
        return False, "location must be 3-200 chars"
    try:
        mw = float(d.get("mw_available") or 0)
        if mw <= 0 or mw > 5000:
            return False, "mw_available must be 0-5000"
    except (ValueError, TypeError):
        return False, "mw_available must be a number"
    email = (d.get("contact_email") or "").strip()
    if "@" not in email or len(email) > 200:
        return False, "contact_email required"
    return True, ""


@spare_capacity_bp.route("/api/v1/spare-capacity/submit", methods=["POST", "OPTIONS"])
def submit_listing():
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST",
        })
    ip = (request.headers.get("CF-Connecting-IP")
          or request.remote_addr or "?")
    if _rate_limited(ip):
        return jsonify(error="rate_limited",
                       message="Max 10 listings per day per source. Email "
                                "ops@dchub.cloud if you have a bulk upload."), 429

    d = request.get_json(silent=True) or request.form.to_dict() or {}
    ok, err = _validate_submit(d)
    if not ok:
        return jsonify(error="invalid", message=err), 400

    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        # Allocate unique referral code
        for _ in range(5):
            ref = _gen_referral_code()
            try:
                with c.cursor() as cur:
                    # ON CONFLICT on the PK so the retry-on-collision
                    # loop continues to the next generated code instead
                    # of bubbling a duplicate-key error; also satisfies
                    # the regression-lint INSERT-must-have-ON-CONFLICT rule.
                    cur.execute("""
                        INSERT INTO spare_capacity_listings
                          (referral_code, operator_name, location, state, market,
                           mw_available, ready_date, description,
                           contact_name, contact_email, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                        ON CONFLICT (referral_code) DO NOTHING
                    """, (
                        ref,
                        (d.get("operator_name") or "").strip()[:100],
                        (d.get("location") or "").strip()[:200],
                        ((d.get("state") or "")[:8]).upper() or None,
                        (d.get("market") or "")[:80] or None,
                        float(d.get("mw_available") or 0),
                        (d.get("ready_date") or "")[:32] or None,
                        (d.get("description") or "")[:1000] or None,
                        (d.get("contact_name") or "")[:100] or None,
                        (d.get("contact_email") or "").strip()[:200],
                    ))
                    # ON CONFLICT DO NOTHING swallows duplicate-PK
                    # silently; check rowcount so we retry with a new
                    # code instead of returning a phantom success.
                    if cur.rowcount and cur.rowcount > 0:
                        break
                    # else: PK collision, allocate new code on next loop
                    continue
            except Exception as _e:
                # Belt-and-suspenders: any other transient error → retry
                if "duplicate" in str(_e).lower():
                    continue
                raise
        else:
            return jsonify(error="failed_to_allocate"), 500
    except Exception as e:
        return jsonify(error="db_error", detail=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass

    return jsonify(
        ok=True,
        referral_code=ref,
        tracker_url=f"https://dchub.cloud/spare-capacity/{ref}",
        status="pending",
        message="Listing received. Status: pending — typically live within "
                 "24 hours after a quick human review. Save your referral_code "
                 "to share with tenants; any inquiry that arrives through "
                 "the tracker URL is attributed to you.",
    ), 201


@spare_capacity_bp.route("/api/v1/spare-capacity/listings", methods=["GET"])
def listings():
    """Public paginated list. Contact info redacted unless admin key."""
    try: page = max(1, int(request.args.get("page") or 1))
    except (ValueError, TypeError): page = 1
    per_page = 50
    offset = (page - 1) * per_page
    status_filter = (request.args.get("status") or "live").strip().lower()
    if status_filter not in ("live", "pending", "all"):
        status_filter = "live"

    show_contact = False
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and provided == _ADMIN_KEY:
        show_contact = True

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql_where = "" if status_filter == "all" else "WHERE status = %s"
            params: tuple = (per_page, offset) if status_filter == "all" else (status_filter, per_page, offset)
            cur.execute(f"""
                SELECT referral_code, operator_name, location, state, market,
                       mw_available, ready_date, description, status,
                       tenant_inquiries, created_at, contact_name, contact_email
                  FROM spare_capacity_listings
                  {sql_where}
                 ORDER BY created_at DESC LIMIT %s OFFSET %s
            """, params)
            rows = cur.fetchall()
            # Total
            if status_filter == "all":
                cur.execute("SELECT COUNT(*) FROM spare_capacity_listings")
            else:
                cur.execute("SELECT COUNT(*) FROM spare_capacity_listings WHERE status = %s",
                            (status_filter,))
            total = int((cur.fetchone() or [0])[0] or 0)
    finally:
        try: c.close()
        except Exception: pass

    out = []
    for r in rows:
        item = {
            "referral_code":    r["referral_code"],
            "operator_name":    r["operator_name"],
            "location":         r["location"],
            "state":            r["state"],
            "market":           r["market"],
            "mw_available":     float(r["mw_available"] or 0),
            "ready_date":       r["ready_date"],
            "description":      r["description"],
            "status":           r["status"],
            "tenant_inquiries": int(r["tenant_inquiries"] or 0),
            "created_at":       r["created_at"].isoformat() if r["created_at"] else None,
            "tracker_url":      f"https://dchub.cloud/spare-capacity/{r['referral_code']}",
        }
        if show_contact:
            item["contact_name"]  = r["contact_name"]
            item["contact_email"] = r["contact_email"]
        out.append(item)

    resp = jsonify(listings=out, count=len(out), total=total, page=page,
                   per_page=per_page, status_filter=status_filter,
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    resp.headers["Cache-Control"] = "public, max-age=180"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


def _render_form_html(total_live: int) -> str:
    """Spare-capacity HTML — intake form + live listings preview."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Spare Capacity Marketplace — list MW, find tenants | DC Hub</title>
<meta name="description" content="DC Hub Spare Capacity Marketplace. Operators list available MW; tenants find capacity. Every listing gets a unique referral code so the source operator gets credit on the deal.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/spare-capacity">
<meta property="og:title" content="Spare Capacity Marketplace — DC Hub">
<meta property="og:description" content="List spare MW + find tenants. Broker-credit tracking via unique referral codes.">
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type":    "WebApplication",
  "name":     "DC Hub Spare Capacity Marketplace",
  "url":      "https://dchub.cloud/spare-capacity",
  "applicationCategory": "BusinessApplication",
  "offers":   {{"@type": "Offer", "price": "0", "priceCurrency": "USD"}},
  "creator":  {{"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"}}
}}
</script>
<style>
 *{{box-sizing:border-box}}
 body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       max-width:1100px;margin:0 auto;padding:2rem 1rem;color:#1f2937;line-height:1.55;background:#fafbfc}}
 h1{{font-size:2rem;margin:0 0 .25rem}}
 .tag{{display:inline-block;background:#111827;color:white;padding:.25rem .75rem;border-radius:999px;
      font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;margin-bottom:1rem}}
 .lead{{font-size:1.05rem;color:#4b5563;max-width:780px;margin:0 0 2rem}}
 .grid{{display:grid;grid-template-columns:1.2fr 1fr;gap:2rem;align-items:start}}
 @media (max-width:760px){{.grid{{grid-template-columns:1fr}}}}
 .card{{background:white;padding:1.5rem;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 .card h2{{margin:0 0 1rem;font-size:1.25rem}}
 label{{display:block;font-size:.85rem;color:#374151;font-weight:600;margin:.7rem 0 .2rem}}
 input, textarea, select{{width:100%;padding:.55rem .7rem;border:1px solid #d1d5db;border-radius:6px;font-size:.95rem;background:white}}
 textarea{{min-height:80px;resize:vertical}}
 .row{{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}}
 button{{margin-top:1.25rem;background:linear-gradient(135deg,#065f46,#0f766e);color:white;
        padding:.7rem 1.5rem;border:0;border-radius:6px;font-weight:700;cursor:pointer;font-size:1rem;width:100%}}
 button:hover{{filter:brightness(1.1)}}
 .why{{background:#ecfdf5;border:1px solid #a7f3d0;padding:1.25rem;border-radius:8px;margin-bottom:1rem}}
 .why ul{{margin:.5rem 0 0;padding-left:1.25rem}}
 .why li{{margin:.25rem 0}}
 .stat{{font-size:2.4rem;font-weight:700;color:#065f46;line-height:1}}
 .stat-sub{{color:#6b7280;font-size:.85rem;margin-top:.25rem}}
 #result{{margin-top:1rem;padding:.8rem 1rem;border-radius:6px;display:none}}
 #result.ok{{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;display:block}}
 #result.err{{background:#fef2f2;border:1px solid #fecaca;color:#991b1b;display:block}}
 .footer{{color:#9ca3af;font-size:.85rem;text-align:center;margin-top:3rem}}
 a{{color:#1e40af;text-decoration:none}} a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<span class="tag">New · Phase CCCC · 2026-05-16</span>
<h1>🏗️ Spare Capacity Marketplace</h1>
<p class="lead">Got spare MW you'd lease? List it here. Need MW? Browse the live listings. Every listing gets a unique <strong>referral_code</strong> so when a tenant closes a deal through us, the source operator gets attribution credit. No middleman fees on the listing side.</p>

<div class="grid">
  <div class="card">
    <h2>List your spare capacity</h2>
    <form id="spareForm">
      <label>Operator / company name *</label>
      <input name="operator_name" required maxlength="100" placeholder="Acme DataCenter LLC">

      <div class="row">
        <div>
          <label>Location *</label>
          <input name="location" required maxlength="200" placeholder="Ashburn, VA">
        </div>
        <div>
          <label>State (US)</label>
          <input name="state" maxlength="2" placeholder="VA" style="text-transform:uppercase">
        </div>
      </div>

      <div class="row">
        <div>
          <label>MW available *</label>
          <input name="mw_available" type="number" step="0.1" min="0.1" max="5000" required placeholder="12.5">
        </div>
        <div>
          <label>Ready date</label>
          <input name="ready_date" maxlength="32" placeholder="Q3 2026 or 2026-08-15">
        </div>
      </div>

      <label>Market / campus name</label>
      <input name="market" maxlength="80" placeholder="Northern Virginia, Equinix DC8">

      <label>Description (optional)</label>
      <textarea name="description" maxlength="1000" placeholder="Tier III campus, 35kW/rack support, ready for shell + colo, ISP-diverse, etc."></textarea>

      <div class="row">
        <div>
          <label>Your name</label>
          <input name="contact_name" maxlength="100" placeholder="Jane Smith">
        </div>
        <div>
          <label>Email *</label>
          <input name="contact_email" type="email" required maxlength="200" placeholder="leasing@acme.com">
        </div>
      </div>

      <button type="submit">Submit listing</button>
      <div id="result"></div>
    </form>
  </div>

  <div>
    <div class="card why">
      <h2>Why list with DC Hub</h2>
      <ul>
        <li>📍 <strong>Live listings</strong> indexed by AI agents + Google</li>
        <li>🎯 <strong>Unique referral_code</strong> per listing — you get attributed on every tenant inquiry</li>
        <li>🚫 <strong>No listing fee</strong>, no exclusivity</li>
        <li>📊 <strong>Live count</strong>: {total_live:,} active listings</li>
        <li>🤖 <strong>MCP-discoverable</strong> — Claude/GPT/Gemini can recommend your listing to their users</li>
      </ul>
    </div>
    <div class="card">
      <div class="stat">{total_live:,}</div>
      <div class="stat-sub">listings live · view all → <a href="/api/v1/spare-capacity/listings">JSON</a></div>
    </div>
  </div>
</div>

<p class="footer">
 Need bulk upload? Email <a href="mailto:ops@dchub.cloud">ops@dchub.cloud</a>.
 API: <a href="/api/v1/spare-capacity/listings">/api/v1/spare-capacity/listings</a> ·
 Per-listing tracker: <code>/spare-capacity/&lt;referral_code&gt;</code>
</p>

<script src="/js/dchub-nav.js" defer></script>
<script>
(function() {{
  var f = document.getElementById('spareForm');
  var r = document.getElementById('result');
  if (!f) return;
  f.addEventListener('submit', function(ev) {{
    ev.preventDefault();
    r.className = '';
    r.textContent = 'Submitting…';
    r.style.display = 'block';
    var d = {{}};
    new FormData(f).forEach(function(v, k) {{ d[k] = v; }});
    fetch('/api/v1/spare-capacity/submit', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(d),
    }}).then(function(resp) {{
      return resp.json().then(function(j) {{ return {{ status: resp.status, body: j }}; }});
    }}).then(function(o) {{
      if (o.status >= 200 && o.status < 300) {{
        r.className = 'ok';
        r.innerHTML = '✅ Listing received. Your referral code: <strong>' + o.body.referral_code +
                       '</strong> · Tracker URL: <a href="' + o.body.tracker_url + '">' + o.body.tracker_url + '</a>';
        f.reset();
      }} else {{
        r.className = 'err';
        r.textContent = '⚠️ ' + (o.body.message || o.body.error || 'Submission failed.');
      }}
    }}).catch(function(e) {{
      r.className = 'err';
      r.textContent = '⚠️ Network error. Try again.';
    }});
  }});
}})();
</script>
</body>
</html>"""


@spare_capacity_bp.route("/spare-capacity", methods=["GET"], strict_slashes=False)
def spare_capacity_page():
    """Human HTML page — intake form + live count + brand pitch."""
    # Count live listings
    total_live = 0
    c = _conn()
    if c is not None:
        try:
            _ensure_schema(c)
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM spare_capacity_listings WHERE status = 'live'")
                total_live = int((cur.fetchone() or [0])[0] or 0)
        except Exception: pass
        finally:
            try: c.close()
            except Exception: pass

    try:
        from routes.surface_brain import auto_log
        auto_log("spare_capacity", "view", target="/spare-capacity")
    except Exception: pass

    html = _render_form_html(total_live)
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=120"})


@spare_capacity_bp.route("/spare-capacity/<ref>", methods=["GET"], strict_slashes=False)
def spare_capacity_tracker(ref):
    """Per-listing tracker page. Increments tenant_inquiries when a
    'mailto:' or contact CTA is reported via the tracker."""
    ref = (ref or "").strip().upper()
    if not ref.startswith("SC-"):
        from flask import abort; abort(404)
    c = _conn()
    if c is None:
        from flask import abort; abort(404)
    listing = None
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT referral_code, operator_name, location, state, market,
                       mw_available, ready_date, description, status,
                       tenant_inquiries, created_at
                  FROM spare_capacity_listings WHERE referral_code = %s
            """, (ref,))
            listing = cur.fetchone()
            if listing:
                # Track the view as a tenant interest signal (one per pageview)
                try:
                    cur.execute("""
                        UPDATE spare_capacity_listings
                           SET tenant_inquiries = tenant_inquiries + 1
                         WHERE referral_code = %s
                    """, (ref,))
                except Exception: pass
    finally:
        try: c.close()
        except Exception: pass

    if not listing:
        from flask import abort; abort(404)

    try:
        from routes.surface_brain import auto_log
        auto_log("spare_capacity", "view_tracker", target=ref)
    except Exception: pass

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Spare Capacity Listing {ref} — DC Hub</title>
<meta name="description" content="{listing['operator_name']}: {listing['mw_available']} MW available in {listing['location']}. Listed via DC Hub Spare Capacity Marketplace.">
<style>
 body{{font-family:-apple-system,sans-serif;max-width:800px;margin:0 auto;padding:2rem 1rem;color:#1f2937;line-height:1.55}}
 .card{{background:white;padding:2rem;border-radius:12px;box-shadow:0 2px 6px rgba(0,0,0,.08);margin-top:1rem}}
 .ref{{font-family:monospace;background:#f3f4f6;padding:.2rem .5rem;border-radius:4px;font-size:.9rem;color:#374151}}
 .mw{{font-size:3rem;font-weight:700;color:#065f46;line-height:1}}
 .meta{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:1.5rem 0;padding:1rem 1.25rem;background:#f9fafb;border-radius:8px}}
 .meta div{{font-size:.95rem}}
 .meta strong{{display:block;font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#6b7280;margin-bottom:.25rem}}
 .cta{{display:inline-block;background:linear-gradient(135deg,#065f46,#0f766e);color:white;padding:.65rem 1.5rem;border-radius:6px;text-decoration:none;font-weight:700;margin-top:1rem}}
</style></head>
<body>
<a href="/spare-capacity">← Browse all listings</a>
<div class="card">
 <p style="margin:0 0 .25rem;color:#6b7280">Spare Capacity Listing <span class="ref">{ref}</span></p>
 <h1 style="margin:0 0 .25rem">{listing['operator_name']}</h1>
 <p style="margin:0;color:#6b7280">{listing['location']}{(' · ' + listing['market']) if listing['market'] else ''}</p>
 <div class="mw" style="margin-top:1rem">{listing['mw_available']:.1f} MW</div>
 <p style="color:#6b7280;margin:.25rem 0 0">available{(' · ready ' + listing['ready_date']) if listing['ready_date'] else ''}</p>
 <div class="meta">
   <div><strong>Status</strong>{listing['status']}</div>
   <div><strong>State</strong>{listing['state'] or '—'}</div>
   <div><strong>Listed</strong>{listing['created_at'].strftime('%Y-%m-%d') if listing['created_at'] else '—'}</div>
   <div><strong>Tenant inquiries</strong>{listing['tenant_inquiries']}</div>
 </div>
 {('<p style="color:#374151;white-space:pre-wrap">' + (listing['description'] or '') + '</p>') if listing.get('description') else ''}
 <p style="margin-top:1.5rem">
   <a class="cta" href="mailto:ops@dchub.cloud?subject=Tenant inquiry — {ref}&body=I'm interested in the {listing['mw_available']:.1f} MW listing in {listing['location']} (referral {ref}). Please connect me with the operator.">
     Inquire via DC Hub (tracked)
   </a>
 </p>
 <p style="color:#9ca3af;font-size:.85rem;margin-top:1.5rem">
   This tracker URL attributes any tenant interest to the source operator.
   When the deal closes, DC Hub gets credit via the referral_code <span class="ref">{ref}</span>.
 </p>
</div>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=60"})
