"""DC Hub Daily — image renderer.

Three themes × three share formats = 9 variants per day.

Themes:  a = Mainframe Glow   (homage to Visual Capitalist)
         b = Editorial Neon   (premium magazine, KPI tiles)
         c = Terminal CRT     (phosphor green on black)

Sizes:   portrait  1200x1800  (all 50 states + DC)
         square   1200x1200  (top 25 states by total)
         story    1080x1920  (top 20 states, vertical 9:16)

Usage:
    python render.py --theme a --size portrait --data data.json --out out.png
    # or import:
    from render import render
    img = render(theme="a", size="square", data=payload)  # returns PIL.Image
"""
from __future__ import annotations
import argparse
import datetime
import io
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle
from matplotlib.patheffects import withStroke
import numpy as np
from PIL import Image, ImageDraw

Theme = Literal["a", "b", "c", "d"]
Size = Literal["portrait", "square", "story"]

# --- palettes ---------------------------------------------------------------
PAL = {
    "a": {"bg": "#0A1220", "op": "#6FE66A", "uc": "#4CC4F3", "ann": "#C96BF0",
          "ink": "#E8F8FF", "dim": "#7FD6EA", "accent": "#1FF0B0"},
    "b": {"bg": "#0B0F1A", "op": "#6FE6A9", "uc": "#4CC4F3", "ann": "#E17CFF",
          "ink": "#F6FBFF", "dim": "#7A8FA6", "accent": "#9EF3FF"},
    "c": {"bg": "#02060A", "op": "#39FF6A", "uc": "#4CE0FF", "ann": "#FF5EDC",
          "ink": "#C9FFD6", "dim": "#77D99A", "accent": "#39FF6A"},
    "d": {"bg": "#0A0E1C", "op": "#5B8FFF", "uc": "#8B7FFF", "ann": "#C4A0FF",
          "ink": "#E8ECF7", "dim": "#8B92A8", "accent": "#4F8FFF",
          "card_bg": "#121629"},
}

SIZES = {
    "portrait": (1200, 1800, 50),   # 50 (or all) states
    "square":   (1200, 1200, 25),
    "story":    (1080, 1920, 20),
}


# --- data helpers -----------------------------------------------------------

@dataclass
class RenderData:
    states: list[dict]        # [{name, op, uc, ann}]
    as_of: str
    source: str
    generated: str
    title: str = "U.S. DATA CENTER HUBS OF THE FUTURE"
    subtitle: str = "by status — operational, under construction, announced"
    callouts: list[dict] | None = None   # [{state, color_key, text}]


def load_data(path: str | Path) -> RenderData:
    payload = json.loads(Path(path).read_text())
    return RenderData(
        states=payload["states"],
        as_of=payload.get("as_of", ""),
        source=payload.get("source", ""),
        generated=datetime.date.today().isoformat(),
        callouts=payload.get("callouts"),
    )


def top_n(states: list[dict], n: int) -> list[dict]:
    return sorted(states, key=lambda s: s["op"] + s["uc"] + s["ann"], reverse=True)[:n]


# --- radial-glow background (style A) --------------------------------------

def _radial_bg(w: int, h: int, inner: str, outer: str) -> Image.Image:
    ic = mpl.colors.to_rgb(inner)
    oc = mpl.colors.to_rgb(outer)
    xs = np.arange(w)
    ys = np.arange(h)[:, None]
    cx, cy = w * 0.15, h * 0.28
    maxd = math.hypot(w, h)
    d = np.hypot(xs - cx, ys - cy) / maxd
    t = np.clip(d * 1.35, 0, 1)
    r = (ic[0] * (1 - t) + oc[0] * t) * 255
    g = (ic[1] * (1 - t) + oc[1] * t) * 255
    b = (ic[2] * (1 - t) + oc[2] * t) * 255
    arr = np.dstack([r, g, b]).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")
    # right-side vignette
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    for i in range(70):
        a = int(120 * (i / 70))
        od.rectangle([w - (i * 4), 0, w, h], fill=(5, 5, 20, a))
    return Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")


