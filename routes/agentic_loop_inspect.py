"""routes/agentic_loop_inspect.py — Agentic Loop master shell (#65), part D:
the OWNER INSPECTION dashboard (2026-08-22).

Owner ask (2026-08-22): "we should make a dashboard as well to inspect items."

WHAT THIS IS
  One admin page that lets the owner READ the whole agentic loop in one place
  and take the few decisions the loop hands to a human — without learning the
  nine JSON endpoints behind it. It is a UI over EXISTING admin APIs: no
  business logic lives here, no SQL, no model calls, no egress. Every action
  button is a thin forward to an endpoint that already exists and already
  carries its own gate, validation and audit trail.

SURFACES
  GET  /admin/agentic-loop/inspect                      the page (HTML)
  GET  /api/v1/brain/agentic-loop/inspect               index: tabs, reads,
                                                        actions, registered
                                                        vs expected-later
  GET  /api/v1/brain/agentic-loop/inspect/metrics       the metric strip (JSON)
  GET  /api/v1/brain/agentic-loop/inspect/tab/<name>    one tab, server-rendered
  POST /api/v1/brain/agentic-loop/inspect/act/<name>    one action, forwarded
Kill   AGENTIC_LOOP_SHELL_DISABLE=1 -> 404 on all five. Never 5xx: the CF
       worker reads any 5xx from Railway as a dead origin and fails the site
       over to stale Render (tests/test_shell_killswitch_never_5xx.py).
Gate   like /brain (routes/brain_v2_public.py _pub_admin_ok): X-Admin-Key /
       X-Internal-Key / ?admin_key= / ?key= once -> `dchub_admin` cookie ->
       bookmarkable. No key = 403.

★ WHY THE PAGE TALKS TO ITSELF AND NOT TO THE ENDPOINTS DIRECTLY
  /brain sets the `dchub_admin` cookie httponly — page JavaScript cannot read
  it, which is the point of httponly. The endpoints this page drives read
  X-Admin-Key / X-Internal-Key / ?admin_key= (squasher_* also a DIFFERENT
  cookie, dchub_innov_key); none reads dchub_admin. So a bookmarked,
  cookie-only visit holds no credential the browser could attach to a direct
  call. The helpers under /api/v1/brain/agentic-loop/inspect/* are gated on
  the SAME cookie / key / headers as the page and forward each call
  in-process (the loopback squasher_portal._get already uses) with the
  server's own keys. The key never appears in the page, in a URL or in a log
  line, and the READS / ACTIONS allow-lists below are the only things that
  can be called — a caller-supplied path is never forwarded.

★ /api/v1/brain/ is the prefix with Cloudflare bypass rule 6407517b; a new
  /api/v1/* prefix launches edge-cached (squasher_portal's note). /admin/*
  HTML reaches the origin (measured 2026-08-22 from the outside:
  /admin/stability-shell -> 401, /admin/brain-autonomy -> 403).

★ TRUST BOUNDARY. Analyses, decisions, claim statements, notes and class
  names are MODEL TEXT. Every value rendered here passes through _h() on the
  server; the page inserts server fragments and sets numbers via textContent.
  Nothing from JSON is ever turned into markup in the browser.

★ NEVER A LIVE DRAIN. The only drain this page can request is forced to
  ?dry_run=1 server-side, whatever the caller sent. The page carries no
  request that executes a class action.

★ UNAVAILABLE IS NOT AN ERROR. Parts A (the shell), B (resolve-class,
  graduation, queue ages) and C (learn/recall) may land after
  this page. A 404 from one of those renders "unavailable (not deployed
  yet)"; EXPECTED_LATER names them, and tests/test_agentic_loop_inspect.py
  proves every OTHER target is a registered route on the booted app.

★ PLATFORM UPDATES HAVE NO APPROVE ENDPOINT — BY DESIGN. A card reaches
  /whats-new only when its entry in data/platform_updates.json carries the
  literal status "published", which happens only by the owner merging the PR
  that sets it (routes/platform_updates.py: "there is no write endpoint").
  The Platform tab therefore shows the exact owner step instead of a button.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from html import escape as _escape

from flask import Blueprint, Response, current_app, jsonify, request

logger = logging.getLogger(__name__)
agentic_loop_inspect_bp = Blueprint("agentic_loop_inspect", __name__)

PAGE_PATH = "/admin/agentic-loop/inspect"
API_PREFIX = "/api/v1/brain/agentic-loop/inspect"
_UA = "dchub-inspect/1.0"
_COOKIE = "dchub_admin"
_ANALYSIS_CHARS = 1400          # the portal's own cap for an analysis block
_JSON_CHARS = 4000

# ── reads: name -> (path, query params the page may pass through) ──────────
READS = {
    "ops_claims":   ("/api/v1/ops/claims", ("limit",)),
    "deadman":      ("/api/v1/ops/deadman", ()),
    "claims":       ("/api/v1/brain/claims", ("limit", "kind", "outcome")),
    "convergence":  ("/api/v1/brain/squasher/convergence", ("days",)),
    "classes":      ("/api/v1/brain/squasher/classes", ("dry_run",)),
    "inbox":        ("/api/v1/brain/squasher/inbox", ()),
    "whats_new":    ("/api/v1/whats-new", ()),
    "findings":     ("/api/v1/brain/findings/db-status", ()),
    "enhancements": ("/api/v1/brain/enhancements", ("limit",)),
    # part B — graduation_report() / queue_ages() surfaces
    "graduation":   ("/api/v1/brain/squasher/graduation", ()),
    "queue_ages":   ("/api/v1/brain/squasher/queue-ages", ()),
    # part C — the learn station
    "learn_recall": ("/api/v1/brain/learn/recall", ("q", "k")),
    # part A — the shell itself
    "shell":        ("/api/v1/brain/agentic-loop", ()),
}

# ── actions: each is ONE existing endpoint. `query` is FORCED (the caller's
#    query string is never forwarded); `body` is the allow-list of body keys;
#    `required` must be present and non-empty; `stamp` is added server-side.
ACTIONS = {
    "claims_verify": {
        "method": "POST", "path": "/api/v1/brain/claims/verify",
        "query": {}, "body": (), "required": ()},
    "claims_retract": {
        "method": "POST", "path": "/api/v1/brain/claims/retract",
        "query": {}, "body": ("id", "reason", "superseded_by"),
        "required": ("id", "reason")},
    "class_grant": {
        "method": "POST", "path": "/api/v1/brain/squasher/grant",
        "query": {}, "body": ("class", "granted", "clear_breaker"),
        "required": ("class", "granted"), "stamp": {"by": "inspect-dashboard"}},
    "class_drain_dry": {
        "method": "POST", "path": "/api/v1/brain/squasher/drain",
        "query": {"dry_run": "1"}, "body": (), "required": ()},
    "inbox_resolve": {
        "method": "POST", "path": "/api/v1/brain/squasher/resolve",
        "query": {}, "body": ("id", "outcome", "note"),
        "required": ("id", "outcome")},
    "resolve_class": {
        "method": "POST", "path": "/api/v1/brain/squasher/resolve-class",
        "query": {}, "body": ("class", "decision", "note"),
        "required": ("class", "decision")},
}

# Targets that are NOT on origin/main at the time this page ships. A 404 from
# one of these is rendered as "unavailable (not deployed yet)". The test pins
# this set both ways: everything else must be registered, and nothing here
# may be — once a part lands, its path moves out of this set.
EXPECTED_LATER = frozenset({
    "/api/v1/brain/squasher/graduation",      # B
    "/api/v1/brain/squasher/queue-ages",      # B
    "/api/v1/brain/squasher/resolve-class",   # B
    "/api/v1/brain/learn/recall",             # C
    "/api/v1/brain/agentic-loop",             # A
})

_PRODUCT_DETECTORS = ("measurement_definition_changed", "stored_slug_404",
                      "funnel_step_collapse")
# brain_findings.detector for all three: they are `issue` values on the
# radar's own rows, so by_detector (keyed by module) never lists them.
_RADAR_MODULE = "consistency_radar"
_REJECTED_PROPOSAL_STATUSES = ("rejected", "duplicate")


# ── gate ────────────────────────────────────────────────────────────────────

def _disabled() -> bool:
    return (os.environ.get("AGENTIC_LOOP_SHELL_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    """Mirror of /brain's _pub_admin_ok — header, ?admin_key=, ?key=, or the
    dchub_admin cookie. Keys are read at request time, never snapshotted
    (tests/test_admin_gate_fail_closed.py's class)."""
    keys = set()
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v:
            keys.add(v)
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key")
            or request.args.get("key")
            or request.cookies.get(_COOKIE) or "").strip()
    return bool(sent) and sent in keys


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, private"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


