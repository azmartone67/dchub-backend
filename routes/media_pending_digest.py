"""media_pending_digest.py — surface human-gated media drafts daily (2026-07-18).

WHY THIS EXISTS
===============
The press pipeline is draft-first by design: brain_press_loop (ship-wins),
ai-citations/draft-press, and partnership_press_template all land rows in
press_releases with published=FALSE, and the pitch lanes park rows in
press_pitch_drafts / media_pitch_drafts with status='pending'. That human
gate is deliberate — but NOTHING surfaced the pending set, so drafts rotted
invisibly (observed 2026-07-18: 5 unpublished releases back to week 23,
8 pending press pitches, 9 pending media pitches, 1 press_releases_queue row
stuck since 2026-06-04).

This module closes the surfacing gap without weakening any gate:

  GET  /api/v1/media/pending-drafts            admin JSON inventory
  POST /api/v1/media/pending-drafts/digest     ?send=true emails the digest
                                               (default = dry-run preview)
  GET  /api/v1/media/pending-drafts/approve    ?id=N&t=<hmac> one-click
                                               publish of ONE press_releases
                                               draft — fact-check-guard
                                               gated, fail-closed

HARD RULES
==========
  * The approve flip runs routes.media_fact_check_guard.verify_media_text()
    first and REFUSES (409, with the unverified claims listed) when the text
    cannot be corroborated. The guard is never bypassed; there is no force
    parameter on purpose.
  * The digest emails only the operator inbox(es) from DCHUB_BRIEFING_EMAIL /
    BRAIN_DIGEST_EMAIL — never subscribers.
  * Digest sends nothing when the pending set is empty (no daily noise).
"""
from __future__ import annotations

import os
import hmac
import html
import hashlib
import datetime
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
media_pending_digest_bp = Blueprint("media_pending_digest", __name__)

SITE = "https://dchub.cloud"


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception:
        return None


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or (request.headers.get("Authorization", "")
                    .replace("Bearer ", "").strip()))
    return bool(expected) and provided == expected


# ── one-click approve token (unforgeable, no DB column needed) ───────────
def _approve_secret() -> bytes:
    return (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
            or os.environ.get("DCHUB_SESSION_SECRET") or "dchub-pending-v1").encode()


def _approve_token(row_id: int) -> str:
    return hmac.new(_approve_secret(), f"approve-press:{row_id}".encode(),
                    hashlib.sha256).hexdigest()[:20]


def _approve_url(row_id: int) -> str:
    return (f"{SITE}/api/v1/media/pending-drafts/approve"
            f"?id={row_id}&t={_approve_token(row_id)}")


def _preview_url(row_id: int) -> str:
    # press-fix-2 (2026-07-19): the edge only renders PUBLISHED rows at
    # /news/<slug> (unknown slugs fall through to the digest viewer), so a
    # draft's natural URL 404s on dchub.cloud. The edge DOES proxy
    # /api/v1/media/* to the backend, so drafts are previewed through this
    # token-gated endpoint instead.
    return (f"{SITE}/api/v1/media/pending-drafts/preview"
            f"?id={row_id}&t={_approve_token(row_id)}")


def _repair_url(row_id: int) -> str:
    # guard-repair (2026-07-18): one-click "auto-repair & retry" for drafts the
    # fact-check guard blocks. Same per-row HMAC token as the approve link.
    return (f"{SITE}/api/v1/media/pending-drafts/repair"
            f"?id={row_id}&t={_approve_token(row_id)}")


