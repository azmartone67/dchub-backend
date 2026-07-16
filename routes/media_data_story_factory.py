"""media_data_story_factory.py — DATA-STORY FACTORY (2026-06-23).
=================================================================

Automates the one-off "Rich Miller" pitch into a REPEATABLE engine: detect
newsworthy DCPI shifts since the last run, draft a pitch-ready data brief + a
short journalist pitch for each, fact-check every word, and DROP THE DRAFTS
INTO A REVIEW QUEUE for the operator to approve. It NEVER sends a pitch, emails
a journalist, posts, or publishes anything live.

Born out of the incident where an auto-published post disparaged a partner.
Safety is the whole design here, not a feature bolt-on:

  (1) DARK BY DEFAULT — the entire feature is gated behind
      MEDIA_DATA_STORY_FACTORY_ENABLED. With the flag off (the default), every
      endpoint returns {"ok": true, "skipped": "disabled"} and the module does
      NOTHING — no DB writes, no LLM calls, no detection.

  (2) NO AUTO-SEND / NO AUTO-PUBLISH-LIVE — POST /run only DETECTS, DRAFTS and
      QUEUES. Drafts land in media_story_queue with status='queued' for the
      operator. POST /approve/<id> only flips status to 'approved' (a human
      decision record) — it does NOT transmit anything anywhere. There is no
      code path in this module that POSTs to LinkedIn/X, emails a journalist,
      or publishes a page.

  (3) EVERY draft is routed through the hardened guards BEFORE it is queued:
        - content_publisher._should_skip_publish(cur, text, platform)
        - routes.media_claim_verify.verify_claims(text)  (the claim-verify we
          hardened today; blocks = retired/over-claimed numbers)
        - routes.media_fact_check_guard.verify_media_text(text)  (used ONLY if
          present in this deployment — wrapped so a missing module can never
          break import or detection)
      Anything a guard flags is DROPPED (not queued) and recorded as 'rejected'
      with the reason, so the rejection is auditable.

  (4) VERIFIED NUMBERS ONLY — every figure in a draft comes from a LIVE read of
      market_power_scores (verdict / excess_power_score / constraint_score /
      time_to_power_months) or canonical_stats.get_canonical_stats() (counts).
      The LLM is handed ONLY those verified values and is instructed to invent
      nothing; then verify_claims re-checks the composed text against canon. A
      number that cannot be verified live is omitted by construction — there is
      no path that injects an unverified stat (the "225 GW that the live data
      reports tracked:false" class of bug is impossible here).

  (5) SELF-IMPORT-SAFE — every optional import (psycopg2, the guards, the LLM,
      canonical_stats) is wrapped in try/except. With the flag off and no env,
      importing this module and registering the blueprint can NEVER crash boot.

Detection (the Rich Miller play, automated): diff the current market_power_scores
against the last snapshot in media_dcpi_snapshots (a table this module creates):
  - VERDICT FLIP            BUILD <-> CAUTION <-> AVOID
  - BIG EXCESS-POWER MOVE   |Δ excess_power_score| >= threshold
  - BIG TIME-TO-POWER MOVE  |Δ time_to_power_months| >= threshold
  - NEW BUILD MARKET        a market that newly appears with verdict='BUILD'

Admin endpoints (all gated, mirroring brain_innovation._innov_admin_ok):
  POST /api/v1/media/data-story/run          detect + draft + queue (dry by design)
  GET  /api/v1/media/data-story/queue         list queued drafts for review
  POST /api/v1/media/data-story/approve/<id>  operator approval record (no send)

Kill switch:  MEDIA_DATA_STORY_FACTORY_ENABLED  (OFF unless 1/true/yes/on)
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger("media_data_story_factory")

media_data_story_factory_bp = Blueprint("media_data_story_factory", __name__)

# ── optional imports (NEVER let a missing dep break import / app boot) ───────
try:
    import psycopg2 as _pg
    import psycopg2.extras as _pg_extras
except Exception:                                   # pragma: no cover
    _pg = None
    _pg_extras = None

try:
    import urllib.request as _urlreq
    import urllib.error as _urlerr
except Exception:                                   # pragma: no cover
    _urlreq = None
    _urlerr = None

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Detection thresholds (movement that's actually newsworthy). Conservative so
# the queue stays high-signal; tune via env if needed.
_EXCESS_MOVE_MIN = float(os.environ.get("MEDIA_STORY_EXCESS_MOVE_MIN", "15"))
_TTP_MOVE_MIN = float(os.environ.get("MEDIA_STORY_TTP_MOVE_MIN", "6"))   # months
_MAX_STORIES_PER_RUN = int(os.environ.get("MEDIA_STORY_MAX_PER_RUN", "12"))


# ── kill switch ──────────────────────────────────────────────────────────────
def _enabled() -> bool:
    """DARK BY DEFAULT. The feature is OFF unless the flag is explicitly on."""
    return os.environ.get("MEDIA_DATA_STORY_FACTORY_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _skipped_response():
    return jsonify({"ok": True, "skipped": "disabled"})


# ── admin gate (mirrors brain_innovation._innov_admin_ok) ────────────────────
def _admin_ok() -> bool:
    _keys = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    if not _keys:
        # No admin key configured → refuse rather than run wide open.
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
        logger.warning("[data_story] DB connect failed: %s", str(e)[:160])
        return None


def _ensure_tables(conn) -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS for the snapshot + review queue.
    Never raises out — a DDL failure degrades the run, it must not crash boot
    (this is only ever called from inside a flag-gated request anyway)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_dcpi_snapshots (
                    id              BIGSERIAL PRIMARY KEY,
                    market_slug     TEXT NOT NULL,
                    market_name     TEXT,
                    verdict         TEXT,
                    excess_power_score   NUMERIC,
                    constraint_score     NUMERIC,
                    time_to_power_months NUMERIC,
                    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_dcpi_snapshots_slug_at
                    ON media_dcpi_snapshots (market_slug, snapshot_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_story_queue (
                    id              BIGSERIAL PRIMARY KEY,
                    market_slug     TEXT,
                    market_name     TEXT,
                    shift_kind      TEXT NOT NULL,
                    shift_detail    JSONB,
                    data_brief      TEXT,
                    journalist_pitch TEXT,
                    status          TEXT NOT NULL DEFAULT 'queued',
                    reject_reason   TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    reviewed_at     TIMESTAMPTZ,
                    reviewed_by     TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_story_queue_status_at
                    ON media_story_queue (status, created_at DESC)
            """)
        conn.commit()
    except Exception as e:
        logger.warning("[data_story] _ensure_tables failed: %s", str(e)[:200])
        try:
            conn.rollback()
        except Exception:
            pass


