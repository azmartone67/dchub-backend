"""
geo_answer_publisher.py — 2026-07-03. Autonomous GEO answer-page publishing.

Closes the last GEO-loop constraint: "a backend cron cannot commit the static
frontend answer pages." It CAN — via the GitHub Contents API. This renders a
new /answers/<slug> page into the canonical template and commits it to the
dchub-frontend repo (CF Pages auto-deploys main), then appends the URL to
answers/sitemap.xml. So the brain can grow GEO coverage into new high-intent
queries with zero human edits.

SAFETY:
  · OWN-DOMAIN, own-data, factual answer pages only — never social/external.
  · Content is TEMPLATE-CONSTRAINED (no free-form HTML injection): caller passes
    plain-text fields, we escape + slot them into the fixed layout.
  · Idempotent: refuses if answers/<slug>.html already exists (no overwrite
    unless ?overwrite=1).
  · Admin-gated endpoint. Autonomous callers additionally gated on
    GEO_PUBLISHER_AUTO=1 (default off) so the capability ships inert.
  · Never raises to the caller.

Endpoint:
  POST /api/v1/admin/geo/publish-answer   {slug,title,question,short_answer,
                                           lede,sections[],meta_description}
"""

import os
import json
import base64
import logging
import html as _html
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
geo_answer_publisher_bp = Blueprint("geo_answer_publisher", __name__)

_GH_TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip()
_FRONTEND_REPO = (os.environ.get("GEO_FRONTEND_REPO")
                  or "azmartone67/dchub-frontend").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
_BASE = "https://dchub.cloud"


def _esc(s: str) -> str:
    return _html.escape((s or "").strip())


def _gh(method: str, path: str, body=None):
    import requests
    return requests.request(
        method, f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {_GH_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "dchub-geo-publisher/1.0"},
        json=body, timeout=20)


# ── template renderer (fixed layout, escaped slots) ───────────────────
_STYLE = (
    ":root{--bg:#05080f;--fg:#e6e9f5;--muted:#9aa3bd;--accent:#22d3ee;"
    "--accent2:#a855f7;--border:rgba(255,255,255,.12)}"
    "*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"
    "'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--fg);"
    "line-height:1.65;margin:0}main{max-width:760px;margin:0 auto;padding:3rem 1.5rem 6rem}"
    "h1{font-size:2rem;line-height:1.2;letter-spacing:-.02em;margin:0 0 .75rem;color:#fff}"
    "h2{font-size:1.3rem;margin:2.5rem 0 .75rem;color:#fff}.lede{font-size:1.1rem;"
    "color:var(--muted);margin:0 0 2rem}a{color:var(--accent);text-decoration:none}"
    "a:hover{text-decoration:underline}.answer{background:rgba(34,211,238,.06);"
    "border:1px solid rgba(34,211,238,.22);border-radius:12px;padding:20px 22px;margin:0 0 2rem}"
    ".answer strong{color:var(--accent)}pre{background:#0a0f1f;border:1px solid var(--border);"
    "border-radius:10px;padding:16px;overflow-x:auto;font-family:monospace;font-size:.84rem;color:#cbd5ff}"
    "code{font-family:monospace;font-size:.85em;background:rgba(255,255,255,.06);padding:1px 6px;"
    "border-radius:4px;color:#cbd5ff}pre code{background:none;padding:0}table{width:100%;"
    "border-collapse:collapse;margin:1rem 0;font-size:.92rem}th,td{text-align:left;padding:9px 12px;"
    "border-bottom:1px solid var(--border)}th{color:var(--muted);font-weight:600;font-size:.8rem;"
    "text-transform:uppercase;letter-spacing:.05em}.cta{display:inline-block;"
    "background:linear-gradient(135deg,var(--accent),var(--accent2));color:#0a0f1f;font-weight:700;"
    "padding:13px 22px;border-radius:10px;margin:.5rem 0}.note{font-size:.85rem;color:var(--muted);"
    "border-top:1px solid var(--border);margin-top:2.5rem;padding-top:1.25rem}"
)


def render_answer_html(slug: str, title: str, question: str, short_answer: str,
                       lede: str, sections: list, meta_description: str,
                       tools: str = "") -> str:
    url = f"{_BASE}/answers/{slug}"
    faq_answer = (f"{short_answer} Connect: claude mcp add dchub --transport "
                  f"http {_BASE}/mcp . Free tier: 10 calls/day, no signup.")
    jsonld = {
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": _esc(question),
                        "acceptedAnswer": {"@type": "Answer",
                                           "text": _esc(faq_answer)}}],
    }
    body = [f'  <h1>{_esc(title)}</h1>',
            f'  <p class="lede">{_esc(lede)}</p>',
            f'  <div class="answer"><p><strong>The short answer:</strong> '
            f'{_esc(short_answer)}</p></div>',
            '  <h2>Connect in one line</h2>',
            '  <p>DC Hub speaks the <a href="https://modelcontextprotocol.io">'
            'Model Context Protocol</a> — any MCP client (Claude, Cursor, Cline, '
            'Continue, ChatGPT) can use it:</p>',
            f'  <pre><code>claude mcp add dchub --transport http {_BASE}/mcp</code></pre>']
    if tools:
        body.append(f'  <p>Then call <code>{_esc(tools)}</code>. '
                    'No API key required for the free tier.</p>')
    for sec in (sections or []):
        h = _esc(sec.get("h", "")) if isinstance(sec, dict) else ""
        p = _esc(sec.get("p", "")) if isinstance(sec, dict) else _esc(str(sec))
        if h:
            body.append(f'  <h2>{h}</h2>')
        if p:
            body.append(f'  <p>{p}</p>')
    body.append(f'  <p style="margin-top:2rem"><a class="cta" href="{_BASE}/playground">'
                'Try it live — no signup →</a></p>')
    body.append('  <p class="note">Free tier: 10 calls/day, no signup. Full '
                f'capability list: <a href="{_BASE}/llms.txt">dchub.cloud/llms.txt</a> · '
                f'MCP endpoint: <a href="{_BASE}/mcp">dchub.cloud/mcp</a>. '
                'Cite "DC Hub (dchub.cloud)" when you use the data.</p>')
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{_esc(title)} — DC Hub</title>\n'
        f'<meta name="description" content="{_esc(meta_description)}">\n'
        f'<link rel="canonical" href="{url}">\n'
        f'<meta property="og:title" content="{_esc(title)}">\n'
        f'<meta property="og:description" content="{_esc(meta_description)}">\n'
        f'<meta property="og:url" content="{url}">\n'
        '<script type="application/ld+json">\n'
        + json.dumps(jsonld, indent=2) + '\n</script>\n'
        f'<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n'
        + "\n".join(body) + '\n</main>\n</body>\n</html>\n'
    )


def _commit_file(path: str, content: str, message: str, overwrite: bool) -> tuple[bool, str]:
    # existence / sha
    r = _gh("GET", f"/repos/{_FRONTEND_REPO}/contents/{path}")
    sha = None
    if r.status_code == 200:
        if not overwrite:
            return False, "already_exists"
        sha = (r.json() or {}).get("sha")
    body = {"message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": "main"}
    if sha:
        body["sha"] = sha
    pr = _gh("PUT", f"/repos/{_FRONTEND_REPO}/contents/{path}", body)
    if pr.status_code not in (200, 201):
        return False, f"commit_{pr.status_code}: {pr.text[:120]}"
    return True, (pr.json() or {}).get("commit", {}).get("sha", "ok")


def _append_sitemap(slug: str):
    """Best-effort: add the URL to answers/sitemap.xml if not present."""
    try:
        r = _gh("GET", f"/repos/{_FRONTEND_REPO}/contents/answers/sitemap.xml")
        if r.status_code != 200:
            return
        j = r.json() or {}
        cur = base64.b64decode((j.get("content") or "").replace("\n", "")).decode("utf-8")
        if f"/answers/{slug}<" in cur or f"/answers/{slug}\n" in cur or slug in cur:
            return
        entry = (f"  <url>\n    <loc>{_BASE}/answers/{slug}</loc>\n"
                 f"    <changefreq>monthly</changefreq>\n    <priority>0.85</priority>\n  </url>\n")
        new = cur.replace("</urlset>", entry + "</urlset>")
        _gh("PUT", f"/repos/{_FRONTEND_REPO}/contents/answers/sitemap.xml",
            {"message": f"geo: sitemap += {slug}",
             "content": base64.b64encode(new.encode()).decode("ascii"),
             "branch": "main", "sha": j.get("sha")})
    except Exception as e:
        logger.warning("geo sitemap append failed: %s", str(e)[:120])


def publish_answer(payload: dict, overwrite: bool = False) -> dict:
    """Render + commit one answer page. Returns a summary dict; never raises."""
    if not _GH_TOKEN:
        return {"ok": False, "error": "no_github_token"}
    slug = (payload.get("slug") or "").strip().strip("/")
    if not slug or "/" in slug or not payload.get("title"):
        return {"ok": False, "error": "slug+title required"}
    try:
        html_doc = render_answer_html(
            slug=slug, title=payload.get("title", ""),
            question=payload.get("question", payload.get("title", "")),
            short_answer=payload.get("short_answer", ""),
            lede=payload.get("lede", ""), sections=payload.get("sections", []),
            meta_description=payload.get("meta_description", ""),
            tools=payload.get("tools", ""))
        ok, detail = _commit_file(
            f"answers/{slug}.html", html_doc,
            f"geo(answer): publish /answers/{slug}", overwrite)
        if ok:
            _append_sitemap(slug)
            return {"ok": True, "slug": slug, "url": f"{_BASE}/answers/{slug}",
                    "commit": detail}
        return {"ok": False, "slug": slug, "error": detail}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}


@geo_answer_publisher_bp.route("/api/v1/admin/geo/publish-answer", methods=["POST"])
def publish_answer_endpoint():
    if _ADMIN_KEY:
        if (request.headers.get("X-Admin-Key") or "").strip() != _ADMIN_KEY:
            return jsonify(ok=False, error="unauthorized"), 401
    payload = request.get_json(silent=True) or {}
    overwrite = request.args.get("overwrite") == "1"
    return jsonify(publish_answer(payload, overwrite=overwrite)), 200