def _gate():
    """-> an early response, or None. Kill switch answers 404, never 5xx."""
    if _disabled():
        return _no_store(jsonify(ok=False, error="not found",
                                 hint="AGENTIC_LOOP_SHELL_DISABLE=1")), 404
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="forbidden",
                                 hint="X-Admin-Key / ?key=")), 403
    return None


_FORBIDDEN_HTML = (
    "<!doctype html><meta charset=utf-8><title>DC Hub · internal</title>"
    "<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
    "background:#0a0a0f;color:#a1a1aa;display:flex;align-items:center;"
    "justify-content:center;height:90vh;text-align:center'>"
    "<div><h2 style='color:#fafafa;font-weight:300;letter-spacing:-.02em'>"
    "Internal console</h2><p>The agentic-loop inspection dashboard is "
    "admin-only (X-Admin-Key or ?key= once).</p></div>")


# ── loopback ────────────────────────────────────────────────────────────────

def _self_auth_headers() -> dict:
    """Credentials for a LOOPBACK call. A self-call is anonymous unless you say
    otherwise (squasher_portal learned this live: four of five stages 401'd).
    The page is already admin-gated, so this never widens access — it lets
    the page read what its caller was already authorised to see."""
    h = {"User-Agent": _UA}
    admin = os.environ.get("DCHUB_ADMIN_KEY")
    internal = os.environ.get("DCHUB_INTERNAL_KEY") or os.environ.get("INTERNAL_KEY")
    if admin:
        h["X-Admin-Key"] = admin
    if internal:
        h["X-Internal-Key"] = internal
    return h


def _registered(path: str) -> bool:
    """Is `path` a rule on THIS app? The honest availability signal: main.py
    answers an unknown path with a JSON 404 ({"error": "404 Not Found"}),
    so a 404 body cannot distinguish 'not deployed yet' from a target's own
    404 (kill switch, 'no such claim'). The url_map can."""
    try:
        return any(str(r.rule) == path for r in current_app.url_map.iter_rules())
    except Exception:  # noqa: BLE001
        return False


def _forward(method: str, path: str, query=None, body=None) -> dict:
    """Call ONE existing endpoint in-process.

    -> {ok, status, data, unavailable, error, path}. `unavailable` means the
    path is not registered on this app (A/B/C not deployed yet) — nothing is
    called. A 404 from a REGISTERED target is that target's own answer (kill
    switch, "no such claim") and is reported as its error. Never raises."""
    out = {"ok": False, "status": 0, "data": None, "unavailable": False,
           "error": None, "path": path}
    try:
        if not _registered(path):
            out.update(status=404, unavailable=True)
            return out
        client = current_app.test_client()
        kw = {"headers": _self_auth_headers(), "query_string": dict(query or {})}
        if body is not None:
            kw["json"] = body
        r = client.open(path, method=method, **kw)
        data = r.get_json(silent=True)
        out.update(status=r.status_code, data=data,
                   ok=(200 <= r.status_code < 300))
        if r.status_code == 404:
            err = data.get("error") if isinstance(data, dict) else None
            out["error"] = str(err or "not found")[:200]
        elif r.status_code in (401, 403):
            out["error"] = ("loopback refused (HTTP %d) — is DCHUB_ADMIN_KEY / "
                            "DCHUB_INTERNAL_KEY set on this process?"
                            % r.status_code)
        elif r.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            out["error"] = (str(err)[:200] if err else "HTTP %d" % r.status_code)
    except Exception as e:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    return out


# ── rendering primitives ────────────────────────────────────────────────────

def _h(v) -> str:
    """HTML-escape for text AND attribute context. None -> ''. This is the
    ONLY way a value from an upstream payload reaches the page."""
    if v is None:
        return ""
    return _escape(str(v), quote=True)


def _n(v) -> str:
    """A number or '—'. Zero renders as '0' — measured zero is never hidden."""
    if v is None:
        return "—"
    if isinstance(v, float):
        return "%.3f" % v
    return _h(v)


def _age_hours(iso):
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return None


def _age(iso) -> str:
    h = _age_hours(iso)
    if h is None:
        return "—"
    if h < 48:
        return "%.1fh" % h
    return "%.1fd" % (h / 24.0)


def _when(iso) -> str:
    return _h(str(iso or "")[:19].replace("T", " ")) or "—"


def _js(obj, cap: int = _JSON_CHARS) -> str:
    """A JSON blob as escaped <pre> text."""
    try:
        txt = json.dumps(obj, indent=1, default=str, sort_keys=True)
    except Exception:  # noqa: BLE001
        txt = str(obj)
    if len(txt) > cap:
        txt = txt[:cap] + "\n… (%d more chars)" % (len(txt) - cap)
    return "<pre class='il-pre'>%s</pre>" % _h(txt)


def _tag(text, kind: str = "") -> str:
    cls = {"ok": " il-ok", "warn": " il-warn", "err": " il-err"}.get(kind, "")
    return "<span class='il-tag%s'>%s</span>" % (cls, _h(text))


def _kv(pairs) -> str:
    cells = "".join("<div><dt>%s</dt><dd>%s</dd></div>" % (_h(k), v)
                    for k, v in pairs)
    return "<dl class='il-kv'>%s</dl>" % cells


def _tbl(headers, rows) -> str:
    th = "".join("<th>%s</th>" % _h(x) for x in headers)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r)
                   for r in rows)
    return ("<div class='il-scroll'><table class='il-tbl'><thead><tr>%s</tr>"
            "</thead><tbody>%s</tbody></table></div>" % (th, body))


def _btn(act: str, label: str, attrs: dict | None = None) -> str:
    """An action button. Every attribute value is escaped; the action name is
    validated against ACTIONS at click time by the server, not trusted here."""
    extra = "".join(" data-%s=\"%s\"" % (_h(k), _h(v))
                    for k, v in (attrs or {}).items())
    return ("<button type='button' class='il-act' data-act=\"%s\"%s>%s</button>"
            % (_h(act), extra, _h(label)))


def _unavail(res: dict, label: str) -> str:
    if res.get("unavailable"):
        return ("<p class='il-unavail'>%s: unavailable (not deployed yet) — "
                "<span class='il-dim'>%s</span></p>"
                % (_h(label), _h(res.get("path"))))
    if res.get("error"):
        return ("<p class='il-unavail'>%s: unreadable — %s "
                "<span class='il-dim'>(%s, HTTP %s)</span></p>"
                % (_h(label), _h(res.get("error")), _h(res.get("path")),
                   _n(res.get("status"))))
    return ("<p class='il-unavail'>%s: unreadable — HTTP %s from %s</p>"
            % (_h(label), _n(res.get("status")), _h(res.get("path"))))


def _readable(res: dict) -> bool:
    return bool(res.get("ok")) and isinstance(res.get("data"), dict)


def _clean(v, cap: int = 120):
    if v is None:
        return None
    s = str(v).strip()
    return s[:cap] if s else None


