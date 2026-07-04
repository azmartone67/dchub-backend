"""routes/analyst_note.py — THE DC HUB ANALYST (2026-07-04)
=============================================================

Weekly brain-authored Analyst Note: "What moved in data-center power this
week" — grounded, cited, honest-numbers fenced.

PIPELINE (generate_analyst_note):
  1. GATHER structured inputs — the same real data the media desk ranks:
       · media_editorial.rank_data_events()  (DCPI movers/build, deals,
         interconnection queue, tenants, new facilities — the tested slate)
       · direct SQL recency: deals + news_articles from the last 7 days
       · brain_rag.retrieve_context()        (semantic recall over the corpus)
       · brain_rag.retrieve_lessons()        (past-outcome self-awareness)
     Every input source is fail-soft: an outage shrinks the note, never
     crashes the run.
  2. COMPOSE via ONE structured-outputs LLM call (brain_llm_structured —
     the verified GA output_config.format param; fail-soft ladder to the
     legacy free-text body + brain_models fallback chain). Tier: routine
     (sonnet-class synthesis) via brain_models, else claude-haiku-4-5.
  3. ★HONEST-NUMBERS FENCE — the whitelist twin of brain_investigator's
     _fence_fabricated_figures ban-list: every figure in body_md must appear
     in the structured inputs (with $M→$B / GW→MW scale tolerance); any
     sentence carrying an unsourced figure is STRIPPED, and the strips are
     reported. Exempt: 4-digit years and BARE small integers (<=12: "top 5",
     "last 7 days") — but ONLY without unit/currency context. A small number
     wearing a unit ("$9B", "8 GW", "$12", "7%") is a real-world claim and
     stays FENCED.
  4. PERSIST to analyst_notes — IDEMPOTENT per week_of (second run in the
     same ISO week is a cheap no-op; ?force=1 regenerates).

SERVING:
  GET  /reports/analyst-note           latest note, server-rendered HTML
                                       (house dark style; /reports/* is the
                                       backend-served Reports family —
                                       /research/* is DEAD ON ARRIVAL: it is
                                       prefix-302'd by main.py's
                                       _check_prefix_redirects AND routed to
                                       dchubapiproxy by the CF zone worker)
  GET  /api/v1/analyst-note/latest     latest note, JSON
  POST /api/v1/analyst-note/generate   admin-gated cron target (heartbeat
                                       sends X-Admin-Key via _hit)

DISTRIBUTION: ★NEVER auto-posts. The ONLY outbound surface is a media LEAD
(kind=analyst_note) offered to media_editorial.rank_data_events(), where the
media machine's existing gates (number-lead, novelty, claim-verify, human
approval) decide whether anything ships.

Kill switch: ANALYST_NOTE_DISABLED=1.
"""
from __future__ import annotations

import datetime as _dt
import hmac
import json
import logging
import os
import re
import urllib.error
import urllib.request
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
analyst_note_bp = Blueprint("analyst_note", __name__)

PUBLIC_PATH = "/reports/analyst-note"
PUBLIC_URL = "https://dchub.cloud" + PUBLIC_PATH


# ── config / auth (same shape as brain_rag) ──────────────────────────
def _disabled() -> bool:
    return str(os.environ.get("ANALYST_NOTE_DISABLED", "")).lower() in (
        "1", "true", "yes")


def _admin_ok() -> bool:
    """Accept X-Admin-Key=DCHUB_ADMIN_KEY or X-Internal-Key=DCHUB_INTERNAL_KEY /
    DCHUB_SYNC_KEY — the exact headers cron_heartbeat._hit() attaches."""
    pairs = [
        (request.headers.get("X-Admin-Key"), os.environ.get("DCHUB_ADMIN_KEY")),
        (request.headers.get("X-Internal-Key"), os.environ.get("DCHUB_INTERNAL_KEY")),
        (request.headers.get("X-Internal-Key"), os.environ.get("DCHUB_SYNC_KEY")),
    ]
    for got, exp in pairs:
        got = (got or "").strip()
        exp = (exp or "").strip()
        if got and exp and hmac.compare_digest(got, exp):
            return True
    return False


