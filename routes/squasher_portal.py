"""routes/squasher_portal.py — the bug-squasher portal (2026-08-08).

ONE PANE for "is the squasher performing?" — the question that otherwise costs
an interactive session to answer. Reads the five stages of the tag-team loop
(docs/BRAIN_SUPERUSER_TAGTEAM.md) from their own live surfaces and renders the
honest number for each.

★ WHY IT LIVES UNDER /api/v1/brain/
Two edge traps decide this path, both paid for already:
  1. Non-`/api` HTML needs PHASE_282 allow-listing in the CF worker or it hits
     the Error-1000 trap (brain_innovation_dashboard.py's own note).
  2. ANY NEW `/api/v1/*` prefix launches STALE — CF Rule #3 caches `/api/v1/*`
     with mode:override_origin, which bulldozes no-store. The QA board lost
     ~42 minutes to this and had to have bypass rule d71d7b9b appended.
`/api/v1/brain/` ALREADY has bypass rule 6407517b, so mounting here inherits a
proven-live bypass and needs no new CF rule. Do not move this page to a new
prefix without appending one first.

★ THE HEADLINE IS ACTUATION, NOT ACTIVITY. Every stage here can look busy while
shipping nothing — that is the exact failure the audit named ("the platform
SEES almost everything and ACTS on almost nothing"). So the top-line verdict is
driven by fixes LANDED in the last 7 days, and a loop that detects, routes and
proposes but merges nothing reads AMBER/RED, never green.

Surface:  GET /api/v1/brain/squasher            (HTML)
          GET /api/v1/brain/squasher.json       (JSON, same data)
Kill:     SQUASHER_PORTAL_DISABLE=1
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

squasher_portal_bp = Blueprint("squasher_portal", __name__)

_ACTUATION_WINDOW_DAYS = 7


def _disabled() -> bool:
    return os.environ.get("SQUASHER_PORTAL_DISABLE", "0") == "1"


def _admin_ok() -> bool:
    """Mirror of the brain dashboards' gate — header, ?admin_key=, or cookie."""
    keys = set()
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v:
            keys.add(v)
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key")
            or request.cookies.get("dchub_innov_key") or "").strip()
    return bool(sent) and sent in keys


def _self_auth_headers() -> dict:
    """Credentials for a LOOPBACK read.

    ★ A self-call is an ANONYMOUS call unless you say otherwise. The first live
    run of this page proved it: four of five stages are admin-gated, the
    test_client carried no key, and detect/route/act all came back 401 → {} →
    "cannot read". Same class as the /radar gate 402'ing its own loopback and
    the mcp-server's self-calls needing X-Internal-Key: the request that
    originates inside the app still passes through every before_request gate.

    The page itself is already admin-gated, so this never widens access — it
    only lets the page read what its caller was already authorised to see.
    """
    h = {}
    admin = os.environ.get("DCHUB_ADMIN_KEY")
    internal = os.environ.get("DCHUB_INTERNAL_KEY") or os.environ.get("INTERNAL_KEY")
    if admin:
        h["X-Admin-Key"] = admin
    if internal:
        h["X-Internal-Key"] = internal
    return h


def _get(path: str) -> dict:
    """Internal read. Returns {} on any failure — a dead stage must render as
    UNKNOWN, never as a zero that reads like 'nothing wrong here'."""
    try:
        from flask import current_app
        with current_app.test_client() as c:
            r = c.get(path, headers=_self_auth_headers())
            if r.status_code != 200:
                logger.info("squasher portal: %s -> HTTP %s (stage reads UNKNOWN)",
                            path, r.status_code)
                return {}
            return r.get_json() or {}
    except Exception:
        return {}


def _age_days(iso: str | None):
    if not iso:
        return None
    try:
        t = str(iso).replace("Z", "+00:00")
        ts = datetime.fromisoformat(t)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    except Exception:
        return None


# ── the five stages ──────────────────────────────────────────────────────