# ── tabs ────────────────────────────────────────────────────────────────────

def _claim_row(r: dict) -> str:
    oc = r.get("outcome")
    kind = {"confirmed": "ok", "refuted": "err", "retracted": "warn",
            "unobserved": "warn"}.get(oc or "", "")
    ev = r.get("outcome_evidence")
    actual = ev.get("actual") if isinstance(ev, dict) else None
    head = ("<summary><b>#%s</b> %s %s <span class='il-mute'>%s</span> "
            "<span class='il-dim'>shipped %s · due %s</span></summary>"
            % (_h(r.get("id")), _tag(r.get("kind") or "?"),
               _tag(oc or "open", kind), _h(r.get("subject")),
               _when(r.get("shipped_at")), _when(r.get("due_at"))))
    acts = ""
    if oc != "retracted":
        acts = _btn("claims_retract", "retract (reason required)",
                    {"id": r.get("id"), "need": "reason",
                     "confirm": "Retract claim #%s — reason:" % r.get("id")})
    body = _kv([
        ("statement", "<pre class='il-pre'>%s</pre>" % _h(r.get("statement"))),
        ("expected", "%s <span class='il-dim'>(%s)</span>"
         % (_h(r.get("expected_value")), _h(r.get("expected_metric")))),
        ("actual", _h(actual) if actual is not None else "— (not judged yet)"),
        ("outcome", "%s · %s" % (_h(oc or "open"), _when(r.get("outcome_at")))),
        ("outcome_evidence", _js(ev) if ev else "—"),
        ("regime", _js(r.get("regime")) if r.get("regime") else "—"),
        ("surfaces", _h(", ".join(str(s) for s in (r.get("surfaces") or []))) or "—"),
        ("horizon", "%s h · registered %s" % (_n(r.get("horizon_hours")),
                                              _when(r.get("registered_at")))),
        ("superseded_by", _n(r.get("superseded_by"))),
    ])
    return "<details class='il-row'>%s%s<div class='il-acts'>%s</div></details>" % (
        head, body, acts)


def _tab_claims(args: dict) -> str:
    q = {"limit": "200"}
    for k in ("kind", "outcome"):
        v = _clean(args.get(k))
        if v:
            q[k] = v
    res = _forward("GET", READS["claims"][0], q)
    bar = "<div class='il-acts'>%s <span class='il-dim'>POST %s — judges every "
    bar = bar % (_btn("claims_verify", "verify now (judge due claims)",
                      {"confirm": "Judge every DUE claim now and stamp outcomes?"}),
                 _h(ACTIONS["claims_verify"]["path"]))
    bar += "due claim against its pre-registered expectation</span></div>"
    if not _readable(res):
        return bar + _unavail(res, "claims ledger")
    rows = res["data"].get("claims") or []
    subj = (_clean(args.get("subject")) or "").lower()
    if subj:
        rows = [r for r in rows if subj in str(r.get("subject") or "").lower()]
    parts = [bar, "<p class='il-mute'>%d claim(s) shown · ledger count %s · "
             "filters: kind=%s outcome=%s subject=%s</p>"
             % (len(rows), _n(res["data"].get("count")), _h(q.get("kind") or "*"),
                _h(q.get("outcome") or "*"), _h(subj or "*"))]
    parts.extend(_claim_row(r) for r in rows)
    if not rows:
        parts.append("<p class='il-note'>0 claims match — measured, not hidden.</p>")
    return "".join(parts)


def _class_row(c: dict, open_by_class: dict) -> str:
    cls = c.get("class") or "?"
    granted = bool(c.get("granted"))
    tripped = bool(c.get("breaker_tripped"))
    acts = []
    if granted:
        acts.append(_btn("class_grant", "revoke", {
            "class": cls, "granted": "false",
            "confirm": "Revoke class %s? (it stops executing on drains)" % cls}))
    else:
        acts.append(_btn("class_grant", "grant", {
            "class": cls, "granted": "true",
            "confirm": "Grant class %s? Granted classes EXECUTE on the next real drain." % cls}))
    if tripped:
        acts.append(_btn("class_grant", "clear breaker (keep grant state)", {
            "class": cls, "granted": "true" if granted else "false",
            "clear_breaker": "1",
            "confirm": "Clear the breaker on %s?" % cls}))
    open_rows = open_by_class.get(cls)
    n_open = len(open_rows) if isinstance(open_rows, list) else None
    return "".join([
        "<details class='il-row' open><summary><b>%s</b> %s %s %s "
        "<span class='il-dim'>runs ok %s · failed %s · consecutive %s · "
        "last %s · open inbox rows %s</span></summary>"
        % (_h(cls),
           _tag("granted" if granted else "not granted", "ok" if granted else "warn"),
           _tag("breaker TRIPPED" if tripped else "breaker ok", "err" if tripped else ""),
           _tag("grant test: %s" % ("passes" if c.get("grant_ok") else "fails"),
                "ok" if c.get("grant_ok") else "warn"),
           _n(c.get("runs_ok")), _n(c.get("runs_failed")),
           _n(c.get("consecutive_failed")), _when(c.get("last_run_at")),
           _n(n_open)),
        _kv([
            ("granted_by / at", "%s · %s" % (_h(c.get("granted_by")) or "—",
                                             _when(c.get("granted_at")))),
            ("grant_reason", _h(c.get("grant_reason")) or "—"),
            ("reversible", _h(c.get("reversible"))),
            ("verifier_url", _h(c.get("verifier_url")) or "—"),
            ("bound_params", _js(c.get("bound_params")) if c.get("bound_params") else "—"),
            ("notes", _h(c.get("notes")) or "—"),
        ]),
        "<div class='il-acts'>%s</div></details>" % " ".join(acts),
    ])


def _tab_classes(args: dict) -> str:
    res = _forward("GET", READS["classes"][0], {"dry_run": "1"})
    grad = _forward("GET", READS["graduation"][0])
    bar = ("<div class='il-acts'>%s <span class='il-dim'>POST %s?dry_run=1 — "
           "plan only; this page carries no live drain</span></div>"
           % (_btn("class_drain_dry", "dry-run drain (plan only)"),
              _h(ACTIONS["class_drain_dry"]["path"])))
    parts = [bar]
    if not _readable(res):
        parts.append(_unavail(res, "action classes"))
    else:
        d = res["data"]
        if not d.get("known", True):
            parts.append("<p class='il-unavail'>registry unreadable — %s</p>"
                         % _h(d.get("error")))
        caps = d.get("caps") or {}
        parts.append(_kv([
            ("step enabled (ACTION_CLASSES_ENABLED)", _tag(
                "on" if d.get("enabled") else "off",
                "ok" if d.get("enabled") else "warn")),
            ("day used / cap", "%s / %s" % (_n(d.get("day_used")), _n(caps.get("per_day")))),
            ("per-drain cap · breaker after", "%s · %s"
             % (_n(caps.get("per_drain")), _n(caps.get("breaker_after")))),
            ("verified runs 7d", _n(d.get("verified_7d"))),
        ]))
        classes = d.get("classes") or []
        open_by = d.get("inbox_by_class") or {}
        parts.append("<h3>%d class(es) in the registry</h3>" % len(classes))
        parts.extend(_class_row(c, open_by) for c in classes)
        if not classes:
            parts.append("<p class='il-note'>0 classes — measured, not hidden.</p>")
        plan = d.get("plan")
        if isinstance(plan, dict):
            cands = plan.get("candidates") or []
            results = plan.get("results") or []
            parts.append("<h3>dry-run plan (what a drain WOULD run)</h3>")
            parts.append(_kv([
                ("ran", _n(plan.get("ran"))),
                ("day used / cap", "%s / %s" % (_n(plan.get("day_used")), _n(plan.get("day_cap")))),
                ("note", _h(plan.get("note") or plan.get("error")) or "—"),
            ]))
            parts.append(_tbl(
                ["queue id", "class", "action_url", "skip reason"],
                [[_n(c.get("queue_id")), _h(c.get("class")) or "—",
                  _h(c.get("action_url")) or "—",
                  _h(c.get("skip")) or "<span class='il-ok'>would run</span>"]
                 for c in cands]))
            if not cands:
                parts.append("<p class='il-note'>0 candidates in the plan.</p>")
            if results:
                parts.append("<h4>dry-run results (pre/post verifier reads)</h4>")
                parts.extend(_js(r) for r in results)
        parts.append("<p class='il-dim'>run ledger (brain_action_class_runs): no "
                     "list endpoint exists on main — counters above are the "
                     "ledger's own sums; dry-run results carry the per-candidate "
                     "pre/post verifier reads.</p>")
    parts.append("<h3>graduation report (part B)</h3>")
    if not _readable(grad):
        parts.append(_unavail(grad, "graduation report"))
    else:
        parts.append(_js(grad["data"]))
    return "".join(parts)


