"""
brain_rag.py — general context-assembly RAG (pgvector + Cohere embeddings).

Started as brain-corpus recall for the L6 planner; now a CORPUS-REGISTRY RAG:
"roll RAG out to a new corpus" = add a row to CORPORA (no new code), same as the
grid master shell's dataset registry. Numbers stay in SQL — this embeds only
UNSTRUCTURED prose (findings/recs/news/deals). Owner-greenlit 2026-07-03.

Store:  brain_corpus_embeddings(source_table, source_id, kind, text,
        embedding vector(1024), chunk_ix, meta)  — pgvector on Neon, one table
        for all corpora. Chunked corpora (market_narratives) store one row per
        ~150-300-token chunk, source_id='<slug>#<n>', provenance in meta.
Embed:  Cohere embed-english-v3.0 (1024-d, asymmetric search_document/search_query;
        OpenAI key is out of quota). Batched ≤96/call. Chunked market_narratives
        additionally get an Anthropic contextual-retrieval blurb (haiku + prompt
        caching) prepended before embed — BRAIN_RAG_CONTEXTUAL=0 to disable.
Recall: cosine (<=>), optionally scoped to one corpus.

Endpoints (admin, X-Admin-Key):
  POST /api/v1/admin/brain/rag/reindex?cap=500    — embed up to cap new rows (any corpus)
                                                    + GC orphaned embeddings (sources deleted)
  GET  /api/v1/admin/brain/rag/retrieve?q=&k=8&corpus=news_articles — test
  GET  /api/v1/admin/brain/rag/status             — per-corpus coverage
  POST /api/v1/admin/brain/rag/ingest-fix-history — fix_history corpus ingest
       (pushed gh-issue/commit docs + server-side resolved brain_findings)
Kill: BRAIN_RAG_DISABLED=1 (endpoints) / BRAIN_RAG_ENABLED unset (planner wiring).
Kept fresh by cron_heartbeat (brain_rag_reindex).
"""
import os
import re
import json
import hmac
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request

from routes.url_registry import build_public_url
from util.deals import deals_ok
from util.capacity_pipeline import cp_ok as _cp_ok

brain_rag_bp = Blueprint("brain_rag", __name__)

EMBED_MODEL = "embed-english-v3.0"
EMBED_DIM = 1024
_COHERE_BATCH = 96  # Cohere v1/embed hard limit

# ── rerank (r-rag-rerank 2026-07-04) ─────────────────────────────────
# Two-stage retrieval: over-fetch k*OVERFETCH candidates from the pgvector
# HNSW index (fast bi-encoder cosine), then Cohere rerank (cross-encoder)
# down to k. Lifts relevance@k ~15-40% over raw cosine. Fail-soft: any
# rerank error or missing key falls straight back to the cosine top-k, so
# recall never breaks. Toggle with BRAIN_RAG_RERANK=0.
RERANK_MODEL = "rerank-english-v3.0"
_RERANK_OVERFETCH = 4      # fetch k*4 candidates to rerank
_RERANK_MAX_FETCH = 60     # never over-fetch beyond this
def _rerank_on() -> bool:
    # Rerank is Cohere-only (cross-encoder). On any other embed provider skip it
    # (→ cosine order) rather than burn a Cohere call that would 429.
    if _embed_provider() != "cohere":
        return False
    return (os.environ.get("BRAIN_RAG_RERANK", "1").strip().lower()
            not in ("0", "false", "no", "off"))


# ── neutral rerank (r-rag-neutral-rerank 2026-07-25, shell #31) ──────
# The cross-encoder above is provider-locked, so the day the embed provider
# moved to mistral the pipeline silently lost its ENTIRE second stage — every
# consumer got raw cosine order and nothing went red. This leg restores a
# stage 2 for non-Cohere providers with a bounded lexical re-score of the
# over-fetched cosine candidates: no API call, no key, no new failure mode
# (any surprise falls back to cosine order). The bonus is deliberately small
# (0.08 max, ~the measured mistral gap between on-topic and nonsense) so it
# re-orders within the candidate set rather than overruling the embedding.
# Master toggle BRAIN_RAG_RERANK still rules; BRAIN_RAG_RERANK_NEUTRAL=0
# kills just this leg.
_NEUTRAL_STOP = frozenset((
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "with",
    "what", "is", "are", "how", "why", "when", "where", "which", "that",
    "this", "by", "from", "as", "at", "into", "out", "over", "under",
    "about", "across", "your", "our", "their", "its", "it", "they", "we",
    "you", "be", "will"))