# ── DB ────────────────────────────────────────────────────────────────
def _conn():
    """Plain connection, autocommit, explicitly closed by callers — NEVER the
    @contextmanager __enter__ pattern (GC closes the conn mid-use)."""
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=6)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_schema() -> bool:
    """CREATE TABLE IF NOT EXISTS — lazily at generate time, never at boot
    (the boot-DDL-storm trap). Direct psycopg2 (db_utils.safe_db silently
    SKIPS DDL)."""
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS analyst_notes (
                    id           SERIAL PRIMARY KEY,
                    week_of      DATE UNIQUE NOT NULL,
                    headline     TEXT NOT NULL,
                    body_md      TEXT NOT NULL,
                    citations    JSONB DEFAULT '[]'::jsonb,
                    numbers_used JSONB DEFAULT '[]'::jsonb,
                    created_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        return True
    except Exception as e:
        logger.warning("[analyst_note] ensure_schema failed: %s", str(e)[:160])
        return False
    finally:
        try: c.close()
        except Exception: pass


def _week_of(now: _dt.date | None = None) -> _dt.date:
    """Monday of the current ISO week — the idempotency key."""
    d = now or _dt.datetime.utcnow().date()
    return d - _dt.timedelta(days=d.weekday())


# ── 1) GATHER structured inputs (all fail-soft) ───────────────────────
def gather_inputs() -> dict:
    """Everything the note may reason over. The composer may use ONLY the
    numbers present here — that is what the fence verifies afterwards."""
    inputs: dict = {"leads": [], "deals_7d": [], "news_7d": [],
                    "rag_context": [], "lessons": []}

    # a) The media desk's ranked slate — the same tables/helpers it uses
    #    (DCPI movers + build markets, deals, queue, tenants, facilities).
    try:
        from routes.media_editorial import rank_data_events
        inputs["leads"] = [
            {k: v for k, v in (L or {}).items()
             if k in ("kind", "headline_number", "trend", "so_what",
                      "source_url", "dedup_key")}
            for L in (rank_data_events() or [])[:8]
            if (L or {}).get("kind") != "analyst_note"  # never self-feed
        ]
    except Exception as e:
        logger.warning("[analyst_note] rank_data_events failed: %s", str(e)[:160])

    # b) Direct SQL recency — deals + news of the last 7 days.
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT id, date, buyer, seller, value, mw, market
                      FROM deals
                     WHERE buyer IS NOT NULL AND buyer <> ''
                       AND (value IS NOT NULL OR mw IS NOT NULL)
                       AND date >= CURRENT_DATE - 7
                     ORDER BY date DESC NULLS LAST
                     LIMIT 6
                """)
                inputs["deals_7d"] = [
                    {"id": str(r[0]),
                     "date": (r[1].isoformat() if hasattr(r[1], "isoformat")
                              else str(r[1] or "")),
                     "buyer": r[2], "seller": r[3],
                     "value_m": (float(r[4]) if r[4] is not None else None),
                     "mw": (float(r[5]) if r[5] is not None else None),
                     "market": r[6]}
                    for r in (cur.fetchall() or [])]
        except Exception as e:
            logger.warning("[analyst_note] deals probe failed: %s", str(e)[:160])
            try: c.rollback()
            except Exception: pass
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT id, title, source, url, published_at
                      FROM news_articles
                     WHERE published_at > NOW() - INTERVAL '7 days'
                       AND title IS NOT NULL AND title <> ''
                     ORDER BY published_at DESC
                     LIMIT 10
                """)
                inputs["news_7d"] = [
                    {"id": str(r[0]), "title": r[1], "source": r[2],
                     "url": r[3], "published_at": str(r[4] or "")}
                    for r in (cur.fetchall() or [])]
        except Exception as e:
            logger.warning("[analyst_note] news probe failed: %s", str(e)[:160])
            try: c.rollback()
            except Exception: pass
        try: c.close()
        except Exception: pass

    # c) Semantic recall over the brain corpus (rerank fail-soft → []).
    try:
        from routes.brain_rag import retrieve_context
        inputs["rag_context"] = [
            {"source_table": r.get("source_table"),
             "source_id": str(r.get("source_id")),
             "text": (r.get("text") or "")[:400]}
            for r in (retrieve_context(
                "what moved in data-center power, grid capacity, deals and "
                "markets this week", k=6,
                corpus=["news_articles", "deals"]) or [])]
    except Exception as e:
        logger.warning("[analyst_note] retrieve_context failed: %s", str(e)[:160])

    # d) Past-outcome lessons — self-awareness, NOT citable data.
    try:
        from routes.brain_rag import retrieve_lessons
        inputs["lessons"] = [
            (r.get("text") or "")[:300]
            for r in (retrieve_lessons(
                "weekly analyst note synthesis media publishing", k=4) or [])]
    except Exception as e:
        logger.warning("[analyst_note] retrieve_lessons failed: %s", str(e)[:160])

    return inputs


