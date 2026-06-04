"""
Brain — Data-Growth Win Radar (r71, 2026-06-04)
================================================
The "autonomous media" hook: the platform notices when its OWN data moat
materially expands (e.g. power plants 6,961 -> 22,237, or the live
interconnection-queue total crossing a new level) and QUEUES A DRAFT press
release for human review — instead of silently using the bigger numbers.

DRAFT-ONLY BY CONSTRUCTION. Every row is inserted into press_releases_queue with
status='draft', published_at=NULL, and never touches the magnitude-based
auto-publish path in press_queue.py. A human reviews + publishes via the normal
press review surface. Nothing goes live without sign-off.

How it works:
  - Baseline of each metric is persisted in brain_state (key='data_growth_baseline').
  - Each scan reads current values, compares to the last-seen baseline, and fires
    only on a STEP-CHANGE (>= pct AND >= absolute floor, to ignore daily drift),
    then updates the baseline. So it catches milestones between runs, not noise.
  - On a win it (a) queues ONE consolidated draft, (b) logs a brain_notification
    (kind='data_growth_win', severity='win'). It will not pile up drafts: if an
    unreviewed data_growth draft already exists, it logs but does not duplicate.
  - First run with no baseline can be SEEDED (pass {"seed": {...}}) so it detects
    a jump that already happened (the values were real prior values).
"""
import os
import json
import time
import datetime

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, jsonify

brain_data_growth_bp = Blueprint("brain_data_growth", __name__)

_BASELINE_KEY = "data_growth_baseline"

# metric_key -> (human label, SQL returning one numeric, unit, pct_threshold, abs_floor)
# Each query is defensive: if the table/column differs the metric is skipped, not fatal.
_METRICS = [
    ("power_plants", "U.S. power plants mapped",
     "SELECT COUNT(*) FROM discovered_power_plants",
     "plants", 0.15, 1000),
    ("iso_queue_gw", "interconnection-queue capacity tracked",
     "SELECT COALESCE(SUM(g),0) FROM ("
     "  SELECT DISTINCT ON (iso) queued_load_total_gw AS g"
     "  FROM iso_queue_snapshots WHERE queued_load_total_gw IS NOT NULL"
     "  ORDER BY iso, as_of DESC) t",
     "GW", 0.15, 50),
    ("deals", "tracked data-center transactions",
     "SELECT COUNT(*) FROM deals",
     "deals", 0.20, 200),
    ("dcpi_markets", "DCPI markets scored",
     "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores",
     "markets", 0.15, 25),
]


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    return psycopg2.connect(db, sslmode="require", connect_timeout=10)


def _read_metric(cur, sql):
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        # Defensive: an uncertain table/column must not break the whole scan.
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None


def _queue_draft(cur, wins, today):
    """Insert ONE consolidated DRAFT into press_releases_queue. Draft-only."""
    head = wins[0]
    slug = f"data-growth-{head['metric']}-{today}"
    cur.execute("SELECT 1 FROM press_releases_queue WHERE slug=%s", (slug,))
    if cur.fetchone():
        slug = f"{slug}-{int(time.time()) % 100000}"

    # Factual TEMPLATE — always-available fallback.
    bullets = "\n".join(
        f"• {w['label']}: {int(w['prev']):,} → {int(w['now']):,} {w['unit']} (+{w['pct']}%)"
        for w in wins
    )
    sub = "; ".join(f"{w['label']} +{w['pct']}%" for w in wins)[:300]
    title = (f"DC Hub Expands Its Data Moat: "
             f"{head['label'].title()} {int(head['prev']):,} → {int(head['now']):,}")
    body = (
        "DC Hub's coverage expanded materially since the last measurement:\n\n"
        f"{bullets}\n\n"
        "Every figure above is queryable live via the DC Hub API and MCP server, "
        "and updates automatically as new ISO interconnection-queue, EIA-860, and "
        "OpenStreetMap infrastructure data lands.\n\n"
        f"Cite as: DC Hub, {today}, https://dchub.cloud."
    )
    # r71b level-up: prefer LLM-quality copy via the marketing engine; fall back to
    # the template on ANY failure (no key / API error / validation fail). The draft
    # stays DRAFT-only either way — generation never publishes.
    try:
        from routes.marketing_engine import _call_claude_marketing, _validate_release
        facts = "; ".join(
            f"{w['label']}: {int(w['prev']):,} -> {int(w['now']):,} {w['unit']} (+{w['pct']}%)"
            for w in wins)
        prompt = (
            "Write a concise, factual press release announcing a data-coverage "
            "expansion for DC Hub (an agent-native data-center + energy intelligence "
            f"platform). Use ONLY these verified figures — do NOT invent any numbers "
            f"or quotes: {facts}. As of {today}. Every figure is queryable live via "
            "the DC Hub API + MCP. No hype. Return ONLY a JSON object with keys: "
            f"title, subheadline, body, slug, meta_description. body 250-1200 chars, "
            f"ending with the line 'Cite as: DC Hub, {today}, https://dchub.cloud'. "
            "slug lowercase-hyphen, starting 'data-growth-'."
        )
        rel, _err = _call_claude_marketing(prompt)
        if rel and _validate_release(rel)[0]:
            title = rel.get("title") or title
            body = rel.get("body") or body
            sub = (rel.get("subheadline") or sub)[:300]
    except Exception:
        pass
    body = body.rstrip() + (
        "\n\n[DRAFT — auto-queued by the data-growth radar; review before "
        "publishing. Nothing publishes automatically.]"
    )
    cur.execute(
        """INSERT INTO press_releases_queue
             (slug, title, subheadline, body, category, trigger_type,
              trigger_data, status, published_at)
           VALUES (%s, %s, %s, %s, 'DataMoat', 'data_growth', %s, 'draft', NULL)
           ON CONFLICT (slug) DO NOTHING
           RETURNING slug""",
        (slug, title, sub, body, json.dumps({"wins": wins})),
    )
    r = cur.fetchone()
    return r[0] if r else slug