def _neutral_rerank_on() -> bool:
    if _embed_provider() == "cohere":
        return False   # real cross-encoder available — use that instead
    if (os.environ.get("BRAIN_RAG_RERANK", "1").strip().lower()
            in ("0", "false", "no", "off")):
        return False
    return (os.environ.get("BRAIN_RAG_RERANK_NEUTRAL", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _lexical_rerank(query, base, k):
    """Re-order over-fetched candidates by cosine + a capped term-coverage
    bonus. Every result keeps its original "cosine" untouched (the gate
    contract from r-rag-cosine-passthrough); "score" becomes the blend and
    "_rerank"='neutral' marks the leg. Never raises — any surprise returns
    the cosine order."""
    try:
        terms, seen = [], set()
        for tok in re.findall(r"[a-z0-9]{3,}", (query or "").lower()):
            if tok not in _NEUTRAL_STOP and tok not in seen:
                seen.add(tok)
                terms.append(tok)
        terms = terms[:12]
        if not terms:
            return base[:int(k)]
        out = []
        for d in base:
            toks = set(re.findall(r"[a-z0-9]{3,}",
                                  (d.get("text") or "").lower()))
            cov = sum(1 for t in terms if t in toks) / float(len(terms))
            nd = dict(d)
            nd["score"] = round(float(d.get("cosine") or 0.0) + 0.08 * cov, 4)
            nd["_rerank"] = "neutral"
            out.append(nd)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:int(k)]
    except Exception:
        return base[:int(k)]


# ── corpus registry — add a row to roll RAG onto a new source (no new code) ──
# Each: source_table → {id (t-qualified ::text), text (SQL expr over alias t),
# kind, where}. All exprs are hardcoded/trusted (never user input).
CORPORA = {
    # fresh_col notes (r-rag-freshcol 2026-07-04) — every fresh_col below is
    # gated at runtime by _fresh_col_active() (live information_schema check:
    # column EXISTS and is a timestamp/date type) because the repo DDL lies
    # (brain_findings/power_plants schema-drift class). A wrong fresh_col
    # would otherwise make _pending's per-corpus SELECT raise → try/except →
    # that WHOLE corpus silently stops indexing, every run.
    #
    #   brain_findings.last_seen — column verified on LIVE via the public
    #     GET /api/v1/brain/findings/db-status introspection (2026-07-04).
    #     The canonical writer (routes/brain_findings_writer) re-stamps
    #     last_seen=NOW() while updating detail/count IN PLACE on every
    #     re-fire — exactly the insert-only staleness gap fresh_col closes.
    #   brain_strategic_recommendations.updated_at — TIMESTAMPTZ in the
    #     schema_repair DDL that created the table; actively re-stamped by
    #     _mark_pr_on_rec (spec-promoter flow, proven live by recs carrying
    #     pr_url/status='pr_drafted').
    #   news_articles — NO fresh_col: every active writer is INSERT … ON
    #     CONFLICT (id) DO NOTHING (news_engine.py); rows are never updated
    #     in place and no updated/modified-style column is referenced by any
    #     code path, so there is no usable freshness signal.
    #   deals.updated_at — added 2026-07-18 (r-rag-deals-fresh): the admin
    #     edit path (main.py admin_update_deal) now stamps it, so edited
    #     deals re-embed. Bulk one-shot fixers don't stamp (acceptable).
    #     On DBs without the column the live gate leaves insert-only.
    #   discovered_facilities.last_updated — re-stamped in place by two live
    #     crawlers (routes/discovery_routes.py upsert: SET last_updated=
    #     EXCLUDED.last_updated; routes/osm_crawler.py: SET last_updated=
    #     NOW()), so the column exists on live. CAVEAT: some writers pass ISO
    #     strings, and sibling column merged_at is TEXT on live — if
    #     last_updated is TEXT too, the type gate leaves this corpus on
    #     insert-only (visible in /status fresh_cols) instead of breaking it.
    "brain_findings": {
        "id": "t.id::text", "kind": "finding",
        "text": "coalesce(t.issue,'') || ' — ' || coalesce(t.detail,'')",
        "where": "coalesce(t.issue,'') <> ''",
        "fresh_col": "last_seen"},
    "brain_strategic_recommendations": {
        "id": "t.id::text", "kind": "recommendation",
        "text": "coalesce(t.title,'') || ' — ' || coalesce(t.spec_md,'')",
        "where": "coalesce(t.title,'') <> ''",
        "fresh_col": "updated_at"},
    "news_articles": {
        "id": "t.id::text", "kind": "news",
        "text": "coalesce(t.title,'') || ' — ' || coalesce(t.summary,'')",
        "where": "coalesce(t.title,'') <> ''"},
    # ★ QUARANTINE GATE (r-rag-deals-quarantine 2026-07-27) — same class as the
    # press_releases publish gate below, and the same omission: `deals` is in
    # PUBLIC_CORPORA, so anything embedded here is retrievable with a CC-BY
    # citation stamp on the UNAUTHENTICATED /api/v1/rag/search. The 07-17
    # integrity wave quarantined 2,868 rows via data_flag and taught the served
    # query (/api/deals, list_transactions) to exclude them — but this registry
    # never learned, so 2,811 of 4,348 embedded deal chunks (64.7%) pointed at
    # rows the API deliberately refuses to serve: 2,766 duplicates, 34
    # unit-garbage NER fragments ("gap After → Orbion and", "Musk quietly →
    # mobile gas") and 11 example.com seed placeholders.
    # Adding the filter is sufficient to REMOVE them: _sweep_orphans already
    # deletes embeddings whose source row "no longer satisfies the registry
    # where" (capped 1,000/corpus/run, so ~3 reindex ticks to drain).
    # ★ LEFT(...,11) NOT LIKE 'quarantine_%' — spec['where'] is f-string
    # interpolated into queries that DO pass a params tuple (_corpus_total,
    # _count_orphans, _sweep_orphans), so a literal % here would make psycopg2
    # %-substitute and 500 all three (reference_psycopg2_empty_tuple_percent_trap;
    # this exact table taught this exact lesson on 07-17).
    # ★ TEXT TEMPLATE v2 (r-rag-deals-template 2026-07-27). The v1 expression
    # concatenated five fields with fixed separators, so a row carrying only a
    # buyer rendered as "Google →  (, ) " — 811 of 1,544 served deals (52.5%)
    # have neither seller nor notes, and 11 DISTINCT deals collapsed to one
    # byte-identical string, i.e. one vector standing for eleven deals. Worse,
    # 1,015 rows carry a value or MW that v1 threw away entirely.
    # v2 uses concat_ws + nullif so empty fields vanish instead of emitting
    # punctuation, and adds value ($M — the post-07-17 convention), MW and the
    # date. Measured on live: worst identical render 11 → 4, collapsed rows
    # 127 → 47, mean chunk length 74 chars, zero empty renders.
    # ★ Changing this expression does NOT re-embed on its own: _pending only
    # picks rows with no embedding or a moved fresh_col. The one-time re-embed
    # is a `SET updated_at = NOW()` bump on served rows, which is exactly what
    # fresh_col exists for and keeps the old vectors searchable meanwhile.
    "deals": {
        "id": "t.id::text", "kind": "deal",
        "text": ("concat_ws(' · ',"
                 " nullif(concat_ws(' → ', nullif(trim(t.buyer),''),"
                 "                        nullif(trim(t.seller),'')),''),"
                 " nullif(concat_ws(', ', nullif(trim(t.type),''),"
                 "                        nullif(trim(coalesce(t.market,t.region)),'')),''),"
                 " CASE WHEN t.value IS NOT NULL"
                 "      THEN '$' || round(t.value::numeric,1)::text || 'M' END,"
                 " CASE WHEN t.mw IS NOT NULL"
                 "      THEN round(t.mw::numeric,1)::text || ' MW' END,"
                 " nullif(trim(coalesce(t.date, t.year::text)),''),"
                 " nullif(trim(t.notes),''))"),
        "where": ("(coalesce(t.buyer,'') <> '' OR coalesce(t.seller,'') <> '')"
                  " AND " + deals_ok("t")),
        "fresh_col": "updated_at"},
    "discovered_facilities": {
        "id": "t.id::text", "kind": "facility",
        "text": ("coalesce(t.name,'') || ' — ' || coalesce(t.provider,'') || ' · ' || "
                 "concat_ws(', ', t.city, t.state, t.country) || ' · ' || "
                 "coalesce(t.market,'') || ' ' || coalesce(t.facility_type,'')"),
        "where": "coalesce(t.name,'') <> '' AND coalesce(t.is_duplicate, 0) = 0",
        "fresh_col": "last_updated"},
    # ── wave-3 corpus expansion (brain-ascension #28, 2026-07-25) ────────
    # The 07-25 RAG audit found whole prose shelves unindexed while agents
    # asked questions they could answer. Schemas LIVE-VERIFIED against Neon
    # (repo DDL drifts — power_plants trap): fresh_col only where the live
    # column is a real timestamp; TEXT-timestamp tables stay insert-only.
    # ★ PUBLISH GATE (2026-07-25, adversarial review): press_releases is in
    # PUBLIC_CORPORA and the text expr carries the FULL body, so without this
    # filter the 11 unpublished drafts (136 published of 147 live) would be
    # semantically searchable — with a CC-BY-4.0 citation stamp — on the
    # unauthenticated /api/v1/rag/search. Draft writers (brain_press_loop,
    # ai_citation_tracker) insert published=FALSE precisely because that copy
    # is fact-check-gated. Every other public reader gates on published=TRUE;
    # this must too. NOTE: the live draft marker is the `published` BOOLEAN —
    # this table has NO status column.
    "press_releases": {                       # 147 rows, 136 published
        "id": "t.id::text", "kind": "press",
        "text": ("coalesce(t.title,'') || ' — ' || coalesce(t.summary, t.subheadline, '') "
                 "|| ' ' || coalesce(t.body,'')"),
        "where": "coalesce(t.title,'') <> '' AND coalesce(t.published, FALSE) IS TRUE",
        "fresh_col": "published_at"},
    "announcements": {                        # 12.4k rows — industry announcements
        "id": "t.id::text", "kind": "announcement",
        "text": ("coalesce(t.title,'') || ' — ' || coalesce(t.summary, t.content, '') "
                 "|| ' · ' || coalesce(t.companies,'') || ' ' || coalesce(t.locations,'')"),
        "where": "coalesce(t.title,'') <> ''"},
    # ★ PROMOTION GATE (2026-07-25, adversarial review): rows land as
    # row_status='candidate' from machine scrapes and only become
    # 'published' when a human promotes them (agentic_master_shell). Live
    # split is 9 published / 235 candidate / 1 REJECTED — so without this
    # filter 236 of 245 unvetted rows (including one a human explicitly
    # rejected) would be served to agents as authoritative, cited DC Hub
    # permitting guidance. Every other reader filters row_status='published'.
    "permitting_intel": {                     # 245 rows, 9 published
        "id": "t.id::text", "kind": "permitting",
        "text": ("concat_ws(', ', t.jurisdiction, t.state, t.country) || ' [' || "
                 "coalesce(t.class,'') || '] ' || coalesce(t.title,'') || ' — ' || "
                 "coalesce(t.detail,'')"),
        "where": ("coalesce(t.title, t.detail, '') <> '' "
                  "AND t.row_status = 'published'"),
        "fresh_col": "updated_at"},
    "construction_permits": {                 # 708 rows — filed DC permits
        "id": "t.id::text", "kind": "permit",
        "text": ("coalesce(t.project_name, t.permit_number, '') || ' — ' || "
                 "coalesce(t.applicant,'') || ' · ' || concat_ws(', ', t.city, t.state, "
                 "t.county) || ' · ' || coalesce(t.permit_type,'') || ' ' || "
                 "coalesce(t.status,'') || ': ' || coalesce(t.description,'')"),
        "where": "coalesce(t.project_name, t.description, '') <> ''"},
    "tax_incentives_neon": {                  # 50 rows — state incentive programs
        "id": "t.id::text", "kind": "incentive",
        "text": ("coalesce(t.state_name, t.state_abbr, '') || ' data-center incentives: ' "
                 "|| coalesce(t.incentive_details,'') || ' Qualifying: ' || "
                 "coalesce(t.qualifying_investment,'') || ' ' || "
                 "coalesce(t.qualifying_jobs,'') || '. Max benefit: ' || "
                 "coalesce(t.max_benefit,'') || '. ' || coalesce(t.notes,'')"),
        "where": "coalesce(t.incentive_details,'') <> ''",
        # No fresh_col: _pending's staleness predicate is
        # `t.<fresh_col> > e.updated_at`, and created_at (set once at INSERT)
        # can never be later than an embedding written after it — declaring it
        # was a guaranteed no-op that merely LOOKED like freshness tracking.
        # The real mtime column here (last_updated) is TEXT, which the live
        # type gate rejects, so this corpus is honestly insert-only.
        },
    # ★ QUARANTINE GATE (2026-07-31) — the THIRD instance of the missed-consumer
    # class, after press_releases (07-25) and deals (07-27), and the same table
    # pattern as `deals` above. The 07-27 pipeline-GW audit stamped 725 rows via
    # repair_capacity_pipeline_quarantine.py and taught the PUBLISHED figures to
    # exclude them (`_CP_OK = "COALESCE(data_flag,'') = ''"`, main.py) — but this
    # registry never learned, so 718 of 1,966 embedded chunks (36.5%) pointed at
    # rows every published surface deliberately refuses to count: 385
    # quarantine_unparsed (operator blank/'Unknown' — extractor failures), 160
    # quarantine_aggregate (utility interconnection-request QUEUES summed as if
    # one building: AEP 63,000 MW, Dominion 48,000, PPL 25,200, plus impossible
    # singles like Google Nevada 150,000 MW), 102 quarantine_duplicate and 71
    # quarantine_not_pipeline. `capacity_pipeline` is in PUBLIC_CORPORA, so those
    # were retrievable with a CC-BY citation stamp on the UNAUTHENTICATED
    # /api/v1/rag/search — verified live: keyless GET ?corpus=capacity_pipeline
    # returned source_ids 11458/11269 (Nvidia Sweetwater), 10690 (AEP) and 12578
    # (PPL-Blackstone), all quarantine_aggregate. Retrieval reads
    # brain_corpus_embeddings directly and never re-applies this `where`, so the
    # registry IS the gate.
    # ★ COALESCE(...) = '' — the strict form, matching _CP_OK exactly rather than
    # `deals`' LEFT(data_flag,11) <> 'quarantine_'. `deals` needs the prefix test
    # because it carries a legitimate non-quarantine flag (cumulative_capex);
    # capacity_pipeline does not, and repair_capacity_pipeline_quarantine.py
    # prescribes this exact predicate for its consumers. Matching the published-
    # figure guard byte-for-byte is what stops the two from drifting again, and
    # it fails CLOSED: a future flag vocabulary drops out of the public corpus
    # instead of leaking through a prefix that no longer matches.
    # ★ No literal % (the LIKE 'quarantine_%' form would make psycopg2
    # %-substitute and 500 _corpus_total/_count_orphans/_sweep_orphans, which
    # interpolate this string into queries that DO pass a params tuple).
    # ★ Adding the filter is sufficient to REMOVE the 718: _sweep_orphans deletes
    # embeddings whose source row no longer satisfies the registry `where`,
    # capped at 1,000/corpus/run — so one reindex tick drains this.
    "capacity_pipeline": {                    # 1.9k rows — build pipeline
        "id": "t.id::text", "kind": "pipeline",
        "text": ("coalesce(t.operator,'') || ' — ' || concat_ws(', ', t.market, "
                 "t.region, t.country) || ' · ' || coalesce(t.phase,'') || ' ' || "
                 "coalesce(t.status,'') || ' (announced ' || "
                 "coalesce(t.announcement_date,'') || ', completion ' || "
                 "coalesce(t.completion_date,'') || ') ' || coalesce(t.notes,'')"),
        # ★lane 5: cp_ok is the SoT for this predicate; util/capacity_pipeline
        # says in its own docstring that its consumers must not drift, and an
        # inlined copy here is exactly that drift.
        "where": ("coalesce(t.operator, t.market, '') <> '' "
                  "AND " + _cp_ok("t"))},
    "brain_briefs": {                         # 254 rows — brain-internal briefs
        "id": "t.id::text", "kind": "brief",
        "text": "coalesce(t.summary, left(t.brief_md, 1200), '')",
        "where": "coalesce(t.summary, t.brief_md, '') <> '' AND coalesce(t.error,'') = ''",
        "fresh_col": "generated_at"},
    # ── self-learning "lessons" (r-rag-lessons 2026-07-04) ───────────────
    # The brain already CAPTURES outcomes (autopilot_outcomes,
    # brain_finding_outcomes) but never RECALLED them, so it re-proposed ideas
    # that already failed. Embedding these as a 'lesson' corpus lets any layer
    # retrieve_lessons(query) recall what worked/failed before it acts. Brain-
    # internal → deliberately NOT in PUBLIC_CORPORA. fresh_col=verified_at so a
    # re-verified outcome re-embeds. Only settled rows (verdict present) embed.
    "autopilot_outcomes": {
        "id": "t.id::text", "kind": "lesson",
        "text": ("'Action ' || coalesce(t.pattern_name,'') || ': ' || "
                 "case when t.succeeded is true then 'WORKED' "
                 "when t.succeeded is false then 'FAILED' else 'unverified' end || "
                 "' — ' || coalesce(t.evidence,'') || ' ' || coalesce(t.failure_reason,'')"),
        "where": ("t.succeeded IS NOT NULL AND "
                  "(coalesce(t.evidence,'') <> '' OR coalesce(t.failure_reason,'') <> '')"),
        "fresh_col": "verified_at"},
    "brain_finding_outcomes": {
        "id": "t.id::text", "kind": "lesson",
        "text": ("'Issue ' || coalesce(t.issue_type,'') || ': ' || coalesce(t.fix_kind,'') || "
                 "' fix ' || coalesce(t.fix_summary,'') || ' → ' || coalesce(t.outcome,'') || "
                 "'. ' || coalesce(t.outcome_detail,'')"),
        "where": "t.outcome <> 'pending' AND coalesce(t.issue_type,'') <> ''",
        "fresh_col": "verified_at"},
    # qa-0704 lane-driver: the brain's own lane decisions + measured outcomes
    # (routes/brain_lane_driver.py ledger). verified_at is TIMESTAMPTZ in the
    # driver's DDL; until the first tick creates the table, _pending's
    # try/except skips this corpus harmlessly.
    "brain_lane_decisions": {
        "id": "t.id::text", "kind": "lane_lesson",
        "text": "'[' || coalesce(t.lane,'') || '] ' || coalesce(t.diagnosis,'') || ' -> action ' || coalesce(t.action,'') || ' (expected: ' || coalesce(t.expected_effect,'') || ') outcome: ' || coalesce(t.outcome,'pending') || ' - ' || coalesce(t.outcome_note,'')",
        "where": "coalesce(t.diagnosis,'') <> ''",
        "fresh_col": "verified_at"},
    # ── learn station: NEGATIVE results (agentic-loop #65 part C, 2026-08-22) ──
    # The claim loop's part 6: a refuted or retracted CLAIM, and a proposal the
    # triage rejected as a duplicate, are the results the planner and the lane
    # driver must RECALL before they act — otherwise a refuted number gets
    # re-stated and a rejected idea re-proposed (41 terminal findings were
    # being re-read every 6h with no memory of the verdict).
    # ★ VIEW-LIKE corpora: the registry key is the corpus NAME (what
    #   brain_corpus_embeddings.source_table carries) and `table` names the
    #   relation it reads — the CHUNKED_CORPORA convention; _src_table()
    #   resolves it at every FROM site. Keying on the name keeps the two
    #   negative corpora distinct from any future positive corpus over the
    #   same tables.
    # ★ GATE = the OUTCOME. `t.outcome IN ('refuted','retracted')` IS the
    #   corpus: drop it and every open/confirmed claim becomes a "lesson".
    #   `source_layer = 'CLAIM'` keeps L16's own prediction rows (947 live,
    #   outcome NULL) out by construction. Brain-internal → in LESSON_CORPORA,
    #   NEVER in PUBLIC_CORPORA (the press_releases/capacity_pipeline leak
    #   class: the keyless /api/v1/rag/search serves left(text,500) with a
    #   CC-BY stamp).
    # ★ fresh_col=outcome_at is a REAL mtime (TIMESTAMPTZ on live, stamped by
    #   _stamp_outcome_sql / _retract_sql): a retraction of an already-refuted
    #   claim re-stamps it → re-embed. No literal percent-sign anywhere in
    #   these specs — `where` is f-string-interpolated into queries that pass a
    #   params tuple (_count_orphans, _sweep_orphans).
    "claim_lessons": {
        "table": "brain_predictions_log",
        "id": "t.id::text", "kind": "claim_lesson",
        "text": ("upper(coalesce(t.outcome,'')) || ': ' || coalesce(t.statement,'') || "
                 "' | expected ' || coalesce(t.expected_metric,'') || ' ' || "
                 "coalesce(t.expected_value,'') || ' | actual ' || "
                 "left(coalesce(t.outcome_evidence,''), 600) || "
                 "' | regime ' || coalesce(t.regime->>'as_of','')"),
        "where": ("t.source_layer = 'CLAIM' "
                  "AND t.outcome IN ('refuted','retracted') "
                  "AND coalesce(t.statement,'') <> ''"),
        "fresh_col": "outcome_at"},
    # Proposals the autonomy shell's triage marked `duplicate` (exact
    # fingerprint re-files) or a human marked `rejected`. The row enters the
    # corpus when its status flips (it never satisfied the `where` before) and
    # leaves through _sweep_orphans if it flips back. NO fresh_col on purpose:
    # brain_enhancement_proposals carries only created_at (11 columns on live,
    # 2026-08-22) and a creation timestamp is the guaranteed no-op fresh_col —
    # insert-only is the honest setting, and the status-flip time is simply
    # not recorded anywhere (learn_station_status says so).
    "proposal_lessons": {
        "table": "brain_enhancement_proposals",
        "id": "t.id::text", "kind": "proposal_lesson",
        "text": ("'REJECTED PROPOSAL (' || coalesce(t.status,'') || '): ' || "
                 "coalesce(t.title,'') || ' [' || coalesce(t.area,'') || '] - ' || "
                 "left(coalesce(t.proposal_json->>'recommendation',''), 500) || "
                 "' | signal ' || left(coalesce(t.proposal_json->>'signal',''), 300)"),
        "where": ("t.status IN ('duplicate','rejected') "
                  "AND coalesce(t.title,'') <> ''")},
}

# The two NEGATIVE-result corpora above. Always a subset of LESSON_CORPORA,
# never of PUBLIC_CORPORA (tests/test_learn_station_shell65c.py pins both).
NEGATIVE_LESSON_CORPORA = ("claim_lessons", "proposal_lessons")

# Brain-internal corpora that carry PAST-OUTCOME lessons (recalled by
# retrieve_lessons; never exposed to public_search). brain_finding_outcomes
# and autopilot_outcomes already embed their FAILED rows (outcome <> 'pending'
# / succeeded IS NOT NULL), so "failed fixes" need no third negative corpus —
# recall_negative_lessons() picks them out of these by their text markers.
LESSON_CORPORA = ("autopilot_outcomes", "brain_finding_outcomes",
                  "brain_lane_decisions") + NEGATIVE_LESSON_CORPORA


def _src_table(src: str, spec: dict) -> str:
    """The relation a flat corpus reads. Most corpus names ARE their table;
    a view-like corpus (claim_lessons over brain_predictions_log) names its
    table in spec['table']. Every FROM site in this module goes through here
    so a name-keyed corpus can never be queried as a non-existent table."""
    return (spec or {}).get("table") or src
# Optional per-corpus "fresh_col" (a t.<timestamp> column): when set AND
# live-verified by _fresh_col_active (exists + timestamp type on the LIVE
# schema), _pending ALSO re-picks rows whose source timestamp is newer than
# the stored embedding (updated-in-place rows re-embed). Corpora without it —
# or whose declared column fails the live check — keep insert-only behavior.
# Activation is visible per-corpus in /api/v1/admin/brain/rag/status
# ("fresh_cols").

# ── chunked corpora (RAG v1, 2026-07-03) ─────────────────────────────
# One DOC → many chunk rows (source_id='<slug>#<n>'). Can't ride the flat
# row-SQL registry above, so these get their own pending/reindex path.
# market_deep_dives regenerates IN PLACE under the same PK (market_slug), so
# staleness = doc.generated_at > max(chunk.updated_at) → re-chunk + re-embed.
CHUNKED_CORPORA = {
    "market_narratives": {
        "table": "market_deep_dives",
        "kind": "market_narrative",
    },
}
_CHUNK_MIN_CHARS = 600    # ~150 tokens (chars/4)
_CHUNK_MAX_CHARS = 1200   # ~300 tokens

# ── contextual retrieval (r-rag-contextual 2026-07-04) ───────────────
# Anthropic's contextual-retrieval recipe: before embedding, a small LLM
# writes a 2-3 sentence blurb situating each chunk within its FULL document;
# the blurb is prepended to the embedded text ("Context: <blurb>\n" + chunk),
# which lifts retrieval accuracy on chunks whose prose doesn't name their
# subject. The full doc rides in a SYSTEM block with cache_control ephemeral
# so all chunks of one doc share a byte-identical prefix. HONESTY NOTE
# (review 2026-07-04): the cache is INERT for this corpus today — deep-dive
# narratives are written with max_tokens=1000 (~<=1.1K-token prefix), below
# Haiku 4.5's 4,096-token minimum cacheable prefix, so per-chunk calls pay
# full input price. Cost stays bounded by the call budget (~$0.35/run worst
# case); if narratives ever grow past the minimum, caching engages
# automatically with no code change.
# BLURB CAP: 200 chars — retrieve_context serves left(text,500) and rerank
# scores that same preview, so a longer blurb would crowd the provenance
# header + chunk FACTS out of what consumers actually see.
# FAIL-SOFT per chunk and per doc: any API failure keeps the existing static
# provenance header, and a total failure never stops the reindex. A circuit
# breaker aborts after 3 CONSECUTIVE call failures (e.g. blackholed network:
# 30s/call x 400-call budget would otherwise hold a gunicorn thread for
# hours — the 07-03 thread-starvation class).
# Toggle: BRAIN_RAG_CONTEXTUAL=0 (default ON, mirrors _rerank_on).
# Budget: BRAIN_RAG_CTX_MAX_CALLS LLM calls per reindex run (default 400) so
# a runaway backfill cannot burn tokens.
CTX_MODEL_DEFAULT = "claude-haiku-4-5-20251001"  # same haiku id the deep-dive writer uses
_CTX_MAX_TOKENS = 80
_CTX_BLURB_MAX_CHARS = 200
_CTX_BREAKER_FAILS = 3   # consecutive call failures -> abort this batch
_CTX_SYSTEM_INSTRUCTION = (
    "You situate a chunk within a document for search retrieval. "
    "Answer with 1-2 short sentences (under 200 characters) of succinct "
    "context and nothing else.")


def _contextual_on() -> bool:
    return (os.environ.get("BRAIN_RAG_CONTEXTUAL", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _ctx_model() -> str:
    return (os.environ.get("BRAIN_RAG_CTX_MODEL") or "").strip() or CTX_MODEL_DEFAULT


def _ctx_max_calls() -> int:
    try:
        return max(0, int(os.environ.get("BRAIN_RAG_CTX_MAX_CALLS", "400")))
    except Exception:
        return 400


def _contextualize_chunks(doc_title, doc_md, chunks, max_calls=None):
    """One Anthropic Messages call per chunk (urllib, same pattern as
    _embed/_rerank) → 2-3 sentence situating blurb. System = [instruction,
    full doc w/ cache_control ephemeral] so per-doc calls share a cached
    prefix. Returns a list parallel to `chunks` — None where the call was
    skipped (no key / over budget) or failed. NEVER raises."""
    out = [None] * len(chunks)
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key or not chunks:
        return out
    n = len(chunks) if max_calls is None else max(0, min(len(chunks), int(max_calls)))
    system = [
        {"type": "text", "text": _CTX_SYSTEM_INSTRUCTION},
        {"type": "text",
         "text": f'<document title="{doc_title}">\n{doc_md}\n</document>',
         "cache_control": {"type": "ephemeral"}},
    ]
    consec_fail = 0
    for i in range(n):
        body = json.dumps({
            "model": _ctx_model(),
            "max_tokens": _CTX_MAX_TOKENS,
            "system": system,
            "messages": [{
                "role": "user",
                "content": ("Situate this chunk within the document for retrieval. "
                            "Chunk:\n" + chunks[i]),
            }],
        }).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                     data=body, method="POST")
        req.add_header("x-api-key", key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            txt = " ".join(
                b.get("text", "") for b in (d.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text").strip()
            if txt:
                out[i] = txt[:_CTX_BLURB_MAX_CHARS]
            consec_fail = 0
        except Exception:
            # fail-soft: this chunk keeps its static-header text. Circuit
            # breaker: 3 consecutive failures (worst case = hung network at
            # 30s each) -> abort the batch rather than hold a request thread
            # for the rest of the budget.
            consec_fail += 1
            if consec_fail >= _CTX_BREAKER_FAILS:
                break
            continue
    return out

# Corpora an unauthenticated agent may semantically search (brain internals excluded).
# wave-3 expansion (2026-07-25): press/announcement/permitting/permit/
# incentive/pipeline join the public search surface. brain_briefs stays
# INTERNAL (operator-facing prose, not agent product).
PUBLIC_CORPORA = ("news_articles", "deals", "discovered_facilities", "market_narratives",
                  "press_releases", "announcements", "permitting_intel",
                  "construction_permits", "tax_incentives_neon", "capacity_pipeline")


# ── auth ──────────────────────────────────────────────────────────────
def _admin_key():
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    exp = (_admin_key() or "").strip()
    if not exp:
        return False
    got = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, exp)


def _disabled() -> bool:
    return str(os.environ.get("BRAIN_RAG_DISABLED", "")).lower() in ("1", "true", "yes")


# ── DB ────────────────────────────────────────────────────────────────
def _db():
    import psycopg2
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not du:
        return None
    return psycopg2.connect(du, connect_timeout=8)


def _ensure() -> bool:
    """Lazy: pgvector extension + table. NEVER at boot (DDL-storm trap)."""
    c = _db()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS brain_corpus_embeddings (
                    id           SERIAL PRIMARY KEY,
                    source_table TEXT NOT NULL,
                    source_id    TEXT NOT NULL,
                    kind         TEXT,
                    text         TEXT,
                    embedding    vector({EMBED_DIM}),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    meta         JSONB,
                    chunk_ix     INT,
                    UNIQUE (source_table, source_id)
                )
            """)
            # RAG v1 (2026-07-03): chunk provenance columns on the LIVE table.
            # Raw psycopg2 (not safe_db, which silently skips DDL) + IF NOT
            # EXISTS, and _ensure only runs lazily at reindex — no boot storm.
            cur.execute("ALTER TABLE brain_corpus_embeddings ADD COLUMN IF NOT EXISTS meta JSONB")
            cur.execute("ALTER TABLE brain_corpus_embeddings ADD COLUMN IF NOT EXISTS chunk_ix INT")
            # ANN index so recall (<=> cosine) uses HNSW, not a full seq scan.
            # IF NOT EXISTS => builds once; instant on a fresh/empty branch table,
            # no-op once present. Plain (non-CONCURRENT) is correct here: this runs
            # on the pooler conn inside _ensure()'s txn, and CONCURRENTLY can't. For
            # a one-shot rebuild on a large live table use
            # tools/add_brain_rag_hnsw_index.py (direct endpoint, CONCURRENTLY).
            cur.execute("""
                CREATE INDEX IF NOT EXISTS brain_corpus_embeddings_hnsw
                ON brain_corpus_embeddings USING hnsw (embedding vector_cosine_ops)
            """)
        c.commit()
        return True
    except Exception:
        try: c.rollback()
        except Exception: pass
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── Cohere embeddings ─────────────────────────────────────────────────
def _embed_provider() -> str:
    """r-rag-mistral (2026-07-06): which embedding provider to use. Cohere
    embed-english-v3.0 (1024-d) was original, but the live COHERE_API_KEY is a
    TRIAL key (1,000/mo) that exhausts → 429 → whole RAG froze. mistral-embed
    is ALSO 1024-d (drop-in, no schema change), so default to it. Set
    RAG_EMBED_PROVIDER=cohere to revert once a Cohere PRODUCTION key is in
    place (then re-embed — cross-provider vectors are not comparable)."""
    return (os.environ.get("RAG_EMBED_PROVIDER") or "mistral").strip().lower()


def _live_embed_model() -> str:
    """The embed model actually in use. brain-ascension #28 (2026-07-25):
    /status and /reindex reported the hardcoded Cohere EMBED_MODEL even while
    mistral vectors were being written — the operator-facing surface lied
    about the live provider."""
    return EMBED_MODEL if _embed_provider() == "cohere" else "mistral-embed"


# ── provider-aware cosine gates (brain-ascension #28 wave 2, 2026-07-25) ──
# Every cosine threshold in the codebase was tuned on Cohere embed-v3's
# asymmetric scale; mistral-embed (symmetric) compresses the range upward.
# Measured LIVE on prod 2026-07-25 via /api/v1/admin/brain/rag/retrieve:
#   nonsense-query top-1        0.65–0.675   (three word-salad probes)
#   weakest on-topic top-1      0.744        (nuclear/hyperscaler query)
#   strong on-topic top-1       0.84–0.85
#   same-story DIFFERENT doc    0.85–0.86
#   near-dup paraphrase         0.925–0.93   (two rephrasings of a live doc)
#   exact duplicate             1.0
# Verdicts: the dup gates (0.90 loose / 0.92 strict) sit correctly in the
# 0.86–0.925 separation gap and are VALIDATED as-is under mistral. The
# related-intel floor 0.30 filtered NOTHING (nonsense scores 0.65+) and the
# eval floors 0.42–0.50 were trivially passable — both re-registered here.
# Gate sites read these defaults (env overrides still win at each site).
PROVIDER_COSINE_GATES = {
    "mistral": {"dup_loose": 0.90, "dup_strict": 0.92,
                "related_min": 0.72, "eval_floor": 0.70},
    # legacy Cohere scale, kept for a future RAG_EMBED_PROVIDER=cohere revert
    "cohere":  {"dup_loose": 0.90, "dup_strict": 0.92,
                "related_min": 0.30, "eval_floor": 0.45},
}


def cosine_gate(name: str) -> float:
    """Provider-appropriate default for a named cosine gate. Unknown names
    fall back to the strictest registered value so a typo can never open a
    gate wider than intended."""
    g = PROVIDER_COSINE_GATES.get(_embed_provider(),
                                  PROVIDER_COSINE_GATES["mistral"])
    if name in g:
        return float(g[name])
    return max(float(v) for v in g.values())


# LC6 Lane B — embed health, counted per reindex run.
#
# "Did the cron run" is not the failure mode here. Both providers below swallow
# EVERY exception into a bare `return None`, and reindex() answers that with
# `continue` — so a total embedding outage finishes cleanly, returns ok=True with
# embedded=0, and leaves a corpus that retrieves nothing at cosine 0.0. Nothing on
# the dead-man board sees it.
#
# Counted at the DISPATCHER so it stays provider-agnostic: RAG_EMBED_PROVIDER
# defaults to mistral, and a revert to cohere must keep measuring.
_EMBED_HEALTH = {"calls": 0, "failed": 0, "http_429": 0, "zero_norm": 0, "vectors": 0}


def _beat_feed(*a, **kw):
    """Lazy + fail-open: a heartbeat must never be able to break the reindex.

    This module has no logger, so the fallback prints — beat_feed() itself logs
    the HTTP-failure case at ERROR through the ingest_runs logger.
    """
    try:
        from routes.ingest_runs import beat_feed
        beat_feed(*a, **kw)
    except Exception as e:  # noqa: BLE001
        print(f"[rag] deadman beat failed (non-fatal): {type(e).__name__}: {e}")


def _embed_health_reset():
    for k in _EMBED_HEALTH:
        _EMBED_HEALTH[k] = 0


def _note_embed_error(exc):
    """Record a provider error. 429 is broken out because throttling is the
    documented way this corpus died before (the Cohere trial key, 2026-07-06) —
    it is a quota problem, not a network blip, and wants a different response."""
    try:
        if getattr(exc, "code", None) == 429:
            _EMBED_HEALTH["http_429"] += 1
    except Exception:
        pass


def _embed_cohere(texts, input_type="search_document"):
    key = (os.environ.get("COHERE_API_KEY") or "").strip()
    if not key or not texts:
        return None
    body = json.dumps({"texts": texts, "model": EMBED_MODEL,
                       "input_type": input_type, "truncate": "END"}).encode()
    req = urllib.request.Request("https://api.cohere.ai/v1/embed", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return d.get("embeddings")
    except Exception as e:
        _note_embed_error(e)
        return None


def _embed_mistral(texts, input_type=None):
    # mistral-embed: symmetric (no doc/query split) + 1024-d. input_type is
    # accepted+ignored for a drop-in signature. Sub-batch to stay under the
    # provider's per-request cap regardless of caller batch size.
    key = (os.environ.get("MISTRAL_API_KEY") or "").strip()
    if not key or not texts:
        return None
    texts = list(texts)
    out = []
    for i in range(0, len(texts), 64):
        sub = texts[i:i + 64]
        body = json.dumps({"model": "mistral-embed", "input": sub}).encode()
        req = urllib.request.Request("https://api.mistral.ai/v1/embeddings",
                                     data=body, method="POST")
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.loads(r.read())
            embs = [it.get("embedding") for it in (d.get("data") or [])]
        except Exception as e:
            _note_embed_error(e)
            return None
        if len(embs) != len(sub):
            return None
        out.extend(embs)
    return out or None


def _embed(texts, input_type="search_document"):
    _EMBED_HEALTH["calls"] += 1
    vecs = (_embed_cohere(texts, input_type) if _embed_provider() == "cohere"
            else _embed_mistral(texts, input_type))
    if not vecs:
        _EMBED_HEALTH["failed"] += 1
        return vecs
    # A zero-norm vector is the silent killer: it does not raise, it returns
    # cosine 0.0 against everything, and the corpus still looks populated.
    for v in vecs:
        _EMBED_HEALTH["vectors"] += 1
        try:
            if all(abs(float(x)) < 1e-9 for x in v):
                _EMBED_HEALTH["zero_norm"] += 1
        except Exception:
            pass
    return vecs


def _vec(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def _rerank(query, docs, top_n):
    """Cohere cross-encoder rerank. `docs` = list of text strings. Returns a
    list of (original_index, relevance_score) for the top_n, best-first — or
    None on any failure (caller falls back to cosine order)."""
    key = (os.environ.get("COHERE_API_KEY") or "").strip()
    if not key or not docs:
        return None
    body = json.dumps({"model": RERANK_MODEL, "query": query,
                       "documents": docs, "top_n": int(top_n)}).encode()
    req = urllib.request.Request("https://api.cohere.ai/v1/rerank", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        out = []
        for item in (d.get("results") or []):
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(docs):
                out.append((idx, round(float(item.get("relevance_score", 0.0)), 4)))
        return out or None
    except Exception:
        return None


# ── corpus selection ──────────────────────────────────────────────────
# fresh_col live-schema gate (r-rag-freshcol 2026-07-04). The repo DDL lies
# (brain_findings/power_plants drift class), so a registry fresh_col only
# activates after the LIVE information_schema confirms the column exists AND
# is a timestamp/date type. Without this gate a wrong fresh_col makes the
# per-corpus SELECT raise inside _pending's try/except → that whole corpus
# silently stops indexing on EVERY run. Fail-closed → insert-only behavior
# (never worse than pre-fresh_col). Positive/negative introspection results
# are process-cached; introspection ERRORS are not cached (transient DB blips
# must not disable freshness until restart).
_FRESH_COL_CACHE = {}   # (table, col) -> bool


def _fresh_col_active(cur, table: str, col: str) -> bool:
    hit = _FRESH_COL_CACHE.get((table, col))
    if hit is not None:
        return hit
    try:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
            (table, col))
        row = cur.fetchone()
        ok = bool(row) and (str(row[0]).startswith("timestamp")
                            or str(row[0]) == "date")
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return False  # uncached: retry next run
    _FRESH_COL_CACHE[(table, col)] = ok
    return ok


def _pending(cur, cap):
    """Rows across ALL registered corpora that don't yet have an embedding —
    plus, for corpora with a `fresh_col` (live-verified via
    _fresh_col_active), rows updated IN PLACE since they were embedded (the
    insert-only staleness gap: a row regenerated under the same PK never
    re-indexed). NEW rows are picked before stale re-embeds (ORDER BY) so a
    corpus whose fresh_col re-stamps often — brain_findings re-fires dozens
    of findings per scan — can't starve its own new content out of the
    per-corpus budget. Allocates the cap roughly EVENLY across corpora so one
    large corpus (facilities) doesn't starve the others until it's done. A
    corpus whose columns don't resolve is skipped (rollback), never fatal."""
    rows = []
    per = max(1, cap // max(1, len(CORPORA) + len(CHUNKED_CORPORA)))
    for src, spec in CORPORA.items():
        if len(rows) >= cap:
            break
        lim = min(per, cap - len(rows))
        fresh = spec.get("fresh_col")
        tbl = _src_table(src, spec)
        pick = "e.id IS NULL"
        order = ""
        if fresh and _fresh_col_active(cur, tbl, fresh):
            pick = f"(e.id IS NULL OR t.{fresh} > e.updated_at)"
            order = "ORDER BY (e.id IS NULL) DESC "
        q = (f"SELECT '{src}', ({spec['id']}) AS sid, '{spec['kind']}', "
             f"left({spec['text']}, 1600) "
             f"FROM {tbl} t "
             f"LEFT JOIN brain_corpus_embeddings e "
             f"  ON e.source_table='{src}' AND e.source_id=({spec['id']}) "
             f"WHERE {pick} AND ({spec['where']}) "
             f"{order}"
             f"LIMIT {int(lim)}")
        try:
            cur.execute(q)
            rows += cur.fetchall()
        except Exception:
            try: cur.connection.rollback()
            except Exception: pass
    return rows


# ── chunked-corpus selection (market_narratives) ──────────────────────
def _split_paragraphs(md: str) -> list:
    import re as _re
    return [p.strip() for p in _re.split(r"\n\s*\n", md or "") if p.strip()]


def _chunk_narrative(market_name: str, generated_at, narrative_md: str) -> list:
    """Split a deep-dive narrative on blank lines, greedy-merge paragraphs
    into ~150-300-token chunks, prepend a provenance header so a chunk is
    self-describing when retrieved on its own."""
    date_s = ""
    try:
        date_s = generated_at.date().isoformat() if hasattr(generated_at, "date") else str(generated_at)[:10]
    except Exception:
        pass
    header = f"{market_name} — DCPI deep-dive ({date_s}): "
    chunks, buf = [], ""
    for para in _split_paragraphs(narrative_md):
        if buf and len(buf) + len(para) + 2 > _CHUNK_MAX_CHARS:
            chunks.append(buf)
            buf = para
        else:
            buf = (buf + "\n\n" + para) if buf else para
        # a single paragraph can overshoot the max — emit it whole rather
        # than splitting mid-sentence (Cohere truncate=END bounds the tail)
        if len(buf) >= _CHUNK_MAX_CHARS:
            chunks.append(buf)
            buf = ""
    if buf:
        # greedy-merge a tiny tail into the previous chunk
        if chunks and len(buf) < _CHUNK_MIN_CHARS and len(chunks[-1]) + len(buf) + 2 <= _CHUNK_MAX_CHARS + _CHUNK_MIN_CHARS:
            chunks[-1] = chunks[-1] + "\n\n" + buf
        else:
            chunks.append(buf)
    return [header + c for c in chunks]


def _pending_chunk_docs(cur, cap):
    """market_deep_dives docs that are missing from the embeddings store OR
    regenerated since their chunks were embedded (generated_at > the newest
    chunk's updated_at — the fresh_col staleness predicate for the chunked
    corpus). Returns up to cap (slug, name, narrative_md, generated_at)."""
    try:
        cur.execute("""
            SELECT d.market_slug, d.market_name, d.narrative_md, d.generated_at
              FROM market_deep_dives d
              LEFT JOIN (
                    SELECT split_part(source_id, '#', 1) AS slug,
                           max(updated_at) AS emb_at
                      FROM brain_corpus_embeddings
                     WHERE source_table = 'market_narratives'
                     GROUP BY 1
              ) e ON e.slug = d.market_slug
             WHERE coalesce(d.narrative_md, '') <> ''
               AND (e.emb_at IS NULL OR d.generated_at > e.emb_at)
             ORDER BY d.generated_at DESC
             LIMIT %s
        """, (int(cap),))
        return cur.fetchall()
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return []


def _pending_chunk_count(cur) -> int:
    try:
        cur.execute("""
            SELECT count(*)
              FROM market_deep_dives d
              LEFT JOIN (
                    SELECT split_part(source_id, '#', 1) AS slug,
                           max(updated_at) AS emb_at
                      FROM brain_corpus_embeddings
                     WHERE source_table = 'market_narratives'
                     GROUP BY 1
              ) e ON e.slug = d.market_slug
             WHERE coalesce(d.narrative_md, '') <> ''
               AND (e.emb_at IS NULL OR d.generated_at > e.emb_at)
        """)
        return cur.fetchone()[0] or 0
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return 0


def _reindex_chunk_docs(c, doc_cap: int) -> int:
    """Chunk + embed pending market_deep_dives docs. Per doc: DELETE the old
    chunk rows (chunk counts shrink/grow across regenerations, so upsert alone
    would strand tail chunks), then INSERT slug#0..slug#n with chunk_ix + meta
    (citable market name / deep-dive URL / generated_at). Returns chunk rows
    written. Commits per doc so a mid-run failure keeps completed docs."""
    if doc_cap <= 0:
        return 0
    written = 0
    ctx_left = _ctx_max_calls() if _contextual_on() else 0
    with c.cursor() as cur:
        docs = _pending_chunk_docs(cur, doc_cap)
    # Cohere preflight (review 2026-07-04): blurb tokens are spent BEFORE the
    # embed that can still fail — if Cohere is down, every doc stays pending
    # and the NEXT run re-pays the full contextualization. One cheap embed
    # probe gates the whole run's Anthropic spend.
    if ctx_left > 0 and docs:
        if not _embed(["cohere preflight"], input_type="search_document"):
            ctx_left = 0
    for slug, name, md, gen_at in docs:
        chunks = _chunk_narrative(name or slug, gen_at, md)
        if not chunks:
            continue
        # Contextual retrieval (see _contextualize_chunks): prepend an
        # LLM-written situating blurb to each chunk BEFORE embedding. The
        # static provenance header stays as the base — a failed/skipped
        # chunk embeds exactly as before. Budgeted per reindex run.
        if ctx_left > 0:
            attempted = min(len(chunks), ctx_left)
            try:
                blurbs = _contextualize_chunks(name or slug, md, chunks,
                                               max_calls=ctx_left)
            except Exception:
                blurbs = None  # belt-and-suspenders: helper never raises
            ctx_left -= attempted
            if blurbs:
                chunks = [f"Context: {b}\n{t}" if b else t
                          for t, b in zip(chunks, blurbs)]
            # Run-level breaker: a doc that attempted several calls and got
            # ZERO blurbs signals systemic API failure (auth/outage) — stop
            # contextualizing for the rest of the run; docs still embed with
            # their static headers.
            if (blurbs is None) or (attempted >= _CTX_BREAKER_FAILS
                                    and not any(blurbs)):
                ctx_left = 0
        vecs = []
        for i in range(0, len(chunks), _COHERE_BATCH):
            v = _embed(chunks[i:i + _COHERE_BATCH], input_type="search_document")
            if not v:
                vecs = None
                break
            vecs += v
        if not vecs or len(vecs) != len(chunks):
            continue
        meta = json.dumps({
            "market_slug": slug,
            "market_name": name,
            "generated_at": (gen_at.isoformat() if hasattr(gen_at, "isoformat") else str(gen_at)),
            "url": build_public_url("markets", slug, subpath="deep-dive"),
        })
        try:
            with c.cursor() as cur:
                cur.execute(
                    "DELETE FROM brain_corpus_embeddings "
                    "WHERE source_table='market_narratives' AND source_id LIKE %s",
                    (f"{slug}#%",))
                for ix, (text, vec) in enumerate(zip(chunks, vecs)):
                    cur.execute("""
                        INSERT INTO brain_corpus_embeddings
                          (source_table, source_id, kind, text, embedding, chunk_ix, meta)
                        VALUES ('market_narratives', %s, 'market_narrative',
                                %s, %s::vector, %s, %s::jsonb)
                        ON CONFLICT (source_table, source_id) DO UPDATE
                          SET embedding=EXCLUDED.embedding, text=EXCLUDED.text,
                              chunk_ix=EXCLUDED.chunk_ix, meta=EXCLUDED.meta,
                              updated_at=NOW()
                    """, (f"{slug}#{ix}", text[:1600], _vec(vec), ix, meta))
            c.commit()
            written += len(chunks)
        except Exception:
            try: c.rollback()
            except Exception: pass
    return written


def _corpus_total(cur):
    total = 0
    for src, spec in CORPORA.items():
        try:
            cur.execute(f"SELECT count(*) FROM {_src_table(src, spec)} t WHERE ({spec['where']})")
            total += cur.fetchone()[0] or 0
        except Exception:
            try: cur.connection.rollback()
            except Exception: pass
    return total


# ── orphan GC (r-rag-orphan-gc 2026-07-18) ────────────────────────────
# Embeddings outlive their sources: pruned brain_findings / deleted
# news_articles rows leave brain_corpus_embeddings rows behind forever, so
# /status coverage_pct drifts past 100% (observed 102.6% live). Swept from
# reindex (cron_heartbeat keeps it running). A source row that still exists
# but no longer satisfies the registry `where` (e.g. a facility later marked
# duplicate, a finding whose issue was blanked) is equally dead: it left the
# coverage denominator AND shouldn't be retrievable — swept too.
_ORPHAN_SWEEP_CAP = 1000   # max rows deleted per corpus per run


def _count_orphans(cur) -> dict:
    """Read-only twin of _sweep_orphans for /status: {corpus: orphan_count}
    for corpora with any. Same fail-soft-per-corpus contract."""
    out = {}
    for src, spec in CORPORA.items():
        try:
            cur.execute(
                f"SELECT count(*) FROM brain_corpus_embeddings e"
                f" WHERE e.source_table = %s"
                f"   AND NOT EXISTS (SELECT 1 FROM {_src_table(src, spec)} t"
                f"                   WHERE ({spec['id']}) = e.source_id"
                f"                     AND ({spec['where']}))",
                (src,))
            n = cur.fetchone()[0] or 0
            if n:
                out[src] = n
        except Exception:
            try: cur.connection.rollback()
            except Exception: pass
    return out


def _sweep_orphans(c, per_corpus_cap=_ORPHAN_SWEEP_CAP) -> dict:
    """DELETE embedding rows whose source row is gone (or out of the registry
    `where`), per flat corpus, batch-capped per run. Fail-soft per corpus like
    _pending — a corpus whose table/columns don't resolve is rolled back and
    skipped, and NOTHING is deleted for it. Commits per corpus. Returns
    {corpus: deleted} for corpora that deleted anything. Never raises."""
    deleted = {}
    for src, spec in CORPORA.items():
        try:
            with c.cursor() as cur:
                cur.execute(
                    f"DELETE FROM brain_corpus_embeddings WHERE id IN ("
                    f"  SELECT e.id FROM brain_corpus_embeddings e"
                    f"  WHERE e.source_table = %s"
                    f"    AND NOT EXISTS (SELECT 1 FROM {_src_table(src, spec)} t"
                    f"                    WHERE ({spec['id']}) = e.source_id"
                    f"                      AND ({spec['where']}))"
                    f"  LIMIT %s)",
                    (src, int(per_corpus_cap)))
                n = cur.rowcount or 0
            c.commit()
            if n:
                deleted[src] = n
        except Exception:
            try: c.rollback()
            except Exception: pass
    return deleted


# ── retrieval (importable by any consumer) ────────────────────────────
def _keyword_fallback(query: str, k: int = 8, corpus=None) -> list:
    """Degraded-mode recall when the embedding provider is unavailable
    (r-rag-embed-fallback 2026-07-05: Cohere trial key 429 → _embed None).
    Postgres full-text (OR of significant terms) over the ALREADY-STORED chunk
    text so semantic_search / search_intelligence return relevant CITED rows
    instead of []. Marked _fallback='keyword', cosine=0.0 so cosine-tuned gates
    (>=0.82) treat it as 'not a match'. Fail-soft → []."""
    _STOP = {"the","a","an","of","for","to","in","on","and","or","with","what",
             "is","are","how","why","when","where","which","that","this","by",
             "from","as","at","into","out","over","under","about","across","your",
             "our","their","its","it","they","we","you","be","will"}
    seen, terms = set(), []
    for t in re.findall(r"[a-z0-9]{3,}", (query or "").lower()):
        if t not in _STOP and t not in seen:
            seen.add(t); terms.append(t)
    terms = terms[:12]
    if not terms:
        return []
    tsq = " | ".join(terms)
    c = _db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            if corpus:
                cs = list(corpus) if isinstance(corpus, (list, tuple)) else [corpus]
                cur.execute("""
                    SELECT source_table, source_id, kind, left(text, 500),
                           ts_rank(to_tsvector('english', text),
                                   to_tsquery('english', %s)) AS rank
                    FROM brain_corpus_embeddings
                    WHERE source_table = ANY(%s)
                      AND to_tsvector('english', text) @@ to_tsquery('english', %s)
                    ORDER BY rank DESC LIMIT %s
                """, (tsq, cs, tsq, int(k)))
            else:
                cur.execute("""
                    SELECT source_table, source_id, kind, left(text, 500),
                           ts_rank(to_tsvector('english', text),
                                   to_tsquery('english', %s)) AS rank
                    FROM brain_corpus_embeddings
                    WHERE to_tsvector('english', text) @@ to_tsquery('english', %s)
                    ORDER BY rank DESC LIMIT %s
                """, (tsq, tsq, int(k)))
            rows = cur.fetchall()
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass
    return [{"source_table": r[0], "source_id": r[1], "kind": r[2],
             "text": r[3], "score": round(float(r[4] or 0.0), 4),
             "cosine": 0.0, "_fallback": "keyword"} for r in rows]


def retrieve_context(query: str, k: int = 8, corpus: str = None) -> list:
    """Top-k most semantically-relevant rows, optionally scoped to one corpus
    (e.g. corpus='news_articles'). Two-stage: pgvector cosine over-fetch →
    Cohere rerank → top-k (rerank fail-soft → cosine order). Fail-soft → [].

    Returns [{source_table, source_id, kind, text, score, cosine}] where
      - "cosine" is ALWAYS the original bi-encoder similarity (1 - pgvector
        cosine distance), present on EVERY result whether rerank fired or not.
        Threshold on THIS for similarity/dedup gates (e.g. the feature
        proposer's >= 0.82 duplicate check) — it lives on the embed-v3 cosine
        scale the thresholds were tuned for.
      - "score" is the RANKING signal: the Cohere cross-encoder relevance when
        rerank fired, else identical to "cosine". Cross-encoder relevance runs
        on a DIFFERENT scale (typically ~0.05-0.3), so sorting/display by
        "score" is right but thresholding cosine-tuned gates on it silently
        disarms them (r-rag-cosine-passthrough 2026-07-04, live bug: the
        proposer's 0.82 gate never fired once rerank shipped)."""
    if not query:
        return []
    qv = _embed([query], input_type="search_query")
    if not qv:
        # r-rag-embed-fallback (2026-07-05): embedding provider down (e.g.
        # Cohere trial-key 429) → don't return [] (silent "0 results"); fall
        # back to keyword full-text over the stored corpus so search still answers.
        return _keyword_fallback(query, k, corpus)
    qs = _vec(qv[0])
    # Over-fetch candidates when a stage 2 will re-order them (Cohere
    # cross-encoder OR the provider-neutral lexical leg); otherwise exactly k.
    rerank = _rerank_on()
    neutral = (not rerank) and _neutral_rerank_on()
    fetch_k = (min(int(k) * _RERANK_OVERFETCH, _RERANK_MAX_FETCH)
               if (rerank or neutral) else int(k))
    c = _db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            if corpus:
                cs = list(corpus) if isinstance(corpus, (list, tuple)) else [corpus]
                # r-rag-scoped-postfilter (2026-08-26, live bug since ~08-20):
                # pgvector POST-filters an HNSW scan — the index yields
                # hnsw.ef_search (default 40) global nearest rows and the
                # `source_table = ANY(...)` predicate is applied AFTER. So a
                # scoped query whose corpus is absent from the global top-40
                # returns ZERO rows, and one that is thinly represented returns
                # a SILENTLY TRUNCATED set — neither raises, so the fail-soft
                # `except: return []` below never sees it. Measured on live:
                # discovered_facilities 0/32, announcements 18/32.
                # iterative_scan keeps scanning until LIMIT filtered rows are
                # found (pgvector >= 0.8). strict_order preserves exact distance
                # order, which the cosine-tuned gates downstream rely on.
                # Fail-soft: an older pgvector rejects the GUC and aborts the
                # txn, so roll back and run the un-tuned query rather than
                # returning [] — degraded recall beats none.
                try:
                    cur.execute("SET LOCAL hnsw.iterative_scan = strict_order")
                except Exception:
                    c.rollback()
                cur.execute("""
                    SELECT source_table, source_id, kind, left(text, 500),
                           1 - (embedding <=> %s::vector)
                    FROM brain_corpus_embeddings WHERE source_table = ANY(%s)
                    ORDER BY embedding <=> %s::vector LIMIT %s
                """, (qs, cs, qs, int(fetch_k)))
            else:
                cur.execute("""
                    SELECT source_table, source_id, kind, left(text, 500),
                           1 - (embedding <=> %s::vector)
                    FROM brain_corpus_embeddings
                    ORDER BY embedding <=> %s::vector LIMIT %s
                """, (qs, qs, int(fetch_k)))
            # "cosine" rides on every row and NEVER gets overwritten; "score"
            # starts as cosine and becomes rerank relevance if stage 2 fires.
            base = [{"source_table": r[0], "source_id": r[1], "kind": r[2],
                     "text": r[3], "score": round(float(r[4]), 4),
                     "cosine": round(float(r[4]), 4)} for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass

    # Stage 2 — rerank. Only worth it when we over-fetched more than k.
    if rerank and len(base) > int(k):
        ranked = _rerank(query, [d["text"] for d in base], int(k))
        if ranked:
            out = []
            for idx, rel in ranked:
                d = dict(base[idx]); d["score"] = rel; out.append(d)
            return out
    if neutral and len(base) > int(k):
        return _lexical_rerank(query, base, int(k))
    return base[:int(k)]


def retrieve_lessons(query: str, k: int = 5) -> list:
    """Recall PAST-OUTCOME lessons (what worked / failed before) from the
    autopilot_outcomes + brain_finding_outcomes corpora, so a layer can avoid
    repeating a known failure. Thin scoped wrapper over retrieve_context —
    inherits rerank + fail-soft [].

    Dedup (r-rag-lesson-dedup 2026-07-04): the outcome tables write
    near-identical rows per verify pass, so the SAME lesson text can occupy
    several of the k slots and crowd out distinct lessons. Results with
    identical text are collapsed to one; retrieve_context returns best-first,
    so keeping the FIRST occurrence keeps the highest-scoring copy."""
    results = retrieve_context(query, k=k, corpus=list(LESSON_CORPORA))
    seen = set()
    out = []
    for r in results or []:
        t = (r.get("text") or "").strip()
        if t in seen:
            continue
        seen.add(t)
        out.append(r)
    return out


# ── learn station: NEGATIVE recall (agentic-loop #65 part C, 2026-08-22) ──
# The section title the strategic planner renders these under. The planner
# hand-picks ctx keys, so the key ("refuted_claims") and this title ship
# together there; learn_station_status() publishes the title so a shell can
# grep the preview prompt for exactly what the planner emits.
PLANNER_WRONG_SECTION_TITLE = "WHAT WE GOT WRONG (do not repeat)"
LEARN_REINDEX_CADENCE_HOURS = 4    # brain_rag_reindex_4h (cron_heartbeat, :20)

# Text markers that identify a NEGATIVE row inside the mixed lesson corpora
# (their text templates are ours — see CORPORA): autopilot_outcomes renders
# "Action <p>: FAILED — …", brain_finding_outcomes "… → failed." /
# "→ rolled_back." / "→ partial.", brain_lane_decisions "outcome: regressed".
# The two NEGATIVE_LESSON_CORPORA are negative by construction (their `where`
# IS the verdict) and need no marker.
_NEGATIVE_MARKERS = (": FAILED", "→ failed", "→ rolled_back", "→ partial",
                     "outcome: regressed")


def _learn_disabled() -> bool:
    """Kill switch for the learn station's recall + self-test endpoint
    (LEARN_STATION_DISABLE=1). Recall returns [] and the endpoint 404s —
    never 5xx. Indexing is NOT affected (that is BRAIN_RAG_DISABLED's job)."""
    return str(os.environ.get("LEARN_STATION_DISABLE", "")).strip().lower() in (
        "1", "true", "yes")


def _is_negative_text(text: str) -> bool:
    t = text or ""
    return any(m in t for m in _NEGATIVE_MARKERS)


def recall_negative_lessons(query: str, k: int = 4) -> list:
    """Recall what we got WRONG: claims the verifier REFUTED or the owner
    RETRACTED (claim_lessons), proposals rejected as duplicates
    (proposal_lessons), and the FAILED rows of the mixed lesson corpora
    (autopilot / finding / lane outcomes, picked by text marker). Best-first,
    identical texts collapsed (retrieve_lessons' dedup rule), capped at k,
    every result stamped negative=True. Fail-soft → [] (no query, kill
    switch, provider/DB down, any surprise) — recall never blocks a plan."""
    if not query or _learn_disabled():
        return []
    try:
        k = max(1, int(k))
        fetch = max(k * 3, 8)     # room for the negative filter + dedup
        corpus = list(NEGATIVE_LESSON_CORPORA) + [
            c for c in LESSON_CORPORA if c not in NEGATIVE_LESSON_CORPORA]
        results = retrieve_context(query, k=fetch, corpus=corpus)
    except Exception:
        return []
    out, seen = [], set()
    for r in results or []:
        try:
            t = (r.get("text") or "").strip()
        except Exception:
            continue
        if not t or t in seen:
            continue
        if not (r.get("source_table") in NEGATIVE_LESSON_CORPORA or _is_negative_text(t)):
            continue
        seen.add(t)
        d = dict(r)
        d["negative"] = True
        out.append(d)
        if len(out) >= k:
            break
    return out


def _iso(v):
    if v is None:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def _within_cycle(row: dict, now) -> "bool | None":
    """Was the corpus's newest source row embedded within one reindex cycle?
    True = every row embedded and the newest embedding is not older than the
    newest source row; None = nothing to judge (no rows) or the newest row is
    younger than one cadence (+1h slack) and simply not due yet; False = a
    row has waited longer than a cycle and is still pending."""
    import datetime as _dt
    rows = row.get("rows")
    if not rows:
        return None
    ns, ne = row.get("_newest_source"), row.get("_newest_embedding")
    if ns is None:
        return None
    pending = row.get("pending")
    if (pending == 0 and ne is not None and ne >= ns):
        return True
    try:
        age_h = (now - ns).total_seconds() / 3600.0
    except Exception:
        return None
    if age_h <= LEARN_REINDEX_CADENCE_HOURS + 1:
        return None
    return False


def learn_station_status() -> dict:
    """The learn station's self-description for the agentic-loop shell's learn
    lane (imported lazily there). JSON-safe; never raises. Per negative
    corpus: registered / lesson / public (must be False) / table / where /
    fresh_col, live rows under the gate, embedded rows, pending rows, newest
    source vs newest embedding and whether that is within one reindex cycle
    (None = cannot judge yet). Plus the effect bandit's earned vocabulary
    (routes.brain_work_selector.learned_outcome_weights) with its raw sample
    counts, so a lane can print "?" WITH the numbers below the floor."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    out = {
        "ok": True, "generated_at": now.isoformat(),
        "disabled": _learn_disabled(),
        "planner_section": PLANNER_WRONG_SECTION_TITLE,
        "planner_rag_enabled": str(os.environ.get("BRAIN_RAG_ENABLED", "")).strip().lower()
        in ("1", "true", "yes"),
        "reindex_cadence_hours": LEARN_REINDEX_CADENCE_HOURS,
        "corpora": {}, "errors": [],
    }
    for name in NEGATIVE_LESSON_CORPORA:
        spec = CORPORA.get(name)
        out["corpora"][name] = {
            "registered": spec is not None,
            "lesson_corpus": name in LESSON_CORPORA,
            "public": name in PUBLIC_CORPORA,          # MUST stay False
            "table": _src_table(name, spec) if spec else None,
            "where": (spec or {}).get("where"),
            "fresh_col": (spec or {}).get("fresh_col"),
            "newest_source_basis": ((spec or {}).get("fresh_col")
                                    or "created_at (status-flip time is not recorded)"),
            "rows": None, "embedded": None, "pending": None,
            "newest_source_at": None, "newest_embedding_at": None,
            "embedded_within_cycle": None,
        }
    out["leak"] = any(v["public"] for v in out["corpora"].values())
    c = None
    try:
        c = _db()
    except Exception as e:
        out["errors"].append(f"db: {type(e).__name__}")
    if c is None:
        out["ok"] = False
        out["errors"].append("db_unavailable")
    else:
        try:
            with c.cursor() as cur:
                for name in NEGATIVE_LESSON_CORPORA:
                    spec = CORPORA.get(name)
                    if not spec:
                        continue
                    row = out["corpora"][name]
                    tbl = _src_table(name, spec)
                    ts_col = spec.get("fresh_col") or "created_at"
                    try:
                        cur.execute(f"SELECT count(*), max(t.{ts_col}) FROM {tbl} t "
                                    f"WHERE ({spec['where']})")
                        n, newest = cur.fetchone() or (0, None)
                        row["rows"] = int(n or 0)
                        row["_newest_source"] = newest
                        row["newest_source_at"] = _iso(newest)
                    except Exception as e:
                        try: c.rollback()
                        except Exception: pass
                        out["errors"].append(f"{name}: rows unreadable ({type(e).__name__})")
                    try:
                        cur.execute("SELECT count(*), max(updated_at) FROM brain_corpus_embeddings "
                                    "WHERE source_table = %s", (name,))
                        n, newest = cur.fetchone() or (0, None)
                        row["embedded"] = int(n or 0)
                        row["_newest_embedding"] = newest
                        row["newest_embedding_at"] = _iso(newest)
                    except Exception as e:
                        try: c.rollback()
                        except Exception: pass
                        out["errors"].append(f"{name}: embeddings unreadable ({type(e).__name__})")
                    try:
                        cur.execute(f"SELECT count(*) FROM {tbl} t "
                                    f"LEFT JOIN brain_corpus_embeddings e "
                                    f"  ON e.source_table='{name}' AND e.source_id=({spec['id']}) "
                                    f"WHERE e.id IS NULL AND ({spec['where']})")
                        row["pending"] = int((cur.fetchone() or (0,))[0] or 0)
                    except Exception as e:
                        try: c.rollback()
                        except Exception: pass
                        out["errors"].append(f"{name}: pending unreadable ({type(e).__name__})")
                    row["embedded_within_cycle"] = _within_cycle(row, now)
                    row.pop("_newest_source", None)
                    row.pop("_newest_embedding", None)
        except Exception as e:
            out["ok"] = False
            out["errors"].append(f"status read failed: {type(e).__name__}")
        finally:
            try: c.close()
            except Exception: pass
    try:
        from routes.brain_work_selector import learned_outcome_weights
        out["weights"] = learned_outcome_weights()
    except Exception as e:
        out["weights"] = {"measured": False, "non_empty": False,
                          "learned_class_weights": {}, "sample_counts": {},
                          "error": f"{type(e).__name__}: {str(e)[:120]}"}
    if out["errors"]:
        out["ok"] = False
    return out


# ── fix-history corpus (r-rag-fix-history 2026-07-18) ─────────────────
# Memory of the brain's OWN FIXES, so investigations start at "have I solved
# this class before?" instead of from scratch. Three sub-sources, one corpus
# (source_table='fix_history' in the SAME brain_corpus_embeddings store):
#   gh_issue          — closed GitHub issues (title/labels/body/close date).
#                       Harvested LOCALLY via gh CLI (the server has no gh and
#                       no GH token scope guarantee) and PUSHED to the ingest
#                       endpoint as docs.
#   commit            — fix/feat git commit subjects+bodies (rich postmortems).
#                       Same push path: the deployed image has no .git history.
#   resolved_finding  — resolved brain_findings episodes, pulled SERVER-SIDE
#                       (read-only SELECT; writes go through this module's own
#                       upsert, never the findings writer). Stable id
#                       finding#<row id>#<resolved date> — a reopened row that
#                       re-resolves gets a new resolved_at → a NEW episode doc.
# Dedupe = the store's UNIQUE(source_table, source_id); re-runs skip
# already-present ids (cheap idempotency) unless force. Brain-internal —
# deliberately NOT in PUBLIC_CORPORA. Recall via retrieve_prior_fixes().
FIX_HISTORY_TABLE = "fix_history"
_FIX_HISTORY_KINDS = ("gh_issue", "commit", "resolved_finding")
_FIX_DOC_MAX_CHARS = 1600   # matches the left(text,1600) cap of the flat corpora
_FIX_INGEST_DEFAULT_CAP = 200   # docs per run — cron-safe batch size


def normalize_fix_docs(raw) -> tuple:
    """Validate + normalize pushed fix-history docs into the canonical shape
    {id, kind, title, text, date, ref}. Rejects rows missing a stable id/text
    or with an unknown kind; collapses in-batch duplicates on id (first wins —
    idempotent, not an error). Returns (docs, rejected_count). Never raises."""
    docs, seen, rejected = [], set(), 0
    for d in (raw or []):
        if not isinstance(d, dict):
            rejected += 1
            continue
        sid = str(d.get("id") or "").strip()[:200]
        text = str(d.get("text") or "").strip()
        kind = str(d.get("kind") or "").strip()
        if not sid or not text or kind not in _FIX_HISTORY_KINDS:
            rejected += 1
            continue
        if sid in seen:
            continue
        seen.add(sid)
        docs.append({
            "id": sid, "kind": kind,
            "text": text[:_FIX_DOC_MAX_CHARS],
            "title": str(d.get("title") or "").strip()[:300],
            "date": str(d.get("date") or "").strip()[:32],
            "ref": str(d.get("ref") or "").strip()[:400],
        })
    return docs, rejected


def _pending_resolved_finding_docs(cur, limit) -> list:
    """Resolved brain_findings episodes not yet in the fix_history corpus,
    newest resolutions first. Read-only over brain_findings (canonical-writer
    rule untouched — the only write is this module's own embeddings upsert).
    The anti-join keys on the SAME stable id the doc carries, so re-runs are
    naturally incremental. [] on any error (corpus skipped, never fatal)."""
    if limit <= 0:
        return []
    try:
        cur.execute("""
            SELECT t.id, t.issue, t.detector, t.detail, t.resolved_at
              FROM brain_findings t
              LEFT JOIN brain_corpus_embeddings e
                ON e.source_table = %s
               AND e.source_id = 'finding#' || t.id::text || '#'
                                 || to_char(t.resolved_at, 'YYYY-MM-DD')
             WHERE t.resolved_at IS NOT NULL
               AND coalesce(t.issue, '') <> ''
               AND e.id IS NULL
             ORDER BY t.resolved_at DESC
             LIMIT %s
        """, (FIX_HISTORY_TABLE, int(limit)))
        rows = cur.fetchall()
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return []
    docs = []
    for fid, issue, detector, detail, resolved_at in rows:
        try:
            date_s = resolved_at.date().isoformat()
        except Exception:
            date_s = str(resolved_at)[:10]
        docs.append({
            "id": f"finding#{fid}#{date_s}",
            "kind": "resolved_finding",
            "title": (issue or "")[:300],
            "date": date_s,
            "ref": f"brain_findings/{fid}",
            "text": (f"Resolved finding [{detector or 'unknown'}] ({date_s}): "
                     f"{issue or ''} — {detail or ''}")[:_FIX_DOC_MAX_CHARS],
        })
    return docs


def _upsert_fix_docs(c, docs, force=False) -> tuple:
    """Embed + upsert normalized fix-history docs through the store's standard
    ON CONFLICT (source_table, source_id) upsert. Unless force, docs whose id
    is already present are skipped BEFORE embedding (idempotent re-runs cost
    ~0 embed calls). Commits per embed-batch so a mid-run failure keeps
    completed batches. Returns (embedded, skipped_existing)."""
    if not docs:
        return 0, 0
    skipped = 0
    if not force:
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT source_id FROM brain_corpus_embeddings "
                    "WHERE source_table = %s AND source_id = ANY(%s)",
                    (FIX_HISTORY_TABLE, [d["id"] for d in docs]))
                have = {r[0] for r in cur.fetchall()}
        except Exception:
            try: c.rollback()
            except Exception: pass
            have = set()
        if have:
            skipped = sum(1 for d in docs if d["id"] in have)
            docs = [d for d in docs if d["id"] not in have]
    embedded = 0
    for i in range(0, len(docs), _COHERE_BATCH):
        batch = docs[i:i + _COHERE_BATCH]
        vecs = _embed([d["text"] for d in batch], input_type="search_document")
        if not vecs or len(vecs) != len(batch):
            continue
        try:
            with c.cursor() as cur:
                for d, vec in zip(batch, vecs):
                    meta = json.dumps({"title": d.get("title") or "",
                                       "date": d.get("date") or "",
                                       "ref": d.get("ref") or "",
                                       "src": d.get("kind") or ""})
                    cur.execute("""
                        INSERT INTO brain_corpus_embeddings
                          (source_table, source_id, kind, text, embedding, meta)
                        VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb)
                        ON CONFLICT (source_table, source_id) DO UPDATE
                          SET embedding=EXCLUDED.embedding, text=EXCLUDED.text,
                              kind=EXCLUDED.kind, meta=EXCLUDED.meta,
                              updated_at=NOW()
                    """, (FIX_HISTORY_TABLE, d["id"], d["kind"],
                          d["text"][:_FIX_DOC_MAX_CHARS], _vec(vec), meta))
            c.commit()
            embedded += len(batch)
        except Exception:
            try: c.rollback()
            except Exception: pass
    return embedded, skipped


def retrieve_prior_fixes(query: str, k: int = 3) -> list:
    """'Have I solved this class before?' — semantic recall over the
    fix_history corpus (closed GitHub issues + fix/feat commits + resolved
    brain_findings episodes). Returns compact best-first
    [{title, date, ref, kind, score, cosine, text}] ready to attach to an
    investigation context as prior_fixes.

    HARD FAIL-SOFT: [] on ANY error — prior-fix recall must never block an
    investigation (same contract as retrieve_lessons)."""
    try:
        hits = retrieve_context(query, k=k, corpus=FIX_HISTORY_TABLE) or []
    except Exception:
        return []
    if not hits:
        return []
    # Hydrate title/date/ref from the embedding rows' own meta (written at
    # ingest time) — same pattern as market_narratives hydration.
    metas = {}
    c = _db()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT source_id, meta FROM brain_corpus_embeddings "
                    "WHERE source_table = %s AND source_id = ANY(%s)",
                    (FIX_HISTORY_TABLE,
                     [h.get("source_id") for h in hits if h.get("source_id")]))
                metas = {r[0]: (r[1] or {}) for r in cur.fetchall()}
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
    out = []
    for h in hits:
        m = metas.get(h.get("source_id")) or {}
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except Exception:
                m = {}
        text = (h.get("text") or "").strip()
        out.append({
            "title": ((m.get("title") or "").strip()
                      or text.split("\n", 1)[0])[:200],
            "date": m.get("date") or "",
            "ref": m.get("ref") or h.get("source_id") or "",
            "kind": h.get("kind") or m.get("src") or "",
            "score": h.get("score"),
            "cosine": h.get("cosine"),
            "text": text[:400],
        })
    return out


# ── hydration for agent-facing search (attach citable source fields) ───
_HYDRATE = {
    "news_articles": (
        "SELECT id, title, url, source, published_at FROM news_articles WHERE id::text = ANY(%s)",
        lambda r: {"title": r[1], "url": r[2], "source": r[3], "published_at": str(r[4])}),
    "deals": (
        "SELECT id, buyer, seller, value, mw, year, market FROM deals WHERE id::text = ANY(%s)",
        lambda r: {"buyer": r[1], "seller": r[2],
                   "value": (str(r[3]) if r[3] is not None else None),
                   "mw": r[4], "year": r[5], "market": r[6]}),
    "discovered_facilities": (
        "SELECT id, name, provider, city, state, country, market, power_mw, slug "
        "FROM discovered_facilities WHERE id::text = ANY(%s)",
        lambda r: {"name": r[1], "provider": r[2],
                   "location": ", ".join([x for x in (r[3], r[4], r[5]) if x]),
                   "market": r[6], "power_mw": r[7],
                   "url": (build_public_url("facility", r[8]) if r[8] else None)}),
    # Chunked corpus: provenance lives in the embedding row's own meta column
    # (written at index time), so hydration never re-derives the '#<n>' split.
    "market_narratives": (
        "SELECT source_id, meta FROM brain_corpus_embeddings "
        "WHERE source_table='market_narratives' AND source_id = ANY(%s)",
        lambda r: {"title": ((r[1] or {}).get("market_name") or "") + " — DCPI deep-dive",
                   "market": (r[1] or {}).get("market_slug"),
                   "generated_at": (r[1] or {}).get("generated_at"),
                   "url": (r[1] or {}).get("url")}),
    # ── wave-3 corpora (2026-07-25): citable source fields per new kind ──
    # No url: /press/<slug> is served from a DIFFERENT table
    # (press_releases_queue) and only for status='published', so minting a
    # dchub.cloud/press/<slug> link from THIS table's slug hands agents
    # citation URLs that can 404. Omit rather than fabricate — the corpus is
    # publish-gated above, but the canonical URL still isn't ours to guess.
    "press_releases": (
        "SELECT id, title, slug, published_at FROM press_releases WHERE id::text = ANY(%s)",
        lambda r: {"title": r[1], "slug": r[2],
                   "published_at": str(r[3]) if r[3] is not None else None}),
    "announcements": (
        "SELECT id, title, source, source_url, published_date "
        "FROM announcements WHERE id::text = ANY(%s)",
        lambda r: {"title": r[1], "source": r[2], "url": r[3],
                   "published_at": r[4]}),
    "permitting_intel": (
        "SELECT id, title, jurisdiction, state, source_url "
        "FROM permitting_intel WHERE id::text = ANY(%s)",
        lambda r: {"title": r[1],
                   "location": ", ".join([x for x in (r[2], r[3]) if x]),
                   "url": r[4]}),
    "construction_permits": (
        "SELECT id, project_name, permit_number, city, state, source_url "
        "FROM construction_permits WHERE id::text = ANY(%s)",
        lambda r: {"title": r[1] or r[2],
                   "location": ", ".join([x for x in (r[3], r[4]) if x]),
                   "url": r[5]}),
    "tax_incentives_neon": (
        "SELECT id, state_name, source_url FROM tax_incentives_neon "
        "WHERE id::text = ANY(%s)",
        lambda r: {"title": (r[1] or "") + " data-center incentives",
                   "url": r[2]}),
    "capacity_pipeline": (
        "SELECT id, operator, market, status, source_url "
        "FROM capacity_pipeline WHERE id::text = ANY(%s)",
        lambda r: {"title": ((r[1] or "") + " — " + (r[2] or "")).strip(" —"),
                   "status": r[3], "url": r[4]}),
}


# ── ★2026-08-29 lane 5 (corpus-serveability) ─────────────────────────────
#
# A corpus `where` clause gates what gets EMBEDDED. It does not gate what gets
# SERVED, because embeddings are durable: a row embedded while healthy and
# quarantined afterwards stays in the index forever. graph_spine_master_shell
# measured the consequence — 2,811 of 4,348 embedded deal chunks (64.7%)
# pointed at rows /api/deals deliberately refuses to serve, and `deals` is in
# PUBLIC_CORPORA, so they were reachable on the keyless /api/v1/rag/search.
#
# _hydrate fetched citation fields BY ID with no gate at all (0 of 10 entries
# carried one), and an id that failed to hydrate was still returned — just
# with an empty `cite`. The retrieved TEXT is the chunk itself, so the
# quarantined content was served either way; the missing citation was the only
# visible symptom.
#
# So the gate is applied at BOTH ends from ONE map. Where a util module already
# owns the predicate it is imported, never restated: util.deals.deals_ok and
# util.capacity_pipeline.cp_ok exist precisely so two consumers cannot drift,
# and the capacity_pipeline corpus had already drifted by inlining a copy.
def serve_gates() -> dict:
    """table -> SQL predicate a row must satisfy to be SERVED publicly.

    Unaliased (hydrate queries a single table). Only tables whose served
    endpoint refuses rows: a corpus with a content-only `where`
    (coalesce(title,'') <> '') has nothing to enforce here.
    """
    gates = {
        "discovered_facilities": "coalesce(is_duplicate, 0) = 0",
        "press_releases":        "coalesce(published, FALSE) IS TRUE",
        "permitting_intel":      "row_status = 'published'",
    }
    try:
        gates["deals"] = deals_ok()
    except Exception:
        gates["deals"] = "coalesce(data_flag,'') = ''"
    try:
        from util.capacity_pipeline import cp_ok as _cp_ok
        gates["capacity_pipeline"] = _cp_ok()
    except Exception:
        gates["capacity_pipeline"] = "coalesce(data_flag,'') = ''"
    return gates


def _gated_sql(src: str, sql: str) -> str:
    """Append the serve gate to a hydrate query. The base SQL always ends in
    a WHERE, so this is an AND."""
    gate = serve_gates().get(src)
    return f"{sql} AND ({gate})" if gate else sql


def _hydrate(results):
    """Attach citable source fields, and DROP rows that are no longer served.

    Fail-soft on citation, FAIL-CLOSED on serveability: if a gated row cannot
    be confirmed servable it is removed rather than returned uncited. Serving
    a quarantined row without a citation is still serving it.
    """
    gates = serve_gates()
    by_src = {}
    for r in results:
        by_src.setdefault(r["source_table"], []).append(r["source_id"])
    got = {}
    hydrated_ok = set()
    c = _db()
    if c is None:
        # We cannot vouch for anything gated. Dropping is the safe direction
        # for a PUBLIC surface; returning them would republish the leak the
        # moment the DB blips.
        kept = [r for r in results if r["source_table"] not in gates]
        for r in kept:
            r["cite"] = {}
        return kept
    try:
        with c.cursor() as cur:
            for src, ids in by_src.items():
                spec = _HYDRATE.get(src)
                if not spec:
                    continue
                sql, mapper = spec
                try:
                    cur.execute(_gated_sql(src, sql), (ids,))
                    for row in cur.fetchall():
                        got[(src, str(row[0]))] = mapper(row)
                        hydrated_ok.add((src, str(row[0])))
                except Exception:
                    try: cur.connection.rollback()
                    except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    out = []
    for r in results:
        key = (r["source_table"], r["source_id"])
        if r["source_table"] in gates and key not in hydrated_ok:
            # Embedded while healthy, quarantined since — or its hydrate query
            # failed. Either way we cannot show it is still servable.
            continue
        r["cite"] = got.get(key, {})
        out.append(r)
    return out


# ── endpoints ─────────────────────────────────────────────────────────
@brain_rag_bp.route("/api/v1/admin/brain/rag/reindex", methods=["POST", "GET"])
def reindex():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="BRAIN_RAG_DISABLED"), 200
    if not _ensure():
        return jsonify(ok=False, error="ensure_failed (pgvector/table)"), 200
    try:
        cap = max(1, min(1500, int(request.args.get("cap", "500"))))
    except Exception:
        cap = 500
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    embedded = 0
    _embed_health_reset()          # LC6: counters are per-run
    try:
        with c.cursor() as cur:
            rows = _pending(cur, cap)
        for i in range(0, len(rows), _COHERE_BATCH):
            batch = rows[i:i + _COHERE_BATCH]
            vecs = _embed([r[3] or "" for r in batch], input_type="search_document")
            if not vecs or len(vecs) != len(batch):
                continue
            with c.cursor() as cur:
                for (st, sid, kind, text), vec in zip(batch, vecs):
                    cur.execute("""
                        INSERT INTO brain_corpus_embeddings
                          (source_table, source_id, kind, text, embedding)
                        VALUES (%s,%s,%s,%s,%s::vector)
                        ON CONFLICT (source_table, source_id) DO UPDATE
                          SET embedding=EXCLUDED.embedding, text=EXCLUDED.text, updated_at=NOW()
                    """, (st, sid, kind, text, _vec(vec)))
            c.commit()
            embedded += len(batch)
        # Chunked corpus (market_narratives): docs, not rows — each yields
        # ~3-6 chunk embeddings. Cap in DOC units from the leftover budget.
        doc_cap = max(0, min(60, (cap - len(rows)) // 4)) if len(rows) < cap else 0
        if doc_cap:
            embedded += _reindex_chunk_docs(c, doc_cap)
        # GC orphaned embeddings BEFORE the coverage count so `remaining`
        # (and /status coverage_pct next read) reflect the post-sweep store.
        orphans = _sweep_orphans(c)
        with c.cursor() as cur:
            total = _corpus_total(cur)
            # market_narratives (chunk units) and fix_history (external docs,
            # not a CORPORA table) are excluded — both would skew row-vs-row
            # coverage math against _corpus_total.
            cur.execute("SELECT count(*) FROM brain_corpus_embeddings "
                        "WHERE source_table NOT IN ('market_narratives', 'fix_history')")
            emb = cur.fetchone()[0] or 0
            chunk_docs_pending = _pending_chunk_count(cur)
        remaining = max(0, total - emb) + chunk_docs_pending
        # LC6 Lane B — RAG had no dead-man coverage at all. Report DEGRADED when
        # the run "succeeded" but the embeddings are worthless: any 429, any
        # failed batch (reindex() answers those with `continue`), or >5% zero-norm
        # vectors. A clean run beats success with the embedded count.
        _h = _EMBED_HEALTH
        _degraded = bool(
            _h["http_429"] or _h["failed"]
            or (_h["vectors"] and _h["zero_norm"] / max(_h["vectors"], 1) > 0.05))
        _beat_feed("rag-embed-index",
                   status="degraded" if _degraded else "success",
                   rows_inserted=embedded, cadence_hours=24)
        return jsonify(ok=True, embedded=embedded, remaining=remaining,
                       embed_health=dict(_h),
                       narrative_docs_pending=chunk_docs_pending,
                       orphans_deleted=sum(orphans.values()),
                       orphans_by_corpus=orphans,
                       done=(remaining == 0), model=_live_embed_model(),
                       provider=_embed_provider()), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}", embedded=embedded), 200
    finally:
        try: c.close()
        except Exception: pass


@brain_rag_bp.route("/api/v1/admin/brain/rag/retrieve", methods=["GET"])
def retrieve():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(error="q required"), 400
    try:
        k = max(1, min(50, int(request.args.get("k", "8"))))
    except Exception:
        k = 8
    corpus = (request.args.get("corpus") or "").strip() or None
    return jsonify(ok=True, query=q, corpus=corpus, results=retrieve_context(q, k, corpus)), 200


@brain_rag_bp.route("/api/v1/admin/brain/rag/ingest-fix-history", methods=["POST"])
def ingest_fix_history():
    """Ingest the brain's FIX HISTORY into the RAG store (corpus 'fix_history').

    JSON body (all optional):
      docs:          [{id, kind: gh_issue|commit|resolved_finding, title,
                       text, date, ref}] — pushed by the local harvester
                      (gh CLI + git log run on a dev machine; the deployed
                      image has neither gh nor the git history).
      cap:           max docs embedded this run (default 200 — cron-safe).
      skip_findings: don't pull resolved brain_findings server-side.
      force:         re-embed docs that already exist (default: skip them,
                     so re-runs are cheap and idempotent).

    Pushed docs spend the cap first; resolved brain_findings episodes ride
    the leftover budget (so a body-less cron call ingests up to cap freshly
    resolved findings). Reports counts per source."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="BRAIN_RAG_DISABLED"), 200
    if not _ensure():
        return jsonify(ok=False, error="ensure_failed (pgvector/table)"), 200
    body = request.get_json(silent=True) or {}
    try:
        cap = max(1, min(500, int(body.get("cap", _FIX_INGEST_DEFAULT_CAP))))
    except Exception:
        cap = _FIX_INGEST_DEFAULT_CAP
    force = str(body.get("force", "")).lower() in ("1", "true", "yes")
    skip_findings = str(body.get("skip_findings", "")).lower() in ("1", "true", "yes")
    raw = body.get("docs") or []
    pushed, rejected = normalize_fix_docs(raw)
    pushed = pushed[:cap]
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        emb_pushed, skip_pushed = _upsert_fix_docs(c, pushed, force=force)
        findings_docs = []
        emb_findings = skip_findings_n = 0
        budget = cap - len(pushed)
        if not skip_findings and budget > 0:
            with c.cursor() as cur:
                findings_docs = _pending_resolved_finding_docs(cur, budget)
            emb_findings, skip_findings_n = _upsert_fix_docs(
                c, findings_docs, force=force)
        by_kind = {}
        total = 0
        with c.cursor() as cur:
            try:
                cur.execute(
                    "SELECT kind, count(*) FROM brain_corpus_embeddings "
                    "WHERE source_table = %s GROUP BY kind",
                    (FIX_HISTORY_TABLE,))
                by_kind = {str(k): int(v) for k, v in cur.fetchall()}
                total = sum(by_kind.values())
            except Exception:
                try: c.rollback()
                except Exception: pass
        return jsonify(
            ok=True, corpus=FIX_HISTORY_TABLE, cap=cap,
            received=len(raw), rejected=rejected,
            pushed_embedded=emb_pushed, pushed_skipped_existing=skip_pushed,
            findings_selected=len(findings_docs),
            findings_embedded=emb_findings,
            findings_skipped_existing=skip_findings_n,
            corpus_total=total, corpus_by_kind=by_kind,
            provider=_embed_provider()), 200
    except Exception as e:
        return jsonify(ok=False,
                       error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    finally:
        try: c.close()
        except Exception: pass


# ── keyed-caller check for the public search gate ─────────────────────
# _resolve_caller_tier maps a dch_live_/dch_trial_ free key to FREE (rank 0),
# same as fully-anonymous — so "keyed" needs its own check: any VALIDATED
# identity (tier rank, internal/admin key, session cookie via
# caller_is_privileged) OR a live key in mcp_dev_keys / api_keys. Small TTL
# cache so repeat searches don't pay a DB lookup per call. Fail-closed →
# keyless cap.
_KEYED_CACHE = {}          # key -> (expires_epoch, bool)
_KEYED_TTL = 300


def _key_is_live(key: str) -> bool:
    import time
    now = time.time()
    hit = _KEYED_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    ok = False
    c = _db()
    if c is not None:
        try:
            with c.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT 1 FROM mcp_dev_keys WHERE api_key = %s AND status = 'active' LIMIT 1",
                        (key,))
                    ok = cur.fetchone() is not None
                except Exception:
                    try: c.rollback()
                    except Exception: pass
                if not ok:
                    try:
                        cur.execute(
                            "SELECT 1 FROM api_keys WHERE (key_prefix = %s OR key_hash = %s) LIMIT 1",
                            (key[:16], key))
                        ok = cur.fetchone() is not None
                    except Exception:
                        try: c.rollback()
                        except Exception: pass
        finally:
            try: c.close()
            except Exception: pass
    if len(_KEYED_CACHE) > 5000:
        _KEYED_CACHE.clear()
    _KEYED_CACHE[key] = (now + _KEYED_TTL, ok)
    return ok


def _search_caller_keyed() -> bool:
    try:
        from routes.tier_gate import caller_is_privileged
        if caller_is_privileged("IDENTIFIED"):
            return True
    except Exception:
        pass
    key = (request.headers.get("X-API-Key") or request.args.get("api_key") or "").strip()
    if not key:
        return False
    try:
        return _key_is_live(key)
    except Exception:
        return False


_ANON_SEARCH_K = 3


@brain_rag_bp.route("/api/v1/rag/search", methods=["GET"])
def public_search():
    """Agent-facing SEMANTIC search over the public corpora (news / deals /
    facilities / market deep-dive narratives) — meaning-based retrieval +
    citable fields, not keyword/SQL. Brain internals (findings/recs) are never
    exposed here.

    Gate (RAG v1, 2026-07-03): keyless/anonymous callers are capped at k=3 —
    this endpoint previously handed full k=15 hydrated results to anyone over
    plain HTTP, bypassing the MCP trial trim. Any validated key (free dev key
    included) gets full k; claiming one is a single call to claim_free_key."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(error="q required"), 400
    try:
        k = max(1, min(15, int(request.args.get("k", "8"))))
    except Exception:
        k = 8
    keyed = _search_caller_keyed()
    capped = False
    if not keyed and k > _ANON_SEARCH_K:
        k = _ANON_SEARCH_K
        capped = True
    req_corpus = (request.args.get("corpus") or "").strip()
    if req_corpus:
        cs = [x.strip() for x in req_corpus.split(",") if x.strip() in PUBLIC_CORPORA]
    else:
        cs = list(PUBLIC_CORPORA)
    if not cs:
        return jsonify(error="corpus must be one or more of " + ",".join(PUBLIC_CORPORA)), 400
    results = _hydrate(retrieve_context(q, k, corpus=cs))
    # Agentic demand capture (2026-07-18): pgvector always returns nearest
    # neighbors, so a search that "answered" with only WEAK matches is still
    # unmet demand. Absolute cosine CANNOT gate this on the live provider —
    # mistral-embed is symmetric and scores ~0.75+ even for nonsense (an
    # absurd probe out-scored a plausible one live) — so relevance is judged
    # by TERM OVERLAP: how many significant query terms actually appear in
    # the top result texts (provider-independent, deterministic). Queries
    # with >=3 terms need 2 hits; shorter need 1. Keyword-fallback/empty
    # results are misses too. Lazy import, fail-soft; the shell's demand
    # lane clusters captures into unmet_demand findings.
    try:
        _terms = [t for t in re.findall(r"[a-z0-9]{3,}", q.lower())
                  if t not in ("the", "and", "for", "with", "near", "data")][:10]
        _hay = " ".join((r.get("text") or "") for r in results[:3]).lower()
        _hits = sum(1 for t in set(_terms) if t in _hay)
        _need = 1 if len(set(_terms)) <= 2 else 2
        _best = max((r.get("cosine") or 0.0) for r in results) if results else 0.0
        if not results or _hits < _need:
            from routes.agentic_master_shell import capture_query_miss
            capture_query_miss("rag_public_search", q,
                               {"corpus": cs, "top_cosine": round(_best, 3),
                                "term_hits": _hits, "terms": len(set(_terms))})
    except Exception:
        pass
    out = dict(ok=True, query=q, corpus=cs, count=len(results), results=results,
               _cite="Data: DC Hub (dchub.cloud), CC-BY-4.0 — cite as \"DC Hub, dchub.cloud\"")
    if capped:
        out["k_capped"] = _ANON_SEARCH_K
        out["_unlock"] = {
            "message": (f"Keyless callers get the top {_ANON_SEARCH_K} results. Any free key "
                        f"unlocks full k (up to 15) — claim one in a single call, no email."),
            "claim_url": "https://dchub.cloud/api/v1/keys/claim",
            "how": "Retry with header X-API-Key: <your key>.",
        }
    return jsonify(out), 200


@brain_rag_bp.route("/api/v1/admin/brain/rag/duplicate-findings", methods=["GET"])
def duplicate_findings():
    """Semantic dedup: near-duplicate OPEN findings (theme-dups that fuzzy/keyword
    dedup misses) so the janitor/L6 can merge them. Bounded to the recent scan set
    to keep the pairwise cosine cheap."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    try:
        thr = max(0.5, min(0.99, float(request.args.get("threshold", "0.88"))))
    except Exception:
        thr = 0.88
    try:
        limit = max(1, min(200, int(request.args.get("limit", "50"))))
    except Exception:
        limit = 50
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    pairs = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                WITH recent AS (
                    SELECT id, source_id, text, embedding
                    FROM brain_corpus_embeddings
                    WHERE source_table='brain_findings'
                    ORDER BY id DESC LIMIT 400
                )
                SELECT a.source_id, b.source_id,
                       round((1 - (a.embedding <=> b.embedding))::numeric, 4),
                       left(a.text, 110), left(b.text, 110)
                FROM recent a
                JOIN brain_corpus_embeddings b
                  ON b.source_table='brain_findings' AND b.id > a.id
                 AND (1 - (a.embedding <=> b.embedding)) >= %s
                JOIN brain_findings fa ON fa.id::text = a.source_id
                 AND coalesce(fa.status,'open') NOT IN ('resolved','wont_fix')
                JOIN brain_findings fb ON fb.id::text = b.source_id
                 AND coalesce(fb.status,'open') NOT IN ('resolved','wont_fix')
                ORDER BY 3 DESC LIMIT %s
            """, (thr, limit))
            for a, b, sim, ta, tb in cur.fetchall():
                pairs.append({"a": a, "b": b, "similarity": float(sim),
                              "a_text": ta, "b_text": tb})
        return jsonify(ok=True, threshold=thr, pairs=len(pairs), duplicates=pairs), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    finally:
        try: c.close()
        except Exception: pass


@brain_rag_bp.route("/api/v1/admin/brain/rag/status", methods=["GET"])
def status():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT source_table, count(*) FROM brain_corpus_embeddings GROUP BY source_table")
                by = dict(cur.fetchall())
                cur.execute("SELECT max(updated_at) FROM brain_corpus_embeddings")
                last = cur.fetchone()[0]
            except Exception:
                c.rollback(); by = {}; last = None
            # coverage_pct compares flat corpora only — market_narratives is
            # chunk-rows vs docs (different units) and fix_history is external
            # docs (no CORPORA total to compare against); both get own lines.
            emb = sum(v for s, v in by.items()
                      if s not in ("market_narratives", FIX_HISTORY_TABLE))
            total = 0
            per = {}
            for src, spec in CORPORA.items():
                try:
                    cur.execute(f"SELECT count(*) FROM {_src_table(src, spec)} t WHERE ({spec['where']})")
                    n = cur.fetchone()[0] or 0
                except Exception:
                    try: cur.connection.rollback()
                    except Exception: pass
                    n = 0
                total += n
                per[src] = f"{by.get(src, 0)}/{n}"
            # Chunked corpus: coverage in chunk-rows + docs-pending (regenerated
            # docs count as pending until re-chunked — the staleness predicate).
            try:
                cur.execute("SELECT count(*) FROM market_deep_dives WHERE coalesce(narrative_md,'') <> ''")
                _docs = cur.fetchone()[0] or 0
            except Exception:
                try: cur.connection.rollback()
                except Exception: pass
                _docs = 0
            per["market_narratives"] = (f"{by.get('market_narratives', 0)} chunks / "
                                        f"{_docs} docs ({_pending_chunk_count(cur)} pending)")
            per[FIX_HISTORY_TABLE] = (
                f"{by.get(FIX_HISTORY_TABLE, 0)} docs "
                f"(gh issues + fix/feat commits + resolved findings; "
                f"POST ingest-fix-history)")
            # fresh_col activation per corpus, checked against the LIVE
            # schema — the deploy-visible answer to "did freshness actually
            # engage?" (a declared column that's missing or non-timestamp
            # shows inactive_missing_or_wrong_type, corpus stays insert-only).
            fresh = {}
            for src, spec in CORPORA.items():
                fc = spec.get("fresh_col")
                if not fc:
                    fresh[src] = "none (insert-only)"
                elif _fresh_col_active(cur, _src_table(src, spec), fc):
                    fresh[src] = f"active ({fc})"
                else:
                    fresh[src] = f"inactive_missing_or_wrong_type ({fc})"
            # Orphan drift (r-rag-orphan-gc): embeddings whose source row is
            # gone/filtered — swept by reindex; shown here so drift is
            # visible before it pushes coverage_pct past 100 again.
            orphans = _count_orphans(cur)
        return jsonify(ok=True, embedded=emb, corpus_total=total,
                       coverage_pct=round(100.0 * emb / max(1, total), 1),
                       by_corpus=per, fresh_cols=fresh,
                       orphans=orphans, orphans_total=sum(orphans.values()),
                       last_indexed=str(last), model=_live_embed_model(),
                       provider=_embed_provider(),
                       planner_wired=str(os.environ.get("BRAIN_RAG_ENABLED", "")).lower() in ("1", "true", "yes")), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    finally:
        try: c.close()
        except Exception: pass


@brain_rag_bp.route("/api/v1/brain/learn/recall", methods=["GET"])
def learn_recall():
    """Learn-station self-test (agentic-loop #65 part C): the negative lessons
    the planner and the lane driver would RECALL for ?q=, plus
    learn_station_status(). Admin-gated; LEARN_STATION_DISABLE=1 → 404, never
    5xx. Lives under /api/v1/brain/ on purpose — that prefix carries the
    Cloudflare bypass (/api/v1/admin/* GETs are edge-cached 17–42 min)."""
    if _learn_disabled():
        return jsonify(error="not found"), 404
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(error="q required",
                       example="/api/v1/brain/learn/recall?q=deals"), 400
    try:
        k = max(1, min(10, int(request.args.get("k", "4"))))
    except Exception:
        k = 4
    lessons = recall_negative_lessons(q, k=k)
    resp = jsonify(ok=True, query=q, k=k, count=len(lessons), lessons=lessons,
                   planner_section=(PLANNER_WRONG_SECTION_TITLE if lessons else None),
                   status=learn_station_status())
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200