def collect() -> dict:
    """Read every stage. Each returns its own `known` flag so the page can
    distinguish 'measured zero' from 'could not look' — the BLIND≠RED rule the
    QA superuser enforces at construction."""
    out = {"as_of": datetime.now(timezone.utc).isoformat()}

    # DETECT ─ audit registry + heal findings
    intake = _get("/api/v1/brain/audit-intake")
    out["detect"] = {
        "known": bool(intake),
        "open_red": intake.get("open_red_total"),
        "seeded": len(intake.get("seeded") or []),
        "deferred": intake.get("deferred_to_next_cycle"),
        "registry_total": intake.get("registry_total"),
        "closure_pct": intake.get("closure_pct"),
    }

    # ROUTE ─ finding router buckets
    routes = _get("/api/v1/brain/finding-routes")
    counts = (routes.get("counts") or {}) if routes else {}
    out["route"] = {
        "known": bool(counts),
        "active": counts.get("active"),
        "operator_config": counts.get("operator_config"),
        "mcp_server": counts.get("mcp_server"),
        "terminal": counts.get("terminal"),
    }

    # The ACTIONABLE list — what the operator can press a button on. Only the
    # `active` bucket: an operator_config finding is theirs to decide, an
    # mcp_server one belongs to another repo, and a terminal one is already
    # triaged. Offering a fix button on those would be offering a lever that
    # does nothing.
    out["actionable"] = [
        {"key": f.get("url") or "", "title": (f.get("issue") or "")[:200],
         "source": "heal"}
        for f in ((routes.get("active") or []) if routes else [])[:20]
        if (f.get("url") or f.get("issue"))
    ]

    # The operator's own queue — what they already submitted, and its outcome.
    try:
        from routes.squasher_queue import queue_rows
        out["queue"] = queue_rows(12)
    except Exception:
        out["queue"] = []

    # PROPOSE ─ the three-state recorder
    ps = _get("/api/v1/brain/propose-stage/status")
    runs = (ps.get("runs") or []) if ps else []
    considered = sum(int(r.get("considered") or 0) for r in runs[:10])
    generated = sum(int(r.get("generated") or 0) for r in runs[:10])
    out["propose"] = {
        "known": bool(runs),
        "verdict": ps.get("verdict"),
        "jam_streak": ps.get("jam_streak"),
        "threshold": ps.get("streak_threshold"),
        "considered_10": considered,
        "generated_10": generated,
        "last_run": (runs[0].get("ts") if runs else None),
        "runs": [{"ts": r.get("ts"), "source": r.get("source"),
                  "considered": r.get("considered"),
                  "generated": r.get("generated")} for r in runs[:8]],
    }

    # ACT ─ the auto-merge lane. This is the stage that decides the verdict.
    am = _get("/api/v1/brain/automerge/status")
    recent = (am.get("recent") or []) if am else []
    merges = [r for r in recent if (r.get("kind") or "") == "merge"]
    last_merge = merges[0] if merges else None
    last_age = _age_days(last_merge.get("merged_at_utc")) if last_merge else None
    landed_7d = 0
    for m in merges:
        a = _age_days(m.get("merged_at_utc"))
        if a is not None and a <= _ACTUATION_WINDOW_DAYS:
            landed_7d += 1
    out["act"] = {
        "known": bool(am),
        "enabled": am.get("enabled"),
        "breaker_tripped": am.get("breaker_tripped"),
        "rate_cap": am.get("rate_cap"),
        "landed_7d": landed_7d if am else None,
        "last_merge_pr": (last_merge or {}).get("pr_number"),
        "last_merge_at": (last_merge or {}).get("merged_at_utc"),
        "last_merge_days": round(last_age, 1) if last_age is not None else None,
        "last_merge_class": (last_merge or {}).get("klass"),
    }

    # ACTION CLASSES ─ claim loop step 2 (routes/squasher_action_classes.py).
    # Direct import, not a loopback GET: an in-process read cannot 401
    # itself. UNKNOWN when unreadable — never "nothing granted".
    try:
        from routes.squasher_action_classes import summary as _classes_summary
        out["action_classes"] = _classes_summary()
    except Exception:
        out["action_classes"] = {"known": False}
    out["act"] = fold_class_runs(out["act"], out["action_classes"])

    # QUEUE AGES (#65 B): per status × class, how long the human queue has
    # waited. Direct import; UNKNOWN when unreadable — never "nothing waits".
    try:
        from routes.squasher_queue import queue_ages as _queue_ages
        out["queue_ages"] = _queue_ages()
    except Exception:
        out["queue_ages"] = {"known": False}

    # VERIFY ─ closure of the audit registry
    out["verify"] = {
        "known": bool(intake),
        "closure_pct": intake.get("closure_pct"),
        "registry_total": intake.get("registry_total"),
    }

    # SPEC DEBT ─ the obligation book (Phase 0, 2026-08-14). Direct import,
    # not a loopback GET: an in-process read cannot 401 itself. UNKNOWN when
    # unreadable — never zero (the 60/0 checklist hole was invisible exactly
    # because no surface counted it).
    try:
        from routes.brain_spec_debt import spec_debt_summary
        out["spec_debt"] = spec_debt_summary()
    except Exception:
        out["spec_debt"] = {"known": False, "state": "UNMEASURED"}

    out["verdict"] = verdict_for(out)
    return out


def fold_class_runs(act: dict, classes: dict) -> dict:
    """Granted-class runs that VERIFIED count as fixes LANDED.

    Only verified runs: an executed run whose verifier showed no drop is not
    a fix, whatever the HTTP status said. And an unreadable class stage adds
    nothing — `class_runs_verified_7d` stays None rather than a zero that
    reads as measured. The auto-merge count survives as `merges_7d`.
    """
    act = dict(act or {})
    act["merges_7d"] = act.get("landed_7d")
    v7 = None
    if (classes or {}).get("known"):
        try:
            v7 = int(classes.get("verified_7d") or 0)
        except (TypeError, ValueError):
            v7 = None
    act["class_runs_verified_7d"] = v7
    if v7:
        act["landed_7d"] = int(act.get("landed_7d") or 0) + v7
    return act


