"""Phase 300 (Phase R-4) — public /brain page.

Surfaces what the Brain v2 self-learning loop has done recently:
  • Layer + activation state
  • Proposed fixes (pending vs approved)
  • Recent learning attempts + outcomes
  • Plain-English explanation of how the loop works

This is the "show your work" surface — turns the autonomy into a visible
asset. Press / AI evaluators / future hires read this and see a system
that monitors itself.

Read-only. No auth. Cached 60s. Pulls from in-memory state in
routes/brain_v2_layer4.py so this page is always consistent with the
machine-readable /api/v1/brain/* endpoints.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from html import escape as _h
from urllib.parse import quote as _quote
from flask import Blueprint, Response, request

brain_v2_public_bp = Blueprint("brain_v2_public", __name__)

# ── Claim Loop step 5 (2026-08-22): the week line + the last claims ─────
# The public page leads with what DC Hub claimed about itself and whether it
# stood. Claim WORDING is screened through the media bridge's _SELF_CRITICAL
# filter — the SAME pattern the brain→media bridge uses, imported so the two
# cannot drift. The fallback below is a verbatim copy used ONLY if that
# import fails (tests/test_ops_claims_feed.py pins the two patterns equal).
# The machine feed (/api/v1/ops/claims) strips nothing — only this HTML
# page is positive-leaning; the counts it shows are never hidden on zero.
import re as _re
_SELF_CRITICAL_FALLBACK = _re.compile(
    r"verified vs|unverified|discovery pile|backlog|dormant|\bleak|divergence|"
    r"_fallback|fallback|error|\bfail|refut|stale|drift|\bgap\b|unconverted|"
    r"unaddressed|not growing|0 (?:converted|paid)|leakage|over-?count|"
    r"\(unknown\)|orphan|broken|missing|defect|\bbug\b|"
    # internal-ops / negative vocabulary (opened all areas 2026-06-24):
    r"abort|exception|timeout|\bslow\b|retry|collision|\bflap|replica|"
    r"runtime_error|heartbeat|self-?heal|deprecat|schema|serializer|"
    r"allow-?list|\bDTO\b|detector|normaliz|dedup|merge[d ]|INSERT|"
    r"churn|deficit|shortfall|friction|activation gap|paid-plan|"
    r"decline|dropp|deadlock|stuck|degrad|outage|unreachable|throttl|"
    r"rate.?limit|429|memory high|aborted|null\b|mismatch|regression",
    _re.I,
)
try:
    from routes.brain_media_bridge import _SELF_CRITICAL as _SELF_CRITICAL_RE
except Exception:  # noqa: BLE001 — fallback: the local copy of the same pattern
    _SELF_CRITICAL_RE = _SELF_CRITICAL_FALLBACK

_CLAIMS_SHOWN = 10
_WITHHELD = "internal finding · wording withheld"


def _claims_section_html(feed, limit: int = _CLAIMS_SHOWN) -> str:
    """The week line ("N claims confirmed at horizon this week · as of
    <date>") and the last `limit` claims (kind, subject, outcome, when).

    Renders "0" on a measured zero and "—" when the ledger could not be
    read — never hides a number. A claim whose wording trips the
    self-critical filter keeps its row (kind, outcome and time are facts)
    with the wording withheld."""
    ok = (isinstance(feed, dict) and bool(feed.get("ok"))
          and isinstance(feed.get("week"), dict))
    week = feed.get("week") if ok else {}

    def _n(key):
        v = week.get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            return "—"
        return str(v)

    as_of = (str(week.get("as_of") or "")[:10] if ok
             else datetime.now(timezone.utc).date().isoformat())
    confirmed = _n("confirmed")
    noun = "claim" if confirmed == "1" else "claims"
    head = f"{confirmed} {noun} confirmed at horizon this week · as of {as_of}"
    if ok:
        sub = (f"{_n('shipped')} shipped · {_n('open')} awaiting horizon · "
               f"{_n('refuted_kept')} refuted (kept) · {_n('retracted')} retracted · "
               f"{_n('unobserved')} unobserved")
    else:
        sub = "claim ledger not readable right now — a dash is not a zero"
    parts = ['<div class="banner claims"><b>', _h(head), '</b><div>', _h(sub),
             ' · every claim is pre-registered with an expectation and judged at '
             'horizon by a verifier, never by its author · '
             '<a href="/api/v1/ops/claims">machine feed</a></div>']
    rows = [c for c in (feed.get("claims") or []) if isinstance(c, dict)][:limit] if ok else []
    if rows:
        parts.append('<table class="claims-table"><thead><tr><th>kind</th>'
                     '<th>claim</th><th>outcome</th><th>when (UTC)</th></tr></thead><tbody>')
        for c in rows:
            subject = str(c.get("subject") or "")
            statement = str(c.get("statement") or "")
            text = subject or statement
            if statement and statement != subject and len(statement) <= 80:
                text = f"{subject} — {statement}" if subject else statement
            if _SELF_CRITICAL_RE.search(subject) or _SELF_CRITICAL_RE.search(statement):
                text = _WITHHELD
            outcome = str(c.get("outcome") or "open")
            when = str(c.get("outcome_at") or c.get("shipped_at") or "")[:16].replace("T", " ")
            parts.append(
                f'<tr><td>{_h(str(c.get("kind") or ""))}</td><td>{_h(text[:140])}</td>'
                f'<td class="oc oc-{_h(outcome)}">{_h(outcome)}</td><td>{_h(when)}</td></tr>')
        parts.append('</tbody></table>')
    else:
        parts.append('<div class="claims-empty">— no shipped claims to show</div>')
    parts.append('</div>')
    return "".join(parts)


def _pub_admin_ok():
    # r-brain-gate (2026-06-18): /brain is an internal ops view (file paths,
    # infra failures, admin URLs) — admin/internal-key only.
    _keys = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    _sent = (request.headers.get("X-Internal-Key")
             or request.headers.get("X-Admin-Key")
             or request.args.get("admin_key")
             or request.args.get("key")              # browser-friendly shorthand
             or request.cookies.get("dchub_admin")   # set once via ?key=, then bookmarkable
             or "").strip()
    return bool(_sent) and _sent in _keys


_PUB_ADMIN_ONLY_HTML = (
    "<!doctype html><meta charset=utf-8><title>DC Hub · internal</title>"
    "<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
    "background:#0a0a0a;color:#9a9a9a;display:flex;align-items:center;"
    "justify-content:center;height:90vh;text-align:center'>"
    "<div><h2 style='color:#e6e6e6;font-weight:300;letter-spacing:-.02em'>"
    "Internal console</h2><p>The DC Hub brain dashboard is admin-only.</p></div>")


@brain_v2_public_bp.after_request
def _pub_no_store(resp):
    # The public brain view (/brain-live, /brain/public) is sanitized and safe
    # to edge-cache; every other response on this blueprint is admin-only and
    # must never be cached.
    if request.path in ("/brain-live", "/brain/public"):
        resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=30"
    else:
        resp.headers["Cache-Control"] = "no-store, private"
    return resp


def _get_state():
    """Pull current Brain v2 state from the layer-4 module. Defensive — if
    the module isn't loaded for any reason, returns sensible defaults.

    Phase S (2026-05-12): prefer the Postgres store when available so the
    /brain page shows the full cross-worker, cross-deploy history instead
    of just the per-worker in-memory snapshot the layer4 module happens
    to hold. Falls back to in-memory if the store is offline.

    Phase MM (2026-05-15): 60-second in-memory cache so the /brain
    dashboard isn't 3s on every hit. The data is admin-facing + cron-
    refreshed every hour; 60s staleness is fine, 3s page load isn't.
    """
    import time as _time
    global _STATE_CACHE
    try:
        _STATE_CACHE
    except NameError:
        _STATE_CACHE = {"payload": None, "ts": 0}
    if _STATE_CACHE["payload"] and (_time.time() - _STATE_CACHE["ts"]) < 60:
        return _STATE_CACHE["payload"]

    try:
        from routes.brain_v2_layer4 import (
            _proposed_fixes, _learning_log,
            ANTHROPIC_API_KEY, BRAIN_MODEL,
            _STORE_OK, compute_brain_verdict, _brain_age_min,
            _cached_actionable_count,
        )
    except Exception as e:
        return {"loaded": False, "active": False, "model": "?",
                "proposed": [], "log": [], "persistence": [],
                "verdict": "unknown",
                "verdict_detail": f"Brain v2 module failed to load: {str(e)[:160]}",
                "error": str(e)[:200]}

    proposed = list(_proposed_fixes)
    # r67: read the DURABLE learning log from Postgres (brain_v2_store) so it
    # survives gunicorn-worker restarts and reflects the real recent stream — the
    # in-memory _learning_log resets per process (and was showing weeks-stale rows
    # on whichever worker answered). Fall back to in-memory if the store is down,
    # so this can never blank the panel.
    log = []
    if _STORE_OK:
        try:
            from routes import brain_v2_store as _bstore
            log = _bstore.list_log(limit=40) or []
        except Exception:
            log = []
    if not log:
        log = list(_learning_log)
    persistence: list = []
    last_run_at = None
    if _STORE_OK:
        try:
            from routes import brain_v2_store as _store
            proposed = _store.list_proposals(limit=200)
            log = _store.list_log(limit=200)
            persistence = _store.most_persistent_unfixed(min_count=2, limit=20)
            _m = _store.get_meta("last_run_at")
            last_run_at = _m.get("value") if _m else None
        except Exception:
            pass

    # Phase RR (2026-05-14): compute the same honest verdict the
    # /api/v1/brain/status API returns, so the dashboard headline tells
    # the truth ("Healthy — nothing to fix") instead of looking broken
    # whenever proposals == 0.
    _last_t = log[-1].get("t") if log else None
    try:
        _actionable = _cached_actionable_count()
    except Exception:
        _actionable = 0
    verdict, verdict_detail = compute_brain_verdict(
        bool(ANTHROPIC_API_KEY),
        _brain_age_min(last_run_at),
        _brain_age_min(_last_t),
        len(proposed),
        len(log),
        actionable_count=_actionable,
    )
    result = {
        "loaded": True,
        "active": bool(ANTHROPIC_API_KEY),
        "actionable_findings_count": _actionable,
        "model": BRAIN_MODEL,
        "store_backed": bool(_STORE_OK),
        "proposed": proposed,
        "log": log,
        "persistence": persistence,
        "verdict": verdict,
        "verdict_detail": verdict_detail,
    }
    # Phase MM: cache for 60s — saves 3 DB round-trips on /brain hits.
    try:
        _STATE_CACHE["payload"] = result
        _STATE_CACHE["ts"] = _time.time()
    except Exception:
        pass
    return result


def _color_for(item: dict) -> str:
    if item.get("approved"): return "var(--green)"
    if item.get("approval_count", 0) >= 1: return "var(--amber)"
    return "var(--tx2)"


_BRAIN_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub · Brain v2 — self-learning autonomy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="What the DC Hub self-learning system has detected, proposed, and learned in the last 24 hours. Live transparency on autonomous bug-fixing.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/brain">
<meta property="og:title" content="DC Hub · Brain v2 — self-learning autonomy">
<meta property="og:description" content="Live transparency on how the DC Hub system monitors itself, learns new bug patterns, and proposes fixes via Anthropic's Claude API.">
<meta property="og:url" content="https://dchub.cloud/brain">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "TechArticle",
  "headline": "DC Hub Brain v2 — autonomous self-learning bug-fix loop",
  "description": "Live status page for the DC Hub Brain v2 system. Reads its own healer findings, asks Claude API for fixes on novel patterns, validates suggestions, and applies them via a 2-cycle approval gate. Public transparency dashboard.",
  "url": "https://dchub.cloud/brain",
  "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "isAccessibleForFree": true
}
</script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--bg2:#0f1119;--card:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--tx3:#6b7280;--green:#10b981;--amber:#f59e0b;--red:#ef4444;--acc:#6366f1;--acc-light:#818cf8;--gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);}
*{box-sizing:border-box}
body{font-family:'Instrument Sans',system-ui;background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1100px;margin:0 auto;padding:3rem 1.5rem;}
.eyebrow{font-family:'JetBrains Mono',monospace;font-size:0.74rem;color:var(--acc);text-transform:uppercase;letter-spacing:0.14em;margin-bottom:0.6rem;}
h1{font-size:clamp(2.4rem,5vw,3.2rem);margin:0 0 0.7rem;font-weight:800;letter-spacing:-0.025em;line-height:1.05;}
h1 .grad{background:var(--gradient);-webkit-background-clip:text;background-clip:text;color:transparent;}
.lede{color:var(--tx2);font-size:1.1rem;max-width:760px;margin:0 0 2.5rem;}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin:2rem 0;}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.2rem 1.3rem;}
.kpi .v{font-family:'JetBrains Mono',monospace;font-size:2rem;font-weight:800;line-height:1;}
.kpi .v.green{color:var(--green);}.kpi .v.red{color:var(--red);}.kpi .v.amber{color:var(--amber);}
.kpi .l{color:var(--tx2);font-size:0.75rem;margin-top:0.55rem;text-transform:uppercase;letter-spacing:0.08em;}
.section{margin:2.5rem 0;}
.section-title{font-size:1.15rem;font-weight:700;margin:0 0 1rem;letter-spacing:-0.01em;}
.card-list{display:grid;gap:0.8rem;}
.proposal{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.1rem 1.3rem;}
.proposal .head{display:flex;justify-content:space-between;align-items:center;gap:1rem;margin-bottom:0.6rem;}
.proposal .label{font-family:'JetBrains Mono',monospace;font-size:0.74rem;color:var(--tx2);text-transform:uppercase;letter-spacing:0.08em;}
.proposal .badge{display:inline-block;padding:0.2rem 0.6rem;border-radius:99px;font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;}
.proposal .badge.approved{background:rgba(16,185,129,0.12);color:var(--green);border:1px solid rgba(16,185,129,0.25);}
.proposal .badge.pending{background:rgba(245,158,11,0.12);color:var(--amber);border:1px solid rgba(245,158,11,0.25);}
.proposal .find,.proposal .replace{font-family:'JetBrains Mono',monospace;font-size:0.82rem;padding:0.55rem 0.8rem;border-radius:6px;margin:0.35rem 0;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);overflow-x:auto;}
.proposal .find{border-left:3px solid var(--red);}
.proposal .replace{border-left:3px solid var(--green);}
.proposal .meta{color:var(--tx3);font-size:0.78rem;margin-top:0.6rem;}
.log-row{display:flex;justify-content:space-between;gap:1rem;padding:0.55rem 0.8rem;border-bottom:1px solid var(--bd);font-size:0.84rem;}
.log-row:last-child{border-bottom:none;}
.log-time{font-family:'JetBrains Mono',monospace;color:var(--tx3);font-size:0.76rem;flex:0 0 auto;}
.log-issue{color:var(--tx2);flex:1;min-width:0;}
.log-outcome{font-family:'JetBrains Mono',monospace;font-size:0.78rem;}
.log-outcome.ok{color:var(--green);}.log-outcome.warn{color:var(--amber);}.log-outcome.err{color:var(--red);}
.explainer{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:1.4rem 1.5rem;color:var(--tx2);font-size:0.95rem;}
.explainer p{margin:0.7rem 0;}
.explainer code{background:var(--bg);padding:2px 6px;border-radius:4px;color:var(--tx);font-family:'JetBrains Mono',monospace;font-size:0.88rem;}
.explainer a{color:var(--acc-light);text-decoration:none;border-bottom:1px dotted rgba(129,140,248,0.5);}
.foot{margin-top:3rem;color:var(--tx3);font-size:0.8rem;text-align:center;}
.foot a{color:var(--tx2);}
.empty{padding:1.4rem;text-align:center;color:var(--tx3);font-size:0.92rem;background:var(--card);border:1px dashed var(--bd);border-radius:10px;}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">DC Hub · Brain v2 · live</div>
  <h1>The system is <span class="grad">learning to fix itself</span>.</h1>
  <p class="lede">When the DC Hub healer detects a frontend bug it doesn't already know how to fix, it asks Anthropic's Claude API for a safe substitution, validates the suggestion against 5 safety rules, and queues it for human-or-automation review. This page shows every proposal in real time.</p>

  <div class="kpis">
    <div class="kpi"><div class="v {{status_class}}">{{status_text}}</div><div class="l">Layer 4 status</div></div>
    <div class="kpi"><div class="v {{code_proposals_class}}">{{code_proposals_count}}</div><div class="l">Code proposals (Layer 5)</div></div>
    <div class="kpi"><div class="v {{actionable_class}}">{{actionable_count}}</div><div class="l">Open actionable findings</div></div>
    <div class="kpi"><div class="v">{{total_proposals}}</div><div class="l">HTML fixes (Layer 4)</div></div>
    <div class="kpi"><div class="v green">{{approved_count}}</div><div class="l">HTML fixes approved (≥2 cycles)</div></div>
    <div class="kpi"><div class="v amber">{{pending_count}}</div><div class="l">HTML fixes pending review</div></div>
    <div class="kpi"><div class="v">{{log_count}}</div><div class="l">Learning attempts (logged)</div></div>
  </div>

  {{verdict_banner}}

  {{grade_block}}

  {{control_room}}

  <div class="section">
    <h2 class="section-title">Layer 5 — Code Proposals</h2>
    <p style="color:var(--tx2);font-size:14px;margin:-6px 0 14px">Backend/source-level fixes the brain wrote for real Python files (not HTML data-cells). Each is stored in <code>brain_proposed_code_fixes</code>, never auto-applied — high-confidence ones open a draft PR for human review.</p>
    <div class="card-list">
{{code_proposals_html}}
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">Layer 4 — HTML fixes</h2>
    <p style="color:var(--tx2);font-size:14px;margin:-6px 0 14px">Stale-string / placeholder substitutions on the public site. "0 pending" here means nothing is broken to fix — the real backend work lives in Layer 5 above.</p>
    <div class="card-list">
{{proposals_html}}
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">Stuck-issue worklist</h2>
    <p style="color:var(--tx2);font-size:14px;margin:-6px 0 14px">Issues the healer has seen many cycles AND that have NOT yet produced a successful proposal. The brain prioritizes these on the next learn pass — this is the "learn from errors it misses" surface.</p>
    <div style="background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;">
{{persistence_html}}
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">Learning log</h2>
    <div style="background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;">
{{log_html}}
    </div>
  </div>

  <div class="section">
    <h2 class="section-title">How the loop works</h2>
    <div class="explainer">
      <p><strong>Every hour</strong>, a GitHub Actions cron triggers <code>POST /api/v1/brain/learn</code>.</p>
      <p>The endpoint pulls current findings from <code>/api/v1/heal/findings</code> — the healer's view of every detected stale string, broken link, or placeholder leak across the public site.</p>
      <p>For any issue NOT in the master-heal workflow's hand-curated FIX_MAP, the loop calls Claude with the issue label, the page URL, the count, and a tight 1.5KB HTML snippet. The system prompt forbids modifying typography in meta descriptions or prose — only data-cell placeholders are eligible.</p>
      <p>Claude returns a (find, replace, rationale) JSON object. Five validators reject the suggestion if: find is shorter than 5 chars, replace introduces HTML/JS, the substitution is a no-op, replace is too long, or rationale is missing. Surviving proposals get an approval counter.</p>
      <p><strong>A proposal is only auto-applied after it crosses a 2-cycle approval gate</strong> — i.e., the exact same fix is proposed twice on independent hourly runs. Single-shot Claude hallucinations stay in the queue but never make it to the live site.</p>
      <p>Machine-readable: <a href="/api/v1/brain/status">/api/v1/brain/status</a>, <a href="/api/v1/brain/proposed-fixes">/api/v1/brain/proposed-fixes</a>, <a href="/api/v1/brain/proposed-fixes?approved=true">approved subset</a>. The master-heal cron consumes the approved subset and merges entries into its runtime FIX_MAP.</p>
    </div>
  </div>

  <p class="foot">
    As of {{as_of}}.
    <a href="/freshness">Data freshness</a> · <a href="/dcpi">DCPI</a> · <a href="/api/v1/brain/proposed-code">Code proposals (Layer 5)</a> · <a href="/audit/">Audit</a>
  </p>
</div>
</body>
</html>"""


# r43-H (2026-05-28): /brain made 4 sequential in-process sub-requests
# (self-assessment, value-shipped, page-integrity, lifecycle/findings), each
# expensive, stacking to ~16s p95 (the latency tracker flagged it). It's a
# dashboard — a few minutes stale is fine — so memoize each sub-call by path. On a cache
# hit the expensive .get() is skipped entirely; the page logic is unchanged.
class _CachedResp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
    def get_json(self):
        return self._json

_INTERNAL_GET_CACHE: dict = {}
_INTERNAL_GET_TTL = 300   # 5 min — /brain's data barely changes; 90s re-cooled before the next visit

def _cached_internal_get(path, client, ttl=_INTERNAL_GET_TTL):
    import time as _t
    now = _t.time()
    hit = _INTERNAL_GET_CACHE.get(path)
    if hit and hit[0] > now:
        return hit[1]
    try:
        rv = client.get(path)
        try:
            jd = rv.get_json()
        except Exception:
            jd = None
        resp = _CachedResp(rv.status_code, jd)
    except Exception:
        resp = _CachedResp(0, None)
    _INTERNAL_GET_CACHE[path] = (now + ttl, resp)
    return resp


# r-brain-public (2026-06-18): the PUBLIC, sanitized brain-evolution view.
# /brain itself is admin-only (it renders file paths, source URLs, stuck-issue
# worklists, raw learning logs). This handler exposes ONLY safe aggregates — the
# health verdict LABEL (never verdict_detail), the self-assessment letter grade +
# component scores, and high-level evolution counts — all pulled from the
# already-public /api/v1/brain/{self-assessment,evolution} JSON. It OWNS the
# /brain-live alias (moved off the gated handler) so the distribute.html "Watch
# brain evolve" link + the brain's own page-monitors get a 200, not the 403.
_PUBLIC_BRAIN_HEAD = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    '<title>DC Hub · Brain — watch it evolve</title>'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="description" content="Live, public view of the DC Hub self-learning system: health verdict, self-assessment grade, and how many issues it has autonomously resolved.">'
    '<meta name="robots" content="index,follow">'
    '<link rel="canonical" href="https://dchub.cloud/brain-live">'
    '<meta property="og:title" content="DC Hub · Brain — watch it evolve">'
    '<meta property="og:description" content="Live public scorecard for the DC Hub autonomous self-learning loop.">'
    '<meta property="og:url" content="https://dchub.cloud/brain-live">'
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">'
    "<style>"
    ":root{--bg:#0a0a12;--bg2:#0f1119;--card:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--tx3:#6b7280;--green:#10b981;--amber:#f59e0b;--red:#ef4444;--acc:#6366f1;--acc-light:#818cf8;--gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);}"
    "*{box-sizing:border-box}"
    "body{font-family:'Inter',system-ui;background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased;}"
    ".wrap{max-width:1000px;margin:0 auto;padding:3rem 1.5rem;}"
    ".eyebrow{font-family:'JetBrains Mono',monospace;font-size:0.74rem;color:var(--acc);text-transform:uppercase;letter-spacing:0.14em;margin-bottom:0.6rem;}"
    "h1{font-size:clamp(2.2rem,5vw,3rem);margin:0 0 0.7rem;font-weight:800;letter-spacing:-0.025em;line-height:1.05;}"
    "h1 .grad{background:var(--gradient);-webkit-background-clip:text;background-clip:text;color:transparent;}"
    ".lede{color:var(--tx2);font-size:1.08rem;max-width:720px;margin:0 0 1rem;}"
    ".banner{background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.3);border-radius:10px;padding:1rem 1.25rem;margin:0.4rem 0 1.5rem;}"
    ".banner b{font-size:0.95rem;} .banner div{color:var(--tx2);font-size:0.9rem;margin-top:0.25rem;}"
    ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1rem;margin:1.4rem 0;}"
    ".kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.2rem 1.3rem;}"
    ".kpi .v{font-family:'JetBrains Mono',monospace;font-size:2rem;font-weight:800;line-height:1;}"
    ".kpi .v.green{color:var(--green);}.kpi .v.red{color:var(--red);}.kpi .v.amber{color:var(--amber);}"
    ".kpi .l{color:var(--tx2);font-size:0.75rem;margin-top:0.55rem;text-transform:uppercase;letter-spacing:0.08em;}"
    ".section{margin:2rem 0;} .section-title{font-size:1.15rem;font-weight:700;margin:0 0 1rem;}"
    ".explainer{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:1.4rem 1.5rem;color:var(--tx2);font-size:0.95rem;}"
    ".explainer p{margin:0.7rem 0;}"
    ".foot{margin-top:3rem;color:var(--tx3);font-size:0.8rem;text-align:center;} .foot a{color:var(--tx2);}"
    ".banner.claims a{color:#c4b5fd;} .claims-table{width:100%;border-collapse:collapse;font-size:0.84rem;margin-top:0.8rem;}"
    ".claims-table th{text-align:left;color:var(--tx3);font-weight:500;padding:0.25rem 0.5rem;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.06em;}"
    ".claims-table td{padding:0.35rem 0.5rem;border-top:1px solid var(--bd);color:var(--tx2);vertical-align:top;}"
    ".oc{font-family:'JetBrains Mono',monospace;} .oc-confirmed{color:var(--green);} .oc-refuted{color:var(--red);} .oc-retracted{color:var(--amber);} .oc-unobserved,.oc-open{color:var(--tx3);}"
    ".claims-empty{color:var(--tx3);margin-top:0.6rem;font-size:0.9rem;}"
    "</style></head>"
)


