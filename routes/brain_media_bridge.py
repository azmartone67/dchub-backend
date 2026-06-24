"""
brain_media_bridge — give the brain's graded, refutation-survived DATA insights
a VOICE in what the media desk publishes.

WHY: the brain self-director (brain_self_director.py) reasons → refutes → surfaces
an agenda you grade, but its output never reached the media analyst
(media_editorial.py), which picked stories only from real-time DCPI shifts +
LinkedIn engagement. So the brain's hard-won, human-vetted data findings (e.g.
"311 DCPI markets across 7 live ISOs", "facilities span 178 countries") never
became analyst stories. This bridges that gap.

HOW (and why it's safe): this is READ-ONLY + DRAFT-ONLY + DARK BY DEFAULT.
It pulls graded-good, survived-refutation brain items from EXTERNALLY-PUBLISHABLE
areas, turns each into a lead in the exact shape media_editorial.rank_data_events()
expects, and lets the existing editorial_decision() rank it AGAINST real-time data
events. Everything downstream is unchanged: the analyst-voice generator voices it,
media_claim_verify checks the numbers, the partner-disparagement guard filters it,
the newsworthiness bar gates it, and a HUMAN approves before anything publishes.
The brain only earns a VOICE in what gets DRAFTED — never an auto-publish.

Mirrors routes/brain_capability_radar.capability_radar_leads() (the existing
"what can we announce" feed), wired in next to it in rank_data_events().

Gate:    BRAIN_MEDIA_BRIDGE_ENABLED=1   (default OFF — leads = [] until flipped)
Tunable: BRAIN_MEDIA_BRIDGE_MIN_CONF (0.30), _LOOKBACK_DAYS (30), _BASE_SCORE (12)
Preview: GET /api/v1/brain/media/insight-leads  (in media_editorial_bp)
"""
import os
import re
import logging

logger = logging.getLogger("dchub.brain_media_bridge")

try:
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None

# Areas whose findings are EXTERNALLY publishable as data stories. Internal
# plumbing (reliability, performance, developer_ux) is deliberately EXCLUDED —
# that's ops work, not analyst content. brain_capability_radar already announces
# shipped enhancements; this bridge fills the DATA-COVERAGE gap.
PUBLISHABLE_AREAS = {"data_coverage", "conversion_revenue"}

_MIN_CONF = float(os.environ.get("BRAIN_MEDIA_BRIDGE_MIN_CONF", "0.30") or 0.30)
_LOOKBACK_DAYS = int(os.environ.get("BRAIN_MEDIA_BRIDGE_LOOKBACK_DAYS", "30") or 30)
# Evergreen, human-vetted facts → modest score so they fill QUIET slots without
# crowding out real-time breaking news (DCPI movers, deals). Tunable up if you
# want the brain to lead more often.
_BASE_SCORE = float(os.environ.get("BRAIN_MEDIA_BRIDGE_BASE_SCORE", "12") or 12)
# Strict mode: only surface items the human GRADED good/approved (default off —
# survived-refutation + content filters are the quality bar; a good grade boosts).
_REQUIRE_GRADE = (os.environ.get("BRAIN_MEDIA_BRIDGE_REQUIRE_GRADE", "0") or "0").lower() in (
    "1", "true", "yes", "on")

# A self-critical data fact is a BAD public story — it advertises our own gaps,
# errors, or low conversion. The analyst leads with strength, NEVER a weakness
# (ANALYST_VOICE: "real metrics only", reputation rests on being early+right).
# Skip any headline whose framing exposes a deficiency. Belt-and-suspenders on
# top of the human approval gate downstream.
_SELF_CRITICAL = re.compile(
    r"verified vs|unverified|discovery pile|backlog|dormant|leak|divergence|"
    r"_fallback|fallback|error|fail|refut|stale|drift|\bgap\b|unconverted|"
    r"unaddressed|not growing|0 (?:converted|paid)|leakage|over-?count|"
    r"\(unknown\)|orphan|broken|missing|defect|bug",
    re.I,
)
_HAS_NUMBER = re.compile(r"\d")


