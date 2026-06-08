"""
innovation_hub.py — unified "What's Next" surface (2026-06-08, r73).
=====================================================================
Aggregates the three concept streams that were previously SCATTERED across
separate pages/tables, so there's ONE place to review everything the brain +
media + product layers generate:

  • Product enhancements / strategic bets — Layer 6
      (brain_strategic_recommendations: build-next, competitor-feature gaps,
       funnel optimizations w/ $-lift, wildcard)
  • New capabilities — Layer 23
      (brain_lifecycle_proposals: proactive features, Opus-proposed)
  • DC Hub Media concepts — the live topic mix (media_topic_tuner)

Surfaces:
  GET /brain/roadmap   (+ alias /innovation)   human page
  GET /api/v1/brain/roadmap                     machine-readable JSON

Defensive: every stream is wrapped in its own try/except → a missing/empty
source renders an empty section, never a 500.
"""
from flask import Blueprint, jsonify
import html as _h
import datetime as _dt

innovation_hub_bp = Blueprint("innovation_hub", __name__)


def _db():
    """Lazy import to avoid the main<->routes circular import at module load."""
    try:
        from main import get_read_db
        c = get_read_db()
        if c:
            return c
    except Exception:
        pass
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _rows(sql, params=()):
    """Run a read query with a RealDictCursor; always close the conn."""
    conn = _db()
    if conn is None:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        out = [dict(r) for r in (cur.fetchall() or [])]
        cur.close()
        return out
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _strategic(limit=15):
    """Layer-6 product/strategic recommendations."""
    rows = _rows(
        """
        SELECT kind, title, spec_md, dollar_lift, confidence, pr_url, week_of
          FROM brain_strategic_recommendations
         ORDER BY week_of DESC NULLS LAST, id DESC
         LIMIT %s
        """, (limit,))
    out = []
    for r in rows:
        out.append({
            "kind": r.get("kind") or "gap",
            "title": (r.get("title") or "").strip(),
            "spec": (r.get("spec_md") or "").strip()[:360],
            "dollar_lift": r.get("dollar_lift"),
            "confidence": r.get("confidence"),
            "pr_url": r.get("pr_url"),
            "week_of": str(r.get("week_of")) if r.get("week_of") else None,
        })
    return out


def _lifecycle(limit=15):
    """Layer-23 new-capability proposals."""
    rows = _rows(
        """
        SELECT proposal_kind, proposal_text, approved, proposed_at
          FROM brain_lifecycle_proposals
         ORDER BY proposed_at DESC
         LIMIT %s
        """, (limit,))
    out = []
    for r in rows:
        out.append({
            "kind": r.get("proposal_kind") or "capability",
            "text": (r.get("proposal_text") or "").strip()[:360],
            "approved": r.get("approved"),
            "proposed_at": str(r.get("proposed_at")) if r.get("proposed_at") else None,
        })
    return out


def _media():
    """DC Hub Media — live topic mix (what's being created + how it performs)."""
    try:
        from routes.media_topic_tuner import current_topic_mix
        mix = current_topic_mix() or []
        return mix[:15]
    except Exception:
        return []


def _data():
    return {
        "strategic": _strategic(),
        "lifecycle": _lifecycle(),
        "media": _media(),
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
    }


@innovation_hub_bp.route("/api/v1/brain/roadmap", methods=["GET"])
def roadmap_json():
    return jsonify(_data()), 200, {
        "Cache-Control": "public, max-age=300, s-maxage=600, stale-while-revalidate=3600",
    }


def _conf_badge(c):
    c = (c or "").lower()
    color = {"high": "#22c55e", "medium": "#f59e0b", "low": "#94a3b8"}.get(c, "#64748b")
    return (f'<span style="background:{color}22;color:{color};border:1px solid {color}55;'
            f'border-radius:6px;padding:1px 7px;font-size:.72rem;font-weight:600">{_h.escape(c or "—")}</span>')


def _card(title, body, meta="", accent="#6366f1"):
    return (
        f'<div style="background:#13131f;border:1px solid #23233a;border-left:3px solid {accent};'
        f'border-radius:10px;padding:14px 16px;margin:0 0 12px">'
        f'<div style="font-weight:600;color:#e8e8f0;font-size:.98rem;margin-bottom:4px">{title}</div>'
        f'<div style="color:#9aa0b4;font-size:.86rem;line-height:1.5">{body}</div>'
        f'{("<div style=margin-top:6px>" + meta + "</div>") if meta else ""}'
        f'</div>')