def verdict_for(d: dict) -> dict:
    """The honest top line.

    ★ Actuation is the ONLY thing that can make this green. A loop that
    detects, routes and proposes but merges nothing is not 'performing' — that
    was true of this platform for six weeks and every stage-level indicator
    looked fine throughout. Order matters: hard failures first, then the
    actuation question, then the propose question.
    """
    act = d.get("act") or {}
    prop = d.get("propose") or {}

    def _with_debt(v: dict) -> dict:
        """Attach the spec-debt reading to whatever verdict was reached, so
        the verdict SEES the obligation book (Phase 0 exit criterion). It
        annotates — it never flips a state: debt is an obligation ledger,
        not an actuation failure, and the headline stays actuation-driven."""
        sd = d.get("spec_debt") or {}
        if sd.get("known"):
            v["spec_debt_open"] = sd.get("open")
            v["spec_debt_unknown"] = sd.get("unknown")
            v["detail"] = (v.get("detail", "") +
                           f" Spec debt: {sd.get('open')} open obligation(s), "
                           f"{sd.get('unknown')} no-checklist doc(s) UNKNOWN "
                           f"(basis: /api/v1/brain/spec-debt).")
        else:
            v["spec_debt_open"] = None
            v["detail"] = (v.get("detail", "") +
                           " Spec debt: UNMEASURED (queue unreadable — not "
                           "zero).")
        return v

    if not act.get("known"):
        return _with_debt({"state": "UNKNOWN", "headline": "Cannot read the auto-merge lane",
                "detail": "The act stage did not answer. This page will not "
                          "guess — an unobserved stage is never a pass."})
    if act.get("breaker_tripped"):
        return _with_debt({"state": "RED", "headline": "Breaker tripped — the lane is halted",
                "detail": "An auto-merge was reverted by its canary. The lane "
                          "stays closed until an operator clears it."})
    if not act.get("enabled"):
        return _with_debt({"state": "RED", "headline": "Auto-merge is disarmed",
                "detail": "BRAIN_AUTOMERGE_ENABLED is off — nothing can land "
                          "without a human, whatever the other stages show."})

    landed = act.get("landed_7d")
    if landed and landed > 0:
        return _with_debt({"state": "GREEN",
                "headline": f"{landed} fix(es) landed in the last "
                            f"{_ACTUATION_WINDOW_DAYS} days",
                "detail": "Detect → propose → merge → verify is closing "
                          "end-to-end without a human in the fix loop."})

    days = act.get("last_merge_days")
    ago = f"{days:.0f} days ago" if days is not None else "never"
    if (prop.get("generated_10") or 0) == 0 and (prop.get("considered_10") or 0) > 0:
        return _with_debt({"state": "AMBER",
                "headline": f"Armed and idle — 0 fixes in "
                            f"{_ACTUATION_WINDOW_DAYS}d (last: {ago})",
                "detail": "The lane is armed and healthy but the propose stage "
                          "is emitting nothing, so it has nothing to merge. "
                          "The bottleneck is PROPOSE, not ACT."})
    return _with_debt({"state": "AMBER",
            "headline": f"No fix landed in {_ACTUATION_WINDOW_DAYS}d "
                        f"(last: {ago})",
            "detail": "Detection and routing are running. Actuation is not "
                      "yet proven — this is the number to watch."})


# ── rendering ────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _n(v, dash="—"):
    """A number, or a dash. NEVER coerce unknown to 0 — that is the lie this
    whole loop exists to stop telling."""
    return dash if v is None else str(v)


