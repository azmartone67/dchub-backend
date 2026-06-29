"""media_reactive_news.py — REACTIVE-NEWS LANE / "master shell" (2026-06-28).
=============================================================================

Turns the one-off "CBRE Q1 → time-to-power reframe" into a REPEATABLE engine:
when a major industry report drops (CBRE / JLL / Cushman / Morgan Stanley /
Synergy / DataCenterHawk / DCD …), take its headline number, reframe it against
DC Hub's LIVE grid/DCPI data ("their number → our number → so-what"), and DROP
THE DRAFT INTO A REVIEW QUEUE for the operator. It never sends or auto-publishes
anything live (unless the operator explicitly opts in — see autopost flag below).

This is the same safety architecture as routes/media_data_story_factory.py — by
design, not by accident — and ships all three phases as ONE dark module:

  • PHASE 0  POST /api/v1/media/reactive-news/react   (operator-seeded)
        Operator hands in {market, claim, source[, source_url]}. The lane
        resolves the market → reads live DCPI → drafts the reframe → guards →
        queues. Productizes exactly the hand-built CBRE post.

  • PHASE 1  POST /api/v1/media/reactive-news/scan    (auto-detect)
        Scans the Postgres news_articles feed for items from the analyst-firm
        allowlist in a recent window, extracts the headline number + market,
        and reacts to each (same draft→guard→queue path). De-duped on URL.
        Activate by pointing a cron at this endpoint.

  • PHASE 2  reactive_news_leads()                    (editorial integration)
        Importable collector that exposes queued reactive drafts as standard
        editorial leads (kind="reactive") so they can compete in
        media_editorial.rank_data_events(). Wire-in is one line (see bottom).

SAFETY (identical posture to the data-story factory):
  (1) DARK BY DEFAULT — gated on MEDIA_REACTIVE_NEWS_ENABLED. Flag off (default)
      => every endpoint returns {"ok": true, "skipped": "disabled"} and the
      module does NOTHING: no DB writes, no LLM calls, no detection.
  (2) NO AUTO-PUBLISH — /react and /scan only DRAFT + QUEUE (status='queued').
      /approve/<id> records a human decision. It only crosses into the live
      LinkedIn publisher if the operator ALSO sets MEDIA_REACTIVE_AUTOPOST_ON_
      APPROVE=1 (default OFF), in which case approve enqueues an *approved*
      social_media_posts row for the existing publisher. Default: approve is a
      decision record only; the operator pushes via the normal flow.
  (3) GUARD GAUNTLET on every draft BEFORE queueing:
        - content_publisher._should_skip_publish (partner-disparagement /
          LLM-disclaimer / quality / zero-stat)
        - routes.media_claim_verify.verify_claims (over-claimed / retired #s)
        - routes.media_fact_check_guard.verify_media_text (if present)
        - _analyst_respect_ok — REACTIVE-LANE-SPECIFIC: hard-blocks any draft
          that disparages the cited source. These firms are our DATA SOURCES
          and channel partners; the play is to BUILD ON their authority, never
          dunk on it. External numbers are attributed ("per CBRE"), never
          asserted as ours.
  (4) VERIFIED NUMBERS ONLY for OUR claims — every DC Hub figure comes from a
      live read of market_power_scores. The external figure is carried as an
      attributed quote, never re-asserted as DC Hub data, then verify_claims
      re-checks the composed text.
  (5) SELF-IMPORT-SAFE — every optional import is wrapped; flag-off + missing
      deps can NEVER crash boot.

Kill switches:
  MEDIA_REACTIVE_NEWS_ENABLED          master (OFF unless 1/true/yes/on)
  MEDIA_REACTIVE_AUTOPOST_ON_APPROVE   approve→live publisher (default OFF)
"""
from __future__ import annotations

import os
import re
import json
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify

logger = logging.getLogger("media_reactive_news")

media_reactive_news_bp = Blueprint("media_reactive_news", __name__)

# ── optional imports (NEVER let a missing dep break import / app boot) ───────
try:
    import psycopg2 as _pg
    import psycopg2.extras as _pg_extras
