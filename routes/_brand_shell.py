"""
routes/_brand_shell.py — DC Hub on-brand HTML shell for server-rendered pages.

Matches the site standard: Instrument Sans + /static/dchub-brand.css + the shared
/js/dchub-nav.js nav, brand palette (#0a2540 ink, #1976d2 accent). Use brand_page()
so new server-rendered pages look like the rest of dchub.cloud instead of generic CSS.
"""
import html as _h

_BRAND_CSS = (
    ":root{--ink:#0a2540;--muted:#5a6b85;--accent:#1976d2;--line:#e1e5ec;--line2:#e9eef5;--bg2:#eef2f8}"
    "body{font-family:'Instrument Sans',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
    "max-width:920px;margin:0 auto;padding:24px;line-height:1.55;color:var(--ink);background:#fff}"
    ".dc-main{margin-top:6px}"
    "h1{font-size:2rem;margin:0 0 6px;letter-spacing:-.01em}"
    ".lede{font-size:1.05rem;color:var(--muted);margin:4px 0 22px}"
    "h2{margin-top:38px;padding-top:14px;border-top:1px solid var(--line2);font-size:1.3rem}"
    "h2 .sub{font-weight:400;font-size:.9rem;color:var(--muted)}"
    "table{border-collapse:collapse;width:100%;margin:14px 0}"
    "th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line2);font-size:.95rem}"
    "th{font-weight:600;color:var(--muted);background:#f8fafc}"
    "a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}"
    ".big{font-size:1.05rem;background:var(--bg2);border:1px solid #d6e2f2;border-radius:10px;"
    "padding:14px 18px;margin:16px 0}"
    ".v{font-weight:700;font-size:.74rem;padding:3px 9px;border-radius:6px;text-transform:uppercase;letter-spacing:.02em}"
    ".v.build{background:#dcfce7;color:#166534}.v.caution{background:#fef9c3;color:#854d0e}"
    ".v.avoid{background:#fee2e2;color:#991b1b}"
    ".cite{color:var(--muted);font-size:.85rem;margin-top:28px;border-top:1px solid var(--line);padding-top:16px}"
    ".cite a{color:var(--accent)}"
)


def brand_page(title, description, canonical, body_html, ld_jsons=None, og_desc=None):
    """Return a full on-brand HTML page. title/description/og_desc are escaped for
    meta tags; body_html and ld_jsons are inserted as-is (caller builds safe HTML)."""
    e = lambda x: _h.escape(str(x if x is not None else ""))
    ld = "".join(f'<script type="application/ld+json">{j}</script>' for j in (ld_jsons or []))
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{e(title)}</title>"
        f"<meta name=\"description\" content=\"{e(description)}\">"
        f"<link rel=\"canonical\" href=\"{e(canonical)}\">"
        f"<meta property=\"og:title\" content=\"{e(title)}\">"
        f"<meta property=\"og:description\" content=\"{e(og_desc or description)}\">"
        f"<meta property=\"og:url\" content=\"{e(canonical)}\"><meta property=\"og:type\" content=\"website\">"
        "<meta property=\"og:image\" content=\"https://dchub.cloud/images/og-home.png\">"
        f"{ld}"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link href=\"https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&display=swap\" rel=\"stylesheet\">"
        "<link rel=\"stylesheet\" href=\"https://dchub.cloud/static/dchub-brand.css\">"
        f"<style>{_BRAND_CSS}</style></head><body>"
        "<script src=\"/js/dchub-nav.js\" defer></script>"
        f"<main class=\"dc-main\">{body_html}</main></body></html>"
    )
