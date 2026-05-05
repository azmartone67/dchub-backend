"""
news_slug_route.py - /news/<slug> Flask route, registered from main.py.

Renders any row from the press_releases table as a styled dark-theme HTML
page. Called by main.py at startup via register(app, get_conn, return_conn).
"""
import html as _html


_PAGE_HEAD_1 = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>"""

_PAGE_HEAD_2 = """ - DC Hub</title>
<meta name="description" content=\""""

_PAGE_HEAD_3 = """\">
<style>
:root { --bg:#0a0a0f; --fg:#e8eaf0; --muted:#888; --muted2:#aaa;
  --accent:#8b6fff; --accent-bg:rgba(139,111,255,0.12); --border:#232737; }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.65 -apple-system,"Inter","Segoe UI",Roboto,Arial,sans-serif;
  background:var(--bg); color:var(--fg); }
.wrap { max-width:780px; margin:0 auto; padding:40px 28px 80px; }
.kicker { font-size:11px; letter-spacing:1.5px; text-transform:uppercase;
  color:var(--accent); font-weight:700; margin-bottom:12px; }
h1 { font-size:36px; line-height:1.15; letter-spacing:-0.8px;
  margin:0 0 14px; font-weight:800; }
.subhead { font-size:19px; color:var(--muted2); margin:0 0 28px; line-height:1.5; }
.meta { font-size:13px; color:var(--muted); margin:0 0 36px;
  padding-bottom:24px; border-bottom:1px solid var(--border); }
p { margin:0 0 18px; font-size:17px; line-height:1.7; }
blockquote { margin:24px 0; padding:18px 22px; background:var(--accent-bg);
  border-left:3px solid var(--accent); border-radius:4px;
  font-style:italic; color:var(--muted2); }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.footer { margin-top:48px; padding-top:24px; border-top:1px solid var(--border);
  font-size:13px; color:var(--muted); }
.nav { margin-bottom:24px; font-size:13px; }
.nav a { color:var(--muted); }
</style>
</head><body><div class="wrap">
<div class="nav"><a href="/news">&larr; All News</a></div>
<div class="kicker">"""

_PAGE_BODY_1 = """</div>
<h1>"""

_PAGE_BODY_2 = """</h1>
<div class="subhead">"""

_PAGE_BODY_3 = """</div>
<div class="meta"><strong>"""

_PAGE_BODY_4 = """</strong></div>
<div class="body">"""

_PAGE_BODY_5 = """</div>
<div class="footer">Published by DC Hub &middot;
<a href="https://dchub.cloud">dchub.cloud</a> &middot;
<a href="/press">Press Room</a> &middot;
<a href="/api/press-releases/"""

_PAGE_FOOTER = """\">JSON</a></div>
</div></body></html>"""


_NOT_FOUND_1 = """<!doctype html><html><head>
<meta charset="utf-8"/><title>Article Not Found - DC Hub</title>
<style>body{font:16px/1.5 -apple-system,sans-serif;background:#0a0a0f;
color:#e8eaf0;text-align:center;padding:80px 20px}
h1{color:#8b6fff}a{color:#8b6fff}</style></head><body>
<h1>Article not found</h1><p>No press release with slug '"""

_NOT_FOUND_2 = """' exists.</p>
<p><a href="/news">&larr; Back to all news</a></p></body></html>"""


def _format_date(date_val):
    if not date_val:
        return ""
    try:
        if hasattr(date_val, "strftime"):
            return date_val.strftime("%B %d, %Y")
        return str(date_val)
    except Exception:
        return str(date_val)


def _body_to_html(body_text):
    if not body_text:
        return ""
    parts = [p.strip() for p in body_text.split("\n\n") if p.strip()]
    out = []
    for p in parts:
        esc = _html.escape(p).replace("\n", "<br>")
        first = esc.lstrip()[:15].lower()
        if first.startswith("quote from") or first.startswith("&quot;"):
            out.append("<blockquote>" + esc + "</blockquote>")
        else:
            out.append("<p>" + esc + "</p>")
    return "\n".join(out)


def _render_page(title, subheadline, body, category, date_human, slug, meta):
    return (
        _PAGE_HEAD_1 + _html.escape(title) +
        _PAGE_HEAD_2 + _html.escape(meta) +
        _PAGE_HEAD_3 + _html.escape(category.upper()) +
        _PAGE_BODY_1 + _html.escape(title) +
        _PAGE_BODY_2 + _html.escape(subheadline) +
        _PAGE_BODY_3 + _html.escape(date_human) +
        _PAGE_BODY_4 + _body_to_html(body) +
        _PAGE_BODY_5 + _html.escape(slug) +
        _PAGE_FOOTER
    )


def _render_not_found(slug):
    return _NOT_FOUND_1 + _html.escape(slug) + _NOT_FOUND_2


def register(app, get_pg_conn, return_pg_conn):
    """Register the /news/<slug> route on the given Flask app."""
    @app.route("/news/<slug>", methods=["GET"])
    def news_slug_page(slug):
        if slug.startswith("digest-"):
            return "Use /news/digest-YYYY-MM-DD for date-based digests", 404

        conn = None
        try:
            conn = get_pg_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, slug, title, subheadline, body, category, date, "
                "meta_description FROM press_releases WHERE slug = %s LIMIT 1",
                (slug,),
            )
            row = cur.fetchone()
            return_pg_conn(conn)
            conn = None

            if not row:
                return _render_not_found(slug), 404

            _id, _slug, title, subheadline, body, category, date_val, meta = row
            return _render_page(
                title=title or "",
                subheadline=subheadline or "",
                body=body or "",
                category=category or "Press Release",
                date_human=_format_date(date_val),
                slug=_slug or slug,
                meta=(meta or subheadline or title or "")[:300],
            )
        except Exception as e:
            try:
                if conn:
                    return_pg_conn(conn)
            except Exception:
                pass
            return _render_not_found(slug + " (error: " + str(e)[:120] + ")"), 500
