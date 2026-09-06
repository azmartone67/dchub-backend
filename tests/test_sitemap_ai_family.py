#!/usr/bin/env python3
"""tests/test_sitemap_ai_family.py — the ungated facility sitemap for retrieval
crawlers, and the two ways it could quietly become a liability.

NO NETWORK, NO DB.

WHY IT EXISTS. The capacity gate (see test_sitemap_thin_gate.py) submits only
pages that can rank, and for Google and Bing that is still the right trade. It
is the wrong one for a retrieval crawler: Perplexity and GPTBot are building an
entity index, not ranking us against a competitor, and a facility with no
capacity figure is still a unique record that can ground an answer.

Measured over the 7d to 2026-09-05 (ai_requests, all HTTP 200):

    chatgpt     12,001 distinct /facilities/ URLs · 17,039 hits
    perplexity   7,563 distinct                   ·  7,736 hits

GPTBot reached roughly DOUBLE the 6,266 URLs the sitemap publishes, by
following links. The publishable universe is 20,488.

THE TWO FAILURES THIS FILE EXISTS TO CATCH:

  1. The AI family leaking into /sitemap.xml. That file is the artefact
     submitted to GSC and Bing Webmaster; putting 20k ungated URLs in it
     defeats the capacity gate it sits beside, and the failure looks like
     success — a valid sitemap, served 200, with more entries.

  2. The AI family falling back to a LIVE build. It is the ~20k-row union that
     saturated the Neon primary in the 07-20 stampede, and it is fetched by
     crawlers sweeping thousands of URLs an hour. One cold cache would rebuild
     it per request.

Assertions that scan source run COMMENT-STRIPPED: the comments in main.py quote
both failure modes in order to explain them, and a guard that matches its own
documentation gets "fixed" by deleting the documentation.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "main.py")


def _full():
    return open(SRC, encoding="utf-8").read()


def _fn(name, src=None):
    """Source of one top-level function, comments stripped."""
    s = src if src is not None else _full()
    i = s.index("def %s(" % name)
    j = s.index("\ndef ", i + 10)
    return "\n".join(l for l in s[i:j].splitlines()
                     if not l.lstrip().startswith("#"))


def _consts():
    """Exec just the constants + the pure helper, with no app import."""
    s = _full()
    ns = {}
    for pat in (r"^_SITEMAP_FACILITIES_PER_SHARD = .*$",
                r"^_SITEMAP_AI_INDEX_KEY = .*$",
                r"^_SITEMAP_AI_SHARD_PREFIX = .*$"):
        m = re.search(pat, s, re.M)
        assert m, "constant missing: %s" % pat
        exec(m.group(0), ns)
    exec(_fn("_ai_shard_keys"), ns)
    return ns


# ── 1. The gate-defeat guard ────────────────────────────────────────────────

def test_ai_shards_ARE_in_the_submitted_index_now():
    """★ REVERSED 2026-09-06, on evidence gathered after the exclusion.

    This test previously asserted the opposite — that the AI family must never
    appear in /sitemap.xml, because that file is what GSC and Bing Webmaster
    read. What changed:

      · Crawlers discover shards by WALKING THIS INDEX, not by reading our
        discovery files. GPTBot fetched /sitemap-markets.xml, named nowhere but
        here, and GUESSED /sitemap-news.xml (404). ClaudeBot read llms.txt and
        AGENTS.md — both of which name /sitemap-ai.xml — then fetched
        /sitemap.xml instead, twice.
      · External fetches of /sitemap-ai.xml over a full day, two crawlers: ZERO.
      · robots.txt cannot scope it: `Sitemap:` is a NON-GROUP directive in
        RFC 9309.

    ★ It also caught itself being vacuous: the old assertion read
    _sitemap_shard_files(), and the shards are appended in the REBUILD instead,
    so it stayed green while the index started carrying them. A guard that
    passes for a reason unrelated to its subject is worse than no guard.
    """
    body = _fn("_rebuild_sitemap_snapshot")
    assert "ai_shard_keys]" in body and "shard_files + " in body, (
        "the AI shards are no longer appended to the submitted index — the "
        "family goes back to being published and unreachable")
    assert "rows = [r for r in rows if r[0] != 'index']" in body, (
        "the earlier index row is not removed, so the snapshot would carry TWO "
        "'index' rows and which one serves is undefined")


def test_the_capacity_GATE_itself_is_untouched():
    """★ THE LOAD-BEARING ASSERTION NOW. Adding a second family beside the
    gated one is not the same as widening the first. sitemap-facilities-N must
    still come from the CAPACITY-GATED set — if this change ever leaks the
    ungated list into it, Google and Bing get the thin pages in the family they
    already index, which is the thing the 2026-08-14 evidence actually argued
    against."""
    body = _fn("_rebuild_sitemap_snapshot")
    # ★ Bounded by the NEXT construct, not by a character count. A 700-char
    # window ran straight into the AI block and failed on its `ai_fac` — the
    # fourth fixed-slice failure in two days. The gated shard construction ends
    # where the index row is first written.
    i = body.index("fac = sections.get('facilities')")
    seg = body[i:body.index("rows.append(('index'", i)]
    assert "_build_sitemap_facilities_ungated" not in seg, (
        "the GATED facility shards are being built from the ungated list — the "
        "capacity gate is void, not merely bypassed for a second family")
    assert "ai_fac" not in seg, (
        "the ungated list leaked into the gated shard construction")


def test_a_failed_ai_build_leaves_a_VALID_index():
    """If the ungated build fails, the index must still be the gated one — not
    an index naming shards that were never written."""
    body = _fn("_rebuild_sitemap_snapshot")
    # the append happens only inside the success branch
    i = body.index("shard_files + ")
    j = body.index("except Exception as _ai_e")
    assert i < j, "the index rewrite sits outside the guarded block"
    assert "ai_shard_keys = []" in body, (
        "no empty default — a failed build would reference an unbound name")


def test_ai_sections_never_reach_the_live_builder():
    """The AI branch must return BEFORE _sitemap_sections(), the live union."""
    body = _fn("serve_sitemap_shard")
    assert "_SITEMAP_AI_INDEX_KEY" in body, (
        "serve_sitemap_shard has no AI branch — an AI section would fall "
        "through to the live builder or to a 404")
    ai_at = body.index("_SITEMAP_AI_INDEX_KEY")
    live_at = body.index("_sitemap_sections()")
    assert ai_at < live_at, (
        "the AI branch sits AFTER the live _sitemap_sections() call, so a "
        "snapshot miss rebuilds the ~20k-row union per request — the 07-20 "
        "Neon stampede, on the surface crawlers hit hardest")


def test_a_snapshot_miss_is_503_not_404():
    """A 404 tells a crawler the URL does not exist and it may stop asking. A
    failed rebuild would then silently retire the whole surface."""
    body = _fn("serve_sitemap_shard")
    seg = body[body.index("_SITEMAP_AI_INDEX_KEY"):]
    seg = seg[:seg.index("sections, hit = _sitemap_sections()")]
    assert "503" in seg, "an AI snapshot miss must be 503, not 404"
    assert "Retry-After" in seg, (
        "503 without Retry-After gives a crawler no schedule to come back on")
    assert "404" not in seg, (
        "the AI branch returns a 404 somewhere — that is the response that "
        "makes a crawler forget the URL")


# ── 3. The superset guard ───────────────────────────────────────────────────

def test_the_rebuild_refuses_an_ungated_set_smaller_than_the_gated_one():
    """The ungated set is the gated query minus one AND clause, so it is a
    strict superset by construction. Smaller means the build is broken, and
    publishing it would RETIRE URLs from the AI crawlers rather than add any —
    the same 'shrinking looks like success' failure the thin-gate floor
    guards."""
    body = _fn("_rebuild_sitemap_snapshot")
    assert "len(ai_fac) < len(fac)" in body, (
        "no superset check — a broken ungated build would publish a SHORTER "
        "AI sitemap and read as a successful rebuild")
    assert "ai_shard_keys = []" in body, (
        "the refusal path must leave the AI shards unpublished for this "
        "generation, not fall through and emit the short list")


def test_an_ai_build_failure_does_not_take_the_gated_sitemap_down():
    """The gated artefact is the one GSC and Bing read. A failure in the
    additive family must degrade to 'no AI shards this generation'."""
    body = _fn("_rebuild_sitemap_snapshot")
    i = body.index("ai_shard_keys = []")
    j = body.index("conn = None", i)
    seg = body[i:j]
    assert "try:" in seg and "except Exception" in seg, (
        "the ungated build is not wrapped — a failure there would abort the "
        "whole snapshot rebuild and take the gated sitemap with it")


# ── 4. Sharding arithmetic + prefix collision ───────────────────────────────

def test_ai_shard_keys_chunk_at_the_shared_shard_size():
    ns = _consts()
    keys, per = ns["_ai_shard_keys"], ns["_SITEMAP_FACILITIES_PER_SHARD"]
    assert keys(0) == ["ai-facilities-1"], (
        "an empty set must still name one shard, or the index is empty and "
        "the URL 503s forever")
    assert keys(1) == ["ai-facilities-1"]
    assert keys(per) == ["ai-facilities-1"]
    assert keys(per + 1) == ["ai-facilities-1", "ai-facilities-2"]
    # 20,488 is the measured publishable universe on 2026-09-05.
    assert len(keys(20488)) == 3


def test_the_ai_prefix_cannot_be_read_as_a_gated_shard():
    """serve_sitemap_shard matches gated shards with ^facilities-(\\d{1,3})$.
    'ai-facilities-1' must not satisfy it, or an AI URL would be served the
    GATED slice under an AI name."""
    gated = re.compile(r"^facilities-(\d{1,3})$")
    ns = _consts()
    for key in ns["_ai_shard_keys"](20488):
        assert not gated.match(key), (
            "%r matches the gated shard pattern — it would be served the "
            "capacity-gated slice" % key)
    assert gated.match("facilities-1"), "the gated pattern itself stopped working"