def _citable_universe(inputs: dict) -> list[dict]:
    """The ONLY (source_table, source_id) pairs a citation may reference."""
    out: list[dict] = []
    for d in inputs.get("deals_7d") or []:
        out.append({"source_table": "deals", "source_id": str(d.get("id")),
                    "label": f"{d.get('buyer') or ''}/{d.get('seller') or ''} deal"})
    for n in inputs.get("news_7d") or []:
        out.append({"source_table": "news_articles",
                    "source_id": str(n.get("id")),
                    "label": (n.get("title") or "")[:80]})
    for r in inputs.get("rag_context") or []:
        if r.get("source_table") and r.get("source_id"):
            out.append({"source_table": str(r["source_table"]),
                        "source_id": str(r["source_id"]),
                        "label": (r.get("text") or "")[:80]})
    for L in inputs.get("leads") or []:
        if L.get("dedup_key"):
            out.append({"source_table": "media_leads",
                        "source_id": str(L["dedup_key"]),
                        "label": (L.get("headline_number") or "")[:80]})
    return out


# ── 3) ★HONEST-NUMBERS FENCE (whitelist twin of _fence_fabricated_figures) ─
_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# Unit/currency context DEFEATS the small-integer exemption: "$9B", "8 GW",
# "$12", "7%" are real-world claims, not list positions. Matched against the
# text immediately AFTER the number — attached ("9B", "34%", "65/MWh") or the
# immediate neighbor token ("8 GW", "5 MTok"). Case-insensitive. The negative
# lookahead keeps ordinary words out: "3 markets" must NOT match [bmk]→"m".
_UNIT_AFTER = re.compile(
    r"^\s*/?\s*("
    r"%|percent(?:age)?|pct|bps|"
    r"bn|mn|tn|billion(?:s)?|million(?:s)?|trillion(?:s)?|thousand(?:s)?|"
    r"[kmgt]wh|[kmgt]w|mtok|tokens?|tok|"
    r"[bmk]"
    r")(?![A-Za-z0-9&])", re.I)  # & keeps "3 M&A deals" a bare count
# Currency SYMBOL attached/adjacent before the number: "$9B", "US$ 12", "€40".
_CURRENCY_BEFORE = re.compile(r"[$€£]\s*$")
# Currency CODE as the immediate previous token: "USD 12".
_CURRENCY_WORDS = {"$", "usd", "eur", "gbp", "us$", "c$", "a$"}


def _has_unit_context(sentence: str, start: int, end: int) -> bool:
    """True when the numeric token at sentence[start:end] carries adjacent
    unit/currency context — same token ("$9B", "34%") or immediate neighbor
    ("8 GW", "USD 12"). Adjacent means: attached, or the very next/previous
    whitespace-separated token."""
    before = sentence[:start]
    after = sentence[end:]
    if _CURRENCY_BEFORE.search(before):
        return True
    if _UNIT_AFTER.match(after):  # ^\s* also covers the neighbor token
        return True
    prev = before.rstrip().rsplit(None, 1)
    prev_tok = (prev[-1] if prev else "").strip(".,;:()").lower()
    if prev_tok in _CURRENCY_WORDS:
        return True
    return False