def _inbox_row(r: dict) -> str:
    rid = r.get("id")
    st = r.get("status") or "?"
    acts = [
        _btn("inbox_resolve", "resolve: done", {
            "id": rid, "outcome": "done",
            "confirm": "Mark inbox row #%s DONE (the action was run / decided)?" % rid}),
        _btn("inbox_resolve", "resolve: rejected (note required)", {
            "id": rid, "outcome": "rejected", "need": "note",
            "confirm": "Reject row #%s — why?" % rid}),
    ]
    conf = r.get("confidence")
    analysis = str(r.get("analysis") or "")
    if len(analysis) > _ANALYSIS_CHARS:
        analysis = analysis[:_ANALYSIS_CHARS] + " …"
    seen = r.get("seen_count")
    return "".join([
        "<details class='il-row'><summary><b>#%s</b> %s %s "
        "<span class='il-dim'>age %s · seen ×%s · %s</span></summary>"
        % (_h(rid), _tag(st, "warn" if st.startswith("awaiting") else ""),
           _h((r.get("title") or r.get("finding_key") or "")[:160]),
           _age(r.get("finished_at") or r.get("requested_at")), _n(seen),
           _when(r.get("finished_at") or r.get("requested_at"))),
        _kv([
            ("finding_key", _h(r.get("finding_key")) or "—"),
            ("reason", _h(r.get("reason")) or "—"),
            ("confidence", _n(conf)),
            ("analysis", "<pre class='il-pre'>%s</pre>" % _h(analysis) if analysis else "—"),
            ("decision", "<pre class='il-pre'>%s</pre>" % _h(r.get("decision"))
             if r.get("decision") else "—"),
            ("action", "%s %s" % (_h(r.get("action_method")), _h(r.get("action_url")))
             if r.get("action_url") else "—"),
            ("last_seen", _when(r.get("last_seen"))),
        ]),
        "<div class='il-acts'>%s</div></details>" % " ".join(acts),
    ])


def _tab_inbox(args: dict) -> str:
    res = _forward("GET", READS["inbox"][0])
    ages = _forward("GET", READS["queue_ages"][0])
    if not _readable(res):
        return _unavail(res, "inbox")
    d = res["data"]
    rows = d.get("rows") or []
    counts = d.get("counts") or {}
    groups: dict = {}
    for r in rows:
        groups.setdefault(r.get("action_class") or "unclassified", []).append(r)
    parts = ["<p class='il-mute'>%d open row(s) · by status: %s · %d class group(s) · "
             "collapse ratio (classes / rows) %s</p>"
             % (len(rows), _h(", ".join("%s %s" % (k, v) for k, v in sorted(counts.items()))) or "—",
                len(groups), ("%.2f" % (len(groups) / len(rows))) if rows else "—")]
    if _readable(ages):
        parts.append("<h3>queue ages (part B)</h3>" + _js(ages["data"]))
    else:
        parts.append(_unavail(ages, "queue ages (part B)")
                     + "<p class='il-dim'>ages below are computed from each row's "
                     "own timestamps.</p>")
    for cls in sorted(groups, key=lambda k: (k == "unclassified", k)):
        g = groups[cls]
        oldest = max((_age_hours(r.get("finished_at") or r.get("requested_at")) or 0)
                     for r in g)
        by_status: dict = {}
        for r in g:
            by_status.setdefault(r.get("status") or "?", []).append(r)
        decide = "" if cls == "unclassified" else _btn(
            "resolve_class", "Decide class (all open rows of %s)" % cls,
            {"class": cls, "need": "decision,note",
             "confirm": "Decision for every open row of class %s:" % cls})
        parts.append("<section class='il-grp'><h3>%s <span class='il-dim'>%d row(s) · "
                     "oldest %.1fh · %s</span></h3><div class='il-acts'>%s "
                     "<span class='il-dim'>POST %s — status + note only, never "
                     "executes the action</span></div>"
                     % (_h(cls), len(g), oldest,
                        _h(", ".join("%s %d" % (s, len(v)) for s, v in sorted(by_status.items()))),
                        decide, _h(ACTIONS["resolve_class"]["path"])))
        for st in sorted(by_status):
            parts.append("<h4>%s</h4>" % _tag(st, "warn"))
            parts.extend(_inbox_row(r) for r in by_status[st])
        parts.append("</section>")
    if not rows:
        parts.append("<p class='il-note'>0 rows waiting on a human — measured, not hidden.</p>")
    return "".join(parts)


def _store_entries() -> dict:
    """id -> {status, announced} from the file the approval gate reads. Pure
    file read through the owning module; {} on any failure."""
    try:
        from routes.platform_updates import STORE_PATH, _read_store
        ups, _err = _read_store(STORE_PATH)
        out = {}
        for e in ups or []:
            if isinstance(e, dict) and e.get("id"):
                out[str(e["id"])] = {"status": e.get("status"),
                                     "announced": e.get("announced")}
        return out
    except Exception:  # noqa: BLE001
        return {}


def _tab_platform(args: dict) -> str:
    res = _forward("GET", READS["whats_new"][0])
    if not _readable(res):
        return _unavail(res, "what's new")
    d = res["data"]
    store = _store_entries()
    published = d.get("platform")
    withheld = d.get("platform_withheld") or []
    parts = [_kv([
        ("published", _n(len(published)) if isinstance(published, list) else
         "— (%s)" % _h(d.get("platform_unavailable_reason") or "unmeasured")),
        ("platform_pending", _n(d.get("platform_pending"))),
        ("platform_withheld", _n(len(withheld))),
        ("platform_as_of", _when(d.get("platform_as_of"))),
    ])]
    parts.append(
        "<section class='il-grp'><h3>approval mechanism</h3>"
        "<p>There is <b>no approve/reject endpoint</b>, by design "
        "(routes/platform_updates.py: \"merging that PR IS the approval\"). "
        "A card is served only when its entry carries the literal status "
        "<b>\"published\"</b>. Owner step per entry:</p>"
        "<ol><li>edit <b>data/platform_updates.json</b> → the entry with that "
        "<b>id</b> → set <b>\"status\": \"published\"</b> (or leave it "
        "<b>\"archived\"</b> to keep it retired)</li>"
        "<li>open a PR; merge it — the next read of /api/v1/whats-new serves "
        "the card</li></ol>"
        "<p class='il-dim'>platform_pending counts every NON-published entry "
        "(\"not approved\" reason), so archived cards count as pending — the "
        "store status column below says which are actually retired.</p></section>")
    if isinstance(published, list):
        parts.append("<h3>published (%d)</h3>" % len(published))
        parts.append(_tbl(
            ["id", "tag", "title", "announced", "age", "link"],
            [[_h(c.get("id")), _h(c.get("tag")) or "—", _h(c.get("title")),
              _h(c.get("announced")) or "—", _age(c.get("announced")),
              _h(c.get("link_href")) or "—"] for c in published]))
        if not published:
            parts.append("<p class='il-note'>0 published cards — measured, not hidden.</p>")
    parts.append("<h3>withheld / pending (%d)</h3>" % len(withheld))
    rows = []
    for w in withheld:
        wid = str((w or {}).get("id") or "")
        s = store.get(wid) or {}
        rows.append([_h(wid) or "—", _h((w or {}).get("reason")),
                     _tag(s.get("status") or "unknown",
                          "warn" if s.get("status") not in ("archived",) else ""),
                     _h(s.get("announced")) or "—", _age(s.get("announced"))])
    parts.append(_tbl(["id", "reason (gate)", "store status", "announced", "age"], rows))
    if not withheld:
        parts.append("<p class='il-note'>0 withheld — measured, not hidden.</p>")
    return "".join(parts)


