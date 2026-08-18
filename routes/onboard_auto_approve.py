"""
routes/onboard_auto_approve.py — VALIDATED auto-approve lane (2026-07-18).
==========================================================================

r-white-glove BUILD 2: bring new partners on automatically.

Today routes/agent_self_register.py queues submissions and
routes/ai_platform_onboarder.py processes them: fit ≥85 auto-approves
(rare — the rubric maxes most legit-but-small platforms in the 70-84
band), 70-84 lands in 'pending_review' (the human queue), <70 is
'rejected_low_fit'. In practice almost everything waits on a human.

This module adds a VALIDATED lane the onboarder cron consults for rows
that would otherwise sit in the human queue. A submission auto-approves
IFF every check passes:

  · reachable HTTPS url (scheme must be https AND the onboarder's
    reachability probe succeeded)
  · non-spam name (simple deterministic heuristics — length, letter
    ratio, no embedded URLs, no spam keywords, no shouting/repeat runs)
  · real contact email format (RFC-lite + not a disposable/test domain)
  · not a duplicate platform (name or URL host already connected or
    approved)

On approve, the lane delivers the full onboarding package:
  1. WORKING developer key minted via the EXISTING path
     (routes.partner_key_issuer._issue_internal — the same dual-write
     the self-register auto-approve uses).
  2. A generated /integrations/<slug> stub page — built with
     routes.integrations_landing._recipe_page (the same template
     machinery behind the Grok/Mistral/Perplexity recipes), generic
     copy-paste MCP config + the platform's name, stored in
     integration_stub_pages and served by this blueprint.
  3. A tool-tuner seed proposal row in
     mcp_tool_descriptions_per_platform (generated_by
     'auto_onboard_seed') so the next tuner reseed picks the platform
     up.
  4. Welcome email — the EXISTING Resend onboarding path
     (ai_platform_onboarder._send_confirmation with the auto_approved
     copy) fires as part of the normal cron flow; no new send path.

Anything failing validation stays in the human queue EXACTLY as today
(status 'pending_review', untouched). Garbage (<70 fit) never reaches
this lane at all.

Safety:
  · Kill switch: ONBOARD_AUTO_APPROVE_DISABLE=1 → lane is a no-op.
  · Hard cap: 3 auto-approvals per UTC day
    (ONBOARD_AUTO_APPROVE_DAILY_CAP), counted on
    approved_by='auto_validated' rows.
  · Approvals stamped approved_by='auto_validated' so they are
    distinguishable from the fit≥85 'auto_onboarder' lane and from
    manual approvals — auditable + revocable as a cohort.
  · Pure validators (unit-tested, zero-false-approve tolerance in
    tests/test_onboard_auto_approve.py). When in doubt → human queue.

Public route:
  GET /integrations/<slug> — serves a stored stub page. Unknown slugs
  308-redirect to /integrations/<slug>/ so the legacy static
  integration-package route in main.py keeps working unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import re
from html import escape as _esc

from flask import Blueprint, jsonify, redirect
from ai_surface_canon import canon_text
_CANON_FAC = canon_text("{canon_facilities}")

logger = logging.getLogger(__name__)

onboard_auto_approve_bp = Blueprint("onboard_auto_approve", __name__)

KILL_SWITCH_ENV = "ONBOARD_AUTO_APPROVE_DISABLE"
DAILY_CAP = int(os.environ.get("ONBOARD_AUTO_APPROVE_DAILY_CAP", "3"))
APPROVED_BY = "auto_validated"


def _disabled() -> bool:
    return (os.environ.get(KILL_SWITCH_ENV) or "").strip().lower() in (
        "1", "true", "yes")


# ── Pure validators (unit-tested; deterministic; no I/O) ─────────────
_HTTPS_URL_RE = re.compile(
    r"^https://[A-Za-z0-9._\-]+(?:\.[A-Za-z0-9._\-]+)+(?::\d{2,5})?(?:/[^\s]*)?$")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")

# Disposable / test domains — a REAL contact address is part of the bar.
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com",
    "tempmail.com", "temp-mail.org", "yopmail.com", "sharklasers.com",
    "trashmail.com", "getnada.com", "dispostable.com", "maildrop.cc",
    "example.com", "example.org", "test.com", "email.com", "asdf.com",
}
_FAKE_LOCAL_PARTS = {"test", "asdf", "qwerty", "abc", "aaa", "xxx",
                     "noreply", "no-reply", "fake", "spam", "none"}

SPAM_NAME_KEYWORDS = (
    "casino", "viagra", "cialis", "porn", "xxx", "escort", "betting",
    "free money", "get rich", "loan", "payday", "forex signal",
    "pump", "airdrop", "giveaway", "click here", "seo service",
    "backlink", "followers", "essay writing",
)


def name_spam_reasons(name: str) -> list[str]:
    """Deterministic spam heuristics for a platform name. Empty list ⇒
    the name looks legitimate. Any reason ⇒ human queue."""
    reasons: list[str] = []
    n = (name or "").strip()
    if len(n) < 3:
        reasons.append("name_too_short")
        return reasons
    if len(n) > 80:
        reasons.append("name_too_long")
    low = n.lower()
    if "http://" in low or "https://" in low or "www." in low or ".com" in low:
        reasons.append("name_contains_url")
    for kw in SPAM_NAME_KEYWORDS:
        if kw in low:
            reasons.append(f"name_spam_keyword:{kw}")
            break
    letters = sum(1 for ch in n if ch.isalpha())
    if letters < max(2, len(n) // 3):
        reasons.append("name_low_letter_ratio")
    digits = sum(1 for ch in n if ch.isdigit())
    if digits > 4:
        reasons.append("name_too_many_digits")
    if re.search(r"(.)\1{4,}", n):
        reasons.append("name_repeated_char_run")
    if len(n) >= 12 and n.isupper():
        reasons.append("name_all_caps_shouting")
    if re.search(r"[$€£¥]{1,}", n):
        reasons.append("name_currency_symbols")
    return reasons


def valid_https_url(url: str) -> bool:
    return bool(_HTTPS_URL_RE.match((url or "").strip()))


def valid_contact_email(email: str) -> bool:
    e = (email or "").strip().lower()
    if not _EMAIL_RE.match(e):
        return False
    local, _, domain = e.partition("@")
    if domain in DISPOSABLE_EMAIL_DOMAINS:
        return False
    if local in _FAKE_LOCAL_PARTS:
        return False
    if re.fullmatch(r"(.)\1*", local):   # aaaa@… / xxxx@…
        return False
    return True


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _url_host(url: str) -> str:
    m = re.match(r"^https?://([^/:?#]+)", (url or "").strip().lower())
    host = m.group(1) if m else ""
    return host[4:] if host.startswith("www.") else host


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]


def validate_submission(sub: dict, existing_names=(), existing_hosts=(),
                        reachable: bool = False) -> tuple[bool, list[str]]:
    """Pure validation gate. Returns (ok, failure_reasons).

    ZERO-FALSE-APPROVE CONTRACT: a submission missing ANY requirement
    returns ok=False — the caller leaves it in the human queue. Tested
    in tests/test_onboard_auto_approve.py."""
    reasons: list[str] = []
    url = (sub.get("url") or "").strip()
    if not valid_https_url(url):
        reasons.append("url_not_https_or_malformed")
    if not reachable:
        reasons.append("url_not_reachable")
    reasons.extend(name_spam_reasons(sub.get("name") or ""))
    if not valid_contact_email(sub.get("contact_email") or ""):
        reasons.append("contact_email_invalid_or_disposable")
    norm = _norm_name(sub.get("name") or "")
    if norm and norm in {_norm_name(x) for x in existing_names}:
        reasons.append("duplicate_platform_name")
    host = _url_host(url)
    if host and host in {h.lower() for h in existing_hosts if h}:
        reasons.append("duplicate_platform_url_host")
    return (len(reasons) == 0, reasons)


# ── DB-backed pieces ─────────────────────────────────────────────────
def _existing_platform_sets(c) -> tuple[set, set]:
    """(names, url_hosts) of platforms already connected or approved —
    the duplicate gate. Fail-CLOSED: on DB error return sentinel sets
    that force the duplicate check to be inconclusive → human queue."""
    names: set = set()
    hosts: set = set()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT name, url FROM connected_ai_platforms")
            for name, url in cur.fetchall():
                if name:
                    names.add(name)
                if url:
                    hosts.add(_url_host(url))
            cur.execute(
                "SELECT name, url FROM ai_platform_submissions "
                "WHERE status IN ('approved', 'auto_approved')")
            for name, url in cur.fetchall():
                if name:
                    names.add(name)
                if url:
                    hosts.add(_url_host(url))
    except Exception as e:
        logger.warning("[auto-approve] duplicate-set read failed: %s", e)
        try:
            c.rollback()
        except Exception:
            pass
        return None, None   # caller treats as validation failure
    return names, hosts


def _todays_auto_approvals(c) -> int | None:
    """Count of validated-lane approvals so far this UTC day. None on
    error (caller fails closed)."""
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ai_platform_submissions "
                "WHERE approved_by = %s "
                "AND approved_at >= date_trunc('day', NOW() AT TIME ZONE 'utc')",
                (APPROVED_BY,))
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        logger.warning("[auto-approve] daily-cap read failed: %s", e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


# ── Stub page (reuses integrations_landing._recipe_page machinery) ───
def build_stub_html(platform_name: str, slug: str) -> str:
    """Generic per-partner connect page: copy-paste MCP config + the
    platform's name, rendered through the SAME _recipe_page template
    the Grok/Mistral/Perplexity recipes use."""
    from routes.integrations_landing import _recipe_page
    name = _esc((platform_name or "your platform").strip()[:80])
    return _recipe_page(
        slug=slug,
        title=f"Connect DC Hub to {name} — MCP server for live data-center "
              f"&amp; grid intelligence",
        description=f"Connect DC Hub to {name} as an MCP integration: point "
                    f"it at https://dchub.cloud/mcp (streamable HTTP). Live "
                    f"grid scoreboards, {_CANON_FAC} data-center facilities, "
                    f"interconnection queues and tracked deals. Free tier: "
                    f"10 calls/day, no signup.",
        og_title=f"Connect DC Hub to {name} — MCP in minutes",
        og_desc="Live grid + data-center intelligence · one MCP URL · "
                "free tier no signup",
        jsonld_altname=f"DC Hub for {name}",
        jsonld_desc=f"Model Context Protocol server that connects to {name} "
                    f"— live grid scoreboards, {_CANON_FAC} data-center "
                    f"facilities, interconnection queues, fiber intelligence "
                    f"and tracked deals, with per-response citations. Free "
                    f"tier: 10 calls/day, no signup.",
        eyebrow=f"{name} · Model Context Protocol",
        h1=f"Connect DC Hub to {name}.",
        lead=f"Give {name} live, citable data-center and power-grid "
             f"intelligence — real-time grid scoreboards, {_CANON_FAC} "
             f"facilities, interconnection queues, tracked deal flow. "
             f"One URL.",
        steps_heading=f"Connect in {name}",
        steps_html=f"""<ol>
    <li>Copy the endpoint above: <code>https://dchub.cloud/mcp</code>.</li>
    <li>In {name}, open the MCP / connector / integrations settings and add a <b>remote MCP server</b> (transport: streamable HTTP).</li>
    <li>Paste the URL. Auth is optional — leave it blank for the keyless free tier, or send <code>Authorization: Bearer &lt;your-dchub-key&gt;</code>.</li>
    <li>If {name} uses a JSON config file, this block works verbatim:</li>
  </ol>
  <pre>{{
  "dchub": {{
    "transport": "streamable-http",
    "url": "https://dchub.cloud/mcp"
  }}
}}</pre>
  <p>Then ask the assistant: <i>"Use DC Hub — which US grid has the most headroom right now?"</i> and confirm a <code>get_grid_scoreboard</code> call fires.</p>""",
        auth_html="""<div class="pane">
  <h2>Authentication</h2>
  <p>Optional. DC Hub accepts <code>Authorization: Bearer &lt;your-dchub-key&gt;</code> and
  <code>X-API-Key</code>. No key? The keyless free tier gives 10 calls/day — or ask the assistant to call
  <code>claim_free_key</code> for a durable free key with higher limits.</p>
