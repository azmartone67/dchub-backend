"""routes/media_milestones.py — the NUMBERS lane of the editorial desk (2026-08-07).

WHY THIS EXISTS
===============
Operator directive, 2026-08-07: "we just passed 4 million transactions on our
/ai page! that is a press release ... new product rollouts is a story, new
numbers." The desk had a lane for the first half (routes/brain_capability_radar
announces PRODUCT rollouts — new feeds, new tools, the evergreen moat cards)
and no reliable lane for the second. This is the second: a `platform_milestone`
lead that fires when a canonical headline number crosses a round threshold.

It is deliberately NOT a second copy of the radar. The radar owns "we shipped
something"; this owns "a published number crossed a line". Where the two touch
the same metric, they share bookkeeping and de-conflict — see DE-CONFLICTION.

THE FOUR HOUSE RULES THIS LANE OBEYS
  1. CANONICAL SOURCES ONLY. Every number comes from a surface DC Hub already
     publishes — /api/v1/canon/phrases (the floored, citation-safe phrases) and
     /api/public/mcp-count (the counter the /ai page renders). Nothing is
     recomputed here from raw SQL, so a milestone can never disagree with the
     page a reader checks it against. A source we cannot read yields NO lead —
     never a fallback number (never-fabricate).
  2. HONEST LABELS. The /ai counter counts REQUESTS SERVED, all sources. It is
     not users, not visitors, not agents, not customers. `label_is_honest()` is
     an EXECUTED guard on the rendered text, not just a test assertion: a lead
     whose own copy trips it is dropped before it can reach a composer.
  3. FIRES ONCE. Each (metric, threshold) crossing is recorded in
     media_milestone_crossings with ON CONFLICT dedup, so the 4,000,000 crossing
     is announced exactly one time no matter how many slots consult the desk.
  4. THE DESK'S REAL SCALE. Scores live in 22-45, against a bar of 8 and a live
     board where a marquee deal scores ~80 and agent_demand ~66. This is the bug
     PR #2356 fixed for the operator lead (seeded 0.90 against a bar of 8, so it
     was PRODUCED but could never be SELECTED) — a milestone must beat the
     repetitive DCPI one-liners without out-ranking a genuine $19B transaction.

BOOKKEEPING (the radar's precedent, and for the same reason)
  platform_milestone_leads() is READ-ONLY with respect to announcements. A
  crossing is retired ONLY by mark_milestone_announced(), called from
  linkedin_quad_daily after a SUCCESSFUL publish — so a preview never consumes
  the announcement and a failed post leaves the milestone to retry. The one
  write on the read path is the SEED row (below), which is idempotent.

SEEDING — why a first sighting does not announce
  With no record of a metric we cannot prove the current level is new, so the
  first sighting records the current bucket and stays SILENT. The lone
  exception is ai_requests_total, which carries seed_value=3,000,000: the 3M
  crossing was announced by hand on 2026-07-27 (documented in
  brain_capability_radar's requests_served_total entry), so 3M is a KNOWN
  last-announced level, not a guess. That is what lets the 4M crossing fire.

DE-CONFLICTION WITH brain_capability_radar
  Four of the radar's registry rows are numeric milestones over the same
  metrics. Two mechanisms keep one crossing to one announcement:
    · cross-run — a metric's baseline is the HIGHER of this lane's own ledger
      and the radar's data_milestone_snapshots.last_value, and
      mark_milestone_announced advances BOTH (never downward), so whichever
      lane posts first silences the other;
    · same-run — rank_data_events drops a platform_milestone whose radar_key
      already produced a data_milestone lead in the same slate.

Kill: MEDIA_MILESTONE_LANE_DISABLE=1
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# The desk's cap for this lane. A milestone is real news, but a genuine marquee
# deal (~80) or a live agent-demand story (~66) still outranks it.
_MILESTONE_SCORE_CAP = 45.0
# A crossing that lands on a multiple of five buckets (5M requests, 20,000
# facilities) is the bigger story — a bounded bump, still under the cap.
_MAJOR_BUCKET_BONUS = 6.0


# ── House rule 2: the labels this lane may never use ────────────────────
# A request is not a person. The /ai counter has been mis-narrated as "users"
# before; these are banned in the RENDERED copy of every milestone lead, and
# the check runs at render time (not only in the test suite) so a future
# registry edit cannot smuggle one through.
_BANNED_LABELS = re.compile(
    r"\b(users?|visitors?|people|customers?|subscribers?|"
    r"companies|humans?|readers?|accounts?)\b", re.IGNORECASE)


def label_is_honest(text: str) -> bool:
    """False when milestone copy describes a COUNT OF REQUESTS (or facilities,
    or tools) using a word that means PEOPLE. Pure; no I/O."""
    return not _BANNED_LABELS.search(str(text or ""))


def _canon_num(v):
    """Parse a canonical published figure into an int, or None.

    Canon phrases are floored strings ('16,900+', '1,700+', '170+'); tools is a
    bare int. None on anything we cannot read — the caller then emits no lead
    for that metric rather than guessing."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n if n > 0 else None
    m = re.match(r"^\s*([\d,]+)", str(v or ""))
    if not m:
        return None
    try:
        n = int(m.group(1).replace(",", ""))
    except Exception:  # noqa: BLE001
        return None
    return n if n > 0 else None


