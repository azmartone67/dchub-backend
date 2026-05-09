"""Phase 118 — Ask the Index. AI chat over the DCPI data layer.

GET/POST /api/v1/dcpi/ask?q=<question>
  Returns:
    {answer: "...", citations: [{slug, name, score}], q: "..."}
"""
import os, json, re
from flask import Blueprint, request, jsonify
import psycopg2, psycopg2.extras
import requests

dcpi_ask_bp = Blueprint("dcpi_ask", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _get_index_snapshot():
    """Pull the current full DCPI snapshot, compact form."""
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (market_slug)
              market_slug, market_name, state, iso, latitude, longitude,
              excess_power_score, constraint_score, time_to_power_months,
              verdict, top_risks_json, top_opportunities_json
            FROM market_power_scores
            ORDER BY market_slug, computed_at DESC
        """)
        rows = cur.fetchall()
    return rows


def _format_context(snapshot):
    """Format the index as compact text the model can reason over."""
    lines = ["DCPI · Data Center Power Index — current snapshot:"]
    lines.append("market_slug | name | state | iso | excess | constraint | ttp_months | verdict | lat,lon")
    for r in snapshot:
        lines.append(
            f"{r['market_slug']} | {r['market_name']} | {r['state']} | {r['iso']} | "
            f"{r['excess_power_score']} | {r['constraint_score']} | "
            f"{r['time_to_power_months']} | {r['verdict']} | "
            f"{r['latitude']:.3f},{r['longitude']:.3f}"
        )
    return "\n".join(lines)


SYSTEM_PROMPT = """You are the AI assistant for the DC Hub Power Index (DCPI), a daily-updated power-availability scoring system for U.S. data center markets.

For each user question:
1. Use ONLY the provided DCPI snapshot data — never hallucinate scores.
2. Cite every market by name AND its excess_power_score / constraint_score / verdict.
3. If the user asks about distance, calculate Haversine on lat/lon (miles).
4. If the user's question requires data we don't have, say so and recommend the closest answer we can give.
5. Keep answers under 250 words. Lead with the recommendation, then justify with 2-4 specific markets cited.

Output format (plain prose with embedded citations like [Williston ND · Excess 87 · BUILD]). No JSON. No headers. Conversational but specific.
"""


def _call_anthropic(question: str, context: str) -> tuple[str, list]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return "I'm not able to answer right now — Anthropic API key not configured.", []
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 600,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {"role": "user",
                     "content": f"DCPI snapshot:\n\n{context}\n\nQuestion: {question}"}
                ],
            },
            timeout=20,
        )
        if r.status_code != 200:
            return f"(brain returned HTTP {r.status_code}: {r.text[:200]})", []
        data = r.json()
        text = "".join(b.get("text","") for b in data.get("content", []) if b.get("type")=="text")
        return text.strip() or "(empty answer)", _extract_citations(text)
    except Exception as e:
        return f"(brain error: {type(e).__name__}: {str(e)[:200]})", []


def _extract_citations(text: str) -> list:
    """Pull market mentions out of the answer for hyperlink rendering on the
    client side. Looks for [Market Name · ...] bracketed citations."""
    cites = []
    for m in re.finditer(r"\[([^\]]+?)·([^\]]+?)\]", text):
        cites.append({"label": m.group(1).strip(), "detail": m.group(2).strip()})
    return cites


@dcpi_ask_bp.route("/api/v1/dcpi/ask", methods=["GET", "POST"])
def ask():
    q = (request.args.get("q") or
         (request.get_json(silent=True) or {}).get("q") or "").strip()
    if not q:
        return jsonify(error="provide a question via ?q=..."), 400

    snapshot = _get_index_snapshot()
    if not snapshot:
        return jsonify(answer="The DCPI hasn't been computed yet. Visit /api/v1/dcpi/recompute to seed it, then ask again.", citations=[]), 200

    context = _format_context(snapshot)
    answer, citations = _call_anthropic(q, context)
    return jsonify(q=q, answer=answer, citations=citations,
                   snapshot_size=len(snapshot)), 200
