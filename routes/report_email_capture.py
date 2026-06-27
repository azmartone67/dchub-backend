"""Shared, NON-gating email-capture block for the public CC-BY report surfaces.

The report bodies stay fully public + crawlable (the GEO/citation engine), and the
machine-readable /api/v1/reports/*.json|.csv|.md endpoints stay OPEN for agents and
LLMs to cite. This component only ADDS, to the human HTML pages:
  1. a digest subscribe box  → POST /api/v1/digest/subscribe  (fills the retention audience)
  2. an OPTIONAL email step before the human "Download dataset" button (client-side only —
     the raw API download target is unchanged, so agents/curl are never blocked).

Self-contained: its own gradient background + explicit colors so it reads on both the
dark (state-of-power / energy) and light (monthly / quarterly-deep) report themes. No
external CSS/JS deps. IDs are namespaced (rc-*) and the script is wrapped in an IIFE.
"""
import html as _html
import json as _json


def report_capture_block(cadence: str, dataset_url: str = None, source: str = None) -> str:
    """Return an HTML+JS snippet to inject right before </body> on a report page.

    cadence     — human label, e.g. "monthly" / "quarterly" / "daily State of Power".
    dataset_url — if given, renders a soft-gated "Download dataset" link (email first).
    source      — digest subscribe `source` tag; defaults to report_<cadence-slug>.
    """
    cadence = (cadence or "report").strip()
    nice = _html.escape(cadence)
    slug = "".join(c if c.isalnum() else "_" for c in cadence.lower()).strip("_") or "report"
    src = source or ("report_" + slug)
    src_js = _json.dumps(src)

    dataset_html = ""
    dl_js = "var dl=null;"
    if dataset_url:
        durl = _html.escape(dataset_url, quote=True)
        dataset_html = (
            '<div style="margin-top:14px;padding-top:12px;border-top:1px solid rgba(255,255,255,.22)">'
            f'<a href="{durl}" id="rc-dl" '
            'style="color:#fff;font-weight:700;font-size:13.5px;text-decoration:none;'
            'border-bottom:1px solid rgba(255,255,255,.5);padding-bottom:1px">⬇ Download the full dataset</a>'
            '<span style="font-size:12px;opacity:.82;margin-left:8px">— enter your email above first</span>'
            "</div>"
        )
        dl_js = "var dl=document.getElementById('rc-dl');"

    js = (
        "(function(){var src=" + src_js + ";"
        "var form=document.getElementById('rc-form'),"
        "email=document.getElementById('rc-email'),"
        "msg=document.getElementById('rc-msg');" + dl_js +
        "var done=false;"
        "function sub(source){var e=(email.value||'').trim();"
        "if(!e||e.indexOf('@')<1||e.indexOf('.')<2){msg.textContent='Please enter a valid email.';return Promise.reject();}"
        "return fetch('/api/v1/digest/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({email:e,source:source})}).then(function(r){return r.json().catch(function(){return {};});});}"
        "if(form)form.addEventListener('submit',function(ev){ev.preventDefault();"
        "sub(src).then(function(){done=true;msg.textContent='\\u2713 Subscribed \\u2014 check your inbox.';}).catch(function(){});});"
        "if(dl)dl.addEventListener('click',function(ev){if(done)return;ev.preventDefault();"
        "sub(src+'_dataset').then(function(){done=true;msg.textContent='\\u2713 Thanks \\u2014 starting your download\\u2026';"
        "setTimeout(function(){window.location.href=dl.getAttribute('href');},500);})"
        ".catch(function(){msg.textContent='Enter your email above, then click download.';});});})();"
    )

    return (
        '<section style="max-width:760px;margin:44px auto 8px;padding:24px 26px;border-radius:14px;'
        'background:linear-gradient(135deg,#0891b2,#7c3aed);color:#fff;'
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Inter,sans-serif;"
        'box-shadow:0 12px 34px rgba(0,0,0,.20)">'
        f'<div style="font-weight:800;font-size:18px;margin-bottom:4px">📩 Get the {nice} report by email</div>'
        '<div style="font-size:13px;opacity:.92;margin-bottom:14px">'
        "Fresh DC Hub data-center intelligence in your inbox. No spam — unsubscribe anytime.</div>"
        '<form id="rc-form" style="display:flex;gap:8px;flex-wrap:wrap">'
        '<input id="rc-email" type="email" required placeholder="you@company.com" '
        'style="flex:1 1 220px;min-width:0;padding:11px 13px;border:none;border-radius:8px;'
        'font-size:14px;color:#0a0a0e;background:#fff"/>'
        '<button type="submit" style="padding:11px 20px;border:none;border-radius:8px;cursor:pointer;'
        'font-weight:700;font-size:14px;color:#6d28d9;background:#fff">Subscribe</button>'
        "</form>"
        '<div id="rc-msg" style="font-size:12.5px;margin-top:10px;min-height:16px;opacity:.95"></div>'
        + dataset_html +
        "<script>" + js + "</script>"
        "</section>"
    )
