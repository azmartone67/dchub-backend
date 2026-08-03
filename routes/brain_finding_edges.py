"""brain_finding_edges.py — the causal graph, PERSISTED (#49 lane 2, 2026-08-02).

brain_layer14_causal already does the expensive part: it joins related findings,
calls Claude for the ROOT CAUSE, and returns up to four causal chains. Then it
throws the graph away — the output is a narrative in brain_causal_cache, read by
a dashboard and by nothing that makes decisions. So brain_work_selector.rank_work()
sees a FLAT list and can spend a whole run's MAX_DRAFT_PRS_PER_RUN budget on five
symptoms of one cause, landing nothing.

This module is the missing store. It is deliberately a LEAF (no flask, lazy
psycopg2) so the writer, the selector and the tests can all import it.

★KEYED BY ISSUE STRING, NOT BY finding id. L14's `symptoms` are free-text lines
describing findings, not foreign keys, and inventing an id resolution step would
put a fuzzy match on the critical path of a write. The selector's candidates are
dicts carrying an `issue` string, so that is the join key that actually exists on
both sides. Normalisation is deliberately blunt (lowercase, collapse whitespace,
drop a trailing "@ /url" and any "(seen xN)" suffix) — a key that is too specific
silently matches nothing, which reads exactly like "no causal edges found".

Table (self-healed, same idempotent pattern as brain_findings_writer):

    brain_finding_edges(
        cause_key TEXT,      -- normalised root: the chain's title
        effect_key TEXT,     -- normalised symptom
        kind TEXT,           -- one of FINDING_EDGE_KINDS
        confidence REAL,     -- 0..1, from the chain's high/medium/low
        chain_title TEXT,    -- human-readable root, unnormalised
        created_at TIMESTAMPTZ
    )

A chain is REPLACED wholesale on each write (delete-by-cause then insert) so a
re-analysis that drops a symptom does not leave the old edge behind — a stale
edge would collapse two findings that are no longer believed related, which is
worse than having no edge at all.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# The vocabulary. Declared in graph_master_shell.FINDING_EDGE_KINDS and repeated
# here as the writer's own guard so an unknown kind cannot reach the table.
VALID_KINDS = ("causes", "duplicates", "blocks", "symptom_of")

_CONFIDENCE = {"high": 0.9, "medium": 0.6, "low": 0.3}

# Suffixes that make two descriptions of the SAME finding look different.
_SEEN_SUFFIX = re.compile(r"\(\s*(?:seen|value)\s*x?\s*[\d,]+\s*\)", re.I)
_URL_SUFFIX = re.compile(r"\s*@\s*\S+$")
_WS = re.compile(r"\s+")


def normalize_key(text) -> str:
    """Blunt on purpose — see the module docstring. Empty string when there is
    nothing to key on, and callers must treat "" as 'no edge', never as a
    wildcard that matches every finding."""
    t = str(text or "").strip().lower()
    if not t:
        return ""
    t = _SEEN_SUFFIX.sub(" ", t)
    t = _URL_SUFFIX.sub("", t)
    t = t.replace("brain finding:", " ").replace("brain_findings:", " ")
    t = _WS.sub(" ", t).strip(" .:-")
    return t[:200]


def confidence_of(label) -> float:
    if isinstance(label, (int, float)):
        try:
            return max(0.0, min(1.0, float(label)))
        except Exception:
            return 0.5
    return _CONFIDENCE.get(str(label or "").strip().lower(), 0.5)


def _conn():
    try:
        import psycopg2
        url = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL"))
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_finding_edges: connect failed: %s", str(e)[:120])
        return None


def ensure_schema(cur) -> bool:
    """Idempotent. False when the table could not be created — every caller
    treats that as 'no edges', never as an error worth raising."""
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_finding_edges (
                cause_key   TEXT NOT NULL,
                effect_key  TEXT NOT NULL,
                kind        TEXT NOT NULL DEFAULT 'symptom_of',
                confidence  REAL NOT NULL DEFAULT 0.5,
                chain_title TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS brain_finding_edges_effect_idx "
                    "ON brain_finding_edges (effect_key)")
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_finding_edges: ensure_schema failed: %s",
                       str(e)[:160])
        return False
    # Dedup guard, in its OWN try: an edge is identified by (cause, effect), so
    # two workers analysing at once must not double-write it. ★Isolated because
    # a unique index can legitimately fail to build (pre-existing duplicates on
    # a table created before this line), and losing the index is a degraded
    # store — losing the whole store is an outage. The INSERT uses a bare
    # ON CONFLICT DO NOTHING precisely so it stays correct either way.
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS brain_finding_edges_uniq "
                    "ON brain_finding_edges (cause_key, effect_key)")
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_finding_edges: unique index unavailable "
                       "(store still usable): %s", str(e)[:160])
    return True


def edges_from_chain(chain: dict) -> list:
    """One L14 causal chain → edge rows. Pure; no DB, no I/O.

    Skips a chain with no title or fewer than two symptoms: a "chain" of one is
    a finding restating itself, and collapsing on it would merge nothing while
    still consuming a root slot."""
    if not isinstance(chain, dict):
        return []
    title = str(chain.get("title") or "").strip()
    cause = normalize_key(title)
    if not cause:
        return []
    symptoms = chain.get("symptoms")
    if not isinstance(symptoms, list) or len(symptoms) < 2:
        return []
    conf = confidence_of(chain.get("confidence"))
    out, seen = [], set()
    for s in symptoms:
        eff = normalize_key(s)
        # A symptom that normalises to the root adds a self-loop, which would
        # make the root its own child and drop it out of its own group.
        if not eff or eff == cause or eff in seen:
            continue
        seen.add(eff)
        out.append({"cause_key": cause, "effect_key": eff,
                    "kind": "symptom_of", "confidence": conf,
                    "chain_title": title[:200]})
    return out


def write_chains(chains) -> dict:
    """Persist L14's chains. Returns {written, chains, skipped, error}.
    NEVER raises: L14's analyze path must not start failing because a
    diagnostic store is unavailable."""
    report = {"written": 0, "chains": 0, "skipped": 0, "error": ""}
    if not isinstance(chains, list) or not chains:
        return report
    c = _conn()
    if c is None:
        report["error"] = "no_database"
        return report
    try:
        with c.cursor() as cur:
            if not ensure_schema(cur):
                report["error"] = "no_schema"
                return report
            for chain in chains:
                rows = edges_from_chain(chain)
                if not rows:
                    report["skipped"] += 1
                    continue
                cause = rows[0]["cause_key"]
                try:
                    # ★REPLACE, don't append. A re-analysis that drops a symptom
                    # must not leave the old edge behind: a stale edge collapses
                    # two findings nobody believes are related any more, which is
                    # worse than no edge.
                    cur.execute("DELETE FROM brain_finding_edges "
                                "WHERE cause_key = %s", (cause,))
                    for r in rows:
                        # Bare ON CONFLICT DO NOTHING (no target) is valid with
                        # OR without the unique index above, so the write stays
                        # correct on a store whose index failed to build.
                        cur.execute("""
                            INSERT INTO brain_finding_edges
                                (cause_key, effect_key, kind, confidence,
                                 chain_title)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING""",
                            (r["cause_key"], r["effect_key"], r["kind"],
                             r["confidence"], r["chain_title"]))
                    report["written"] += len(rows)
                    report["chains"] += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("brain_finding_edges: chain write failed: %s",
                                   str(e)[:160])
                    report["skipped"] += 1
    except Exception as e:  # noqa: BLE001
        report["error"] = str(e)[:200]
    finally:
        try:
            c.close()
        except Exception:
            pass
    return report


def load_root_map() -> dict:
    """{effect_key: (cause_key, confidence)} — everything the selector needs in
    one read. {} on any failure, which degrades ranking to exactly today's
    flat behaviour."""
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT effect_key, cause_key, confidence "
                            "FROM brain_finding_edges "
                            "WHERE kind = 'symptom_of'")
            except Exception:
                return {}
            for eff, cause, conf in cur.fetchall() or []:
                if eff and cause:
                    out[str(eff)] = (str(cause), float(conf or 0.5))
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_finding_edges: load_root_map failed: %s",
                       str(e)[:160])
        return {}
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def root_of(candidate, root_map: dict) -> str:
    """The root this candidate belongs to, or "" when it is not in any chain.

    Tries the candidate's identifying strings against the map. Substring
    matching is one-directional on purpose: an edge key must appear IN the
    candidate's text, never the reverse — a two-word key like "slow api" would
    otherwise swallow every finding whose text happens to contain it."""
    if not isinstance(candidate, dict) or not root_map:
        return ""
    for field in ("issue", "claim", "title", "rationale"):
        key = normalize_key(candidate.get(field))
        if not key:
            continue
        hit = root_map.get(key)
        if hit:
            return hit[0]
    for field in ("issue", "claim", "title"):
        text = normalize_key(candidate.get(field))
        if len(text) < 8:
            continue
        for eff, (cause, _conf) in root_map.items():
            if len(eff) >= 8 and eff in text:
                return cause
    return ""
