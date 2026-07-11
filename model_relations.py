"""Model Relations master shell (2026-07-11) — the platform-eval loop, automated.

Runs the standard neutral evaluation protocol against DC Hub's live REST rail
using each AI platform's OWN flagship model as the evaluator (the loop that
produced the 07-11 fix waves: fields-wrapper, capacity_mw parity, TTP
self-explanation, percentile-fallback discrimination). Outside-in QA + verdict
freshness + co-design intake, one tick.

HARD RULES (encoded, not aspirational):
- NEVER publishes anything. Verdicts land in model_relations_runs for HUMAN
  review; card publication stays consent-gated and manual (the ai-wars
  fabricated-quote lesson + ChatGPT's no-official-endorsement constraint).
- Neutral framing only — the system prompt asks for "your genuine evaluation,
  positive or negative". No praise-steering, ever.
- One tick per platform per cadence; ≤8 model calls per run; max_tokens capped.
  Billing failures record status=blocked_billing and move on (fail-soft).
- Harness executes model-requested HTTP ONLY against the DC Hub origin.

Findings flow to brain_findings via the CANONICAL writer (detector
model_relations) so the existing brain pipeline drafts fixes under normal
governance. A run that observes 5xx where the previous run was clean files a
regression finding.

Cadence: external cron POSTs /api/jobs/model-relations (worker-proxied — the
r-eval-fixwave-2 pool lesson) monthly, or on a platform's model release.
Partner keys come from env (MODELREL_KEY_<PLATFORM>) — never hardcoded (the
leaked-secret checklist).
"""

from __future__ import annotations

import os
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DCHUB_BASE = "https://dchub-backend-production.up.railway.app"
MAX_MODEL_CALLS = 8
MAX_TOKENS = 1200
TRUNCATE_AT = 15000

# Platform registry. llm_key_env = the platform's own API key (already on
# Railway); partner_key_env = the comp DC Hub key its evaluator uses (set as
# MODELREL_KEY_*). pick = substring preference order over /models ids;
# fixed = try-in-order when the API has no models list.
_PLATFORMS = {
    "openai":     {"base": "https://api.openai.com/v1", "llm_key_env": "OPENAI_API_KEY",
                   "partner_key_env": "MODELREL_KEY_OPENAI",
                   "pick": ["gpt-5.6", "gpt-5.5", "gpt-5", "o3", "gpt-4o"],
                   "avoid": ["mini", "nano", "audio", "realtime", "search", "image"]},
    "mistral":    {"base": "https://api.mistral.ai/v1", "llm_key_env": "MISTRAL_API_KEY",
                   "partner_key_env": "MODELREL_KEY_MISTRAL",
                   "pick": ["mistral-large"], "avoid": ["small", "mini", "codestral", "embed"]},
    "meta":       {"base": "https://api.groq.com/openai/v1", "llm_key_env": "GROQ_API_KEY",
                   "partner_key_env": "MODELREL_KEY_META",
                   "pick": ["llama-4", "llama-3.3-70b"], "avoid": ["guard", "vision", "1b", "3b", "8b"]},
    "perplexity": {"base": "https://api.perplexity.ai", "llm_key_env": "PERPLEXITY_API_KEY",
                   "partner_key_env": "MODELREL_KEY_PERPLEXITY",
                   "fixed": ["sonar-pro", "sonar"]},
    "xai":        {"base": "https://api.x.ai/v1", "llm_key_env": "XAI_API_KEY",
                   "partner_key_env": "MODELREL_KEY_XAI",
                   "pick": ["grok-4", "grok-3"], "avoid": ["mini", "fast", "image"]},
    # gemini: GEMINI_API_KEY on Railway is malformed (known 07-10) — enable
    # once a valid key lands; Gemini runs its own validation cycles meanwhile.
}

_SYSTEM = (
    'You are evaluating a live infrastructure data API (DC Hub, dchub.cloud) for '
    'agent-native integration quality. You issue HTTP requests via the harness by '
    'replying with a JSON object {"call": {"method", "url", "body"?}} and nothing '
    'else; the harness returns the response. When you have seen enough (4-6 calls), '
    'reply instead with {"verdict": {...}} containing: findings (ranked strengths/'
    'weaknesses you observed), top_structural_gap, token_efficiency_observations '
    '(tokens/record vs typical industry APIs), and assessment (2-4 sentences, your '
    'genuine evaluation, positive or negative).'
)
_KICKOFF = (
    "Evaluate the deterministic-rail workflow: envelope discovery via GET /openapi.json, "
    "then GET /api/v1/interconnection-queue/refined (predicates: min_mw, max_ttp_months, "
    "iso, baseload_only, fuel_type, geocoded_only, limit) with 2-3 predicate combinations "
    "of your choosing, then follow one survivor's site_evaluation_handoff verbatim into "
    "GET /api/site-score?lat=&lon=&capacity_mw=, then optionally POST /api/v1/rank-sites "
    '{"candidates":[{"id":"...","<metric>":<number>}],"objectives":{"<field>":1.0},'
    '"percentile":true} (candidate metrics at top level). Judge legibility, determinism, '
    "honesty of caveats, and token efficiency."
)

