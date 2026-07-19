"""media_draft_repair.py — auto-repair guard-blocked press drafts (2026-07-18).

WHY THIS EXISTS
===============
"Fact-check & publish" from the pending-drafts digest runs the media
fact-check guard and — correctly — refuses uncorroborated text. But the
refusal was raw JSON ({"blocked_by":"fact_check_guard","unverified":[...]})
with NO repair path: the human hit a dead end and the draft rotted. The
hybrid-newsroom editorial gate (6a38db7b) judges NOVELTY only; it has no
claim repair. This module closes the loop WITHOUT weakening the guard:

    repair_text(text)          — remove/qualify uncorroborated numeric claims
                                 per the guard's own `expected` hints, re-run
                                 the guard, iterate (max 3 passes). The final
                                 verdict is ALWAYS verify_media_text() on the
                                 final text — the guard is never bypassed.
    auto_repair_press_draft()  — load an unpublished draft, repair, SAVE the
                                 repaired body (draft stays unpublished unless
                                 publish=True AND the guard passes).
    annotate_pending_rows()    — digest wiring: tag each pending press draft
                                 passing / auto_repaired / repairable /
                                 blocked so the digest shows the state and a
                                 repair link instead of a dead end.

REPAIR RULES (all derived from the guard's report — never invented):
  * count/agent over-claims (found_live is a number, expected "<= N ...")
        → replace the claimed number with the corroborated live figure.
  * time-to-power mismatch (expected "~N months for <market>")
        → replace the claimed months with the live months.
  * DCPI verdict mismatch (expected "... is BUILD, not AVOID")
        → replace the claimed verdict word with the live verdict.
  * everything uncorroborable (MW/GW aggregates, bare %, dollar aggregates,
    canon unavailable) → DROP the clause containing the claim (the guard's
    own hint: "omit it"). Clause bounds respect sentence punctuation, em/en
    dashes, newlines, and HTML tag edges, so markup survives.

Endpoint (HMAC-gated exactly like the digest approve link):
  GET/POST /api/v1/media/pending-drafts/repair?id=N&t=<hmac>
      → repair, re-run guard, and PUBLISH only if the repaired text passes.
        Renders a human-readable result page for GET (email click-through).
"""
from __future__ import annotations

import re
import html
import logging

logger = logging.getLogger(__name__)

try:
    from flask import Blueprint, jsonify, request, Response
    media_draft_repair_bp = Blueprint("media_draft_repair", __name__)
except Exception:  # pragma: no cover — Flask is always present in prod
    Blueprint = None
    media_draft_repair_bp = None

_MAX_PASSES = 3
# Clause boundaries: sentence punctuation, newlines, em/en dashes, semicolons,
# and HTML tag edges ('>' opens a text node, '<' closes it) so a removal can
# never cut across markup.
_CLAUSE_START = ".!?\n>—–;"
_CLAUSE_END = ".!?\n<—–;"

_NUM_TOKEN = re.compile(r"[\d][\d,\.]*")


def _verify(text: str) -> dict:
    from routes.media_fact_check_guard import verify_media_text
    return verify_media_text(text) or {}


# ── single-claim repairs ─────────────────────────────────────────────────
def _replace_number_in_claim(text: str, raw: str, new_num: str):
    """Swap the numeric token inside `raw` for `new_num` and substitute the
    first occurrence in `text`. Returns (new_text, note) or None."""
    if not raw or raw not in text:
        return None
    m = _NUM_TOKEN.search(raw)
    if not m:
        return None
    fixed = raw[:m.start()] + new_num + raw[m.end():]
    # a "22,000+" style over-claim replaced by the exact live figure must not
    # keep the "+" (that would immediately over-claim again)
    fixed = fixed.replace(new_num + "+", new_num)
    if fixed == raw:
        return None
    return (text.replace(raw, fixed, 1),
            f'replaced "{raw}" with corroborated "{fixed}"')


def _drop_clause(text: str, needle: str):
    """Remove the clause containing `needle`. Returns (new_text, note) or None."""
    if not needle:
        return None
    idx = text.find(needle)
    if idx < 0:
        m = re.search(re.escape(needle), text, re.I)
        if not m:
            return None
        idx = m.start()
    start = idx
    while start > 0 and text[start - 1] not in _CLAUSE_START:
        start -= 1
    end = idx + len(needle)
    while end < len(text) and text[end] not in _CLAUSE_END:
        end += 1
    if end < len(text) and text[end] in ".!?;":
        end += 1
    removed = text[start:end].strip()
    new_text = (text[:start] + text[end:])
    # tidy doubled spaces left behind (never touch inside tags)
    new_text = re.sub(r"[ \t]{2,}", " ", new_text)
    return new_text, f'dropped uncorroborated clause "{removed[:90]}"'


_LIVE_MONTHS_RE = re.compile(r"~(\d+(?:\.\d+)?)\s*months")
_LIVE_VERDICT_RE = re.compile(r"\bis\s+(BUILD|CAUTION|AVOID|WATCH)\b")


