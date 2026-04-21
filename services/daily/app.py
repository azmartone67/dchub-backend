"""FastAPI service — DC Hub Daily.

Endpoints:
    GET  /                          landing (HTML)
    GET  /health                    JSON health check
    POST /refresh                   pull fresh snapshot + regenerate all 9 PNGs  (cron hits this)
    GET  /today                     HTML page with today's 9 variants
    GET  /share/{date}              HTML page for a specific date
    GET  /generate                  ?theme=a&size=portrait[&date=YYYY-MM-DD]  -> PNG
    GET  /snapshot                  JSON of the latest raw counts
"""
from __future__ import annotations

import datetime
import io
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Header
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import render as R

# Lazy imports for optional backing services — keeps the app bootable
# in local dev with no Neon / no R2 credentials.
def _lazy_db():
    import db
    return db

def _lazy_storage():
    import storage
    return storage

def _lazy_mcp():
    import mcp_client
    return mcp_client

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="DC Hub Daily")

THEMES = ["a", "b", "c"]
SIZES = ["portrait", "square", "story"]
REFRESH_SECRET = os.environ.get("REFRESH_SECRET", "")


# ---------- helpers ----------------------------------------------------------

def _data_for(date: datetime.date | None) -> R.RenderData:
    snap = None
    if os.environ.get("DATABASE_URL"):
        try:
            snap = _lazy_db().get_snapshot(date)
        except Exception as e:  # noqa: BLE001
            log.warning("db read failed: %s", e)
    if not snap:
        # cold start: fall back to bundled seed
        import json
        snap = json.loads((Path(__file__).parent / "data.json").read_text())
    return R.RenderData(
        states=snap["states"],
        as_of=snap.get("as_of", ""),
        source=snap.get("source", "DC Hub MCP"),
        generated=(date or datetime.date.today()).isoformat(),
    )


def _r2_key(date: datetime.date, theme: str, size: str) -> str:
    return f"{date.isoformat()}/{theme}_{size}.png"


def default_theme(date: datetime.date) -> str:
    """Rotate A/B/C daily so the default share image never looks stale."""
    return THEMES[date.toordinal() % 3]


# ---------- routes -----------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True, "date": datetime.date.today().isoformat()}


@app.get("/snapshot")
def snapshot(date: str | None = None):
    d = datetime.date.fromisoformat(date) if date else None
    if os.environ.get("DATABASE_URL"):
        try:
            snap = _lazy_db().get_snapshot(d)
        except Exception as e:  # noqa: BLE001
            log.warning("db read failed: %s", e)
            snap = None
    else:
        import json
        snap = json.loads((Path(__file__).parent / "data.json").read_text())
    if not snap:
        raise HTTPException(404, "no snapshot for that date")
    return snap


@app.get("/generate")
def generate(
    theme: str = Query("a", pattern="^[abcd]$"),
    size: str = Query("portrait", pattern="^(portrait|square|story)$"),
    brief: str | None = Query(None, pattern="^(gdci|grid)$"),
    date: str | None = None,
):
    d = datetime.date.fromisoformat(date) if date else datetime.date.today()
    if brief == "gdci":
        data = _lazy_mcp().fetch_gdci()
        if not data:
            raise HTTPException(503, "GDCI data unavailable")
        img = R.render_gdci(data, size)
    elif brief == "grid":
        data = _lazy_mcp().fetch_grid()
        if not data:
            raise HTTPException(503, "Grid data unavailable")
        img = R.render_grid(data, size)
    else:
        img = R.render(theme, size, _data_for(d))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _do_refresh() -> dict:
    """Pull a fresh snapshot, render all 9 variants, push to R2, autopost, persist."""
    today = datetime.date.today()
    log.info("refresh start date=%s", today)
    snap = _lazy_mcp().fetch_snapshot()
    _lazy_db().upsert_snapshot(today, snap)

    data = R.RenderData(
        states=snap["states"],
        as_of=snap.get("as_of", ""),
        source=snap.get("source", "DC Hub MCP"),
        generated=today.isoformat(),
    )

    outputs: list[dict] = []
    # remember the "hero" image (the one that goes to social)
    import poster
    hero_theme, hero_size = poster.pick_autopost_variant(today)
    hero_bytes: bytes | None = None
    hero_url: str = ""

    for theme in THEMES:
        for size in SIZES:
            img = R.render(theme, size, data)
            buf = io.BytesIO(); img.save(buf, format="PNG")
            nbytes = buf.tell()
            key = _r2_key(today, theme, size)
            url = ""
            try:
                url = _lazy_storage().upload(key, buf.getvalue())
                _lazy_db().upsert_render(today, theme, size, key, nbytes)
            except Exception as e:  # noqa: BLE001
                log.error("upload failed %s: %s", key, e)
            outputs.append({"theme": theme, "size": size, "bytes": nbytes, "url": url})
            if theme == hero_theme and size == hero_size:
                hero_bytes = buf.getvalue()
                hero_url = url

    # Brief renders (Phase 3) — also render GDCI + Grid daily briefs to R2
    for brief_name, fetch_fn, render_fn in [
        ("gdci", _lazy_mcp().fetch_gdci, R.render_gdci),
        ("grid", _lazy_mcp().fetch_grid, R.render_grid),
    ]:
        try:
            brief_data = fetch_fn()
            if not brief_data:
                log.warning("brief %s: no data, skipping", brief_name)
                continue
            for size in SIZES:
                img = render_fn(brief_data, size)
                buf = io.BytesIO(); img.save(buf, format="PNG")
                nbytes = buf.tell()
                key = _r2_key(today, brief_name, size)
                url = ""
                try:
                    url = _lazy_storage().upload(key, buf.getvalue())
                    _lazy_db().upsert_render(today, brief_name, size, key, nbytes)
                except Exception as e:  # noqa: BLE001
                    log.error("brief upload failed %s: %s", key, e)
                outputs.append({"theme": brief_name, "size": size, "bytes": nbytes, "url": url})
        except Exception as e:  # noqa: BLE001
            log.error("brief %s failed: %s", brief_name, e)

    # fan out to social (all no-ops if AUTOPOST_ENABLED != 1)
    post_result = {}
    if hero_bytes is not None:
        try:
            post_result = poster.autopost(today, snap, hero_bytes, hero_url).as_dict()
        except Exception as e:  # noqa: BLE001
            log.error("autopost failed: %s", e)
            post_result = {"error": str(e)[:200]}

    log.info("refresh complete, %d renders, posts=%s", len(outputs), post_result)
    return {"date": today.isoformat(), "renders": outputs, "posts": post_result}