# --- core bar plotter -------------------------------------------------------

def _bars(ax, rows: list[dict], pal: dict, label_fontsize: float, num_fontsize: float,
          right_pad_frac: float = 0.14, inline_numbers: bool = True,
          summary_labels: bool = False):
    names = [s["name"] for s in rows]
    op  = np.array([s["op"]  for s in rows])
    uc  = np.array([s["uc"]  for s in rows])
    ann = np.array([s["ann"] for s in rows])
    y = np.arange(len(names))[::-1]

    ax.barh(y, op,  color=pal["op"],  edgecolor="none", height=0.72, zorder=3)
    ax.barh(y, uc,  left=op,       color=pal["uc"],  edgecolor="none", height=0.72, zorder=3)
    ax.barh(y, ann, left=op + uc,  color=pal["ann"], edgecolor="none", height=0.72, zorder=3)

    totals = op + uc + ann
    max_total = max(totals.max(), 1)
    ax.set_xlim(0, max_total * (1 + right_pad_frac))

    if inline_numbers:
        for i, (o, u, a, t) in enumerate(zip(op, uc, ann, totals)):
            yy = y[i]
            seg_min = 0.06 * max_total
            if o >= seg_min:
                ax.text(o / 2, yy, str(o), color="#082010", fontsize=num_fontsize,
                        ha="center", va="center", family="monospace", weight="bold", zorder=4)
            if u >= seg_min:
                ax.text(o + u / 2, yy, str(u), color="#062838", fontsize=num_fontsize,
                        ha="center", va="center", family="monospace", weight="bold", zorder=4)
            if a >= seg_min:
                ax.text(o + u + a / 2, yy, str(a), color="#1f0830", fontsize=num_fontsize,
                        ha="center", va="center", family="monospace", weight="bold", zorder=4)

            if summary_labels:
                ax.text(t + max_total * 0.01, yy,
                        f"{o} {u} {a}" if t < 0.10 * max_total else f"{t:,}",
                        color=pal["dim"], fontsize=num_fontsize,
                        va="center", family="monospace")
            else:
                if t < 0.10 * max_total:
                    ax.text(t + max_total * 0.01, yy, f"{o} {u} {a}",
                            color=pal["ink"], fontsize=num_fontsize,
                            va="center", family="monospace")

    ax.set_yticks(y)
    ax.set_yticklabels(names, color=pal["ink"], family="monospace", fontsize=label_fontsize)
    ax.tick_params(left=False)
    ax.set_xticks([])
    for s in ax.spines.values():
        s.set_visible(False)


# --- theme A ----------------------------------------------------------------

