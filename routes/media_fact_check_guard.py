"""media_fact_check_guard.py — the media SAFETY FOUNDATION (2026-06-23).

WHY THIS EXISTS
===============
The operator just had an auto-published post disparage a partner and nearly
shipped a "225 GW" stat the live data reports as tracked:false. Two failure
modes there:
  1. A composed claim quoted a NUMBER that ground truth does not corroborate.
  2. Nothing forced the composer to PROVE each figure before it could queue.

routes/media_claim_verify.verify_claims() already catches the KNOWN-BANNED
retired over-claims (the inflated facility count, the unverified M&A dollar
aggregate, stale market counts) and CANON over-claims (a counted metric that
exceeds canonical_stats beyond tolerance). It is the right gate for *retired
lies* and *count over-claims*, and it stays the publish-time block wired into
content_publisher._should_skip_publish.

This module STRENGTHENS that with a complementary, per-claim CORROBORATION
pass that the other four media modules call BEFORE they queue anything:

    verify_media_text(text) -> {
        ok:        bool,                  # True iff nothing landed in unverified
        unverified:[{claim, found_live, expected}],  # every claim we could NOT corroborate
        checked:   int,                   # how many numeric claims we examined
        ...
    }

It extracts numeric claims (market/facility/country/deal counts, GW/MW, %,
time-to-power months, DCPI verdicts named with a market) and verifies EACH
against LIVE data:
  - canonical_stats.get_canonical_stats() ............ counts (facilities,
        countries, markets) — a count claim is corroborated only if it is at or
        below the live canon within tolerance (rounding DOWN is always safe).
  - market_power_scores (read-only autocommit query) . per-market DCPI verdict
        + time-to-power months + the derived composite_score, so a sentence like
        "DCPI rates Ashburn BUILD" or "~36 months to power in Phoenix" is checked
        against the row the platform actually published.
  - GW/MW and bare % .................................. NOT corroborable from a
        structured source here → always reported unverified (omit-or-prove). This
        is exactly the "225 GW" failure: an aggregate magnitude we cannot prove
        live MUST be treated as unverified by construction, never silently passed.

THE PRINCIPLE (inherited from media_claim_verify): verify the value is TRUE,
not merely PRESENT — and here, every numeric claim is GUILTY until corroborated.

HARD SAFETY CONTRACT
====================
  * verify_media_text() NEVER raises and NEVER fabricates. On any internal
    failure it FAILS CLOSED for the affected claim (reports it unverified) so a
    lookup blip can never green-light an unproven number. The whole-function
    wrapper degrades to ok:False with an explanatory note — it can refuse, it
    can never invent.
  * The module imports cleanly with NO env and NO DB (every optional import,
    DB handle, and LLM call is wrapped) so registering the blueprint can never
    crash app boot.
  * The standalone admin endpoint + any cron are DARK unless
    MEDIA_FACT_CHECK_GUARD_ENABLED is truthy. The verify_media_text() FUNCTION
    itself always works when called in-process (that is the gate the other
    modules rely on); only the externally-reachable surface is flag-gated.
  * No auto-send, no auto-publish: this module produces a REPORT. It writes
    nothing live and posts to no external surface.

Admin endpoint (flag-gated, admin-keyed):
  POST /api/v1/admin/media/fact-check   {"text": "..."}  -> the report
"""
from __future__ import annotations

import os
import re
import logging

logger = logging.getLogger(__name__)

try:
    from flask import Blueprint, jsonify, request
    media_fact_check_guard_bp = Blueprint("media_fact_check_guard", __name__)
except Exception:  # pragma: no cover - Flask must be importable in prod, but never crash boot
    Blueprint = None
    media_fact_check_guard_bp = None


# ── kill switch (DARK BY DEFAULT) ────────────────────────────────────────────
FLAG = "MEDIA_FACT_CHECK_GUARD_ENABLED"


def _enabled() -> bool:
    """The standalone endpoint/cron is OFF unless explicitly enabled. The
    verify_media_text() function still works in-process regardless — only the
    externally-reachable surface is gated."""
    return os.environ.get(FLAG, "").strip().lower() in ("1", "true", "yes", "on")