_DDL = """
CREATE TABLE IF NOT EXISTS model_relations_runs (
  id          SERIAL PRIMARY KEY,
  platform    TEXT NOT NULL,
  model_id    TEXT,
  status      TEXT NOT NULL,           -- ok | blocked_billing | no_verdict | error | no_partner_key
  calls_made  INTEGER DEFAULT 0,
  http_5xx    INTEGER DEFAULT 0,
  verdict     JSONB,
  verdict_diff TEXT,                   -- first_run | unchanged | changed
  transcript  JSONB,
  notes       TEXT,
  started_at  TIMESTAMPTZ DEFAULT now(),
  finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_mrr_platform ON model_relations_runs (platform, started_at DESC);
"""


def _http_json(url, method="GET", headers=None, body=None, timeout=45):
    req = urllib.request.Request(url, method=method,
                                 data=(json.dumps(body).encode() if body is not None else None))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)[:300]


def _pick_model(cfg, llm_key):
    if "fixed" in cfg:
        return cfg["fixed"][0], cfg["fixed"][1:]
    code, txt = _http_json(cfg["base"] + "/models", headers={"Authorization": f"Bearer {llm_key}"}, timeout=30)
    if code != 200:
        return None, []
    try:
        ids = [m.get("id", "") for m in json.loads(txt).get("data", [])]
    except Exception:
        return None, []
    avoid = cfg.get("avoid", [])
    for pref in cfg.get("pick", []):
        cand = sorted([i for i in ids if pref in i and not any(a in i for a in avoid)], reverse=True)
        if cand:
            return cand[0], []
    return None, []


def _chat(cfg, llm_key, model, messages, use_temp=True):
    body = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS}
    if use_temp:
        body["temperature"] = 0.3
    code, txt = _http_json(cfg["base"] + "/chat/completions", method="POST",
                           headers={"Authorization": f"Bearer {llm_key}"}, body=body, timeout=120)
    if code == 400 and use_temp and "temperature" in txt:
        return _chat(cfg, llm_key, model, messages, use_temp=False)
    return code, txt


def _parse_model_json(txt):
    """content → dict; tolerates ```json fences."""
    try:
        content = json.loads(txt)["choices"][0]["message"]["content"] or ""
    except Exception:
        return None, None
    s = content.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        s = s[4:] if s[:4].lower() == "json" else s
    try:
        return json.loads(s.strip()), content
    except Exception:
        return None, content


def _run_platform(platform, cfg):
    """One neutral protocol run. Returns the row dict for model_relations_runs."""
    row = {"platform": platform, "model_id": None, "status": "error", "calls_made": 0,
           "http_5xx": 0, "verdict": None, "transcript": [], "notes": ""}
    llm_key = os.environ.get(cfg["llm_key_env"], "").strip()
    partner = os.environ.get(cfg["partner_key_env"], "").strip()
    if not llm_key:
        row.update(status="error", notes=f"missing {cfg['llm_key_env']}")
        return row
    if not partner:
        row.update(status="no_partner_key", notes=f"set {cfg['partner_key_env']}")
        return row

    model, fallbacks = _pick_model(cfg, llm_key)
    if not model:
        row.update(status="blocked_billing", notes="models list unreachable (billing/permission?)")
        return row
    row["model_id"] = model

    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _KICKOFF}]
    reminded = False
    for _ in range(MAX_MODEL_CALLS):
        code, txt = _chat(cfg, llm_key, model, messages)
        row["calls_made"] += 1
        if code in (401, 402, 403, 429) or (code == 400 and "quota" in txt.lower()):
            if fallbacks:
                model = fallbacks.pop(0)
                row["model_id"] = model
                row["calls_made"] -= 1
                continue
            row.update(status="blocked_billing", notes=f"HTTP {code}: {txt[:200]}")
            return row
        if code != 200:
            row.update(status="error", notes=f"LLM HTTP {code}: {txt[:200]}")
            return row
        obj, content = _parse_model_json(txt)
        row["transcript"].append({"role": "assistant", "content": (content or txt)[:TRUNCATE_AT]})
        if obj and "verdict" in obj:
            row.update(status="ok", verdict=obj["verdict"])
            return row
        if obj and "call" in obj:
            call = obj["call"] or {}
            url = str(call.get("url", ""))
            if url.startswith("/"):
                url = DCHUB_BASE + url
            if not url.startswith(DCHUB_BASE):
                feed = json.dumps({"error": "harness: only DC Hub origin calls are executed"})
            else:
                mcode, mtxt = _http_json(url, method=str(call.get("method", "GET")).upper(),
                                         headers={"X-API-Key": partner,
                                                  "Accept": "application/json"},
                                         body=call.get("body"), timeout=45)
                if mcode >= 500 or mcode == 0:
                    row["http_5xx"] += 1
                feed = f"HTTP {mcode}\n" + (mtxt[:TRUNCATE_AT] +
                        ("\n[harness: truncated at 15000 chars]" if len(mtxt) > TRUNCATE_AT else ""))
            row["transcript"].append({"role": "harness", "content": feed[:TRUNCATE_AT]})
            messages.append({"role": "assistant", "content": content or ""})
            messages.append({"role": "user", "content": feed})
            continue
        if not reminded:
            reminded = True
            messages.append({"role": "assistant", "content": content or ""})
            messages.append({"role": "user", "content":
                             'Reply with ONLY a JSON object: {"call": {...}} or {"verdict": {...}}.'})
            continue
        row.update(status="no_verdict", notes="model would not follow the JSON protocol")
        return row
    row.update(status="no_verdict", notes="call budget exhausted without a verdict")
    return row


