"""routes/story_debt_master_shell.py — master shell: STORY DEBT (2026-08-17).

THE FAILURE THIS WATCHES. /whats-new#platform is fed by data/platform_updates
.json, whose designed author — capability_announcements.stage_announcement_pr()
— has ZERO callers: no cron, no workflow, no brain route. So cards appear only
when whoever ships a feature hand-writes one, and for 19 days nobody did: the
newest card sat at 2026-07-29 while the nav's own "Just Shipped" section
badged the Grid Transition Radar, three Land & Power views and the facility
database as NEW. The platform was telling agents its story pipeline was live
while the pipeline had no writer.

WHAT IT MEASURES (read-only; the ACTUATOR is tools/story_debt_author.py on a
daily GH cron, which stages draft-status cards as a GitHub draft PR — merge
stays the approval, drafts stay invisible either way):

  A  store_gate       — the store through the REAL loader (published_updates):
                        cards served, withheld entries split into staged
                        drafts (expected) vs gate refusals (RED — someone
                        meant to publish and the figure fence or a malformed
                        field is withholding it in silence).
  B  story_age        — days since the newest PUBLISHED card's `announced`.
                        GAUGE by rule: the platform declares no announcement
                        cadence, so there is no honest threshold to convict
                        against (no invented targets).
  C  ship_vs_story    — the core lane. Fetches the frontend nav (public edge
                        asset js/dchub-nav.js), reads every `badge: 'NEW'`
                        entry — the platform's OWN list of fresh products —
                        and REDs the ones no published card covers, at path
                        grain (utils/story_debt.py, shared with the author so
                        shell and author cannot disagree). A parse of zero
                        entries files BLIND, never PASS.
  D  author_heartbeat — last ingest_runs beat from feed 'story-debt-author'
                        (DB read, no self-request). RED only past 60h or on an
                        error beat — 2x the 30h watch.py cadence, the deadman
                        board's own overdue rule. 'never ran' is a GAUGE: this
                        lane ships in the same PR as the author, so a day-one
                        absence is expected, not a defect.

Relation to other shells: roadmap #23 lane 5 reads this same store as SHIPPED
CONTEXT for the backlog; nothing measures whether shipping produced a story.
This shell owns that gap. The QA super-user probes the RENDERED page from the
caller's seat; this shell reads the store and the nav sources directly.

Endpoints (X-Admin-Key / ?admin_key= vs DCHUB_ADMIN_KEY, DCHUB_INTERNAL_KEY
fallback — same gate as the sibling shells):
  GET/POST /api/v1/admin/story-debt/master-tick   JSON state (lanes + summary)
  GET      /api/v1/admin/story-debt               alias of the JSON state
  GET      /admin/story-debt                      HTML board (server-rendered)
Kill switch: STORY_DEBT_SHELL_DISABLED=1.
★ Reads through the CF edge must cache-bust (&_=<ts>) — admin GETs under
/api/v1/* are edge-cached unless a bypass rule exists (see the QA super-user
board's 42-minute-stale lesson).
"""
from __future__ import annotations

import html
import logging
import os
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, request

from utils.story_debt import (
    compute_debt,
    parse_nav_new_items,
    ship_vs_story_verdict,
)

logger = logging.getLogger("story_debt")
story_debt_bp = Blueprint("story_debt", __name__)

SHELL_NAME = "story-debt"
NAV_URL = "https://dchub.cloud/js/dchub-nav.js"
PROBE_UA = "dchub-story-debt-shell/1.0"
AUTHOR_FEED = "story-debt-author"
# The deadman board's own rule: overdue at 2x cadence. watch.py registers the
# author workflow at 30h, so the shell convicts only past 60h — same math,
# stated once here so the two instruments cannot disagree by a constant.
AUTHOR_CADENCE_H = 30
AUTHOR_OVERDUE_H = AUTHOR_CADENCE_H * 2


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


def _lane(name, verdict, note, evidence=None):
    return {"lane": name, "verdict": verdict, "note": note,
            "evidence": evidence or {}}