_CSS = """
:root{--bg:#0a0a12;--surface:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;
 --tx3:#6b7280;--indigo:#6366f1;--violet:#a855f7;--green:#10b981;
 --amber:#f59e0b;--red:#ef4444;--mono:'JetBrains Mono','SF Mono',ui-monospace,
 monospace;color-scheme:dark}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
 background:var(--bg);color:var(--tx);margin:0;line-height:1.5;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:2rem 1.25rem}
.kicker{font-family:var(--mono);font-size:.74rem;color:#c4b5fd;
 text-transform:uppercase;letter-spacing:.14em;margin-bottom:.5rem;
 display:flex;align-items:center;gap:.5rem}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);
 box-shadow:0 0 8px var(--green);animation:p 2s ease-in-out infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
h1{margin:0 0 .35rem;font-size:1.9rem;font-weight:800;letter-spacing:-.02em;
 background:linear-gradient(90deg,#fff,#c4b5fd);-webkit-background-clip:text;
 background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:820px;margin:0 0 1.5rem;font-size:.92rem}
.verdict{border-radius:14px;padding:1.35rem 1.5rem;margin-bottom:1.75rem;
 border:1px solid var(--bd);background:var(--surface);position:relative;
 overflow:hidden}
.verdict::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.v-GREEN::before{background:var(--green)} .v-AMBER::before{background:var(--amber)}
.v-RED::before{background:var(--red)} .v-UNKNOWN::before{background:var(--tx3)}
.v-state{font-family:var(--mono);font-size:.72rem;letter-spacing:.14em;
 text-transform:uppercase;margin-bottom:.4rem}
.v-GREEN .v-state{color:var(--green)} .v-AMBER .v-state{color:var(--amber)}
.v-RED .v-state{color:var(--red)} .v-UNKNOWN .v-state{color:var(--tx3)}
.v-head{font-size:1.3rem;font-weight:700;margin-bottom:.35rem}
.v-detail{color:var(--tx2);font-size:.9rem;max-width:760px}
.flow{display:grid;grid-template-columns:repeat(5,1fr);gap:.85rem;
 margin-bottom:1.5rem}
@media(max-width:980px){.flow{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.flow{grid-template-columns:1fr}}
.stage{background:var(--surface);border:1px solid var(--bd);border-radius:12px;
 padding:1rem;position:relative;overflow:hidden}
.stage::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
 background:linear-gradient(90deg,var(--indigo),var(--violet))}
.stage.dim::before{background:var(--tx3)}
.s-name{font-family:var(--mono);font-size:.68rem;letter-spacing:.12em;
 text-transform:uppercase;color:var(--tx3);margin-bottom:.6rem}
.s-big{font-size:1.75rem;font-weight:800;line-height:1;margin-bottom:.3rem;
 font-family:var(--mono)}
.s-unit{font-size:.78rem;color:var(--tx2);margin-bottom:.7rem}
.s-rows{border-top:1px solid var(--bd);padding-top:.6rem;font-size:.76rem}
.s-row{display:flex;justify-content:space-between;gap:.5rem;padding:.13rem 0;
 color:var(--tx2)}
.s-row b{color:var(--tx);font-family:var(--mono);font-weight:600}
.ok{color:var(--green)} .warn{color:var(--amber)} .bad{color:var(--red)}
.muted{color:var(--tx3)}
h2{font-size:.78rem;color:var(--tx2);text-transform:uppercase;
 letter-spacing:.1em;margin:1.75rem 0 .8rem;font-weight:700}
table{width:100%;border-collapse:collapse;background:var(--surface);
 border:1px solid var(--bd);border-radius:12px;overflow:hidden;font-size:.8rem}
th{text-align:left;padding:.6rem .8rem;color:var(--tx3);font-weight:600;
 font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;
 border-bottom:1px solid var(--bd)}
td{padding:.55rem .8rem;border-bottom:1px solid rgba(31,32,48,.5);
 font-family:var(--mono);color:var(--tx2)}
tr:last-child td{border-bottom:none}
.foot{margin-top:2rem;color:var(--tx3);font-size:.76rem;font-family:var(--mono)}
.fix{background:var(--indigo);color:#fff;border:none;border-radius:7px;
 padding:.34rem .7rem;font-size:.72rem;font-weight:600;cursor:pointer;
 font-family:inherit;white-space:nowrap}
.fix:hover{background:#4f46e5}
.fix:disabled{background:var(--bd);color:var(--tx3);cursor:default}
.grant{background:var(--violet);color:#fff;border:none;border-radius:7px;
 padding:.3rem .6rem;font-size:.7rem;font-weight:600;cursor:pointer;
 font-family:inherit;white-space:nowrap;margin-right:.3rem}
.grant.off{background:#1f2030;color:var(--tx2)}
.grant:hover{filter:brightness(1.15)}
.grant:disabled{background:var(--bd);color:var(--tx3);cursor:default}
.decide-bar{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;
 margin:.2rem 0 .5rem;font-size:.74rem}
.decide-bar input,.decide-bar select{background:#0d0e16;color:var(--tx);
 border:1px solid var(--bd);border-radius:7px;padding:.3rem .5rem;
 font-family:var(--mono);font-size:.72rem}
.decide-bar input{min-width:260px}
.decide{background:var(--amber);color:#111;border:none;border-radius:7px;
 padding:.3rem .6rem;font-size:.7rem;font-weight:700;cursor:pointer;
 font-family:inherit;white-space:nowrap}
.decide:disabled{background:var(--bd);color:var(--tx3);cursor:default}
h3{font-size:.74rem;color:var(--tx2);text-transform:uppercase;
 letter-spacing:.08em;margin:1.1rem 0 .45rem;font-weight:700}
.p-awaiting_ops{color:var(--amber)} .p-awaiting_decision{color:#c4b5fd}
.p-resolved{color:var(--green)} .p-superseded{color:var(--tx3)}
.seen{font-family:var(--mono);font-size:.66rem;color:var(--tx3)}
td.t{font-family:inherit;color:var(--tx);max-width:640px}
.pill{display:inline-block;font-family:var(--mono);font-size:.66rem;
 padding:.1rem .45rem;border-radius:99px;border:1px solid var(--bd)}
.p-queued{color:var(--amber)} .p-running{color:#c4b5fd}
.p-proposed{color:var(--green)} .p-refused{color:var(--tx3)}
.p-failed{color:var(--red)}
.note{color:var(--tx3);font-size:.76rem;margin:.4rem 0 .9rem;max-width:820px}
a{color:#c4b5fd}
td.an{background:#0d0e16;color:#9ca3af;font-size:.78rem;line-height:1.55;white-space:pre-wrap}
td.an b{color:#c4b5fd}
"""