def _apply_one_repair(text: str, item: dict):
    """Repair ONE unverified item per the guard's own hints.
    Returns (new_text, note) or None if no rule applies / nothing changed."""
    raw = str(item.get("claim") or "")
    expected = str(item.get("expected") or "")
    found_live = item.get("found_live")

    # 1) over-claims with a corroborated live number → replace with live
    if isinstance(found_live, (int, float)) and expected.startswith("<="):
        out = _replace_number_in_claim(text, raw, f"{int(found_live):,}")
        if out:
            return out

    # 2) time-to-power mismatch → replace with the live months
    if "months" in expected:
        m = _LIVE_MONTHS_RE.search(expected)
        if m:
            out = _replace_number_in_claim(text, raw, m.group(1))
            if out:
                return out

    # 3) DCPI verdict mismatch → replace claimed verdict with the live one
    m = _LIVE_VERDICT_RE.search(expected)
    if m and isinstance(found_live, str):
        live_v = m.group(1)
        parts = raw.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].upper() in ("BUILD", "CAUTION", "AVOID", "WATCH"):
            market, claimed_v = parts[0], parts[1]
            # fix the verdict word in the clause that names the market
            pat = re.compile(
                re.escape(market) + r"([^.!?\n<]{0,60}?)\b" + re.escape(claimed_v) + r"\b")
            new_text, n = pat.subn(lambda mm: market + mm.group(1) + live_v, text, count=1)
            if n:
                return new_text, f'corrected verdict "{market} {claimed_v}" → "{market} {live_v}"'

    # 4) uncorroborable (MW/GW aggregates, bare %, dollar aggregates, canon
    #    unavailable, unresolvable market/verdict) → drop the clause ("omit it")
    return _drop_clause(text, raw)


# ── the repair loop (guard-final, never bypassed) ────────────────────────
def repair_text(text: str, max_passes: int = _MAX_PASSES) -> dict:
    """Iteratively repair `text` per the guard's hints. The returned `ok` is
    ALWAYS the guard's verdict on the FINAL text — this function cannot pass
    anything the guard does not corroborate."""
    changes: list[str] = []
    report = _verify(text)
    passes = 0
    while not report.get("ok") and passes < max_passes:
        progressed = False
        for item in (report.get("unverified") or []):
            try:
                out = _apply_one_repair(text, item)
            except Exception as e:
                logger.warning("[draft_repair] rule failed: %s", str(e)[:120])
                out = None
            if out:
                text, note = out
                changes.append(note)
                progressed = True
        if not progressed:
            break
        passes += 1
        report = _verify(text)
    return {"ok": bool(report.get("ok")), "text": text, "changes": changes,
            "passes": passes, "report": report}


# ── draft-level operations ───────────────────────────────────────────────
def auto_repair_press_draft(conn, row_id: int, publish: bool = False) -> dict:
    """Load an UNPUBLISHED press_releases row, repair its title+body, save the
    repaired text, and (only when publish=True AND the guard passes the final
    text) flip published. Fail-closed everywhere; never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT slug, title, body, published
                           FROM press_releases WHERE id = %s""", (row_id,))
            row = cur.fetchone()
        if not row:
            return {"status": "not_found", "id": row_id}
        slug, title, body, published = row
        if published:
            return {"status": "already_published", "slug": slug}

        combined = f"{title or ''}\n{body or ''}"
        if _verify(combined).get("ok"):
            result = {"status": "passing", "slug": slug, "changes": []}
        else:
            rep = repair_text(combined)
            if not rep["ok"]:
                return {"status": "blocked", "slug": slug,
                        "changes": rep["changes"],
                        "unverified": (rep["report"].get("unverified") or [])[:10]}
            new_title, _, new_body = rep["text"].partition("\n")
            new_title = new_title.strip() or title  # never blank the title
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE press_releases SET title = %s, body = %s
                    WHERE id = %s AND published = FALSE
                """, (new_title[:300], new_body, row_id))
            # final safety: verify what was actually SAVED
            if not _verify(f"{new_title}\n{new_body}").get("ok"):
                return {"status": "blocked", "slug": slug,
                        "changes": rep["changes"],
                        "unverified": [{"claim": "(post-save verify failed)"}]}
            result = {"status": "repaired", "slug": slug,
                      "changes": rep["changes"], "title": new_title}

        if publish:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE press_releases
                    SET published = TRUE, published_at = NOW(),
                        published_date = COALESCE(published_date, to_char(NOW(), 'YYYY-MM-DD')),
                        date = COALESCE(date, to_char(NOW(), 'YYYY-MM-DD'))
                    WHERE id = %s AND published = FALSE
                    RETURNING slug
                """, (row_id,))
                result["published"] = bool(cur.fetchone())
        return result
    except Exception as e:
        logger.warning("[draft_repair] auto_repair failed: %s", str(e)[:160])
        return {"status": "error", "error": str(e)[:160]}


