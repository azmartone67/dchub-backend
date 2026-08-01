"""
routes/brain_shipped_state.py — SHIPPED-STATE feeds (r-shipstate, 2026-07-31).

WHY THIS EXISTS (all measured, 2026-07-31)
------------------------------------------
The 2026-07-31 innovation digest led with SIX "Next Product Moves — awaiting
your approval" for `get_power_availability_timeline` — a tool that had shipped
LIVE ~11 hours earlier (dchub-mcp-server PR #113, "registered AND routable").
The six were the newest of EIGHT open proposals for it, all dated 2026-05-27
(the every-2h curator era, before the r81 dedupe guard), never graded, and
therefore headlining every digest since. Three distinct blind spots:

  1. L23's proposal prompt carried a HARDCODED "Existing 23 MCP tools" list —
     written when there were 23. The gateway serves 82; every tool added since
     looked "novel" to the curator.
  2. `_fetch_shipped_context` / `_proposal_already_exists` only see work that
     shipped THROUGH the L23 lane (`brain_lifecycle_proposals.shipped_at`).
     A tool built out-of-band is invisible, so zombie proposals never resolve.
  3. Investigator evidence has no view of merged PRs, so investigations
     re-litigate diagnoses corrected days earlier (digest #100018/#100020 both
     recycled conclusions disproven on 07-28).

WHAT IT IS — three cached, read-only, fail-soft feeds + one reconciler
----------------------------------------------------------------------
  · live_tool_names() — the LIVE gateway registry via ai_surface_canon's
    _mcp_tool_names() (tools/list, probe UA excluded from analytics), falling
    back to canon PINNED["tool_manifest"] so consumers never see an empty
    registry just because the gate hiccuped.
  · recent_merged_prs() — merged PR titles (default 14d) from the repos in
    BRAIN_SHIPPED_REPOS (backend + mcp-server) via the GitHub API
    (GITHUB_TOKEN / PR_SUBMIT_TOKEN; unauthenticated works at low rate).
  · gather_shipped_state() — investigator Source 9 items. NOTE the consumer
    contract: brain_investigator._evidence_block CLIPS values at 200 chars, so
    the registry/PR lists are CHUNKED into <=190-char values — one giant value
    would silently truncate to 8 tool names and defeat the purpose.
  · shipped_state_block() — the UN-clipped prompt block for L23's curator.
  · reconcile_l23_shipped() — marks open/approved L23 proposals whose
    capability is now LIVE on the gateway as shipped (the zombie sweep).

DEFAULT ON, kill switch BRAIN_SHIPPED_STATE_DISABLE=1
-----------------------------------------------------
Deliberately NOT the dark-by-default Source 7/8 convention: this source exists
to kill staleness, and a staleness-killer shipped dark IS the failure class it
fixes (Source 8 sat dark for weeks; both Source 7/8 flags are enabled in prod
anyway). Everything here is read-only except the one UPDATE in the reconciler,
which is parameterized, id-scoped, and idempotent.

SAFETY: every network/DB touch is try/except + bounded timeout; failures
negative-cache briefly so a down dependency costs one timeout per 2 minutes,
not one per investigation. NEVER raises out of any public function.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_UA = "dchub-brain/1.0"
_TTL_S = 600          # happy-path cache: 10 min
_NEG_TTL_S = 120      # failure cache: don't re-pay timeouts every call
_DEFAULT_REPOS = "azmartone67/dchub-backend,azmartone67/dchub-mcp-server"

# key -> (expires_at_epoch, value)
_CACHE: dict = {}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _disabled() -> bool:
    return _truthy(os.environ.get("BRAIN_SHIPPED_STATE_DISABLE"))


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if not hit:
        return None
    expires_at, value = hit
    if time.time() >= expires_at:
        return None
    return value


def _cache_put(key: str, value, ttl_s: int) -> None:
    _CACHE[key] = (time.time() + ttl_s, value)


def _norm_name(n: str) -> str:
    """Mirror of brain_layer23_lifecycle._norm_cap_name (kept dependency-free
    here so this module never imports Flask-bearing route modules)."""
    return (n or "").strip().lower().replace("-", "_").replace(" ", "_")


# ── Feed 1: the LIVE tool registry ───────────────────────────────────

def live_tool_names_with_source(timeout: int = 6) -> tuple[list[str], str]:
    """(sorted tool names, source) where source is 'tools/list' (live probe),
    'pinned' (canon fallback manifest) or 'none'. Cached; never raises."""
    if _disabled():
        return [], "none"
    cached = _cache_get("tools")
    if cached is not None:
        return cached
    names: list[str] = []
    src = "none"
    try:
        from ai_surface_canon import _mcp_tool_names  # raises on transport
        got = _mcp_tool_names(timeout=timeout)
        if got:
            names = sorted({str(n) for n in got if n})
            src = "tools/list"
    except Exception as e:
        logger.warning("brain_shipped_state: live tools/list probe failed: %s", e)
    if not names:
        try:
            from ai_surface_canon import PINNED
            pinned = PINNED.get("tool_manifest") or []
            if pinned:
                names = sorted({str(n) for n in pinned if n})
                src = "pinned"
        except Exception as e:
            logger.warning("brain_shipped_state: pinned manifest fallback failed: %s", e)
    out = (names, src)
    _cache_put("tools", out, _TTL_S if names else _NEG_TTL_S)
    return out


def live_tool_names(timeout: int = 6) -> list[str]:
    return live_tool_names_with_source(timeout=timeout)[0]


def is_live_tool(name: str) -> Optional[str]:
    """Return the LIVE registry tool name matching `name`, else None.
    Matching is case/separator-insensitive and lenient about a leading `get_`
    ('power_availability_timeline' == 'get_power_availability_timeline' — the
    same capability, and exactly the alias an LLM curator produces).
    Never raises."""
    try:
        norm = _norm_name(name)
        if not norm:
            return None
        stripped = norm[4:] if norm.startswith("get_") else norm
        for live in live_tool_names():
            ln = _norm_name(live)
            ls = ln[4:] if ln.startswith("get_") else ln
            if norm == ln or (stripped and stripped == ls):
                return live
    except Exception as e:
        logger.warning("brain_shipped_state: is_live_tool failed: %s", e)
    return None


# ── Feed 2: merged PRs (the "what already landed" record) ────────────

def _github_json(url: str, timeout: int = 6):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _UA)
    req.add_header("Accept", "application/vnd.github+json")
    tok = (os.environ.get("GITHUB_TOKEN") or os.environ.get("PR_SUBMIT_TOKEN") or "").strip()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def recent_merged_prs(days: int = 14, per_repo: int = 30) -> list[dict]:
    """Merged PRs (newest first per repo) across BRAIN_SHIPPED_REPOS as
    {repo, number, title, merged_at}. One repo failing never hides the other.
    Cached; [] on total failure; never raises."""
    if _disabled():
        return []
    key = f"prs:{days}:{per_repo}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    repos = [r.strip() for r in
             (os.environ.get("BRAIN_SHIPPED_REPOS") or _DEFAULT_REPOS).split(",")
             if r.strip()]
    cutoff = time.time() - days * 86400
    out: list[dict] = []
    any_ok = False
    for repo in repos:
        try:
            rows = _github_json(
                f"https://api.github.com/repos/{repo}/pulls"
                f"?state=closed&sort=updated&direction=desc&per_page={int(per_repo)}"
            )
            any_ok = True
            for p in rows or []:
                merged = p.get("merged_at")
                if not merged:
                    continue  # closed-unmerged is not shipped state
                try:
                    ts = time.mktime(time.strptime(merged, "%Y-%m-%dT%H:%M:%SZ"))
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                title = re.sub(r"\s+", " ", str(p.get("title") or "")).strip()
                out.append({
                    "repo": repo.split("/")[-1],
                    "number": p.get("number"),
                    "title": title,
                    "merged_at": merged,
                })
        except Exception as e:
            logger.warning("brain_shipped_state: PR fetch failed for %s: %s", repo, e)
    _cache_put(key, out, _TTL_S if any_ok else _NEG_TTL_S)
    return out


# ── Feed 3a: investigator Source 9 (200-char-clip-aware chunks) ──────

def _chunk_csv(items: list[str], limit: int = 190, max_chunks: int = 12) -> list[str]:
    """Comma-join `items` into chunks whose rendered length stays under the
    investigator's 200-char value clip (with margin). Order preserved."""
    chunks: list[str] = []
    cur = ""
    for it in items:
        piece = it if not cur else cur + ", " + it
        if len(piece) > limit and cur:
            chunks.append(cur)
            cur = it[:limit]
        else:
            cur = piece[:limit] if len(piece) > limit else piece
        if len(chunks) >= max_chunks:
            return chunks
    if cur and len(chunks) < max_chunks:
        chunks.append(cur)
    return chunks