except Exception:                                   # pragma: no cover
    _pg = None
    _pg_extras = None

try:
    import urllib.request as _urlreq
except Exception:                                   # pragma: no cover
    _urlreq = None

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Phase-1 scan tuning (conservative so the queue stays high-signal).
_SCAN_WINDOW_HOURS = int(os.environ.get("MEDIA_REACTIVE_SCAN_WINDOW_HOURS", "24"))
_SCAN_MIN_RELEVANCE = float(os.environ.get("MEDIA_REACTIVE_MIN_RELEVANCE", "0.55"))
_SCAN_MAX_PER_RUN = int(os.environ.get("MEDIA_REACTIVE_MAX_PER_RUN", "8"))
_LEAD_SCORE = float(os.environ.get("MEDIA_REACTIVE_LEAD_SCORE", "22"))

# Analyst / CRE-research firms we react TO (and must cite respectfully). Kept in
# sync with the vocabulary already used in routes/ai_citation_tracker.py.
ANALYST_FIRMS = (
    "CBRE", "JLL", "Cushman", "Cushman & Wakefield", "Newmark", "Savills",
    "Knight Frank", "Morgan Stanley", "Goldman Sachs", "Goldman",
    "Synergy Research", "Synergy", "DataCenterHawk", "Datacenter Hawk",
    "datacenterHawk", "DCD", "Data Center Dynamics", "DataCenter Dynamics",
    "DCF", "Data Center Frontier", "Structure Research", "Dell'Oro",
    "Omdia", "Gartner", "McKinsey", "Bain", "BCG",
)
_ANALYST_FIRMS_RE = re.compile(
    r"\b(" + "|".join(re.escape(f) for f in ANALYST_FIRMS) + r")\b", re.I
)