@app.post("/refresh")
def refresh(background: BackgroundTasks,
            x_refresh_secret: str | None = Header(default=None)):
    if REFRESH_SECRET and x_refresh_secret != REFRESH_SECRET:
        raise HTTPException(401, "bad refresh secret")
    # fire-and-forget so cron doesn't time out
    background.add_task(_do_refresh)
    return {"queued": True}


@app.get("/today", response_class=HTMLResponse)
def today_page():
    return share_page_html(datetime.date.today())


@app.get("/share/{date}", response_class=HTMLResponse)
def share_page(date: str):
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return share_page_html(d)


def share_page_html(date: datetime.date) -> str:
    rows = []
    if os.environ.get("DATABASE_URL"):
        try:
            rows = _lazy_db().get_renders(date)
        except Exception:
            rows = []
    base = os.environ.get("R2_PUBLIC_BASE", "")
    cards = []
    for theme in THEMES:
        for size in SIZES:
            # prefer R2 url if present, else fall back to inline /generate
            if base:
                src = f"{base}/{_r2_key(date, theme, size)}"
            else:
                src = f"/generate?theme={theme}&size={size}&date={date.isoformat()}"
            cards.append(f'''
            <figure class="card">
              <img loading="lazy" src="{src}" alt="{theme} {size}" />
              <figcaption>
                <b>Theme {theme.upper()}</b> · {size}
                <a href="{src}" download>download</a>
              </figcaption>
            </figure>''')
    grid = "\n".join(cards)

    return f"""<!doctype html>
<html><head>
<meta charset="utf-8"/>
<title>DC Hub Daily — {date.isoformat()}</title>
<meta property="og:title" content="DC Hub Daily — {date.isoformat()}"/>
<meta property="og:image" content="{base}/{_r2_key(date, default_theme(date), 'square')}"/>
<meta name="twitter:card" content="summary_large_image"/>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; font-family: ui-sans-serif, system-ui, sans-serif;
         background:#0A1220; color:#E8F8FF; }}
  header {{ padding: 24px 32px; border-bottom: 1px solid #1c2840; }}
  header h1 {{ margin: 0; font-size: 22px; letter-spacing: 2px; }}
  header p  {{ margin: 6px 0 0; color:#7FD6EA; }}
  main {{ display:grid; gap:24px; padding:32px;
          grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
  figure {{ margin:0; border:1px solid #1c2840; border-radius:8px;
           overflow:hidden; background:#0b1424; }}
  figure img {{ width:100%; display:block; }}
  figcaption {{ padding:12px 14px; font-size:13px; display:flex;
                justify-content:space-between; align-items:center; }}
  figcaption a {{ color:#9EF3FF; text-decoration: none; }}
</style></head><body>
<header>
  <h1>DC HUB · DAILY</h1>
  <p>U.S. Data Center Hubs — {date.isoformat()}. 3 themes × 3 formats. Click any to download.</p>
</header>
<main>{grid}</main>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return today_page()
