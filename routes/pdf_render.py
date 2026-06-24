"""Shared HTML->PDF rendering via the private Gotenberg service (r-pdfrender 2026-06-24).

weasyprint can't load on Railway (libgobject missing), so PDF rendering is delegated to
a private Gotenberg (chromium) service reachable only on Railway's internal network at
render-pdf.railway.internal:3000 — no public exposure. Used by the premium Site Analysis
endpoint and (to fix the long-broken weasyprint path) the /site-report PDF export.

  html_to_pdf(html) -> bytes        # raises on failure; callers fall back / 500 gracefully
"""
import os
import requests

# Private internal URL (Railway service-to-service). Overridable via env for staging/local.
RENDER_PDF_URL = (os.environ.get("RENDER_PDF_URL")
                  or "http://render-pdf.railway.internal:3000").rstrip("/")


def render_available() -> bool:
    return bool(RENDER_PDF_URL)


def html_to_pdf(html: str, *, landscape: bool = False, margins_in: float = 0.0,
                timeout: int = 60) -> bytes:
    """Render a full HTML document to PDF bytes via Gotenberg's chromium engine.

    printBackground=true is REQUIRED for the dark Site Analysis theme (else the
    backgrounds drop out); preferCssPageSize=true honors the form's @page size.
    """
    files = {"files": ("index.html", html, "text/html")}
    data = {
        "printBackground": "true",
        "preferCssPageSize": "true",
        "marginTop": str(margins_in), "marginBottom": str(margins_in),
        "marginLeft": str(margins_in), "marginRight": str(margins_in),
    }
    if landscape:
        data["landscape"] = "true"
    r = requests.post(RENDER_PDF_URL + "/forms/chromium/convert/html",
                      files=files, data=data, timeout=timeout)
    r.raise_for_status()
    return r.content