def render(d: dict) -> str:
    v = d.get("verdict") or {}
    det, rt, pr, ac, vf = (d.get(k) or {} for k in
                           ("detect", "route", "propose", "act", "verify"))

    def stage(name, big, unit, rows, known=True):
        rs = "".join(
            "<div class='s-row'><span>%s</span><b class='%s'>%s</b></div>"
            % (_esc(lbl), cls, _esc(val)) for lbl, val, cls in rows)
        return ("<div class='stage%s'><div class='s-name'>%s</div>"
                "<div class='s-big'>%s</div><div class='s-unit'>%s</div>"
                "<div class='s-rows'>%s</div></div>"
                % ("" if known else " dim", _esc(name), _esc(big),
                   _esc(unit), rs))

    landed = ac.get("landed_7d")
    landed_cls = "ok" if (landed or 0) > 0 else "warn"

    flow = "".join([
        stage("1 · Detect", _n(det.get("open_red")), "audit findings OPEN-RED", [
            ("seeded this cycle", _n(det.get("seeded")), ""),
            ("deferred to next", _n(det.get("deferred")), "muted"),
            ("registry total", _n(det.get("registry_total")), "muted"),
        ], det.get("known")),
        stage("2 · Route", _n(rt.get("active")), "honest backlog", [
            ("→ operator", _n(rt.get("operator_config")), ""),
            ("→ mcp-server", _n(rt.get("mcp_server")), ""),
            ("triaged out", _n(rt.get("terminal")), "muted"),
        ], rt.get("known")),
        stage("3 · Propose", _n(pr.get("generated_10")), "proposals · last 10 runs", [
            ("considered", _n(pr.get("considered_10")), ""),
            ("verdict", _n(pr.get("verdict")), ""),
            ("jam streak", "%s / %s" % (_n(pr.get("jam_streak")),
                                        _n(pr.get("threshold"))), "muted"),
        ], pr.get("known")),
        stage("4 · Act", _n(landed), "fixes landed · 7d", [
            ("armed", "yes" if ac.get("enabled") else "NO",
             "ok" if ac.get("enabled") else "bad"),
            ("breaker", "TRIPPED" if ac.get("breaker_tripped") else "clear",
             "bad" if ac.get("breaker_tripped") else "ok"),
            ("last merge", ("%s d ago" % _n(ac.get("last_merge_days")))
             if ac.get("last_merge_days") is not None else "never", "muted"),
        ], ac.get("known")),
        stage("5 · Verify", "%s%%" % _n(vf.get("closure_pct")), "audit closure", [
            ("of findings", _n(vf.get("registry_total")), "muted"),
            ("closes only on", "its own checker", "muted"),
            ("acks while red", "never counted", "muted"),
        ], vf.get("known")),
    ])

    runs = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td></tr>"
        % (_esc((r.get("ts") or "")[:19].replace("T", " ")),
           _esc(r.get("source") or ""), _n(r.get("considered")),
           "ok" if (r.get("generated") or 0) > 0 else "muted",
           _n(r.get("generated")))
        for r in (pr.get("runs") or []))
    if not runs:
        runs = "<tr><td colspan='4' class='muted'>no recorded runs</td></tr>"

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        "<title>Bug squasher — DC Hub</title><style>%s</style></head><body>"
        "<div class='wrap'>"
        "<div class='kicker'><span class='pulse'></span>self-heal loop</div>"
        "<h1>Bug squasher</h1>"
        "<p class='sub'>Detect → route → propose → act → verify, read live from "
        "each stage's own surface. A stage that cannot be read shows a dash, "
        "never a zero. The verdict is driven by fixes <b>landed</b>, because "
        "every other number here can look busy while nothing ships.</p>"
        "<div class='verdict v-%s'><div class='v-state'>%s</div>"
        "<div class='v-head'>%s</div><div class='v-detail'>%s</div></div>"
        "<div class='flow'>%s</div>"
        "%s%s%s%s"
        "<h2>Propose stage — recent runs</h2>"
        "<table><tr><th>when (UTC)</th><th>source</th><th>considered</th>"
        "<th>generated</th></tr>%s</table>"
        "<div class='foot'>as of %s · no-store · "
        "/api/v1/brain/squasher.json for the same data</div>"
        "%s</div></body></html>"
        % (_CSS, _esc(v.get("state", "UNKNOWN")), _esc(v.get("state", "UNKNOWN")),
           _esc(v.get("headline", "")), _esc(v.get("detail", "")),
           flow, _spec_debt_html(d), _actionable_html(d), _queue_html(d),
           _classes_html(d), runs, _esc(d.get("as_of", "")), _JS)
    )


