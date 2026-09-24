"""Bounded path key for the AI-crawl per-path counter (ai_daily_path_stats).

r-facility-dead-slug (2026-09-24). meta-externalagent re-crawled
/facilities/null ~60 times in 42h and nothing in /api/ai tracking could say
so: ai_daily_stats is keyed (date, platform) only, and the raw ai_requests
rows are a feed, not a counter. This adds the PATH dimension — additively,
as its own table — without letting arbitrary crawler paths explode it.

★ CARDINALITY IS BOUNDED BY CONSTRUCTION, not by a LIMIT. Every path maps to
  one of a FIXED set of keys:

    1. a dead facility slug — /facilities/null, /facilities/None.json,
       /api/v1/facility/undefined ... — keeps its own key, with the token
       normalised: "/facilities/{null}". 3 sections x 5 tokens = 15 keys.
       These are the hits this counter exists to see.
    2. a KNOWN section (the first segment of a crawler_externality bucket
       prefix, or of the extra list below) collapses to "/<seg>" for the bare
       page and "/<seg>/*" for anything beneath it. The slug, id, query and
       case never reach the key.
    3. everything else is "other".

  So the table grows by at most len(path_key_space()) rows per platform per
  day, whatever a crawler requests. path_key_space() enumerates that set, and
  the test pins every key crawl_path_key() can return to it.
"""
from __future__ import annotations

from util.dead_slug import DEAD_SLUG_TOKENS, is_dead_slug

# Sections whose slug segment is watched for dead-slug hits. Order matters:
# the longest prefix first.
DEAD_SLUG_SECTIONS = ("/api/v1/facilities", "/api/v1/facility", "/facilities")

# Canonical spelling of each dead token in a key ("" -> "blank").
_TOKEN_NAME = {"": "blank", "null": "null", "none": "none",
               "undefined": "undefined", "nan": "nan"}

# First segments that get their own key beyond crawler_externality's buckets.
_EXTRA_SECTIONS = ("api", "facility", "grid", "iso", "llms.txt",
                   "llms-full.txt", "robots.txt", "sitemap.xml", "mcp",
                   ".well-known", "database", "ai")

MAX_KEY_LEN = 80
OTHER = "other"


def _known_sections():
    segs = set(_EXTRA_SECTIONS)
    try:
        from crawler_externality import _BUCKET_RULES
        for _bucket, prefixes in _BUCKET_RULES:
            for p in prefixes:
                seg = str(p).strip("/").split("/", 1)[0].split("?", 1)[0]
                if seg:
                    segs.add(seg.lower())
    except Exception:
        pass
    return frozenset(segs)


KNOWN_SECTIONS = _known_sections()


def _dead_key(path):
    for sec in DEAD_SLUG_SECTIONS:
        if path.lower().startswith(sec + "/"):
            rest = path[len(sec) + 1:].strip("/")
            if rest == "" or "/" in rest:
                return None      # the section root, or a deeper path
            if not is_dead_slug(rest):
                return None
            tok = rest.strip().lower()
            for ext in (".html", ".json"):
                if tok.endswith(ext):
                    tok = tok[: -len(ext)].strip()
            return f"{sec}/{{{_TOKEN_NAME.get(tok, 'blank')}}}"
    return None


def crawl_path_key(path):
    """Map a request path to its bounded counter key. NEVER raises."""
    try:
        p = str(path or "").split("?", 1)[0].split("#", 1)[0].strip()
        if not p.startswith("/"):
            p = "/" + p
        try:
            from urllib.parse import unquote
            p = unquote(p)
        except Exception:
            pass
        dk = _dead_key(p)
        if dk:
            return dk
        parts = [x for x in p.split("/") if x != ""]
        if not parts:
            return "/"
        seg = parts[0].lower()
        if seg not in KNOWN_SECTIONS:
            return OTHER
        key = f"/{seg}/*" if len(parts) > 1 else f"/{seg}"
        return key if len(key) <= MAX_KEY_LEN else OTHER
    except Exception:
        return OTHER


def path_key_space():
    """Every key crawl_path_key() can return — the table's cardinality bound
    per (date, platform)."""
    keys = {"/", OTHER}
    for seg in KNOWN_SECTIONS:
        for k in (f"/{seg}", f"/{seg}/*"):
            if len(k) <= MAX_KEY_LEN:
                keys.add(k)
    for sec in DEAD_SLUG_SECTIONS:
        for tok in DEAD_SLUG_TOKENS:
            keys.add(f"{sec}/{{{_TOKEN_NAME[tok]}}}")
    return frozenset(keys)