def detect_data_growth_wins(dry_run=False, seed=None):
    """Compare current metrics vs baseline; on a step-change queue a DRAFT + log.

    dry_run: read + compare, but do not queue a draft or update the baseline.
    seed:    dict of prior values; ONLY applied when no baseline exists yet
             (lets the first scan detect a jump that already happened).
    """
    out = {"ok": True, "checked": [], "wins": [], "draft_slug": None,
           "dry_run": dry_run, "seeded": False}
    conn = _conn()
    if conn is None:
        return {"ok": False, "error": "no_database_url"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS brain_state (
                       id BIGSERIAL PRIMARY KEY,
                       state_key TEXT NOT NULL UNIQUE,
                       state_value JSONB NOT NULL,
                       updated_at TIMESTAMPTZ DEFAULT NOW())"""
            )
            conn.commit()

            cur.execute("SELECT state_value FROM brain_state WHERE state_key=%s",
                        (_BASELINE_KEY,))
            row = cur.fetchone()
            baseline = (row[0] if row else None) or {}
            if isinstance(baseline, str):
                baseline = json.loads(baseline or "{}")
            if not baseline and isinstance(seed, dict) and seed:
                baseline = {k: float(v) for k, v in seed.items()
                            if isinstance(v, (int, float))}
                out["seeded"] = True

            current, wins = {}, []
            for key, label, sql, unit, pct_t, abs_t in _METRICS:
                v = _read_metric(cur, sql)
                if v is None:
                    continue
                current[key] = v
                out["checked"].append({"metric": key, "value": v})
                prev = baseline.get(key)
                if prev is not None and prev > 0:
                    delta = v - prev
                    if delta >= abs_t and (delta / prev) >= pct_t:
                        wins.append({"metric": key, "label": label, "unit": unit,
                                     "prev": prev, "now": v, "delta": delta,
                                     "pct": round(delta / prev * 100, 1)})
            out["wins"] = wins

            today = datetime.date.today().isoformat()
            if wins and not dry_run:
                cur.execute(
                    """SELECT slug FROM press_releases_queue
                       WHERE trigger_type='data_growth' AND status='draft'
                       ORDER BY created_at DESC LIMIT 1"""
                )
                existing = cur.fetchone()
                if existing:
                    out["draft_slug"] = existing[0]
                    out["note"] = "unreviewed data_growth draft already open — not duplicating"
                else:
                    out["draft_slug"] = _queue_draft(cur, wins, today)

            # Always advance the baseline to current (so we detect the NEXT jump,
            # not gradual drift). Skip on dry_run.
            if not dry_run and current:
                merged = dict(baseline)
                merged.update(current)
                merged["_updated"] = datetime.datetime.utcnow().isoformat()
                cur.execute(
                    """INSERT INTO brain_state (state_key, state_value, updated_at)
                       VALUES (%s, %s, NOW())
                       ON CONFLICT (state_key)
                       DO UPDATE SET state_value=EXCLUDED.state_value, updated_at=NOW()""",
                    (_BASELINE_KEY, json.dumps(merged)),
                )
            conn.commit()

        if wins:
            try:
                from routes.brain_evolution import log_notification
                summ = "; ".join(
                    f"{w['label']} {int(w['prev']):,}→{int(w['now']):,} (+{w['pct']}%)"
                    for w in wins)
                log_notification(
                    "data_growth_win",
                    f"Data-growth milestone detected — press draft queued for review: {summ}",
                    detail={"wins": wins, "draft_slug": out.get("draft_slug"),
                            "dry_run": dry_run, "seeded": out.get("seeded")},
                    severity="win",
                )
            except Exception:
                pass
        return out
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _authorized():
    sent = (request.headers.get("X-Admin-Key")
            or request.headers.get("X-Internal-Key")
            or request.headers.get("X-Internal-Cron")
            or request.args.get("admin_key") or "").strip()
    allowed = {"dchub-internal-sync-2026"}
    for n in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY"):
        v = os.environ.get(n)
        if v:
            allowed.add(v)
    return bool(sent) and sent in allowed


@brain_data_growth_bp.route("/api/v1/brain/data-growth-scan", methods=["GET", "POST"])
def data_growth_scan():
    if not _authorized():
        return jsonify(error="auth_required",
                       hint="send X-Admin-Key / X-Internal-Key"), 401
    dry = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    body = request.get_json(silent=True) or {}
    seed = body.get("seed") if isinstance(body, dict) else None
    return jsonify(detect_data_growth_wins(dry_run=dry, seed=seed))