def _render_a(data: RenderData, size: Size) -> Image.Image:
    W, H, N = SIZES[size]
    pal = PAL["a"]
    rows = top_n(data.states, N)

    bg = _radial_bg(W, H, "#123556", "#2A0E3E")

    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    fig.patch.set_alpha(0)
    ax_bg = fig.add_axes([0, 0, 1, 1])
    ax_bg.imshow(np.array(bg), extent=[0, 1, 0, 1], aspect="auto", zorder=-5)
    ax_bg.axis("off")

    # ---- layout depends on size ----
    if size == "portrait":
        # title on right half, bars on left half
        ax_t = fig.add_axes([0.52, 0.80, 0.45, 0.16]); ax_t.axis("off")
        ax_t.set_xlim(0, 1); ax_t.set_ylim(0, 1)
        ax_t.text(0, 0.72, "U.S. DATA",   fontsize=56, color=pal["ink"], family="monospace", weight="bold",
                  path_effects=[withStroke(linewidth=4, foreground=pal["accent"], alpha=0.35)])
        ax_t.text(0, 0.37, "CENTER HUBS", fontsize=56, color=pal["ink"], family="monospace", weight="bold",
                  path_effects=[withStroke(linewidth=4, foreground=pal["accent"], alpha=0.35)])
        ax_t.text(0, 0.04, "OF THE FUTURE", fontsize=34, color=pal["accent"], family="monospace", weight="bold")

        ax_sub = fig.add_axes([0.52, 0.74, 0.45, 0.05]); ax_sub.axis("off")
        ax_sub.set_xlim(0, 1); ax_sub.set_ylim(0, 1)
        ax_sub.text(0, 0.5, f"DAILY BRIEF · {data.generated.upper()}",
                    fontsize=13, color=pal["dim"], family="monospace", weight="bold", va="center")

        # legend
        ax_l = fig.add_axes([0.52, 0.63, 0.45, 0.08]); ax_l.axis("off")
        ax_l.set_xlim(0, 1); ax_l.set_ylim(0, 1)
        for i, (lbl, k) in enumerate([("OPERATIONAL", "op"), ("UNDER CONSTRUCTION", "uc"), ("ANNOUNCED", "ann")]):
            y = 0.82 - i * 0.33
            ax_l.add_patch(Rectangle((0, y - 0.1), 0.07, 0.2, color=pal[k]))
            ax_l.text(0.10, y, lbl, color=pal["ink"], fontsize=11, family="monospace",
                      weight="bold", va="center")

        # callouts
        ax_c = fig.add_axes([0.52, 0.10, 0.45, 0.48]); ax_c.axis("off")
        ax_c.set_xlim(0, 1); ax_c.set_ylim(0, 1)
        callouts = data.callouts or _default_callouts()
        step = 1.0 / max(len(callouts), 1)
        for i, co in enumerate(callouts):
            top = 0.98 - i * step
            ax_c.text(0, top, co["state"], color=pal[co.get("color_key", "op")],
                      fontsize=15, family="monospace", weight="bold")
            for j, line in enumerate(_wrap(co["text"], 36)):
                ax_c.text(0, top - 0.06 - j * 0.045, line, color=pal["ink"],
                          fontsize=11, family="monospace", va="top")

        ax = fig.add_axes([0.05, 0.04, 0.44, 0.92])
        _bars(ax, rows, pal, label_fontsize=8.5, num_fontsize=7.5, right_pad_frac=0.08)
    elif size == "square":
        ax_t = fig.add_axes([0.05, 0.88, 0.9, 0.11]); ax_t.axis("off")
        ax_t.set_xlim(0, 1); ax_t.set_ylim(0, 1)
        ax_t.text(0, 0.62, "U.S. DATA CENTER HUBS", fontsize=44, color=pal["ink"],
                  family="monospace", weight="bold",
                  path_effects=[withStroke(linewidth=3, foreground=pal["accent"], alpha=0.3)])
        ax_t.text(0, 0.22, f"OF THE FUTURE · TOP 25 STATES · {data.generated}",
                  fontsize=14, color=pal["dim"], family="monospace", weight="bold")
        # legend row
        ax_l = fig.add_axes([0.05, 0.84, 0.9, 0.035]); ax_l.axis("off")
        ax_l.set_xlim(0, 1); ax_l.set_ylim(0, 1)
        for i, (lbl, k) in enumerate([("OPERATIONAL", "op"), ("UNDER CONSTRUCTION", "uc"), ("ANNOUNCED", "ann")]):
            x = i * 0.32
            ax_l.add_patch(Rectangle((x, 0.35), 0.025, 0.5, color=pal[k]))
            ax_l.text(x + 0.035, 0.6, lbl, color=pal["ink"], fontsize=12, family="monospace",
                      weight="bold", va="center")
        ax = fig.add_axes([0.17, 0.05, 0.80, 0.78])
        _bars(ax, rows, pal, label_fontsize=10.5, num_fontsize=9, right_pad_frac=0.10)
    else:  # story
        ax_t = fig.add_axes([0.05, 0.89, 0.9, 0.10]); ax_t.axis("off")
        ax_t.set_xlim(0, 1); ax_t.set_ylim(0, 1)
        ax_t.text(0, 0.72, "U.S. DATA",    fontsize=62, color=pal["ink"], family="monospace", weight="bold",
                  path_effects=[withStroke(linewidth=4, foreground=pal["accent"], alpha=0.3)])
        ax_t.text(0, 0.30, "CENTER HUBS",  fontsize=62, color=pal["ink"], family="monospace", weight="bold",
                  path_effects=[withStroke(linewidth=4, foreground=pal["accent"], alpha=0.3)])
        ax_t.text(0, 0.02, f"OF THE FUTURE  ·  {data.generated}", fontsize=18, color=pal["accent"],
                  family="monospace", weight="bold")
        ax_l = fig.add_axes([0.05, 0.84, 0.9, 0.04]); ax_l.axis("off")
        ax_l.set_xlim(0, 1); ax_l.set_ylim(0, 1)
        for i, (lbl, k) in enumerate([("OPERATIONAL", "op"), ("UNDER CONSTRUCTION", "uc"), ("ANNOUNCED", "ann")]):
            x = i * 0.34
            ax_l.add_patch(Rectangle((x, 0.3), 0.022, 0.5, color=pal[k]))
            ax_l.text(x + 0.03, 0.55, lbl, color=pal["ink"], fontsize=12, family="monospace",
                      weight="bold", va="center")
        ax = fig.add_axes([0.18, 0.04, 0.79, 0.78])
        _bars(ax, rows, pal, label_fontsize=11, num_fontsize=10, right_pad_frac=0.10)

    # footer
    ax_f = fig.add_axes([0.05, 0.005, 0.9, 0.025]); ax_f.axis("off")
    ax_f.set_xlim(0, 1); ax_f.set_ylim(0, 1)
    ax_f.text(0, 0.5, f"SOURCE: {data.source} · AS OF {data.as_of}",
              color=pal["dim"], fontsize=9, family="monospace", va="center")
    ax_f.text(1, 0.5, "DCHUB.CLOUD / DAILY", color=pal["dim"], fontsize=9,
              family="monospace", va="center", ha="right", weight="bold")

    return _figure_to_image(fig, facecolor="#0A1220")