@brain_v2_public_bp.route("/brain-live", methods=["GET"])  # PUBLIC sanitized view — distribute.html "Watch brain evolve" + page-monitors land here.
@brain_v2_public_bp.route("/brain/public", methods=["GET"])
def brain_public_page():
    """PUBLIC, sanitized brain-evolution view — no admin key required."""
    from flask import current_app

    # --- health verdict LABEL only (never verdict_detail) ---
    try:
        verdict = (_get_state() or {}).get("verdict", "unknown")
    except Exception:
        verdict = "unknown"
    _LABELS = {
        "healthy":         ("HEALTHY",    "green", "Healthy — the self-learning loop is running."),
        "healthy_quiet":   ("HEALTHY",    "green", "Healthy — quiet because there's nothing to fix."),
        "healthy_working": ("WORKING",    "green", "Healthy — actively proposing fixes."),
        "healthy_backlog": ("HEALTHY",    "green", "Healthy — clean; open work is queued."),
        "warming_up":      ("WARMING UP", "amber", "Warming up — first learn pass pending."),
        "stalled":         ("STALLED",    "red",   "Stalled — the learn loop stopped firing."),
        "dormant":         ("DORMANT",    "amber", "Dormant — model key not set."),
        "unknown":         ("LIVE",       "amber", "Status updating…"),
    }
    status_text, status_class, headline = _LABELS.get(verdict, _LABELS["unknown"])

    # --- safe numbers from the already-public JSON endpoints ---
    sa, evo = {}, {}
    try:
        with current_app.test_client() as _client:
            _r = _cached_internal_get("/api/v1/brain/self-assessment", _client)
            if _r.status_code == 200:
                sa = _r.get_json() or {}
            _r = _cached_internal_get("/api/v1/brain/evolution", _client)
            if _r.status_code == 200:
                evo = _r.get_json() or {}
    except Exception:
        pass

    def _i(v):
        try:
            return int(v)
        except Exception:
            return 0

    evo_score = evo.get("evolution_score")
    fr7       = _i(evo.get("findings_resolved_7d"))
    fr30      = _i(evo.get("findings_resolved_30d"))
    aa24      = _i(evo.get("autonomous_actions_24h"))
    dt30      = _i(evo.get("decisions_taken_30d"))
    _l5d      = evo.get("layer_5_proposals_30d") or {}
    l5        = _i(_l5d.get("submitted") if isinstance(_l5d, dict) else _l5d)

    parts = [_PUBLIC_BRAIN_HEAD]
    parts.append('<body><div class="wrap">')
    parts.append('<div class="eyebrow">DC Hub · Brain · live</div>')
    parts.append('<h1>Watch the system <span class="grad">learn to fix itself</span>.</h1>')
    parts.append('<p class="lede">DC Hub runs an autonomous self-learning loop: it watches its own surfaces, learns new failure patterns, proposes fixes through Claude, and validates them before anything ships. This is the live, public scorecard.</p>')
    # ★2026-08-22 Claim Loop step 5 — the week line LEADS the page: what
    # the system claimed about itself this week and whether it held. Read
    # straight from the ledger (not via HTTP); the public feed's kill switch
    # blanks it to "—" here too.
    try:
        from routes import ops_claims as _oc
        _feed = None if _oc._disabled() else _oc.read_feed(limit=_CLAIMS_SHOWN)
    except Exception:
        _feed = None
    parts.append(_claims_section_html(_feed))
    parts.append(f'<div class="banner"><b>{_h(headline)}</b><div>Reflects the most recent learn pass.</div></div>')

    # Discoverable link to the brain INNOVATION DASHBOARD — the one
    # browser-openable surface for the brain's verified, adversarially-refuted
    # analysis (self-agenda / investigations / proposals) with per-item grade +
    # approve. Carry the same admin key the operator opened /brain with so the link
    # works without re-auth. Served under /api/ (the CF worker passes /api/* through
    # unconditionally; /brain/innovation is owned by a different surface + /brain/*
    # subpaths hit CF Error-1000). (r-innov-link 2026-06-19)
    _ak = (request.args.get("admin_key") or "").strip()
    _akq = f"?admin_key={_quote(_ak)}" if _ak else ""
    parts.append(
        '<div class="banner" style="border-color:rgba(99,102,241,0.45);'
        'background:rgba(99,102,241,0.10);">'
        f'<b><a href="/api/v1/brain/innovation/dashboard{_akq}" style="color:#c4b5fd;text-decoration:none;">'
        '★ Innovation dashboard →</a></b>'
        '<div style="color:var(--tx2);">What the brain is thinking — its self-chosen '
        'agenda, verified investigations and ranked proposals, each with a grade + '
        'approve control.</div></div>'
    )

    parts.append('<div class="kpis">')
    parts.append(f'<div class="kpi"><div class="v {status_class}">{_h(status_text)}</div><div class="l">Loop status</div></div>')
    if evo_score is not None:
        parts.append(f'<div class="kpi"><div class="v">{_i(evo_score)}</div><div class="l">Evolution score · /100</div></div>')
    parts.append(f'<div class="kpi"><div class="v green">{fr7}</div><div class="l">Findings resolved · 7d</div></div>')
    parts.append(f'<div class="kpi"><div class="v">{fr30}</div><div class="l">Findings resolved · 30d</div></div>')
    parts.append(f'<div class="kpi"><div class="v">{aa24}</div><div class="l">Autonomous actions · 24h</div></div>')
    parts.append(f'<div class="kpi"><div class="v">{dt30}</div><div class="l">Decisions taken · 30d</div></div>')
    parts.append(f'<div class="kpi"><div class="v">{l5}</div><div class="l">Code proposals · 30d</div></div>')
    parts.append('</div>')

    grade = sa.get("grade")
    if grade:
        _gc = {"A": "green", "B": "green", "C": "amber", "D": "red", "F": "red", "I": "amber"}.get(str(grade), "amber")
        _ws = sa.get("weighted_score")
        _score_txt = f" · {_h(str(round(_ws, 1)))}/100" if isinstance(_ws, (int, float)) else ""
        parts.append('<div class="section"><h2 class="section-title">Self-assessment</h2><div class="kpis">')
        parts.append(f'<div class="kpi"><div class="v {_gc}" style="font-size:2.6rem">{_h(str(grade))}</div><div class="l">Letter grade{_score_txt}</div></div>')
        _comp = sa.get("component_scores") or {}
        # Closed-by-default: only these known component keys ever render on the
        # public page, regardless of any field the upstream JSON adds later.
        _labels = {"fix_success": "fix success", "rejection": "rejection rate", "cron_health": "cron health", "volume": "volume", "memory": "memory depth"}
        if isinstance(_comp, dict):
            for _k, _lbl in _labels.items():
                if _k not in _comp:
                    continue
                parts.append(f'<div class="kpi"><div class="v">{_i(_comp[_k])}<span style="font-size:0.9rem;color:var(--tx3)">/4</span></div><div class="l">{_h(_lbl)}</div></div>')
        parts.append('</div></div>')

    parts.append('<div class="section"><h2 class="section-title">How the loop works</h2><div class="explainer">')
    parts.append('<p><strong>Continuously</strong>, the system reads its own health findings — stale data, broken links, regressions — across the public site.</p>')
    parts.append("<p>For anything it doesn't already know how to fix, it asks Claude for a safe, narrow change, then runs that through a set of safety validators.</p>")
    parts.append('<p>A fix is only applied after it clears a multi-cycle approval gate — proposed independently more than once. One-shot suggestions stay queued and never reach the live site.</p>')
    parts.append('</div></div>')

    parts.append('<p class="foot">Live · DC Hub (dchub.cloud) · numbers update every learn pass · <a href="/dcpi">DCPI</a> · <a href="/freshness">Data freshness</a></p>')
    parts.append('</div></body></html>')
    return Response("".join(parts), mimetype="text/html")


