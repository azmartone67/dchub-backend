"""
routes/agent_expansion_master_shell.py — Agent Expansion Master Shell (#45, 2026-08-02).

Born from the 2026-08-01 partner-round synthesis: discovery is won (#1 on ten
Smithery queries, nine registries listed, execute_plan third in every listing
as of 08-02) — expansion now lives in what happens AFTER discovery. Five
lanes, one per lever, in the order the operator ranked them:

  1. FRONT-DOOR FUNNEL — the Smithery playground receipt: humans clicked
     tools top-down and called execute_plan all but never. The listing
     reorder (mcp-server #124, live 2026-08-02) is the intervention; this
     lane watches whether front-door calls actually move, with the pre-move
     baseline in the detail so drift is visible against a fact, not a vibe.
  2. PLANNER ADOPTION — reach converted, behavior hasn't: first-call-is-
     execute_plan per agent-day plus the per-platform attribution gate state
     (born gated until 7 clean days after the 07-28 attribution fix). The
     lane surfaces the measured rate — it does not invent a target.
  3. PLATFORM DOORS — the three doors only a human can open: the Anthropic
     Connectors Directory (Team-org portal), Microsoft 365 tenant attach
     (documented live in mcp-server #118), Le Chat catalog (Mistral-side).
     Statuses are HUMAN-OWNED rows; the lane is BORN RED until a door reads
     'open'. The red is the work order, exactly like ascension lane 7.
  4. PARTNER KEYS — Grok asked for a scoped power-user key program
     (out-of-band issuance, never through chat). BORN RED until at least one
     partner key is issued AND used — the lane that keeps the platform-
     licence motion from silently stalling.
  5. ENTERPRISE EMBEDDING — long-lived configured agents compound; sampling
     doesn't. Embedded = an agent active on >=5 distinct days in 30d on the
     canonical identity basis. The lane surfaces the count and keeps the
     cohort instrumentation (report v5) importable.

★ HONESTY RULE (inherited from Integrity #25 via Ascension #28): a lane must
never read PASS when it couldn't check — an indeterminate critical check is
not green. Born-red lanes render FAIL until the world changes, and say so.

★ COUNT DISCIPLINE: every agent/call figure in this shell comes from the
canonical identity basis (mcp_calls_deloop.canonical_external_activity_sql /
the mcp_calls_identity view) — never raw ip strings, never session_id.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing.
PURE-DB: lanes never make HTTP calls (the 2026-07-06 flywheel-outage
invariant); there is no scan endpoint because nothing here needs one.

v1 has NO cron and NO dead-man beat ON PURPOSE — the schedule/_RUNNERS split
has bitten before (a scheduled name absent from _RUNNERS fires zero, silently)
— wave 2 registers both together or not at all. The board is on-demand.

Endpoints:
  GET/POST /api/v1/admin/agent-expansion/master-tick   JSON scoreboard (5 lanes)
  GET      /admin/agent-expansion                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/agent-expansion                CF zone-worker bypass alias

Auth: same admin gate as the sibling shells (X-Admin-Key / ?admin_key=).
Kill: AGENT_EXPANSION_SHELL_DISABLE=1

★ WAVE 2 (2026-08-02, same day): the operator asked for the four ACCELERATION
moves as a board. Moves 1+2 (open a door / issue a partner key) ALREADY ARE
lanes 3+4 — duplicating them into a second board is the two-sources-of-truth
drift this repo keeps killing, so wave 2 adds only the two unmeasured moves:

  6. STORY SHIPPED — the record week is untold until it's posted. Automated
     half: the media pipeline is actually publishing (cards + X diversity
     landed 08-01). Human half: the owner-voiced LinkedIn piece is a
     personal-account act our DB cannot see — a human-owned row flips it.
  7. DATA DECIDES — the discipline that the NEXT build target is chosen from
     Tuesday's per-platform read, not from enthusiasm. BORN RED THROUGH
     TUESDAY on purpose: the row flips to 'decided' only after the gate opens
     and the owner records which target the data picked.

Human-owned rows share agent_expansion_doors (one status registry, one
ownership rule); lane 3's aggregate is scoped to ITS doors so a posted story
can never green the doors lane.
"""
from __future__ import annotations