# --- theme B ----------------------------------------------------------------

def _render_b(data: RenderData, size: Size, pal_key: str = "b") -> Image.Image:
    W, H, N = SIZES[size]
    pal = PAL[pal_key]
    rows = top_n(data.states, N)

    total_op  = sum(s["op"]  for s in data.states)
    total_uc  = sum(s["uc"]  for s in data.states)
    total_ann = sum(s["ann"] for s in data.states)

    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    fig.patch.set_facecolor(pal["bg"])

    if size == "portrait":
        ax_h = fig.add_axes([0.05, 0.89, 0.9, 0.10]); ax_h.axis("off")
        _header_b(ax_h, data, big=40, kicker_size=14, pal=pal)
        _kpi_row_b(fig, [0.05, 0.80, 0.9, 0.075], total_op, total_uc, total_ann, pal)
        ax_c = fig.add_axes([0.02, 0.05, 0.18, 0.73]); ax_c.axis("off")
        _commentary_b(ax_c, data, pal)
        ax = fig.add_axes([0.24, 0.05, 0.73, 0.73])
        _bars(ax, rows, pal, label_fontsize=9.5, num_fontsize=7.5, right_pad_frac=0.12,
              inline_numbers=True, summary_labels=True)
    elif size == "square":
        ax_h = fig.add_axes([0.05, 0.86, 0.9, 0.13]); ax_h.axis("off")
        _header_b(ax_h, data, big=36, kicker_size=12, pal=pal)
        _kpi_row_b(fig, [0.05, 0.78, 0.9, 0.07], total_op, total_uc, total_ann, pal)
        ax = fig.add_axes([0.22, 0.04, 0.75, 0.72])
        _bars(ax, rows, pal, label_fontsize=10.5, num_fontsize=8.5, right_pad_frac=0.12,
              inline_numbers=True, summary_labels=True)
    else:  # story
        ax_h = fig.add_axes([0.05, 0.89, 0.9, 0.10]); ax_h.axis("off")
        _header_b(ax_h, data, big=38, kicker_size=12, pal=pal)
        _kpi_row_b(fig, [0.05, 0.80, 0.9, 0.075], total_op, total_uc, total_ann, pal)
        ax = fig.add_axes([0.22, 0.04, 0.75, 0.75])
        _bars(ax, rows, pal, label_fontsize=11, num_fontsize=9, right_pad_frac=0.12,
              inline_numbers=True, summary_labels=True)

    ax_f = fig.add_axes([0.05, 0.005, 0.9, 0.025]); ax_f.axis("off")
    ax_f.set_xlim(0, 1); ax_f.set_ylim(0, 1)
    ax_f.text(0, 0.5, f"Source: {data.source} · as of {data.as_of}",
              color=pal["dim"], fontsize=9, family="sans-serif", va="center")
    ax_f.text(1, 0.5, "DCHUB.CLOUD / DAILY",
              color=pal["accent"], fontsize=9, family="sans-serif",
              va="center", ha="right", weight="bold")
    return _figure_to_image(fig, facecolor=pal["bg"])