def _render():
    d = _data()
    strat, life, media = d["strategic"], d["lifecycle"], d["media"]

    def strat_cards():
        if not strat:
            return '<p style="color:#64748b">No strategic recs yet — Layer-6 synthesis runs Mondays.</p>'
        out = []
        for s in strat:
            lift = (f' · <span style="color:#22c55e">~${int(s["dollar_lift"]):,}/yr lift</span>'
                    if s.get("dollar_lift") else "")
            pr = (f' · <a href="{_h.escape(s["pr_url"])}" style="color:#818cf8">draft PR</a>'
                  if s.get("pr_url") else "")
            meta = (f'<span style="color:#6b7280;font-size:.74rem">{_h.escape(s["kind"])}'
                    f' · {_conf_badge(s.get("confidence"))}{lift}{pr}'
                    f'{(" · " + _h.escape(s["week_of"])) if s.get("week_of") else ""}</span>')
            out.append(_card(_h.escape(s["title"] or "(untitled)"), _h.escape(s["spec"]), meta, "#6366f1"))
        return "".join(out)

    def life_cards():
        if not life:
            return '<p style="color:#64748b">No capability proposals yet — Layer-23 curator runs every 2h.</p>'
        out = []
        for p in life:
            ap = p.get("approved")
            badge = ('<span style="color:#22c55e">✓ approved</span>' if ap is True
                     else '<span style="color:#ef4444">rejected</span>' if ap is False
                     else '<span style="color:#f59e0b">pending</span>')
            meta = (f'<span style="color:#6b7280;font-size:.74rem">{_h.escape(p["kind"])} · {badge}'
                    f'{(" · " + _h.escape(p["proposed_at"][:10])) if p.get("proposed_at") else ""}</span>')
            out.append(_card("New capability", _h.escape(p["text"]), meta, "#a855f7"))
        return "".join(out)

    def media_cards():
        if not media:
            return '<p style="color:#64748b">No topic mix yet.</p>'
        out = ['<div style="display:flex;flex-wrap:wrap;gap:8px">']
        for m in media:
            topic = _h.escape(str(m.get("topic", "")))
            w = m.get("weight")
            wt = f' · {round(float(w)*100)}%' if isinstance(w, (int, float)) else ""
            n = m.get("n_posts")
            np = f' · {int(n)} posts' if n else ""
            out.append(f'<span style="background:#13131f;border:1px solid #23233a;border-radius:8px;'
                       f'padding:6px 12px;color:#cbd0e0;font-size:.84rem">{topic}'
                       f'<span style="color:#6b7280;font-size:.74rem">{wt}{np}</span></span>')
        out.append("</div>")
        return "".join(out)

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Innovation Hub — What's Next | DC Hub</title>
<meta name="description" content="Live view of everything the DC Hub brain, media, and product layers are proposing next — strategic product bets, new capabilities, and content concepts, in one place.">
<meta name="robots" content="noindex,nofollow">
<link rel="canonical" href="https://dchub.cloud/brain/roadmap">
<style>
 body{{font-family:'Instrument Sans',-apple-system,system-ui,sans-serif;background:#0a0a12;color:#e8e8f0;margin:0;padding:0 16px 60px}}
 .wrap{{max-width:880px;margin:0 auto}}
 h1{{font-size:1.7rem;margin:28px 0 2px}}
 .sub{{color:#9aa0b4;font-size:.92rem;margin:0 0 6px}}
 .ts{{color:#6b7280;font-size:.76rem;margin-bottom:22px}}
 h2{{font-size:1.05rem;margin:30px 0 12px;color:#e8e8f0;display:flex;align-items:center;gap:8px}}
 h2 .pill{{font-size:.7rem;font-weight:600;color:#818cf8;background:#6366f122;border:1px solid #6366f155;border-radius:6px;padding:1px 8px}}
 a{{color:#818cf8}}
</style></head><body><div class="wrap">
 <h1>🧭 Innovation Hub — What's Next</h1>
 <p class="sub">One surface for everything the brain + media + product layers are generating. Previously scattered across <a href="/brain">/brain</a>, the admin backlog, and the morning brief.</p>
 <p class="ts">Generated {_h.escape(d["generated_at"])} · JSON: <a href="/api/v1/brain/roadmap">/api/v1/brain/roadmap</a></p>

 <h2>🚀 Product enhancements <span class="pill">Layer 6 · strategic</span></h2>
 {strat_cards()}

 <h2>✨ New capabilities <span class="pill">Layer 23 · lifecycle</span></h2>
 {life_cards()}

 <h2>📣 DC Hub Media concepts <span class="pill">topic mix</span></h2>
 {media_cards()}
</div></body></html>"""


@innovation_hub_bp.route("/brain/roadmap", methods=["GET"])
@innovation_hub_bp.route("/innovation", methods=["GET"])
def roadmap_page():
    return _render(), 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=120, stale-while-revalidate=600",
    }