# ── The registry: one row per announceable canonical number ─────────────
# `headline`/`trend` take (threshold, ctx) where ctx is the fetched metric
# bundle, so every rendered figure traces to a canonical source in ctx.
#
# ★ Each headline is written so a number sits ADJACENT to its unit. The desk's
# leads_with_number() gate drops any lead whose opening number is not next to a
# recognized unit, and it drops them SILENTLY (rank_data_events logs a count,
# not the text). tests/test_media_milestones.py asserts every headline below
# clears that gate, because a milestone that cannot be selected is the same
# defect as one that is never produced.
_MILESTONES = [
    {
        "key": "ai_requests_total",
        "step": 1_000_000,
        "min_threshold": 1_000_000,
        "base_score": 34.0,
        # Shares a baseline with brain_capability_radar's requests_served_total:
        # both are SUM(total_requests) over ai_cumulative — literally the same
        # quantity, so the radar's last-announced value is directly comparable.
        "radar_key": "requests_served_total",
        "shares_baseline": True,
        # the 3M crossing was announced by hand on 2026-07-27 (see that entry) —
        # a documented last-announced level, which is why 4M is announceable.
        "seed_value": 3_000_000,
        "source_url": "https://dchub.cloud/ai",
        "headline": lambda t, c: (
            "DC Hub's live infrastructure data layer has now served "
            f"{t:,}+ total requests"),
        "trend": lambda t, c: (
            "all sources, per the public dchub.cloud/ai counter"
            + (f"; {int(c['ai_requests_external']):,} of them are external "
               "AI-platform requests, broken out on the same page"
               if c.get("ai_requests_external") else "")),
        "so_what": ("request volume is the adoption curve: agents are querying "
                    "live infrastructure data instead of guessing from a static "
                    "training set."),
    },
    {
        "key": "facilities",
        "step": 1_000,
        "min_threshold": 1_000,
        "base_score": 30.0,
        # ★ Same METRIC NAME, different QUANTITY — so the radar's value is used
        # only to keep both lanes off the board on the same day, never as this
        # lane's baseline. Measured live 2026-08-07: facility_coverage's
        # baseline is 24,118 (a raw COUNT(*) of discovered_facilities records)
        # while canon publishes the deduped, citation-safe floor of 16,900. A
        # 43% gap, not a rounding difference. Treating 24,118 as "already
        # announced" would have silently retired every facilities milestone up
        # to 24,000 — including the 17,000 crossing this lane was asked for.
        "radar_key": "facility_coverage",
        "shares_baseline": False,
        "source_url": "https://dchub.cloud/map",
        "headline": lambda t, c: (
            f"DC Hub's live index just crossed {t:,}+ facilities"),
        "trend": lambda t, c: (
            "the machine-readable map of the physical AI buildout"
            + (f", now spanning {int(c['countries']):,} countries"
               if c.get("countries") else "")
            + ", refreshed daily"),
        "so_what": ("every one of them queryable by an agent with power, fiber "
                    "and tenant context attached, not a PDF directory."),
    },
    {
        "key": "deals",
        "step": 100,
        "min_threshold": 100,
        "base_score": 26.0,
        "radar_key": None,
        "source_url": "https://dchub.cloud/transactions",
        "headline": lambda t, c: (
            f"DC Hub's M&A tracker just crossed {t:,}+ deals"),
        "trend": ("the deduplicated transaction record behind data-center "
                  "capital flows, published as a citation-safe floor"),
        "so_what": ("comparable transactions are how a site gets priced — the "
                    "set is queryable rather than locked in a broker deck."),
    },
    {
        "key": "tools",
        "step": 1,
        "min_threshold": 10,
        "base_score": 28.0,
        "radar_key": None,
        "source_url": "https://dchub.cloud/capabilities",
        "headline": lambda t, c: (
            f"The DC Hub MCP surface now exposes {t:,} live tools"),
        "trend": ("new agent-callable primitives ship continuously; the count "
                  "is published at /api/v1/canon/phrases so it can be checked"),
        "so_what": ("each tool is one more infrastructure question an agent can "
                    "answer directly instead of approximating."),
    },
]