def _header_b(ax, data: RenderData, big: int, kicker_size: int, pal: dict):
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.text(0, 0.80, "D A I L Y   D A T A   C E N T E R   B R I E F",
            color=pal["accent"], fontsize=kicker_size, weight="bold", family="sans-serif")
    ax.text(0, 0.40, "Where America is Building",
            color=pal["ink"], fontsize=big, weight="bold", family="sans-serif")
    ax.text(0, 0.10, f"{data.generated} · by status · {len(data.states)} states + DC",
            color=pal["dim"], fontsize=12, family="sans-serif")


def _kpi_row_b(fig, rect: list[float], total_op: int, total_uc: int, total_ann: int, pal: dict):
    x0, y0, w, h = rect
    kpi = [("Operational", total_op, pal["op"]),
           ("Under Construction", total_uc, pal["uc"]),
           ("Announced", total_ann, pal["ann"])]
    for i, (lbl, val, col) in enumerate(kpi):
        xi = x0 + i * (w / 3) + 0.005
        wi = w / 3 - 0.01
        ax = fig.add_axes([xi, y0, wi, h]); ax.axis("off")
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.add_patch(FancyBboxPatch((0.01, 0.05), 0.98, 0.90,
                                    boxstyle="round,pad=0.02,rounding_size=0.03",
                                    linewidth=1.2, edgecolor=col, facecolor=pal.get("card_bg", "#121829")))
        ax.text(0.05, 0.68, " ".join(lbl.upper()), color=col, fontsize=10,
                family="sans-serif", weight="bold")
        ax.text(0.05, 0.22, f"{val:,}", color=pal["ink"], fontsize=24,
                family="sans-serif", weight="bold")


def _commentary_b(ax, data: RenderData, pal: dict):
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    highlights = _commentary_items(data)
    cur_y = 0.98
    for col_key, h, body in highlights:
        col = pal[col_key]
        ax.text(0.03, cur_y, h, color=col, fontsize=12, weight="bold", family="sans-serif")
        cur_y -= 0.032
        for line in _wrap(body, 20):
            ax.text(0.03, cur_y, line, color=pal["dim"], fontsize=9.5, family="sans-serif")
            cur_y -= 0.024
        cur_y -= 0.022


# --- theme C ----------------------------------------------------------------