# ── inventory ────────────────────────────────────────────────────────────
def _collect_pending(cur) -> dict:
    """Every human-gated draft lane, one dict. Each query fails soft so a
    missing table can never take down the whole digest."""
    out = {}

    def _q(key, sql):
        try:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            out[key] = [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as e:
            logger.warning("[pending_digest] %s query failed: %s", key, str(e)[:120])
            out[key] = []

    _q("press_releases_unpublished", """
        SELECT id, slug, title, category, source, created_at
        FROM press_releases
        WHERE published = FALSE
        ORDER BY created_at DESC LIMIT 40
    """)
    # Enrich with the brain's editorial review (why was this held?). Own
    # try so a missing media_editorial_reviews table can't hurt the digest.
    try:
        cur.execute("""
            SELECT DISTINCT ON (press_slug) press_slug, score, reasons
            FROM media_editorial_reviews
            ORDER BY press_slug, created_at DESC
        """)
        reviews = {r[0]: {"score": r[1], "reasons": r[2]} for r in cur.fetchall()}
        for row in out.get("press_releases_unpublished") or []:
            rv = reviews.get(row.get("slug"))
            if rv:
                row["editorial_score"] = rv["score"]
                row["editorial_reasons"] = (rv["reasons"] or [])[:3]
    except Exception:
        pass
    _q("press_releases_queue_drafts", """
        SELECT id, slug, title, trigger_type, created_at
        FROM press_releases_queue
        WHERE status = 'draft'
        ORDER BY created_at DESC LIMIT 40
    """)
    _q("press_pitch_drafts_pending", """
        SELECT id, subject, angle_key, created_at
        FROM press_pitch_drafts
        WHERE status = 'pending'
        ORDER BY created_at DESC LIMIT 40
    """)
    _q("media_pitch_drafts_pending", """
        SELECT id, subject, publication, created_at
        FROM media_pitch_drafts
        WHERE status = 'pending'
        ORDER BY created_at DESC LIMIT 40
    """)
    _q("media_story_queue_pending", """
        SELECT id, market_name, shift_kind, created_at
        FROM media_story_queue
        WHERE status = 'pending'
        ORDER BY created_at DESC LIMIT 40
    """)
    # agent-iteration suite (2026-07-19): fresh unpublished partner verdicts
    # ride the same daily digest — one-click consent-gated publication to
    # /agent-verdicts. Own try; suite absence can't hurt the digest.
    try:
        from routes.agent_iteration_suite import pending_verdicts_for_digest
        out["agent_verdicts_pending"] = pending_verdicts_for_digest()
    except Exception:
        out["agent_verdicts_pending"] = []
    out["total"] = sum(len(v) for v in out.values() if isinstance(v, list))
    return out


def _age_days(ts) -> str:
    try:
        now = datetime.datetime.now(ts.tzinfo) if getattr(ts, "tzinfo", None) \
            else datetime.datetime.utcnow()
        return f"{max(0, (now - ts).days)}d"
    except Exception:
        return "?"


def _render_email_html(pending: dict) -> str:
    """Email-client-safe HTML (inline styles + tables only)."""
    def esc(s):
        return html.escape(str(s or ""))

    sections = []

    rows = pending.get("press_releases_unpublished") or []
    if rows:
        trs = "".join(
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #e2e8f0">'
            f'<a href="{_preview_url(r["id"])}" style="color:#1d4ed8">{esc(r["title"])[:110]}</a>'
            f'<br><span style="color:#64748b;font-size:12px">{esc(r["category"])} · {_age_days(r["created_at"])} old'
            + (f' · <span style="color:#b45309">brain: {r["editorial_score"]}/10 — {esc((r.get("editorial_reasons") or [""])[0])[:90]}</span>'
               if r.get("editorial_score") is not None else "")
            + (f' · <span style="color:#b91c1c">fact-check blocked: {esc(", ".join(r.get("fact_check_claims") or []))[:90]} — <a href="{_repair_url(r["id"])}" style="color:#b91c1c">auto-repair</a></span>'
               if r.get("fact_check") == "blocked" else "")
            + (' · <span style="color:#b45309">auto-repaired to pass fact-check</span>'
               if r.get("fact_check") == "auto_repaired" else "")
            + '</span></td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #e2e8f0;white-space:nowrap">'
            f'<a href="{_approve_url(r["id"])}" style="color:#fff;background:#16a34a;padding:5px 12px;'
            f'border-radius:5px;text-decoration:none;font-size:13px">Fact-check &amp; publish</a></td></tr>'
            for r in rows)
        sections.append(("Unpublished press releases (approve = fact-check gate, then live)", trs))

    lane_labels = [
        ("press_releases_queue_drafts", "Press-queue drafts (data-growth lane)",
         lambda r: f'{esc(r["title"])[:110]}<br><span style="color:#64748b;font-size:12px">{esc(r.get("trigger_type"))} · {_age_days(r["created_at"])} old</span>'),
        ("press_pitch_drafts_pending", "Journalist pitch drafts pending",
         lambda r: f'{esc(r["subject"])[:110]}<br><span style="color:#64748b;font-size:12px">{esc(r.get("angle_key"))} · {_age_days(r["created_at"])} old</span>'),
        ("media_pitch_drafts_pending", "Media outreach drafts pending",
         lambda r: f'{esc(r["subject"])[:110]}<br><span style="color:#64748b;font-size:12px">{esc(r.get("publication"))} · {_age_days(r["created_at"])} old</span>'),
        ("media_story_queue_pending", "Story-queue briefs pending review",
         lambda r: f'{esc(r.get("market_name"))} — {esc(r.get("shift_kind"))}<br><span style="color:#64748b;font-size:12px">{_age_days(r["created_at"])} old</span>'),
    ]
    for key, label, fmt in lane_labels:
        rows = pending.get(key) or []
        if rows:
            trs = "".join(
                f'<tr><td style="padding:6px 10px;border-bottom:1px solid #e2e8f0">{fmt(r)}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:12px">review in admin</td></tr>'
                for r in rows)
            sections.append((label, trs))

    rows = pending.get("agent_verdicts_pending") or []
    if rows:
        trs = "".join(
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #e2e8f0">'
            f'<b>{esc(r["platform"])}</b> ({esc(r.get("model") or "")}): '
            f'&ldquo;{esc(r["snippet"])}&hellip;&rdquo;</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #e2e8f0;white-space:nowrap">'
            f'<a href="{esc(r["publish_url"])}" style="color:#fff;background:#7c3aed;padding:5px 12px;'
            f'border-radius:5px;text-decoration:none;font-size:13px">Publish verdict</a></td></tr>'
            for r in rows)
        sections.append(("Partner verdicts awaiting showcase approval (→ /agent-verdicts)", trs))

    body_sections = "".join(
        f'<h3 style="margin:22px 0 8px;font-size:15px;color:#0f172a">{html.escape(label)}</h3>'
        f'<table cellpadding="0" cellspacing="0" width="100%" '
        f'style="border-collapse:collapse;background:#fff;border:1px solid #e2e8f0;border-radius:6px">{trs}</table>'
        for label, trs in sections)

    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
background:#f1f5f9;padding:24px">
<div style="max-width:640px;margin:0 auto">
<div style="background:#0f172a;color:#fff;padding:16px 22px;border-radius:8px 8px 0 0">
<strong>DC Hub Media — pending drafts digest</strong>
<div style="color:#94a3b8;font-size:13px">{pending.get("total", 0)} item(s) awaiting a human decision</div>
</div>
<div style="background:#f8fafc;padding:6px 22px 22px;border:1px solid #e2e8f0;border-top:0;border-radius:0 0 8px 8px">
{body_sections}
<p style="color:#64748b;font-size:12px;margin-top:20px">
"Fact-check &amp; publish" runs the media fact-check guard first and refuses
(with the uncorroborated claims listed) if the text can't be verified — the
honesty gate is never bypassed. Sent daily only when the pending set is
non-empty.</p>
</div></div></div>"""


# ── endpoints ────────────────────────────────────────────────────────────
@media_pending_digest_bp.route("/api/v1/media/pending-drafts", methods=["GET"])
def pending_drafts():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            pending = _collect_pending(cur)
        for r in pending.get("press_releases_unpublished") or []:
            r["approve_url"] = _approve_url(r["id"])
            r["view_url"] = _preview_url(r["id"])
        return jsonify(ok=True, pending=pending), 200
    finally:
        try: c.close()
        except Exception: pass


@media_pending_digest_bp.route("/api/v1/media/pending-drafts/digest",
                               methods=["POST", "GET"])
def pending_drafts_digest():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    send = request.args.get("send", "").lower() in ("1", "true", "yes")

    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            pending = _collect_pending(cur)
        # guard-repair (2026-07-18): close the automation loop — before the
        # digest goes out, auto-repair guard-blocked press drafts per the
        # guard's own hints (drafts stay UNPUBLISHED; they re-surface here as
        # passing). Dry-run annotates without mutating. Fail-soft: a repair
        # blip must never block the digest.
        try:
            from routes.media_draft_repair import annotate_pending_rows
            c.autocommit = True
            annotate_pending_rows(
                c, pending.get("press_releases_unpublished") or [], repair=send)
        except Exception as _rep_e:
            logger.warning("[pending_digest] repair annotate skipped: %s",
                           str(_rep_e)[:120])
    finally:
        try: c.close()
        except Exception: pass

    recipients = [e.strip() for e in
                  (os.environ.get("DCHUB_BRIEFING_EMAIL")
                   or os.environ.get("BRAIN_DIGEST_EMAIL")
                   or "jonathan@dchub.cloud").split(",") if e.strip()]

    if pending.get("total", 0) == 0:
        return jsonify(ok=True, sent=0, total_pending=0,
                       note="pending set empty — nothing to send"), 200

    html_body = _render_email_html(pending)
    if not send:
        return jsonify(ok=True, dry_run=True, total_pending=pending["total"],
                       recipients=recipients, html_bytes=len(html_body),
                       pending={k: len(v) for k, v in pending.items()
                                if isinstance(v, list)}), 200

    sent = 0
    failed = 0
    try:
        # r-chokepoint: route the operator-only pending-drafts digest through the
        # sanctioned transactional sender (main._resend_email) instead of a raw
        # Resend POST. Lazy import (top-level would be circular). Transport-only swap.
        from main import _resend_email
        subject = f"DC Hub Media: {pending['total']} pending draft(s) awaiting review"
        from_hdr = os.environ.get("DCHUB_FROM_EMAIL", "DC Hub <jonathan@dchub.cloud>")
        if "<" in from_hdr and ">" in from_hdr:
            from_name = from_hdr.split("<", 1)[0].strip() or "DC Hub"
            from_email = from_hdr.split("<", 1)[1].split(">", 1)[0].strip()
        else:
            from_name, from_email = "DC Hub", from_hdr.strip()
        for em in recipients:
            try:
                if _resend_email(em, subject, html_body,
                                 from_email=from_email, from_name=from_name):
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160], sent=sent, failed=failed), 500
    return jsonify(ok=True, sent=sent, failed=failed,
                   total_pending=pending["total"]), 200


@media_pending_digest_bp.route("/api/v1/media/pending-drafts/preview",
                               methods=["GET"])
def preview_pending_draft():
    """Token-gated draft preview. The public edge only renders published
    rows at /news/<slug>, so this is the ONLY dchub.cloud URL that shows a
    draft before approval. Published rows redirect to their live page."""
    from flask import redirect, Response
    try:
        row_id = int(request.args.get("id", ""))
    except Exception:
        return jsonify(error="bad_id"), 400
    token = (request.args.get("t") or "").strip()
    if not (_admin_ok() or (token and hmac.compare_digest(token, _approve_token(row_id)))):
        return jsonify(error="unauthorized"), 401

    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT slug, title, subheadline, body, category,
                                  published, created_at
                           FROM press_releases WHERE id = %s""", (row_id,))
            row = cur.fetchone()
    finally:
        try: c.close()
        except Exception: pass
    if not row:
        return jsonify(error="not_found", id=row_id), 404
    slug, title, subheadline, body, category, published, created_at = row
    if published:
        return redirect(f"{SITE}/news/{slug}", code=302)

    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DRAFT — {html.escape(title or slug)}</title></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