import datetime as _dt
import os

from flask import Blueprint, Response, jsonify

# Shared shell plumbing — imported, not copied, so the honesty semantics can
# never drift between boards (the transcribed-contract lesson).
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _conn, _lane_verdict, _safe_lane)

agent_expansion_master_shell_bp = Blueprint(
    "agent_expansion_master_shell", __name__)

# The listing reorder that lane 1 watches for effect (mcp-server #124).
LIST_REORDER_DATE = _dt.date(2026, 8, 2)
# The attribution fix the per-platform gate counts clean days from
# (mirrors routes/agent_success_report.py — one date, stated once here).
ATTRIBUTION_FIX_DATE = _dt.date(2026, 7, 28)

_DOORS_SEED = (
    ("anthropic_directory",
     "pending-owner",
     "Team/Enterprise portal submission — package ready since 07-03, "
     "refreshed 07-31 (~/Downloads portal package). Owner decision: Team org."),
    ("m365_tenant_attach",
     "pending-owner",
     "Tenant attach works today; certification = verified publisher "
     "(mcp-server #118). Owner action: attach in a tenant, then decide on "
     "certification."),
    ("lechat_catalog",
     "pending-partner",
     "Mistral-side: escalation sent 07-31 with the catalogue-contradiction "
     "argument; scoreboard is the agreed verification surface."),
)

# Wave-2 human-owned rows (lanes 6+7). Same table, same ownership rule; NOT
# part of _DOORS_SEED so lane 3's aggregate never reads them.
_ACCEL_SEED = (
    ("story_posted",
     "pending-owner",
     "The owner-voiced LinkedIn piece + connector-CTA X draft (verified "
     "drafts in ~/Downloads, 08-01). Personal-account posting is invisible "
     "to this DB — flip to 'open' when posted."),
    ("post_gate_decision",
     "pending-data",
     "Per-platform gate opens 2026-08-04; the scheduled Tuesday read "
     "surfaces the board. Flip to 'decided' with a note naming the target "
     "the DATA picked. Staying red until then is the discipline, not a bug."),
)


def _disabled() -> bool:
    return os.environ.get("AGENT_EXPANSION_SHELL_DISABLE", "") == "1"


