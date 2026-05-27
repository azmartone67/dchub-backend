"""
partnership_admin_dashboard.py — one-page review UI for partnership drafts.

Phase ZZZZZ-round47.24 (2026-05-26). After r47.23 introduced the draft-
then-approve gate for partnership press + LinkedIn, the daily workflow
required 3+ curl commands. This page collapses it into a single URL:

  https://dchub.cloud/admin/partnerships/review
    ↓
  - Lists every pending press draft (title + subhead + body preview)
  - Lists every pending LinkedIn draft (headline + body)
  - Lists all 7 tracks at-a-glance status
  - Approve + Reject buttons that POST to existing endpoints
  - Admin key entered once, kept in localStorage

Auth model: anyone can VIEW the page (drafts are not secret),
but the approve/reject actions require the admin key.
"""
import os
import datetime
from contextlib import contextmanager
from flask import Blueprint, Response

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

partnership_admin_bp = Blueprint("partnership_admin_dashboard", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _gather_pending():
    out = {"press": [], "linkedin": [], "approved_press": [],
           "posted_linkedin": [], "enterprise_leads": [], "inquiries": []}
    if not (_pg and _dsn()):
        return out
    try:
        with _conn() as c:
            # Pending press drafts
            with c.cursor() as cur:
                cur.execute("""
                    SELECT id, slug, title, subheadline, summary, body, created_at
                      FROM press_releases
                     WHERE category = 'partnership' AND published = FALSE
                     ORDER BY created_at DESC LIMIT 20
                """)
                out["press"] = [{
                    "id": r[0], "slug": r[1], "title": r[2],
                    "subheadline": r[3] or "", "summary": r[4] or "",
                    "body": r[5] or "", "created_at": r[6].isoformat() if r[6] else None,
                } for r in cur.fetchall()]

            # r47.37: pending enterprise lead drafts (outbound outreach)
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT id, email, paid_hits_30d, top_tools, score,
                               subject, body, created_at
                          FROM enterprise_lead_drafts
                         WHERE status = 'pending'
                         ORDER BY score DESC NULLS LAST, created_at DESC LIMIT 20
                    """)
                    out["enterprise_leads"] = [{
                        "id": r[0], "email": r[1],
                        "paid_hits_30d": int(r[2] or 0),
                        "top_tools": r[3] or [],
                        "score": float(r[4] or 0),
                        "subject": r[5] or "", "body": r[6] or "",
                        "created_at": r[7].isoformat() if r[7] else None,
                    } for r in cur.fetchall()]
            except Exception:
                pass

            # r47.37: new inbound enterprise inquiries (last 14d, status=new)
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT id, name, email, firm, tier_requested,
                               use_case, notes, created_at
                          FROM enterprise_inquiries
                         WHERE status = 'new'
                            OR created_at > NOW() - INTERVAL '14 days'
                         ORDER BY created_at DESC LIMIT 20
                    """)
                    out["inquiries"] = [{
                        "id": r[0], "name": r[1], "email": r[2], "firm": r[3],
                        "tier_requested": r[4] or "", "use_case": r[5] or "",
                        "notes": r[6] or "",
                        "created_at": r[7].isoformat() if r[7] else None,
                    } for r in cur.fetchall()]
            except Exception:
                pass

            # Pending LinkedIn drafts
            with c.cursor() as cur:
                cur.execute("""
                    SELECT id, iso_year, iso_week, track_slug, headline, body, url, created_at, status
                      FROM linkedin_partnership_drafts
                     WHERE status IN ('pending', 'pending_review')
                     ORDER BY created_at DESC LIMIT 20
                """)
                out["linkedin"] = [{
                    "id": r[0], "iso_year": r[1], "iso_week": r[2],
                    "track": r[3], "headline": r[4], "body": r[5], "url": r[6],
                    "created_at": r[7].isoformat() if r[7] else None,
                    "status": r[8],
                } for r in cur.fetchall()]

            # Recently published press (read-only context)
            with c.cursor() as cur:
                cur.execute("""
                    SELECT slug, title, published_at
                      FROM press_releases
                     WHERE category = 'partnership' AND published = TRUE
                     ORDER BY published_at DESC LIMIT 10
                """)
                out["approved_press"] = [{
                    "slug": r[0], "title": r[1],
                    "published_at": r[2].isoformat() if r[2] else None,
                } for r in cur.fetchall()]

            # Recently posted LinkedIn (read-only context)
            with c.cursor() as cur:
                cur.execute("""
                    SELECT track_slug, headline, posted_at, linkedin_urn
                      FROM linkedin_partnership_drafts
                     WHERE status = 'posted'
                     ORDER BY posted_at DESC LIMIT 10
                """)
                out["posted_linkedin"] = [{
                    "track": r[0], "headline": r[1],
                    "posted_at": r[2].isoformat() if r[2] else None,
                    "linkedin_urn": r[3],
                } for r in cur.fetchall()]
    except Exception:
        pass
    return out