</div>""",
        extra_html="",
    )


def _ensure_stub_table(c) -> None:
    with c.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS integration_stub_pages ("
            " slug TEXT PRIMARY KEY,"
            " platform_name TEXT NOT NULL,"
            " html TEXT NOT NULL,"
            " submission_id INTEGER,"
            " generated_by TEXT DEFAULT 'auto_onboard',"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")


def store_stub_page(c, slug: str, platform_name: str, html: str,
                    submission_id: int | None) -> bool:
    try:
        _ensure_stub_table(c)
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO integration_stub_pages "
                "(slug, platform_name, html, submission_id) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (slug) DO UPDATE SET "
                " platform_name = EXCLUDED.platform_name,"
                " html = EXCLUDED.html,"
                " submission_id = EXCLUDED.submission_id,"
                " updated_at = NOW()",
                (slug, platform_name, html, submission_id))
        try:
            c.commit()
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("[auto-approve] stub store failed for %s: %s", slug, e)
        try:
            c.rollback()
        except Exception:
            pass
        return False


def load_stub_html(slug: str):
    """DB lookup for the public route. None when missing/unavailable.
    Split out so tests can monkeypatch it."""
    c = None
    try:
        from main import get_db
        c = get_db()
        if c is None:
            return None
        with c.cursor() as cur:
            cur.execute("SELECT html FROM integration_stub_pages "
                        "WHERE slug = %s", (slug,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        try:
            if c is not None:
                c.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            if c is not None:
                c.close()
        except Exception:
            pass


# ── Tool-tuner seed proposal row ─────────────────────────────────────
def seed_tool_tuner_proposal(c, slug: str, platform_name: str) -> bool:
    """One seed proposal row in mcp_tool_descriptions_per_platform so
    the tuner's coverage view + next reseed pick the platform up. Uses
    the tuner's own _ensure_table/_upsert (idempotent ON CONFLICT)."""
    try:
        from routes.ai_platform_tool_tuner import (
            GENERIC_DESCRIPTIONS, _ensure_table, _upsert)
        _ensure_table(c)
        generic = GENERIC_DESCRIPTIONS.get(
            "search_facilities",
            canon_text("Search {canon_facilities} global data-center facilities."))
        _upsert(c, slug, "search_facilities",
                f"[seed proposal · {platform_name.strip()[:60]}] {generic}"[:280],
                "auto_onboard_seed")
        return True
    except Exception as e:
        logger.warning("[auto-approve] tuner seed failed for %s: %s", slug, e)
        return False