def _lane_store_gate():
    try:
        from routes.platform_updates import MAX_CARDS, published_updates
        block = published_updates(force=True)
    except Exception as e:
        return _lane("store_gate", "BLIND",
                     "loader unreadable (%s) — store unobserved" % type(e).__name__)
    drafts, refused = [], []
    for w in block.get("withheld") or []:
        reason = str((w or {}).get("reason") or "")
        (drafts if reason.startswith("not approved") else refused).append(w)
    ev = {"cards_served": block.get("count"),
          "headroom_to_cap": max(0, MAX_CARDS - int(block.get("count") or 0)),
          "truncated": block.get("truncated"),
          "staged_drafts": drafts, "gate_refusals": refused,
          "loader_ok": block.get("ok"), "loader_reason": block.get("reason")}
    if not block.get("ok"):
        return _lane("store_gate", "RED",
                     "loader reports the store unreadable: %s" % block.get("reason"), ev)
    if refused:
        return _lane("store_gate", "RED",
                     "%d entr%s meant for publish are withheld by the gate "
                     "(figure fence / malformed field) — invisible on the page, "
                     "silent in CI" % (len(refused), "y" if len(refused) == 1 else "ies"),
                     ev)
    note = "%d card(s) served" % (block.get("count") or 0)
    if drafts:
        note += " · %d staged draft(s) awaiting copy + publish" % len(drafts)
    return _lane("store_gate", "PASS", note, ev)


def _lane_story_age():
    try:
        from routes.platform_updates import STORE_PATH, _read_store
        ups, err = _read_store(STORE_PATH)
    except Exception as e:
        return _lane("story_age", "BLIND", "store unreadable (%s)" % type(e).__name__)
    if err:
        return _lane("story_age", "BLIND", "store unreadable: %s" % err)
    newest = None
    for e in ups:
        if not isinstance(e, dict):
            continue
        if str(e.get("status") or "").strip().lower() != "published":
            continue
        a = str(e.get("announced") or "")
        if len(a) == 10:
            newest = a if (newest is None or a > newest) else newest
    if newest is None:
        return _lane("story_age", "GAUGE", "no published card carries an announced date",
                     {"newest_announced": None})
    try:
        age = (date.today() - date.fromisoformat(newest)).days
    except Exception:
        return _lane("story_age", "GAUGE", "announced date unparsable: %r" % newest,
                     {"newest_announced": newest})
    return _lane("story_age", "GAUGE",
                 "newest published card announced %s (%dd ago) — gauge, not a "
                 "verdict: no announcement cadence is declared anywhere, so "
                 "there is no honest threshold (lane C carries the conviction)"
                 % (newest, age),
                 {"newest_announced": newest, "age_days": age})


def _lane_ship_vs_story():
    try:
        import requests
        r = requests.get(NAV_URL, headers={"User-Agent": PROBE_UA}, timeout=8)
        if r.status_code != 200:
            return _lane("ship_vs_story", "BLIND",
                         "nav fetch HTTP %s — surface unobserved" % r.status_code,
                         {"nav_url": NAV_URL})
        # ★ decode bytes as utf-8 ourselves: requests falls back to latin-1 on
        # charsetless text/* and mangles multi-byte labels (the SSE lesson).
        js_text = r.content.decode("utf-8", "replace")
    except Exception as e:
        return _lane("ship_vs_story", "BLIND",
                     "nav fetch failed (%s) — surface unobserved" % type(e).__name__,
                     {"nav_url": NAV_URL})
    items = parse_nav_new_items(js_text)
    try:
        from routes.platform_updates import STORE_PATH, _read_store
        ups, err = _read_store(STORE_PATH)
        if err:
            return _lane("ship_vs_story", "BLIND", "store unreadable: %s" % err)
    except Exception as e:
        return _lane("ship_vs_story", "BLIND", "store unreadable (%s)" % type(e).__name__)
    debt = compute_debt(items, ups)
    verdict, note = ship_vs_story_verdict(len(items), debt)
    return _lane("ship_vs_story", verdict, note,
                 {"nav_new_items": len(items),
                  "debt": debt,
                  "basis": "badge:'NEW' entries in %s vs published card link "
                           "paths in data/platform_updates.json" % NAV_URL})