def gather_shipped_state() -> list[dict]:
    """Investigator Source 9: SHIPPED STATE as {claim, source, value} items.
    Every value is <=190 chars because _evidence_block clips at 200 — a single
    long value would silently truncate the registry to ~8 names. [] when the
    kill switch is on or nothing could be fetched. Never raises."""
    if _disabled():
        return []
    items: list[dict] = []
    try:
        names, src = live_tool_names_with_source()
        if names:
            chunks = _chunk_csv(names, max_chunks=16)
            # No silent caps (house rule): if the registry ever outgrows the
            # chunk budget, SAY so instead of quietly dropping the tail.
            covered = sum(1 for c in chunks for x in c.split(", ") if x)
            shown = (f" — WARNING: only {covered} of {len(names)} names shown"
                     if covered < len(names) else "")
            items.append({
                "claim": (f"LIVE MCP tool registry — {len(names)} tools ALREADY EXIST "
                          f"on the gateway (full names in the {len(chunks)} "
                          "SHIPPED-TOOLS items below). NEVER propose building one of "
                          f"these and NEVER diagnose one as missing{shown}"),
                "source": f"mcp tools/list via ai_surface_canon [{src}]",
                "value": len(names),
            })
            for i, ch in enumerate(chunks, 1):
                items.append({
                    "claim": f"SHIPPED-TOOLS ({i}/{len(chunks)}) — already live",
                    "source": f"mcp tools/list [{src}]",
                    "value": ch,
                })
    except Exception as e:
        logger.warning("brain_shipped_state: tools evidence failed: %s", e)
    try:
        prs = recent_merged_prs()
        by_repo: dict[str, list[dict]] = {}
        for p in prs:
            by_repo.setdefault(p["repo"], []).append(p)
        for repo, rows in by_repo.items():
            titles = [f"#{r['number']} {r['title']}" for r in rows]
            for i, ch in enumerate(_chunk_csv(titles, max_chunks=2), 1):
                items.append({
                    "claim": (f"MERGED PRs last 14d, {repo} ({i}) — this work ALREADY "
                              "LANDED; do not re-diagnose it as an open gap, and "
                              "re-verify any finding older than these merges before "
                              "citing it"),
                    "source": f"github {repo} merged PRs (newest first)",
                    "value": ch,
                })
    except Exception as e:
        logger.warning("brain_shipped_state: merged-PR evidence failed: %s", e)
    return items