def run_model_relations_tick(platforms=None):
    """The tick. Runs each requested (default: all registered) platform once,
    persists runs, diffs verdicts, files brain findings via the canonical
    writer. Returns a summary dict (logged — swallowed-write rule)."""
    import psycopg2
    import psycopg2.extras
    db = os.environ.get("DATABASE_URL")
    if not db:
        return {"ok": False, "error": "no DATABASE_URL"}
    conn = psycopg2.connect(db, sslmode="require", connect_timeout=10)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(_DDL)

    todo = {k: v for k, v in _PLATFORMS.items()
            if not platforms or k in platforms}
    summary = {"ok": True, "runs": {}}
    for platform, cfg in todo.items():
        row = _run_platform(platform, cfg)
        # verdict diff vs last ok run
        diff = None
        if row["status"] == "ok":
            cur.execute("SELECT verdict FROM model_relations_runs "
                        "WHERE platform=%s AND status='ok' ORDER BY started_at DESC LIMIT 1",
                        (platform,))
            prev = cur.fetchone()
            if not prev or not prev[0]:
                diff = "first_run"
            else:
                prev_a = (prev[0].get("assessment") or "") if isinstance(prev[0], dict) else ""
                diff = "unchanged" if prev_a == (row["verdict"].get("assessment") or "") else "changed"
        cur.execute(
            "INSERT INTO model_relations_runs (platform, model_id, status, calls_made, "
            "http_5xx, verdict, verdict_diff, transcript, notes, finished_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) RETURNING id",
            (platform, row["model_id"], row["status"], row["calls_made"], row["http_5xx"],
             json.dumps(row["verdict"]) if row["verdict"] else None, diff,
             json.dumps(row["transcript"][-40:]), row["notes"][:500]))
        run_id = cur.fetchone()[0]

        # findings → brain (canonical writer only — the wrong-col trap)
        try:
            from routes.brain_findings_writer import upsert_brain_finding
            if row["status"] == "ok":
                v = row["verdict"] or {}
                detail = (f"[model_relations run {run_id}, {row['model_id']}] "
                          f"assessment: {(v.get('assessment') or '')[:400]} | "
                          f"top_structural_gap: {str(v.get('top_structural_gap'))[:300]} | "
                          f"diff: {diff}")
                upsert_brain_finding(cur, issue=f"model_eval_{platform}",
                                     url="/api/v1/interconnection-queue/refined",
                                     detail=detail, detector="model_relations")
            if row["http_5xx"] > 0:
                upsert_brain_finding(cur, issue=f"model_eval_regression_{platform}",
                                     url=DCHUB_BASE,
                                     detail=(f"[run {run_id}] evaluator observed "
                                             f"{row['http_5xx']} 5xx/timeout during the protocol"),
                                     detector="model_relations")
        except Exception as e:
            logger.warning("model_relations: finding write failed: %s", str(e)[:120])

        summary["runs"][platform] = {"status": row["status"], "model": row["model_id"],
                                     "calls": row["calls_made"], "5xx": row["http_5xx"],
                                     "diff": diff, "run_id": run_id}
        logger.info("model_relations %s: %s", platform, summary["runs"][platform])
    conn.close()
    logger.info("model_relations tick: %s", summary)
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    plats = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    print(json.dumps(run_model_relations_tick(plats), indent=1, default=str))