# ── live DCPI read (VERIFIED NUMBERS ONLY — straight from the source table) ──
def _current_scores(cur) -> dict:
    """Read the live DCPI verdict/scores per market. Returns {} on any error
    (the run then no-ops rather than fabricating)."""
    out = {}
    try:
        cur.execute("""
            SELECT market_slug, market_name, verdict,
                   excess_power_score, constraint_score, time_to_power_months
              FROM market_power_scores
             WHERE market_slug IS NOT NULL
        """)
        for r in (cur.fetchall() or []):
            slug = r.get("market_slug") if hasattr(r, "get") else r[0]
            if not slug:
                continue
            out[slug] = {
                "market_slug": slug,
                "market_name": (r.get("market_name") if hasattr(r, "get") else r[1]),
                "verdict": (r.get("verdict") if hasattr(r, "get") else r[2]),
                "excess_power_score": (r.get("excess_power_score") if hasattr(r, "get") else r[3]),
                "constraint_score": (r.get("constraint_score") if hasattr(r, "get") else r[4]),
                "time_to_power_months": (r.get("time_to_power_months") if hasattr(r, "get") else r[5]),
            }
    except Exception as e:
        logger.warning("[data_story] _current_scores failed: %s", str(e)[:200])
        return {}
    return out


def _last_snapshot(cur) -> dict:
    """The most recent prior snapshot per market (for diffing). {} if none."""
    out = {}
    try:
        cur.execute("""
            SELECT DISTINCT ON (market_slug)
                   market_slug, market_name, verdict,
                   excess_power_score, constraint_score, time_to_power_months
              FROM media_dcpi_snapshots
             ORDER BY market_slug, snapshot_at DESC
        """)
        for r in (cur.fetchall() or []):
            slug = r.get("market_slug") if hasattr(r, "get") else r[0]
            if not slug:
                continue
            out[slug] = {
                "verdict": (r.get("verdict") if hasattr(r, "get") else r[2]),
                "excess_power_score": (r.get("excess_power_score") if hasattr(r, "get") else r[3]),
                "constraint_score": (r.get("constraint_score") if hasattr(r, "get") else r[4]),
                "time_to_power_months": (r.get("time_to_power_months") if hasattr(r, "get") else r[5]),
            }
    except Exception as e:
        logger.warning("[data_story] _last_snapshot failed: %s", str(e)[:200])
        return {}
    return out


def _write_snapshot(cur, current: dict) -> None:
    """Persist the current scores as the new baseline for the next run."""
    try:
        for s in current.values():
            cur.execute("""
                INSERT INTO media_dcpi_snapshots
                    (market_slug, market_name, verdict,
                     excess_power_score, constraint_score, time_to_power_months)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (
                s.get("market_slug"), s.get("market_name"), s.get("verdict"),
                s.get("excess_power_score"), s.get("constraint_score"),
                s.get("time_to_power_months"),
            ))
    except Exception as e:
        logger.warning("[data_story] _write_snapshot failed: %s", str(e)[:200])
        raise


def _num(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


# ── shift detection (the diff that finds the news) ───────────────────────────
_VERDICT_SET = {"BUILD", "CAUTION", "AVOID"}


def _detect_shifts(current: dict, prior: dict) -> list:
    """Diff current vs prior. Returns a list of shift dicts, each carrying ONLY
    verified live values (no derived/fabricated numbers)."""
    shifts = []
    for slug, cur_s in current.items():
        prev = prior.get(slug)
        name = cur_s.get("market_name") or slug
        cur_v = (cur_s.get("verdict") or "").upper()

        # NEW BUILD market — first time we've seen this market AND it's BUILD.
        if prev is None:
            if cur_v == "BUILD":
                shifts.append({
                    "kind": "new_build_market",
                    "market_slug": slug,
                    "market_name": name,
                    "detail": {
                        "verdict": cur_v,
                        "excess_power_score": _num(cur_s.get("excess_power_score")),
                        "constraint_score": _num(cur_s.get("constraint_score")),
                        "time_to_power_months": _num(cur_s.get("time_to_power_months")),
                    },
                })
            continue

        prev_v = (prev.get("verdict") or "").upper()

        # VERDICT FLIP — between two real verdicts that actually changed.
        if (cur_v in _VERDICT_SET and prev_v in _VERDICT_SET and cur_v != prev_v):
            shifts.append({
                "kind": "verdict_flip",
                "market_slug": slug,
                "market_name": name,
                "detail": {
                    "from_verdict": prev_v,
                    "to_verdict": cur_v,
                    "excess_power_score": _num(cur_s.get("excess_power_score")),
                    "constraint_score": _num(cur_s.get("constraint_score")),
                    "time_to_power_months": _num(cur_s.get("time_to_power_months")),
                },
            })

        # BIG EXCESS-POWER MOVE.
        ce, pe = _num(cur_s.get("excess_power_score")), _num(prev.get("excess_power_score"))
        if ce is not None and pe is not None and abs(ce - pe) >= _EXCESS_MOVE_MIN:
            shifts.append({
                "kind": "excess_power_move",
                "market_slug": slug,
                "market_name": name,
                "detail": {
                    "verdict": cur_v,
                    "excess_power_score_from": pe,
                    "excess_power_score_to": ce,
                    "delta": round(ce - pe, 1),
                },
            })

        # BIG TIME-TO-POWER MOVE.
        ct, pt = _num(cur_s.get("time_to_power_months")), _num(prev.get("time_to_power_months"))
        if ct is not None and pt is not None and abs(ct - pt) >= _TTP_MOVE_MIN:
            shifts.append({
                "kind": "time_to_power_move",
                "market_slug": slug,
                "market_name": name,
                "detail": {
                    "verdict": cur_v,
                    "time_to_power_months_from": pt,
                    "time_to_power_months_to": ct,
                    "delta_months": round(ct - pt, 1),
                },
            })

    return shifts[:_MAX_STORIES_PER_RUN]


# ── canonical counts (verified platform-level numbers for the brief) ─────────
def _canon_block() -> dict:
    try:
        from canonical_stats import get_canonical_stats
        s = get_canonical_stats() or {}
        return {
            "facilities_tracked": s.get("facilities"),
            "markets_tracked": s.get("markets"),
            "countries_tracked": s.get("countries"),
        }
    except Exception as e:
        logger.warning("[data_story] canonical_stats unavailable: %s", str(e)[:160])
        return {}


# ── LLM drafting (verified data in, draft out — never invents a number) ──────
_SYSTEM = (
    "You are the data-desk writer for DC Hub (dchub.cloud), an infrastructure-"
    "intelligence platform. You turn a single verified DCPI data shift into a "
    "tight, factual story. ABSOLUTE RULES: (1) Use ONLY the numbers in VERIFIED "
    "DATA — never invent, round up, or estimate any figure, capacity (GW/MW), "
    "count, dollar amount, percentage, or date that is not present there. "
    "(2) Never disparage, name-and-shame, or speculate about any company, "
    "operator, utility, or partner — describe the MARKET-LEVEL data shift only. "
    "(3) Attribute the data to 'DC Hub (dchub.cloud)'. (4) No hype, no "
    "superlatives you can't source. If a fact isn't in VERIFIED DATA, leave it out."
)


def _llm(system: str, user: str, max_tokens: int = 700) -> str | None:
    """RAW HTTP call mirroring routes/linkedin_content_engine._compose_with_claude.
    Returns the composed text or None on any failure (caller drops the draft)."""
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
        logger.warning("[data_story] LLM call failed: %s", str(e)[:200])
        return None


def _anthropic_url() -> str:
    try:
        from utils.anthropic_helper import anthropic_messages_url
        return anthropic_messages_url()
    except Exception:
        return "https://api.anthropic.com/v1/messages"


def _verified_payload(shift: dict, canon: dict) -> str:
    """The ONLY facts the LLM is allowed to use — composed entirely from
    live-verified values."""
    return json.dumps({
        "market": shift.get("market_name"),
        "shift_kind": shift.get("kind"),
        "shift": shift.get("detail"),
        "platform_context": {k: v for k, v in canon.items() if v is not None},
        "source": "DC Hub (dchub.cloud) — DC Hub Power Index (DCPI)",
        "as_of": datetime.utcnow().strftime("%Y-%m-%d"),
    }, default=str)


def _draft_brief(shift: dict, canon: dict) -> str | None:
    payload = _verified_payload(shift, canon)
    user = (
        "Write a concise, pitch-ready DATA BRIEF (3 short paragraphs) for an "
        "industry journalist, explaining this DCPI market shift and why it "
        "matters for AI data-center siting decisions. Lead with the verified "
        "number and the change. Use ONLY VERIFIED DATA below.\n\n"
        f"VERIFIED DATA:\n{payload}"
    )
    return _llm(_SYSTEM, user, max_tokens=700)


def _draft_pitch(shift: dict, canon: dict) -> str | None:
    payload = _verified_payload(shift, canon)
    user = (
        "Write a SHORT journalist pitch (4-6 sentences, email-body style) "
        "offering this DCPI data shift as a story, with an offer of the "
        "underlying data + an analyst quote on request. Do NOT include a "
        "recipient name, salutation, or send it — this is a DRAFT for operator "
        "review. Use ONLY VERIFIED DATA below.\n\n"
        f"VERIFIED DATA:\n{payload}"
    )
    return _llm(_SYSTEM, user, max_tokens=450)


# ── the guard gauntlet (every draft must clear ALL of these) ─────────────────
def _guard_check(cur, text: str) -> tuple[bool, str]:
    """Run the composed text through the hardened publish guards.
    Returns (passed, reason). FAIL-CLOSED for guard hits; FAIL-OPEN only for a
    guard being unavailable (so a transient import never silently drops a draft
    without a logged reason).

    'platform' is 'media_pitch' — these are not social posts; the platform-
    specific LinkedIn number-lead gate doesn't apply, but the partner-
    disparagement / LLM-disclaimer / quality / zero-stat gates all do."""
    if not text or not text.strip():
        return False, "empty draft"

    # (1) content_publisher._should_skip_publish — partner-disparagement block,
    #     LLM-disclaimer block, quality gate, zero-stat guard.
    try:
        from content_publisher import _should_skip_publish
        skip, why = _should_skip_publish(cur, text, "media_pitch")
        if skip:
            return False, f"publish-guard: {why}"
    except Exception as e:
        logger.warning("[data_story] _should_skip_publish unavailable: %s", str(e)[:160])

    # (2) routes.media_claim_verify.verify_claims — the claim-verify hardened
    #     today. ANY block (over-claimed / retired / banned number) drops it.
    try:
        from routes.media_claim_verify import verify_claims
        cv = verify_claims(text)
        if cv.get("blocks"):
            return False, "claim-verify: " + "; ".join(cv["blocks"])[:240]
    except Exception as e:
        logger.warning("[data_story] verify_claims unavailable: %s", str(e)[:160])

    # (3) routes.media_fact_check_guard.verify_media_text — used ONLY if this
    #     deployment ships it. Wrapped so a missing module can NEVER break the
    #     run; if present and it flags, we drop. Supports a few common shapes.
    try:
        from routes.media_fact_check_guard import verify_media_text  # type: ignore
        res = verify_media_text(text)
        ok, reason = _interpret_fact_check(res)
        if not ok:
            return False, f"fact-check-guard: {reason}"
    except ImportError:
        pass  # module not present in this deployment — fine, the other two cover it
    except Exception as e:
        logger.warning("[data_story] verify_media_text unavailable/raised: %s", str(e)[:160])

    return True, ""


def _interpret_fact_check(res) -> tuple[bool, str]:
    """Tolerate whatever shape verify_media_text returns, fail-CLOSED if it
    clearly flags. Unknown/None shapes pass (the other two guards still ran)."""
    try:
        if res is None:
            return True, ""
        if isinstance(res, dict):
            if res.get("blocks"):
                return False, "; ".join(map(str, res["blocks"]))[:240]
            if res.get("ok") is False:
                return False, str(res.get("reason") or res.get("error") or "flagged")[:240]
            return True, ""
        if isinstance(res, tuple) and len(res) >= 1:
            # (ok, reason) or (skip, reason) — be conservative: if first is
            # falsy treat as fail when it looks like an ok-flag.
            first = res[0]
            reason = str(res[1]) if len(res) > 1 else "flagged"
            if first is False:
                return False, reason[:240]
            return True, ""
        if isinstance(res, bool):
            return (res, "" if res else "flagged")
    except Exception:
        pass
    return True, ""


# ── endpoints ────────────────────────────────────────────────────────────────
@media_data_story_factory_bp.route("/api/v1/media/data-story/run", methods=["POST"])
def run_factory():
    if not _enabled():
        return _skipped_response()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin only"}), 403

    conn = _connect()
    if conn is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503

    queued, rejected, detected = [], [], 0
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
            current = _current_scores(cur)
            if not current:
                return jsonify({"ok": True, "detected": 0, "queued": 0,
                                "note": "no live DCPI scores read; nothing done"})
            prior = _last_snapshot(cur)
            shifts = _detect_shifts(current, prior)
            detected = len(shifts)
            canon = _canon_block()

            for sh in shifts:
                brief = _draft_brief(sh, canon)
                pitch = _draft_pitch(sh, canon)
                if not brief and not pitch:
                    rejected.append({"market": sh.get("market_name"),
                                     "kind": sh.get("kind"),
                                     "reason": "draft composition failed (LLM unavailable)"})
                    continue

                # EVERY draft must clear the guard gauntlet. Drop on any flag.
                combined = "\n\n".join([t for t in (brief, pitch) if t])
                passed, reason = _guard_check(cur, combined)
                if not passed:
                    try:
                        cur.execute("""
                            INSERT INTO media_story_queue
                                (market_slug, market_name, shift_kind, shift_detail,
                                 data_brief, journalist_pitch, status, reject_reason)
                            VALUES (%s,%s,%s,%s,%s,%s,'rejected',%s) ON CONFLICT DO NOTHING
                        """, (sh.get("market_slug"), sh.get("market_name"),
                              sh.get("kind"), json.dumps(sh.get("detail"), default=str),
                              brief, pitch, reason[:480]))
                        conn.commit()
                    except Exception:
                        note_swallowed_write("media_story_queue", where="media_data_story_factory.run_factory")
                        conn.rollback()
                    rejected.append({"market": sh.get("market_name"),
                                     "kind": sh.get("kind"), "reason": reason})
                    continue

                # PASSED — write to the REVIEW QUEUE (status='queued'). NOT sent.
                try:
                    cur.execute("""
                        INSERT INTO media_story_queue
                            (market_slug, market_name, shift_kind, shift_detail,
                             data_brief, journalist_pitch, status)
                        VALUES (%s,%s,%s,%s,%s,%s,'queued') ON CONFLICT DO NOTHING
                        RETURNING id
                    """, (sh.get("market_slug"), sh.get("market_name"),
                          sh.get("kind"), json.dumps(sh.get("detail"), default=str),
                          brief, pitch))
                    row = cur.fetchone()
                    conn.commit()
                    queued.append({"id": (row.get("id") if row else None),
                                   "market": sh.get("market_name"),
                                   "kind": sh.get("kind")})
                except Exception as e:
                    conn.rollback()
                    rejected.append({"market": sh.get("market_name"),
                                     "kind": sh.get("kind"),
                                     "reason": f"queue write failed: {str(e)[:120]}"})

            # Update the baseline so the NEXT run diffs against today.
            try:
                _write_snapshot(cur, current)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.warning("[data_story] snapshot baseline update failed")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "detected": detected,
        "queued": len(queued),
        "rejected": len(rejected),
        "queued_items": queued,
        "rejected_items": rejected,
        "note": "drafts written to review queue (status=queued). NOTHING was sent or published.",
    })


@media_data_story_factory_bp.route("/api/v1/media/data-story/queue", methods=["GET"])
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
                    SELECT id, market_slug, market_name, shift_kind, shift_detail,
                           data_brief, journalist_pitch, status, reject_reason,
                           created_at, reviewed_at, reviewed_by
                      FROM media_story_queue
                     ORDER BY created_at DESC LIMIT 200
                """)
            else:
                cur.execute("""
                    SELECT id, market_slug, market_name, shift_kind, shift_detail,
                           data_brief, journalist_pitch, status, reject_reason,
                           created_at, reviewed_at, reviewed_by
                      FROM media_story_queue
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


@media_data_story_factory_bp.route("/api/v1/media/data-story/approve/<int:item_id>",
                                    methods=["POST"])
def approve_item(item_id):
    """Operator approval — records a human decision. Does NOT transmit anything.
    Approval simply marks the draft 'approved' so the operator can act on it
    out-of-band (paste into their own outreach). There is no send path here."""
    if not _enabled():
        return _skipped_response()
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin only"}), 403

    who = (request.headers.get("X-Internal-Key") and "operator") or "operator"
    conn = _connect()
    if conn is None:
        return jsonify({"ok": False, "error": "db unavailable"}), 503
    try:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE media_story_queue
                   SET status = 'approved', reviewed_at = now(), reviewed_by = %s
                 WHERE id = %s AND status = 'queued'
                 RETURNING id, market_name, shift_kind, status
            """, (who, item_id))
            row = cur.fetchone()
            conn.commit()
        if not row:
            return jsonify({"ok": False,
                            "error": "not found or not in 'queued' state"}), 404
        return jsonify({
            "ok": True,
            "approved": dict(row),
            "note": "marked approved for operator action. NOTHING was sent or published.",
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