def _spec(key: str):
    return next((s for s in _MILESTONES if s["key"] == key), None)


# ── The crossing decision (pure — no I/O, unit-tested directly) ─────────

def milestone_crossing(spec: dict, value, baseline) -> dict | None:
    """Decide what this metric's current `value` warrants.

    baseline = the highest value already announced for this metric (from this
    lane's ledger or the radar's), or None when nothing is on record.

    Returns None (nothing to do), {'action': 'seed', ...} (record the current
    level silently — we cannot prove it is new), or {'action': 'announce', ...}.
    Never raises; an unreadable input is 'nothing to do', never an announce."""
    try:
        step = int(spec.get("step") or 0)
        v = int(value)
    except Exception:  # noqa: BLE001
        return None
    if step <= 0 or v <= 0:
        return None
    bucket = v // step
    threshold = bucket * step
    if threshold < int(spec.get("min_threshold") or step):
        return None
    if baseline is None:
        seed = spec.get("seed_value")
        if seed is None:
            # First sighting with nothing on record: the honest move is to
            # remember this level, not to announce it as if it were new.
            return {"action": "seed", "threshold": threshold, "value": v}
        baseline = seed
    try:
        base_bucket = int(baseline) // step
    except Exception:  # noqa: BLE001
        return None
    # POSITIVE-RESULTS MANDATE: only an INCREASE is a milestone. A metric that
    # slipped back below its last announced bucket says nothing here.
    if base_bucket >= bucket:
        return None
    return {"action": "announce", "threshold": threshold, "value": v}


def render_milestone_lead(spec: dict, threshold: int, ctx: dict) -> dict | None:
    """Render one crossing as a desk lead. None if the copy fails the honest-
    label rule (house rule 2) or the renderer raises — never a partial lead."""
    try:
        def _txt(f):
            return f(threshold, ctx) if callable(f) else str(f or "")

        headline = _txt(spec.get("headline"))
        trend = _txt(spec.get("trend"))
        so_what = _txt(spec.get("so_what"))
        if not headline:
            return None
        if not all(label_is_honest(t) for t in (headline, trend, so_what)):
            logger.warning("[milestones] %s copy used a people-noun for a "
                           "count — dropped", spec.get("key"))
            return None
        step = int(spec.get("step") or 1)
        bucket = int(threshold) // step if step else 0
        score = float(spec.get("base_score") or 22.0)
        if bucket and bucket % 5 == 0:
            score += _MAJOR_BUCKET_BONUS
        return {
            "kind": "platform_milestone",
            "headline_number": headline,
            "trend": trend,
            "so_what": so_what,
            "source_url": spec.get("source_url", "https://dchub.cloud"),
            # entity carries the threshold, so each crossing is its own event
            # to the desk's 14-day (kind, entity) ledger.
            "dedup_key": f"platform_milestone:{spec['key']}@{int(threshold)}",
            "score": round(min(_MILESTONE_SCORE_CAP, score), 2),
            # structured fields ride along for de-confliction + verification
            "metric_key": spec["key"],
            "radar_key": spec.get("radar_key"),
            "threshold": int(threshold),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[milestones] render %s failed: %s",
                       spec.get("key"), str(e)[:140])
        return None


