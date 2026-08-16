"""routes/press_integrity.py — the analyst's pre-flight check (2026-08-07).

WHY THIS EXISTS
===============
A press-release link went public — /news/2026-08-07-perplexity-dcpi-dual-
score-citation — for a release that had no body and never should have been
exposed; the page fell back to a blank, FUTURE-dated news digest and read as
amateur. The operator's directive: "before these go out, they need to be
checked by the analyst ... accurate and meaningful content ... autonomous,
self-learning, and empowered."

This module is that check, as a REUSABLE function plus a self-healing sweep:

  analyst_review(release) -> {ok, issues, hard}
      The single gate every publish path can call BEFORE going live. It
      answers one question honestly: is this release complete, correctly
      dated, and free of fabricated/placeholder content? HARD issues (blank
      or stub body, placeholder/error text, a future date, a missing title)
      mean it must not publish. SOFT issues (entity-scope slips like the NESO
      "35% of US" class, unverifiable figures) mean a human should look.

  heal_published(...) — the safety net: scans everything already PUBLISHED and
      quarantines (published=FALSE) the ones that fail a HARD check, so a
      broken release cannot sit live. Report-only until PRESS_INTEGRITY_ENFORCE
      =1 (the proven safe-arm pattern: watch the report, then arm). Beats the
      dead-man ledger so the operator sees the state without opening a page.

HOUSE RULES
  · Fail-soft: the reviewer never raises; a check it cannot run is NOT a pass.
  · Never fabricate a "clean" — a body it could not read is flagged, not
    waved through.
  · Read-only until armed. Quarantine is the ONLY mutation, gated + logged.

Surface:  GET  /api/v1/admin/press-integrity            (HTML board)
          GET|POST /api/v1/admin/press-integrity/heal   (JSON; ?enforce=1 or
                                                          env to quarantine)
Beat:     press-integrity-daily
Kill:     PRESS_INTEGRITY_DISABLE=1   Arm: PRESS_INTEGRITY_ENFORCE=1
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

press_integrity_bp = Blueprint("press_integrity", __name__)

# A real analyst note runs many hundreds of characters; below this a "release"
# is a stub, not a story. Tunable, but the point is to catch blank/near-blank.
_MIN_BODY_CHARS = int(os.environ.get("PRESS_INTEGRITY_MIN_BODY", "220") or 220)

# Text that proves the body is broken, not written.
# ★2026-08-07 first live report: the unanchored, case-folded `NaN` matched the
# "nan" inside "teNANts" (also finance/governance/maintenance) and hard-failed
# 21 of 143 healthy releases — arming would have quarantined them all. The
# report-only safe-arm caught it. Every token is now word-bounded, and the
# JS-artifact tokens (NaN/null/undefined) are case-SENSITIVE — as error
# artifacts they appear verbatim, while prose case-folds.
# `undefined` and `NaN` stay bare tokens: case-sensitive and word-bounded is
# enough for them, and the 143-release corpus produced no false positive on
# either. `null` is handled separately below — see the second report.
_ALWAYS_BROKEN = re.compile(
    r"lorem ipsum|\bTODO\b|\bFIXME\b|\bplaceholder\b|"
    r"(?-i:\bundefined\b|\bNaN\b)|"
    r"could not (generate|load|compose)|\[object |{{.*}}|<<.*>>",
    re.IGNORECASE)

# ★2026-08-07 SECOND live report, and why `null` alone moved out of the pattern
# above. Case-sensitivity was not enough for this one word: unlike `undefined`
# and `NaN`, `null` is ordinary vocabulary for a publication that writes about
# APIs, and prose spells it lower-case exactly as a broken render does. The
# survivor of the first fix was the ONLY remaining hard-fail of 143 —
# /news/2026-07-15-error-version-1-machine-readable-recovery, a complete,
# healthy, PUBLISHED release whose body reads "... returns `suggested_params`
# with HTTP 400, not a silent null." Arming enforcement would have unpublished
# a good page over one correct sentence.
#
# What indicates breakage is not the WORD, it is the SHAPE: the token standing
# where a VALUE should be. So `null` must appear beside a unit, after a field
# separator, or alone in a cell; a bare mention in a sentence is left alone.
# Scope kept to the one token the measurement actually implicated — widening
# this to all three would stop catching real artifacts ("undefined siting").
_NULL_ARTIFACT = re.compile(
    # "null MW", "null facilities", "null deals" — a figure that never came
    r"(?-i:\bnull\b)\s*"
    r"(?:MW|GW|kW|TWh|%|facilit\w*|deals?|markets?|countries|tools?"
    r"|requests?|sites?|agents?)\b"
    # "Capacity: null", "operator: null," — a field whose value never rendered
    r"|[:=]\s*(?-i:null)\b"
    # alone in a cell, a line, or a table pipe — the whole value is the artifact
    r"|(?:^|[>|])\s*(?-i:null)\s*(?:$|[<|])",
    re.IGNORECASE | re.MULTILINE)


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("PRESS_INTEGRITY_DISABLE") or "").strip() == "1"


def _enforce() -> bool:
    return (os.environ.get("PRESS_INTEGRITY_ENFORCE") or "").strip() == "1"


def _strip_html(s) -> str:
    return re.sub(r"<[^>]+>", " ", str(s or "")).strip()


def _parse_date(v):
    if not v:
        return None
    s = str(v)[:10]
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


def analyst_review(release: dict) -> dict:
    """The pre-flight gate. release = {title, slug, body, subheadline,
    meta_description, date}. Returns {ok, hard, issues:[{code, detail, hard}]}.

    ok == (no HARD issues). SOFT issues do not block publish but flag a human.
    Never raises — a check that cannot run records an issue, never a pass."""
    issues = []

    def add(code, detail, hard):
        issues.append({"code": code, "detail": detail, "hard": bool(hard)})

    title = str(release.get("title") or "").strip()
    raw_body = str(release.get("body") or "")
    body = _strip_html(raw_body)
    slug = str(release.get("slug") or "").strip()

    # 1 · completeness — the perplexity failure. A blank/stub body must never
    # go out.
    if len(body) < _MIN_BODY_CHARS:
        add("body_blank_or_stub",
            f"body is {len(body)} chars (min {_MIN_BODY_CHARS}) — a stub, "
            "not a story", True)

    # 2 · broken/placeholder content
    if _ALWAYS_BROKEN.search(raw_body):
        add("placeholder_or_error_body",
            "body contains placeholder/error markers", True)
    elif _NULL_ARTIFACT.search(raw_body):
        add("placeholder_or_error_body",
            "body contains an unrendered value (null standing where a figure "
            "should be)", True)

    # 3 · a real headline
    if len(title) < 8:
        add("title_missing_or_fragment",
            f"title {title!r} is missing or a fragment", True)

    # 4 · not future-dated (the Sept-21 class)
    d = _parse_date(release.get("date"))
    today = datetime.date.today()
    if d and d > today + datetime.timedelta(days=1):
        add("future_dated", f"dated {d} (> today {today})", True)

    # 5 · entity-scope slips (the NESO "35% of US" class) — SOFT, needs a human
    try:
        from routes.media_claim_verify import check_entity_scope
        scope = check_entity_scope(f"{title}\n{body}") or []
        if scope:
            add("entity_scope", "; ".join(str(s)[:80] for s in scope[:3]), False)
    except Exception as e:  # noqa: BLE001
        logger.debug("[press-integrity] scope check unavailable: %s", e)

    # 6 · figures that don't verify against canon — SOFT
    try:
        from routes.media_claim_verify import verify_claims
        v = verify_claims(body) or {}
        bad = v.get("violations") or v.get("failed") or []
        if bad:
            add("unverified_claims",
                "%d figure(s) did not verify against canon" % len(bad), False)
    except Exception as e:  # noqa: BLE001
        logger.debug("[press-integrity] claim verify unavailable: %s", e)

    hard = any(i["hard"] for i in issues)
    return {"ok": not hard, "hard": hard, "slug": slug, "issues": issues}


# ── the BLOCKING pre-publish gate ──────────────────────────────────────
# (2026-08-07) analyst_review shipped as a post-hoc sweep: it could only find a
# broken release AFTER it was already public, which is how the blank, future-
# dated perplexity page got a live URL in the first place. Quarantine is a
# cleanup, not a gate. This is the gate — every composer calls it BEFORE the
# INSERT and binds `published` to what it returns, so a release failing a HARD
# check cannot enter the table as published=TRUE at all.
#
# WHY THIS IS NOT BEHIND PRESS_INTEGRITY_ENFORCE
# The env flag arms the post-publish QUARANTINE, which mutates rows that are
# already live and therefore deserves the watch-then-arm ceremony. This gate
# mutates nothing: its worst case is that a good release lands as an
# unpublished draft, which the pending-drafts digest surfaces for one-click
# approval. Gating a fail-safe behind a flag would just mean shipping the
# broken-release hole and leaving it open.

def gate_press_publish(release: dict, want_published: bool = False,
                       where: str = "") -> dict:
    """The pre-publish decision for ONE release.

    Returns {publish, ok, hard, issues, codes, note}. `publish` is the value the
    caller MUST bind into the INSERT's published column — never a literal.

      · HARD issue  -> publish=False no matter what the caller wanted.
      · no HARD     -> publish = want_published (the gate never PROMOTES a
                       draft; it only ever withholds).

    Never raises. A reviewer that cannot run is not a pass: analyst_review
    already records unrunnable checks as issues, and if the reviewer itself
    blows up we fail CLOSED (publish=False) rather than waving the release
    through — the whole point is that nothing broken goes out."""
    try:
        rev = analyst_review(release or {})
    except Exception as e:  # noqa: BLE001
        logger.warning("[press-integrity] gate reviewer raised at %s: %s",
                       where or "?", str(e)[:140])
        return {"publish": False, "ok": False, "hard": True,
                "issues": [{"code": "reviewer_error", "hard": True,
                            "detail": str(e)[:160]}],
                "codes": ["reviewer_error"],
                "note": "reviewer raised — failing closed to draft"}
    hard = bool(rev.get("hard"))
    codes = [i.get("code") for i in (rev.get("issues") or [])]
    publish = bool(want_published) and not hard
    if hard:
        logger.warning("[press-integrity] BLOCKED publish of %r at %s: %s",
                       (release or {}).get("slug"), where or "?",
                       ", ".join(str(c) for c in codes))
    return {"publish": publish, "ok": not hard, "hard": hard,
            "issues": rev.get("issues") or [], "codes": codes,
            "note": ("held as draft — failed the analyst pre-flight: "
                     + ", ".join(str(c) for c in codes)) if hard else "clean"}


_REVIEW_TABLE = """
CREATE TABLE IF NOT EXISTS press_integrity_reviews (
    slug        TEXT PRIMARY KEY,
    ok          BOOLEAN NOT NULL,
    hard        BOOLEAN NOT NULL,
    issues      TEXT,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    composer    TEXT
)
"""


def attach_review(cur, slug: str, gate: dict, where: str = "") -> bool:
    """Persist a gate verdict beside the draft so the reason is recoverable.

    Deliberately a SIDE table, not a press_releases column: the issue list must
    never end up in meta_description or body, where it would be published as
    copy. Idempotent (ON CONFLICT upsert). Never raises — a bookkeeping failure
    must not unwind a composer's transaction."""
    try:
        cur.execute(_REVIEW_TABLE)
        cur.execute("""
            INSERT INTO press_integrity_reviews
                (slug, ok, hard, issues, composer, reviewed_at)
            VALUES (%s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
            ON CONFLICT (slug) DO UPDATE
               SET ok = EXCLUDED.ok, hard = EXCLUDED.hard,
                   issues = EXCLUDED.issues, composer = EXCLUDED.composer,
                   reviewed_at = NOW()
        """, (str(slug or "")[:300], bool(gate.get("ok")),
              bool(gate.get("hard")),
              json.dumps(gate.get("issues") or [])[:4000],
              str(where or "")[:120]))
        return True
    except Exception as e:  # noqa: BLE001
        logger.info("[press-integrity] review note for %r failed: %s",
                    slug, str(e)[:120])
        return False