def _classes_html(d: dict) -> str:
    """Action classes (claim loop step 2): the registry with its grant /
    breaker state, the global switch, and the inbox grouped by class.
    UNREADABLE renders as UNREADABLE — never as 'nothing granted'."""
    ac = d.get("action_classes") or {}
    if not ac.get("known"):
        return ("<h2>Action classes</h2><p class='note'>UNREADABLE — the "
                "class registry could not be read (%s). This is not 'nothing "
                "granted'.</p>" % _esc(ac.get("error") or "no reason recorded"))
    on = bool(ac.get("enabled"))
    caps = ac.get("caps") or {}
    head = (
        "<h2>Action classes — inbox findings grouped by the endpoint they "
        "name</h2>"
        "<p class='note'>Global switch <b class='%s'>%s</b> "
        "(ACTION_CLASSES_ENABLED) · caps %s per drain, %s per day (%s used "
        "in 24h) · breaker trips after %s consecutive failed runs · runs that "
        "<b>verified</b> in 7d: <b>%s</b> (counted as fixes landed). A class "
        "runs only when it is granted AND its breaker is clear AND the switch "
        "is on; every run is verified against the class's own read endpoint "
        "before its row is called resolved — an executed run that did not "
        "verify is a failure.</p>"
        % ("ok" if on else "warn", "ON" if on else "OFF (dark)",
           _n(caps.get("per_drain")), _n(caps.get("per_day")),
           _n(ac.get("day_used")), _n(caps.get("breaker_after")),
           _n(ac.get("verified_7d"))))

    def _cls_row(c):
        name = c.get("class") or ""
        tripped = bool(c.get("breaker_tripped"))
        granted = bool(c.get("granted"))
        grant_ok = bool(c.get("grant_ok"))
        btn_grant = ("<button class='grant' data-class=\"%s\" "
                     "data-granted=\"true\"%s title=\"%s\">Grant class</button>"
                     % (_esc(name), "" if grant_ok else " disabled",
                        _esc(c.get("grant_reason") or "")))
        btn_revoke = ("<button class='grant off' data-class=\"%s\" "
                      "data-granted=\"false\">Revoke</button>" % _esc(name))
        btn_clear = (("<button class='grant' data-class=\"%s\" "
                      "data-granted=\"%s\" data-clear=\"1\">Clear breaker"
                      "</button>" % (_esc(name), "true" if granted else "false"))
                     if tripped else "")
        return (
            "<tr><td class='t'>%s</td>"
            "<td><span class='pill %s'>%s</span>%s</td>"
            "<td class='%s'>%s</td>"
            "<td>%s ok · %s failed · %s consecutive</td>"
            "<td>%s</td><td class='t'>%s</td><td>%s</td><td>%s%s%s</td></tr>"
            % (_esc(name),
               "p-resolved" if granted else "p-refused",
               "GRANTED" if granted else "not granted",
               (" <span class='muted'>by %s</span>" % _esc(c.get("granted_by")))
               if granted and c.get("granted_by") else "",
               "bad" if tripped else "ok", "TRIPPED" if tripped else "clear",
               _n(c.get("runs_ok")), _n(c.get("runs_failed")),
               _n(c.get("consecutive_failed")),
               _esc((c.get("last_run_at") or "never")[:19].replace("T", " ")),
               _esc(c.get("verifier_url") or ""),
               "yes" if c.get("reversible") else "NO",
               btn_revoke if granted else btn_grant, " ", btn_clear))

    classes = ac.get("classes") or []
    table = (
        "<table><tr><th>class</th><th>grant</th><th>breaker</th><th>runs</th>"
        "<th>last run (UTC)</th><th>verifier</th><th>reversible</th><th></th>"
        "</tr>%s</table>"
        % ("".join(_cls_row(c) for c in classes)
           or "<tr><td colspan='8' class='muted'>no classes registered</td></tr>"))

    groups = ac.get("inbox_by_class") or {}
    # #65 B: one decision → N rows. Only a REGISTRY class gets the control —
    # "unclassified" is not a class, and closing it wholesale would be the
    # silent abandonment the inbox exists to end.
    registry = {c.get("class") for c in classes if c.get("class")}
    ages = (d.get("queue_ages") or {}).get("by_class") or {}
    inbox = ""
    for cls in sorted(groups, key=lambda k: (k == "unclassified", k)):
        rows = groups[cls] or []
        body = "".join(
            "<tr><td>%s</td><td><span class='pill p-%s'>%s</span></td>"
            "<td class='t'>%s</td><td>%s</td></tr>"
            % (_n(r.get("id")), _esc(r.get("status") or ""),
               _esc(r.get("status") or ""),
               _esc(((r.get("action_method") or "") + " "
                     + (r.get("action_url") or r.get("finding_key") or "")).strip()[:160]),
               _esc((r.get("finished_at") or "")[:19].replace("T", " ")))
            for r in rows)
        age = (ages.get(cls) or {}).get("oldest_age_hours")
        age_txt = (" · oldest %s h" % _n(age)) if age is not None else ""
        decide = ""
        if cls in registry:
            decide = (
                "<div class='decide-bar' data-class=\"%s\">"
                "<input class='decide-text' placeholder=\"decision, e.g. "
                "granted-class handles it\">"
                "<select class='decide-outcome'>"
                "<option value='done'>done — close as resolved</option>"
                "<option value='rejected'>rejected — close as refused</option>"
                "</select>"
                "<button class='decide' data-class=\"%s\">Decide class "
                "(%d rows)</button>"
                "<span class='muted'>one decision closes every open row of "
                "this class — it runs NOTHING, and a re-observation reopens "
                "the finding</span></div>"
                % (_esc(cls), _esc(cls), len(rows)))
        inbox += ("<h3>%s · %d waiting%s</h3>%s<table><tr><th>id</th><th>status"
                  "</th><th>action / finding</th><th>since (UTC)</th></tr>%s"
                  "</table>" % (_esc(cls), len(rows), age_txt, decide, body))
    if not inbox:
        inbox = "<p class='note'>The inbox is empty — nothing is waiting.</p>"
    return head + table + _graduation_html(ac) + inbox