# ── I/O: canonical sources, the ledger, and the lead builder ────────────

def _disabled() -> bool:
    return (os.environ.get("MEDIA_MILESTONE_LANE_DISABLE") or "").strip() == "1"


def _get(path: str, timeout: int = 8) -> dict:
    """GET a canonical surface over loopback. Loopback (not the public host) so
    the read never leaves the box — brain_capability_radar documented a
    backend->Cloudflare->backend self-request timing out and silently starving
    its leads. {} on any failure; the caller then emits no lead."""
    try:
        import requests
        r = requests.get(
            f"http://localhost:8080{path}", timeout=timeout,
            headers={"User-Agent": "dchub-internal-milestones/1.0",
                     "X-Internal-Request": "1"})
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception as e:  # noqa: BLE001
        logger.info("[milestones] %s unavailable: %s", path, str(e)[:120])
        return {}


def fetch_canonical_metrics() -> dict:
    """Today's canonical headline numbers, keyed by registry key. A metric whose
    source is unreadable is simply ABSENT (never zero, never a fallback)."""
    out: dict = {}
    canon = _get("/api/v1/canon/phrases")
    for key, field in (("facilities", "facilities"), ("deals", "deals"),
                       ("tools", "tools")):
        n = _canon_num(canon.get(field))
        if n is not None:
            out[key] = n
    n = _canon_num(canon.get("countries"))
    if n is not None:
        out["countries"] = n

    # The /ai page counter. total_including_infrastructure is the all-sources
    # headline the page leads with (the operator owns that choice); `total` is
    # the external AI-platform subset, named in the trend line so the copy can
    # never read as if all 4M were third-party agents.
    counter = _get("/api/public/mcp-count")
    n = _canon_num(counter.get("total_including_infrastructure"))
    if n is not None:
        out["ai_requests_total"] = n
    n = _canon_num(counter.get("total"))
    if n is not None:
        out["ai_requests_external"] = n
    return out


def _conn():
    try:
        import psycopg2
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not dsn:
            return None
        c = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[milestones] connect failed: %s", str(e)[:120])
        return None


def _ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS media_milestone_crossings (
            metric_key   TEXT   NOT NULL,
            threshold    BIGINT NOT NULL,
            value_at     BIGINT,
            entry_kind   TEXT   NOT NULL DEFAULT 'announced',
            recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (metric_key, threshold)
        )
    """)


def _own_baselines(cur) -> dict:
    """metric_key -> highest threshold this lane has already recorded (whether
    announced or seeded — both prove we have seen that level)."""
    out: dict = {}
    try:
        cur.execute("SELECT metric_key, MAX(threshold) "
                    "FROM media_milestone_crossings GROUP BY metric_key")
        for k, t in (cur.fetchall() or []):
            if k is not None and t is not None:
                out[str(k)] = int(t)
    except Exception as e:  # noqa: BLE001
        logger.warning("[milestones] baseline read failed: %s", str(e)[:120])
    return out


def _radar_baselines(cur) -> dict:
    """radar source_key -> last ANNOUNCED value, from the capability radar's
    ledger. Read-only; this is the cross-lane half of DE-CONFLICTION."""
    out: dict = {}
    try:
        cur.execute("SELECT source_key, last_value "
                    "FROM data_milestone_snapshots")
        for k, v in (cur.fetchall() or []):
            if k is not None and v is not None:
                out[str(k)] = float(v)
    except Exception as e:  # noqa: BLE001
        # table absent on a fresh install — the radar creates it. Not an error.
        logger.info("[milestones] radar ledger unavailable: %s", str(e)[:120])
    return out


def _baseline_for(spec: dict, own: dict, radar: dict):
    """The highest level already on record for this metric, or None.

    ★ The radar's value counts ONLY when `shares_baseline` says the two lanes
    measure the same quantity. Sharing a metric NAME is not sharing a NUMBER:
    facility_coverage is a raw COUNT(*) of records (24,118 live) while this
    lane reads canon's deduped published floor (16,900). Folding that in as a
    baseline would silently retire every crossing up to 24,000. Same-slate
    de-confliction still uses radar_key for every mapped metric — that is a
    question about today's board, not about comparable units."""
    vals = []
    if spec["key"] in own:
        vals.append(float(own[spec["key"]]))
    rk = spec.get("radar_key")
    if rk and spec.get("shares_baseline") and rk in radar:
        vals.append(float(radar[rk]))
    return max(vals) if vals else None