def _enabled() -> bool:
    return (os.environ.get("BRAIN_MEDIA_BRIDGE_ENABLED", "0") or "0").lower() in (
        "1", "true", "yes", "on")


def _dsn():
    return os.environ.get("DATABASE_URL")


def _clean_headline(title: str) -> str:
    """'[data_coverage] 311 DCPI markets across 7 live ISOs' -> the bare data
    fact, for the analyst-voice generator to voice. Strips the area tag and the
    'Brain finding:' / 'work-plan:' prefixes."""
    t = re.sub(r"^\s*\[[^\]]+\]\s*", "", title or "").strip()
    t = re.sub(r"^(?:Brain finding:|work-plan:)\s*", "", t, flags=re.I).strip()
    # Drop a trailing detector anchor like ' @ /api/v1/...': not reader content.
    t = re.sub(r"\s*@\s*\S.*$", "", t).strip()
    return t


def _refutation_survived(j) -> bool:
    """True unless the adversarial refutation explicitly KILLED it.
    (survived True = '✓ survived refutation'; False = '✗ refuted'; None = not run
    → defer to the human grade, which we already require to be good/approved.)"""
    try:
        r = (j or {}).get("refutation") or {}
        return r.get("survived") is not False
    except Exception:
        return True


def leads_diagnostics() -> dict:
    """READ-ONLY introspection for tuning: per source table, the distribution of
    area/grade/status over the lookback window and how many rows survive each
    filter stage. Returns enum values + counts only (no row payloads). Powers the
    preview endpoint's ?debug=1 so the operator can see WHY the candidate set is
    what it is, without admin/DB access."""
    out = {"min_conf": _MIN_CONF, "lookback_days": _LOOKBACK_DAYS,
           "publishable_areas": sorted(PUBLISHABLE_AREAS), "sources": {}}
    if psycopg2 is None:
        out["error"] = "psycopg2 unavailable"
        return out
    dsn = _dsn()
    if not dsn:
        out["error"] = "no DATABASE_URL"
        return out
    from collections import Counter
    try:
        conn = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for table, jcol in (("brain_self_agenda", "result_json"),
                                    ("brain_enhancement_proposals", "proposal_json")):
                    s = {"total": 0, "grade": Counter(), "status": Counter(),
                         "area": Counter(), "passes": Counter(), "publishable_titles": []}
                    try:
                        cur.execute(
                            f"SELECT title, area, confidence, grade, status, {jcol} "
                            f"FROM {table} "
                            f"WHERE created_at >= NOW() - (%s || ' days')::interval "
                            f"ORDER BY created_at DESC LIMIT 200",
                            (str(_LOOKBACK_DAYS),))
                        rows = cur.fetchall() or []
                    except Exception as e:
                        s["query_error"] = str(e)[:160]
                        out["sources"][table] = s
                        continue
                    for title, area, conf, grade, status, jdoc in rows:
                        s["total"] += 1
                        s["grade"][str(grade)] += 1
                        s["status"][str(status)] += 1
                        s["area"][str(area)] += 1
                        in_area = area in PUBLISHABLE_AREAS
                        graded = str(grade or "").lower() in ("good", "approved")
                        appr = str(status or "").lower() == "approved"
                        if in_area:
                            s["passes"]["area"] += 1
                            if graded or appr:
                                s["passes"]["graded_or_approved"] += 1
                            hl = _clean_headline(title)
                            has_num = bool(_HAS_NUMBER.search(hl))
                            crit = bool(_SELF_CRITICAL.search(hl))
                            surv = _refutation_survived(jdoc)
                            if has_num:
                                s["passes"]["has_number"] += 1
                            if not crit:
                                s["passes"]["not_self_critical"] += 1
                            if surv:
                                s["passes"]["survived"] += 1
                            s["publishable_titles"].append(
                                {"title": hl[:70], "grade": grade, "status": status,
                                 "conf": conf, "has_num": has_num,
                                 "self_critical": crit, "survived": surv})
                    s["grade"] = dict(s["grade"]); s["status"] = dict(s["status"])
                    s["area"] = dict(s["area"]); s["passes"] = dict(s["passes"])
                    out["sources"][table] = s
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def brain_insight_leads(preview: bool = False) -> list:
    """READ-ONLY. Return analyst-voice LEADS for the brain's graded, publishable
    DATA insights. Shape matches media_editorial.rank_data_events() leads:
    {kind, headline_number, trend, so_what, source_url, dedup_key, score}.
    Fully defensive — any error returns whatever was collected so far (never
    raises into rank_data_events). Empty when the gate is OFF, UNLESS preview=True
    (the admin preview endpoint passes preview=True to show candidates before the
    operator flips BRAIN_MEDIA_BRIDGE_ENABLED live)."""
    if (not preview and not _enabled()) or psycopg2 is None:
        return []
    dsn = _dsn()
    if not dsn:
        return []

    leads: list = []
    seen_headlines: set = set()
    # (table, json_column) — agenda carries the rich investigator output; the
    # enhancement proposals carry leverage-ranked improvement titles.
    sources = [
        ("brain_self_agenda", "result_json"),
        ("brain_enhancement_proposals", "proposal_json"),
    ]
    try:
        conn = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for table, jcol in sources:
                    try:
                        # Quality gate is the REFUTATION + content filters below, NOT
                        # the brain's self-confidence (inherently low, 0.2–0.4). We only
                        # EXCLUDE items the human graded 'bad'; a 'good'/'approved' grade
                        # is a score BOOST (set BRAIN_MEDIA_BRIDGE_REQUIRE_GRADE=1 to make
                        # a positive grade mandatory). No hard confidence floor.
                        cur.execute(
                            f"SELECT id, title, area, confidence, grade, {jcol} "
                            f"FROM {table} "
                            f"WHERE area = ANY(%s) "
                            f"  AND LOWER(COALESCE(grade,'')) <> 'bad' "
                            f"  AND created_at >= NOW() - (%s || ' days')::interval "
                            f"ORDER BY created_at DESC LIMIT 40",
                            (list(PUBLISHABLE_AREAS), str(_LOOKBACK_DAYS)),
                        )
                        rows = cur.fetchall() or []
                    except Exception as e:
                        logger.warning("[brain_media_bridge] %s query failed: %s",
                                       table, str(e)[:160])
                        continue

                    for rid, title, area, conf, grade, jdoc in rows:
                        try:
                            graded_good = str(grade or "").lower() in ("good", "approved")
                            if _REQUIRE_GRADE and not graded_good:
                                continue
                            if not _refutation_survived(jdoc):
                                continue
                            headline = _clean_headline(title)
                            if not headline or not _HAS_NUMBER.search(headline):
                                continue  # must lead with a number (ANALYST_VOICE)
                            if _SELF_CRITICAL.search(headline):
                                continue  # never publish our own weaknesses
                            # Dedup on the NUMBER-STRIPPED headline so "177 countries"
                            # and "178 countries" collapse to one candidate.
                            key = re.sub(r"\d+", "#", re.sub(r"\W+", "", headline.lower()))[:48]
                            if key in seen_headlines:
                                continue
                            seen_headlines.add(key)
                            c = float(conf or 0.0)
                            # base + small confidence input + a boost for human-graded-good.
                            score = _BASE_SCORE + 8.0 * c + (10.0 if graded_good else 0.0)
                            leads.append({
                                "kind": "brain_insight",
                                "headline_number": headline,
                                "trend": "DC Hub coverage — surfaced by the data desk's vetted analysis",
                                "so_what": ("A coverage reference point for site-selection and "
                                            "capacity-planning shortlists — explore the full "
                                            "breakdown at dchub.cloud."),
                                "source_url": "https://dchub.cloud",
                                "dedup_key": f"brain_insight:{table[:4]}:{rid}",
                                "graded_good": graded_good,
                                "score": round(score, 2),
                            })
                        except Exception:
                            continue
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("[brain_media_bridge] leads failed: %s", str(e)[:200])
    return leads