def _graduation_html(ac: dict) -> str:
    """#65 B: the track record and the proposal, per class. UNREADABLE renders
    as UNREADABLE — never as 'nothing eligible'. The grant stays the button in
    the registry table; this section only shows what the code rule says."""
    g = ac.get("graduation") or {}
    if not g.get("known"):
        return ("<h3>Graduation — track record → proposal</h3><p class='note'>"
                "UNREADABLE — the graduation report could not be computed (%s). "
                "This is not 'nothing eligible'.</p>"
                % _esc(g.get("error") or "no reason recorded"))
    rows = ""
    for e in g.get("classes") or []:
        req = e.get("track_record_required") or {}
        prop = e.get("proposal") or {}
        if e.get("eligible_for_grant"):
            elig_cls, elig = "ok", "ELIGIBLE — a human decides"
        else:
            elig_cls = "muted"
            elig = "; ".join(e.get("not_eligible_because") or []) or "—"
        if prop:
            prop_txt = "#%s %s" % (_n(prop.get("id")), _esc(prop.get("status") or ""))
        elif e.get("eligible_for_grant"):
            prop_txt = "not filed yet — POST /graduation (or the #65 tick) files it"
        else:
            prop_txt = "—"
        rows += ("<tr><td class='t'>%s<br><span class='seen'>%s</span></td>"
                 "<td><span class='pill %s'>%s</span></td>"
                 "<td>%s / %s <span class='muted'>(%s reads)</span></td>"
                 "<td>%s ok · %s failed</td><td class='%s'>%s</td>"
                 "<td class='%s'>%s</td><td>%s</td></tr>"
                 % (_esc(e.get("class") or ""),
                    _esc((e.get("candidate_reason") or "")[:180]),
                    "p-resolved" if e.get("granted") else "p-refused",
                    "GRANTED" if e.get("granted") else "candidate",
                    _n(e.get("clean_dry_runs_7d")), _n(req.get("clean_dry_runs")),
                    _n(e.get("dry_run_reads_7d")),
                    _n(e.get("runs_ok_7d")), _n(e.get("runs_failed_7d")),
                    "bad" if e.get("breaker_tripped") else "ok",
                    "TRIPPED" if e.get("breaker_tripped") else "clear",
                    elig_cls, _esc(elig), prop_txt))
    return (
        "<h3>Graduation — track record → proposal; never an automatic grant</h3>"
        "<p class='note'>A candidate earns its record from drain probes "
        "(verifier read → the endpoint's dry call → verifier read, ledgered). "
        "When the code rule passes — reversible · verifier · bound params · "
        "≥N clean dry runs · 0 consecutive failures — ONE inbox row asks you "
        "to grant it. The grant itself stays the button above.</p>"
        "<table><tr><th>class</th><th>state</th>"
        "<th>clean dry runs 7d / required</th><th>runs 7d</th><th>breaker</th>"
        "<th>eligible?</th><th>proposal row</th></tr>%s</table>"
        % (rows or "<tr><td colspan='7' class='muted'>no classes</td></tr>"))


def _spec_debt_html(d: dict) -> str:
    """The obligation book (Phase 0). UNMEASURED renders as UNMEASURED — an
    unreadable corpus must never render as 'no debt'."""
    sd = d.get("spec_debt") or {}
    if not sd.get("known"):
        return ("<h2>Spec debt</h2><p class='note'>UNMEASURED — the landed-"
                "spec corpus could not be read (%s). This is not zero debt."
                "</p>" % _esc(sd.get("reason") or "no reason recorded"))
    return (
        "<h2>Spec debt — merged specs whose human checklist was never "
        "completed</h2>"
        "<p class='note'><b class='%s'>%s open obligation(s)</b> · "
        "%s closed · %s UNKNOWN (no checklist — not closed) · %s docs "
        "scanned. Oldest open: %s (%s days). Basis + full list: "
        "<a href='/api/v1/brain/spec-debt'>/api/v1/brain/spec-debt</a>. "
        "Specs auto-merge in minutes by design; this book is how the "
        "obligation survives the merge.</p>"
        % ("bad" if (sd.get("open") or 0) > 0 else "ok",
           _n(sd.get("open")), _n(sd.get("closed")), _n(sd.get("unknown")),
           _n(sd.get("total_docs")), _esc(sd.get("oldest_doc") or "—"),
           _n(sd.get("oldest_age_days"))))


def _actionable_html(d: dict) -> str:
    items = d.get("actionable") or []
    if not items:
        return ("<h2>Submit a fix</h2><p class='note'>No actionable findings "
                "in the honest backlog right now — or the route stage could "
                "not be read (check the Route card above).</p>")
    rows = "".join(
        "<tr><td class='t'>%s</td><td>%s</td><td>"
        "<button class='fix' data-key=\"%s\" data-title=\"%s\">"
        "Queue fix</button></td></tr>"
        % (_esc(it.get("title") or it.get("key")), _esc(it.get("source") or ""),
           _esc(it.get("key") or ""), _esc((it.get("title") or "")[:200]))
        for it in items)
    return (
        "<h2>Submit a fix</h2>"
        "<p class='note'>Pick a finding and put it at the head of the queue "
        "instead of waiting for the rotation. The button only <b>submits</b> — "
        "the investigate → propose chain runs off-request (it takes ~a minute "
        "and would be killed by the 15s edge timeout if it ran on this click), "
        "and the outcome lands in the queue below. <b>This lane refuses more "
        "often than it succeeds, on purpose</b>: most findings here are "
        "config, data, or a judgement call rather than a single-string fix. "
        "It never merges — every success is a PR for you to review.</p>"
        "<table><tr><th>finding</th><th>source</th><th></th></tr>%s</table>"
        % rows)