# ── lane 1 · front-door funnel ───────────────────────────────────────────────
def _lane_front_door() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("front_door_calls", "execute_plan calls measured (7d)",
                       False, "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE created_at >= now() - interval '7 days'),
                  COUNT(*) FILTER (WHERE created_at <  now() - interval '7 days'
                                     AND created_at >= now() - interval '14 days')
                  FROM mcp_calls_identity
                 WHERE is_public_ip AND is_real_external
                   AND tool_name = 'execute_plan'
            """)
            row = cur.fetchone() or (0, 0)
            cur_7d, prev_7d = int(row[0] or 0), int(row[1] or 0)
            cur.execute("""
                SELECT COUNT(*) FROM mcp_calls_identity
                 WHERE is_public_ip AND is_real_external
                   AND created_at >= now() - interval '7 days'
            """)
            total_7d = int((cur.fetchone() or [0])[0] or 0)
        share = (100.0 * cur_7d / total_7d) if total_7d else None
        checks.append(_check(
            "front_door_calls", "execute_plan calls measured (7d)", True,
            f"{cur_7d} this 7d vs {prev_7d} prior 7d — listing reorder live "
            f"{LIST_REORDER_DATE.isoformat()} (mcp#124); judge the move "
            f"against that date, not against hope", critical=True))
        checks.append(_check(
            "front_door_share", "front-door share of real calls (7d)",
            share is not None,
            (f"{share:.2f}% of {total_7d} real calls"
             if share is not None else
             "zero real calls in window — share unmeasurable"),
            critical=True))
    except Exception as e:
        checks.append(_check(
            "front_door_calls", "execute_plan calls measured (7d)", False,
            f"probe failed: {type(e).__name__}: {str(e)[:100]}", critical=True))
    finally:
        try: c.close()
        except Exception: pass
    return checks


# ── lane 2 · planner adoption ────────────────────────────────────────────────
def _lane_planner_adoption() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("adoption_rate", "first-call-is-execute_plan (agent-day)",
                       False, "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            # First call of each (agent, day): the same agent-day unit the
            # planner-bypass work established — never session_id.
            cur.execute("""
                WITH firsts AS (
                  SELECT DISTINCT ON (agent_id, created_at::date)
                         tool_name
                    FROM mcp_calls_identity
                   WHERE is_public_ip AND is_real_external
                     AND created_at >= now() - interval '7 days'
                   ORDER BY agent_id, created_at::date, created_at
                )
                SELECT COUNT(*) FILTER (WHERE tool_name = 'execute_plan'),
                       COUNT(*)
                  FROM firsts
            """)
            row = cur.fetchone() or (0, 0)
            fd_first, episodes = int(row[0] or 0), int(row[1] or 0)
            # ★2026-08-04: this counted only platform='mcp'. The gate's own
            # definition of "generic" is GENERIC_BUCKETS — 'mcp' AND
            # 'mcp-generic-client', the 07-28 rename. Counting one of the two
            # under-reports the share and feeds the gate a number that does not
            # mean what the gate thinks it means. Import the list rather than
            # restate it, so a future rename cannot desync them again.
            from routes.agent_success_report import GENERIC_BUCKETS as _GB
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE platform = ANY(%s)),
                  COUNT(*)
                  FROM mcp_calls_identity
                 WHERE is_public_ip AND is_real_external
                   AND created_at >= now() - interval '7 days'
            """, (list(_GB),))
            prow = cur.fetchone() or (0, 0)
            mcp_calls, all_calls = int(prow[0] or 0), int(prow[1] or 0)
        if episodes:
            rate = 100.0 * fd_first / episodes
            checks.append(_check(
                "adoption_rate", "first-call-is-execute_plan (agent-day)", True,
                f"{rate:.1f}% of {episodes} agent-days opened with the front "
                f"door ({fd_first}) — the measured value IS the point; no "
                f"invented target", critical=True))
        else:
            checks.append(_check(
                "adoption_rate", "first-call-is-execute_plan (agent-day)", False,
                "zero agent-day episodes in 7d — UNMEASURED", critical=True))
        # Contract-read: the SAME gate function the public report uses.
        try:
            from routes.agent_success_report import _attribution_gate
            days_since = (_dt.date.today() - ATTRIBUTION_FIX_DATE).days
            # ★2026-08-04 UNIT BUG: this passed a PERCENTAGE (27.4) into a
            # gate that compares against MCP_BUCKET_MAX_SHARE_TO_PUBLISH = 0.8,
            # a FRACTION. 27.4 > 0.8, so the lane reported
            # GATED_ATTRIBUTION_UNVERIFIED while the public report — running
            # the same gate on the same day with the correct units — reported
            # MEASURED. Two readings of one question, disagreeing, because of
            # a factor of 100. Fraction, as the gate documents.
            mcp_share = (mcp_calls / all_calls) if all_calls else 1.0
            gate = _attribution_gate(days_since, mcp_share)
            state = "OPEN" if gate[0] else "GATED"
            checks.append(_check(
                "per_platform_gate", "per-platform gate state readable", True,
                f"{state} — {gate[1] if len(gate) > 1 else ''} "
                f"(day {days_since} post-fix, generic bucket {100 * mcp_share:.1f}%)",
                critical=False))
        except Exception as e:
            checks.append(_check(
                "per_platform_gate", "per-platform gate state readable", False,
                f"gate import/compute failed: {str(e)[:100]}", critical=False))
    except Exception as e:
        checks.append(_check(
            "adoption_rate", "first-call-is-execute_plan (agent-day)", False,
            f"probe failed: {type(e).__name__}: {str(e)[:100]}", critical=True))
    finally:
        try: c.close()
        except Exception: pass
    return checks


