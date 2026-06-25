#!/usr/bin/env python3
"""Martone Advisors Site Analysis — premium 5-page Site Study renderer (r-premium 2026-06-24).

The polished, branded, dark-theme study served by /api/v1/site-report?form=premium
(PRO-gated, same gate as the standard report). Renders the SAME survey dict S that
site_report._build_survey_data() produces, into the A1 "Site Analysis" template, then
the caller hands the HTML to routes.pdf_render.html_to_pdf (Gotenberg / chromium).

Ported from ~/Documents/tearsheet-factory/site_study_engine.py (the Phase-1 engine),
reworked for a headless backend:
  • the A1 <head> is INLINED (the on-disk template only exists on the author's Mac);
  • maps are ArcGIS basemap *URLs* with SVG pin/route overlays — no PIL, no fonts,
    no backend image egress (the URL is fetched by Gotenberg's chromium at render
    time, exactly like the standard report's site_map, which is proven to work);
  • the 5 emitted pages are built directly from S with defensive .get() so a missing
    field degrades to "—" instead of raising (the engine's rigid config would KeyError).

Public:  render_premium_html(S, lat=None, lon=None) -> str
"""
import html as _html
import math

# ════════════════════════════════════════════════════════════════════════════
#  A1 template <head> (inlined verbatim through </head>; __TITLE__ is replaced)
# ════════════════════════════════════════════════════════════════════════════
A1_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#0a0a0e;
    --panel:rgba(255,255,255,0.04);
    --panel2:rgba(255,255,255,0.065);
    --line:rgba(255,255,255,0.10);
    --txt:#eef1f6;
    --body:#c4ccda;
    --mute:#8b94a6;
    --dim:#5c6475;
    --cyan:#34d3ee;
    --gold:#e8c98a;
    --green:#34d399;
    --amber:#f5a623;
    --red:#fb7185;
    --mono: ui-monospace,'SF Mono','JetBrains Mono',Menlo,monospace;
    --sans:'Helvetica Neue',Inter,Arial,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  html,body{background:var(--bg);}
  body{font-family:var(--sans);color:var(--body);-webkit-print-color-adjust:exact;print-color-adjust:exact;-webkit-font-smoothing:antialiased;}
  @page{size:Letter;margin:0;}

  .page{position:relative;width:8.5in;height:11in;overflow:hidden;page-break-after:always;
    background:
      radial-gradient(120% 80% at 100% 0%, rgba(34,211,238,0.085), transparent 46%),
      radial-gradient(90% 70% at 0% 38%, rgba(139,92,246,0.06), transparent 52%),
      var(--bg);}
  .page:last-child{page-break-after:auto;}
  .inner{position:absolute;inset:0;padding:0.58in 0.62in 0.5in;display:flex;flex-direction:column;}

  .foot{margin-top:auto;display:flex;justify-content:space-between;align-items:center;gap:16px;
    padding-top:13px;border-top:1px solid var(--line);
    font-family:var(--mono);font-size:8px;letter-spacing:0.06em;color:var(--dim);}

  /* ---- cover ---- */
  .top{display:flex;justify-content:space-between;align-items:center;}
  .pill{font-family:var(--mono);font-size:8.5px;letter-spacing:0.14em;color:var(--mute);
    border:1px solid var(--line);border-radius:999px;padding:6px 14px;text-transform:uppercase;}
  .eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:0.26em;color:var(--cyan);text-transform:uppercase;font-weight:700;}
  h1{font-size:44px;line-height:1.05;font-weight:600;color:var(--txt);letter-spacing:-0.01em;}
  .coords{font-family:var(--mono);font-size:12px;color:var(--gold);margin-top:13px;letter-spacing:0.02em;}
  .lede{font-size:12.5px;line-height:1.62;color:var(--body);margin-top:14px;max-width:6.7in;}
  .lede b,.assess b,.bottomline p b,.dnote b,.hsub b,.hnote b,.mapcap b{color:var(--txt);font-weight:700;}
  .usecase{margin-top:18px;background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--cyan);
    border-radius:10px;padding:13px 16px;max-width:6.7in;}
  .ul{font-family:var(--mono);font-size:8.5px;letter-spacing:0.16em;text-transform:uppercase;color:var(--cyan);font-weight:700;margin-bottom:6px;}
  .usecase p{font-size:11px;line-height:1.55;color:var(--body);}
  .usecase p b{color:var(--txt);font-weight:700;}

  /* ---- grids ---- */
  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:13px;}
  .grid6{display:grid;grid-template-columns:repeat(6,1fr);gap:9px;}
  .cards2{display:grid;grid-template-columns:1fr 1fr;gap:13px;}

  /* ---- metric cards (cover) ---- */
  .mcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 15px;}
  .ml{font-family:var(--mono);font-size:8px;letter-spacing:0.13em;text-transform:uppercase;color:var(--mute);}
  .mv{font-size:27px;font-weight:700;color:var(--txt);margin-top:7px;line-height:1;}
  .mv small{font-size:11px;font-weight:600;color:var(--mute);margin-left:3px;}
  .ms{font-size:9.5px;color:var(--mute);margin-top:9px;display:flex;align-items:center;gap:6px;}
  .mdot{width:6px;height:6px;border-radius:50%;background:var(--green);display:inline-block;flex:none;}
  .ms.warn .mdot{background:var(--amber);} .ms.bad .mdot{background:var(--red);}

  /* ---- section header ---- */
  .seyebrow{font-family:var(--mono);font-size:10px;letter-spacing:0.22em;text-transform:uppercase;color:var(--cyan);font-weight:700;}
  .seyebrow b{color:var(--gold);margin-right:8px;}
  h2{font-size:23px;font-weight:700;color:var(--txt);margin-top:8px;display:flex;align-items:center;gap:9px;}
  .hr{height:1px;background:var(--line);margin:12px 0 16px;}

  /* ---- detail cards ---- */
  .dcard{background:var(--panel);border:1px solid var(--line);border-top:2.5px solid var(--cyan);border-radius:12px;padding:15px 16px;}
  .dcard.v{border-top-color:#38bdf8;} .dcard.c{border-top-color:#22d3ee;}
  .dcard.t{border-top-color:#2dd4bf;} .dcard.a{border-top-color:#f5a623;} .dcard.r{border-top-color:#fb7185;}
  .dl{font-family:var(--mono);font-size:8px;letter-spacing:0.12em;text-transform:uppercase;color:var(--mute);}
  .dt{font-size:21px;font-weight:700;color:var(--txt);margin-top:8px;line-height:1.06;}
  .dsub{font-size:10px;color:var(--mute);margin-top:5px;line-height:1.4;}
  .drow{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:10px;}
  .drow:last-of-type{border-bottom:none;}
  .drow .k{color:var(--mute);white-space:nowrap;}
  .drow .val{color:var(--body);text-align:right;font-weight:600;}
  .drow .val.tl{color:var(--green);} .drow .val.am{color:var(--amber);}
  .chips{display:flex;flex-wrap:wrap;gap:5px;margin:10px 0 4px;}
  .chip{font-family:var(--mono);font-size:7.5px;color:var(--mute);border:1px solid var(--line);border-radius:5px;padding:2px 6px;}
  .src{font-family:var(--mono);font-size:7.5px;color:var(--dim);margin-top:10px;}
  .src b{color:var(--mute);}

  .assess{font-size:10.5px;line-height:1.62;color:var(--body);margin-top:14px;}

  /* ---- headroom ---- */
  .headroom{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-top:14px;}
  .hh{font-size:13px;font-weight:700;color:var(--txt);display:flex;align-items:center;gap:8px;}
  .hsub{font-size:10px;color:var(--mute);margin-top:6px;line-height:1.5;}
  .hnote{font-size:9.5px;color:var(--mute);line-height:1.55;margin-top:11px;}

  /* ---- small status cards ---- */
  .scard{background:var(--panel2);border:1px solid var(--line);border-top:2.5px solid var(--cyan);border-radius:10px;padding:12px 13px;}
  .scard.a{border-top-color:#f5a623;} .scard.t{border-top-color:#2dd4bf;}
  .scard.c{border-top-color:#22d3ee;} .scard.v{border-top-color:#38bdf8;} .scard.r{border-top-color:#fb7185;}
  .sl{font-family:var(--mono);font-size:7.5px;letter-spacing:0.1em;text-transform:uppercase;color:var(--mute);}
  .sv{font-size:17px;font-weight:700;color:var(--txt);margin-top:6px;line-height:1.08;}
  .ss{font-size:9px;color:var(--mute);margin-top:5px;line-height:1.4;}

  /* ---- list cards ---- */
  .lcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
  .lh{font-family:var(--mono);font-size:8.5px;letter-spacing:0.12em;text-transform:uppercase;color:var(--cyan);font-weight:700;margin-bottom:9px;}
  .lcard ul{list-style:none;}
  .lcard li{font-size:10px;line-height:1.5;color:var(--body);padding:5px 0 5px 14px;position:relative;border-bottom:1px solid rgba(255,255,255,0.04);}
  .lcard li:last-child{border-bottom:none;}
  .lcard li:before{content:'';position:absolute;left:0;top:11px;width:5px;height:5px;border-radius:50%;background:var(--cyan);}
  .lcard li b{color:var(--txt);font-weight:700;}

  /* ---- maps ---- */
  .mapwrap{background:#0b0b10;border:1px solid var(--line);border-radius:12px;padding:10px;overflow:hidden;}
  .mapwrap img{width:100%;display:block;border-radius:8px;}
  .mapcap{font-family:var(--mono);font-size:8px;line-height:1.55;color:var(--mute);margin-top:9px;}

  /* ---- siting ---- */
  .legend{display:flex;flex-wrap:wrap;gap:16px;margin-top:12px;font-family:var(--mono);font-size:8.5px;color:var(--mute);}
  .legend span{display:flex;align-items:center;gap:6px;}
  .lsw{width:14px;height:4px;border-radius:2px;display:inline-block;}
  .bottomline{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--gold);border-radius:10px;padding:14px 16px;margin-top:16px;}
  .bl{font-family:var(--mono);font-size:8.5px;letter-spacing:0.16em;text-transform:uppercase;color:var(--gold);font-weight:700;margin-bottom:7px;}
  .bottomline p{font-size:10.5px;line-height:1.6;color:var(--body);}
  .disc{font-size:8px;line-height:1.5;color:var(--dim);margin-top:14px;}
  .disc b{color:var(--mute);}
</style>
</head>"""

# Extra rules the body markup references (mirrors the engine's CSS_EXTRA; injected
# in place of the A1 </style> so it lands inside the same <style> block).
CSS_EXTRA = """
  .mapwrap img.contain{object-fit:contain;background:#0b0b10;}
  .mapbox{position:relative;border-radius:8px;overflow:hidden;background:#0b0b10;}
  .mapbox svg{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;}
  .ndate{font-family:var(--mono);font-size:8px;color:var(--dim);margin-right:6px;}
  .lcard li i{color:var(--dim);font-style:normal;font-size:9.3px;}
  .brandrow{display:flex;flex-direction:column;gap:2px;}
  .wordmark{font-weight:800;font-size:22px;color:#fff;letter-spacing:-.02em;}
  .wordmark span{color:var(--cyan);}
  .preparedfor{font-family:var(--mono);font-size:8px;letter-spacing:0.14em;text-transform:uppercase;color:var(--gold);}
  .maplabel{font-family:var(--mono);font-size:8.5px;letter-spacing:0.13em;text-transform:uppercase;color:var(--cyan);font-weight:700;margin:0 0 7px;}
  .maplabel span{color:var(--mute);font-weight:600;}
  .ramprow{display:flex;align-items:center;gap:10px;margin:8px 0;}
  .rfy{font-family:var(--mono);font-size:10px;color:var(--mute);width:90px;font-weight:700;}
  .rbar{flex:1;height:21px;background:rgba(255,255,255,0.05);border-radius:6px;overflow:hidden;display:flex;}
  .rseg{height:100%;}
  .rseg.grid{background:linear-gradient(90deg,#22d3ee,#0ea5e9);}
  .rseg.gas{background:linear-gradient(90deg,#f59e0b,#f97316);}
  .rtot{font-weight:800;font-size:12.5px;width:78px;text-align:right;}
  .rlegend{display:flex;gap:18px;font-family:var(--mono);font-size:9px;color:var(--mute);margin-top:8px;}
  .rsw{display:inline-block;width:12px;height:7px;border-radius:2px;vertical-align:middle;margin-right:5px;}
  .dtable{width:100%;border-collapse:collapse;margin-top:6px;font-size:10px;}
  .dtable th{text-align:left;font-family:var(--mono);font-size:7.6px;letter-spacing:0.1em;text-transform:uppercase;color:var(--mute);border-bottom:1px solid rgba(255,255,255,0.13);padding:5px 8px;}
  .dtable td{padding:5px 8px;border-bottom:1px solid rgba(255,255,255,0.06);color:#cdd6e4;}
  .dtable td.m{font-family:var(--mono);}
</style>"""

CHIPS = ('<span class="chip">CO</span><span class="chip">GHG</span><span class="chip">NO&#8322;</span>'
         '<span class="chip">O&#8323;</span><span class="chip">PM10</span><span class="chip">PM2.5</span>'
         '<span class="chip">Pb</span><span class="chip">SO&#8322;</span>')

ARC_IMG = "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/export"
ARC_REF = "https://server.arcgisonline.com/arcgis/rest/services/World_Street_Map/MapServer/export"


# ════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ════════════════════════════════════════════════════════════════════════════
def _h(x):
    """HTML-escape a plain-text data value (&, <, >). None/'' -> em dash."""
    if x is None:
        return "&mdash;"
    s = str(x)
    if s == "" or s == "—":
        return "&mdash;"
    return _html.escape(s, quote=False)


def _dash(x):
    """Pass a value through, mapping the empty/sentinel cases to None."""
    if x is None:
        return None
    s = str(x).strip()
    return None if s in ("", "—", "None") else x


def _status(color):
    """Map a DC Hub score color (grn/amb/red/cy/vio/ind) -> mcard status class."""
    c = (color or "").lower()
    if c == "red":
        return "bad"
    if c == "amb":
        return "warn"
    return "ok"  # grn / cy / vio / ind / unknown -> default green dot


_ACCENT_FOR_VERDICT = {"grn": "t", "amb": "a", "red": "r"}


def _capnum(x):
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return None


def _short_target(label):
    return (str(label or "core").split(" (")[0].split(",")[0]).strip() or "core"


def _fmt_mi(d):
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    return "on-site" if d < 0.3 else (f"{d:.1f} mi" if d < 10 else f"{d:.0f} mi")


# ── card builders (lifted from site_study_engine; inputs are already HTML-safe) ──
def mcard(label, value, small, status, note):
    sm = f'<small> {small}</small>' if small else ''
    return (f'<div class="mcard"><div class="ml">{label}</div><div class="mv">{value}{sm}</div>'
            f'<div class="ms {status}"><span class="mdot"></span>{note}</div></div>')


def scard(accent, label, value, sub):
    return (f'<div class="scard {accent}"><div class="sl">{label}</div>'
            f'<div class="sv">{value}</div><div class="ss">{sub}</div></div>')


def dcard(d):
    rows = ''.join(
        f'<div class="drow"><span class="k">{k}</span>'
        f'<span class="val{" tl" if tl else ""}">{v}</span></div>'
        for k, v, tl in d.get('rows', []))
    chips = f'<div class="chips">{CHIPS}</div>' if d.get('chips') else ''
    return (f'<div class="dcard {d["accent"]}"><div class="dl">{d["label"]}</div>'
            f'<div class="dt">{d["title"]}</div><div class="dsub">{d["sub"]}</div>'
            f'{rows}{chips}<div class="src"><b>Source:</b> {d["src"]}</div></div>')


def _page(body, foot_l, foot_r):
    return (f'<section class="page"><div class="inner">{body}'
            f'<div class="foot"><span>{foot_l}</span><span>{foot_r}</span></div></div></section>')


def _head(title):
    return A1_HEAD.replace("</style>", CSS_EXTRA).replace("__TITLE__", _h(title))


# ════════════════════════════════════════════════════════════════════════════
#  Maps — ArcGIS basemap URLs + SVG pin/route overlays (fetched by chromium)
# ════════════════════════════════════════════════════════════════════════════
_MAP_W, _MAP_H = 1080, 400          # display aspect 2.7:1 (~6.4in x 2.37in)
_MAP_ASPECT = _MAP_W / _MAP_H


def _arc_url(base, bbox):
    return (f"{base}?bbox={bbox[0]:.5f},{bbox[1]:.5f},{bbox[2]:.5f},{bbox[3]:.5f}"
            f"&bboxSR=4326&imageSR=4326&size={_MAP_W},{_MAP_H}&format=png&f=image")


def _fit_aspect(bbox):
    """Expand a (lon0,lat0,lon1,lat1) bbox so its raw degree aspect == _MAP_ASPECT.
    ArcGIS 4326 export maps degrees linearly to pixels, so matching the degree
    aspect to the pixel aspect keeps the image undistorted and lets the linear
    _px() projection align the SVG overlay exactly."""
    lon0, lat0, lon1, lat1 = bbox
    dlon = max(lon1 - lon0, 1e-6)
    dlat = max(lat1 - lat0, 1e-6)
    cur = dlon / dlat
    if cur < _MAP_ASPECT:          # too tall -> widen longitude
        want = dlat * _MAP_ASPECT
        cx = (lon0 + lon1) / 2
        lon0, lon1 = cx - want / 2, cx + want / 2
    else:                          # too wide -> heighten latitude
        want = dlon / _MAP_ASPECT
        cy = (lat0 + lat1) / 2
        lat0, lat1 = cy - want / 2, cy + want / 2
    return (lon0, lat0, lon1, lat1)


def _px(lon, lat, bbox):
    x = (lon - bbox[0]) / (bbox[2] - bbox[0]) * _MAP_W
    y = (bbox[3] - lat) / (bbox[3] - bbox[1]) * _MAP_H
    return x, y


def _pin_svg(x, y, color, label=None, lab_fill="#fff", r=13):
    s = (f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r}" fill="{color}" stroke="#fff" stroke-width="3"/>'
         f'<circle cx="{x:.0f}" cy="{y:.0f}" r="3.5" fill="#fff"/>')
    if label:
        tw = 8.7 * len(label) + 16
        bx = x + r + 7
        s += (f'<rect x="{bx-5:.0f}" y="{y-19:.0f}" rx="4" width="{tw:.0f}" height="30" fill="rgba(0,0,0,0.62)"/>'
              f'<text x="{bx+3:.0f}" y="{y+1:.0f}" font-family="monospace" font-size="20" '
              f'font-weight="700" fill="{lab_fill}">{label}</text>')
    return s


def _mapwrap(url, overlay_svg, caption):
    return (f'<div class="mapwrap"><div class="mapbox">'
            f'<img src="{url}" alt="map">'
            f'<svg viewBox="0 0 {_MAP_W} {_MAP_H}" preserveAspectRatio="none">{overlay_svg}</svg>'
            f'</div><div class="mapcap">{caption}</div></div>')


def _aerial_map(power, lat, lon, caption):
    """Build-out aerial. Reuse the survey's site_map if present (a proven ArcGIS
    URL or an uploaded data URI); else a tight imagery export. Center 'Site' pin."""
    src = _dash(power.get("site_map"))
    if not src and lat is not None and lon is not None:
        bbox = _fit_aspect((lon - 0.0095, lat - 0.0065, lon + 0.0095, lat + 0.0065))
        src = _arc_url(ARC_IMG, bbox)
    overlay = _pin_svg(_MAP_W / 2, _MAP_H / 2, "#ff8a1f", "Site") if power.get("_site_map_pin", True) else ""
    return _mapwrap(src or "", overlay, caption)


def _fiber_map(fiber, lat, lon, caption):
    """Network fiber map. Uploaded fiber_map wins; else a wider imagery export of
    the corridor with a center 'Site' pin (carrier coords aren't in S -> named in
    the caption, never drawn as invented pins)."""
    src = _dash(fiber.get("fiber_map"))
    overlay = ""
    if not src and lat is not None and lon is not None:
        bbox = _fit_aspect((lon - 0.03, lat - 0.022, lon + 0.03, lat + 0.022))
        src = _arc_url(ARC_IMG, bbox)
        overlay = _pin_svg(_MAP_W / 2, _MAP_H / 2, "#22d3ee", "Site", lab_fill="#fff")
    return _mapwrap(src or "", overlay, caption)


def _latency_map(fiber, lat, lon, caption):
    """Network latency map. Uploaded latency_map wins; else a regional street
    basemap spanning site->hub with a great-circle route overlay (line + pins +
    ms label). Hub resolved from the stored latency_target label."""
    src = _dash(fiber.get("latency_map"))
    if src:
        return _mapwrap(src, "", caption)
    # Prefer the explicit target coords the gatherer resolved (the nearest carrier
    # hotel); fall back to the metro-hub lookup only if they're absent.
    hub = None
    _tlat, _tlon = fiber.get("latency_target_lat"), fiber.get("latency_target_lng")
    if _tlat is not None and _tlon is not None:
        try:
            hub = (float(_tlat), float(_tlon),
                   _short_target(fiber.get("latency_target_name") or fiber.get("latency_target")))
        except (TypeError, ValueError):
            hub = None
    if not hub:
        hub = _hub_for(fiber.get("latency_target"))
    if lat is None or lon is None or not hub:
        # No coords/hub: a wider street basemap centered on the site, site pin only.
        if lat is None or lon is None:
            return _mapwrap("", "", caption)
        bbox = _fit_aspect((lon - 0.5, lat - 0.37, lon + 0.5, lat + 0.37))
        url = _arc_url(ARC_REF, bbox)
        return _mapwrap(url, _pin_svg(_MAP_W / 2, _MAP_H / 2, "#ff8a1f", "Site"), caption)
    hlat, hlon, hlabel = hub
    # Scale padding to the site↔hotel separation so a CLOSE carrier hotel (e.g.
    # Ashburn's is ~1.6 mi) zooms in enough to separate the two pins, while a
    # distant one (rural → metro hotel) still shows the full route.
    pad_x = abs(hlon - lon) * 0.45 + 0.03
    pad_y = abs(hlat - lat) * 0.45 + 0.03
    bbox = _fit_aspect((min(lon, hlon) - pad_x, min(lat, hlat) - pad_y,
                        max(lon, hlon) + pad_x, max(lat, hlat) + pad_y))
    url = _arc_url(ARC_REF, bbox)
    sx, sy = _px(lon, lat, bbox)
    hx, hy = _px(hlon, hlat, bbox)
    ms = _dash(fiber.get("latency_ms"))
    mid_x, mid_y = (sx + hx) / 2, (sy + hy) / 2
    cap_txt = (f"{ms} ms" if ms else "fiber")
    tw = 9 * len(cap_txt) + 18
    overlay = (
        f'<line x1="{sx:.0f}" y1="{sy:.0f}" x2="{hx:.0f}" y2="{hy:.0f}" '
        f'stroke="#7c3aed" stroke-width="7" stroke-linecap="round" opacity="0.92"/>'
        f'<rect x="{mid_x-tw/2:.0f}" y="{mid_y-17:.0f}" rx="5" width="{tw:.0f}" height="28" fill="rgba(0,0,0,0.72)"/>'
        f'<text x="{mid_x:.0f}" y="{mid_y+2:.0f}" text-anchor="middle" font-family="monospace" '
        f'font-size="19" font-weight="700" fill="#fff">{cap_txt}</text>'
        + _pin_svg(hx, hy, "#0ea5e9", (str(hlabel or "hub"))[:20])
        + _pin_svg(sx, sy, "#ff8a1f", "Site"))
    return _mapwrap(url, overlay, caption)


def _hub_for(label):
    """Resolve a latency-target label back to (lat, lon, label) hub coords."""
    try:
        from routes.site_report import LATENCY_TARGETS, DEFAULT_TARGET
    except Exception:
        return None
    if label:
        for _k, (la, lo, lab) in LATENCY_TARGETS.items():
            if lab == label:
                return (la, lo, lab)
        base = str(label).split(" (")[0].strip().lower()
        for k, (la, lo, lab) in LATENCY_TARGETS.items():
            if k == base or lab.lower().startswith(base):
                return (la, lo, lab)
    d = LATENCY_TARGETS.get(DEFAULT_TARGET)
    return (d[0], d[1], d[2]) if d else None


# ════════════════════════════════════════════════════════════════════════════
#  Public entry — render the 5-page premium study from the survey dict S
# ════════════════════════════════════════════════════════════════════════════
def render_premium_html(S, lat=None, lon=None):
    S = S or {}
    power = S.get("power") or {}
    gas = S.get("gas") or {}
    air = S.get("air") or {}
    fiber = S.get("fiber") or {}        # merged fiber + latency
    market = S.get("market") or {}
    energy = S.get("energy") or {}
    meta = S.get("_meta") or {}
    verdict = S.get("verdict") or {}
    summary = S.get("summary") or {}

    cap = _capnum(meta.get("capacity_mw")) or 100
    iso = _dash(meta.get("iso")) or _dash(power.get("iso")) or "—"
    iso_h = _h(iso) if iso != "—" else "&mdash;"
    site_name = _dash(S.get("site_name")) or "Candidate Site"
    location = _dash(S.get("location")) or "United States"
    coords = _dash(S.get("coords")) or "—"
    date = _dash(S.get("date")) or ""
    client = (S.get("prepared_for") or "").strip()
    preparer = (S.get("prepared_by") or "DC Hub").strip() or "DC Hub"
    branded = preparer.lower().replace("·", "").replace(" ", "") not in ("dchub",)  # non-DC-Hub preparer -> top billing
    use_case = _dash(S.get("use_case")) or ("Data-center / AI-infrastructure site "
                                            "evaluation (power, gas, water, air, fiber, latency).")
    bottom = _dash((summary or {}).get("bottom_line")) or ""
    ind = energy.get("industrial_cents_kwh")
    cost_html = (f"{ind}&cent;/kWh" if ind not in (None, "") else "&mdash;")
    cost_num_html = (f"{ind}" if ind not in (None, "") else "&mdash;")
    mname_h = _h(_dash(market.get("name")) or location)
    short_t = _short_target(fiber.get("latency_target_name") or fiber.get("latency_target"))
    lat_ms = _dash(fiber.get("latency_ms"))

    def ttp_str():
        v = market.get("time_to_power_months")
        try:
            return f"~{int(float(v))} mo"
        except (TypeError, ValueError):
            return "Load study"

    if branded:
        foot_brand = (f"Prepared by {_h(preparer)}" + (f" for {_h(client)}" if client else "")
                      + " &middot; DC Hub data-center &amp; energy intelligence &middot; dchub.cloud")
    else:
        foot_brand = ("Prepared by DC Hub" + (f" for {_h(client)}" if client else "")
                      + " &middot; data-center &amp; energy intelligence &middot; dchub.cloud")
    short = f"{_h(location)} &middot; {_h(coords)}"

    # ── Page 1 · COVER ──────────────────────────────────────────────────────
    cover_cards = "".join(
        mcard(_h(sc.get("label")), _h(_dash(sc.get("value")) or "—"),
              _html.escape((sc.get("unit") or "").strip(), quote=False),  # empty unit stays empty (not em-dash)
              _status(sc.get("color")), _h(_dash(sc.get("sub")) or "—"))
        for sc in (S.get("scorecards") or [])[:6])
    if branded:
        brand = (f'<div class="wordmark">{_h(preparer)}</div>'
                 f'<div class="preparedfor">Powered by DC Hub &middot; dchub.cloud</div>')
    else:
        brand = '<div class="wordmark">DC<span>&middot;Hub</span></div>'
    if client:
        brand += f'<div class="preparedfor">Prepared for {_h(client)}</div>'
    h1 = f"{_h(site_name)}<br>{cap} MW Site"
    p1 = _page(f"""
  <div class="top">
    <div class="brandrow">{brand}</div>
    <div class="pill">Site Analysis &middot; {_h(date)}</div>
  </div>
  <div style="margin-top:1.05in;">
    <div class="eyebrow">Power &middot; Economics &middot; Air &middot; Fiber &middot; Latency &middot; Land</div>
    <h1 style="margin-top:14px;">{h1}</h1>
    <div class="coords">&#8859; {_h(coords)} &middot; {_h(location)}</div>
    <p class="lede">{_h(bottom)}</p>
    <div class="usecase"><div class="ul">Intended Use Case</div><p>{_h(use_case)}</p></div>
  </div>
  <div class="grid3" style="margin-top:20px;">{cover_cards}</div>""", foot_brand, short)

    # ── Page 2 · CAMPUS BUILD-OUT (aerial + capacity + delivery) ─────────────
    aerial_cap = _h(_dash(power.get("site_map_caption")) or
                    "DC Hub satellite view — the candidate parcel and immediate surroundings (imagery: Esri).")
    aerial = _aerial_map(power, lat, lon, aerial_cap)
    grid_pct = 100.0
    bars = (f'<div class="ramprow"><div class="rfy">Target</div><div class="rbar">'
            f'<div class="rseg grid" style="width:{grid_pct:.0f}%"></div></div>'
            f'<div class="rtot">{cap:,} MW</div></div>')
    captbl = f'<tr><td class="m">Target</td><td class="m">{cap:,}</td><td class="m">{cap:,}</td><td class="m">0</td></tr>'
    sub_note = _dash(power.get("substation_note")) or "—"
    volt = _dash(power.get("voltage")) or "—"
    delrows = "".join(
        f'<tr><td>{ph}</td><td class="m">{dt}</td><td class="m">{u}</td><td>{note}</td></tr>'
        for ph, dt, u, note in [
            ("Grid tie", "&mdash;", _h(volt), _h(sub_note)),
            ("Headroom", "Load study", "&mdash;",
             "Per-substation transfer headroom is not published &mdash; utility load study required."),
            ("Expansion", "Study", "&mdash;",
             f"Confirm additional capacity and tariff with {iso_h} / the interconnecting utility."),
        ])
    parcel_label = (f"{_h(site_name)} &middot; {cap} MW &middot; {_h(volt)} / {iso_h}")
    ramp_assess = _h(_dash(power.get("assessment")) or
                     "Capacity is a screening target; a load study with the interconnecting utility confirms deliverable MW.")
    p2 = _page(f"""
  <div class="seyebrow"><b>01</b> CAMPUS BUILD-OUT &middot; SITE AERIAL &amp; POWER</div>
  <h2>&#127959; Site Aerial &amp; Power Delivery</h2>
  <div class="hr"></div>
  <div class="maplabel">Parcel Aerial &middot; <span>{parcel_label}</span></div>
  {aerial}
  <div class="cards2" style="margin-top:14px;">
    <div class="lcard"><div class="lh">Capacity (screening target)</div>
      <div>{bars}</div>
      <div class="rlegend"><span><span class="rsw" style="background:#22d3ee;"></span>Target capacity</span></div>
      <table class="dtable"><tr><th>Stage</th><th>Total MW</th><th>Grid MW</th><th>Gas MW</th></tr>{captbl}</table>
    </div>
    <div class="lcard"><div class="lh">Power Delivery &amp; Diligence</div>
      <table class="dtable"><tr><th>Item</th><th>Status</th><th>kV</th><th>Note</th></tr>{delrows}</table>
      <div class="hnote" style="margin-top:10px;color:var(--mute);font-size:9.5px;line-height:1.55;">
        Single-stage target shown ({cap} MW). Staging and timing require a load study; DC Hub does not publish
        per-substation transfer headroom &mdash; treat capacity as a screening target, not commissioned capacity.</div>
    </div>
  </div>
  <p class="assess"><b>Power basis.</b> {ramp_assess}</p>""", "DC Hub &middot; Site Analysis", short)

    # ── Page 3 · INFRASTRUCTURE (power / economics / air / fiber) ────────────
    power_dc = dict(accent="v", label="Power &middot; Transmission", title=_h(volt),
                    sub=_h(sub_note),
                    rows=[["Substation", _h(_dash(power.get("substation")) or "—"), False],
                          ["Voltage", _h(volt), True],
                          ["Operator", _h(_dash(power.get("operator")) or "—"), False],
                          ["ISO / region", iso_h, False]],
                    src=_h(_dash(power.get("substation_source")) or "DC Hub grid layer"))
    gas_dist = _fmt_mi(gas.get("_dist"))
    gas_dc = dict(accent="c", label="Gas &middot; Power Economics", title=cost_html,
                  sub=(_h(_dash(gas.get("type")) or "Natural gas")
                       + ((" &middot; " + _h(_dash(gas.get("status")))) if _dash(gas.get("status")) else "")),
                  rows=[["Nearest pipeline", _h(_dash(gas.get("name")) or "—"), False],
                        ["Distance", _h(gas_dist or "—"), False],
                        ["Operator", _h(_dash(gas.get("operator")) or "—"), False],
                        ["Industrial power", cost_html, True]],
                  src=_h(_dash(gas.get("source")) or "DC Hub gas layer &middot; EIA"))
    air_pollutants = ", ".join(air.get("pollutants") or [])
    air_dc = dict(accent="t", label="Air &amp; Permitting",
                  title=(_h(_dash(air.get("risk")) or "—") + " &middot; " + _h(_dash(air.get("pathway")) or "—")),
                  sub=_h(_dash(air.get("risk_note")) or "Grid power is zero on-site emissions."),
                  rows=[["Pathway", _h(_dash(air.get("pathway")) or "—"), False],
                        ["Offset estimate", _h(_dash(air.get("offset")) or "—"), False],
                        ["Pollutants flagged", _h(air_pollutants or "—"), False],
                        [_h(air.get("context_label") or "Context"),
                         _h(_dash(air.get("context")) or "—"), False]],
                  chips=True, src=_h(_dash(air.get("sources")) or "EPA Green Book &middot; AQS &middot; NEI"))
    fcount = fiber.get("_count", 0) or 0
    fiber_dc = dict(accent="c", label="Fiber &amp; Connectivity",
                    title=_h(f"{fcount} carrier" + ("s" if fcount != 1 else "")),
                    sub=_h(_dash(fiber.get("adjacent")) or "see fiber-locator"),
                    rows=[["Adjacent (<5 mi)", _h(_dash(fiber.get("adjacent")) or "—"), False],
                          ["Nearby (<50 mi)", _h(_dash(fiber.get("nearby")) or "—"), False],
                          ["Latency", (_h(lat_ms or "—") + " ms &rarr; " + _h(short_t)), True]],
                    src=_h(_dash(fiber.get("source")) or "DC Hub fiber routes"))
    dcards = "".join(dcard(d) for d in (power_dc, gas_dc, air_dc, fiber_dc))
    assess2 = (_h(_dash(power.get("assessment")) or "") + " " + _h(_dash(gas.get("assessment")) or "")).strip() or "&mdash;"
    hcards = "".join(scard(*x) for x in [
        ("c", _h(volt) + " tie", _h(_fmt_mi(power.get("_dist")) or "adjacent"),
         _h(_dash(power.get("operator")) or "grid")),
        ("v", "Power cost", cost_html, "industrial &middot; EIA"),
        ("t", "Time-to-power", _h(ttp_str()), mname_h),
    ])
    p3 = _page(f"""
  <div class="seyebrow"><b>02</b> POWER &middot; ECONOMICS &middot; AIR &middot; CONNECTIVITY</div>
  <h2>&#9889; Infrastructure Intelligence</h2>
  <div class="hr"></div>
  <div class="cards2">{dcards}</div>
  <p class="assess"><b>Assessment.</b> {assess2}</p>
  <div class="headroom">
    <div class="hh">&#9889; Grid Headroom &middot; {iso_h}</div>
    <div class="hsub">DC Hub does not publish per-substation transfer headroom &mdash; a load study with the
      interconnecting utility is the critical-path item to confirm campus-scale capacity.</div>
    <div class="grid3" style="margin-top:11px;">{hcards}</div>
    <div class="hnote">Capacity, cost and timing are screening figures &mdash; confirm transfer capacity, tariff
      and queue position with the utility, ISO and regulator before commitment.</div>
  </div>""", "DC Hub &middot; Site Analysis", short)

    # ── Page 4 · MARKET INTELLIGENCE (DCPI / DCGI) ──────────────────────────
    mkt_cards = "".join([
        mcard("Grid", iso_h, "interconnect", _status("ind"), mname_h),
        mcard("Power cost", cost_num_html, "&cent;/kWh", "warn", "industrial &middot; EIA"),
        mcard("Latency", _h(lat_ms or "—"), "ms", "ok", "&rarr; " + _h(short_t)),
    ])
    dcgi_card = dict(accent="c", label="DCGI &middot; Grid Intelligence",
                     title=iso_h + " interconnected", sub=mname_h,
                     rows=[["ISO / region", iso_h, False],
                           ["Time-to-power", _h(ttp_str()), True],
                           ["Queue wait", _qmonths(market.get("queue_wait_months")), False],
                           ["Reserve margin", _pct(market.get("reserve_margin_pct")), False]],
                     src="DC Hub DCGI &middot; live")
    v_acc = _ACCENT_FOR_VERDICT.get(market.get("verdict_color"), "c")
    dcpi_card = dict(accent=v_acc, label="DCPI &middot; Power Index",
                     title=_h(_dash(market.get("verdict")) or "—"), sub="Composite screening verdict",
                     rows=[["Verdict", _h(_dash(market.get("verdict")) or "—"), True],
                           ["Excess-power score", _h(_dash(market.get("excess_power_score")) or "—"), False],
                           ["Constraint score", _h(_dash(market.get("constraint_score")) or "—"), False],
                           ["Distance to market", _qmiles(market.get("distance_mi")), False]],
                     src="DC Hub DCPI &middot; live")
    why = []
    if _dash(market.get("verdict")):
        why.append(f"DCPI verdict <b>{_h(market['verdict'])}</b> for {mname_h}.")
    if market.get("time_to_power_months") is not None:
        why.append(f"Time-to-power <b>{_h(ttp_str())}</b> in-market (DCGI).")
    if ind not in (None, ""):
        why.append(f"Industrial power <b>{ind}&cent;/kWh</b> (EIA).")
    if iso != "—":
        why.append(f"Interconnects via <b>{iso_h}</b>.")
    if lat_ms:
        why.append(f"<b>{_h(lat_ms)} ms</b> to {_h(short_t)}.")
    if not why:
        why.append("Screening-level market read &mdash; confirm demand and queue depth in-market.")
    why_html = "".join(f"<li>{x}</li>" for x in why)
    signals = (verdict.get("reasons") or [])[:5]
    sig_html = "".join(f"<li>{_h(s)}</li>" for s in signals) or "<li>No screening-level blockers surfaced.</li>"
    assess4 = _h(bottom) or "&mdash;"
    p4 = _page(f"""
  <div class="seyebrow"><b>03</b> MARKET INTELLIGENCE &middot; DCPI / DCGI</div>
  <h2>&#128202; Market Intelligence &mdash; {mname_h}</h2>
  <div class="hr"></div>
  <div class="grid3">{mkt_cards}</div>
  <div class="cards2" style="margin-top:13px;">{dcard(dcgi_card)}{dcard(dcpi_card)}</div>
  <div class="cards2" style="margin-top:13px;">
    <div class="lcard"><div class="lh">DC Hub Market Read</div><ul>{why_html}</ul></div>
    <div class="lcard"><div class="lh">What It Means For This Site</div><ul>{sig_html}</ul></div>
  </div>
  <p class="assess"><b>Market read.</b> {assess4}</p>""", "DC Hub &middot; DCPI / DCGI &middot; dchub.cloud", short)

    # ── Page 5 · NETWORK (fiber routes & latency) ───────────────────────────
    fiber_cap = _h(_dash(fiber.get("fiber_map_caption")) or _dash(fiber.get("assessment")) or
                   "Carrier fiber serving the site (DC Hub fiber routes). Confirm entrance-facility "
                   "distances and dark-fiber availability with a fiber-locator export.")
    lat_cap = _h(_dash(fiber.get("latency_map_caption")) or _dash(fiber.get("latency_note")) or
                 "Estimated round-trip latency over the regional fiber mesh; a measured fiber-locator route supersedes this estimate.")
    fiber_map = _fiber_map(fiber, lat, lon, fiber_cap)
    latency_map = _latency_map(fiber, lat, lon, lat_cap)
    net_cards = "".join(scard(*x) for x in [
        ("c", "Carriers", _h(str(fcount)), _h(_dash(fiber.get("adjacent")) or "see fiber-locator")),
        ("t", "Latency &rarr; " + _h(short_t), (_h(lat_ms or "—") + " ms"),
         _h(_dash(fiber.get("latency_distance")) or "fiber route")),
        ("v", "Transport", "Fiber (est.)", _h(_dash(fiber.get("latency_transport")) or "fiber &middot; est. RTT")),
    ])
    p5 = _page(f"""
  <div class="seyebrow"><b>03</b> NETWORK &middot; FIBER ROUTES &amp; LATENCY</div>
  <h2>&#127760; {_h(site_name)} &mdash; Fiber &amp; Latency</h2>
  <div class="hr"></div>
  <div class="maplabel">Fiber Network &middot; <span>{_h(_dash(fiber.get("adjacent")) or _dash(fiber.get("nearby")) or "carrier routes")}</span></div>
  {fiber_map}
  <div class="maplabel" style="margin-top:13px;">Latency Route &middot; <span>{_h(lat_ms or "—")} ms &rarr; {_h(short_t)}</span></div>
  {latency_map}
  <div class="grid3" style="margin-top:13px;">{net_cards}</div>""", "DC Hub &middot; Fiber Routes &amp; Latency &middot; dchub.cloud", short)

    title = f"{site_name} — Site Analysis · {preparer}"
    return _head(title) + "\n<body>\n" + "\n".join([p1, p2, p3, p4, p5]) + "\n</body>\n</html>\n"


def _qmonths(v):
    try:
        return f"{int(float(v))} mo"
    except (TypeError, ValueError):
        return "&mdash;"


def _pct(v):
    try:
        return f"{float(v):g}%"
    except (TypeError, ValueError):
        return "&mdash;"


def _qmiles(v):
    try:
        return f"{int(round(float(v)))} mi"
    except (TypeError, ValueError):
        return "&mdash;"