# ── kill switches ────────────────────────────────────────────────────────────
def _enabled() -> bool:
    """DARK BY DEFAULT. OFF unless the master flag is explicitly on."""
    return os.environ.get("MEDIA_REACTIVE_NEWS_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _autopost_on_approve() -> bool:
    """Whether /approve also enqueues an approved social_media_posts row for the
    live LinkedIn publisher. Default OFF — approve is a decision record only."""
    return os.environ.get("MEDIA_REACTIVE_AUTOPOST_ON_APPROVE", "").strip().lower() in (
        "1", "true", "yes", "on")


def _skipped_response():
    return jsonify({"ok": True, "skipped": "disabled"})


# ── admin gate (mirrors media_data_story_factory._admin_ok) ──────────────────
def _admin_ok() -> bool:
    _keys = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    if not _keys:
        return False
    _sent = (request.headers.get("X-Internal-Key")
             or request.headers.get("X-Admin-Key")
             or request.args.get("admin_key") or "").strip()
    return bool(_sent) and _sent in _keys


# ── DB ───────────────────────────────────────────────────────────────────────
def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _connect():
    """Best-effort connection. Returns None on any failure (caller degrades)."""
    if not _pg or not _dsn():
        return None
    try:
        try:
            from main import get_pg_connection
            return get_pg_connection()
        except Exception:
            return _pg.connect(_dsn(), connect_timeout=8)
    except Exception as e:                            # pragma: no cover
        logger.warning("[reactive] DB connect failed: %s", str(e)[:160])
        return None


def _ensure_tables(conn) -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS for the reactive review queue.
    Degrades on failure; never crashes (only called inside a flag-gated req)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_reactive_queue (
                    id              BIGSERIAL PRIMARY KEY,
                    source          TEXT,
                    source_url      TEXT,
                    external_claim  TEXT,
                    market_slug     TEXT,
                    market_name     TEXT,
                    our_data        JSONB,
                    post_draft      TEXT,
                    status          TEXT NOT NULL DEFAULT 'queued',
                    reject_reason   TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    reviewed_at     TIMESTAMPTZ,
                    reviewed_by     TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_reactive_queue_status_at
                    ON media_reactive_queue (status, created_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_reactive_queue_source_url
                    ON media_reactive_queue (source_url)
            """)
            # advisory guard notes (added 2026-06-28) — backfill existing tables
            cur.execute("ALTER TABLE media_reactive_queue "
                        "ADD COLUMN IF NOT EXISTS guard_warnings TEXT")
        conn.commit()
    except Exception as e:
        logger.warning("[reactive] _ensure_tables failed: %s", str(e)[:200])
        try:
            conn.rollback()
        except Exception:
            pass


def _num(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


# ── market-name → our slug resolver ──────────────────────────────────────────
# Small alias map for the phrasings reports use; everything else falls through
# to a DB match against market_power_scores.market_name.
# Keys are stored in normalized form (see _norm: lowercased, punctuation -> space,
# whitespace collapsed) so "Dallas-Ft. Worth" and "Northern Virginia" match.
_MARKET_ALIASES = {
    "northern virginia": "northern-virginia",
    "n virginia": "northern-virginia",
    "nova": "northern-virginia",
    "ashburn": "northern-virginia",
    "loudoun": "northern-virginia",
    "loudoun county": "northern-virginia",
    "dallas fort worth": "dallas",
    "dallas ft worth": "dallas",
    "dfw": "dallas",
    "dallas": "dallas",
    "silicon valley": "silicon-valley",
    "santa clara": "silicon-valley",
    "bay area": "silicon-valley",
    "the dalles": "the-dalles",
    "salt lake": "salt-lake-city",
    "nyc": "new-york",
    "new york city": "new-york",
}


def _norm(s: str) -> str:
    """Lowercase, punctuation -> space, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def _resolve_market_slug(cur, free_text: str) -> tuple[str | None, str | None]:
    """Map a free-text market name to (market_slug, market_name). Returns
    (None, None) if nothing matches. Alias map first, then DB exact/substring."""
    if not free_text:
        return None, None
    norm = _norm(free_text)
    if norm in _MARKET_ALIASES:
        slug = _MARKET_ALIASES[norm]
        # confirm against DB to also get the canonical name
        try:
            cur.execute(
                "SELECT market_slug, market_name FROM market_power_scores "
                "WHERE market_slug = %s LIMIT 1", (slug,))
            r = cur.fetchone()
            if r:
                return (r.get("market_slug"), r.get("market_name"))
        except Exception:
            pass
        return slug, free_text.strip()
    # DB match: exact (case-insensitive) on market_name, then substring.
    try:
        cur.execute("""
            SELECT market_slug, market_name
              FROM market_power_scores
             WHERE market_slug IS NOT NULL
               AND (LOWER(market_name) = %s OR LOWER(market_slug) = %s)
             LIMIT 1
        """, (norm, norm.replace(" ", "-")))
        r = cur.fetchone()
        if r:
            return (r.get("market_slug"), r.get("market_name"))
        # substring fallback — only if it's unambiguous (exactly one hit).
        cur.execute("""
            SELECT market_slug, market_name
              FROM market_power_scores
             WHERE market_slug IS NOT NULL
               AND LOWER(market_name) LIKE %s
             LIMIT 2
        """, (f"%{norm}%",))
        rows = cur.fetchall() or []
        if len(rows) == 1:
            return (rows[0].get("market_slug"), rows[0].get("market_name"))
    except Exception as e:
        logger.warning("[reactive] slug resolve failed: %s", str(e)[:160])
    return None, None


def _live_dcpi(cur, slug: str) -> dict:
    """Verified live DCPI read for ONE market. {} if not found."""
    try:
        cur.execute("""
            SELECT market_slug, market_name, verdict,
                   excess_power_score, constraint_score, time_to_power_months
              FROM market_power_scores
             WHERE market_slug = %s
             LIMIT 1
        """, (slug,))
        r = cur.fetchone()
        if not r:
            return {}
        return {
            "market_slug": r.get("market_slug"),
            "market_name": r.get("market_name"),
            "verdict": r.get("verdict"),
            "excess_power_score": _num(r.get("excess_power_score")),
            "constraint_score": _num(r.get("constraint_score")),
            "time_to_power_months": _num(r.get("time_to_power_months")),
        }
    except Exception as e:
        logger.warning("[reactive] _live_dcpi failed: %s", str(e)[:160])
        return {}


# ── claim extraction (Phase 1 — pull the headline number from a report) ──────
_CLAIM_RE = re.compile(
    r"(\$?\d[\d,\.]*\s?(?:%|bps|basis points|GW|MW|kW|/kW-month|million|billion|"
    r"months?|years?|sq\s?ft|MWh))",
    re.I,
)


def _extract_claim(title: str, summary: str) -> str | None:
    """Best-effort headline-number extraction from a news item. Prefers the
    title; falls back to the summary. Returns None if no number-with-unit."""
    for src in (title or "", summary or ""):
        m = _CLAIM_RE.search(src)
        if m:
            # carry a little context around the number for the reframe.
            start = max(0, m.start() - 40)
            end = min(len(src), m.end() + 40)
            return src[start:end].strip()
    return None


# ── analyst-respect guard (REACTIVE-LANE-SPECIFIC) ───────────────────────────
# NOTE: deliberately excludes domain-neutral words that read as disparagement
# in isolation but are normal grid language ("behind on interconnection",
# "lagging the queue", "60-month explanation behind it"). Including them caused
# a live false-positive that rejected a perfectly respectful CBRE reframe.
_DISPARAGE_TERMS = (
    "wrong", "inaccurate", "outdated", "stale", "missed", "misses", "flawed",
    "failed", "fails", "clueless", "doesn't get",
    "does not get", "can't see", "cannot see", "blind to", "gets it wrong",
    "out of touch", "obsolete", "useless", "garbage", "nonsense",
)
_DISPARAGE_RE = re.compile(
    r"(" + "|".join(re.escape(t) for t in _DISPARAGE_TERMS) + r")", re.I
)


def _analyst_respect_ok(text: str) -> tuple[bool, str]:
    """Hard-block any draft that names an analyst firm within ~70 chars of a
    disparaging term (either order). The reactive lane builds ON these sources;
    it must never dunk on them. Returns (ok, reason)."""
    if not text:
        return True, ""
    for fm in _ANALYST_FIRMS_RE.finditer(text):
        a, b = fm.start(), fm.end()
        window = text[max(0, a - 70): min(len(text), b + 70)]
        d = _DISPARAGE_RE.search(window)
        if d:
            return False, (f"analyst-respect: '{fm.group(0)}' framed with "
                           f"'{d.group(0)}' — cite, don't disparage")
    return True, ""


# ── LLM drafting (verified DC Hub data in; external # attributed, not asserted) ─
_SYSTEM = (
    "You are the data-desk writer for DC Hub (dchub.cloud), an infrastructure-"
    "intelligence platform. You write ONE tight LinkedIn post that reacts to a "
    "third-party industry report by reframing it against DC Hub's live grid/DCPI "
    "data. ABSOLUTE RULES: "
    "(1) The EXTERNAL number is the report author's claim — ALWAYS attribute it "
    "to them by name ('per CBRE', 'JLL reports'); NEVER restate it as DC Hub "
    "data. (2) For DC Hub's own figures use ONLY the values in VERIFIED DC HUB "
    "DATA — never invent, round, or estimate any number, verdict, score, or "
    "timeline not present there. (3) BUILD ON the report — it is the demand/"
    "scarcity signal; DC Hub's grid/time-to-power data is the cause behind it. "
    "NEVER say the report is wrong, late, or flawed; never disparage the firm, "
    "any operator, utility, or partner. (4) Open with a number. Plain text, no "
    "markdown, 120-180 words, 3-4 short paragraphs. End with the DC Hub market "
    "URL provided. (5) If a fact isn't in VERIFIED DC HUB DATA or the attributed "
    "external claim, leave it out."
)


def _anthropic_url() -> str:
    try:
        from utils.anthropic_helper import anthropic_messages_url
        return anthropic_messages_url()
    except Exception:
        return "https://api.anthropic.com/v1/messages"


def _llm(system: str, user: str, max_tokens: int = 600) -> str | None:
    """RAW HTTP call mirroring media_data_story_factory._llm. None on failure."""
    if not ANTHROPIC_API_KEY or not _urlreq:
        return None
    try:
        body = json.dumps({
            "model": "claude-sonnet-4-5",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")
        req = _urlreq.Request(
            _anthropic_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": ANTHROPIC_API_KEY,
                "User-Agent": "dchub-brain/1.0",
                "Anthropic-Version": "2023-06-01",
            },
        )
        with _urlreq.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
        parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if text.startswith('"') and text.endswith('"') and len(text) > 10:
            text = text[1:-1].strip()
        return text or None
    except Exception as e:
        logger.warning("[reactive] LLM call failed: %s", str(e)[:200])
        return None


def _market_url(slug: str) -> str:
    return f"https://dchub.cloud/dcpi/{slug}" if slug else "https://dchub.cloud"


def _draft_reframe(external: dict, dcpi: dict) -> str | None:
    """Compose the reframe post. external = {source, claim, source_url};
    dcpi = verified live DCPI for the matched market."""
    payload = json.dumps({
        "external_report": {
            "source": external.get("source"),
            "their_claim": external.get("claim"),
        },
        "verified_dchub_data": {
            "market": dcpi.get("market_name"),
            "dcpi_verdict": dcpi.get("verdict"),
            "excess_power_score": dcpi.get("excess_power_score"),
            "constraint_score": dcpi.get("constraint_score"),
            "time_to_power_months": dcpi.get("time_to_power_months"),
        },
        "market_url": _market_url(dcpi.get("market_slug")),
        "as_of": datetime.utcnow().strftime("%Y-%m-%d"),
    }, default=str)
    user = (
        "Write the LinkedIn reaction post. Attribute the external claim to its "
        "source by name and reframe it against the verified DC Hub data (the "
        "report shows demand/scarcity; DC Hub's grid + time-to-power data is the "
        "cause). End with the market_url.\n\n"
        f"INPUT:\n{payload}"
    )
    return _llm(_SYSTEM, user, max_tokens=600)


# Publish-guard reasons treated as ADVISORY in the reactive lane (queue the draft
# with a warning instead of hard-rejecting): the LLM editor's verifiability calls
# + fact-check flags on attributed external numbers. These collide with the lane's
# premise — it carries a third party's number ("per CBRE, 1.8%") plus live DCPI
# figures the text can't self-verify — so a human reviewer adjudicates rather than
# an auto-reject. Disparagement and entity-dedup stay HARD (see _guard_check).
_ADVISORY_GUARD_SIGNALS = (
    "editor rejected", "unverifiable", "uncited", "citation",
    "fact-check", "cannot verify", "can't verify", "could not verify",
)


def _is_advisory_guard(why: str) -> bool:
    w = (why or "").lower()
    return any(sig in w for sig in _ADVISORY_GUARD_SIGNALS)


# ── guard gauntlet ───────────────────────────────────────────────────────────
def _guard_check(cur, text: str) -> tuple[bool, str, list]:
    """Returns (passed, hard_reason, warnings).

    HARD blocks (never queue): empty draft, analyst-respect disparagement, the
    hard checks inside _should_skip_publish (partner-disparagement, entity dedup,
    number-lead, zero-stat, quality, LLM-disclaimer), and claim-verify
    over-claimed DC Hub canonical stats.

    ADVISORY (queue anyway, attach a warning for the human reviewer): the LLM
    'editor' verifiability gate and the fact-check guard."""
    warnings: list = []
    if not text or not text.strip():
        return False, "empty draft", warnings

    # (0) reactive-lane-specific: never disparage the cited source. HARD.
    ok, why = _analyst_respect_ok(text)
    if not ok:
        return False, why, warnings

    # (1) _should_skip_publish — HARD for disparagement/dedup/number-lead/
    #     zero-stat/quality; ADVISORY for the editor verifiability gate.
    try:
        from content_publisher import _should_skip_publish
        skip, w = _should_skip_publish(cur, text, "linkedin")
        if skip:
            if _is_advisory_guard(w):
                warnings.append(f"publish-guard(advisory): {w}"[:300])
            else:
                return False, f"publish-guard: {w}", warnings
    except Exception as e:
        logger.warning("[reactive] _should_skip_publish unavailable: %s", str(e)[:160])

    # (2) claim-verify — HARD (over-claimed / retired DC Hub canonical stats).
    try:
        from routes.media_claim_verify import verify_claims
        cv = verify_claims(text)
        if cv.get("blocks"):
            return False, "claim-verify: " + "; ".join(cv["blocks"])[:240], warnings
    except Exception as e:
        logger.warning("[reactive] verify_claims unavailable: %s", str(e)[:160])

    # (3) fact-check guard — ADVISORY (flags attributed external numbers).
    try:
        from routes.media_fact_check_guard import verify_media_text  # type: ignore
        res = verify_media_text(text)
        if isinstance(res, dict) and (res.get("blocks") or res.get("ok") is False):
            warnings.append("fact-check(advisory): " + str(
                res.get("blocks") or res.get("reason") or "flagged")[:240])
    except ImportError:
        pass
    except Exception as e:
        logger.warning("[reactive] verify_media_text raised: %s", str(e)[:160])

    return True, "", warnings


# ── shared core: react to ONE external claim ─────────────────────────────────
def _react_one(cur, external: dict) -> dict:
    """Resolve → live-read → draft → guard. Returns a result dict carrying
    either a passing draft (status='queued') or a rejection (status='rejected').
    Does NOT write — the caller persists, so /react and /scan share this path."""
    market = (external.get("market") or "").strip()
    slug, name = _resolve_market_slug(cur, market)
    if not slug:
        return {"status": "rejected", "reason": f"no market match for {market!r}",
                "market_slug": None, "market_name": market}
    dcpi = _live_dcpi(cur, slug)
    if not dcpi or not dcpi.get("verdict"):
        return {"status": "rejected", "reason": f"no live DCPI for {slug}",
                "market_slug": slug, "market_name": name}
    draft = _draft_reframe(external, dcpi)
    if not draft:
        return {"status": "rejected", "reason": "draft composition failed (LLM unavailable)",
                "market_slug": slug, "market_name": name, "our_data": dcpi}
    passed, reason, warnings = _guard_check(cur, draft)
    return {
        "status": "queued" if passed else "rejected",
        "reason": None if passed else reason,
        "warnings": warnings or None,
        "market_slug": slug,
        "market_name": dcpi.get("market_name") or name,
        "our_data": dcpi,
        "post_draft": draft,
    }


def _persist(cur, conn, external: dict, res: dict) -> int | None:
    try:
        cur.execute("""
            INSERT INTO media_reactive_queue
                (source, source_url, external_claim, market_slug, market_name,
                 our_data, post_draft, status, reject_reason, guard_warnings)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            external.get("source"), external.get("source_url"),
            external.get("claim"), res.get("market_slug"), res.get("market_name"),
            json.dumps(res.get("our_data"), default=str) if res.get("our_data") else None,
            res.get("post_draft"), res.get("status"), res.get("reason"),
            "; ".join(res.get("warnings")) if res.get("warnings") else None,
        ))
        row = cur.fetchone()
        conn.commit()
        return row.get("id") if row else None
    except Exception as e:
        conn.rollback()
        logger.warning("[reactive] persist failed: %s", str(e)[:160])
        return None


# ── PHASE 1 helper: pull recent analyst-firm news from Postgres ──────────────
def _recent_analyst_news(cur) -> list:
    """Recent news_articles whose source is an analyst firm. Defensive: returns
    [] if the PG news table isn't present in this deployment."""
    try:
        cur.execute("""
            SELECT id, title, summary, url, source, published_at, relevance_score
              FROM news_articles
             WHERE published_at >= now() - (%s || ' hours')::interval
               AND COALESCE(relevance_score, 0) >= %s
             ORDER BY published_at DESC
             LIMIT 200
        """, (str(_SCAN_WINDOW_HOURS), _SCAN_MIN_RELEVANCE))
        out = []
        for r in (cur.fetchall() or []):
            src = (r.get("source") or "")
            blob = f"{src} {r.get('title') or ''} {r.get('summary') or ''}"
            if _ANALYST_FIRMS_RE.search(blob):
                out.append(dict(r))
        return out
    except Exception as e:
        logger.warning("[reactive] _recent_analyst_news unavailable: %s", str(e)[:160])
        return []


def _already_reacted(cur, source_url: str) -> bool:
    if not source_url:
        return False
    try:
        cur.execute("SELECT 1 FROM media_reactive_queue WHERE source_url = %s LIMIT 1",
                    (source_url,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _market_from_news(title: str, summary: str) -> str | None:
    """Find the first market alias mentioned in a news item (cheap NER)."""
    blob = _norm(f"{title} {summary}")
    for alias in sorted(_MARKET_ALIASES, key=len, reverse=True):
        if alias in blob:
            return alias
    return None


# ── endpoints ────────────────────────────────────────────────────────────────
@media_reactive_news_bp.route("/api/v1/media/reactive-news/react", methods=["POST"])
def react():
    """PHASE 0 — operator-seeded reaction. Body: {market, claim, source[, source_url]}."""
    if not _enabled():
        return _skipped_response()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin only"}), 403

    body = request.get_json(silent=True) or {}
    external = {
        "market": (body.get("market") or "").strip(),
        "claim": (body.get("claim") or "").strip(),
        "source": (body.get("source") or "").strip(),
        "source_url": (body.get("source_url") or "").strip() or None,
    }
    if not external["market"] or not external["claim"] or not external["source"]:
        return jsonify({"ok": False,
                        "error": "market, claim and source are required"}), 400

    conn = _connect()
    if conn is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
            res = _react_one(cur, external)
            qid = _persist(cur, conn, external, res)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "queue_id": qid,
        "status": res.get("status"),
        "market": res.get("market_name"),
        "reason": res.get("reason"),
        "warnings": res.get("warnings"),
        "post_draft": res.get("post_draft") if res.get("status") == "queued" else None,
        "note": "draft written to review queue. NOTHING was published.",
    })


@media_reactive_news_bp.route("/api/v1/media/reactive-news/scan", methods=["POST"])
def scan():
    """PHASE 1 — auto-detect: react to recent analyst-firm news. Point a cron here."""
    if not _enabled():
        return _skipped_response()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin only"}), 403

    conn = _connect()
    if conn is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503

    queued, rejected, skipped, scanned = [], [], 0, 0
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
            news = _recent_analyst_news(cur)
            scanned = len(news)
            for item in news:
                if len(queued) >= _SCAN_MAX_PER_RUN:
                    break
                url = item.get("url")
                if _already_reacted(cur, url):
                    skipped += 1
                    continue
                claim = _extract_claim(item.get("title"), item.get("summary"))
                market = _market_from_news(item.get("title"), item.get("summary"))
                if not claim or not market:
                    skipped += 1
                    continue
                external = {"market": market, "claim": claim,
                            "source": item.get("source"), "source_url": url}
                res = _react_one(cur, external)
                qid = _persist(cur, conn, external, res)
                row = {"id": qid, "market": res.get("market_name"),
                       "source": item.get("source")}
                (queued if res.get("status") == "queued" else rejected).append(
                    {**row, "reason": res.get("reason")} if res.get("status") != "queued" else row)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return jsonify({
        "ok": True, "scanned": scanned, "queued": len(queued),
        "rejected": len(rejected), "skipped": skipped,
        "queued_items": queued, "rejected_items": rejected,
        "note": "drafts written to review queue (status=queued). NOTHING was published.",
    })


@media_reactive_news_bp.route("/api/v1/media/reactive-news/queue", methods=["GET"])
def list_queue():
    if not _enabled():
        return _skipped_response()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin only"}), 403

    status = (request.args.get("status") or "queued").strip().lower()
    if status not in ("queued", "approved", "rejected", "all"):
        status = "queued"

    conn = _connect()
    if conn is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    items = []
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
            if status == "all":
                cur.execute("""
                    SELECT id, source, source_url, external_claim, market_slug,
                           market_name, our_data, post_draft, status, reject_reason,
                           guard_warnings, created_at, reviewed_at, reviewed_by
                      FROM media_reactive_queue
                     ORDER BY created_at DESC LIMIT 200
                """)
            else:
                cur.execute("""
                    SELECT id, source, source_url, external_claim, market_slug,
                           market_name, our_data, post_draft, status, reject_reason,
                           guard_warnings, created_at, reviewed_at, reviewed_by
                      FROM media_reactive_queue
                     WHERE status = %s
                     ORDER BY created_at DESC LIMIT 200
                """, (status,))
            for r in (cur.fetchall() or []):
                d = dict(r)
                for k in ("created_at", "reviewed_at"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                items.append(d)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify({"ok": True, "status": status, "count": len(items), "items": items})


@media_reactive_news_bp.route("/api/v1/media/reactive-news/approve/<int:item_id>",
                              methods=["POST"])
def approve_item(item_id):
    """Operator approval. Flips the queue row to 'approved' (a human decision
    record). Only enqueues a LIVE social_media_posts row if MEDIA_REACTIVE_
    AUTOPOST_ON_APPROVE=1 — otherwise nothing is transmitted."""
    if not _enabled():
        return _skipped_response()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin only"}), 403

    conn = _connect()
    if conn is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE media_reactive_queue
                   SET status = 'approved', reviewed_at = now(), reviewed_by = 'operator'
                 WHERE id = %s AND status = 'queued'
                 RETURNING id, market_name, source, post_draft, status
            """, (item_id,))
            row = cur.fetchone()
            conn.commit()
            if not row:
                return jsonify({"ok": False,
                                "error": "not found or not in 'queued' state"}), 404

            posted = False
            if _autopost_on_approve() and row.get("post_draft"):
                try:
                    cur.execute("""
                        INSERT INTO social_media_posts (content, platform, status, created_at)
                        VALUES (%s, 'linkedin', 'approved', now())
                    """, (row.get("post_draft"),))
                    conn.commit()
                    posted = True
                except Exception as e:
                    conn.rollback()
                    logger.warning("[reactive] autopost enqueue failed: %s", str(e)[:160])
        return jsonify({
            "ok": True,
            "approved": {"id": row.get("id"), "market": row.get("market_name"),
                         "source": row.get("source")},
            "enqueued_for_publish": posted,
            "note": ("approved + enqueued to the LinkedIn publisher" if posted
                     else "marked approved (decision record only — nothing transmitted)"),
        })
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── PHASE 2: editorial integration (importable collector) ────────────────────
def reactive_news_leads(limit: int = 5) -> list:
    """Expose queued reactive drafts as standard editorial leads so they can
    compete in media_editorial.rank_data_events(). DARK-gated: returns [] when
    the feature is off. Lead shape matches the data-desk leads.

    WIRE-IN (one line, at the top of rank_data_events in media_editorial.py):
        try:
            from routes.media_reactive_news import reactive_news_leads
            leads += reactive_news_leads()
        except Exception:
            pass
    """
    if not _enabled():
        return []
    conn = _connect()
    if conn is None:
        return []
    out = []
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, source, source_url, external_claim, market_name
                  FROM media_reactive_queue
                 WHERE status = 'queued'
                 ORDER BY created_at DESC
                 LIMIT %s
            """, (int(limit),))
            for r in (cur.fetchall() or []):
                src = r.get("source") or "industry report"
                out.append({
                    "kind": "reactive",
                    "headline_number": (r.get("external_claim") or "").strip(),
                    "trend": f"{src} report vs DC Hub live grid data",
                    "so_what": (f"Reframes the {src} read on {r.get('market_name')} "
                                "against DC Hub's live grid/time-to-power data."),
                    "source_url": r.get("source_url") or "https://dchub.cloud",
                    "dedup_key": f"reactive:{r.get('id')}",
                    "score": _LEAD_SCORE,
                })
    except Exception as e:
        logger.warning("[reactive] reactive_news_leads failed: %s", str(e)[:160])
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out
