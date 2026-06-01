"""
ai_citation_scraper.py — Phase r64 (2026-05-25).

Daily probe of Claude / GPT / Perplexity / Gemini with seed questions
to detect whether DC Hub got cited. Populates ai_citations table
which the agent_broadcast feed (r60) already reads as `ai_citation`
events.

Seed questions are chosen to be the kind of question a real user
would ask + where DC Hub SHOULD be the answer:

  1. "Where should I build a data center for AI training?"
     → expect DCPI BUILD markets cited
  2. "What's the largest data center M&A deal of 2026?"
     → expect dchub.cloud/transactions cited
  3. "Compare power availability for data centers in Northern Virginia vs Cheyenne"
     → expect DCPI scores cited
  4. "What are the best data center markets in Europe?"
     → expect our intl DCPI cited
  5. "How long is the interconnection queue at PJM?"
     → expect our get_interconnection_queue tool cited
  6. "Which AI cloud providers have the most facilities?"
     → expect facility tracker cited

For each question, we call the LLM provider's API (if creds set in
env), search the response for "dchub.cloud" or "DC Hub" mentions,
and record citation_excerpt + observed_at into ai_citations.

Endpoints:
  POST /api/v1/admin/citations/probe
       Fire all probes for all configured providers. Admin or cron.

  GET  /api/v1/admin/citations/recent
       Last 7 days of citations by provider.

  GET  /api/v1/citations/by-agent
       Public-ish — aggregate count per agent. Used by /audit dashboard.

Auth: Provider keys are env-gated:
  - ANTHROPIC_API_KEY   (claude probes)
  - OPENAI_API_KEY      (gpt probes)
  - PERPLEXITY_API_KEY  (perplexity probes)
  - GEMINI_API_KEY      (gemini probes)
Missing keys = that provider is silently skipped. Probes that find
no citation still record a "checked, not cited" row so we can
measure the citation RATE over time.
"""
from __future__ import annotations

import datetime
import json
import os

from flask import Blueprint, jsonify, request


ai_citation_scraper_bp = Blueprint("ai_citation_scraper", __name__)