# ── Live key mint (EXISTING path) ────────────────────────────────────
def mint_live_key(slug: str, email: str, platform_name: str) -> dict:
    try:
        from routes.partner_key_issuer import _issue_internal
        res = _issue_internal(
            partner_slug=slug,
            target_email=email,
            plan="developer",
            company=platform_name,
            name=platform_name,
            label=f"auto-onboard: {platform_name}"[:80],
            issued_by="onboard_auto_approve",
        )
        return res if isinstance(res, dict) else {"ok": False,
                                                  "error": "bad_response"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}


# ── The lane (called from ai_platform_onboarder._process_one) ────────
def try_auto_approve_lane(c, submission: dict, meta: dict,
                          reachable: bool) -> dict:
    """Decide + (on pass) deliver the auto-onboarding package for one
    'pending_review'-bound submission. NEVER raises. Returns
    {approved: bool, reasons: […], …package details}."""
    out: dict = {"approved": False, "lane": "auto_validated"}
    try:
        if _disabled():
            out["reasons"] = [f"{KILL_SWITCH_ENV}=1"]
            return out
        if c is None:
            out["reasons"] = ["no_db"]
            return out
        # Daily cap — fail CLOSED on unknown count.
        used = _todays_auto_approvals(c)
        if used is None or used >= DAILY_CAP:
            out["reasons"] = [
                f"daily_cap_reached({used}/{DAILY_CAP})" if used is not None
                else "daily_cap_unknown"]
            return out
        names, hosts = _existing_platform_sets(c)
        if names is None:
            out["reasons"] = ["duplicate_check_unavailable"]
            return out
        # Exclude the submission's own prior row echoes: the sets only
        # contain approved/connected rows, so a pending row never
        # self-collides.
        ok, reasons = validate_submission(
            submission, existing_names=names, existing_hosts=hosts,
            reachable=bool(reachable))
        if not ok:
            out["reasons"] = reasons
            return out

        name = (submission.get("name") or "").strip()
        email = (submission.get("contact_email") or "").strip().lower()
        slug = slugify(name) or f"platform-{submission.get('id')}"

        # 1) live developer key (existing dual-write mint path)
        key_res = mint_live_key(slug, email, name)
        out["key_minted"] = bool(key_res.get("ok"))
        out["key_prefix"] = (key_res.get("key_prefix")
                             or (key_res.get("key") or "")[:12] or None)
        if not key_res.get("ok"):
            # No credential → the promise of "approved" would be hollow;
            # leave it for the human queue instead.
            out["reasons"] = [f"key_mint_failed:{key_res.get('error')}"]
            return out

        # 2) /integrations/<slug> stub page
        try:
            html = build_stub_html(name, slug)
            out["stub_stored"] = store_stub_page(
                c, slug, name, html, submission.get("id"))
        except Exception as e:
            out["stub_stored"] = False
            out["stub_error"] = str(e)[:150]
        out["stub_slug"] = slug
        out["integration_url"] = f"https://dchub.cloud/integrations/{slug}"

        # 3) tool-tuner seed proposal row
        out["tuner_seeded"] = seed_tool_tuner_proposal(c, slug, name)

        # 4) welcome email: sent by the EXISTING onboarder Resend path —
        # _process_one fires _send_confirmation with the auto_approved
        # copy once this lane flips the status. No new send path.
        out["approved"] = True
        out["reasons"] = ["validated: https+reachable, name, email, "
                          "not-duplicate", f"cap {used + 1}/{DAILY_CAP}"]
        return out
    except Exception as e:
        logger.warning("[auto-approve] lane crashed (fail-closed): %s", e)
        out["reasons"] = [f"lane_error:{type(e).__name__}"]
        return out