def _norm_forms(tok: str, scaled: bool = False) -> set:
    """Normalized string forms of one numeric token. With scaled=True (input
    side ONLY) also admit /1000 and *1000 so '$10.0B' in the note matches
    value_m=10000 in the inputs and 'GW' matches 'MW'. The note side is never
    scaled, bounding the tolerance at one unit-hop."""
    t = tok.replace(",", "")
    forms = {t}
    try:
        f = float(t)
    except Exception:
        return forms
    vals = (f, f / 1000.0, f * 1000.0) if scaled else (f,)
    for v in vals:
        forms.add(f"{v:g}")
        forms.add(f"{v:.1f}")
        try:
            forms.add(str(int(round(v))))
        except Exception:
            pass
    return forms


def _allowed_numbers(inputs_blob: str) -> set:
    """Every numeric form present in the structured inputs (scale-tolerant)."""
    allowed: set = set()
    for m in _NUM_TOKEN.finditer(inputs_blob or ""):
        allowed |= _norm_forms(m.group(0), scaled=True)
    return allowed


def _fence_unsourced_figures(body_md: str, allowed: set) -> tuple[str, list[dict]]:
    """Strip every sentence of body_md that carries a figure NOT present in
    the structured inputs. Returns (clean_md, stripped). Exempt: 4-digit
    years and BARE small integers <= 12 (list positions / '7 days' noise) —
    the exemptions apply ONLY when the number has NO unit/currency context.
    '$9B', '8 GW', '$12', '7%' are claims and MUST be sourced; '12 deals'
    and 'top 3 markets' are bare counts and pass."""
    stripped: list[dict] = []
    kept_lines: list[str] = []
    for line in (body_md or "").split("\n"):
        if not line.strip():
            kept_lines.append(line)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept: list[str] = []
        for s in sentences:
            offender = None
            for m in _NUM_TOKEN.finditer(s):
                tok = m.group(0)
                base = tok.replace(",", "")
                if not _has_unit_context(s, m.start(), m.end()):
                    if _YEAR_RE.match(base):
                        continue
                    if base.isdigit() and int(base) <= 12:
                        continue
                if not (_norm_forms(tok, scaled=False) & allowed):
                    offender = tok
                    break
            if offender is None:
                kept.append(s)
            else:
                stripped.append({"sentence": s[:200], "figure": offender})
        if kept:
            kept_lines.append(" ".join(kept))
    return "\n".join(kept_lines).strip(), stripped


# ── 2) COMPOSE — one structured-outputs LLM call ──────────────────────
_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "body_md": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_table": {"type": "string"},
                    "source_id": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["source_table", "source_id", "label"],
                "additionalProperties": False,
            },
        },
        "numbers_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "body_md", "citations", "numbers_used"],
    "additionalProperties": False,
}

_NOTE_SYSTEM = (
    "You are THE DC HUB ANALYST writing the weekly Analyst Note: 'What moved "
    "in data-center power this week' for site-selection leads, hyperscaler "
    "capacity planners, developers and investors. Dry, specific, confident — "
    "explaining, not selling.\n\n"
    "HARD RULES:\n"
    "1. EVERY figure (MW, GW, $, scores, counts, percentages) MUST be copied "
    "from the STRUCTURED INPUTS verbatim. NEVER compute, extrapolate, or "
    "invent a number — an automated fence deletes any sentence whose figure "
    "is not in the inputs.\n"
    "2. headline: one sentence that LEADS with the single most newsworthy "
    "number of the week.\n"
    "3. body_md: 600-900 words of markdown. Short ## sections (e.g. 'The "
    "number of the week', 'Markets', 'Deals', 'Grid'), 2-4 sentence "
    "paragraphs. Open on the biggest move; close on what it means for a real "
    "build/capex decision next week.\n"
    "4. citations: cite ONLY items from the CITABLE SOURCES list, copying "
    "source_table and source_id exactly.\n"
    "5. numbers_used: every figure you used, as it appears in the inputs.\n"
    "6. The LESSONS block is self-awareness about past publishing outcomes — "
    "never cite it and never quote its numbers.\n"
    "7. No marketing voice, no 'DC Hub is the authority', no fear closers, "
    "no disparaging other companies or AI platforms. Reply with ONLY the "
    "JSON object. No prose before or after."
)


