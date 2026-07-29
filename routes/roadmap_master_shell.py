"""
routes/roadmap_master_shell.py — Roadmap Master Shell #23 (2026-07-16).

DC Hub's product roadmap/ideas are FEDERATED across four stores with no single
pane: brain enhancement proposals, the brain self-agenda, the strategic-
recommendation backlog, and the agent-onboarding distribution worklist. This
shell aggregates them READ-ONLY into one ranked backlog (house master-shell
pattern — mirrors qa_fixwave_master_shell / pillars_master_shell).

The five lanes:
  1. brain-proposals    — brain_enhancement_proposals: grade='good' (accepted)
                          plus open ungraded (status='proposed', grade IS NULL,
                          confidence >= 0.45). Duplicates collapsed on the
                          fingerprint column when populated, else a normalized
                          title prefix. Grade-good first, then confidence desc.
  2. self-agenda        — brain_self_agenda, same treatment (open = surfaced +
                          ungraded).
  3. strategic-recs     — brain_strategic_recommendations open items (live
                          statuses 2026-07-16: new / pr_drafted / meta — 'meta'
                          and done-ish statuses excluded), grouped by
                          LEFT(LOWER(TRIM(title)),120) to collapse the known
                          dup groups, newest first, count per group.
  4. distribution       — the per-platform NEXT ACTIONS the agent-enablement
                          portal (/admin/agents) shows. Read from the SAME
                          source table that /api/v1/admin/agents/state serves:
                          agent_onboarding_snapshots.worklist_json (latest row,
                          written by agent_onboarding_master_shell tier2_score).
                          Fallback: the static PLATFORMS roster imported
                          in-process from routes.agent_onboarding_master_shell.
                          NO self-requests to the backend (house rule).
  5. shipped (context)  — informational: the five platform updates announced
                          honest on the public /whats-new Platform section
                          (2026-07-13). No DB store records shipped platform
                          updates (the announcements* tables are industry-news
                          ingestion, and /whats-new's Platform section is
                          static frontend), so this lane is a static note —
                          nothing fabricated.

Ranked triage at the top of the dashboard:
  NOW   = grade-good proposals/agenda + strategic groups with a pr_drafted row
          + distribution actions marked timing-sensitive.
  NEXT  = open high-confidence ungraded (confidence >= 0.60).
  LATER = the rest (open ungraded 0.45-0.60, new-only strategic groups,
          non-timing-sensitive distribution actions).
Every item carries its SOURCE store so a human can act on it in the right place.

STRICTLY READ-ONLY diagnostic — pure-DB reads + env; it never mutates state,
never writes a snapshot row, and never self-requests the backend. DB access is
replica-preferred: main.get_read_connection (the pooled Neon read-replica
ladder) falling back to the pooled db_utils.get_db wrapper — no raw psycopg2
connections are opened here.

Endpoints:
  GET/POST /api/v1/admin/roadmap/master-tick   JSON backlog (5 lanes + triage)
  GET      /admin/roadmap                       HTML dashboard (120s refresh)
  GET      /api/v1/admin/roadmap                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as the other master shells.
Kill switch: ROADMAP_SHELL_DISABLED=1.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

roadmap_master_shell_bp = Blueprint("roadmap_master_shell", __name__)

_TICK_TTL = 60.0  # seconds (house range: 30–60s)
_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()

_MIN_CONF = 0.45      # open-ungraded floor (lanes 1-2)
_NEXT_CONF = 0.60     # NEXT bucket threshold
_TRIAGE_CAP = 20      # max items surfaced per triage bucket (counts stay true)
_LANE_LIMIT = 80      # max rows pulled per lane (lanes 1-2, 4)
# Lane 3 pulls deeper: the open strategic backlog is ~280 dup-groups (07-16)
# and pr_drafted groups must ALL reach the triage regardless of age.
_STRATEGIC_LIMIT = 400

# Done-ish / meta statuses excluded from the strategic backlog (mirrors the
# qa_fixwave detector-dedup predicate; live statuses 07-16: new/pr_drafted/meta).
_OPEN_STRATEGIC_PREDICATE = (
    "COALESCE(status,'') NOT IN "
    "('done','shipped','merged','closed','rejected','superseded','wont_fix','archived','meta')"
)

# Source chips (which store to act in).
_SRC_PROPOSALS = "brain_enhancement_proposals"
_SRC_AGENDA = "brain_self_agenda"
_SRC_STRATEGIC = "brain_strategic_recommendations"
_SRC_DISTRIBUTION = "agent_onboarding_snapshots.worklist_json"
# ★2026-07-29: there IS a store now — data/platform_updates.json, the
# PR-approved registry that /whats-new#platform renders. This lane reads it.
_SRC_SHIPPED = "data/platform_updates.json (PR-approved)"

# Fallback only, for when the store cannot be read. Kept deliberately short:
# it is a HAND-TYPED MIRROR and it had already drifted — it lists five of the
# six cards the page actually shipped (get_retirement_headroom, added the same
# day by 2090ec4a0, was never added here). A mirror of a store is a second
# thing to go stale; the store is the source.
_SHIPPED_0713 = [
    {"title": "International grid telemetry — JP (OCCTO), KR (KPX), BR (ONS) "
              "ranked; AU + SG live-partial", "announced": "2026-07-13"},
    {"title": "Provenance Envelope v1 — verified/tracked split on "
              "search_facilities + /api/v1/stats/canonical", "announced": "2026-07-13"},
    {"title": "Agent memory — save_site + get_changes per-saved-site deltas",
     "announced": "2026-07-13"},
    {"title": "error_version:1 contract — /api/v1/errors/registry + "
              "/docs/error-codes", "announced": "2026-07-13"},
    {"title": "cluster_sites_by_latency (tool #73)", "announced": "2026-07-13"},
]


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("ROADMAP_SHELL_DISABLED") or "").strip() == "1"


# ── db (pooled, replica-preferred, READ-ONLY) ─────────────────────────

@contextmanager
def _read_db():
    """Pooled READ-ONLY connection, replica-preferred. Ladder:
      1. main.get_read_connection — the Neon read-replica pool
         (DATABASE_READ_URL / NEON_REPLICA_URL; falls back to primary itself).
      2. db_utils.get_db — the pooled primary wrapper (returns to pool on close).
    Yields None when no DB is reachable — callers must fail-soft (WARN lanes).
    Never opens a raw psycopg2 connection."""
    conn = None
    cleanup = None
    try:
        from main import get_read_connection, return_read_connection
        _c, _src = get_read_connection()
        if _c is not None:
            conn = _c
            cleanup = lambda: return_read_connection(_c, _src)
    except Exception as e:
        logger.debug("[roadmap] read pool unavailable: %s", e)
    if conn is None:
        try:
            from db_utils import get_db
            _w = get_db()
            conn = _w
            cleanup = _w.close  # PGConnectionWrapper.close → putconn
        except Exception as e:
            logger.warning("[roadmap] db connect failed: %s", e)
    try:
        yield conn
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:
                    pass


def _rows(c, sql: str, args: tuple | None = None):
    """Fail-soft fetchall. None on error (NOT [] — a lane must distinguish
    'query broke' from 'no rows'). args=None → literal SQL, no %-substitution
    (the psycopg2 empty-tuple trap)."""
    if c is None:
        return None
    cur = None
    try:
        cur = c.cursor()
        if args is None:
            cur.execute(sql)
        else:
            cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:
        logger.debug("[roadmap] query failed: %s -- %s", sql[:90], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def _cols(c, table: str) -> set:
    """Column names of a public table (schemas drift — e.g. proposals carry
    area/signal/question inside proposal_json on some rows, and the
    fingerprint column is new). Empty set on failure."""
    rows = _rows(c, "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s", (table,))
    return {r[0] for r in rows} if rows else set()


# ── pure helpers (unit-tested — no Flask, no DB) ──────────────────────

def _fp_of(title, fingerprint=None) -> str:
    """Dup-group key: the fingerprint column when populated, else the house
    normalized-title prefix LEFT(LOWER(TRIM(title)),120)."""
    fp = (fingerprint or "").strip() if isinstance(fingerprint, str) else ""
    if fp:
        return fp
    return (title or "").strip().lower()[:120]


def _collapse(items: list) -> list:
    """Collapse duplicate items sharing a dup-group key (item['fp']). Keeps
    the first occurrence (callers pre-sort best-first), sums dup_count and
    collects dup ids. Preserves order."""
    seen: dict = {}
    out = []
    for it in items:
        key = it.get("fp") or _fp_of(it.get("title"), it.get("fingerprint"))
        if key in seen:
            first = seen[key]
            first["dup_count"] = int(first.get("dup_count") or 1) + 1
            ids = first.setdefault("dup_ids", [])
            if it.get("id") is not None and len(ids) < 12:
                ids.append(it["id"])
        else:
            it = dict(it)
            it.setdefault("dup_count", 1)
            seen[key] = it
            out.append(it)
    return out


def _is_timing_sensitive(effort) -> bool:
    return "timing-sensitive" in (effort or "").lower()


def _conf(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _triage(proposals: list, agenda: list, strategic: list, dist: list) -> dict:
    """Pure NOW / NEXT / LATER bucketing over the four actionable lanes.
      NOW   = grade-good proposals/agenda + strategic groups with a pr_drafted
              row + distribution actions marked timing-sensitive.
      NEXT  = open ungraded proposals/agenda with confidence >= _NEXT_CONF.
      LATER = the rest.
    Each bucket is capped at _TRIAGE_CAP items; counts carry the full totals.
    NOW is assembled graded-good → timing-sensitive → PR-drafted so the few
    urgent items are never drowned by the (many) PR-drafted groups under the
    cap. Every item carries its SOURCE store."""
    now, nxt, later = [], [], []

    def _graded(items, source):
        for it in items:
            entry = {
                "title": it.get("title") or "(untitled)",
                "source": source,
                "confidence": it.get("confidence"),
                "dup_count": it.get("dup_count") or 1,
                "ref_id": it.get("id"),
            }
            if (it.get("grade") or "") == "good":
                entry["why"] = "graded good (accepted idea)"
                now.append(entry)
            elif _conf(it.get("confidence")) >= _NEXT_CONF:
                entry["why"] = f"open ungraded, high confidence ({_conf(it.get('confidence')):.2f})"
                nxt.append(entry)
            else:
                entry["why"] = f"open ungraded ({_conf(it.get('confidence')):.2f})"
                later.append(entry)

    _graded(proposals, _SRC_PROPOSALS)
    _graded(agenda, _SRC_AGENDA)

    # distribution before strategic: the handful of timing-sensitive windows
    # must land inside the NOW cap ahead of the many PR-drafted groups.
    for w in dist:
        entry = {
            "title": f"{w.get('platform') or w.get('key')}: {(w.get('action') or '')[:160]}",
            "source": _SRC_DISTRIBUTION,
            "priority": w.get("priority"),
            "owner_gated": w.get("owner_gated"),
        }
        if w.get("timing_sensitive"):
            entry["why"] = "timing-sensitive distribution window"
            now.append(entry)
        else:
            entry["why"] = "distribution backlog (ranked worklist)"
            later.append(entry)

    for g in strategic:
        entry = {
            "title": g.get("title") or "(untitled)",
            "source": _SRC_STRATEGIC,
            "dup_count": g.get("count") or 1,
            "pr_url": g.get("pr_url"),
        }
        if g.get("any_pr_drafted"):
            entry["why"] = "PR drafted — review/merge or close"
            now.append(entry)
        else:
            entry["why"] = "strategic backlog (status=new)"
            later.append(entry)

    return {
        "now": now[:_TRIAGE_CAP],
        "next": nxt[:_TRIAGE_CAP],
        "later": later[:_TRIAGE_CAP],
        "counts": {"now": len(now), "next": len(nxt), "later": len(later)},
    }


def _fmt_conf(v) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _lane(key: str, label: str, source: str, status: str, items: list,
          note: str = "", extra: dict | None = None) -> dict:
    d = {"lane": key, "label": label, "source": source,
         "status": status if status in ("pass", "warn", "fail") else "warn",
         "count": len(items), "items": items, "note": (note or "")[:400]}
    if extra:
        d.update(extra)
    return d


# ── lane 1 · brain-proposals ──────────────────────────────────────────

def _fetch_graded_plus_open(c, table: str, open_status: str,
                            title_fallbacks: str) -> list | None:
    """Shared reader for lanes 1-2 (both stores carry the same shape:
    title/area/confidence/grade/status/created_at + new fingerprint column).
    Returns raw item dicts sorted grade-good first then confidence desc, or
    None when the query failed."""
    cols = _cols(c, table)
    if not cols:
        return None
    fp_sel = "fingerprint" if "fingerprint" in cols else "NULL"
    rows = _rows(c, f"""
        SELECT id, {title_fallbacks} AS title,
               COALESCE(NULLIF(TRIM(area),''),'(no area)') AS area,
               confidence, grade, status, created_at, {fp_sel} AS fingerprint
          FROM {table}
         WHERE grade='good'
            OR (COALESCE(status,'')=%s AND grade IS NULL
                AND COALESCE(confidence,0) >= %s)
         ORDER BY (grade='good') DESC, confidence DESC NULLS LAST, created_at DESC
         LIMIT {_LANE_LIMIT}""", (open_status, _MIN_CONF))
    if rows is None:
        return None
    items = []
    for r in rows:
        rid, title, area, conf, grade, status, created, fp = r
        items.append({
            "id": rid,
            "title": (title or "(untitled)")[:220],
            "area": area,
            "confidence": round(_conf(conf), 3) if conf is not None else None,
            "grade": grade,
            "status": status,
            "created_at": str(created)[:19] if created else None,
            "fingerprint": fp,
            "fp": _fp_of(title, fp),
        })
    return _collapse(items)


def _lane_proposals(c) -> dict:
    if c is None:
        return _lane("brain_proposals", "1 · Brain proposals (graded + open)",
                     _SRC_PROPOSALS, "warn", [], "no db connection")
    cols = _cols(c, _SRC_PROPOSALS)
    # schemas drift — some rows carry title/area/question inside proposal_json
    tf = ("COALESCE(NULLIF(TRIM(title),''), proposal_json->>'title', "
          "proposal_json->>'question', proposal_json->>'signal')"
          if "proposal_json" in cols else "NULLIF(TRIM(title),'')")
    items = _fetch_graded_plus_open(c, _SRC_PROPOSALS, "proposed", tf)
    if items is None:
        return _lane("brain_proposals", "1 · Brain proposals (graded + open)",
                     _SRC_PROPOSALS, "warn", [], "query failed")
    note = (f"grade='good' + (status='proposed' AND grade IS NULL AND "
            f"confidence >= {_MIN_CONF}); dups collapsed on "
            f"{'fingerprint column' if 'fingerprint' in cols else 'title-prefix(120)'}"
            f" (falls back to title-prefix when fingerprint is NULL)")
    return _lane("brain_proposals", "1 · Brain proposals (graded + open)",
                 _SRC_PROPOSALS, "pass", items, note)


# ── lane 2 · self-agenda ──────────────────────────────────────────────

def _lane_agenda(c) -> dict:
    if c is None:
        return _lane("self_agenda", "2 · Self-agenda (graded + surfaced)",
                     _SRC_AGENDA, "warn", [], "no db connection")
    cols = _cols(c, _SRC_AGENDA)
    tf = ("COALESCE(NULLIF(TRIM(title),''), question)"
          if "question" in cols else "NULLIF(TRIM(title),'')")
    items = _fetch_graded_plus_open(c, _SRC_AGENDA, "surfaced", tf)
    if items is None:
        return _lane("self_agenda", "2 · Self-agenda (graded + surfaced)",
                     _SRC_AGENDA, "warn", [], "query failed")
    note = (f"grade='good' + (status='surfaced' AND grade IS NULL AND "
            f"confidence >= {_MIN_CONF}); dups collapsed on "
            f"{'fingerprint column' if 'fingerprint' in cols else 'title-prefix(120)'}")
    return _lane("self_agenda", "2 · Self-agenda (graded + surfaced)",
                 _SRC_AGENDA, "pass", items, note)


# ── lane 3 · strategic-recs ───────────────────────────────────────────

def _lane_strategic(c) -> dict:
    label = "3 · Strategic recommendations (open, dup-grouped)"
    if c is None:
        return _lane("strategic_recs", label, _SRC_STRATEGIC, "warn", [],
                     "no db connection")
    rows = _rows(c, f"""
        SELECT LEFT(LOWER(TRIM(title)),120) AS fp,
               COUNT(*) AS n,
               MAX(created_at) AS newest,
               (ARRAY_AGG(title ORDER BY created_at DESC))[1] AS title,
               (ARRAY_AGG(COALESCE(status,'') ORDER BY created_at DESC))[1] AS latest_status,
               BOOL_OR(status='pr_drafted') AS any_pr_drafted,
               MAX(pr_url) FILTER (WHERE pr_url IS NOT NULL AND pr_url <> '') AS pr_url,
               MAX(confidence) AS confidence
          FROM brain_strategic_recommendations
         WHERE {_OPEN_STRATEGIC_PREDICATE}
         GROUP BY 1
         ORDER BY newest DESC
         LIMIT {_STRATEGIC_LIMIT}""")
    if rows is None:
        return _lane("strategic_recs", label, _SRC_STRATEGIC, "warn", [],
                     "query failed")
    total = _rows(c, f"""
        SELECT COUNT(DISTINCT LEFT(LOWER(TRIM(title)),120)), COUNT(*)
          FROM brain_strategic_recommendations
         WHERE {_OPEN_STRATEGIC_PREDICATE}""")
    total_groups, total_rows = (total[0] if total else (None, None))
    items = [{
        "fp": r[0],
        "count": int(r[1] or 0),
        "newest": str(r[2])[:19] if r[2] else None,
        "title": (r[3] or "(untitled)")[:220],
        "latest_status": r[4],
        "any_pr_drafted": bool(r[5]),
        "pr_url": r[6],
        "confidence": round(_conf(r[7]), 3) if r[7] is not None else None,
    } for r in rows]
    note = ("open = NOT done/shipped/merged/closed/rejected/superseded/wont_fix/"
            "archived/meta (live statuses 07-16: new · pr_drafted · meta); "
            "grouped by LEFT(LOWER(TRIM(title)),120), newest first")
    return _lane("strategic_recs", label, _SRC_STRATEGIC, "pass", items, note,
                 {"total_groups": total_groups, "total_rows": total_rows})


# ── lane 4 · distribution-next-actions ────────────────────────────────

def _lane_distribution(c) -> dict:
    label = "4 · Distribution next actions (per platform)"
    # PRIMARY: latest snapshot from the SAME table /api/v1/admin/agents/state
    # (the agent-enablement portal) serves — written by
    # agent_onboarding_master_shell tier2_score. No self-request.
    rows = _rows(c, "SELECT worklist_json, created_at "
                    "FROM agent_onboarding_snapshots ORDER BY id DESC LIMIT 1")
    if rows:
        raw, ts = rows[0]
        try:
            work = raw if isinstance(raw, list) else json.loads(raw or "[]")
        except Exception:
            work = []
        if isinstance(work, list) and work:
            items = [{
                "key": w.get("key"),
                "platform": w.get("platform") or w.get("key"),
                "score": w.get("score"),
                "weakest_dim": w.get("weakest_dim"),
                "action": (w.get("action") or "")[:260],
                "owner_gated": bool(w.get("owner_gated")),
                "effort": w.get("effort"),
                "priority": w.get("priority"),
                "timing_sensitive": _is_timing_sensitive(w.get("effort")),
            } for w in work[:_LANE_LIMIT] if isinstance(w, dict)]
            return _lane("distribution", label, _SRC_DISTRIBUTION, "pass", items,
                         f"latest agent_onboarding_snapshots worklist (as of "
                         f"{str(ts)[:19]}), ranked by reach_weight × (100 − score); "
                         f"same source /admin/agents serves — no self-request",
                         {"as_of": str(ts)[:19]})
    # FALLBACK: import the static roster in-process (module-level constant,
    # stdlib+flask imports only — safe, still no self-request).
    try:
        from routes.agent_onboarding_master_shell import PLATFORMS
        items = [{
            "key": p.get("key"),
            "platform": p.get("name") or p.get("key"),
            "score": None,
            "weakest_dim": None,
            "action": (p.get("next_action") or "")[:260],
            "owner_gated": bool(p.get("owner_gated")),
            "effort": p.get("effort"),
            "priority": p.get("reach_weight"),
            "timing_sensitive": _is_timing_sensitive(p.get("effort")),
        } for p in PLATFORMS]
        return _lane("distribution", label,
                     "routes.agent_onboarding_master_shell.PLATFORMS (static roster)",
                     "warn", items,
                     "no agent_onboarding_snapshots row readable — fell back to "
                     "the static curated roster (unranked; run the onboarding "
                     "master-tick to refresh). See /admin/agents.")
    except Exception as e:
        return _lane("distribution", label, _SRC_DISTRIBUTION, "warn", [],
                     f"snapshot unreadable and roster import failed "
                     f"({type(e).__name__}) — see /admin/agents")


# ── lane 5 · shipped (context) ────────────────────────────────────────

def _lane_shipped(c) -> dict:
    # Reads the real store (data/platform_updates.json) — the same registry
    # /whats-new#platform renders, where a card is visible only because the
    # owner merged the PR that set status="published". Pure file read, no DB.
    # Falls back to the hand-typed mirror if the store is unreadable, and SAYS
    # SO in the note: a silent fallback would make a drifted mirror look like
    # live truth, which is how the mirror drifted in the first place.
    try:
        from routes.platform_updates import published_updates
        block = published_updates()
        cards = block.get("cards") or []
        if block.get("ok") and cards:
            items = [{"title": ct.get("title"), "announced": ct.get("announced"),
                      "source": _SRC_SHIPPED} for ct in cards]
            return _lane("shipped",
                         "5 · Shipped (context — approved platform updates)",
                         _SRC_SHIPPED, "pass", items,
                         "approved platform updates rendered on /whats-new#platform; "
                         "approval is a merged PR against data/platform_updates.json "
                         "— %d withheld (unapproved or rejected)"
                         % int(block.get("withheld_count") or 0))
    except Exception as e:
        logger.warning("roadmap lane5: store unreadable: %s", str(e)[:120])
    items = [dict(s, source="static fallback mirror") for s in _SHIPPED_0713]
    return _lane("shipped", "5 · Shipped (context — STATIC FALLBACK)",
                 "static fallback mirror", "warn", items,
                 "data/platform_updates.json unreadable — showing the hand-typed "
                 "fallback mirror, which is known to have drifted (five of the six "
                 "cards actually shipped). Treat as stale until the store reads.")


# ── tick ──────────────────────────────────────────────────────────────

_LANES = [
    ("brain_proposals", _lane_proposals),
    ("self_agenda", _lane_agenda),
    ("strategic_recs", _lane_strategic),
    ("distribution", _lane_distribution),
    ("shipped", _lane_shipped),
]


def _run_tick() -> dict:
    lanes = []
    with _read_db() as c:
        for key, fn in _LANES:
            try:
                lanes.append(fn(c))
            except Exception as e:  # a lane must never sink the tick
                lanes.append(_lane(key, key, "?", "warn", [],
                                   f"lane crashed: {type(e).__name__}: {str(e)[:160]}"))
    by_key = {l["lane"]: l for l in lanes}
    triage = _triage(by_key.get("brain_proposals", {}).get("items") or [],
                     by_key.get("self_agenda", {}).get("items") or [],
                     by_key.get("strategic_recs", {}).get("items") or [],
                     by_key.get("distribution", {}).get("items") or [])
    return {
        "ok": True,
        "shell": "roadmap-master-shell #23",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "triage": triage,
        "lanes_pass": sum(1 for l in lanes if l["status"] == "pass"),
        "lanes_warn": sum(1 for l in lanes if l["status"] == "warn"),
        "lanes_fail": sum(1 for l in lanes if l["status"] == "fail"),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": ("read-only backlog aggregator — pure-DB (replica-preferred) + "
                 "in-process imports; no self-requests, no writes. "
                 "See routes/roadmap_master_shell.py"),
    }


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes ────────────────────────────────────────────────────────────

@roadmap_master_shell_bp.route("/api/v1/admin/roadmap/master-tick", methods=["GET", "POST"])
def roadmap_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


_SRC_CHIP_COLORS = {
    _SRC_PROPOSALS: "#a78bfa",
    _SRC_AGENDA: "#f472b6",
    _SRC_STRATEGIC: "#34d399",
    _SRC_DISTRIBUTION: "#38bdf8",
    _SRC_SHIPPED: "#64748b",
}

_SRC_CHIP_LABELS = {
    _SRC_PROPOSALS: "proposals",
    _SRC_AGENDA: "agenda",
    _SRC_STRATEGIC: "strategic",
    _SRC_DISTRIBUTION: "distribution",
    _SRC_SHIPPED: "shipped",
}


def _src_chip(source: str) -> str:
    color = _SRC_CHIP_COLORS.get(source, "#64748b")
    label = _SRC_CHIP_LABELS.get(source, source[:14])
    return (f"<span style='background:{color}22;color:{color};border:1px solid {color};"
            f"border-radius:6px;padding:0 6px;font-size:11px;white-space:nowrap'>"
            f"{_esc(label)}</span>")


@roadmap_master_shell_bp.route("/admin/roadmap", methods=["GET"])
@roadmap_master_shell_bp.route("/api/v1/admin/roadmap", methods=["GET"])
def roadmap_dashboard():
    if _disabled():
        return Response("roadmap shell disabled", status=503)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()
    tri = p["triage"]

    def _bucket_card(name: str, color: str, items: list, total: int, sub: str) -> str:
        rows = []
        for it in items:
            meta_bits = []
            if it.get("confidence") is not None:
                meta_bits.append(f"conf {_fmt_conf(it['confidence'])}")
            if (it.get("dup_count") or 1) > 1:
                meta_bits.append(f"×{it['dup_count']}")
            if it.get("priority") is not None:
                meta_bits.append(f"prio {it['priority']}")
            if it.get("owner_gated"):
                meta_bits.append("owner-gated")
            if it.get("pr_url"):
                meta_bits.append(f"<a style='color:#38bdf8' href='{_esc(str(it['pr_url']))}'>PR</a>")
            meta = (" · " + " · ".join(meta_bits)) if meta_bits else ""
            rows.append(
                f"<tr><td style='padding:4px 8px;vertical-align:top'>{_src_chip(it.get('source') or '')}</td>"
                f"<td style='padding:4px 8px;vertical-align:top'>{_esc(str(it.get('title') or ''))}"
                f"<div style='color:#64748b;font-size:11px'>{_esc(str(it.get('why') or ''))}{meta}</div>"
                f"</td></tr>")
        more = (f"<div style='color:#64748b;font-size:12px;margin-top:4px'>"
                f"+{total - len(items)} more (JSON has totals)</div>"
                if total > len(items) else "")
        body = (f"<table style='font-size:13px;border-collapse:collapse'>{''.join(rows)}</table>{more}"
                if rows else "<div style='color:#64748b;font-size:13px'>nothing here</div>")
        return (f"<div style='background:#0f172a;border:1px solid {color};border-radius:12px;"
                f"padding:16px;margin:12px 0'>"
                f"<div style='font-weight:700;font-size:15px;color:{color}'>{name} "
                f"<span style='color:#64748b;font-weight:400;font-size:12px'>({total}) · {sub}</span></div>"
                f"{body}</div>")

    triage_html = (
        _bucket_card("NOW", "#22c55e", tri["now"], tri["counts"]["now"],
                     "grade-good + PR-drafted + timing-sensitive")
        + _bucket_card("NEXT", "#eab308", tri["next"], tri["counts"]["next"],
                       f"open ungraded, confidence ≥ {_NEXT_CONF}")
        + _bucket_card("LATER", "#64748b", tri["later"], tri["counts"]["later"],
                       "the rest of the open backlog"))

    lane_cards = []
    for lane in p["lanes"]:
        rows = []
        for it in (lane["items"] or [])[:30]:
            if lane["lane"] in ("brain_proposals", "self_agenda"):
                bits = [f"conf {_fmt_conf(it.get('confidence'))}",
                        f"grade {_esc(str(it.get('grade') or '—'))}",
                        _esc(str(it.get('created_at') or ''))]
                if (it.get("dup_count") or 1) > 1:
                    bits.append(f"×{it['dup_count']} dups")
                detail = f"[{_esc(str(it.get('area') or ''))}] " + " · ".join(bits)
            elif lane["lane"] == "strategic_recs":
                bits = [f"×{it.get('count')}", _esc(str(it.get('latest_status') or '')),
                        _esc(str(it.get('newest') or ''))]
                if it.get("pr_url"):
                    bits.append(f"<a style='color:#38bdf8' href='{_esc(str(it['pr_url']))}'>PR</a>")
                detail = " · ".join(bits)
            elif lane["lane"] == "distribution":
                bits = [f"score {it.get('score') if it.get('score') is not None else '—'}",
                        f"prio {it.get('priority') if it.get('priority') is not None else '—'}",
                        _esc(str(it.get('effort') or ''))]
                if it.get("owner_gated"):
                    bits.append("owner-gated")
                if it.get("timing_sensitive"):
                    bits.append("<b style='color:#eab308'>timing-sensitive</b>")
                detail = " · ".join(bits) + f"<div style='color:#94a3b8'>{_esc(str(it.get('action') or ''))}</div>"
            else:  # shipped
                detail = f"announced {_esc(str(it.get('announced') or ''))}"
            title = it.get("title") or it.get("platform") or "(untitled)"
            rows.append(
                f"<tr><td style='padding:4px 8px;vertical-align:top'>{_esc(str(title))}</td>"
                f"<td style='padding:4px 8px;vertical-align:top;color:#94a3b8;font-size:12px'>{detail}</td></tr>")
        shown = min(len(lane["items"] or []), 30)
        more = (f"<div style='color:#64748b;font-size:12px;margin-top:4px'>showing {shown} of "
                f"{lane.get('total_groups') or lane['count']}</div>"
                if (lane.get("total_groups") or lane["count"]) > shown else "")
        border = {"pass": "#334155", "fail": "#ef4444"}.get(lane["status"], "#eab308")
        lane_cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_esc(lane['label'])} "
            f"<span style='color:#64748b;font-weight:400;font-size:12px'>"
            f"({lane['count']} items) · source: <code>{_esc(lane['source'])}</code></span></div>"
            f"<div style='color:#64748b;font-size:11px;margin-top:2px'>{_esc(lane['note'])}</div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>"
            f"{''.join(rows)}</table>{more}</div>")

    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='120'>"
        "<title>Roadmap Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:1020px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Roadmap Master Shell "
        f"<span style='color:#22c55e'>NOW {tri['counts']['now']}</span> · "
        f"<span style='color:#eab308'>NEXT {tri['counts']['next']}</span> · "
        f"<span style='color:#64748b'>LATER {tri['counts']['later']}</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>shell #23 · one ranked backlog over the "
        f"four federated idea stores + shipped context · read-only (pure-DB replica-preferred + "
        f"in-process imports; no self-requests, no writes) · {int(_TICK_TTL)}s tick cache · "
        f"auto-refresh 120s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/roadmap/master-tick (?fresh=1 to force)</div>"
        + triage_html
        + "<h3 style='margin:20px 0 0;color:#94a3b8'>Per-lane detail</h3>"
        + "".join(lane_cards) + "</body>")
    return Response(html, mimetype="text/html")