def _tab_lessons(args: dict) -> str:
    q = _clean(args.get("q"), 200) or ""
    parts = []
    # ★ part C serves learn_station_status() INSIDE the recall response
    # (routes/brain_rag.learn_recall -> jsonify(..., status=learn_station_status())).
    # There is no separate status endpoint. An earlier cut invented one and
    # pinned it EXPECTED_LATER, which would have rendered "unavailable (not
    # deployed yet)" FOREVER once C landed -- an unavailable that can never
    # become available is a lie, not an honest blank.
    rec = None
    if q:
        rec = _forward("GET", READS["learn_recall"][0], {"q": q, "k": "8"})
        parts.append("<h3>recall for “%s” (part C)</h3>" % _h(q))
        parts.append(_js(rec["data"]) if _readable(rec) else _unavail(rec, "learn/recall"))
    else:
        parts.append("<p class='il-dim'>type a query above to see what the planner "
                     "would RECALL (GET %s?q=…).</p>" % _h(READS["learn_recall"][0]))
    parts.append("<h3>learn station status (part C)</h3>")
    if rec is None:
        parts.append("<p class='il-dim'>ships inside the recall response — "
                     "run a query above.</p>")
    elif not _readable(rec):
        parts.append(_unavail(rec, "learn station status"))
    elif rec["data"].get("status") is None:
        parts.append("<p class='il-note'>the recall response carried no status "
                     "block — measured, not hidden.</p>")
    else:
        parts.append(_js(rec["data"].get("status")))
    for outcome in ("refuted", "retracted"):
        r = _forward("GET", READS["claims"][0], {"limit": "20", "outcome": outcome})
        parts.append("<h3>latest %s claims</h3>" % _h(outcome))
        if not _readable(r):
            parts.append(_unavail(r, "%s claims" % outcome))
            continue
        rows = r["data"].get("claims") or []
        parts.append(_tbl(
            ["id", "subject", "statement", "expected", "outcome_at"],
            [[_n(c.get("id")), _h(c.get("subject")), _h(str(c.get("statement") or "")[:240]),
              _h(c.get("expected_value")) or "—", _when(c.get("outcome_at"))]
             for c in rows]))
        if not rows:
            parts.append("<p class='il-note'>0 %s claims — measured, not hidden.</p>" % _h(outcome))
    props = _forward("GET", READS["enhancements"][0], {"limit": "200"})
    parts.append("<h3>rejected / duplicate proposals</h3>")
    if not _readable(props):
        parts.append(_unavail(props, "enhancement proposals"))
    else:
        allp = props["data"].get("proposals") or []
        rej = [p for p in allp if str(p.get("status") or "").lower() in _REJECTED_PROPOSAL_STATUSES]
        parts.append("<p class='il-mute'>%d of %d recent proposals are rejected/duplicate</p>"
                     % (len(rej), len(allp)))
        parts.append(_tbl(
            ["id", "status", "area", "title", "grade", "created"],
            [[_n(p.get("id")), _h(p.get("status")), _h(p.get("area")) or "—",
              _h(str(p.get("title") or "")[:200]), _h(p.get("grade")) or "—",
              _when(p.get("created_at"))] for p in rej]))
        if not rej:
            parts.append("<p class='il-note'>0 rejected/duplicate proposals in the last %d — "
                         "measured, not hidden.</p>" % len(allp))
    return "".join(parts)


def _tab_detectors(args: dict) -> str:
    f = _forward("GET", READS["findings"][0])
    oc = _forward("GET", READS["ops_claims"][0], {"limit": "1"})
    parts = ["<h3>the three product detectors</h3>"]
    if not _readable(f):
        parts.append(_unavail(f, "findings db-status"))
    else:
        d = f["data"]
        by = d.get("by_detector") or {}
        recent = d.get("recent") or []
        parts.append(_kv([
            ("brain_findings rows", _n(d.get("total_rows"))),
            ("detector column", _tag("present" if "detector" in (d.get("live_columns") or []) else "absent")),
            ("consistency_radar rows", "%s <span class='il-dim'>%s</span>"
             % (_n(by.get(_RADAR_MODULE)), _h("the module that runs all three"))),
        ]))
        # ★ by_detector is keyed by the `detector` MODULE column
        # (consistency_radar, autonomy_runtime, …), NOT by `issue`. The three
        # product detectors are `issue` values inside consistency_radar's rows,
        # measured live 2026-08-22: by_detector held 10 module keys and none of
        # the three. Printing "0" against them would have been a measured zero
        # that was never measured — say what the column cannot answer instead.
        parts.append(_tbl(
            ["detector (an `issue` value)", "by_detector (keyed by MODULE)", "in last-5 recent"],
            [[_h(name), (_n(by.get(name)) if name in by else
              "<span class='il-dim'>n/a — by_detector keys are modules, not issues</span>"),
              _n(sum(1 for r in recent if r.get("issue") == name))]
             for name in _PRODUCT_DETECTORS]))
        parts.append("<h4>5 most recent findings (product detectors highlighted)</h4>")
        parts.append(_tbl(
            ["issue", "count", "seen", "detail"],
            [[("<b>%s</b>" if r.get("issue") in _PRODUCT_DETECTORS else "%s") % _h(r.get("issue")),
              _n(r.get("count")), _n(r.get("seen_count")), _h(r.get("detail"))]
             for r in recent]))
        parts.append("<p class='il-dim'>db-status exposes a top-10 by detector and the "
                     "last 5 rows only — a detector absent here has not fired "
                     "recently, which is not \"never fired\".</p>")
    parts.append("<h3>brain PRs carrying a detector (this week)</h3>")
    if not _readable(oc):
        parts.append(_unavail(oc, "ops/claims week"))
    else:
        week = oc["data"].get("week") or {}
        det = week.get("brain_prs_with_detector")
        if not isinstance(det, dict):
            parts.append("<p class='il-unavail'>week.brain_prs_with_detector absent — the "
                         "instrument (util/brain_detector_rule) is not importable on this "
                         "process; absent is not 0.</p>")
        else:
            parts.append(_kv([
                ("with_detector / checked", "%s / %s" % (_n(det.get("with_detector")), _n(det.get("checked")))),
                ("unknown", _n(det.get("unknown"))),
                ("basis", _h(det.get("basis")) or "—"),
                ("week", "%s → %s" % (_when(week.get("week_start")), _when(week.get("as_of")))),
            ]))
            prs = det.get("prs")
            if prs:
                parts.append(_js(prs))
    return "".join(parts)