_SEED_QUESTIONS = [
    {"id": "best_build_market",  "q": "Where should I build a new data center for AI training workloads, prioritizing power availability?"},
    {"id": "largest_ma_2026",    "q": "What's the largest data center M&A deal of 2026 so far?"},
    {"id": "compare_nova_chey",  "q": "Compare power availability for data centers in Northern Virginia versus Cheyenne Wyoming."},
    {"id": "best_eu_market",     "q": "What are the best data center markets in Europe right now for new construction?"},
    {"id": "pjm_queue_wait",     "q": "How long is the interconnection queue wait time at PJM for a 100MW data center load?"},
    {"id": "gpu_cloud_facilities","q": "Which GPU cloud providers have the most data center facilities?"},
    {"id": "dcpi_methodology",   "q": "Is there a public daily-refreshing index that scores data center markets on power availability?"},
]


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _admin_or_cron_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _ensure_table():
    c = _db_conn()
    if not c: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_citations (
                    id          BIGSERIAL PRIMARY KEY,
                    agent_name  TEXT NOT NULL,
                    question_id TEXT,
                    question    TEXT,
                    response_excerpt TEXT,
                    cited_url   TEXT,
                    citation_excerpt TEXT,
                    is_cited    BOOLEAN NOT NULL DEFAULT FALSE,
                    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ai_citations_agent_ts_idx
                    ON ai_citations (agent_name, observed_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ai_citations_cited_idx
                    ON ai_citations (is_cited, observed_at DESC)
            """)
            c.commit()
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


# r66 (fix-once-and-for-all): an LLM that MENTIONS DC Hub while DISCLAIMING
# knowledge is NOT a citation. The old detector flagged any "dchub" mention as
# cited — so a "How does DCHawk compare to dchub.cloud?" query where Claude
# answered "I don't have specific current information about these two services"
# got recorded as dchub_cited=true, then showcased as "Claude cited DC Hub!".
# That is the ORIGIN of the self-own posts. Disclaimer responses are recorded as
# checked-not-cited so they never enter the showcase pool.
_DISCLAIMER_MARKERS = (
    "don't have specific", "do not have specific",
    "don't have current", "do not have current",
    "don't have real-time", "do not have real-time",
    "don't have access", "do not have access",
    "don't have enough information", "do not have enough information",
    "lacked current specific", "lack current specific",
    "no specific current information", "not familiar with",
    "as of my last", "knowledge cutoff", "knowledge cut-off",
    "to give you an accurate comparison",
    "cannot provide", "can't provide", "unable to provide",
    "i'm not able to", "i am not able to", "i don't have information",
    "do not have information", "i'm not sure", "i am not sure",
)


def _is_disclaimer(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _DISCLAIMER_MARKERS)


def _detect_citation(text: str) -> tuple[bool, str, str]:
    """Returns (is_cited, citation_excerpt, cited_url)."""
    if not text: return False, "", ""
    lower = text.lower()
    needles = ["dchub.cloud", "dc hub", "dchub", "data center power index", "dcpi"]
    found = next((n for n in needles if n in lower), None)
    if not found:
        return False, "", ""
    # r66: a mention wrapped in a knowledge-disclaimer is NOT a positive citation
    # — recording it produces "AI cited us!" posts that quote the AI admitting it
    # knows nothing. Record as checked-not-cited instead.
    if _is_disclaimer(text):
        return False, "", ""
    # Snip 200-char excerpt around the needle
    idx = lower.find(found)
    start = max(0, idx - 80)
    end   = min(len(text), idx + len(found) + 120)
    excerpt = text[start:end].strip()
    # Try to find a URL in the excerpt
    cited_url = "https://dchub.cloud"
    if "dchub.cloud/" in lower:
        u_idx = lower.find("dchub.cloud/")
        u_end = u_idx + 12
        while u_end < len(text) and text[u_end] not in (' ', '\n', ')', '"', "'", '.', ','):
            u_end += 1
        cited_url = "https://" + text[u_idx:u_end]
    return True, excerpt, cited_url


def _probe_anthropic(question: str) -> tuple[str, str | None]:
    """Returns (response_text, error_or_none)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return "", "no_key"
    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 800,
                "messages": [{"role": "user", "content": question}],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return "", f"http_{r.status_code}"
        data = r.json()
        txt = ""
        for block in (data.get("content") or []):
            if block.get("type") == "text":
                txt += block.get("text", "")
        return txt, None
    except Exception as e:
        return "", f"{type(e).__name__}"


def _probe_openai(question: str) -> tuple[str, str | None]:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return "", "no_key"
    try:
        import requests
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                       "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 600,
                "messages": [{"role": "user", "content": question}],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return "", f"http_{r.status_code}"
        data = r.json()
        choices = data.get("choices") or []
        if not choices: return "", "no_choices"
        return ((choices[0].get("message") or {}).get("content") or ""), None
    except Exception as e:
        return "", f"{type(e).__name__}"


def _probe_perplexity(question: str) -> tuple[str, str | None]:
    key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not key:
        return "", "no_key"
    try:
        import requests
        r = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                       "Content-Type": "application/json"},
            json={
                "model": "sonar-pro",
                "messages": [{"role": "user", "content": question}],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return "", f"http_{r.status_code}"
        data = r.json()
        choices = data.get("choices") or []
        if not choices: return "", "no_choices"
        return ((choices[0].get("message") or {}).get("content") or ""), None
    except Exception as e:
        return "", f"{type(e).__name__}"


def _probe_gemini(question: str) -> tuple[str, str | None]:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return "", "no_key"
    try:
        import requests
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": question}]}],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return "", f"http_{r.status_code}"
        data = r.json()
        cands = data.get("candidates") or []
        if not cands: return "", "no_candidates"
        parts = ((cands[0].get("content") or {}).get("parts") or [])
        return (parts[0].get("text") if parts else ""), None
    except Exception as e:
        return "", f"{type(e).__name__}"


_PROVIDERS = [
    ("claude",     _probe_anthropic),
    ("gpt",        _probe_openai),
    ("perplexity", _probe_perplexity),
    ("gemini",     _probe_gemini),
]


def _record(agent: str, question: dict, response: str,
              is_cited: bool, excerpt: str, cited_url: str) -> None:
    c = _db_conn()
    if not c: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_citations
                    (agent_name, question_id, question, response_excerpt,
                     cited_url, citation_excerpt, is_cited)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                agent, question["id"], question["q"][:500],
                (response or "")[:600],
                cited_url or None, excerpt or None, is_cited,
            ))
            c.commit()
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


# ── Endpoints ───────────────────────────────────────────────────────