def _esc(s):
    """Minimal HTML escape — bodies are operator-written, not user input,
    but defensive anyway."""
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@partnership_admin_bp.route("/admin/partnerships/review",
                             methods=["GET"], strict_slashes=False)
def review():
    data = _gather_pending()
    today = datetime.datetime.utcnow().strftime("%B %d, %Y · %H:%M UTC")

    # Build press drafts cards
    press_cards = ""
    for p in data["press"]:
        press_cards += f"""
    <div class="card draft" data-kind="press" data-id="{_esc(p['slug'])}">
      <div class="card-head">
        <span class="badge press">PRESS DRAFT</span>
        <span class="card-meta">{_esc((p['created_at'] or '')[:16].replace('T',' '))}</span>
      </div>
      <h3 class="card-title">{_esc(p['title'])}</h3>
      <p class="card-sub">{_esc(p['subheadline'])}</p>
      <details>
        <summary>Show full body ({len(p['body'])} chars)</summary>
        <pre class="card-body">{_esc(p['body'])}</pre>
      </details>
      <div class="card-actions">
        <button class="btn btn-approve" data-action="approve"
                data-url="/api/v1/partnerships/press/approve/{_esc(p['slug'])}">
          ✓ Approve &amp; publish
        </button>
        <button class="btn btn-reject" data-action="reject"
                data-url="/api/v1/partnerships/press/reject/{_esc(p['slug'])}">
          ✗ Reject (delete draft)
        </button>
      </div>
    </div>"""

    # Build LinkedIn drafts cards
    linkedin_cards = ""
    for ld in data["linkedin"]:
        linkedin_cards += f"""
    <div class="card draft" data-kind="linkedin" data-id="{ld['id']}">
      <div class="card-head">
        <span class="badge linkedin">LINKEDIN DRAFT · {_esc(ld['track'])}</span>
        <span class="card-meta">Week {ld['iso_year']}-W{ld['iso_week']:02d} · {_esc((ld['created_at'] or '')[:16].replace('T',' '))}</span>
      </div>
      <h3 class="card-title">{_esc(ld['headline'])}</h3>
      <details>
        <summary>Show full body ({len(ld['body'])} chars)</summary>
        <pre class="card-body">{_esc(ld['body'])}</pre>
      </details>
      <div class="card-meta-row">
        <span>Target URL:</span>
        <a href="{_esc(ld['url'])}" target="_blank">{_esc(ld['url'])}</a>
      </div>
      <div class="card-actions">
        <button class="btn btn-approve" data-action="approve"
                data-url="/api/v1/linkedin-partnership/approve/{ld['id']}">
          ✓ Approve &amp; post to LinkedIn
        </button>
        <button class="btn btn-reject" data-action="reject"
                data-url="/api/v1/linkedin-partnership/reject/{ld['id']}">
          ✗ Reject (delete draft)
        </button>
      </div>
    </div>"""

    # Recently published / posted (read-only history)
    approved_press_rows = "".join(
        f'<tr><td><a href="https://dchub.cloud/press-release/{_esc(p["slug"])}" target="_blank">{_esc(p["title"][:80])}</a></td>'
        f'<td>{_esc((p["published_at"] or "")[:16].replace("T", " "))}</td></tr>'
        for p in data["approved_press"]
    ) or '<tr><td colspan="2" class="empty">—</td></tr>'

    posted_li_rows = "".join(
        f'<tr><td>{_esc(p["track"])}</td><td>{_esc(p["headline"][:80])}</td>'
        f'<td>{_esc((p["posted_at"] or "")[:16].replace("T", " "))}</td>'
        f'<td>{("<a target=_blank href=https://www.linkedin.com/feed/update/" + _esc(p["linkedin_urn"]) + "/>view</a>") if p["linkedin_urn"] else "—"}</td></tr>'
        for p in data["posted_linkedin"]
    ) or '<tr><td colspan="4" class="empty">—</td></tr>'

    # r47.37: enterprise lead drafts (outbound outreach) — same approve/reject pattern
    enterprise_cards = ""
    for ld in data.get("enterprise_leads", []):
        tools_chip = ", ".join(ld.get("top_tools", [])[:3]) or "—"
        enterprise_cards += f"""
    <div class="card draft" data-kind="enterprise" data-id="{ld['id']}">
      <div class="card-head">
        <span class="badge enterprise">ENTERPRISE OUTREACH</span>
        <span class="card-meta">{ld['paid_hits_30d']:,} paid hits/30d · score {ld['score']:.0f} · {_esc((ld['created_at'] or '')[:16].replace('T',' '))}</span>
      </div>
      <h3 class="card-title">{_esc(ld['email'])}</h3>
      <div class="card-meta-row">Top tools: <code>{_esc(tools_chip)}</code></div>
      <div class="card-meta-row"><b>Subject:</b> {_esc(ld['subject'])}</div>
      <details>
        <summary>Show full email body ({len(ld['body'])} chars)</summary>
        <pre class="card-body">{_esc(ld['body'])}</pre>
      </details>
      <div class="card-actions">
        <button class="btn btn-approve" data-action="approve"
                data-url="/api/v1/admin/enterprise/leads/approve/{ld['id']}">
          ✓ Approve &amp; send via Resend
        </button>
        <button class="btn btn-reject" data-action="reject"
                data-url="/api/v1/admin/enterprise/leads/reject/{ld['id']}">
          ✗ Reject
        </button>
      </div>
    </div>"""

    # r47.37: inbound inquiries from /enterprise page — read-only, link to inbox
    inquiry_rows = "".join(
        f'<tr><td>{_esc(i["firm"][:40])}</td><td><a href="mailto:{_esc(i["email"])}">{_esc(i["name"][:30])}</a></td>'
        f'<td>{_esc(i["tier_requested"])}</td>'
        f'<td>{_esc(i["use_case"][:40])}</td>'
        f'<td>{_esc((i["created_at"] or "")[:16].replace("T", " "))}</td></tr>'
        for i in data.get("inquiries", [])
    ) or '<tr><td colspan="5" class="empty">No inbound inquiries yet — /enterprise just shipped.</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Partnership Draft Review — DC Hub Admin</title>