def _tab_shell(args: dict) -> str:
    res = _forward("GET", READS["shell"][0])
    if not _readable(res):
        return _unavail(res, "agentic-loop shell (part A)")
    d = res["data"]
    lanes = d.get("lanes")
    parts = [_kv([(k, _h(v) if not isinstance(v, (dict, list)) else _js(v))
                  for k, v in sorted(d.items()) if k != "lanes"])]
    if isinstance(lanes, list):
        for ln in lanes:
            if not isinstance(ln, dict):
                parts.append(_js(ln))
                continue
            v = ln.get("verdict")
            parts.append("<section class='il-grp'><h3>%s %s</h3>"
                         % (_h(ln.get("name") or ln.get("id") or "lane"),
                            _tag(v or "?", {"PASS": "ok", "FAIL": "err"}.get(v or "", "warn"))))
            checks = ln.get("checks") or []
            parts.append(_tbl(
                ["pass", "check", "detail", "critical"],
                [[_tag("PASS" if c.get("pass") is True else "FAIL" if c.get("pass") is False else "?",
                       "ok" if c.get("pass") is True else "err" if c.get("pass") is False else "warn"),
                  _h(c.get("name") or c.get("id")), _h(c.get("detail")),
                  _h(c.get("critical"))] for c in checks if isinstance(c, dict)]))
            parts.append("</section>")
    else:
        parts.append(_js(d))
    return "".join(parts)


TABS = {
    "claims": _tab_claims,
    "classes": _tab_classes,
    "inbox": _tab_inbox,
    "platform": _tab_platform,
    "lessons": _tab_lessons,
    "detectors": _tab_detectors,
    "shell": _tab_shell,
}


# ── the metric strip ────────────────────────────────────────────────────────

def _rate_block(res: dict):
    if not _readable(res) or not res["data"].get("ok", True):
        return None
    d = res["data"]
    return {"rate": d.get("recurrence_rate"), "closed": d.get("closed"),
            "recurred": d.get("recurred"), "window_days": d.get("window_days")}


def metrics() -> dict:
    """Everything the strip shows, with a `sources` block that says what could
    not be read. A null is 'not measured'; 0 is a measured zero."""
    oc = _forward("GET", READS["ops_claims"][0], {"limit": "1"})
    c30 = _forward("GET", READS["convergence"][0], {"days": "30"})
    c7 = _forward("GET", READS["convergence"][0], {"days": "7"})
    dm = _forward("GET", READS["deadman"][0])
    sh = _forward("GET", READS["shell"][0])
    week = oc["data"].get("week") if _readable(oc) else None
    shell = None
    if _readable(sh):
        d = sh["data"]
        delta = None
        for src in (d, d.get("metric"), d.get("headline")):
            if isinstance(src, dict) and src.get("recurrence_delta_7d") is not None:
                delta = src.get("recurrence_delta_7d")
                break
        shell = {"available": True,
                 "headline": d.get("headline") or d.get("metric") or d.get("metrics"),
                 "verdict": d.get("verdict") or d.get("status"),
                 "recurrence_delta_7d": delta}
    return {
        "ok": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "week": week if isinstance(week, dict) else None,
        "granted_classes": (week or {}).get("granted_action_classes") if isinstance(week, dict) else None,
        "recurrence_30d": _rate_block(c30),
        "recurrence_7d": _rate_block(c7),
        "deadman_overdue": dm["data"].get("overdue_count") if _readable(dm) else None,
        "deadman_tracked": dm["data"].get("tracked") if _readable(dm) else None,
        "shell": shell or {"available": False, "recurrence_delta_7d": None},
        "sources": {name: {"status": r["status"], "unavailable": r["unavailable"],
                           "error": r["error"]}
                    for name, r in (("ops_claims", oc), ("convergence_30d", c30),
                                    ("convergence_7d", c7), ("deadman", dm),
                                    ("shell", sh))},
    }