def _models() -> list:
    """Synthesis tier: sonnet-class via brain_models when available, else the
    haiku id (the cheapest confirmed-valid model)."""
    try:
        from routes.brain_models import brain_model_for, resolve_chain
        chain = resolve_chain(brain_model_for("routine")) or []
        if chain:
            return chain
    except Exception:
        pass
    return ["claude-haiku-4-5"]


def _call_claude(prompt: str) -> tuple:
    """ONE composing call per model attempt — mirrors brain_feature_proposer.
    _call_claude: structured attempt → (on a 400) legacy retry on the SAME
    model → walk the brain_models fallback chain on 404/400."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None, "no_api_key"
    try:
        from utils.anthropic_helper import anthropic_messages_url
        _url = anthropic_messages_url()
    except Exception:
        _url = "https://api.anthropic.com/v1/messages"
    try:
        from routes import brain_llm_structured as _so
    except Exception:
        _so = None
    models = _models()
    last_err = None
    for i, model in enumerate(models):
        _attempts = ((True, False)
                     if (_so is not None
                         and _so.structured_active(model, _NOTE_SCHEMA))
                     else (False,))
        _walk_chain = False
        for _structured in _attempts:
            if _so is not None:
                body_dict, _ = _so.build_messages_body(
                    model, _NOTE_SYSTEM,
                    [{"role": "user", "content": prompt}],
                    4000, _NOTE_SCHEMA if _structured else None)
            else:
                body_dict = {"model": model, "max_tokens": 4000,
                             "system": _NOTE_SYSTEM,
                             "messages": [{"role": "user", "content": prompt}]}
            req = urllib.request.Request(
                _url, data=json.dumps(body_dict).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-API-Key": api_key,
                         "User-Agent": "dchub-analyst-note/1.0",
                         "Anthropic-Version": "2023-06-01"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", ""), None
                return None, "no_text_block"
            except urllib.error.HTTPError as e:
                last_err = f"http_{e.code}"
                if _structured and e.code == 400:
                    try:
                        _etext = e.read().decode("utf-8", "replace")
                    except Exception:
                        _etext = ""
                    if _so is not None and _so.looks_like_structured_rejection(
                            e.code, _etext):
                        _so.mark_model_unsupported(model)
                    continue  # legacy retry, SAME model
                if e.code in (404, 400) and i + 1 < len(models):
                    _walk_chain = True
                    break
                return None, last_err
            except Exception as e:
                return None, f"call_fail: {repr(e)[:160]}"
        if _walk_chain:
            continue
    return None, last_err or "all_models_failed"


def _parse_note_json(text: str):
    """Structured mode → strict json.loads; legacy → fence-strip + substring."""
    if not text:
        return None
    try:
        from routes.brain_llm_structured import parse_structured_json
        parsed = parse_structured_json(text)
        if parsed is not None:
            return parsed
    except Exception:
        pass
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        parsed = json.loads(s[i:j + 1])
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _compose_prompt(inputs: dict, citable: list, week: _dt.date) -> str:
    return (
        f"Write the DC Hub Analyst Note for the week of {week.isoformat()} "
        f"(generated {_dt.datetime.utcnow().isoformat()}Z).\n\n"
        "STRUCTURED INPUTS (the ONLY permitted source of figures):\n"
        + json.dumps({k: inputs.get(k) for k in
                      ("leads", "deals_7d", "news_7d", "rag_context")},
                     default=str, indent=1)[:14000]
        + "\n\nCITABLE SOURCES (cite ONLY these, source_table+source_id verbatim):\n"
        + json.dumps(citable, default=str, indent=1)[:6000]
        + "\n\nLESSONS (self-awareness only — do NOT cite, do NOT reuse figures):\n"
        + json.dumps(inputs.get("lessons") or [], default=str)[:2000]
    )


# ── 4) GENERATE + PERSIST (idempotent per week_of) ────────────────────
def _existing_note(week: _dt.date):
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("SELECT id, headline FROM analyst_notes WHERE week_of = %s",
                        (week,))
            r = cur.fetchone()
        return {"id": r[0], "headline": r[1]} if r else None
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def generate_analyst_note(force: bool = False) -> dict:
    """The weekly synthesis. Idempotent per week_of: a second run in the same
    ISO week no-ops (cheap SELECT) unless force=True. Never raises."""
    if _disabled():
        return {"ok": False, "skipped": "disabled (ANALYST_NOTE_DISABLED)"}
    week = _week_of()
    if not _ensure_schema():
        return {"ok": False, "error": "schema_unavailable"}
    existing = _existing_note(week)
    if existing and not force:
        return {"ok": True, "skipped": "already_generated",
                "week_of": week.isoformat(), "id": existing["id"],
                "headline": existing["headline"]}

    inputs = gather_inputs()
    if not any(inputs.get(k) for k in ("leads", "deals_7d", "news_7d",
                                       "rag_context")):
        return {"ok": False, "error": "no_inputs",
                "note": "every input source came back empty — nothing to write"}
    citable = _citable_universe(inputs)

    text, err = _call_claude(_compose_prompt(inputs, citable, week))
    if err or not text:
        return {"ok": False, "error": f"llm_failed: {err}"}
    parsed = _parse_note_json(text)
    if not parsed or not (parsed.get("headline") and parsed.get("body_md")):
        return {"ok": False, "error": "unparseable_note"}

    # ★ Fence: every figure in the note must exist in the structured inputs.
    inputs_blob = json.dumps(
        {k: inputs.get(k) for k in ("leads", "deals_7d", "news_7d",
                                    "rag_context", "lessons")}, default=str)
    allowed = _allowed_numbers(inputs_blob)
    body_md, stripped = _fence_unsourced_figures(parsed["body_md"], allowed)
    headline, hl_stripped = _fence_unsourced_figures(parsed["headline"], allowed)
    if not headline:
        headline = "What moved in data-center power this week"
    if not body_md:
        return {"ok": False, "error": "fence_stripped_everything",
                "stripped": stripped}

    # Citations may only reference the provided universe.
    universe = {(c0["source_table"], str(c0["source_id"])) for c0 in citable}
    citations = [c1 for c1 in (parsed.get("citations") or [])
                 if isinstance(c1, dict)
                 and (str(c1.get("source_table")), str(c1.get("source_id")))
                 in universe]
    numbers_used = [str(n) for n in (parsed.get("numbers_used") or [])][:60]

    c = _conn()
    if c is None:
        return {"ok": False, "error": "db_unavailable"}
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO analyst_notes
                       (week_of, headline, body_md, citations, numbers_used)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (week_of) DO UPDATE
                   SET headline = EXCLUDED.headline,
                       body_md = EXCLUDED.body_md,
                       citations = EXCLUDED.citations,
                       numbers_used = EXCLUDED.numbers_used,
                       created_at = NOW()
                RETURNING id
            """, (week, headline[:500], body_md, json.dumps(citations),
                  json.dumps(numbers_used)))
            note_id = (cur.fetchone() or [None])[0]
    except Exception as e:
        return {"ok": False, "error": f"persist_failed: {str(e)[:160]}"}
    finally:
        try: c.close()
        except Exception: pass

    return {"ok": True, "id": note_id, "week_of": week.isoformat(),
            "headline": headline, "words": len(body_md.split()),
            "citations": len(citations),
            "fence_stripped": len(stripped) + len(hl_stripped),
            "stripped": (stripped + hl_stripped)[:10],
            "url": PUBLIC_PATH}


