"""routes/surface_truth_master_shell.py — Surface Truth Master Shell (#30, 2026-07-25).

WHY THIS EXISTS
===============
On 2026-07-25 the canonical-counts fence went GREEN while every live
agent-facing surface still published the retired pre-dedup facility floor.

The fence (tests/test_canonical_counts_drift.py) scans four REPO-ROOT files:
llms.txt, llms-full.txt, README.md, .well-known/mcp.json. But `/llms.txt` is
served inline from ai_discovery_routes.py (main.py:20177 records the old file
routes being removed), and further copies live in static/ and dchub-frontend/.
The repo-root files the fence reads are served by NOTHING. Editing them turned
the check green and changed nothing a model can see — four different numbers
were live at once (20,000+, 21,000+, 22,000+) against a canon of 15,000+.

That is the session's recurring failure in its purest form: a check that
verifies an artifact instead of reality stops being a check. The dead-man board
had the same shape (a beat that proved a job ran, not that it ingested), and so
did no_new_data (a status asserting zero was expected, on top of a counter that
was structurally always zero).

So this shell verifies the ONLY thing that matters: the bytes an agent actually
receives. It fetches the live URLs through the public edge — the same path a
crawler takes — and compares them to ai_surface_canon.PINNED. It reads no repo
file to decide whether a surface is honest, except in the one lane whose job is
to catch repo-vs-served divergence.

LANES
  1 · served agent text      /llms.txt, /llms-full.txt carry canon, not a floor
  2 · served manifests       /.well-known/mcp.json, /mcp.json likewise
  3 · repo vs served         the files the FENCE reads agree with what is SERVED
                             (this lane is the one that would have caught today)
  4 · emitter sources        the python that BUILDS the served bytes is clean,
                             so a regression is visible before it deploys

HOUSE RULES
  · A lane never reads PASS when it could not check. An unreachable surface is
    '?', never green-by-silence — the whole point is that silence lied before.
  · Read-only. Fetches and greps. Flips nothing, writes nothing but its own
    dead-man beat into the ingest_runs ledger (L8: never auto-execute).
  · Fail-soft everywhere: a crashed lane renders '?' and never 500s the tick.

Surface:  GET /admin/surface-truth            (HTML)
          GET /api/v1/admin/surface-truth     (HTML)
          GET|POST /api/v1/admin/surface-truth/master-tick   (JSON)
Beat:     surface-truth-shell-daily
Kill:     SURFACE_TRUTH_SHELL_DISABLE=1
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

surface_truth_master_shell_bp = Blueprint("surface_truth_master_shell", __name__)

# Public origin — deliberately the EDGE, not loopback. An agent reaches the
# surface through Cloudflare, so that is the path we must verify; a loopback
# check would pass while a cached/edge-served body stayed stale.
ORIGIN = (os.environ.get("SURFACE_TRUTH_ORIGIN") or "https://dchub.cloud").rstrip("/")

# ★ urllib without a UA gets CF-403'd on this zone. Always send one.
_UA = "dchub-surface-truth/1.0 (+https://dchub.cloud; internal-audit)"

# Any retired facility floor. Deliberately a RANGE, not one value: the point is
# that four different numbers were live simultaneously, so pinning a single old
# value would have missed three of them. ★2026-07-31: + the exact retired canon
# 12,650+ (canon itself 07-24→07-28, now two generations old; swept from every
# live surface in #1101/#1978 — a reappearance is a revert, not history).
# ★★★ 2026-08-31 — THIS BAN WAS A RANGE, AND THE TRUTH GREW INTO IT.
# Was: re.compile(r"\b(?:12,650|(?:19|20|21|22|23),\d{3})\+") — i.e. every
# floor from 19,000 to 23,999 was "retired". PINNED is 18,500+, so
# _acceptable_floor's band is [18,500, 20,350]; the live healed floor 19,700+
# sits INSIDE that band and inside the banned range at the same time. Every
# text and manifest surface therefore PASSED "carries canon floor (found
# 19,700+)" and FAILED "free of retired floors (serves retired floor(s):
# 19,700+)" on the identical byte. Three of four lanes red, permanently, on a
# contradiction — which is why the reds never converted into a fix (SH52-036).
#
# This is the SECOND time this exact class has bitten: scripts/accuracy_fence.py
# froze dchub-frontend production for 19 consecutive deploys on 2026-08-29 when
# `[2-9],\d{3} deals` matched canon the hour deals_tracked passed 2,000, and its
# facilities twin was ~3 days from the identical freeze. The lesson recorded
# then: a retired LITERAL stays wrong forever, but a retired RANGE does not —
# the fleet grows into it. Entity bans were made canon-relative there. This
# shell never got the same treatment.
#
# So: literals stay literal, and the over-claim rule is derived from canon.
# A token is retired when it is a historical literal we will never serve again,
# or when it sits ABOVE the acceptance ceiling _acceptable_floor already
# computes. Anything that function ACCEPTS can never be reported retired — the
# two checks now read one band instead of disagreeing about the same bytes.
_RETIRED_LITERALS = ("12,650+",)

# Live agent-facing surfaces, by lane.
# ★2026-07-30: /agent added — the Agent Concierge landing is served INLINE from
# routes/agent_concierge.py (frontend worker proxies the bare path to Flask).
# Its title stale-cycled through retired tool counts for weeks with no live
# check on it; it now renders ai_surface_canon.PINNED, so the canon-floor
# presence check below fails on any stale build or >1-day-stale CF cache.
# ★2026-07-31: /ai added — the frontend ai.html (CF Pages; the SSR bytes are
# what crawlers and agents read) is HEAL-BOUND: its seeds carry
# resolve_canon()'s live floor (15,300+/15,500+), not PINNED (15,000+), which
# is why it was deferred on 07-30 ("red by construction" under the exact-PINNED
# check). _acceptable_floor below takes either, so the beat can hold it now.
_TEXT_SURFACES = ("/llms.txt", "/llms-full.txt", "/agent", "/ai")
_MANIFEST_SURFACES = ("/.well-known/mcp.json", "/mcp.json")

# Lane 3: the files the canonical-counts FENCE scans -> the URL that actually
# serves that surface. A disagreement means the fence is guarding a file nobody
# reads, which is exactly how 2026-07-25 happened.
_FENCE_FILE_TO_URL = {
    "llms.txt": "/llms.txt",
    "llms-full.txt": "/llms-full.txt",
    os.path.join(".well-known", "mcp.json"): "/.well-known/mcp.json",
}

# Lane 4: server-side python that EMITS the served bytes inline.
_EMITTER_SOURCES = ("ai_discovery_routes.py",)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("SURFACE_TRUTH_SHELL_DISABLE") or "").strip() == "1"


# ── helpers ───────────────────────────────────────────────────────────

def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate, renders '?')."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    """PASS only when every critical check affirmatively passed. An
    indeterminate critical check yields '?' — never green-by-silence."""
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in crits):
        return "?"
    return "PASS"


def _canon_floor() -> str | None:
    """The published facility floor, from the single source of truth."""
    try:
        from ai_surface_canon import PINNED
        return (PINNED.get("public") or {}).get("facilities")
    except Exception as e:  # noqa: BLE001
        logger.warning("[surface-truth] canon import failed: %s", e)
        return None


def _fetch(path: str):
    """GET a live surface. Returns (body, error). Never raises.

    requests, not urllib (regression_lint urllib-request-on-railway).
    A non-2xx is an ERROR, not a body: a CF 403 or a 404 means we could NOT
    check, which must render '?' — never a PASS on an error page's contents.
    """
    try:
        import requests as _rq
        r = _rq.get(ORIGIN + path, headers={"User-Agent": _UA}, timeout=12)
        if r.status_code >= 400:
            return None, "HTTP %d" % r.status_code
        return r.text, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:110])


def _read_repo(rel: str):
    try:
        with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8",
                  errors="ignore") as fh:
            return fh.read()
    except Exception:
        return None


def _floors_in(text: str, canon: str | None = None) -> list[str] | None:
    """Floor tokens in `text` that are RETIRED, given canon.

    Returns None when canon is unknown — indeterminate, never "clean". A fence
    that cannot resolve canon must not certify a page as free of stale floors;
    that is the fail-open direction this shell exists to prevent.

    Retired means: an explicit historical literal, or an over-claim strictly
    above the same ceiling _acceptable_floor uses. Values inside the acceptance
    band are canon-family by definition and are never retired here."""
    body = text or ""
    retired = {lit for lit in _RETIRED_LITERALS if lit in body}

    if canon is None:
        return None
    try:
        base = int(canon.replace(",", "").rstrip("+"))
    except Exception:  # noqa: BLE001
        return None
    hi = int(base * 1.10)          # SAME ceiling as _acceptable_floor
    for m in _FLOOR_TOKEN.finditer(body):
        v = int(m.group(1).replace(",", ""))
        if v > hi:
            retired.add(m.group(0))
    return sorted(retired)


# Any comma-formatted "N+" floor token, for the acceptance band below.
_FLOOR_TOKEN = re.compile(r"\b(\d{1,3}(?:,\d{3})+)\+")


def _acceptable_floor(body, canon):
    """The facility floor the body carries, if it is canon-family — else None.

    Heal-bound pages (the llms.txt front-door block, /ai) carry
    resolve_canon()'s LIVE floor — 15,300+/15,500+ while PINNED holds 15,000+
    — and the live floor moves at 100-granularity as the fleet grows, with the
    daily heal lagging the origin by up to ~24h. Exact-phrase matching against
    either single value therefore reds a healthy page (the reason /ai was
    deferred from _TEXT_SURFACES on 07-30). Accept the PINNED phrase itself, or
    ANY comma-formatted "N+" floor within [PINNED, PINNED x 1.10]: the band
    tracks realistic PINNED-to-live drift (3-4% today), rejects the 17k/18k
    legacy/raw-basis over-claims, and self-heals across floor bumps with no
    beat edits.
    ★Collision note: no OTHER canon quantity states a comma-"N+" floor inside
    this band (13,000+ US plants sits below; 126k+ substations far above) — if
    one ever does, tighten this to a facilities-context match.
    """
    if not body or not canon:
        return None
    if canon in body:
        return canon
    try:
        base = int(canon.replace(",", "").rstrip("+"))
    except Exception:  # noqa: BLE001
        return None
    hi = int(base * 1.10)
    for m in _FLOOR_TOKEN.finditer(body):
        v = int(m.group(1).replace(",", ""))
        if base <= v <= hi:
            return m.group(0)
    return None


def _audit_body(cid: str, label: str, body, err, canon: str) -> list[dict]:
    """Two checks per surface: canon present, and no retired floor."""
    if body is None:
        # Unreachable is INDETERMINATE, not healthy. This is the distinction
        # the whole shell exists to preserve.
        return [_check(cid + "_reachable", label + " reachable", None,
                       "fetch failed: %s" % err, critical=True)]
    stale = _floors_in(body, canon)
    found = _acceptable_floor(body, canon)
    return [
        _check(cid + "_canon", label + " carries canon floor",
               found is not None,
               ("found %s" % found) if found is not None
               else ("no canon-family facility floor in the served body "
                     "(accepts PINNED %s or a live-healed floor within 10%% "
                     "above it)" % canon),
               critical=True),
        _check(cid + "_stale", label + " free of retired floors",
               (None if stale is None else not stale),
               "clean" if not stale else "serves retired floor(s): %s"
               % ", ".join(stale),
               critical=True),
    ]


# ── lanes ─────────────────────────────────────────────────────────────

def _lane_served_text(canon: str) -> list[dict]:
    out: list[dict] = []
    for path in _TEXT_SURFACES:
        body, err = _fetch(path)
        out.extend(_audit_body(path.strip("/").replace(".", "_").replace("-", "_"),
                               path, body, err, canon))
    return out


def _lane_served_manifests(canon: str) -> list[dict]:
    out: list[dict] = []
    for path in _MANIFEST_SURFACES:
        body, err = _fetch(path)
        cid = path.strip("/").replace("/", "_").replace(".", "_").replace("-", "_")
        checks = _audit_body(cid, path, body, err, canon)
        if body is not None:
            try:
                json.loads(body)
                checks.append(_check(cid + "_json", path + " is valid JSON",
                                     True, "parses", critical=False))
            except Exception as e:  # noqa: BLE001
                checks.append(_check(cid + "_json", path + " is valid JSON",
                                     False, "parse failed: %s" % str(e)[:90],
                                     critical=False))
        out.extend(checks)
    return out


def _lane_repo_vs_served(canon: str) -> list[dict]:
    """THE lane that would have caught 2026-07-25.

    The canonical-counts fence scans repo files. If a fence file disagrees with
    the body actually served at its URL, the fence is guarding an artifact and
    its green means nothing.
    """
    out: list[dict] = []
    for rel, url in _FENCE_FILE_TO_URL.items():
        cid = "parity_" + rel.replace(os.sep, "_").replace(".", "_").replace("-", "_")
        repo = _read_repo(rel)
        body, err = _fetch(url)
        if repo is None:
            out.append(_check(cid, "%s vs %s" % (rel, url), None,
                              "fence file unreadable in repo", critical=True))
            continue
        if body is None:
            out.append(_check(cid, "%s vs %s" % (rel, url), None,
                              "served body unreachable: %s" % err, critical=True))
            continue
        # Accept-either here too: served bodies are heal-bound (live floor),
        # repo copies may hold PINNED — both are canon-family, and the lane's
        # job is the 07-25 shape (fence green, live stale), not phrasing.
        _repo_stale = _floors_in(repo, canon)
        _served_stale = _floors_in(body, canon)
        if _repo_stale is None or _served_stale is None:
            # canon unresolvable -> indeterminate, never a pass. Same rule as
            # an unreachable body above.
            out.append(_check(cid, "%s vs %s" % (rel, url), None,
                              "canon floor unresolvable — cannot judge",
                              critical=True))
            continue
        repo_ok = _acceptable_floor(repo, canon) is not None and not _repo_stale
        served_ok = _acceptable_floor(body, canon) is not None and not _served_stale
        if repo_ok and not served_ok:
            detail = ("FENCE GREEN, LIVE STALE — repo %s is canon-clean while %s "
                      "serves %s. The fence is guarding a file nobody serves."
                      % (rel, url, ", ".join(_served_stale) or "no canon floor"))
            out.append(_check(cid, "%s vs %s" % (rel, url), False, detail,
                              critical=True))
        elif served_ok and not repo_ok:
            out.append(_check(cid, "%s vs %s" % (rel, url), False,
                              "live is canon-clean but repo %s is not — the fence "
                              "will fail on a surface that is actually fine" % rel,
                              critical=True))
        else:
            out.append(_check(cid, "%s vs %s" % (rel, url), bool(served_ok),
                              "agree (%s)" % ("both clean" if served_ok
                                              else "BOTH carry a retired floor"),
                              critical=True))
    return out


def _lane_emitter_sources(canon: str) -> list[dict]:
    """The python that builds the served bytes. Catches a regression at review
    time instead of after it deploys."""
    out: list[dict] = []
    for rel in _EMITTER_SOURCES:
        src = _read_repo(rel)
        cid = "emitter_" + rel.replace(".", "_")
        if src is None:
            out.append(_check(cid, rel + " readable", None,
                              "source unreadable", critical=True))
            continue
        stale = _floors_in(src, _canon_floor())
        out.append(_check(cid, rel + " free of retired floors",
                          (None if stale is None else not stale),
                          "canon floor unresolvable — cannot judge"
                          if stale is None
                          else ("clean" if not stale
                                else "emits retired floor(s): %s" % ", ".join(stale)),
                          critical=True))
    return out


# ── dead-man beat ─────────────────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    """Best-effort beat into the SHIPPED ingest_runs ledger. NEVER raises."""
    try:
        body = json.dumps({
            "feed": "surface-truth-shell-daily",
            # ★ batch-3/Screen D: this was the literal "success", which is in
            # routes/ingest_runs._OK_STATUS, so a shell whose every lane FAILED
            # still read green on /api/v1/ops/deadman. Measured 2026-08-30:
            # 11 of 15 shell feeds carried FAIL lanes in `note` while the board
            # reported 0 of 150 loops overdue. Liveness is not health.
            "status": ("lanes_failing" if failing else "success"),
            "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint urllib-request-on-railway)
        _rq.post("http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-surface-truth-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[surface-truth] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn, *a) -> list[dict]:
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       "lane crashed: %s: %s" % (type(e).__name__, str(e)[:120]),
                       critical=True)]


def _run_tick() -> dict:
    canon = _canon_floor()
    if not canon:
        # No canon = nothing to compare against. Every lane is indeterminate;
        # say so loudly rather than rendering a green board.
        lanes = [{"id": "canon", "name": "0 · canon available",
                  "checks": [_check("canon_missing", "ai_surface_canon PINNED "
                                    "public.facilities", None,
                                    "canon unavailable — no lane can be judged",
                                    critical=True)]}]
    else:
        lanes = [
            {"id": "served_text", "name": "1 · served agent text",
             "checks": _safe_lane(_lane_served_text, canon)},
            {"id": "served_manifests", "name": "2 · served manifests",
             "checks": _safe_lane(_lane_served_manifests, canon)},
            {"id": "repo_vs_served", "name": "3 · repo vs served (fence honesty)",
             "checks": _safe_lane(_lane_repo_vs_served, canon)},
            {"id": "emitter_sources", "name": "4 · emitter sources",
             "checks": _safe_lane(_lane_emitter_sources, canon)},
        ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join("%s=%s" % (ln["id"], ln["verdict"]) for ln in lanes)
    out = {
        "ok": True,
        "shell": "surface-truth-30",
        "origin": ORIGIN,
        "canon_floor": canon,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


@surface_truth_master_shell_bp.route("/api/v1/admin/surface-truth/master-tick",
                                     methods=["GET", "POST"])
def master_tick():
    if _disabled():
        # ★404, never 5xx (2026-08-12). The CF worker's proxyWithRetry reads ANY
        # 5xx from Railway as a dead origin and fails the site over to the stale
        # Render backend; two within 10s break the site for 30s. So flipping
        # SURFACE_TRUTH_SHELL_DISABLE=1 — an operator turning off ONE diagnostic
        # shell — could take the whole site to the stale origin. graph_spine
        # already documents this; this shell was the last one still returning
        # 404, and it was found by the audit that wrote shell #63.
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    return jsonify(_run_tick())


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@surface_truth_master_shell_bp.route("/admin/surface-truth", methods=["GET"])
@surface_truth_master_shell_bp.route("/api/v1/admin/surface-truth", methods=["GET"])
def dashboard():
    if _disabled():
        # 404, never 5xx — see the note on the JSON route above.
        return "<h1>Surface Truth</h1><p>disabled</p>", 404
    if not _admin_ok():
        return "<h1>401</h1><p>admin key required</p>", 401
    t = _run_tick()
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in t["lanes"]:
        rows.append("<tr><td><b>%s</b></td><td style='color:%s'><b>%s</b></td>"
                    "<td>%s</td></tr>"
                    % (_esc(ln["name"]), color.get(ln["verdict"], "#eab308"),
                       _esc(ln["verdict"]),
                       "<br>".join(
                           "%s <i>%s</i> — %s"
                           % ({True: "✓", False: "✗"}.get(k["pass"], "?"),
                              _esc(k["name"]), _esc(k["detail"]))
                           for k in ln["checks"])))
    return ("<html><head><title>Surface Truth #30</title>"
            "<meta http-equiv='refresh' content='60'></head>"
            "<body style='font-family:system-ui;max-width:1100px;margin:24px auto'>"
            "<h1>Surface Truth <small>#30</small></h1>"
            "<p>Canon floor <b>%s</b> · origin <code>%s</code> · %s</p>"
            "<p><small>Checks the bytes an agent actually receives, not the repo. "
            "A lane never reads PASS when it could not check.</small></p>"
            "<table cellpadding='8' style='border-collapse:collapse;width:100%%'>"
            "<tr><th align='left'>lane</th><th align='left'>verdict</th>"
            "<th align='left'>checks</th></tr>%s</table>"
            "<p><small>refreshes 60s · kill SURFACE_TRUTH_SHELL_DISABLE=1</small></p>"
            "</body></html>"
            % (_esc(t.get("canon_floor")), _esc(t["origin"]),
               _esc(t["generated_at"]), "".join(rows))), 200