# ── lane 3 · platform doors ──────────────────────────────────────────────────
def _ensure_doors(c) -> None:
    with c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_expansion_doors (
                door       TEXT PRIMARY KEY,
                status     TEXT NOT NULL,
                note       TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        for door, status, note in _DOORS_SEED + _ACCEL_SEED:
            # ON CONFLICT DO NOTHING is DELIBERATE here and documented:
            # status is HUMAN-OWNED once set — a redeploy must never clobber
            # an operator's 'open'/'declined' back to the seed. (Contrast the
            # mcp_presence_listings listing_url case, where DO NOTHING was the
            # bug BECAUSE the seed was the source of truth. Ownership decides.)
            cur.execute("""
                INSERT INTO agent_expansion_doors (door, status, note)
                VALUES (%s, %s, %s)
                ON CONFLICT (door) DO NOTHING
            """, (door, status, note))
    c.commit()


def _lane_platform_doors() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("doors_readable", "door registry readable", False,
                       "db unavailable — could not check", critical=True)]
    try:
        _ensure_doors(c)
        door_names = [d[0] for d in _DOORS_SEED]
        with c.cursor() as cur:
            # Scoped to lane 3's OWN doors — wave-2 rows share this table and
            # must never satisfy (or fail) the doors aggregate.
            cur.execute("""
                SELECT door, status,
                       EXTRACT(EPOCH FROM (now() - updated_at))/86400.0
                  FROM agent_expansion_doors
                 WHERE door = ANY(%s) ORDER BY door
            """, (door_names,))
            rows = {r[0]: (r[1], float(r[2] or 0)) for r in (cur.fetchall() or [])}
        for door, _, _ in _DOORS_SEED:
            st, age = rows.get(door, (None, None))
            checks.append(_check(
                f"door_{door}", f"{door} status tracked", st is not None,
                (f"status={st} · {age:.1f}d since update"
                 if st is not None else "row missing"), critical=False))
        any_open = any(v[0] == "open" for v in rows.values())
        checks.append(_check(
            "any_door_open", "at least one door is OPEN", any_open,
            ("a door is open" if any_open else
             "BORN RED — all three doors await their owners (update via "
             "UPDATE agent_expansion_doors SET status='open', "
             "updated_at=now() WHERE door='…'); the red is the work order"),
            critical=True))
    except Exception as e:
        checks.append(_check(
            "doors_readable", "door registry readable", False,
            f"probe failed: {type(e).__name__}: {str(e)[:100]}", critical=True))
    finally:
        try: c.close()
        except Exception: pass
    return checks