def _control_room_html():
    """Self-contained, fail-safe 'autonomy control room' block for /brain:
    the last full master-tick step health, the LLM reasoning lane's latest
    typed decisions (propose-only), and the maturing-organ counts (#3/#4/#7/#8).
    Reads the DB directly (the in-process test client can't auth the admin
    master-tick endpoint). Returns '' on ANY error so the page never breaks."""
    try:
        import os as _os, json as _json
        import psycopg2 as _pg
        dsn = _os.environ.get("DATABASE_URL")
        if not dsn:
            return ""
        conn = _pg.connect(dsn)
        cur = conn.cursor()

        def _q(sql, args=None):
            # NB: call execute(sql) with NO args when there are none — passing
            # () makes psycopg2 %-substitute and trip on the literal % in LIKE.
            try:
                cur.execute(sql, args) if args else cur.execute(sql)
                return cur.fetchall()
            except Exception:
                try: conn.rollback()
                except Exception: pass
                return []

        mt = _q("SELECT ran_at, report FROM brain_master_ticks "
                "WHERE tiers LIKE '%1%' OR tiers LIKE '%verify%' "
                "ORDER BY ran_at DESC LIMIT 1")
        steps = []; ran_at = None
        if mt:
            ran_at = mt[0][0]
            rep = mt[0][1] if isinstance(mt[0][1], dict) else (_json.loads(mt[0][1]) if mt[0][1] else {})
            steps = rep.get("steps", []) or []
        cands = _q("SELECT finding_issue, candidate_type, routing, confidence, action "
                   "FROM brain_reasoning_candidates ORDER BY created_at DESC LIMIT 6")
        unfix   = (_q("SELECT count(*) FROM brain_findings WHERE issue='pattern_unfixable_needs_rechannel'") or [[0]])[0][0]
        fcount  = (_q("SELECT count(*) FROM brain_findings WHERE issue LIKE 'forecast_%'") or [[0]])[0][0]
        ledger  = (_q("SELECT count(*) FROM brain_strategic_outcomes") or [[0]])[0][0]
        recallg = (_q("SELECT count(*) FROM brain_autopilot_actions WHERE error LIKE 'recall-gate:%'") or [[0]])[0][0]
        try: conn.close()
        except Exception: pass

        _nice = {"tier1.autopilot_run": "autopilot", "tier2.l15_auto_action": "L15 actions",
                 "tier2.l22_draft_prs": "L22 draft-PRs", "tier3.l23_lifecycle": "L23 lifecycle",
                 "tier3.promote_on_failure": "promote-on-failure #4", "tier3.forecast_findings": "forecast #7",
                 "tier3.strategic_ledger_stamp": "ledger #8", "tier3.reasoning_lane_drain": "reasoning #6",
                 "verify.autopilot": "verify"}
        pills = []
        for s in steps:
            if not isinstance(s, dict): continue
            nm = _nice.get(s.get("step"), s.get("step") or "?")
            dot = "var(--green)" if s.get("ok") else "var(--red)"
            ms = s.get("ms")
            tail = f" · {round(ms / 1000, 1)}s" if isinstance(ms, (int, float)) else ""
            pills.append(
                '<span style="display:inline-flex;align-items:center;gap:6px;background:var(--bg2);'
                'border:1px solid var(--bd);border-radius:99px;padding:0.3rem 0.7rem;font-size:0.78rem;color:var(--tx2);">'
                f'<span style="width:7px;height:7px;border-radius:50%;background:{dot};"></span>{_h(nm)}'
                f'<span style="color:var(--tx3);font-family:\'JetBrains Mono\',monospace;">{_h(tail)}</span></span>')
        pills_html = "".join(pills) or '<span style="color:var(--tx3)">No tick recorded yet.</span>'
        try:
            when = ran_at.strftime("%H:%M UTC · %b %d") if ran_at else "—"
        except Exception:
            when = "—"

        _route = {"pr_drafted_pending": ("draft PR", "var(--amber)", "rgba(245,158,11,0.12)"),
                  "human_digest": ("digest", "var(--acc-light)", "rgba(99,102,241,0.14)"),
                  "require_human": ("human", "var(--tx2)", "rgba(255,255,255,0.05)")}
        cblocks = []
        for row in cands:
            fi, ctype, routing, conf, action = row
            lbl, col, bg = _route.get(routing or "", ("review", "var(--tx2)", "rgba(255,255,255,0.05)"))
            try: cpct = f"{round(float(conf) * 100)}%"
            except Exception: cpct = "—"
            cblocks.append(
                '<div class="proposal"><div class="head">'
                f'<span class="label">{_h(fi or "?")}</span>'
                f'<span class="badge" style="background:{bg};color:{col};border:1px solid {bg};">{_h(ctype or "?")} &rarr; {_h(lbl)}</span>'
                '</div>'
                f'<div style="color:var(--tx);font-size:0.92rem;line-height:1.5;">{_h((action or "")[:240])}</div>'
                f'<div class="meta">confidence {cpct}</div></div>')
        cands_html = "\n".join(cblocks) or '<div class="empty">No reasoning candidates yet — the lane drains the top leverage-ranked findings each tick.</div>'

        return (
            '<div class="section">'
            '<h2 class="section-title">Autonomy control room</h2>'
            '<p style="color:var(--tx2);font-size:14px;margin:-6px 0 14px">A tiered self-improvement tick runs about every 2 hours. '
            'Below: the last full tick\'s step health, the LLM reasoning lane\'s latest typed decisions (propose-only — auto-merge is off), '
            'and the organs still arming as data accrues.</p>'
            '<div style="background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.1rem 1.3rem;margin-bottom:0.8rem;">'
            f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:0.74rem;color:var(--tx2);text-transform:uppercase;letter-spacing:0.08em;margin-bottom:0.7rem;">Last full tick &middot; {_h(when)}</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:0.5rem;">{pills_html}</div></div>'
            '<div class="kpis" style="margin:0.8rem 0;">'
            f'<div class="kpi"><div class="v amber">{unfix}</div><div class="l">Unfixable flagged (#4)</div></div>'
            f'<div class="kpi"><div class="v">{fcount}</div><div class="l">Forecast findings (#7)</div></div>'
            f'<div class="kpi"><div class="v">{ledger}</div><div class="l">Strategic outcomes (#8)</div></div>'
            f'<div class="kpi"><div class="v">{recallg}</div><div class="l">Recall-gate escalations (#3)</div></div>'
            '</div>'
            '<h3 style="font-size:1rem;font-weight:700;margin:1.2rem 0 0.7rem;">Reasoning lane &mdash; latest decisions (#6)</h3>'
            f'<div class="card-list">{cands_html}</div>'
            '</div>')
    except Exception:
        return ""