# ── tolerances ───────────────────────────────────────────────────────────────
# A COUNT claim ("300+ markets", "21,000+ facilities") is corroborated when it is
# at or below the live canon within this slack (rounding DOWN to a clean floor is
# always safe; the canonical_stats *_phrase helpers do exactly that). A figure
# ABOVE the live canon by more than this is an over-claim → unverified.
_COUNT_OVER_FRAC = 0.05     # >5% above live canon = unverified over-claim
_COUNT_UNDER_FRAC = 0.60    # absurdly below (<40% of live) = probably a different metric
# A time-to-power-months claim must land within this of the live row.
_TTP_TOL_MONTHS = 2.0


# ── numeric extraction (reuse media_claim_verify's regexes, fail-safe) ───────
def _import_claim_verify():
    """Return the media_claim_verify module or None. Never raises."""
    try:
        from routes import media_claim_verify as mcv
        return mcv
    except Exception as e:
        logger.warning("[fact_check_guard] media_claim_verify import failed: %s", str(e)[:160])
        return None


# Local fallback figure regex — only used if media_claim_verify can't be
# imported, so extraction never silently returns nothing. Mirrors the shape of
# media_claim_verify._FIGURE_RE.
_FALLBACK_FIGURE_RE = re.compile(
    r"""
    (?P<dollar>\$\s*)?
    (?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)
    \s*
    (?P<mag>billion|million|thousand|[BMK](?![A-Za-z]))?
    \+?
    \s*
    (?P<unit>%|GW|MW|kW|facilit\w*|countr\w*|market\w*|deal\w*|month\w*|dollars?)?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_FALLBACK_MAG = {"billion": 1e9, "b": 1e9, "million": 1e6, "m": 1e6,
                 "thousand": 1e3, "k": 1e3}


def _extract_numeric_claims(text: str) -> list[dict]:
    """Pull numeric claims out of `text`. Each: {raw, value, metric, is_dollar}.
    Prefers media_claim_verify.extract_claims (single source of truth for the
    extraction grammar); falls back to a local regex. Adds a 'months' metric for
    time-to-power. Never raises — returns [] on total failure."""
    if not text:
        return []
    mcv = _import_claim_verify()
    claims: list[dict] = []
    if mcv is not None and hasattr(mcv, "extract_claims"):
        try:
            claims = list(mcv.extract_claims(text) or [])
        except Exception as e:
            logger.warning("[fact_check_guard] mcv.extract_claims failed: %s", str(e)[:160])
            claims = []
    # The shared extractor doesn't bucket 'months'; detect time-to-power phrasing
    # ("~36 months to power", "36 months to power", "time to power: 36mo") and
    # re-tag those numbers so we can corroborate them against the live row.
    try:
        for m in re.finditer(
                r"(\d+(?:\.\d+)?)\s*(?:mo\b|months?\b)(?:\s*(?:to|until)\s*power)?",
                text, re.I):
            val = float(m.group(1))
            raw = m.group(0).strip()
            # avoid duplicating one the shared extractor already returned verbatim
            if not any(abs((c.get("value") or -1) - val) < 1e-9 and "month" in (c.get("raw") or "").lower()
                       for c in claims):
                claims.append({"raw": raw, "value": val, "metric": "months", "is_dollar": False})
    except Exception:
        pass
    # If the shared extractor was unavailable, fall back to the local regex.
    if not claims and (mcv is None):
        try:
            for m in _FALLBACK_FIGURE_RE.finditer(text):
                try:
                    base = float(m.group("num").replace(",", ""))
                except Exception:
                    continue
                mag = (m.group("mag") or "").lower()
                value = base * _FALLBACK_MAG.get(mag, 1.0)
                unit = (m.group("unit") or "").lower()
                is_dollar = bool(m.group("dollar")) or unit.startswith("dollar")
                if unit.startswith("facilit"):
                    metric = "facilities"
                elif unit.startswith("countr"):
                    metric = "countries"
                elif unit.startswith("market"):
                    metric = "markets"
                elif unit.startswith("deal"):
                    metric = "deals"
                elif unit.startswith("month"):
                    metric = "months"
                elif unit == "gw":
                    metric = "gw"
                elif unit in ("mw", "kw"):
                    metric = "mw"
                elif unit == "%":
                    metric = "percent"
                elif is_dollar:
                    metric = "dollars"
                else:
                    metric = "number"
                claims.append({"raw": m.group(0).strip(), "value": value,
                               "metric": metric, "is_dollar": is_dollar})
        except Exception as e:
            logger.warning("[fact_check_guard] fallback extract failed: %s", str(e)[:160])
    return claims


# ── live ground-truth lookups (all read-only, all fail-safe) ─────────────────
def _live_canon() -> dict:
    """Live canonical counts. Returns {} on any failure (so a count claim then
    lands unverified — fail CLOSED, never invent a corroborating number)."""
    try:
        import canonical_stats as cs
        s = cs.get_canonical_stats() or {}
        out = {}
        for k in ("facilities", "facilities_verified", "countries", "markets"):
            v = s.get(k)
            if isinstance(v, (int, float)) and v > 0:
                out[k] = int(v)
        return out
    except Exception as e:
        logger.warning("[fact_check_guard] canon lookup failed: %s", str(e)[:160])
        return {}


def _db_conn():
    """Read-only autocommit psycopg2 connection, or None. Autocommit so a failed
    SELECT can't poison a shared transaction (the _live_as_of/_build_brief trap).
    Never raises."""
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=6)
        try:
            c.autocommit = True
        except Exception:
            pass
        return c
    except Exception as e:
        logger.warning("[fact_check_guard] db connect failed: %s", str(e)[:160])
        return None


# A DCPI verdict named with a market: "Ashburn ... BUILD", "rates Phoenix AVOID".
_VERDICTS = ("BUILD", "CAUTION", "AVOID", "WATCH", "LOW_SIGNAL")
_VERDICT_RE = re.compile(
    r"\b([A-Z][A-Za-z][A-Za-z .\-]{1,38}?)\b[^.\n]{0,40}?\b(BUILD|CAUTION|AVOID|WATCH)\b"
    r"|\b(BUILD|CAUTION|AVOID|WATCH)\b[^.\n]{0,24}?\b([A-Z][A-Za-z][A-Za-z .\-]{1,38}?)\b",
)


def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _market_row(conn, market_text: str) -> dict | None:
    """Look up the latest published market_power_scores row by name OR slug.
    Returns a dict with verdict / scores / time_to_power_months / composite_score,
    or None if not found / on error. Read-only; never raises."""
    if conn is None or not market_text:
        return None
    name = market_text.strip()
    slug = _slugify(name)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (market_slug)
                   market_slug, market_name, state, iso,
                   constraint_score, excess_power_score,
                   time_to_power_months, verdict
              FROM market_power_scores
             WHERE published = true
               AND (lower(market_slug) = %s
                    OR lower(market_name) = %s
                    OR lower(market_name) LIKE %s)
             ORDER BY market_slug, computed_at DESC
             LIMIT 1
            """,
            (slug, name.lower(), name.lower() + "%"),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        try:
            from routes.dcpi import derive_composite_score
            d["composite_score"] = derive_composite_score(
                d.get("excess_power_score"), d.get("constraint_score"),
                d.get("time_to_power_months"), d.get("verdict"))
        except Exception:
            d["composite_score"] = d.get("excess_power_score")
        return d
    except Exception as e:
        logger.warning("[fact_check_guard] market_row(%r) failed: %s", market_text, str(e)[:160])
        return None


# ── the verifier ─────────────────────────────────────────────────────────────
def verify_media_text(text: str) -> dict:
    """Corroborate every numeric/DCPI claim in `text` against LIVE ground truth.

    Returns:
      {
        ok:         bool,    # True iff unverified == []
        unverified: [ {claim, found_live, expected} ],
        checked:    int,     # numeric claims examined (excludes verdict claims, counted separately)
        verdict_checked: int,
        canon:      {...},   # the live canon snapshot used (transparency)
        note:       str,     # set only on a whole-function degrade
      }

    Contract: NEVER raises, NEVER fabricates a corroborating value. A magnitude
    we cannot prove from a structured source (GW/MW totals, bare %) is reported
    unverified by construction — that is the "225 GW" guard. On any internal
    failure for a given claim, that claim FAILS CLOSED (reported unverified)."""
    unverified: list[dict] = []
    checked = 0
    verdict_checked = 0
    canon: dict = {}
    try:
        text = text or ""
        canon = _live_canon()
        claims = _extract_numeric_claims(text)

        # ---- numeric claims ----
        for c in claims:
            metric = c.get("metric")
            val = c.get("value")
            raw = c.get("raw") or ""
            if val is None:
                continue
            checked += 1

            if metric in ("facilities", "countries", "markets"):
                # 'facilities' may legitimately quote either the tracked floor or
                # the verified subset — corroborate against the LARGER (tracked).
                live = canon.get(metric)
                if metric == "facilities":
                    live = canon.get("facilities") or canon.get("facilities_verified")
                if not live:
                    unverified.append({
                        "claim": raw,
                        "found_live": None,
                        "expected": f"live {metric} count unavailable — cannot corroborate (omit it)",
                    })
                    continue
                if val > live * (1.0 + _COUNT_OVER_FRAC):
                    unverified.append({
                        "claim": raw,
                        "found_live": live,
                        "expected": f"<= {live:,} live {metric} (claim over-states by >5%)",
                    })
                elif val < live * _COUNT_UNDER_FRAC:
                    unverified.append({
                        "claim": raw,
                        "found_live": live,
                        "expected": f"~{live:,} live {metric} (claim is far below — likely a different metric, verify)",
                    })
                # else: corroborated (at/below live within tolerance) — not added.

            elif metric == "gw":
                # No structured live total to prove an aggregate GW figure here.
                # This is EXACTLY the "225 GW tracked:false" case — fail closed.
                unverified.append({
                    "claim": raw,
                    "found_live": None,
                    "expected": "no live aggregate GW source to corroborate — omit unless a verified per-source figure is cited",
                })

            elif metric == "mw":
                unverified.append({
                    "claim": raw,
                    "found_live": None,
                    "expected": "per-facility/market MW not corroborated by this guard — cite the specific facility's verified capacity or omit",
                })

            elif metric == "percent":
                unverified.append({
                    "claim": raw,
                    "found_live": None,
                    "expected": "bare percentage not corroborated against a live source — cite the underlying figures or omit",
                })

            elif metric == "months":
                # Time-to-power: corroborate against ANY market row named in the
                # text. If no market is named we can't tie the number to a row.
                live_ttp = _ttp_live_match(text, val)
                if live_ttp is None:
                    unverified.append({
                        "claim": raw,
                        "found_live": None,
                        "expected": "time-to-power not tied to a live market row — name the market so it can be checked, or omit",
                    })
                elif abs(live_ttp[1] - val) > _TTP_TOL_MONTHS:
                    unverified.append({
                        "claim": raw,
                        "found_live": f"{live_ttp[0]}: ~{live_ttp[1]:.0f} months",
                        "expected": f"~{live_ttp[1]:.0f} months for {live_ttp[0]} (live), claim says {val:.0f}",
                    })
                # else corroborated.

            elif metric in ("deals", "dollars", "number"):
                # 'deals' count and dollar aggregates: media_claim_verify owns the
                # banned-aggregate block; here we only fail closed for dollar
                # aggregates and leave incidental numbers to that gate.
                if metric == "dollars":
                    unverified.append({
                        "claim": raw,
                        "found_live": None,
                        "expected": "dollar aggregate not corroborated — say '2,000+ tracked deals' or cite a verified single-deal figure",
                    })
                # bare 'number'/'deals' counts: left to media_claim_verify's canon
                # + banned checks (run separately by callers); not flagged here to
                # avoid false positives on incidental numbers (years, list counts).

        # ---- DCPI verdict-with-market claims ----
        conn = None
        try:
            if _text_has_verdict(text):
                conn = _db_conn()
                for market_name, verdict in _verdict_pairs(text):
                    verdict_checked += 1
                    row = _market_row(conn, market_name) if conn is not None else None
                    if row is None:
                        unverified.append({
                            "claim": f"{market_name} {verdict}",
                            "found_live": None,
                            "expected": f"no live DCPI row for '{market_name}' — cannot corroborate verdict (omit)",
                        })
                        continue
                    live_v = (row.get("verdict") or "").upper()
                    if live_v != verdict.upper():
                        unverified.append({
                            "claim": f"{market_name} {verdict}",
                            "found_live": f"{row.get('market_name')}: {live_v or 'unscored'}",
                            "expected": f"DCPI verdict for {row.get('market_name')} is {live_v or 'unscored'}, not {verdict}",
                        })
                    # else corroborated.
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    except Exception as e:
        # Whole-function degrade: fail CLOSED. We refuse, we do not invent.
        logger.warning("[fact_check_guard] verify_media_text degraded: %s", str(e)[:200])
        return {
            "ok": False,
            "unverified": [{"claim": "(whole text)", "found_live": None,
                            "expected": f"fact-check unavailable ({str(e)[:120]}) — withhold until it can run"}],
            "checked": checked,
            "verdict_checked": verdict_checked,
            "canon": canon,
            "note": "verifier degraded — failed closed",
        }

    return {
        "ok": not unverified,
        "unverified": unverified,
        "checked": checked,
        "verdict_checked": verdict_checked,
        "canon": canon,
    }


def _text_has_verdict(text: str) -> bool:
    up = (text or "").upper()
    return any(v in up for v in ("BUILD", "CAUTION", "AVOID", "WATCH"))


def _verdict_pairs(text: str) -> list[tuple[str, str]]:
    """Best-effort (market_name, verdict) pairs from the text. Never raises."""
    pairs: list[tuple[str, str]] = []
    if not text:
        return pairs
    try:
        for m in _VERDICT_RE.finditer(text):
            # group 1+2 = "<Market> ... VERDICT"; group 3+4 = "VERDICT ... <Market>"
            if m.group(2):
                name, verdict = m.group(1), m.group(2)
            else:
                verdict, name = m.group(3), m.group(4)
            name = (name or "").strip(" .-")
            verdict = (verdict or "").strip().upper()
            # filter obvious non-markets: too-generic leading words
            if name and verdict and name.lower() not in (
                    "dc hub", "dchub", "the", "dcpi", "ai", "us", "data center",
                    "data centre", "build", "caution", "avoid", "watch"):
                pairs.append((name, verdict))
    except Exception as e:
        logger.warning("[fact_check_guard] verdict_pairs failed: %s", str(e)[:160])
    # de-dup
    seen = set()
    out = []
    for n, v in pairs:
        k = (n.lower(), v)
        if k not in seen:
            seen.add(k)
            out.append((n, v))
    return out[:12]


def _ttp_live_match(text: str, claimed_months: float):
    """If the text names a market, return (market_name, live_ttp_months) for the
    closest live row; else None. Read-only; never raises."""
    pairs = _verdict_pairs(text)
    if not pairs:
        return None
    conn = _db_conn()
    if conn is None:
        return None
    try:
        for market_name, _v in pairs:
            row = _market_row(conn, market_name)
            ttp = row.get("time_to_power_months") if row else None
            if isinstance(ttp, (int, float)) and ttp:
                return (row.get("market_name") or market_name, float(ttp))
        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── combined gate the OTHER media modules call before queueing ───────────────
def gate_media_text(cur, text: str, platform: str = "linkedin") -> dict:
    """One-call pre-queue gate for the other media modules. Runs ALL THREE guards
    and returns a single allow/deny verdict + the reasons. Drop anything denied.

      1. content_publisher._should_skip_publish(cur, text, platform)
            — the live media-judgment + claim-verify + dedup + quality gate.
      2. routes.media_claim_verify.verify_claims(text)
            — banned over-claims + canon over-claims (hard blocks).
      3. verify_media_text(text)  [this module]
            — per-claim corroboration; ANY unverified item denies.

    Returns {allow: bool, reasons: [str], fact_check: {...}}.
    Fail CLOSED on any guard error for the SAFETY guards (2 & 3): an unprovable
    number must never slip through because a checker hiccuped. (#1 is the
    existing publisher gate and keeps its own fail-open posture for transient DB
    blips; we surface but do not override it.)"""
    reasons: list[str] = []
    allow = True

    # (1) publisher gate — partner-disparagement + LLM-disclaimer + claim-verify-block + dedup + quality
    try:
        from content_publisher import _should_skip_publish
        skip, why = _should_skip_publish(cur, text or "", platform)
        if skip:
            allow = False
            reasons.append(f"publisher: {why}")
    except Exception as e:
        logger.warning("[fact_check_guard] _should_skip_publish unavailable: %s", str(e)[:160])
        reasons.append(f"publisher gate unavailable ({str(e)[:80]})")

    # (2) claim-verify — banned + canon over-claims
    try:
        from routes.media_claim_verify import verify_claims
        cv = verify_claims(text or "")
        if cv.get("blocks"):
            allow = False
            reasons.append("claim-verify: " + "; ".join(cv["blocks"])[:240])
    except Exception as e:
        # SAFETY guard — fail closed.
        allow = False
        logger.warning("[fact_check_guard] verify_claims unavailable: %s", str(e)[:160])
        reasons.append(f"claim-verify unavailable — failing closed ({str(e)[:80]})")

    # (3) this module — per-claim corroboration
    fc = verify_media_text(text or "")
    if not fc.get("ok"):
        allow = False
        for u in (fc.get("unverified") or [])[:8]:
            reasons.append(f"fact-check: {u.get('claim')} → {u.get('expected')}")

    return {"allow": allow, "reasons": reasons, "fact_check": fc}


# ── admin gate (mirrors brain_innovation._innov_admin_ok) ────────────────────
def _admin_ok() -> bool:
    try:
        _keys = set()
        for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            _v = os.environ.get(_n)
            if _v:
                _keys.add(_v)
        _sent = (request.headers.get("X-Internal-Key")
                 or request.headers.get("X-Admin-Key")
                 or request.args.get("admin_key") or "").strip()
        return bool(_sent) and _sent in _keys
    except Exception:
        return False


# ── admin endpoint (DARK unless flag on; admin-keyed) ────────────────────────
if media_fact_check_guard_bp is not None:

    @media_fact_check_guard_bp.route("/api/v1/admin/media/fact-check", methods=["POST"])
    def admin_fact_check():
        # DARK BY DEFAULT — does nothing unless the kill-switch is on.
        if not _enabled():
            return jsonify({"ok": True, "skipped": "disabled"}), 200
        if not _admin_ok():
            return jsonify({"ok": False, "error": "admin key required"}), 403
        try:
            data = request.get_json(silent=True) or {}
        except Exception:
            data = {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "missing 'text'"}), 400
        report = verify_media_text(text)
        # This is a REPORT ONLY — nothing is queued, sent, or published.
        return jsonify(report), 200

    @media_fact_check_guard_bp.route("/api/v1/admin/media/fact-check/health", methods=["GET"])
    def fact_check_health():
        if not _enabled():
            return jsonify({"ok": True, "skipped": "disabled"}), 200
        if not _admin_ok():
            return jsonify({"ok": False, "error": "admin key required"}), 403
        # Cheap liveness: can we read the canon? (no fabrication, just presence)
        canon = _live_canon()
        return jsonify({
            "ok": True,
            "flag": FLAG,
            "enabled": True,
            "canon_available": bool(canon),
            "canon_keys": sorted(canon.keys()),
            "guards": ["content_publisher._should_skip_publish",
                       "media_claim_verify.verify_claims",
                       "verify_media_text"],
        }), 200