def _lane_author_heartbeat():
    row = None
    try:
        import psycopg2  # lint-ok: read-only heartbeat lookup, sibling-shell pattern
        db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not db:
            return _lane("author_heartbeat", "BLIND", "no DATABASE_URL — heartbeat unobserved")
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_run, last_status, rows_inserted FROM ingest_runs "
                    "WHERE feed = %s ORDER BY last_run DESC LIMIT 1", (AUTHOR_FEED,))
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        return _lane("author_heartbeat", "BLIND",
                     "ingest_runs unreadable (%s) — heartbeat unobserved" % type(e).__name__)
    if row is None:
        return _lane("author_heartbeat", "GAUGE",
                     "author has never beaten — expected on day one (the "
                     "workflow ships with this shell); becomes RED only by "
                     "staying silent past %dh" % AUTHOR_OVERDUE_H,
                     {"feed": AUTHOR_FEED})
    last_run, status, rows = row
    age_h = None
    if isinstance(last_run, datetime):
        ref = last_run if last_run.tzinfo else last_run.replace(tzinfo=timezone.utc)
        age_h = round((datetime.now(timezone.utc) - ref).total_seconds() / 3600.0, 1)
    ev = {"feed": AUTHOR_FEED, "last_run": str(last_run), "last_status": status,
          "rows_inserted": rows, "age_hours": age_h,
          "overdue_at_hours": AUTHOR_OVERDUE_H}
    if str(status or "").lower() not in ("success", "no_new_data"):
        return _lane("author_heartbeat", "RED",
                     "author's last beat is %r" % status, ev)
    if age_h is not None and age_h > AUTHOR_OVERDUE_H:
        return _lane("author_heartbeat", "RED",
                     "author silent %.1fh (> %dh = 2x its %dh cadence)"
                     % (age_h, AUTHOR_OVERDUE_H, AUTHOR_CADENCE_H), ev)
    return _lane("author_heartbeat", "PASS",
                 "author beat %s (%sh ago), staged %s" % (status, age_h, rows), ev)


def _state():
    lanes = [_lane_store_gate(), _lane_story_age(),
             _lane_ship_vs_story(), _lane_author_heartbeat()]
    counts = {}
    for ln in lanes:
        counts[ln["verdict"]] = counts.get(ln["verdict"], 0) + 1
    return {"shell": SHELL_NAME,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lanes": lanes,
            "summary": counts,
            "note": ("BLIND is unobserved, never a failure. GAUGE reports a "
                     "number with no honest threshold. Only lane C convicts "
                     "the pipeline; lane A convicts a silently-withheld card.")}


def _disabled():
    if os.environ.get("STORY_DEBT_SHELL_DISABLED") == "1":
        return jsonify({"shell": SHELL_NAME, "disabled": True}), 200
    return None


@story_debt_bp.route("/api/v1/admin/story-debt/master-tick", methods=["GET", "POST"])
@story_debt_bp.route("/api/v1/admin/story-debt", methods=["GET"])
def story_debt_state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    off = _disabled()
    if off:
        return off
    return jsonify(_state()), 200


_COLORS = {"PASS": "#2e7d32", "RED": "#c62828", "GAUGE": "#f9a825", "BLIND": "#757575"}


@story_debt_bp.route("/admin/story-debt", methods=["GET"])
def story_debt_board():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    off = _disabled()
    if off:
        return off
    st = _state()
    rows = []
    for ln in st["lanes"]:
        color = _COLORS.get(ln["verdict"], "#757575")
        extra = ""
        debt = (ln.get("evidence") or {}).get("debt")
        if debt:
            extra = "<ul>" + "".join(
                "<li><code>%s</code> — %s</li>" % (
                    html.escape(d.get("path", "")),
                    html.escape(" / ".join(d.get("labels") or [])))
                for d in debt) + "</ul>"
        refused = (ln.get("evidence") or {}).get("gate_refusals")
        if refused:
            extra += "<ul>" + "".join(
                "<li><code>%s</code> — %s</li>" % (
                    html.escape(str(w.get("id"))), html.escape(str(w.get("reason"))))
                for w in refused) + "</ul>"
        rows.append(
            "<div style='margin:10px 0;padding:10px;border-left:4px solid %s;"
            "background:#1b1b1b'><b style='color:%s'>%s</b> — %s%s</div>"
            % (color, color, html.escape(ln["lane"]), html.escape(ln["note"]), extra))
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='120'>"
        "<title>Story Debt — master shell</title></head>"
        "<body style='background:#111;color:#ddd;font:14px -apple-system,sans-serif;"
        "max-width:880px;margin:24px auto;padding:0 16px'>"
        "<h2>Story Debt — ship-to-story master shell</h2>"
        "<div style='color:#888'>generated %s · summary %s · JSON at "
        "<code>/api/v1/admin/story-debt/master-tick</code> (cache-bust reads)</div>%s"
        "<div style='color:#666;margin-top:14px'>%s</div></body></html>"
        % (html.escape(st["generated_at"]), html.escape(str(st["summary"])),
           "".join(rows), html.escape(st["note"])))
    return body, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "no-store"}