# ── lane 4 · partner keys ────────────────────────────────────────────────────
def _lane_partner_keys() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("partner_storage", "partner-key storage queryable", False,
                       "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            # Introspect before use (the standing-page discipline): find where
            # partner keys live rather than assuming a table shape.
            cur.execute("SELECT to_regclass('partner_keys')")
            has_pk = (cur.fetchone() or [None])[0] is not None
            issued = used_30d = None
            if has_pk:
                cur.execute("SELECT COUNT(*) FROM partner_keys")
                issued = int((cur.fetchone() or [0])[0] or 0)
                cur.execute("""
                    SELECT COUNT(*) FROM information_schema.columns
                     WHERE table_name='partner_keys' AND column_name='last_used_at'
                """)
                if int((cur.fetchone() or [0])[0] or 0):
                    cur.execute("""
                        SELECT COUNT(*) FROM partner_keys
                         WHERE last_used_at >= now() - interval '30 days'
                    """)
                    used_30d = int((cur.fetchone() or [0])[0] or 0)
        checks.append(_check(
            "partner_storage", "partner-key storage queryable", has_pk,
            ("partner_keys present" if has_pk else
             "partner_keys table absent — issuer may store elsewhere; "
             "introspection found nothing to count"), critical=True))
        active = bool(issued) and (used_30d is None or used_30d > 0)
        checks.append(_check(
            "partner_key_active", "a partner key is issued and in use", active,
            (f"{issued} issued · {used_30d if used_30d is not None else '?'} "
             f"used in 30d" if issued else
             "BORN RED — none issued; the Grok power-key program is "
             "owner-gated (scope + out-of-band issuance; never through chat)"),
            critical=True))
    except Exception as e:
        checks.append(_check(
            "partner_storage", "partner-key storage queryable", False,
            f"probe failed: {type(e).__name__}: {str(e)[:100]}", critical=True))
    finally:
        try: c.close()
        except Exception: pass
    return checks


# ── lane 5 · enterprise embedding ────────────────────────────────────────────
def _lane_enterprise_embedding() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("embedded_agents", "embedded agents measured (30d)",
                       False, "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM (
                  SELECT agent_id
                    FROM mcp_calls_identity
                   WHERE is_public_ip AND is_real_external
                     AND created_at >= now() - interval '30 days'
                   GROUP BY agent_id
                  HAVING COUNT(DISTINCT created_at::date) >= 5
                ) embedded
            """)
            embedded = int((cur.fetchone() or [0])[0] or 0)
        checks.append(_check(
            "embedded_agents", "embedded agents measured (30d)", True,
            f"{embedded} agents active on >=5 distinct days in 30d "
            f"(canonical identity basis) — the compounding cohort",
            critical=True))
        try:
            from routes.agent_success_report import _cohort_rollup  # noqa: F401
            checks.append(_check(
                "cohort_instrumentation", "report v5 cohort machinery importable",
                True, "_cohort_rollup importable — cohorts stay first-class",
                critical=False))
        except Exception as e:
            checks.append(_check(
                "cohort_instrumentation", "report v5 cohort machinery importable",
                False, f"import failed: {str(e)[:100]}", critical=False))
    except Exception as e:
        checks.append(_check(
            "embedded_agents", "embedded agents measured (30d)", False,
            f"probe failed: {type(e).__name__}: {str(e)[:100]}", critical=True))
    finally:
        try: c.close()
        except Exception: pass
    return checks


# ── lane 6 · story shipped ───────────────────────────────────────────────────
def _lane_story_shipped() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("media_flowing", "media pipeline publishing (7d)", False,
                       "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM linkedin_posts
                 WHERE COALESCE(posted_at, created_at) >= now() - interval '7 days'
                   AND COALESCE(status, 'published') NOT IN ('failed', 'error')
            """)
            published_7d = int((cur.fetchone() or [0])[0] or 0)
        checks.append(_check(
            "media_flowing", "media pipeline publishing (7d)",
            published_7d > 0,
            (f"{published_7d} posts in 7d — cards live behind DCHUB_LI_CARDS, "
             f"X diversity ported 08-01" if published_7d else
             "zero posts in 7d — the pipeline that just gained cards and X "
             "diversity is not flowing; check the publisher token path"),
            critical=True))
        with c.cursor() as cur:
            cur.execute("""
                SELECT status, EXTRACT(EPOCH FROM (now() - updated_at))/86400.0
                  FROM agent_expansion_doors WHERE door = 'story_posted'
            """)
            row = cur.fetchone()
        st, age = (row[0], float(row[1] or 0)) if row else (None, None)
        checks.append(_check(
            "story_posted", "the record week is TOLD (owner post)",
            st == "open",
            (f"posted ({age:.1f}d since flip)" if st == "open" else
             f"BORN RED — status={st or 'missing'}; the verified drafts sit "
             f"in ~/Downloads; flip with UPDATE agent_expansion_doors SET "
             f"status='open', updated_at=now() WHERE door='story_posted'"),
            critical=True))
    except Exception as e:
        checks.append(_check(
            "media_flowing", "media pipeline publishing (7d)", False,
            f"probe failed: {type(e).__name__}: {str(e)[:100]}", critical=True))
    finally:
        try: c.close()
        except Exception: pass
    return checks