# ── latest-note readers ───────────────────────────────────────────────
def latest_note():
    """Newest analyst_notes row as a dict, or None. Fail-soft."""
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, week_of, headline, body_md, citations,
                       numbers_used, created_at
                  FROM analyst_notes
                 ORDER BY week_of DESC LIMIT 1
            """)
            r = cur.fetchone()
        if not r:
            return None
        return {"id": r[0],
                "week_of": (r[1].isoformat() if hasattr(r[1], "isoformat")
                            else str(r[1])),
                "headline": r[2], "body_md": r[3],
                "citations": r[4] or [], "numbers_used": r[5] or [],
                "created_at": (r[6].isoformat() if hasattr(r[6], "isoformat")
                               else str(r[6] or ""))}
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def analyst_note_lead():
    """The media LEAD for the latest note (<=7 days old) — consumed by
    media_editorial.rank_data_events(). The media machine's OWN gates
    (number-lead, novelty, claim-verify, approval) decide posting; this
    module never posts anything. Returns None when there is no fresh note
    or the headline wouldn't survive the number-lead publish gate."""
    n = latest_note()
    if not n:
        return None
    try:
        week = _dt.date.fromisoformat(str(n["week_of"]))
        if (_dt.datetime.utcnow().date() - week).days > 7:
            return None
    except Exception:
        return None
    try:
        from routes.media_editorial import leads_with_number
        if not leads_with_number(n.get("headline") or ""):
            return None  # would bounce off the publish gate — don't offer it
    except Exception:
        pass
    return {
        "kind": "analyst_note",
        "headline_number": (n.get("headline") or "")[:220],
        "trend": "the brain's weekly synthesis of what moved in data-center power",
        "so_what": ("the full cited Analyst Note is live at "
                    f"{PUBLIC_URL} — every figure fenced to real inputs."),
        "source_url": PUBLIC_URL,
        "dedup_key": f"analyst_note:{n['week_of']}",
        "score": 16.0,
    }