def annotate_pending_rows(conn, rows: list[dict], repair: bool = False) -> None:
    """Digest wiring: tag each pending press-draft row with its fact-check
    state so blocked drafts surface with a repair link, never a dead end.
    repair=True additionally SAVES auto-repairs (drafts stay unpublished, so
    they re-surface in the digest as passing). Never raises."""
    for r in rows or []:
        try:
            rid = r.get("id")
            if not rid:
                continue
            if repair:
                res = auto_repair_press_draft(conn, rid, publish=False)
                r["fact_check"] = {"passing": "passing",
                                   "repaired": "auto_repaired"}.get(
                                       res.get("status"), res.get("status"))
                if res.get("status") == "repaired" and res.get("title"):
                    r["title"] = res["title"]
                if res.get("status") == "blocked":
                    r["fact_check_claims"] = [
                        str(u.get("claim") or "") for u in (res.get("unverified") or [])][:4]
            else:
                with conn.cursor() as cur:
                    cur.execute("SELECT title, body FROM press_releases WHERE id = %s "
                                "AND published = FALSE", (rid,))
                    row = cur.fetchone()
                if not row:
                    continue
                rep = repair_text(f"{row[0] or ''}\n{row[1] or ''}")
                if rep["ok"] and not rep["changes"]:
                    r["fact_check"] = "passing"
                elif rep["ok"]:
                    r["fact_check"] = "repairable"
                else:
                    r["fact_check"] = "blocked"
                    r["fact_check_claims"] = [
                        str(u.get("claim") or "")
                        for u in (rep["report"].get("unverified") or [])][:4]
        except Exception as e:
            logger.warning("[draft_repair] annotate failed for %s: %s",
                           r.get("id"), str(e)[:120])


# ── HMAC-gated one-click endpoint (same token scheme as approve) ─────────
def _page(title: str, body_html: str, status: int = 200) -> "Response":
    return Response(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
background:#f1f5f9;margin:0;padding:24px">
<div style="max-width:680px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;
border-radius:8px;padding:26px 30px;line-height:1.6;color:#0f172a">
{body_html}
</div></body></html>""", status=status, mimetype="text/html")


if media_draft_repair_bp is not None:

    @media_draft_repair_bp.route("/api/v1/media/pending-drafts/repair",
                                 methods=["GET", "POST"])
    def repair_pending_draft():
        """One-click "auto-repair & retry" from the blocked-approve page.
        Auth = the SAME per-row HMAC token as the approve link (or admin key).
        Repairs the draft per the guard's hints, re-runs the guard, and
        publishes ONLY if the repaired text passes — the guard stays final."""
        import hmac as _hmac
        from routes.media_pending_digest import (_admin_ok, _approve_token,
                                                 _conn, SITE)
        try:
            row_id = int(request.args.get("id", ""))
        except Exception:
            return jsonify(error="bad_id"), 400
        token = (request.args.get("t") or "").strip()
        if not (_admin_ok() or (token and _hmac.compare_digest(
                token, _approve_token(row_id)))):
            return jsonify(error="unauthorized"), 401

        c = _conn()
        if c is None:
            return jsonify(error="no_database"), 503
        try:
            c.autocommit = True
            res = auto_repair_press_draft(c, row_id, publish=True)
        finally:
            try:
                c.close()
            except Exception:
                pass

        wants_html = request.method == "GET" and \
            "text/html" in (request.headers.get("Accept") or "")
        status = res.get("status")
        if status in ("passing", "repaired") and res.get("published"):
            if not wants_html:
                return jsonify(ok=True, **res), 200
            changes = "".join(f"<li>{html.escape(ch)}</li>"
                              for ch in res.get("changes") or []) or \
                "<li>no changes needed — the draft already passed</li>"
            return _page("Repaired & published", f"""
<h2 style="margin-top:0;color:#166534">Fact-check passed — published</h2>
<p>Draft <strong>{html.escape(str(res.get('slug') or row_id))}</strong> now
passes the fact-check guard and is live.</p>
<ul>{changes}</ul>
<p><a href="{SITE}/news/{html.escape(str(res.get('slug') or ''))}"
style="color:#1d4ed8">View the published release →</a></p>""")
        if status == "already_published":
            return (jsonify(ok=True, **res), 200) if not wants_html else \
                _page("Already published",
                      f"<p>This draft is already live: <a href=\"{SITE}/news/"
                      f"{html.escape(str(res.get('slug') or ''))}\">view it</a>.</p>")
        if status == "blocked":
            if not wants_html:
                return jsonify(ok=False, blocked_by="fact_check_guard", **res), 409
            claims = "".join(
                f"<li><code>{html.escape(cl.get('claim') if isinstance(cl, dict) else str(cl))}</code></li>"
                for cl in res.get("unverified") or []) or "<li>(unknown)</li>"
            return _page("Auto-repair could not corroborate", f"""
<h2 style="margin-top:0;color:#b91c1c">Auto-repair could not fully corroborate this draft</h2>
<p>The fact-check guard still refuses after repair — the draft stays
UNPUBLISHED (the guard is never bypassed). Remaining claims:</p>
<ul>{claims}</ul>
<p>Edit the draft body to remove or source these claims, then use the
"Fact-check &amp; publish" link again.</p>""", status=409)
        return (jsonify(ok=False, **res),
                404 if status == "not_found" else 500)