background:#f1f5f9;margin:0;padding:24px">
<div style="max-width:720px;margin:0 auto">
<div style="background:#b45309;color:#fff;padding:10px 18px;border-radius:8px 8px 0 0;
font-size:14px"><strong>DRAFT PREVIEW</strong> — not published.
{html.escape(category or '')} · created {html.escape(str(created_at)[:16])}
&nbsp;·&nbsp;<a href="{_approve_url(row_id)}" style="color:#fde68a">Fact-check &amp; publish</a></div>
<div style="background:#fff;padding:26px 30px;border:1px solid #e2e8f0;border-top:0;
border-radius:0 0 8px 8px;line-height:1.6;color:#0f172a">
{f'<p style="color:#475569;font-style:italic">{html.escape(subheadline)}</p>' if subheadline else ''}
{body or '<p>(empty body)</p>'}
</div></div></body></html>"""
    return Response(page, mimetype="text/html")


@media_pending_digest_bp.route("/api/v1/media/pending-drafts/approve",
                               methods=["GET", "POST"])
def approve_pending_draft():
    """One-click publish of a single unpublished press_releases row.

    Auth = HMAC token (from the digest email) OR the admin key. The
    fact-check guard runs first and REFUSES uncorroborated text — there is
    deliberately no way to skip it from this endpoint."""
    try:
        row_id = int(request.args.get("id", ""))
    except Exception:
        return jsonify(error="bad_id"), 400
    token = (request.args.get("t") or "").strip()
    if not (_admin_ok() or (token and hmac.compare_digest(token, _approve_token(row_id)))):
        return jsonify(error="unauthorized"), 401

    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("""SELECT slug, title, body, published
                           FROM press_releases WHERE id = %s""", (row_id,))
            row = cur.fetchone()
            if not row:
                return jsonify(error="not_found", id=row_id), 404
            slug, title, body, published = row
            if published:
                return jsonify(ok=True, already_published=True, slug=slug,
                               url=f"{SITE}/news/{slug}"), 200

            # COMPLETENESS GATE (press-integrity, 2026-08-07) — runs BEFORE the
            # fact-check guard because it is cheaper and answers a different
            # question. The fact-check lane proves the NUMBERS are true; it has
            # nothing to say about a body that is blank, a stub, placeholder or
            # error text, or a release dated in the future. That gap is what put
            # /news/2026-08-07-perplexity-dcpi-dual-score-citation live as an
            # empty, future-dated page. The composers now hold such a release as
            # a draft; without this, one click here would publish it anyway.
            try:
                from routes.press_integrity import gate_press_publish
                _pi = gate_press_publish(
                    {"title": title, "slug": slug, "body": body,
                     "date": datetime.date.today().isoformat()},
                    want_published=True,
                    where="media_pending_digest.approve")
            except Exception as _pie:
                return jsonify(ok=False, error="press_integrity_unavailable",
                               detail=str(_pie)[:120],
                               note="fail-closed: draft NOT published"), 503
            if not _pi.get("publish"):
                return jsonify(
                    ok=False, blocked_by="press_integrity",
                    slug=slug, issues=_pi.get("issues") or [],
                    repair_url=_repair_url(row_id),
                    note=("The release is incomplete — " + _pi.get("note", "")
                          + ". Fix the draft body/title/date (or follow "
                            "repair_url) and retry. This check cannot be "
                            "bypassed from this endpoint.")), 409

            # HONESTY GATE — corroborate before anything goes live. Guard
            # unavailable = fail closed (stay a draft), never publish blind.
            try:
                from routes.media_fact_check_guard import verify_media_text
                report = verify_media_text(f"{title}\n{body or ''}") or {}
            except Exception as e:
                return jsonify(ok=False, error="fact_check_guard_unavailable",
                               detail=str(e)[:120],
                               note="fail-closed: draft NOT published"), 503
            if not report.get("ok"):
                # guard-repair (2026-07-18): the raw-JSON 409 dead-ended the
                # human (no repair path). Browser click-throughs now get a
                # readable page listing the claims + a one-click HMAC-gated
                # "auto-repair & retry" (the repairer edits the draft per the
                # guard's own hints and publishes ONLY if the guard passes the
                # repaired text — the guard itself is never weakened).
                unverified = report.get("unverified", [])[:10]
                wants_html = (request.method == "GET" and
                              "text/html" in (request.headers.get("Accept") or ""))
                if wants_html:
                    from flask import Response
                    claim_rows = "".join(
                        '<tr>'
                        f'<td style="padding:6px 10px;border-bottom:1px solid #e2e8f0">'
                        f'<code>{html.escape(str(u.get("claim") or ""))}</code></td>'
                        f'<td style="padding:6px 10px;border-bottom:1px solid #e2e8f0;'
                        f'color:#475569;font-size:13px">{html.escape(str(u.get("expected") or ""))}</td>'
                        '</tr>'
                        for u in unverified) or '<tr><td>(none listed)</td><td></td></tr>'
                    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fact-check blocked — {html.escape(title or slug)}</title></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
background:#f1f5f9;margin:0;padding:24px">
<div style="max-width:720px;margin:0 auto">
<div style="background:#b91c1c;color:#fff;padding:12px 18px;border-radius:8px 8px 0 0">
<strong>Blocked by the fact-check guard</strong> — the draft stays unpublished.</div>
<div style="background:#fff;padding:24px 28px;border:1px solid #e2e8f0;border-top:0;
border-radius:0 0 8px 8px;line-height:1.6;color:#0f172a">
<h2 style="margin-top:0;font-size:18px">{html.escape(title or slug)}</h2>
<p>These claims could not be corroborated against live data
({report.get("checked") or 0} checked):</p>
<table cellpadding="0" cellspacing="0" width="100%"
style="border-collapse:collapse;border:1px solid #e2e8f0;border-radius:6px">
<tr><th style="text-align:left;padding:6px 10px;border-bottom:2px solid #e2e8f0">Claim</th>
<th style="text-align:left;padding:6px 10px;border-bottom:2px solid #e2e8f0">Guard's hint</th></tr>
{claim_rows}</table>
<p style="margin-top:20px">
<a href="{_repair_url(row_id)}" style="color:#fff;background:#b45309;padding:9px 16px;
border-radius:6px;text-decoration:none;font-weight:600">Auto-repair &amp; retry</a>
&nbsp;&nbsp;<a href="{_preview_url(row_id)}" style="color:#1d4ed8">Preview the draft</a></p>
<p style="color:#64748b;font-size:13px">Auto-repair removes or corrects each
uncorroborated figure per the guard's own hints, re-runs the guard, and
publishes only if the repaired text passes. The guard is never bypassed.</p>
</div></div></body></html>"""
                    return Response(page, status=409, mimetype="text/html")
                return jsonify(ok=False, blocked_by="fact_check_guard",
                               unverified=unverified,
                               checked=report.get("checked"),
                               repair_url=_repair_url(row_id),
                               note=("Uncorroborated claims — fix the draft "
                                     "body or follow repair_url, then retry. "
                                     "The guard cannot be bypassed from this "
                                     "endpoint.")), 409

            cur.execute("""
                UPDATE press_releases
                SET published = TRUE,
                    published_at = NOW(),
                    published_date = COALESCE(published_date, to_char(NOW(), 'YYYY-MM-DD')),
                    date = COALESCE(date, to_char(NOW(), 'YYYY-MM-DD'))
                WHERE id = %s AND published = FALSE
                RETURNING slug
            """, (row_id,))
            updated = cur.fetchone()
        return jsonify(ok=True, published=bool(updated), slug=slug,
                       url=f"{SITE}/news/{slug}",
                       fact_check={"checked": report.get("checked"),
                                   "ok": True}), 200
    finally:
        try: c.close()
        except Exception: pass