def _record(cur, metric_key: str, threshold: int, value, entry_kind: str) -> None:
    cur.execute("""
        INSERT INTO media_milestone_crossings
            (metric_key, threshold, value_at, entry_kind)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (metric_key, threshold) DO UPDATE
           SET entry_kind = CASE WHEN media_milestone_crossings.entry_kind
                                      = 'announced'
                                 THEN 'announced' ELSE EXCLUDED.entry_kind END,
               value_at    = COALESCE(EXCLUDED.value_at,
                                      media_milestone_crossings.value_at)
    """, (metric_key, int(threshold),
          int(value) if value is not None else None, entry_kind))


def platform_milestone_leads() -> list[dict]:
    """READ-ONLY w.r.t. announcements. Returns the platform_milestone leads for
    every canonical number that crossed a threshold nobody has announced yet.
    Fully defensive: any failure yields fewer leads, never a wrong one."""
    if _disabled():
        return []
    vals = fetch_canonical_metrics()
    if not vals:
        return []
    leads: list[dict] = []
    c = _conn()
    if c is None:
        return leads
    try:
        with c.cursor() as cur:
            _ensure_table(cur)
            own = _own_baselines(cur)
            radar = _radar_baselines(cur)
            for spec in _MILESTONES:
                try:
                    v = vals.get(spec["key"])
                    if v is None:
                        continue
                    res = milestone_crossing(spec, v,
                                             _baseline_for(spec, own, radar))
                    if not res:
                        continue
                    if res["action"] == "seed":
                        # The one write on this path, and it announces nothing.
                        _record(cur, spec["key"], res["threshold"],
                                res["value"], "seed")
                        continue
                    lead = render_milestone_lead(spec, res["threshold"], vals)
                    if lead:
                        leads.append(lead)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[milestones] %s skipped: %s",
                                   spec.get("key"), str(e)[:140])
    except Exception as e:  # noqa: BLE001
        logger.warning("[milestones] lead pass failed: %s", str(e)[:160])
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    return leads


def mark_milestone_announced(dedup_key: str) -> bool:
    """Retire a crossing because its lead was just POSTED. The ONLY writer of an
    'announced' row, called from linkedin_quad_daily on a successful publish —
    so a preview never consumes the milestone and a failed post retries.

    Also advances the capability radar's baseline for a shared metric (never
    downward), so the radar cannot re-announce the same crossing."""
    key = str(dedup_key or "")
    if not key.startswith("platform_milestone:") or "@" not in key:
        return False
    body = key.split(":", 1)[1]
    metric_key, _, thr = body.rpartition("@")
    spec = _spec(metric_key)
    if not spec:
        return False
    try:
        threshold = int(thr)
    except Exception:  # noqa: BLE001
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            _ensure_table(cur)
            _record(cur, metric_key, threshold, threshold, "announced")
            rk = spec.get("radar_key")
            if rk and spec.get("shares_baseline"):
                # Only when the two lanes measure the SAME quantity — writing a
                # deduped floor into a raw-count baseline would corrupt the
                # radar's own bucket math (see _baseline_for). GREATEST so a
                # threshold can never pull an existing baseline backwards.
                cur.execute("""
                    INSERT INTO data_milestone_snapshots
                        (source_key, last_value, announced_at)
                    VALUES (%s, %s, NOW() ON CONFLICT DO NOTHING)
                    ON CONFLICT (source_key) DO UPDATE
                       SET last_value = GREATEST(
                               COALESCE(data_milestone_snapshots.last_value, 0),
                               EXCLUDED.last_value),
                           announced_at = NOW()
                """, (rk, float(threshold)))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[milestones] mark %s failed: %s", key, str(e)[:140])
        return False
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