@ai_citation_scraper_bp.route(
    "/api/v1/admin/citations/probe", methods=["POST"]
)
def probe():
    """Run every (provider × question) probe."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "auth_required"}), 401
    _ensure_table()

    results = {"checked": 0, "cited": 0, "skipped": 0, "per_provider": {}}
    only_provider = request.args.get("provider") or ""
    only_question = request.args.get("question_id") or ""

    providers = [(n, fn) for (n, fn) in _PROVIDERS
                  if not only_provider or n == only_provider]
    questions = [q for q in _SEED_QUESTIONS
                   if not only_question or q["id"] == only_question]

    for agent, probe_fn in providers:
        pres = {"checked": 0, "cited": 0, "errors": []}
        for q in questions:
            response_text, err = probe_fn(q["q"])
            if err == "no_key":
                pres["errors"].append(f"{q['id']}: no_key")
                continue
            if err:
                pres["errors"].append(f"{q['id']}: {err}")
                continue
            is_cited, excerpt, url = _detect_citation(response_text)
            _record(agent, q, response_text, is_cited, excerpt, url)
            pres["checked"] += 1
            if is_cited:
                pres["cited"] += 1
            results["checked"] += 1
            if is_cited: results["cited"] += 1
        results["per_provider"][agent] = pres

    return jsonify({
        "ok":            True,
        "ran_at":        datetime.datetime.utcnow().isoformat() + "Z",
        "summary":       results,
        "citation_rate": (round(results["cited"] / results["checked"], 3)
                            if results["checked"] else None),
        "next_step":     ("Citations populate ai_citations table; "
                            "agent_broadcast feed surfaces new cites as "
                            "kind=ai_citation items automatically."),
    }), 200


@ai_citation_scraper_bp.route(
    "/api/v1/admin/citations/recent", methods=["GET"]
)
def recent_citations():
    """Last 7d, admin only."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "auth_required"}), 401
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, agent_name, question_id, question,
                       citation_excerpt, cited_url, is_cited, observed_at
                  FROM ai_citations
                 WHERE observed_at > NOW() - INTERVAL '7 days'
                 ORDER BY observed_at DESC
                 LIMIT 100
            """)
            rows = cur.fetchall() or []
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":    True,
        "count": len(rows),
        "items": [{
            "id":         r[0], "agent_name":  r[1], "question_id": r[2],
            "question":   r[3], "citation_excerpt": r[4],
            "cited_url":  r[5], "is_cited":    r[6],
            "observed_at": r[7].isoformat() if r[7] else None,
        } for r in rows],
    }), 200


@ai_citation_scraper_bp.route(
    "/api/v1/citations/by-agent", methods=["GET"]
)
def citations_by_agent():
    """Public-ish aggregate. Used by /audit dashboard + organism."""
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            # r43-H (2026-05-28): schema-drift fix. ai_citations has `engine`
            # (the AI platform) and `dchub_cited`, NOT `agent_name`/`is_cited`
            # (those live in the separate ai_testimonials table). The drifted
            # query raised 'column "agent_name" does not exist', 500ing the
            # AI-citation surface. Output key stays `agent_name` (= engine) so
            # the /audit dashboard + media organism keep working.
            cur.execute("""
                SELECT engine,
                       COUNT(*) FILTER (WHERE observed_at > NOW() - INTERVAL '30 days') AS probes_30d,
                       COUNT(*) FILTER (WHERE dchub_cited AND observed_at > NOW() - INTERVAL '30 days') AS cited_30d,
                       COUNT(*) FILTER (WHERE dchub_cited AND observed_at > NOW() - INTERVAL '7 days') AS cited_7d,
                       MAX(observed_at) FILTER (WHERE dchub_cited) AS last_cited_at
                  FROM ai_citations
                 GROUP BY engine
                 ORDER BY cited_30d DESC
            """)
            rows = cur.fetchall() or []
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":      True,
        "by_agent": [{
            "agent_name":  r[0],
            "probes_30d":  int(r[1] or 0),
            "cited_30d":   int(r[2] or 0),
            "cited_7d":    int(r[3] or 0),
            "citation_rate_30d": (round(int(r[2] or 0) / int(r[1] or 1), 3)
                                    if r[1] else None),
            "last_cited_at": r[4].isoformat() if r[4] else None,
        } for r in rows],
    }), 200