# ── HTML rendering (house dark style — matches open_data /research/<slug>) ─
def _md_to_html(md: str) -> str:
    """Tiny dependency-free markdown subset: ##/### headings, **bold**,
    '-' bullets, paragraphs. Input is HTML-escaped FIRST (the body is
    LLM-authored)."""
    out: list[str] = []
    in_list = False
    for raw in (md or "").split("\n"):
        line = _esc(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{stripped[2:].strip()}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not stripped:
            continue
        if stripped.startswith("### "):
            out.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h2>{stripped[2:]}</h2>")
        else:
            out.append(f"<p>{stripped}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


@analyst_note_bp.route(PUBLIC_PATH, methods=["GET"])
def analyst_note_page():
    n = latest_note()
    if not n:
        return Response("<h1>No Analyst Note published yet</h1>"
                        "<p>The first weekly note lands Thursday 14:00 UTC.</p>",
                        status=404, mimetype="text/html")
    cites = n.get("citations") or []
    cite_html = "".join(
        f"<li><span class='mono'>{_esc(str(c.get('source_table','')))}"
        f"#{_esc(str(c.get('source_id','')))}</span> — "
        f"{_esc(str(c.get('label',''))[:120])}</li>"
        for c in cites if isinstance(c, dict))
    body_html = _md_to_html(n.get("body_md") or "")
    week = _esc(str(n.get("week_of") or ""))
    headline = _esc(n.get("headline") or "")
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Analyst Note · Week of {week} · DC Hub Research</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="DC Hub Analyst Note · Week of {week}">
<meta property="og:description" content="{headline}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#0a0a12; --card:#11121a; --bd:#1f2030; --bd-hi:#2a2c3e;
  --tx:#fff; --tx2:#9ca3af; --tx3:#6b7280;
  --acc:#6366f1; --acc-light:#818cf8;
}}
*{{box-sizing:border-box}}
body{{font-family:'Inter',-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--tx);margin:0;padding:0;line-height:1.7;-webkit-font-smoothing:antialiased}}
.mono,code{{font-family:'JetBrains Mono',monospace}}
.top-nav{{border-bottom:1px solid var(--bd);background:rgba(10,10,18,0.85);backdrop-filter:blur(8px);position:sticky;top:0;z-index:100}}
.top-nav-inner{{max-width:880px;margin:0 auto;padding:1rem 1.5rem;display:flex;align-items:center;justify-content:space-between;gap:1.5rem}}
.logo{{font-weight:800;font-size:1.05rem;color:var(--tx);text-decoration:none}}
.logo span{{color:var(--acc)}}
.nav-links{{display:flex;gap:1.5rem;flex-wrap:wrap}}
.nav-links a{{color:var(--tx2);text-decoration:none;font-size:0.92rem;font-weight:500}}
.nav-links a:hover{{color:var(--tx)}}
.wrap{{max-width:880px;margin:0 auto;padding:3rem 1.5rem}}
.crumbs{{font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--tx3);margin-bottom:1.5rem}}
.crumbs a{{color:var(--acc-light);text-decoration:none}}
h1{{font-size:clamp(1.8rem,4vw,2.6rem);margin:0 0 0.4rem;font-weight:800;letter-spacing:-0.025em;line-height:1.15}}
.subtitle{{color:var(--tx2);font-family:'JetBrains Mono',monospace;font-size:0.92rem;margin:0 0 2.5rem;text-transform:uppercase;letter-spacing:0.06em}}
.note{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:2rem 2.25rem}}
.note h2{{font-size:1.1rem;margin:1.8rem 0 0.6rem;color:var(--acc-light);letter-spacing:0.02em}}
.note h3{{font-size:1rem;margin:1.4rem 0 0.5rem;color:var(--tx)}}
.note p{{margin:0.7rem 0;color:#ddd;font-size:0.98rem}}
.note ul{{padding-left:1.4rem;margin:0.5rem 0}}
.note li{{margin:0.4rem 0;color:#ddd;font-size:0.95rem}}
.cite{{color:var(--tx3);font-size:0.85rem;margin-top:3rem;border-top:1px solid var(--bd);padding-top:1.5rem;font-family:'JetBrains Mono',monospace}}
.cite strong{{color:var(--tx2)}}
.cite ul{{padding-left:1.2rem}}
.cite li{{margin:0.35rem 0}}
@media(max-width:600px){{.nav-links{{display:none}}}}
</style>
</head><body>
<nav class="top-nav">
  <div class="top-nav-inner">
    <a class="logo" href="/">DC <span>Hub</span></a>
    <div class="nav-links">
      <a href="/api/v1/dcpi/page">DCPI</a>
      <a href="/api/v1/research">Research</a>
      <a href="/api/v1/data">Open Data</a>
      <a href="/pricing">Pricing</a>
    </div>
  </div>
</nav>
<div class="wrap">
  <div class="crumbs"><a href="/api/v1/research">Research</a> / Analyst Note</div>
  <h1>{headline}</h1>
  <p class="subtitle">The DC Hub Analyst · Week of {week} · brain-authored, fenced &amp; cited</p>
  <div class="note">
{body_html}
  </div>
  <div class="cite">
    <strong>Sources cited</strong> — every figure above is verified against DC Hub's live structured inputs; sentences that failed verification were removed before publish.
    <ul>{cite_html or "<li>No row-level citations recorded for this note.</li>"}</ul>
    <p>DC Hub — the live infrastructure data layer for AI agents. JSON: <a href="/api/v1/analyst-note/latest" style="color:var(--acc-light)">/api/v1/analyst-note/latest</a></p>
  </div>
</div>
</body></html>"""
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=900"
    return resp


@analyst_note_bp.route("/api/v1/analyst-note/latest", methods=["GET"])
def analyst_note_latest():
    n = latest_note()
    if not n:
        return jsonify({"ok": False, "error": "no_note_yet"}), 404
    n["ok"] = True
    n["url"] = PUBLIC_PATH
    n["citation"] = "DC Hub Analyst Note, dchub.cloud" + PUBLIC_PATH
    return jsonify(n), 200


@analyst_note_bp.route("/api/v1/analyst-note/generate", methods=["POST"])
def analyst_note_generate():
    """Cron target (heartbeat _hit sends X-Admin-Key). Idempotent per week_of.
    ?force=1 regenerates the current week."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    try:
        return jsonify(generate_analyst_note(force=force)), 200
    except Exception as e:
        logger.exception("[analyst_note] generate failed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {str(e)[:160]}"}), 200


@analyst_note_bp.route("/api/v1/analyst-note/health", methods=["GET"])
def analyst_note_health():
    n = latest_note()
    return jsonify({
        "blueprint": "analyst_note",
        "disabled": _disabled(),
        "latest_week_of": (n or {}).get("week_of"),
        "current_week_of": _week_of().isoformat(),
        "has_note_this_week": bool(n and (n.get("week_of")
                                          == _week_of().isoformat())),
    }), 200