def _render_c(data: RenderData, size: Size) -> Image.Image:
    W, H, N = SIZES[size]
    pal = PAL["c"]
    rows = top_n(data.states, N)

    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    fig.patch.set_facecolor(pal["bg"])

    # scanlines
    ax_bg = fig.add_axes([0, 0, 1, 1]); ax_bg.axis("off")
    ax_bg.set_xlim(0, 1); ax_bg.set_ylim(0, 1)
    for i in range(0, H, 3):
        ax_bg.axhline(i / H, color="#0a3b1e", linewidth=0.3, alpha=0.2, zorder=0)

    total = sum(s["op"] + s["uc"] + s["ann"] for s in data.states)

    if size == "portrait":
        ax_t = fig.add_axes([0.05, 0.895, 0.9, 0.10]); ax_t.axis("off")
        ax_t.set_xlim(0, 1); ax_t.set_ylim(0, 1)
        ax_t.text(0, 0.78, ">  DCHUB :: DAILY_REPORT", color=pal["accent"],
                  fontsize=20, family="monospace", weight="bold")
        ax_t.text(0, 0.44, "U.S. DATA CENTER HUBS OF THE FUTURE",
                  color=pal["ink"], fontsize=21, family="monospace", weight="bold")
        ax_t.text(0, 0.14, f"$ generated {data.generated}  ·  dataset {data.as_of}  ·  src {data.source.lower()}",
                  color=pal["dim"], fontsize=11, family="monospace")
        ax_l = fig.add_axes([0.05, 0.855, 0.9, 0.035]); ax_l.axis("off")
        _legend_c(ax_l, pal, total)
        ax = fig.add_axes([0.19, 0.04, 0.80, 0.80])
        _bars(ax, rows, pal, label_fontsize=9, num_fontsize=7.5, right_pad_frac=0.15,
              inline_numbers=False, summary_labels=True)
    elif size == "square":
        ax_t = fig.add_axes([0.05, 0.87, 0.9, 0.12]); ax_t.axis("off")
        ax_t.set_xlim(0, 1); ax_t.set_ylim(0, 1)
        ax_t.text(0, 0.80, ">  DCHUB :: DAILY_REPORT", color=pal["accent"],
                  fontsize=20, family="monospace", weight="bold")
        ax_t.text(0, 0.44, "TOP 25 U.S. DATA CENTER HUBS",
                  color=pal["ink"], fontsize=24, family="monospace", weight="bold")
        ax_t.text(0, 0.14, f"$ generated {data.generated}  ·  dataset {data.as_of}  ·  src {data.source.lower()}",
                  color=pal["dim"], fontsize=11, family="monospace")
        ax_l = fig.add_axes([0.05, 0.83, 0.9, 0.035]); ax_l.axis("off")
        _legend_c(ax_l, pal, total)
        ax = fig.add_axes([0.18, 0.05, 0.80, 0.76])
        _bars(ax, rows, pal, label_fontsize=10.5, num_fontsize=9, right_pad_frac=0.18,
              inline_numbers=False, summary_labels=True)
    else:  # story
        ax_t = fig.add_axes([0.05, 0.885, 0.9, 0.11]); ax_t.axis("off")
        ax_t.set_xlim(0, 1); ax_t.set_ylim(0, 1)
        ax_t.text(0, 0.80, ">  DCHUB :: DAILY_REPORT", color=pal["accent"],
                  fontsize=22, family="monospace", weight="bold")
        ax_t.text(0, 0.40, "TOP 20 U.S. DC HUBS",
                  color=pal["ink"], fontsize=30, family="monospace", weight="bold")
        ax_t.text(0, 0.12, f"$ gen {data.generated}  ·  {data.as_of}",
                  color=pal["dim"], fontsize=12, family="monospace")
        ax_l = fig.add_axes([0.05, 0.85, 0.9, 0.035]); ax_l.axis("off")
        _legend_c(ax_l, pal, total)
        ax = fig.add_axes([0.18, 0.04, 0.80, 0.80])
        _bars(ax, rows, pal, label_fontsize=11.5, num_fontsize=10, right_pad_frac=0.22,
              inline_numbers=False, summary_labels=True)

    ax_f = fig.add_axes([0.05, 0.005, 0.9, 0.028]); ax_f.axis("off")
    ax_f.set_xlim(0, 1); ax_f.set_ylim(0, 1)
    ax_f.text(0, 0.5, "$ _", color=pal["accent"], fontsize=12, family="monospace",
              va="center", weight="bold")
    ax_f.text(1, 0.5, "dchub.cloud/daily", color=pal["dim"], fontsize=10,
              family="monospace", va="center", ha="right")
    return _figure_to_image(fig, facecolor=pal["bg"])