# ── Public stub route ────────────────────────────────────────────────
@onboard_auto_approve_bp.route("/integrations/<slug>", methods=["GET"])
def serve_stub(slug: str):
    """Serve a generated partner stub page. Unknown slug → 308 to the
    trailing-slash path so main.py's static integration-package route
    (and its 404) behave exactly as before this lane existed."""
    s = (slug or "").strip().lower()
    if re.fullmatch(r"[a-z0-9\-]{1,60}", s):
        html = load_stub_html(s)
        if html:
            return html, 200, {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "public, max-age=600, s-maxage=1800",
            }
    return redirect(f"/integrations/{slug}/", code=308)


@onboard_auto_approve_bp.route("/api/v1/onboard/auto-approve/config",
                               methods=["GET"])
def lane_config():
    """Public, secret-free introspection of the lane posture."""
    return jsonify(
        ok=True,
        lane="auto_validated",
        disabled=_disabled(),
        daily_cap=DAILY_CAP,
        requirements=[
            "reachable https url",
            "non-spam platform name (deterministic heuristics)",
            "real contact email (format + non-disposable)",
            "not a duplicate of a connected/approved platform",
        ],
        note="Submissions failing any check stay in the human review "
             "queue exactly as before.",
    ), 200


def register_onboard_auto_approve(app) -> None:
    app.register_blueprint(onboard_auto_approve_bp)
    logger.info("onboard_auto_approve registered: GET /integrations/<slug> "
                "(stub pages) · GET /api/v1/onboard/auto-approve/config")