# ── the page ────────────────────────────────────────────────────────────────
# Class names deliberately avoid the substrings dchub-brand.css wildcards
# match with !important (card, panel, nav, pill, wrapper, container, code,
# mono, btn-brand) — see the brand.css wrapper-trap note. Tokens mirror
# static/dchub-brand.css so the page reads as the house theme; no external
# stylesheet or script is loaded.
_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>DC Hub · agentic loop · inspect</title>
<style>
:root{--dch-bg:#0a0a0f;--dch-surface:#131319;--dch-surface-2:#1a1a22;--dch-border:rgba(255,255,255,.08);--dch-text:#fafafa;--dch-text-mute:#a1a1aa;--dch-text-dim:#71717a;--dch-indigo:#818cf8;--dch-indigo-deep:#6366f1;--dch-violet:#a855f7;--dch-ok:#10b981;--dch-warn:#f59e0b;--dch-err:#ef4444}
@media (prefers-color-scheme: light){:root{--dch-bg:#fafafa;--dch-surface:#ffffff;--dch-surface-2:#f1f1f4;--dch-border:rgba(0,0,0,.12);--dch-text:#111114;--dch-text-mute:#4b4b55;--dch-text-dim:#6b6b75}}
*{box-sizing:border-box}
body{margin:0;background:var(--dch-bg);color:var(--dch-text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.il-top{max-width:1280px;margin:0 auto;padding:1.2rem 1.2rem 3rem}
.il-top h1{font-size:1.25rem;margin:0 0 .2rem;letter-spacing:-.01em}
.il-top h1 span{color:var(--dch-indigo)}
.il-lede{color:var(--dch-text-mute);margin:0 0 1rem}
.il-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.5rem;margin:.6rem 0 1rem}
.il-stat{background:var(--dch-surface);border:1px solid var(--dch-border);border-radius:8px;padding:.6rem .75rem}
.il-stat .v{font:700 1.45rem/1.1 ui-monospace,SFMono-Regular,Menlo,monospace}
.il-stat .l{color:var(--dch-text-mute);font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;margin-top:.3rem}
.il-tabs{display:flex;flex-wrap:wrap;gap:.3rem;border-bottom:1px solid var(--dch-border);margin-bottom:.8rem}
.il-tab{background:none;border:1px solid transparent;border-bottom:none;color:var(--dch-text-mute);padding:.45rem .8rem;border-radius:6px 6px 0 0;cursor:pointer;font:inherit}
.il-tab[aria-selected="true"]{color:var(--dch-text);background:var(--dch-surface);border-color:var(--dch-border)}
.il-flt{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:0 0 .8rem;color:var(--dch-text-mute)}
.il-flt input,.il-flt select{background:var(--dch-surface-2);color:var(--dch-text);border:1px solid var(--dch-border);border-radius:6px;padding:.3rem .5rem;font:inherit}
.il-flt button,.il-act{background:var(--dch-indigo-deep);color:#fff;border:0;border-radius:6px;padding:.35rem .7rem;font:inherit;cursor:pointer}
.il-act:disabled{opacity:.5;cursor:wait}
.il-acts{margin:.5rem 0;display:flex;flex-wrap:wrap;gap:.4rem;align-items:center}
.il-status{min-height:1.4em;color:var(--dch-warn);margin:.4rem 0 .8rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
.il-view{background:var(--dch-surface);border:1px solid var(--dch-border);border-radius:8px;padding:.9rem 1rem}
.il-view h3{margin:1rem 0 .4rem;font-size:1rem}
.il-view h4{margin:.7rem 0 .3rem;font-size:.85rem;color:var(--dch-text-mute)}
.il-row{border:1px solid var(--dch-border);border-radius:6px;padding:.4rem .6rem;margin:.35rem 0;background:var(--dch-surface-2)}
.il-row summary{cursor:pointer}
.il-grp{border-top:1px dashed var(--dch-border);padding-top:.4rem;margin-top:.6rem}
.il-kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:.3rem .9rem;margin:.4rem 0}
.il-kv dt{color:var(--dch-text-dim);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em}
.il-kv dd{margin:0;overflow-wrap:anywhere}
.il-scroll{overflow-x:auto}
.il-tbl{border-collapse:collapse;width:100%;font-size:.85rem}
.il-tbl th,.il-tbl td{text-align:left;padding:.3rem .45rem;border-bottom:1px solid var(--dch-border);vertical-align:top;overflow-wrap:anywhere}
.il-tbl th{color:var(--dch-text-dim);font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.06em}
.il-tag{display:inline-block;border:1px solid var(--dch-border);border-radius:999px;padding:0 .5rem;font-size:.75rem;color:var(--dch-text-mute)}
.il-tag.il-ok{color:var(--dch-ok);border-color:var(--dch-ok)}
.il-tag.il-warn{color:var(--dch-warn);border-color:var(--dch-warn)}
.il-tag.il-err{color:var(--dch-err);border-color:var(--dch-err)}
.il-ok{color:var(--dch-ok)}.il-warn{color:var(--dch-warn)}.il-err{color:var(--dch-err)}
.il-pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--dch-bg);border:1px solid var(--dch-border);border-radius:6px;padding:.5rem;margin:.2rem 0;font:.8rem/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:28rem;overflow:auto}
.il-unavail{color:var(--dch-warn);border:1px dashed var(--dch-warn);border-radius:6px;padding:.4rem .6rem}
.il-note{color:var(--dch-text-mute)}.il-mute{color:var(--dch-text-mute)}.il-dim{color:var(--dch-text-dim);font-size:.85em}
</style>
</head>
<body>
<div class="il-top">
<h1>Agentic loop <span>· owner inspection</span> <span class="il-dim">shell #65 · part D</span></h1>
<p class="il-lede">A UI over the loop's existing admin endpoints. Reads are live on every tab switch; actions forward to the endpoint named beside each button. A null reads “—” (not measured); a zero reads “0”.</p>
<div class="il-strip">
<div class="il-stat"><div class="v" data-m="week.shipped">—</div><div class="l">claims shipped · wk</div></div>
<div class="il-stat"><div class="v il-ok" data-m="week.confirmed">—</div><div class="l">confirmed</div></div>
<div class="il-stat"><div class="v il-err" data-m="week.refuted_kept">—</div><div class="l">refuted-and-kept</div></div>
<div class="il-stat"><div class="v il-warn" data-m="week.retracted">—</div><div class="l">retracted</div></div>
<div class="il-stat"><div class="v" data-m="week.open">—</div><div class="l">open (horizon pending)</div></div>
<div class="il-stat"><div class="v" data-m="granted_classes">—</div><div class="l">granted classes</div></div>
<div class="il-stat"><div class="v" data-m="recurrence_30d.rate">—</div><div class="l">recurrence · 30d</div></div>
<div class="il-stat"><div class="v" data-m="recurrence_7d.rate">—</div><div class="l">recurrence · 7d window</div></div>
<div class="il-stat"><div class="v" data-m="shell.recurrence_delta_7d">—</div><div class="l">Δ recurrence 7d (shell)</div></div>
<div class="il-stat"><div class="v il-warn" data-m="deadman_overdue">—</div><div class="l">dead-man overdue</div></div>
<div class="il-stat"><div class="v" data-m="week.brain_prs_with_detector.with_detector">—</div><div class="l">brain PRs w/ detector · wk</div></div>
</div>
<div class="il-dim" id="il-mstat">reading…</div>
<div class="il-tabs" role="tablist">
<button type="button" class="il-tab" role="tab" data-tab="claims">Claims</button>
<button type="button" class="il-tab" role="tab" data-tab="classes">Classes</button>
<button type="button" class="il-tab" role="tab" data-tab="inbox">Inbox</button>
<button type="button" class="il-tab" role="tab" data-tab="platform">Platform updates</button>
<button type="button" class="il-tab" role="tab" data-tab="lessons">Lessons</button>
<button type="button" class="il-tab" role="tab" data-tab="detectors">Detectors</button>
<button type="button" class="il-tab" role="tab" data-tab="shell">Shell</button>
</div>
<form class="il-flt" data-for="claims" hidden>
<label>kind <input name="kind" size="12" placeholder="canon, finding…"></label>
<label>outcome <select name="outcome"><option value="">any</option><option>open</option><option>confirmed</option><option>refuted</option><option>retracted</option><option>unobserved</option></select></label>
<label>subject contains <input name="subject" size="18"></label>
<button type="submit">filter</button>
</form>
<form class="il-flt" data-for="lessons" hidden>
<label>recall query <input name="q" size="32" placeholder="e.g. deals count"></label>
<button type="submit">recall</button>
</form>
<div class="il-status" id="il-status"></div>
<div class="il-view" id="il-view">loading…</div>
<p class="il-dim">as of {{as_of}} · index: <span id="il-index">/api/v1/brain/agentic-loop/inspect</span> · kill AGENTIC_LOOP_SHELL_DISABLE=1</p>
</div>
<script>
(function(){
'use strict';
var API = '/api/v1/brain/agentic-loop/inspect';
var TABS = ['claims','classes','inbox','platform','lessons','detectors','shell'];
function $(s){ return document.querySelector(s); }
function pick(o, p){ var cur = o; var ks = p.split('.'); for (var i = 0; i < ks.length; i++){ if (cur === null || cur === undefined || typeof cur !== 'object') return null; cur = cur[ks[i]]; } return (cur === undefined) ? null : cur; }
function fmt(v){ if (v === null || v === undefined) return '—'; if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(3); if (typeof v === 'object') return JSON.stringify(v); return String(v); }
function setStatus(t){ $('#il-status').textContent = t; }
function currentTab(){ var t = location.hash.replace('#', ''); return TABS.indexOf(t) >= 0 ? t : 'claims'; }
function loadMetrics(){
  var st = $('#il-mstat'); st.textContent = 'reading…';
  fetch(API + '/metrics', {credentials: 'same-origin'}).then(function(r){
    return r.json().then(function(d){ return {status: r.status, data: d}; }, function(){ return {status: r.status, data: null}; });
  }).then(function(res){
    if (res.status !== 200 || !res.data){ st.textContent = 'metrics unreadable (HTTP ' + res.status + ')'; return; }
    var els = document.querySelectorAll('[data-m]');
    for (var i = 0; i < els.length; i++){ els[i].textContent = fmt(pick(res.data, els[i].getAttribute('data-m'))); }
    var bad = [];
    var src = res.data.sources || {};
    for (var k in src){ if (src[k] && (src[k].unavailable || src[k].error)) bad.push(k + (src[k].unavailable ? ' (not deployed yet)' : ': ' + src[k].error)); }
    st.textContent = 'as of ' + (res.data.as_of || '?') + (bad.length ? ' · unreadable: ' + bad.join('; ') : '');
  }, function(){ st.textContent = 'metrics unreadable'; });
}
function qs(tab){
  var f = document.querySelector('form[data-for="' + tab + '"]');
  if (!f) return '';
  var p = new URLSearchParams(new FormData(f)); var s = p.toString();
  return s ? ('?' + s) : '';
}
function loadTab(tab){
  var view = $('#il-view'); view.textContent = 'loading ' + tab + '…';
  fetch(API + '/tab/' + encodeURIComponent(tab) + qs(tab), {credentials: 'same-origin'}).then(function(r){
    return r.text().then(function(html){ return {status: r.status, html: html}; });
  }).then(function(res){
    if (res.status !== 200){ view.textContent = 'tab unreadable (HTTP ' + res.status + ')'; return; }
    /* the ONE place markup is inserted: a same-origin, admin-gated fragment
       that the server rendered and escaped (routes/agentic_loop_inspect._h). */
    view.innerHTML = res.html;
  }, function(){ view.textContent = 'tab unreadable'; });
}
function select(tab){
  if (TABS.indexOf(tab) < 0) tab = 'claims';
  var tabs = document.querySelectorAll('.il-tab');
  for (var i = 0; i < tabs.length; i++){ tabs[i].setAttribute('aria-selected', tabs[i].getAttribute('data-tab') === tab ? 'true' : 'false'); }
  var forms = document.querySelectorAll('form[data-for]');
  for (var j = 0; j < forms.length; j++){ forms[j].hidden = (forms[j].getAttribute('data-for') !== tab); }
  if (location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
  loadTab(tab);
}
function describe(act, status, d){
  if (!d) return act + ': HTTP ' + status;
  if (d.unavailable) return act + ': unavailable (not deployed yet) — ' + (d.path || '');
  var up = d.upstream || {}; var bits = [act, 'HTTP ' + (d.status || status)];
  if (d.error) bits.push(String(d.error));
  if (up.error && up.error !== d.error) bits.push(String(up.error));
  if (up.ok === true) bits.push('ok');
  if (up.refused) bits.push('refused');
  if (up.already) bits.push('already');
  if (typeof up.stamped === 'number') bits.push('stamped ' + up.stamped + ' of ' + up.due + ' due');
  if (typeof up.resolved === 'number') bits.push('resolved ' + up.resolved);
  if (up.dry_run === true) bits.push('dry run');
  return bits.join(' · ');
}
function act(b){
  var name = b.getAttribute('data-act'); var body = {};
  var skip = {act: 1, need: 1, confirm: 1};
  for (var i = 0; i < b.attributes.length; i++){
    var a = b.attributes[i]; if (a.name.indexOf('data-') !== 0) continue;
    var k = a.name.slice(5); if (skip[k]) continue; body[k] = a.value;
  }
  if ('granted' in body) body.granted = (body.granted === 'true');
  if ('clear_breaker' in body) body.clear_breaker = (body.clear_breaker === '1');
  var need = (b.getAttribute('data-need') || '').split(',').filter(Boolean);
  var confirmText = b.getAttribute('data-confirm') || '';
  for (var n = 0; n < need.length; n++){
    var v = window.prompt((confirmText ? confirmText + ' ' : '') + '[' + need[n] + ', required]');
    if (v === null) return;
    if (!v.trim()){ setStatus(need[n] + ' is required — nothing was sent'); return; }
    body[need[n]] = v.trim();
  }
  if (!need.length && confirmText && !window.confirm(confirmText)) return;
  b.disabled = true; var old = b.textContent; b.textContent = 'sending…';
  fetch(API + '/act/' + encodeURIComponent(name), {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(function(r){
    return r.json().then(function(d){ return {status: r.status, data: d}; }, function(){ return {status: r.status, data: null}; });
  }).then(function(res){
    setStatus(describe(name, res.status, res.data));
    if (res.status === 200 && res.data && res.data.ok){ setTimeout(function(){ loadTab(currentTab()); loadMetrics(); }, 600); }
  }, function(){ setStatus(name + ': request failed'); }).then(function(){ b.disabled = false; b.textContent = old; });
}
document.addEventListener('click', function(e){
  var t = e.target.closest('.il-tab'); if (t){ select(t.getAttribute('data-tab')); return; }
  var b = e.target.closest('button[data-act]'); if (b) act(b);
});
document.addEventListener('submit', function(e){
  var f = e.target.closest('form[data-for]'); if (!f) return;
  e.preventDefault(); loadTab(f.getAttribute('data-for'));
});
window.addEventListener('hashchange', function(){ select(currentTab()); });
loadMetrics(); select(currentTab());
})();
</script>
</body>
</html>
"""


# ── routes ──────────────────────────────────────────────────────────────────

@agentic_loop_inspect_bp.after_request
def _never_cached(resp):
    return _no_store(resp)


@agentic_loop_inspect_bp.route(PAGE_PATH, methods=["GET"])
def page():
    if _disabled():
        return jsonify(ok=False, disabled=True,
                       hint="AGENTIC_LOOP_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return Response(_FORBIDDEN_HTML, status=403, mimetype="text/html")
    html = _PAGE.replace("{{as_of}}", _h(datetime.now(timezone.utc).isoformat()))
    resp = Response(html, mimetype="text/html")
    # ?key= once, then the cookie carries it: bookmarkable as the bare path
    # and the key stays out of later URLs/logs (same as /brain).
    qk = (request.args.get("admin_key") or request.args.get("key") or "").strip()
    if qk:
        resp.set_cookie(_COOKIE, qk, max_age=2592000,
                        httponly=True, secure=True, samesite="Lax")
    return resp


@agentic_loop_inspect_bp.route(API_PREFIX, methods=["GET"])
def index():
    early = _gate()
    if early:
        return early
    try:
        rules = {str(r.rule) for r in current_app.url_map.iter_rules()}
    except Exception:  # noqa: BLE001
        rules = set()
    targets = sorted({p for p, _ in READS.values()} | {s["path"] for s in ACTIONS.values()})
    return jsonify(
        ok=True, page=PAGE_PATH, api=API_PREFIX, tabs=sorted(TABS),
        reads={k: v[0] for k, v in READS.items()},
        actions={k: {"method": s["method"], "path": s["path"],
                     "forced_query": s["query"], "body": list(s["body"]),
                     "required": list(s["required"])}
                 for k, s in ACTIONS.items()},
        expected_later=sorted(EXPECTED_LATER),
        registered={p: (p in rules) for p in targets},
        note=("registered=false for a path outside expected_later means the "
              "target is missing on THIS process; a tab reads 'unavailable "
              "(not deployed yet)' only for expected_later paths"),
        generated_at=datetime.now(timezone.utc).isoformat())


@agentic_loop_inspect_bp.route(API_PREFIX + "/metrics", methods=["GET"])
def metrics_get():
    early = _gate()
    if early:
        return early
    try:
        return jsonify(metrics())
    except Exception as e:  # noqa: BLE001
        logger.warning("[agentic_loop_inspect] metrics failed: %s", e)
        return jsonify(ok=False, error=str(e)[:200]), 200


@agentic_loop_inspect_bp.route(API_PREFIX + "/tab/<name>", methods=["GET"])
def tab_get(name):
    early = _gate()
    if early:
        return early
    fn = TABS.get(name)
    if fn is None:
        return jsonify(ok=False, error="unknown tab", tabs=sorted(TABS)), 404
    try:
        html = fn(request.args)
    except Exception as e:  # noqa: BLE001 — a tab must never 5xx the page
        logger.warning("[agentic_loop_inspect] tab %s failed: %s", name, e)
        html = ("<p class='il-unavail'>%s: unreadable — %s: %s</p>"
                % (_h(name), _h(type(e).__name__), _h(str(e)[:200])))
    return Response(html, mimetype="text/html")


@agentic_loop_inspect_bp.route(API_PREFIX + "/act/<name>", methods=["POST"])
def act_post(name):
    early = _gate()
    if early:
        return early
    spec = ACTIONS.get(name)
    if spec is None:
        return jsonify(ok=False, error="unknown action", actions=sorted(ACTIONS)), 404
    b = request.get_json(silent=True) or {}
    body = {}
    for k in spec["body"]:
        if k in b:
            v = b[k]
            body[k] = v.strip() if isinstance(v, str) else v
    if "granted" in body and isinstance(body["granted"], str):
        body["granted"] = body["granted"].lower() == "true"
    missing = [k for k in spec["required"] if body.get(k) in (None, "")]
    if missing:
        return jsonify(ok=False, error="missing: " + ", ".join(missing),
                       action=name), 400
    body.update(spec.get("stamp") or {})
    # The caller's query string is NEVER forwarded; only the forced one is.
    res = _forward(spec["method"], spec["path"], spec["query"],
                   body if (spec["body"] or spec.get("stamp")) else None)
    return jsonify(ok=res["ok"], status=res["status"], unavailable=res["unavailable"],
                   error=res["error"], upstream=res["data"], action=name,
                   path=spec["path"], forced_query=spec["query"])


def register_agentic_loop_inspect(app) -> bool:
    app.register_blueprint(agentic_loop_inspect_bp)
    return True