# ── Feed 3b: the un-clipped prompt block (L23 curator) ───────────────

def shipped_state_block(max_chars: int = 3200) -> str:
    """Prompt-ready shipped-state block: the full live registry + recent
    merges. Empty string when disabled or nothing available. Never raises."""
    if _disabled():
        return ""
    parts: list[str] = []
    try:
        names, src = live_tool_names_with_source()
        if names:
            parts.append(
                f"LIVE MCP TOOLS ({len(names)}, from {src} — these ALREADY EXIST; "
                "do NOT propose any of these or close variants of them):\n"
                + ", ".join(names)
            )
    except Exception:
        pass
    try:
        prs = recent_merged_prs()
        if prs:
            newest = sorted(prs, key=lambda p: p.get("merged_at") or "", reverse=True)[:24]
            lines = [f"  {p['repo']} #{p['number']} {p['title']}" for p in newest]
            parts.append("MERGED PRs (last 14d — this work already landed):\n"
                         + "\n".join(lines))
    except Exception:
        pass
    return "\n\n".join(parts)[:max_chars]


# ── The reconciler: zombie proposals whose capability went live ──────

def _own_conn():
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_shipped_state: _own_conn failed: %s", e)
    return None


def reconcile_l23_shipped(conn=None) -> dict:
    """Mark un-dismissed, un-shipped brain_lifecycle_proposals whose capability
    name matches a LIVE gateway tool as shipped (shipped_at=NOW(), note
    appended). This is how out-of-band ships resolve zombie proposals — the 8
    get_power_availability_timeline rows from 2026-05-27 headlined every
    digest until this sweep existed. Idempotent (matched rows leave the WHERE
    population). Returns {matched, ids, names, checked}; never raises."""
    result = {"matched": 0, "ids": [], "names": [], "checked": 0}
    if _disabled():
        return result
    live = live_tool_names()
    if not live:
        return result  # no registry, no verdicts — never mark on emptiness
    own = False
    if conn is None:
        conn = _own_conn()
        own = True
    if conn is None:
        return result
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT id, proposal_text FROM brain_lifecycle_proposals "
                    "WHERE shipped_at IS NULL AND dismissed_at IS NULL "
                    "ORDER BY proposed_at DESC LIMIT 500"
                )
                rows = cur.fetchall() or []
            except Exception:
                # pre-r42 schema without dismissed_at (mirrors the email
                # module's fallback) — never let the sweep die on a column.
                try:
                    conn.rollback()
                except Exception:
                    pass
                cur.execute(
                    "SELECT id, proposal_text FROM brain_lifecycle_proposals "
                    "WHERE shipped_at IS NULL "
                    "ORDER BY proposed_at DESC LIMIT 500"
                )
                rows = cur.fetchall() or []
            result["checked"] = len(rows)
            hit_ids: list[int] = []
            hit_names: list[str] = []
            for pid, text in rows:
                try:
                    nm = (json.loads(text or "{}") or {}).get("name") or ""
                except Exception:
                    nm = ""
                live_name = is_live_tool(nm) if nm else None
                if live_name:
                    hit_ids.append(pid)
                    hit_names.append(f"{nm}->{live_name}")
            if hit_ids:
                stamp = (" [auto r-shipstate] capability detected LIVE on the MCP "
                         "gateway (tools/list) " + time.strftime("%Y-%m-%d"))
                cur.execute(
                    "UPDATE brain_lifecycle_proposals "
                    "SET shipped_at = NOW(), "
                    "    notes = COALESCE(notes,'') || %s "
                    "WHERE id = ANY(%s) AND shipped_at IS NULL",
                    (stamp, hit_ids),
                )
                conn.commit()
            result["matched"] = len(hit_ids)
            result["ids"] = hit_ids
            result["names"] = sorted(set(hit_names))
    except Exception as e:
        logger.warning("brain_shipped_state: reconcile failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass
    return result
