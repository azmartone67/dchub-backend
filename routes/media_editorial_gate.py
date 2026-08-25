"""media_editorial_gate.py — the brain's editorial desk (2026-07-19).

WHY THIS EXISTS
===============
The fact-check guard checks that numbers are TRUE; nothing checked that a
story is NEW. Result: the auto lanes re-headlined the same markets on the
same angle for weeks ("Rural SPP 67/100", "Cheyenne 70/100" ~5x each in two
weeks) — technically honest, editorially dead. The operator chose a hybrid
newsroom (2026-07-19): data-driven stories may auto-publish ONLY after this
editorial review; brand-voice releases (product-launch, partnership,
ai-citation) are never auto-published and go to the pending-drafts digest.

    editorial_gate(title, body, category, market_slug=None) -> {
        action:  "publish" | "draft",
        score:   0-10 or None,
        reasons: [str, ...],
        model:   str | None,
    }

Two layers, both fail-CLOSED (any failure → "draft", never a blind publish,
never a silent kill — a human still sees the draft in the daily digest):

  1. Deterministic market cooldown — if `market_slug` was headlined in a
     PUBLISHED release within the last 14 days, the story drafts unless it
     reports a verdict flip (the one delta class that always justifies
     re-covering a market).
  2. LLM editorial review — the candidate + the last 14 days of published
     headlines go to the brain, which scores novelty/newsworthiness 0-10.
     "publish" requires score >= threshold AND a concrete reported delta.

Every review is persisted to media_editorial_reviews so the pending-drafts
digest can show WHY something was held.

Kill switch: MEDIA_EDITORIAL_GATE_DISABLED=1 → pass-through publish
(pre-gate behavior). Model: DCHUB_MEDIA_EDITOR_MODEL (default the brain's
reasoning tier). Threshold: MEDIA_EDITORIAL_MIN_SCORE (default 7).
"""
from __future__ import annotations

import os
import json
import logging
import datetime
from routes.brain_llm_spend import instrumented_post as _llm_post

logger = logging.getLogger(__name__)

_COOLDOWN_DAYS = 14
_RECENT_HEADLINES = 25


def _disabled() -> bool:
    return os.environ.get("MEDIA_EDITORIAL_GATE_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _min_score() -> int:
    try:
        return int(os.environ.get("MEDIA_EDITORIAL_MIN_SCORE", "7"))
    except Exception:
        return 7


def _model() -> str:
    return (os.environ.get("DCHUB_MEDIA_EDITOR_MODEL")
            or os.environ.get("DCHUB_BRAIN_MODEL_REASONING")
            or "claude-opus-4-8").strip()


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=6)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_editorial_reviews (
    id          BIGSERIAL PRIMARY KEY,
    press_slug  TEXT NOT NULL,
    category    TEXT,
    verdict     TEXT NOT NULL,
    score       REAL,
    reasons     JSONB DEFAULT '[]'::jsonb,
    model       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mer_slug ON media_editorial_reviews(press_slug);
-- r-attempt-memory (2026-08-10): the composer's DO-NOT-REPEAT context reads
-- TITLES, and until now this table stored only the slug. See the note on
-- _record_review below for why the composer must read this table at all.
ALTER TABLE media_editorial_reviews ADD COLUMN IF NOT EXISTS title TEXT;
CREATE INDEX IF NOT EXISTS ix_mer_created ON media_editorial_reviews(created_at DESC);
"""


def _record_review(slug: str, category: str, verdict: str, score, reasons, model,
                   title: str | None = None):
    """Persist the review.

    ★ r-attempt-memory (2026-08-10): this row is the ONLY durable record that
    a story was attempted. It is written on its own connection, so it survives
    when the composer's own transaction (press_releases + auto_press_releases
    + press_integrity_reviews, one transaction ending at a single commit)
    rolls back. On 2026-08-09 that happened 19 times out of 20 — and because
    marketing_engine's DO-NOT-REPEAT context read auto_press_releases, which
    only gains a row when the write COMPLETES, the composer had no memory of
    any of them and re-proposed the same Midland-Odessa story 16 times in two
    hours. Keep `title` populated: it is what the composer reads back.
    """
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute("""
                INSERT INTO media_editorial_reviews
                    (press_slug, category, verdict, score, reasons, model, title)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            """, (slug[:200], (category or "")[:80], verdict, score,
                  json.dumps(list(reasons or [])[:8]), model,
                  (title or "")[:300] or None))
    except Exception as e:
        logger.warning("[editorial_gate] review record failed: %s", str(e)[:120])
    finally:
        try: c.close()
        except Exception: pass


def _recent_published() -> list[dict]:
    """Last 14d of PUBLISHED headlines — the context the editor judges against."""
    c = _conn()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT title, slug, category, created_at::date::text
                FROM press_releases
                WHERE published = TRUE
                  AND created_at > NOW() - %s * INTERVAL '1 day'
                ORDER BY created_at DESC
                LIMIT %s
            """ % (_COOLDOWN_DAYS, _RECENT_HEADLINES))
            return [{"title": r[0], "slug": r[1], "category": r[2], "date": r[3]}
                    for r in cur.fetchall()]
    except Exception as e:
        logger.warning("[editorial_gate] recent lookup failed: %s", str(e)[:120])
        return []
    finally:
        try: c.close()
        except Exception: pass


def _market_on_cooldown(market_slug: str, recent: list[dict]) -> str | None:
    """Return the blocking headline if this market was covered <14d ago."""
    ms = (market_slug or "").strip().lower()
    if not ms:
        return None
    for r in recent:
        hay = f"{r.get('slug') or ''} {r.get('title') or ''}".lower()
        if ms in hay or ms.replace("-", " ") in hay:
            return f"{r.get('date')}: {r.get('title')}"
    return None


def _cross_surface_days() -> int:
    try:
        return max(1, int(os.environ.get("MEDIA_CROSS_SURFACE_DAYS", "7")))
    except Exception:
        return 7


def press_blocked_by_linkedin(market_slug: str, title: str = "") -> str | None:
    """The OTHER half of the cross-surface edge (2026-08-24).

    This gate has always run a 14-day cooldown against its OWN press history and
    has never once looked at what the LinkedIn quad published. The quad ran the
    mirror-image blindness. Measured over one week on live data:

        Upper Peninsula MI    LinkedIn 08-19  ·  press 08-21   (2 days)
        Tulsa / Oklahoma SPP  LinkedIn 08-18  ·  press 08-20   (2 days)
        Google $12B deal      LinkedIn 08-20  ·  press 08-20   (SAME DAY)

    Each surface was correct about itself, which is why neither ever flagged it.

    Returns the blocking LinkedIn entity, or None. Fail-OPEN (None) on any
    error or when disabled — a bad read must never stall the press lane. The
    quad-side half lives in media_editorial.recent_press_terms().
    """
    try:
        from routes.media_editorial import (
            recent_lead_ledger, cross_surface_hit, press_terms_from_rows,
            _xs_disabled)
        if _xs_disabled():
            return None
        try:
            days = max(1, int(os.environ.get("MEDIA_CROSS_SURFACE_DAYS", "7")))
        except Exception:
            days = 7
        # Reuse the quad's own normalization so both directions agree on
        # identity — two spellings of "recently covered" is the bug, not the fix.
        terms = press_terms_from_rows(
            [{"slug": market_slug or "", "title": title or ""}])
        for row in (recent_lead_ledger(days=days) or []):
            ent = (row or {}).get("entity") or ""
            if not ent:
                continue
            if (row.get("days_ago") is not None and row["days_ago"] > days):
                continue
            if cross_surface_hit(ent, terms):
                return ent
        return None
    except Exception as e:
        logger.warning("[editorial_gate] linkedin cross-check failed: %s", str(e)[:120])
        return None


def _ask_editor(title: str, body: str, category: str, recent: list[dict]) -> dict | None:
    """One LLM call. Returns parsed {score, verdict, reasons} or None on any
    failure (caller fails closed to draft)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import requests
        from utils.anthropic_helper import anthropic_messages_url
    except Exception:
        return None

    recent_lines = "\n".join(
        f"- [{r['date']}] ({r['category']}) {r['title']}" for r in recent) or "(none)"
    prompt = f"""You are the editorial desk for DC Hub's automated newsroom
(data-center infrastructure intelligence). Judge ONE candidate press release
for novelty and newsworthiness against what we already published.

PUBLISHED IN THE LAST {_COOLDOWN_DAYS} DAYS:
{recent_lines}

CANDIDATE ({category}):
TITLE: {title[:300]}
BODY:
{(body or '')[:3500]}

Score 0-10. A story only deserves "publish" if it reports a CONCRETE change
(verdict flip, score move, new deal, new record, new capability) that is NOT
a re-worded repeat of a recent headline. The chronic failure you exist to
stop: the same market's same score re-headlined every few days ("Rural SPP
67/100" again). Fresh angle on stale numbers = draft. When torn, draft.

Reply with STRICT JSON only, no prose:
{{"score": <0-10>, "verdict": "publish"|"draft", "reasons": ["<short reason>", ...]}}"""

    try:
        r = _llm_post("media_editorial_gate",
            anthropic_messages_url(),
            headers={"x-api-key": api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json",
                     "User-Agent": "dchub-brain/1.0"},
            json={"model": _model(), "max_tokens": 500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        if r.status_code != 200:
            logger.warning("[editorial_gate] llm http_%s", r.status_code)
            return None
        text = " ".join(c.get("text", "") for c in (r.json().get("content") or [])
                        if c.get("type") == "text").strip()
        # tolerate a fenced or prefixed JSON blob
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        out = json.loads(text[start:end + 1])
        if not isinstance(out, dict) or "verdict" not in out:
            return None
        return out
    except Exception as e:
        logger.warning("[editorial_gate] llm call failed: %s", str(e)[:120])
        return None


def editorial_gate(title: str, body: str, category: str,
                   market_slug: str | None = None,
                   press_slug: str | None = None) -> dict:
    """The publish/draft decision for an auto-lane release. NEVER raises."""
    slug_for_log = press_slug or (title or "")[:80]
    try:
        if _disabled():
            return {"action": "publish", "score": None, "model": None,
                    "reasons": ["editorial gate disabled via env"]}

        recent = _recent_published()

        # Layer 1: deterministic market cooldown. Verdict flips are exempt —
        # an AVOID→BUILD reversal is news even about a recently-covered market.
        is_flip = "flip" in (title or "").lower() or "→" in (title or "")
        block = _market_on_cooldown(market_slug or "", recent)
        if block and not is_flip:
            reasons = [f"market cooldown (<{_COOLDOWN_DAYS}d): already published — {block}"]
            _record_review(slug_for_log, category, "draft", None, reasons, "cooldown",
                           title=title)
            return {"action": "draft", "score": None, "model": "cooldown",
                    "reasons": reasons}

        # Layer 1b (2026-08-24): CROSS-SURFACE. The same story on LinkedIn two
        # days ago is a repeat to the reader even though this desk has never
        # covered it. Verdict flips stay exempt for the same reason as above —
        # an AVOID→BUILD reversal is news on any surface.
        li_block = press_blocked_by_linkedin(market_slug or "", title or "")
        if li_block and not is_flip:
            reasons = [f"cross-surface (<{_cross_surface_days()}d): LinkedIn already "
                       f"led with '{li_block}' — drafting rather than repeating it"]
            _record_review(slug_for_log, category, "draft", None, reasons,
                           "cross_surface", title=title)
            return {"action": "draft", "score": None, "model": "cross_surface",
                    "reasons": reasons}

        # Layer 2: brain editorial review. Any failure → draft (fail closed).
        review = _ask_editor(title or "", body or "", category or "", recent)
        if review is None:
            reasons = ["editorial review unavailable — failing closed to draft"]
            _record_review(slug_for_log, category, "draft", None, reasons, None,
                           title=title)
            return {"action": "draft", "score": None, "model": None,
                    "reasons": reasons}

        score = review.get("score")
        reasons = [str(x)[:200] for x in (review.get("reasons") or [])][:6]
        publish = (str(review.get("verdict", "")).lower() == "publish"
                   and isinstance(score, (int, float)) and score >= _min_score())
        verdict = "publish" if publish else "draft"
        _record_review(slug_for_log, category, verdict, score, reasons, _model(),
                       title=title)
        return {"action": verdict, "score": score, "model": _model(),
                "reasons": reasons}
    except Exception as e:
        reasons = [f"gate raised — failing closed ({str(e)[:100]})"]
        try:
            _record_review(slug_for_log, category, "draft", None, reasons, None,
                           title=title)
        except Exception:
            pass
        return {"action": "draft", "score": None, "model": None, "reasons": reasons}