# ── lane 7 · data decides ────────────────────────────────────────────────────
def _lane_data_decides() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("post_gate_decision", "next target chosen FROM the data",
                       False, "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT status, note, EXTRACT(EPOCH FROM (now() - updated_at))/86400.0
                  FROM agent_expansion_doors WHERE door = 'post_gate_decision'
            """)
            row = cur.fetchone()
        st = row[0] if row else None
        note = (row[1] or "") if row else ""
        gate_day = (_dt.date.today() - ATTRIBUTION_FIX_DATE).days
        if st == "decided":
            checks.append(_check(
                "post_gate_decision", "next target chosen FROM the data", True,
                f"decided — {note[:140]}", critical=True))
        else:
            countdown = ("gate opens 2026-08-04 — day "
                         f"{gate_day} of 7 post-fix accumulation"
                         if gate_day < 7 else
                         "gate window ELAPSED — read the per-platform board "
                         "and record the decision")
            checks.append(_check(
                "post_gate_decision", "next target chosen FROM the data", False,
                f"BORN RED THROUGH TUESDAY by design — {countdown}; flip to "
                f"'decided' with a note naming the target the data picked",
                critical=True))
    except Exception as e:
        checks.append(_check(
            "post_gate_decision", "next target chosen FROM the data", False,
            f"probe failed: {type(e).__name__}: {str(e)[:100]}", critical=True))
    finally:
        try: c.close()
        except Exception: pass
    return checks


# ── tick + endpoints ─────────────────────────────────────────────────────────
def _run_tick() -> dict:
    lanes = [
        {"id": "front_door_funnel", "name": "1 · front-door funnel",
         "checks": _safe_lane(_lane_front_door)},
        {"id": "planner_adoption", "name": "2 · planner adoption",
         "checks": _safe_lane(_lane_planner_adoption)},
        {"id": "platform_doors", "name": "3 · platform doors",
         "checks": _safe_lane(_lane_platform_doors)},
        {"id": "partner_keys", "name": "4 · partner keys",
         "checks": _safe_lane(_lane_partner_keys)},
        {"id": "enterprise_embedding", "name": "5 · enterprise embedding",
         "checks": _safe_lane(_lane_enterprise_embedding)},
        {"id": "story_shipped", "name": "6 · story shipped",
         "checks": _safe_lane(_lane_story_shipped)},
        {"id": "data_decides", "name": "7 · data decides",
         "checks": _safe_lane(_lane_data_decides)},
    ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    return {
        "ok": True,
        "shell": "agent-expansion-master-shell#45",
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        "summary": " ".join(f"{ln['id']}={ln['verdict']}" for ln in lanes),
        "lanes": lanes,
        "kill": "AGENT_EXPANSION_SHELL_DISABLE=1",
        "note": ("READ-ONLY board over the five expansion levers (2026-08-01 "
                 "ranking). Lanes 3+4 are BORN RED by design — they go green "
                 "when a human opens a door / issues a partner key, not when "
                 "code changes. v1 has no cron on purpose (see docstring)."),
    }


@agent_expansion_master_shell_bp.route(
    "/api/v1/admin/agent-expansion/master-tick", methods=["GET", "POST"])
def master_tick():
    if _disabled():
        return jsonify(ok=False, error="AGENT_EXPANSION_SHELL_DISABLE=1"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(_run_tick())
    resp.headers["Cache-Control"] = "no-store"
    return resp


_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Agent Expansion Master Shell #45</title>
<meta http-equiv="refresh" content="60">
<style>body{font-family:-apple-system,sans-serif;background:#0a0a12;color:#eee;
padding:24px;max-width:980px;margin:auto}h1{font-size:20px}
.lane{border:1px solid #2a2c3e;border-radius:10px;padding:12px 16px;margin:10px 0}
.PASS{border-left:4px solid #10b981}.FAIL{border-left:4px solid #ef4444}
.chk{margin:4px 0;font-size:14px}.d{color:#9ca3af}
small{color:#6b7280}</style></head><body>
<h1>Agent Expansion Master Shell #45</h1>
<small>generated %%GEN%% · read-only · refreshes 60s · lanes 3+4 born red by
design · kill AGENT_EXPANSION_SHELL_DISABLE=1</small>
%%LANES%%
</body></html>"""


@agent_expansion_master_shell_bp.route("/admin/agent-expansion", methods=["GET"])
@agent_expansion_master_shell_bp.route("/api/v1/admin/agent-expansion",
                                       methods=["GET"])
def dashboard():
    if _disabled():
        return Response("agent-expansion shell disabled", status=503,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("admin key required", status=401, mimetype="text/plain")
    tick = _run_tick()
    lanes_html = []
    for ln in tick["lanes"]:
        rows = "".join(
            f'<div class="chk">{"✅" if ck.get("passed") else "❌"} '
            f'{ck.get("name", "?")} — <span class="d">'
            f'{ck.get("detail", "")}</span></div>'
            for ck in ln["checks"])
        lanes_html.append(
            f'<div class="lane {ln["verdict"]}"><b>{ln["name"]}</b> '
            f'· {ln["verdict"]}{rows}</div>')
    html = (_PAGE.replace("%%GEN%%", tick["generated_at"])
                 .replace("%%LANES%%", "".join(lanes_html)))
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp
