#!/usr/bin/env python3
"""
patch_news_slug_page.py — add /news/<slug> Flask route to render press releases.

Currently /news/<slug> returns 404 because no route is registered. Only
/news/digest-<date_slug> exists (for date-based digests). This patcher
appends a /news/<slug> route that:
  1. Queries press_releases by slug (via the same pg_connection pattern)
  2. Renders a styled dark-theme HTML page inline
  3. Returns 404 with a clean message if slug not found

Idempotent. Creates main.py.bak.newsslug. Auto-restores on syntax error.

Usage:
    python3 patch_news_slug_page.py
"""
from __future__ import annotations
import sys
import shutil
import py_compile
from pathlib import Path

SRC = Path("main.py")
if not SRC.exists():
    print("ERROR: main.py not found.")
    sys.exit(1)

shutil.copy2(SRC, "main.py.bak.newsslug")
text = SRC.read_text()

if "def news_slug_page(" in text:
    print("· /news/<slug> route already present — nothing to do.")
    sys.exit(0)

# Insert before the final `import dchub_cors_patch` so the route registers
# before the app starts serving traffic.
ANCHOR = "import dchub_cors_patch"
if ANCHOR not in text:
    print(f"ERROR: anchor '{ANCHOR}' not found in main.py")
    sys.exit(2)

NEW_ROUTE = '''
# ─────────────────────────────────────────────────────────────────────────
# /news/<slug> — render press release as styled HTML page
# Added: 2026-04-15 (fixes 404 for slug-based news URLs)
# ─────────────────────────────────────────────────────────────────────────

_NEWS_SLUG_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title_safe} — DC Hub</title>
<meta name="description" content="{meta_safe}">
<meta property="og:title" content="{title_safe}">
<meta property="og:description" content="{meta_safe}">
<meta property="og:type" content="article">
<meta property="og:url" content="https://dchub.cloud/news/{slug_safe}">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="https://dchub.cloud/news/{slug_safe}">
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"NewsArticle","headline":"{title_safe}","datePublished":"{date_safe}","author":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},"publisher":{{"@type":"Organization","name":"DC Hub"}}, "description":"{meta_safe}"}}
</script>
<style>
:root {{ --bg:#0a0a0f; --fg:#e8eaf0; --muted:#888; --muted2:#aaa;
  --accent:#8b6fff; --accent-bg:rgba(139,111,255,0.12); --border:#232737; --card:#12141e; }}
*{{box-sizing:border-box}}
body{{margin:0;font:16px/1.65 -apple-system,"Inter","Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--fg)}}
.wrap{{max-width:780px;margin:0 auto;padding:40px 28px 80px}}
.kicker{{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent);font-weight:700;margin-bottom:12px}}
h1{{font-size:36px;line-height:1.15;letter-spacing:-0.8px;margin:0 0 14px;font-weight:800}}
.subhead{{font-size:19px;color:var(--muted2);margin:0 0 28px;line-height:1.5;font-weight:400}}
.meta{{font-size:13px;color:var(--muted);margin:0 0 36px;padding-bottom:24px;border-bottom:1px solid var(--border)}}
p{{margin:0 0 18px;font-size:17px;line-height:1.7}}
.body p:first-child::first-letter{{font-size:1.8em;font-weight:700;color:var(--accent);float:left;line-height:1;padding:6px 12px 0 0}}
blockquote{{margin:24px 0;padding:18px 22px;background:var(--accent-bg);border-left:3px solid var(--accent);border-radius:4px;font-style:italic;color:var(--muted2)}}
a{{color:var(--accent);text-decoration:none;border-bottom:1px dashed rgba(139,111,255,0.4)}}
a:hover{{border-bottom-style:solid}}
.footer{{margin-top:48px;padding-top:24px;border-top:1px solid var(--border);font-size:13px;color:var(--muted)}}
.nav{{margin-bottom:24px;font-size:13px}}
.nav a{{color:var(--muted);border-bottom:none}}
</style>
</head>
<body>
<div class="wrap">
  <div class="nav"><a href="/news">← All News</a></div>
  <div class="kicker">{category_safe}</div>
  <h1>{title_safe}</h1>
  <div class="subhead">{subheadline_safe}</div>
  <div class="meta"><strong>{date_human}</strong></div>
  <div class="body">{body_html}</div>
  <div class="footer">
    Published by DC Hub · <a href="https://dchub.cloud">dchub.cloud</a> ·
    <a href="/press">Press Room</a> · <a href="/api/press-releases/{slug_safe}">JSON</a>
  </div>
</div>
</body>
</html>"""


_NEWS_NOT_FOUND = """<!doctype html><html><head><meta charset="utf-8"/>
<title>Article Not Found — DC Hub</title>
<style>body{font:16px/1.5 -apple-system,sans-serif;background:#0a0a0f;color:#e8eaf0;
text-align:center;padding:80px 20px}h1{color:#8b6fff}a{color:#8b6fff}</style>
</head><body><h1>Article not found</h1><p>No press release with slug '{slug}' exists.</p>
<p><a href="/news">← Back to all news</a></p></body></html>"""


@app.route('/news/<slug>', methods=['GET'])
def news_slug_page(slug):
    """Render a press release as a styled HTML page from the press_releases table."""
    import html as _html
    # Date-based URLs are handled by the existing /news/digest-<date_slug> route.
    # If someone hits /news/2026-04-15 directly without the digest- prefix,
    # let it fall through (Flask routes the digest- prefix one separately).
    if slug.startswith('digest-'):
        # Shouldn't reach here — digest- route is more specific. But just in case:
        from flask import redirect
        return redirect(f'/news/{slug}', code=301)

    conn = None
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, slug, title, subheadline, body, category, date, meta_description "
            "FROM press_releases WHERE slug = %s LIMIT 1",
            (slug,)
        )
        row = cur.fetchone()
        return_pg_connection(conn); conn = None

        if not row:
            return _NEWS_NOT_FOUND.format(slug=_html.escape(slug)), 404

        _id, _slug, title, subheadline, body, category, date_val, meta = row

        # Format date for display
        try:
            if date_val:
                date_human = date_val.strftime('%B %-d, %Y') if hasattr(date_val, 'strftime') else str(date_val)
                date_iso = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)
            else:
                date_human = ''
                date_iso = ''
        except Exception:
            date_human = str(date_val or '')
            date_iso = str(date_val or '')

        # Convert body \n\n to <p> paragraphs (preserves single \n as <br>)
        body_text = body or ''
        paragraphs = [p.strip() for p in body_text.split('\n\n') if p.strip()]
        body_html = ''
        for p in paragraphs:
            esc = _html.escape(p).replace('\n', '<br>')
            # Detect quote-style paragraphs (start with " or "Quote from)
            if esc.lstrip().startswith('Quote from') or esc.lstrip().startswith('&quot;'):
                body_html += f'<blockquote>{esc}</blockquote>\n'
            else:
                body_html += f'<p>{esc}</p>\n'

        return _NEWS_SLUG_TEMPLATE.format(
            title_safe       = _html.escape(title or ''),
            subheadline_safe = _html.escape(subheadline or ''),
            meta_safe        = _html.escape((meta or subheadline or title or '')[:300]),
            slug_safe        = _html.escape(_slug or slug),
            category_safe    = _html.escape((category or 'Press Release').upper()),
            date_human       = _html.escape(date_human),
            date_safe        = _html.escape(date_iso),
            body_html        = body_html,
        )
    except Exception as _e:
        try:
            if conn: return_pg_connection(conn)
        except Exception:
            pass
        return _NEWS_NOT_FOUND.format(slug=f'{slug} (error: {_html.escape(str(_e))})'), 500
'''

# Insert just before the final import dchub_cors_patch
idx = text.rfind(ANCHOR)
text = text[:idx] + NEW_ROUTE + '\n\n' + text[idx:]
SRC.write_text(text)
print(f"✓ Inserted /news/<slug> route ({len(NEW_ROUTE)} bytes)")

try:
    py_compile.compile(str(SRC), doraise=True)
    print("✓ Syntax OK — safe to commit.")
except py_compile.PyCompileError as e:
    print(f"✗ Syntax error: {e}")
    shutil.copy2("main.py.bak.newsslug", SRC)
    print("  Restored backup.")
    sys.exit(3)

print("\nNext:")
print("  git add main.py")
print("  git commit -m 'feat(news): add /news/<slug> route to render press releases as HTML'")
print("  git push")
print()
print("After Railway redeploys (~60s), test:")
print("  curl -sI 'https://dchub.cloud/news/dc-hub-global-infrastructure-1-29m-records-live' | head -3")
print("  # expect: HTTP/2 200")
print("  # then visit in browser to see the rendered page")