@brain_v2_public_bp.route("/brain", methods=["GET"])
@brain_v2_public_bp.route("/brain/", methods=["GET"])  # r-slash (2026-06-18): bare "/brain" is strict-slash → "/brain/" 404'd outright (no 308). Explicit trailing-slash alias.
def brain_page():
    if not _pub_admin_ok():
        return Response(_PUB_ADMIN_ONLY_HTML, mimetype="text/html", status=403)
    state = _get_state()
    control_room_html = _control_room_html()
    proposals = state["proposed"]
    log = state["log"][-30:]  # last 30 entries

    approved_count = sum(1 for p in proposals if p.get("approved"))
    pending_count = sum(1 for p in proposals if not p.get("approved"))

    # Phase RR (2026-05-14): headline the honest verdict, not a bare
    # "ACTIVE". "ACTIVE / 0 / 0 / 0" read as failure every time it was
    # shown — even though 0 proposals is the CORRECT result when the
    # healer's findings are clean. The verdict says which of the five
    # real states the brain is in, and the banner explains it in plain
    # English so nobody has to guess again.
    verdict = state.get("verdict", "unknown")
    verdict_detail = state.get("verdict_detail", "")
    _VERDICT_DISPLAY = {
        "healthy":         ("HEALTHY", "green",
                            "Healthy — the self-learning loop is running"),
        "healthy_quiet":   ("HEALTHY", "green",
                            "Healthy — quiet because there's nothing to fix"),
        "healthy_working": ("WORKING", "green",
                            "Healthy — actively proposing fixes"),
        # r-brain5 (2026-05-31): healthy_backlog was falling through to the
        # amber "UNKNOWN" default — making a perfectly healthy loop look
        # broken. Layer 4 correctly produces 0 HTML fixes; the open work is
        # backend/infra findings that route to Layer 5 + autopilot (shown in
        # their own tiles below). So this is GREEN, not UNKNOWN.
        "healthy_backlog": ("HEALTHY", "green",
                            "Healthy — Layer 4 is clean; open work routes to Layer 5 + autopilot"),
        "warming_up":      ("WARMING UP", "amber",
                            "Warming up — first learn pass pending"),
        "stalled":         ("STALLED", "red",
                            "Stalled — the learn loop stopped firing"),
        "dormant":         ("DORMANT", "amber",
                            "Dormant — ANTHROPIC_API_KEY not set"),
        "unknown":         ("UNKNOWN", "amber", "State unknown"),
    }
    status_text, status_class, verdict_headline = _VERDICT_DISPLAY.get(
        verdict, ("UNKNOWN", "amber", "State unknown"))
    _banner_bg = {"green": "rgba(16,185,129,0.10)",
                  "amber": "rgba(245,158,11,0.10)",
                  "red": "rgba(239,68,68,0.12)"}.get(status_class, "rgba(245,158,11,0.10)")
    _banner_bd = {"green": "rgba(16,185,129,0.4)",
                  "amber": "rgba(245,158,11,0.4)",
                  "red": "rgba(239,68,68,0.45)"}.get(status_class, "rgba(245,158,11,0.4)")
    verdict_banner = (
        f'<div style="background:{_banner_bg};border:1px solid {_banner_bd};'
        f'border-radius:10px;padding:1rem 1.25rem;margin:0.4rem 0 0.4rem;">'
        f'<div style="font-weight:700;font-size:0.95rem;margin-bottom:0.25rem;">'
        f'{_h(verdict_headline)}</div>'
        f'<div style="color:var(--tx2);font-size:0.9rem;line-height:1.5;">'
        f'{_h(verdict_detail)}</div></div>'
    )

    # Phase GG (2026-05-15): Bundle 4 — surface the self-assessment letter
    # grade prominently. Reads /api/v1/brain/self-assessment via the same
    # process so a missing or errored module is non-fatal. The grade is the
    # single best human signal for "is the brain actually getting smarter?"
    # so it deserves dashboard real estate.
    grade_block = ""
    try:
        from flask import current_app
        with current_app.test_client() as _client:
            _r = _cached_internal_get("/api/v1/brain/self-assessment", _client)
            if _r.status_code == 200:
                _sa = _r.get_json() or {}
                _grade = _sa.get("grade", "I")
                _rationale = _sa.get("rationale", "")
                _components = _sa.get("component_scores") or {}
                _score = _sa.get("weighted_score")
                # Color palette per letter grade
                _grade_color = {
                    "A": ("var(--green)", "rgba(16,185,129,0.12)", "rgba(16,185,129,0.4)"),
                    "B": ("var(--green)", "rgba(16,185,129,0.10)", "rgba(16,185,129,0.35)"),
                    "C": ("var(--amber)", "rgba(245,158,11,0.10)", "rgba(245,158,11,0.4)"),
                    "D": ("var(--red)",   "rgba(239,68,68,0.10)", "rgba(239,68,68,0.4)"),
                    "F": ("var(--red)",   "rgba(239,68,68,0.14)", "rgba(239,68,68,0.5)"),
                    "I": ("var(--tx2)",   "rgba(156,163,175,0.08)", "rgba(156,163,175,0.3)"),
                }.get(_grade, ("var(--tx2)", "rgba(156,163,175,0.08)", "rgba(156,163,175,0.3)"))
                _gc, _gbg, _gbd = _grade_color
                _comp_pills = ""
                _comp_labels = {
                    "fix_success": "fix success",
                    "rejection":   "rejection rate",
                    "cron_health": "cron health",
                    "volume":      "volume",
                    "memory":      "memory depth",
                }
                for _k, _v in (_components.items() if isinstance(_components, dict) else []):
                    _lbl = _h(_comp_labels.get(_k, _k))
                    if _v is None:
                        _pill_bg = "rgba(156,163,175,0.08)"; _pill_color = "var(--tx3)"; _pill_text = "—"
                    elif _v >= 3:
                        _pill_bg = "rgba(16,185,129,0.12)"; _pill_color = "var(--green)"; _pill_text = f"{_v}/4"
                    elif _v >= 2:
                        _pill_bg = "rgba(245,158,11,0.12)"; _pill_color = "var(--amber)"; _pill_text = f"{_v}/4"
                    else:
                        _pill_bg = "rgba(239,68,68,0.12)"; _pill_color = "var(--red)"; _pill_text = f"{_v}/4"
                    _comp_pills += (
                        f'<span style="display:inline-flex;align-items:center;gap:0.35rem;'
                        f'padding:0.25rem 0.6rem;border-radius:99px;background:{_pill_bg};'
                        f'color:{_pill_color};font-size:0.74rem;font-weight:600;'
                        f'margin:0.2rem;">'
                        f'<span style="opacity:0.75">{_lbl}</span>'
                        f'<span style="font-family:JetBrains Mono,monospace;font-weight:700">{_pill_text}</span>'
                        f'</span>')
                _score_str = (f' · weighted {_score:.2f}/4' if isinstance(_score, (int, float)) else '')
                grade_block = (
                    f'<div style="background:{_gbg};border:1px solid {_gbd};'
                    f'border-radius:10px;padding:1.1rem 1.3rem;margin:0.4rem 0 0.4rem;'
                    f'display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap;">'
                    f'<div style="flex:0 0 auto;font-family:JetBrains Mono,monospace;'
                    f'font-size:3.4rem;font-weight:800;line-height:1;color:{_gc};'
                    f'min-width:64px;text-align:center;">{_h(_grade)}</div>'
                    f'<div style="flex:1;min-width:240px;">'
                    f'<div style="font-size:0.72rem;font-family:JetBrains Mono,monospace;'
                    f'color:var(--tx2);text-transform:uppercase;letter-spacing:0.1em;'
                    f'margin-bottom:0.35rem;">Brain self-assessment{_h(_score_str)}</div>'
                    f'<div style="color:var(--tx);font-size:0.95rem;line-height:1.5;'
                    f'margin-bottom:0.5rem;">{_h(_rationale)}</div>'
                    f'<div style="margin-left:-0.2rem;">{_comp_pills}</div>'
                    f'<a href="/api/v1/brain/self-assessment" style="color:var(--acc-light);'
                    f'font-size:0.78rem;text-decoration:none;border-bottom:1px dotted '
                    f'rgba(129,140,248,0.5);margin-top:0.6rem;display:inline-block;">'
                    f'view full assessment JSON</a>'
                    f'</div>'
                    f'</div>'
                )
    except Exception:
        grade_block = ""

    # r32 (2026-05-24): Brain value-shipped tile — quantified answer
    # to "is the brain making the site more valuable this week?". Same
    # in-process test_client pattern as grade_block; wrapped in try so
    # a missing tier-1 source can never break the /brain page.
    value_shipped_block = ""
    try:
        from flask import current_app
        with current_app.test_client() as _client:
            _rv = _cached_internal_get("/api/v1/brain/value-shipped", _client)
            if _rv.status_code == 200:
                _vs = _rv.get_json() or {}
                _vd = _vs.get("verdict", "silent")
                _v7 = int(_vs.get("total_shipped_7d") or 0)
                _v30 = int(_vs.get("total_shipped_30d") or 0)
                _shipped7 = _vs.get("shipped_7d") or {}
                _verdict_color = {
                    "high_output": ("var(--green)", "rgba(16,185,129,0.12)", "rgba(16,185,129,0.4)"),
                    "steady":      ("var(--green)", "rgba(16,185,129,0.08)", "rgba(16,185,129,0.3)"),
                    "slow":        ("var(--amber)", "rgba(245,158,11,0.10)", "rgba(245,158,11,0.4)"),
                    "silent":      ("var(--red)",   "rgba(239,68,68,0.10)", "rgba(239,68,68,0.4)"),
                }.get(_vd, ("var(--tx2)", "rgba(156,163,175,0.08)", "rgba(156,163,175,0.3)"))
                _vc, _vbg, _vbd = _verdict_color
                _category_labels = {
                    "code_fixes":            "code fixes",
                    "autopilot_actions":     "autopilot",
                    "press_releases":        "press",
                    "linkedin_posts":        "linkedin",
                    "outreach_pitches":      "outreach",
                    "facilities_discovered": "facilities",
                }
                _cat_pills = ""
                for _k, _v in _shipped7.items():
                    if _v is None:
                        continue  # table missing — skip silently
                    _lbl = _h(_category_labels.get(_k, _k))
                    if _v > 0:
                        _pbg = "rgba(16,185,129,0.12)"; _pc = "var(--green)"
                    else:
                        _pbg = "rgba(156,163,175,0.08)"; _pc = "var(--tx3)"
                    _cat_pills += (
                        f'<span style="display:inline-flex;align-items:center;gap:0.35rem;'
                        f'padding:0.25rem 0.6rem;border-radius:99px;background:{_pbg};'
                        f'color:{_pc};font-size:0.74rem;font-weight:600;margin:0.2rem;">'
                        f'<span style="opacity:0.75">{_lbl}</span>'
                        f'<span style="font-family:JetBrains Mono,monospace;font-weight:700">{int(_v)}</span>'
                        f'</span>'
                    )
                value_shipped_block = (
                    f'<div style="background:{_vbg};border:1px solid {_vbd};'
                    f'border-radius:10px;padding:1.1rem 1.3rem;margin:0.4rem 0;'
                    f'display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap;">'
                    f'<div style="flex:0 0 auto;font-family:JetBrains Mono,monospace;'
                    f'font-size:3rem;font-weight:800;line-height:1;color:{_vc};'
                    f'min-width:80px;text-align:center;">{_v7}</div>'
                    f'<div style="flex:1;min-width:240px;">'
                    f'<div style="font-size:0.72rem;font-family:JetBrains Mono,monospace;'
                    f'color:var(--tx2);text-transform:uppercase;letter-spacing:0.1em;'
                    f'margin-bottom:0.35rem;">Value shipped — last 7d · '
                    f'<span style="color:{_vc};font-weight:700">{_h(_vd.upper())}</span></div>'
                    f'<div style="color:var(--tx);font-size:0.95rem;line-height:1.4;'
                    f'margin-bottom:0.5rem;">'
                    f'{_v7} autonomous outputs in the last 7 days; {_v30} over 30 days. '
                    f'Press releases, LinkedIn posts, outreach pitches, autopilot actions, '
                    f'and code fixes the brain shipped while no one was watching.</div>'
                    f'<div style="margin-left:-0.2rem;">{_cat_pills}</div>'
                    f'<a href="/api/v1/brain/value-shipped" style="color:var(--acc-light);'
                    f'font-size:0.78rem;text-decoration:none;border-bottom:1px dotted '
                    f'rgba(129,140,248,0.5);margin-top:0.6rem;display:inline-block;">'
                    f'view full value-shipped JSON</a>'
                    f'</div></div>'
                )
    except Exception:
        value_shipped_block = ""

    # r34 (2026-05-24): Page integrity tile — quantified answer to
    # "is every page dynamic, agentic, learning?". Composes the
    # /api/v1/sentinel/page-integrity rollup (site_score + verdict +
    # alive/broken/orphan breakdown). Same in-process test_client
    # pattern as the grade + value-shipped blocks.
    integrity_block = ""
    try:
        from flask import current_app
        with current_app.test_client() as _client:
            _ri = _cached_internal_get("/api/v1/sentinel/page-integrity", _client)
            if _ri.status_code == 200:
                _ig = _ri.get_json() or {}
                _isc = float(_ig.get("site_score") or 0)
                _ivd = _ig.get("site_verdict", "unknown")
                _ibreak = _ig.get("verdict_breakdown") or {}
                _alive = int(_ibreak.get("alive") or 0)
                _broken = int(_ibreak.get("broken") or 0)
                _orphan = int(_ibreak.get("orphan") or 0)
                _total = int(_ig.get("pages_total") or 0)
                _verdict_color = {
                    "alive":   ("var(--green)", "rgba(16,185,129,0.12)", "rgba(16,185,129,0.4)"),
                    "weak":    ("var(--amber)", "rgba(245,158,11,0.10)", "rgba(245,158,11,0.4)"),
                    "patchy":  ("var(--amber)", "rgba(245,158,11,0.14)", "rgba(245,158,11,0.5)"),
                    "broken":  ("var(--red)",   "rgba(239,68,68,0.10)", "rgba(239,68,68,0.4)"),
                }.get(_ivd, ("var(--tx2)", "rgba(156,163,175,0.08)", "rgba(156,163,175,0.3)"))
                _ic, _ibg, _ibd = _verdict_color
                # Three category pills
                def _pill(label, count, color, bg):
                    return (f'<span style="display:inline-flex;align-items:center;gap:0.35rem;'
                            f'padding:0.25rem 0.6rem;border-radius:99px;background:{bg};'
                            f'color:{color};font-size:0.74rem;font-weight:600;margin:0.2rem;">'
                            f'<span style="opacity:0.75">{label}</span>'
                            f'<span style="font-family:JetBrains Mono,monospace;font-weight:700">{count}</span>'
                            f'</span>')
                _alive_pill  = _pill("alive",  _alive,
                                     "var(--green)", "rgba(16,185,129,0.12)") if _alive  else ""
                _broken_pill = _pill("broken", _broken,
                                     "var(--red)", "rgba(239,68,68,0.12)") if _broken else ""
                _orphan_pill = _pill("orphan", _orphan,
                                     "var(--amber)", "rgba(245,158,11,0.12)") if _orphan else ""
                integrity_block = (
                    f'<div style="background:{_ibg};border:1px solid {_ibd};'
                    f'border-radius:10px;padding:1.1rem 1.3rem;margin:0.4rem 0;'
                    f'display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap;">'
                    f'<div style="flex:0 0 auto;font-family:JetBrains Mono,monospace;'
                    f'font-size:3rem;font-weight:800;line-height:1;color:{_ic};'
                    f'min-width:80px;text-align:center;">{_isc:.1f}</div>'
                    f'<div style="flex:1;min-width:240px;">'
                    f'<div style="font-size:0.72rem;font-family:JetBrains Mono,monospace;'
                    f'color:var(--tx2);text-transform:uppercase;letter-spacing:0.1em;'
                    f'margin-bottom:0.35rem;">Page integrity — {_total} pages · '
                    f'<span style="color:{_ic};font-weight:700">{_h(_ivd.upper())}</span></div>'
                    f'<div style="color:var(--tx);font-size:0.95rem;line-height:1.4;'
                    f'margin-bottom:0.5rem;">'
                    f'Every page in the sentinel manifest scored 0-100 on probe-health + '
                    f'brain-tracking + freshness + heal-findings. Site-wide mean answers '
                    f'"is every page dynamic, agentic, learning?".</div>'
                    f'<div style="margin-left:-0.2rem;">{_alive_pill}{_broken_pill}{_orphan_pill}</div>'
                    f'<a href="/api/v1/sentinel/page-integrity" style="color:var(--acc-light);'
                    f'font-size:0.78rem;text-decoration:none;border-bottom:1px dotted '
                    f'rgba(129,140,248,0.5);margin-top:0.6rem;display:inline-block;">'
                    f'view full per-page report</a>'
                    f'</div></div>'
                )
    except Exception:
        integrity_block = ""

    # r40 (2026-05-25): L23 Lifecycle Curator tile. Shows the brain's
    # PROACTIVE moat-health view (composite + findings + unknown_dims)
    # right next to the reactive integrity block. User asked "where do
    # I view these?" — this is the one rendered surface.
    lifecycle_block = ""
    try:
        from flask import current_app
        with current_app.test_client() as _client:
            _rlc = _cached_internal_get("/api/v1/brain/lifecycle/findings", _client)
            if _rlc.status_code == 200:
                _lf = _rlc.get_json() or {}
                _ch = _lf.get("composite_health")
                _fc = int(_lf.get("findings_count") or 0)
                _findings = _lf.get("findings") or []
                _unknown = _lf.get("unknown_dims") or []
                _pct = round(float(_ch) * 100, 1) if _ch is not None else None
                # color by composite
                if _pct is None:
                    _lc, _lbg, _lbd = ("var(--tx2)", "rgba(156,163,175,0.08)", "rgba(156,163,175,0.3)")
                elif _pct >= 80:
                    _lc, _lbg, _lbd = ("var(--green)", "rgba(16,185,129,0.12)", "rgba(16,185,129,0.4)")
                elif _pct >= 60:
                    _lc, _lbg, _lbd = ("var(--amber)", "rgba(245,158,11,0.12)", "rgba(245,158,11,0.4)")
                else:
                    _lc, _lbg, _lbd = ("var(--red)", "rgba(239,68,68,0.10)", "rgba(239,68,68,0.4)")
                # finding pills (top 5)
                _finding_pills = ""
                for _f in _findings[:5]:
                    _dim = _h(str(_f.get("dim", "")))
                    _finding_pills += (
                        f'<span style="display:inline-flex;align-items:center;gap:0.35rem;'
                        f'padding:0.25rem 0.6rem;border-radius:99px;background:rgba(245,158,11,0.12);'
                        f'color:var(--amber);font-size:0.74rem;font-weight:600;margin:0.2rem;">'
                        f'{_dim}</span>'
                    )
                _unknown_note = ""
                if _unknown:
                    _u_list = ", ".join(_h(str(u)) for u in _unknown[:3])
                    _unknown_note = (
                        f'<div style="font-size:0.78rem;color:var(--tx2);margin-top:0.4rem;">'
                        f'unknown (endpoint unavailable): {_u_list}</div>'
                    )
                _pct_display = f"{_pct:.0f}" if _pct is not None else "—"
                lifecycle_block = (
                    f'<div style="background:{_lbg};border:1px solid {_lbd};'
                    f'border-radius:10px;padding:1.1rem 1.3rem;margin:0.4rem 0;'
                    f'display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap;">'
                    f'<div style="flex:0 0 auto;font-family:JetBrains Mono,monospace;'
                    f'font-size:3rem;font-weight:800;line-height:1;color:{_lc};'
                    f'min-width:80px;text-align:center;">{_pct_display}</div>'
                    f'<div style="flex:1;min-width:240px;">'
                    f'<div style="font-size:0.72rem;font-family:JetBrains Mono,monospace;'
                    f'color:var(--tx2);text-transform:uppercase;letter-spacing:0.1em;'
                    f'margin-bottom:0.35rem;">L23 Lifecycle Curator — composite health · '
                    f'<span style="color:{_lc};font-weight:700">{_fc} weak dim{"s" if _fc != 1 else ""}</span></div>'
                    f'<div style="color:var(--tx);font-size:0.95rem;line-height:1.4;'
                    f'margin-bottom:0.5rem;">'
                    f'Proactive moat-health audit. 10 dimensions (server-card freshness, '
                    f'ai-citations trend, press cadence, topic-pulse, platform activity, '
                    f'registry presence, brain vocab, organism vitality, page integrity, '
                    f'value shipped). Opus 4.7 proposes new capabilities for weak dims '
                    f'every 2h via brain-lifecycle-curator cron.</div>'
                    f'<div style="margin-left:-0.2rem;">{_finding_pills}</div>'
                    f'{_unknown_note}'
                    f'<a href="/api/v1/brain/lifecycle/findings?force=1" style="color:var(--acc-light);'
                    f'font-size:0.78rem;text-decoration:none;border-bottom:1px dotted '
                    f'rgba(129,140,248,0.5);margin-top:0.6rem;display:inline-block;margin-right:1rem;">'
                    f'view findings JSON</a>'
                    f'<a href="/api/v1/brain/lifecycle/proposals" style="color:var(--acc-light);'
                    f'font-size:0.78rem;text-decoration:none;border-bottom:1px dotted '
                    f'rgba(129,140,248,0.5);margin-top:0.6rem;display:inline-block;">'
                    f'Opus proposals stream</a>'
                    f'</div></div>'
                )
    except Exception:
        lifecycle_block = ""

    # r-brain5 (2026-05-31): Layer 5 — Code Proposals. The /brain page
    # was rendering ONLY Layer 4 (HTML data-cell fixes), so it flatlined at
    # "0 proposed / No proposals yet" even though Layer 5 has real backend
    # code proposals and the loop is working. Pull the public, no-auth
    # GET /api/v1/brain/proposed-code (count + items[loop_name, file_path,
    # confidence, status, rationale, proposed_at]) via the same cached
    # in-process test_client helper used by every block above. Purely
    # additive + fully guarded: any error/empty state degrades to a clean
    # "0 — none pending" card and can never 500 the page.
    code_proposals_count = 0
    code_proposals_class = "amber"
    code_proposals_html = (
        '<div class="empty">0 — none pending. Layer 5 writes source-code '
        'proposals when the brain finds a backend/infra fix; none are queued '
        'right now.</div>'
    )
    try:
        from flask import current_app
        with current_app.test_client() as _client:
            _rcp = _cached_internal_get("/api/v1/brain/proposed-code", _client)
            if _rcp.status_code == 200:
                _cp = _rcp.get_json() or {}
                _cp_items = _cp.get("items") or []
                code_proposals_count = int(_cp.get("count") or len(_cp_items) or 0)
                code_proposals_class = "green" if code_proposals_count > 0 else "amber"
                if _cp_items:
                    _cp_blocks = []
                    for _it in _cp_items[:8]:
                        _loop = _h(str(_it.get("loop_name") or "?"))
                        _fp = _h(str(_it.get("file_path") or "?"))
                        _status = _h(str(_it.get("status") or "proposed"))
                        try:
                            _conf = float(_it.get("confidence") or 0)
                        except Exception:
                            _conf = 0.0
                        _rat = _h((str(_it.get("rationale") or ""))[:300])
                        _at = _h((str(_it.get("proposed_at") or ""))[:19])
                        # badge: approved-ish / high-confidence = green, else amber
                        if _status in ("approved", "merged", "pr_open") or _conf >= 0.85:
                            _bcls = "approved"
                        else:
                            _bcls = "pending"
                        _btext = f"{_status} · conf {_conf:.2f}"
                        _cp_blocks.append(
                            f'<div class="proposal">'
                            f'<div class="head">'
                            f'<span class="label">{_loop}</span>'
                            f'<span class="badge {_bcls}">{_h(_btext)}</span>'
                            f'</div>'
                            f'<div class="meta"><code>{_fp}</code>'
                            + (f' · {_rat}' if _rat else '')
                            + (f' · proposed {_at}' if _at else '')
                            + '</div>'
                            f'</div>'
                        )
                    code_proposals_html = "\n".join(_cp_blocks)
    except Exception:
        # Keep the graceful default card + 0 count; never break the page.
        pass

    # r-brain5: actionable-findings count tile. _get_state() already
    # surfaces this (the ~92-117 open backend/SEO/infra findings that route
    # to Layer 5 + autopilot, NOT Layer 4 HTML edits). Show it as a KPI so
    # "0 HTML fixes" is read alongside the real open-work number.
    try:
        actionable_count = int(state.get("actionable_findings_count") or 0)
    except Exception:
        actionable_count = 0
    actionable_class = "amber" if actionable_count > 0 else "green"

    # Render proposals (newest first)
    if proposals:
        prop_blocks = []
        for p in reversed(proposals[-12:]):
            badge_class = "approved" if p.get("approved") else "pending"
            badge_text = (f"approved · {p.get('approval_count',1)} cycles"
                          if p.get("approved")
                          else f"pending · {p.get('approval_count',1)}/2 cycles")
            find = _h((p.get("find") or "")[:300])
            replace = _h((p.get("replace") or "")[:300])
            rationale = _h((p.get("rationale") or "")[:300])
            label = _h(p.get("issue_label") or "?")
            url = _h(p.get("source_url") or "")
            proposed_at = _h((p.get("proposed_at") or "")[:19])
            prop_blocks.append(
                f'<div class="proposal">'
                f'<div class="head">'
                f'<span class="label">{label}</span>'
                f'<span class="badge {badge_class}">{badge_text}</span>'
                f'</div>'
                f'<div class="find">- {find}</div>'
                f'<div class="replace">+ {replace}</div>'
                f'<div class="meta">{rationale} · <code>{url}</code> · proposed {proposed_at}</div>'
                f'</div>'
            )
        proposals_html = "\n".join(prop_blocks)
    else:
        proposals_html = '<div class="empty">No proposals yet. Brain v2 fires hourly — first run lands within the hour.</div>'

    # Render log
    if log:
        log_rows = []
        # 2026-06-12: the store returns newest-first (ORDER BY t DESC) and the
        # old reversed() here flipped it back — the page rendered the OLDEST
        # rows (perpetual 2026-05-26 placeholders) as the "learning log".
        # Sort explicitly newest-first so either source (store rows with 't',
        # legacy in-memory entries with 'ts') displays correctly.
        for entry in sorted(log, key=lambda e: str(e.get("t") or e.get("ts") or ""), reverse=True):
            outcome = (entry.get("outcome") or "")[:60]
            cls = ("ok" if outcome.startswith("proposed") or outcome == "approval_count_incremented"
                   else "err" if "fail" in outcome or "error" in outcome or "refused" in outcome
                   else "warn")
            # Phase S: store rows use "issue_label", legacy in-memory uses
            # "issue" — accept either.
            issue_lbl = (entry.get("issue_label")
                         or entry.get("issue") or "?")[:60]
            log_rows.append(
                f'<div class="log-row">'
                f'<span class="log-time">{_h((entry.get("t") or "")[:19])}</span>'
                f'<span class="log-issue">{_h(issue_lbl)}</span>'
                f'<span class="log-outcome {cls}">{_h(outcome)}</span>'
                f'</div>'
            )
        log_html = "\n".join(log_rows)
    else:
        log_html = '<div class="empty">No learning attempts logged yet.</div>'

    # Phase S (2026-05-12): render the stuck-issue worklist. Items come
    # from brain_v2_store.most_persistent_unfixed via _get_state(). Empty
    # state copy explains what users should expect when everything is
    # already proposed — that's the goal state, not a bug.
    persistence_items = state.get("persistence") or []
    if persistence_items:
        prows = []
        for it in persistence_items:
            seen = int(it.get("seen_count", 0))
            url = _h(it.get("url") or "")
            label = _h(it.get("issue_label") or "?")
            last_o = _h((it.get("last_outcome") or "untried")[:60])
            last_seen = _h((it.get("last_seen_at") or "")[:19])
            prows.append(
                f'<div class="log-row">'
                f'<span class="log-time">{last_seen}</span>'
                f'<span class="log-issue">{label} · <code>{url}</code></span>'
                f'<span class="log-outcome warn">seen ×{seen} · {last_o}</span>'
                f'</div>'
            )
        persistence_html = "\n".join(prows)
    elif state.get("store_backed"):
        persistence_html = '<div class="empty">No stuck issues — every detected pattern has produced a proposal or been auto-fixed.</div>'
    else:
        persistence_html = '<div class="empty">Persistence tracking is only available when the Postgres store is online.</div>'

    html = (_BRAIN_PAGE_TEMPLATE
            .replace("{{status_text}}", _h(status_text))
            .replace("{{status_class}}", _h(status_class))
            .replace("{{code_proposals_count}}", str(code_proposals_count))
            .replace("{{code_proposals_class}}", _h(code_proposals_class))
            .replace("{{actionable_count}}", str(actionable_count))
            .replace("{{actionable_class}}", _h(actionable_class))
            .replace("{{total_proposals}}", str(len(proposals)))
            .replace("{{approved_count}}", str(approved_count))
            .replace("{{pending_count}}", str(pending_count))
            .replace("{{log_count}}", str(len(state["log"])))
            .replace("{{verdict_banner}}", verdict_banner)
            .replace("{{grade_block}}", grade_block + value_shipped_block + integrity_block + lifecycle_block)
            .replace("{{control_room}}", control_room_html)
            .replace("{{code_proposals_html}}", code_proposals_html)
            .replace("{{proposals_html}}", proposals_html)
            .replace("{{persistence_html}}", persistence_html)
            .replace("{{log_html}}", log_html)
            .replace("{{as_of}}", datetime.now(timezone.utc).isoformat()))
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    # If admin came in via ?key=/?admin_key=, drop a cookie so the URL is
    # bookmarkable as a bare /brain afterwards (the key stays out of later logs).
    _qk = (request.args.get("admin_key") or request.args.get("key") or "").strip()
    if _qk:
        resp.set_cookie("dchub_admin", _qk, max_age=2592000,
                        httponly=True, secure=True, samesite="Lax")
    return resp