def _legend_c(ax, pal: dict, total: int):
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.text(0.00, 0.5, "[OP]  operational",       color=pal["op"],  fontsize=11,
            family="monospace", va="center")
    ax.text(0.23, 0.5, "[UC]  under construction", color=pal["uc"],  fontsize=11,
            family="monospace", va="center")
    ax.text(0.52, 0.5, "[AN]  announced",          color=pal["ann"], fontsize=11,
            family="monospace", va="center")
    ax.text(1.00, 0.5, f"TOTAL {total:,}", color=pal["ink"], fontsize=11,
            family="monospace", va="center", ha="right", weight="bold")


# --- utils ------------------------------------------------------------------

def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for w in text.split():
        if len(line) + 1 + len(w) > width and line:
            out.append(line); line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out


def _default_callouts() -> list[dict]:
    return [
        {"state": "TEXAS", "color_key": "op",
         "text": "Leads with its large land area, fast permitting, and deregulated grid that lets developers secure power quickly."},
        {"state": "GEORGIA", "color_key": "ann",
         "text": "Proposed a ban on most new data centers, potentially putting announced projects at risk."},
        {"state": "CALIFORNIA", "color_key": "uc",
         "text": "Strict permitting and energy access, with strengthening environmental reporting requirements."},
    ]


def _commentary_items(data: RenderData) -> list[tuple[str, str, str]]:
    rows = {s["name"]: s for s in data.states}
    out: list[tuple[str, str, str]] = []
    if "VIRGINIA" in rows:
        out.append(("op", "VIRGINIA",
                    f'Still the single biggest operational base — {rows["VIRGINIA"]["op"]} live sites.'))
    if "TEXAS" in rows:
        out.append(("ann", "TEXAS",
                    f'Leads the build-out. {rows["TEXAS"]["ann"]} announced facilities.'))
    if "GEORGIA" in rows:
        out.append(("ann", "GEORGIA",
                    f'{rows["GEORGIA"]["ann"]} announced, but proposed moratorium puts pipeline at risk.'))
    if "OHIO" in rows:
        out.append(("uc", "OHIO",
                    f'Quiet mover — {rows["OHIO"]["uc"]} facilities under construction.'))
    return out


def _figure_to_image(fig, facecolor: str) -> Image.Image:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=facecolor)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


# --- public entrypoint ------------------------------------------------------

def render(theme: Theme, size: Size, data: RenderData) -> Image.Image:
    if theme == "d":
        return _render_b(data, size, pal_key="d")
    fn = {"a": _render_a, "b": _render_b, "c": _render_c}[theme]
    return fn(data, size)


# --- CLI --------------------------------------------------------------------

def _cli():
    p = argparse.ArgumentParser()
    p.add_argument("--theme", choices=["a", "b", "c", "d"], default="d")
    p.add_argument("--size", choices=list(SIZES), default="portrait")
    p.add_argument("--data", default="data.json")
    p.add_argument("--out", default="out.png")
    args = p.parse_args()
    data = load_data(args.data)
    img = render(args.theme, args.size, data)
    img.save(args.out)
    print(f"wrote {args.out}  ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    _cli()