def _queue_html(d: dict) -> str:
    q = d.get("queue") or []
    if not q:
        return ""
    # ★ Every row is a HISTORICAL record — without its time, a terminal
    # "refused: investigator disabled" from before the flag was armed is
    # indistinguishable from a live refusal, and the board reads as "the
    # investigator is still off" (ten such rows did exactly that, 2026-08-08).
    def _row(r):
        # ★ The ANALYSIS is this lane's real product. Until 2026-08-09 a ~48s
        #   investigation ran on every click and was thrown away, leaving only
        #   a one-line refusal — the same "investigation arriving and being
        #   discarded" hole the QA dashboard closed in #2231. Render it.
        extra = ""
        if r.get("analysis"):
            conf = r.get("confidence")
            head = ("analysis" if not isinstance(conf, (int, float))
                    else "analysis · confidence %.2f" % conf)
            extra = ("<tr><td colspan='5' class='an'><b>%s</b><br>%s%s</td></tr>"
                     % (_esc(head), _esc(str(r["analysis"])[:1400]),
                        ("<br><br><b>decision:</b> "
                         + _esc(str(r["decision"])[:600]))
                        if r.get("decision") else ""))
        # ★ 2026-08-22: a re-filed finding refreshes its open row instead of
        #   inserting another; show the count, or a re-file looks lost.
        n = r.get("seen_count")
        seen = ((" <span class='seen' title='seen %d times; the open row was "
                 "refreshed, not duplicated'>\u00d7%d</span>" % (n, n))
                if isinstance(n, int) and not isinstance(n, bool) and n > 1
                else "")
        return ("<tr><td class='t'>%s</td><td><span class='pill p-%s'>%s</span>"
                "</td><td class='t'>%s</td><td>%s</td><td>%s</td></tr>%s"
                % (_esc((r.get("title") or r.get("finding_key") or "")[:160])
                   + seen,
                   _esc(r.get("status") or ""), _esc(r.get("status") or ""),
                   _esc((r.get("reason") or "")[:240]),
                   _esc(((r.get("finished_at") or r.get("requested_at") or "")[:19])
                        .replace("T", " ")),
                   ("<a href='%s' target='_blank' rel='noopener'>PR</a>"
                    % _esc(r["pr_url"])) if r.get("pr_url") else "", extra))
    rows = "".join(_row(r) for r in q)
    return ("<h2>Your queue</h2><table><tr><th>finding</th><th>status</th>"
            "<th>reason</th><th>when (UTC)</th><th></th></tr>%s</table>" % rows)


# The page carries ?admin_key= onto its POSTs — same pattern as the brain and
# QA dashboards. No new auth scheme.
_JS = """<script>
const KEY = new URLSearchParams(location.search).get('admin_key') || '';
document.addEventListener('click', async (e) => {
  const b = e.target.closest('.fix');
  if (!b) return;
  b.disabled = true; b.textContent = 'submitting…';
  try {
    const r = await fetch('/api/v1/brain/squasher/queue?admin_key='
                          + encodeURIComponent(KEY), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: b.dataset.key, title: b.dataset.title,
                            source: 'operator'}),
    });
    const d = await r.json();
    b.textContent = d.ok ? (d.already ? ('already open'
                                         + (d.seen_count ? ' \u00d7' + d.seen_count : ''))
                                      : 'queued ✓')
                         : (d.reason || d.error || 'failed');
    if (d.ok) setTimeout(() => location.reload(), 1200);
  } catch (err) { b.textContent = 'error'; b.disabled = false; }
});
document.addEventListener('click', async (e) => {
  const g = e.target.closest('.grant');
  if (!g) return;
  g.disabled = true; g.textContent = 'saving…';
  try {
    const r = await fetch('/api/v1/brain/squasher/grant?admin_key='
                          + encodeURIComponent(KEY), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({class: g.dataset.class,
                            granted: g.dataset.granted === 'true',
                            clear_breaker: g.dataset.clear === '1',
                            by: 'portal'}),
    });
    const d = await r.json();
    g.textContent = d.ok ? 'saved ✓' : (d.error || 'refused');
    if (d.ok) setTimeout(() => location.reload(), 900);
  } catch (err) { g.textContent = 'error'; g.disabled = false; }
});
document.addEventListener('click', async (e) => {
  const b = e.target.closest('.decide');
  if (!b) return;
  const bar = b.closest('.decide-bar');
  const decision = ((bar.querySelector('.decide-text') || {}).value || '').trim();
  const outcome = (bar.querySelector('.decide-outcome') || {}).value || 'done';
  if (!decision) { b.textContent = 'type the decision first'; return; }
  b.disabled = true; b.textContent = 'deciding…';
  try {
    const r = await fetch('/api/v1/brain/squasher/resolve-class?admin_key='
                          + encodeURIComponent(KEY), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({class: b.dataset.class, decision: decision,
                            note: decision, outcome: outcome, by: 'portal'}),
    });
    const d = await r.json();
    b.textContent = d.ok ? ('decided ' + d.count + ' row(s) ✓')
                         : (d.error || 'refused');
    if (d.ok) setTimeout(() => location.reload(), 1200); else b.disabled = false;
  } catch (err) { b.textContent = 'error'; b.disabled = false; }
});
</script>"""


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


@squasher_portal_bp.get("/api/v1/brain/squasher")
def squasher_page():
    if _disabled():
        return _no_store(Response("<h1>Squasher portal disabled</h1>",
                                  status=503, mimetype="text/html"))
    if not _admin_ok():
        return _no_store(Response("<h1>401</h1><p>admin key required</p>",
                                  status=401, mimetype="text/html"))
    try:
        html = render(collect())
    except Exception as e:  # noqa: BLE001
        logger.warning("squasher portal render failed: %s", e)
        html = ("<h1>Squasher portal</h1><p>Render failed: %s</p>"
                % _esc(str(e)[:200]))
    return _no_store(Response(html, mimetype="text/html"))


@squasher_portal_bp.get("/api/v1/brain/squasher.json")
def squasher_json():
    if _disabled():
        return _no_store(jsonify(ok=False, error="disabled")), 503
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    try:
        return _no_store(jsonify({"ok": True, **collect()}))
    except Exception as e:  # noqa: BLE001
        return _no_store(jsonify(ok=False, error=str(e)[:200])), 200