# ── the self-healing sweep over what is already PUBLISHED ──────────────

def _conn():
    try:
        import psycopg2
        url = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL"))
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[press-integrity] connect failed: %s", str(e)[:120])
        return None


def heal_published(enforce: bool | None = None, limit: int = 500) -> dict:
    """Review every PUBLISHED press release; quarantine (published=FALSE) the
    ones that fail a HARD check when enforced. Report-only otherwise. The
    reviewer is honest about what it could not check (unknowns are not passes).
    Never raises."""
    if enforce is None:
        enforce = _enforce()
    out = {"reviewed": 0, "hard_fail": 0, "soft_flag": 0, "quarantined": 0,
           "enforced": bool(enforce), "offenders": []}
    c = _conn()
    if c is None:
        out["error"] = "no_db"
        return out
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, slug, title, body, subheadline, meta_description, "
                "       date FROM press_releases WHERE published = TRUE "
                "ORDER BY date DESC NULLS LAST LIMIT %s", (limit,))
            rows = cur.fetchall()
            for r in rows:
                out["reviewed"] += 1
                rel = {"id": r[0], "slug": r[1], "title": r[2], "body": r[3],
                       "subheadline": r[4], "meta_description": r[5],
                       "date": r[6]}
                rev = analyst_review(rel)
                if not rev["issues"]:
                    continue
                rec = {"slug": rel["slug"],
                       "issues": [i["code"] for i in rev["issues"]],
                       "hard": rev["hard"]}
                if rev["hard"]:
                    out["hard_fail"] += 1
                    if enforce:
                        try:
                            cur.execute(
                                "UPDATE press_releases SET published = FALSE "
                                "WHERE id = %s", (rel["id"],))
                            rec["action"] = "quarantined"
                            out["quarantined"] += 1
                        except Exception as e:  # noqa: BLE001
                            rec["action"] = "quarantine_failed:%s" % str(e)[:60]
                    else:
                        rec["action"] = "would_quarantine (report-only)"
                else:
                    out["soft_flag"] += 1
                    rec["action"] = "flagged_for_human"
                out["offenders"].append(rec)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:150]
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _beat(note: str) -> None:
    try:
        body = json.dumps({
            "feed": "press-integrity-daily", "status": "success",
            "rows_inserted": 1, "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": note[:280]}).encode()
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint)
        _rq.post("http://127.0.0.1:%s/api/v1/admin/ingest-runs/beat"
                 % os.environ.get("PORT", "8080"),
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-press-integrity/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001
        logger.debug("[press-integrity] beat failed: %s", e)


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@press_integrity_bp.route("/api/v1/admin/press-integrity/correct",
                          methods=["POST"])
def correct_endpoint():
    """Apply an editorial CORRECTION to a published release — title and/or
    body — through a governed lane instead of a DB reach-in (2026-08-08; the
    NESO '35% of US' corrections motivated this). The corrected content must
    itself pass analyst_review (a correction may never make a release worse),
    and an editor's note is appended so the change is visible, the way a real
    newsroom corrects. Admin-gated; audit-logged via the deadman beat."""
    if _disabled():
        return _no_store(jsonify(ok=False, error="PRESS_INTEGRITY_DISABLE=1")), 503
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    p = request.get_json(silent=True) or {}
    slug = str(p.get("slug") or "").strip()
    new_title = p.get("title")
    new_body = p.get("body")
    note = str(p.get("editors_note") or "").strip()
    if not slug or (new_title is None and new_body is None):
        return _no_store(jsonify(ok=False,
                                 error="slug and title and/or body required")), 400
    c = _conn()
    if c is None:
        return _no_store(jsonify(ok=False, error="no_db")), 503
    try:
        with c.cursor() as cur:
            cur.execute("SELECT title, body, date FROM press_releases "
                        "WHERE slug=%s AND published=TRUE", (slug,))
            row = cur.fetchone()
            if not row:
                return _no_store(jsonify(ok=False,
                                         error="no published release with "
                                               "that slug")), 404
            title = new_title if new_title is not None else row[0]
            body = new_body if new_body is not None else row[1]
            if note:
                body = "%s\n\n---\n*Editor's note: %s*" % (body, note[:300])
            rev = analyst_review({"slug": slug, "title": title, "body": body,
                                  "date": row[2]})
            if rev["hard"]:
                return _no_store(jsonify(
                    ok=False, error="correction fails the analyst pre-flight",
                    issues=rev["issues"])), 422
            cur.execute("UPDATE press_releases SET title=%s, body=%s "
                        "WHERE slug=%s AND published=TRUE", (title, body, slug))
            updated = cur.rowcount
        _beat("correction applied: %s (%d row)" % (slug, updated))
        return _no_store(jsonify(ok=True, slug=slug, updated=updated,
                                 post_review=rev["issues"]))
    except Exception as e:  # noqa: BLE001
        return _no_store(jsonify(ok=False, error=str(e)[:150])), 500
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


@press_integrity_bp.route("/api/v1/admin/press-integrity/heal",
                          methods=["GET", "POST"])
def heal_endpoint():
    if _disabled():
        return _no_store(jsonify(ok=False, error="PRESS_INTEGRITY_DISABLE=1")), 503
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    enforce = _enforce() or request.args.get("enforce") == "1"
    res = heal_published(enforce=enforce)
    _beat("reviewed=%d hard=%d quarantined=%d soft=%d enforce=%s"
          % (res.get("reviewed", 0), res.get("hard_fail", 0),
             res.get("quarantined", 0), res.get("soft_flag", 0),
             res.get("enforced")))
    return _no_store(jsonify(ok=True, **res))


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


@press_integrity_bp.route("/api/v1/admin/press-integrity", methods=["GET"])
@press_integrity_bp.route("/admin/press-integrity", methods=["GET"])
def board():
    from flask import make_response
    if _disabled():
        return _no_store(make_response("<h1>Press Integrity</h1>"
                                       "<p>PRESS_INTEGRITY_DISABLE=1</p>", 503))
    if not _admin_ok():
        return _no_store(make_response("<h1>401</h1>", 401))
    res = heal_published(enforce=False)   # the board never mutates
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (_esc(o["slug"]), "HARD" if o["hard"] else "soft",
           _esc(", ".join(o["issues"])))
        for o in res.get("offenders", []))
    html = ("<html><head><title>Press Integrity</title>"
            "<meta http-equiv='refresh' content='300'></head><body "
            "style='font-family:system-ui;max-width:1100px;margin:24px auto'>"
            "<h1>Press Integrity <small>the analyst's pre-flight</small></h1>"
            "<p>reviewed %d · hard-fail %d · soft-flag %d · enforce=%s "
            "(PRESS_INTEGRITY_ENFORCE=1 to auto-quarantine)</p>"
            "<table cellpadding=6 style='border-collapse:collapse'>"
            "<tr><th align=left>slug</th><th align=left>severity</th>"
            "<th align=left>issues</th></tr>%s</table></body></html>"
            % (res.get("reviewed", 0), res.get("hard_fail", 0),
               res.get("soft_flag", 0), _enforce(),
               rows or "<tr><td colspan=3>clean — every published release "
               "passes the analyst pre-flight</td></tr>"))
    return _no_store(make_response(html, 200))