<meta name="robots" content="noindex,nofollow">
<style>
 body{{max-width:1080px;margin:0 auto;padding:24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;color:#0f172a;background:#f8fafc}}
 .hero{{padding:18px 0 24px;border-bottom:1px solid #e2e8f0;margin-bottom:24px}}
 .eyebrow{{color:#6366f1;font-size:.74rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}}
 h1{{font-size:1.8rem;margin:.3em 0;letter-spacing:-.025em}}
 h2{{font-size:1.15rem;margin:1.8em 0 .6em;color:#1e293b;letter-spacing:-.01em;border-bottom:1px solid #e2e8f0;padding-bottom:6px;display:flex;justify-content:space-between;align-items:center}}
 .lead{{color:#475569;font-size:.95rem;max-width:780px}}
 #auth-box{{background:#fef3c7;border:1px solid #fbbf24;border-radius:8px;padding:14px 18px;margin:16px 0;font-size:.92rem;color:#92400e}}
 #auth-box input{{margin-left:8px;padding:6px 10px;border:1px solid #fbbf24;border-radius:4px;font-family:ui-monospace,monospace;font-size:.85rem;width:300px}}
 #auth-box button{{margin-left:8px;padding:6px 14px;background:#92400e;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:600}}
 #auth-box.ok{{background:#dcfce7;border-color:#22c55e;color:#15803d}}
 #auth-box.ok button{{background:#15803d}}
 .empty-state{{padding:32px;text-align:center;color:#94a3b8;background:#fff;border-radius:10px;border:1px solid #e2e8f0;font-size:.92rem}}
 .card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:18px 22px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
 .card-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
 .card-meta{{font-size:.78rem;color:#64748b;font-family:ui-monospace,monospace}}
 .card-meta-row{{font-size:.82rem;color:#64748b;margin:6px 0}}
 .card-meta-row a{{color:#6366f1;text-decoration:none}}
 .card-title{{font-size:1.05rem;font-weight:600;margin:6px 0 4px;letter-spacing:-.01em}}
 .card-sub{{color:#475569;font-size:.92rem;margin:4px 0 8px}}
 .card-body{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:14px 16px;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:.82rem;line-height:1.6;color:#1e293b;white-space:pre-wrap;word-wrap:break-word;margin:8px 0;max-height:400px;overflow-y:auto}}
 details summary{{cursor:pointer;color:#6366f1;font-size:.82rem;font-weight:500;padding:4px 0;user-select:none}}
 details summary:hover{{text-decoration:underline}}
 .badge{{display:inline-block;padding:3px 10px;border-radius:4px;font-size:.7rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase}}
 .badge.press{{background:#e0e7ff;color:#3730a3}}
 .badge.linkedin{{background:#dbeafe;color:#1e40af}}
 .badge.enterprise{{background:linear-gradient(135deg,#8b5cf6,#6366f1);color:#fff}}
 .card-meta-row code{{background:#f1f5f9;padding:1px 6px;border-radius:3px;font-size:.8rem}}
 .card-actions{{display:flex;gap:8px;margin-top:12px}}
 .btn{{padding:8px 14px;border:none;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:600;font-family:inherit}}
 .btn-approve{{background:#22c55e;color:#fff}}
 .btn-approve:hover:not(:disabled){{background:#16a34a}}
 .btn-reject{{background:#fff;color:#dc2626;border:1px solid #dc2626}}
 .btn-reject:hover:not(:disabled){{background:#fee2e2}}
 .btn:disabled{{opacity:.5;cursor:not-allowed}}
 .card.processing{{opacity:.6;pointer-events:none}}
 .card.done{{background:#dcfce7;border-color:#22c55e}}
 .card.rejected{{background:#fef2f2;border-color:#dc2626;opacity:.7}}
 .toast{{position:fixed;bottom:24px;right:24px;background:#0f172a;color:#fff;padding:12px 18px;border-radius:8px;font-size:.9rem;box-shadow:0 4px 12px rgba(0,0,0,.2);max-width:380px}}
 .toast.error{{background:#dc2626}}
 .toast.success{{background:#16a34a}}
 table{{width:100%;border-collapse:collapse;font-size:.88rem;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
 th{{background:#0f172a;color:#fff;text-align:left;padding:8px 12px;font-size:.74rem;text-transform:uppercase;letter-spacing:.05em}}
 td{{padding:8px 12px;border-top:1px solid #e2e8f0;font-size:.86rem}}
 td a{{color:#6366f1;text-decoration:none}}
 td .empty{{text-align:center;color:#94a3b8;font-style:italic}}
 .section-count{{font-size:.78rem;color:#64748b;font-weight:400;font-family:ui-monospace,monospace}}
 .refresh-btn{{padding:5px 12px;background:#6366f1;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:.78rem;font-weight:500}}
 .refresh-btn:hover{{background:#4f46e5}}
</style></head><body>

<div class="hero">
 <div class="eyebrow">DC Hub · Admin · Partnership Review</div>
 <h1>Pending drafts awaiting approval</h1>
 <p class="lead">All partnership-related content (press releases and LinkedIn posts that name third parties)
 lands here as a draft. Review the body, then approve or reject. Computed {today}.</p>

 <div id="auth-box">
  <b>Admin key required to approve/reject.</b>
  <input id="admin-key" type="password" placeholder="Paste DCHUB_ADMIN_KEY">
  <button onclick="saveKey()">Save</button>
  <span id="key-status"></span>
 </div>
</div>

<h2>
 Press release drafts
 <span class="section-count">{len(data['press'])} pending</span>
</h2>
{press_cards or '<div class="empty-state">No press release drafts pending. The Tuesday cron will create one this week.</div>'}

<h2>
 LinkedIn post drafts
 <span class="section-count">{len(data['linkedin'])} pending</span>
</h2>
{linkedin_cards or '<div class="empty-state">No LinkedIn drafts pending. The Wednesday cron will create one this week.</div>'}

<h2>
 Enterprise outreach drafts <span style="background:#8b5cf6;color:#fff;padding:1px 8px;border-radius:999px;font-size:.65rem;margin-left:6px;letter-spacing:.06em">REVENUE</span>
 <span class="section-count">{len(data.get('enterprise_leads', []))} pending</span>
</h2>
{enterprise_cards or '<div class="empty-state">No enterprise outreach drafts pending. The Monday 15:00 UTC cron generates 10 leads/week from free-tier users with high paid-tool demand.</div>'}

<h2>
 Inbound enterprise inquiries
 <span class="section-count">{len(data.get('inquiries', []))} new/recent</span>
</h2>
<table>
 <thead><tr><th>Firm</th><th>Contact</th><th>Tier</th><th>Use case</th><th>Received</th></tr></thead>
 <tbody>{inquiry_rows}</tbody>
</table>

<h2>
 Recently published press
 <button class="refresh-btn" onclick="location.reload()">⟳ Refresh</button>
</h2>
<table>
 <thead><tr><th>Title</th><th>Published at</th></tr></thead>
 <tbody>{approved_press_rows}</tbody>
</table>

<h2>Recently posted LinkedIn</h2>
<table>
 <thead><tr><th>Track</th><th>Headline</th><th>Posted at</th><th>URN</th></tr></thead>
 <tbody>{posted_li_rows}</tbody>
</table>

<script>
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

function adminKey() {{ return localStorage.getItem('dchub_admin_key') || ''; }}
function setAdminKey(k) {{ localStorage.setItem('dchub_admin_key', k); }}

function refreshAuthUI() {{
  const box = $('#auth-box');
  const status = $('#key-status');
  const k = adminKey();
  if (k && k.length > 10) {{
    box.classList.add('ok');
    status.textContent = `✓ key set (${{k.length}} chars)`;
    $('#admin-key').value = '';
    $('#admin-key').placeholder = 'Key saved · enter new to overwrite';
  }} else {{
    box.classList.remove('ok');
    status.textContent = 'Not set';
  }}
}}

function saveKey() {{
  const k = $('#admin-key').value.trim();
  if (!k) return;
  setAdminKey(k);
  refreshAuthUI();
  toast('success', 'Admin key saved to local storage');
}}

function toast(type, msg) {{
  const t = document.createElement('div');
  t.className = `toast ${{type}}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}}

async function actOn(button) {{
  const card = button.closest('.card');
  const url = button.dataset.url;
  const action = button.dataset.action;
  const key = adminKey();

  if (!key || key.length < 10) {{
    toast('error', 'Admin key not set — paste it above first');
    return;
  }}

  card.classList.add('processing');
  card.querySelectorAll('button').forEach(b => b.disabled = true);

  try {{
    const r = await fetch(url, {{
      method: 'POST',
      headers: {{ 'X-Admin-Key': key, 'Content-Type': 'application/json' }},
    }});
    const data = await r.json().catch(() => ({{}}));
    if (r.ok) {{
      card.classList.remove('processing');
      card.classList.add(action === 'approve' ? 'done' : 'rejected');
      card.querySelectorAll('button').forEach(b => b.style.display = 'none');
      toast('success', `${{action === 'approve' ? 'Approved' : 'Rejected'}} ✓ ` + (data.title || data.track || data.slug || ''));
      // Auto-refresh after 2s to update the "recently published" tables
      setTimeout(() => location.reload(), 2000);
    }} else {{
      card.classList.remove('processing');
      card.querySelectorAll('button').forEach(b => b.disabled = false);
      toast('error', data.error || data.hint || `HTTP ${{r.status}}`);
    }}
  }} catch (e) {{
    card.classList.remove('processing');
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    toast('error', e.message);
  }}
}}

// Wire up all action buttons
$$('[data-action]').forEach(btn => btn.addEventListener('click', () => actOn(btn)));

// Initial UI state
refreshAuthUI();
</script>

</body></html>"""

    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                             "CDN-Cache-Control": "no-store",
                             "X-Robots-Tag": "noindex, nofollow"})
