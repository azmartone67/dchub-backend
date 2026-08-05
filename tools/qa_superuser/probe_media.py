#!/usr/bin/env python3
"""Surface 4 — what DC Hub Media actually published, read from the public feed.

Media is the one loop that speaks in the platform's own voice, which makes its
failure modes reputational rather than merely technical. Two have shipped before:
a quality gate that reported green while a FALSE claim went out, and a diversity
collapse where the same story was republished because lead SUPPLY had dried up
and nothing noticed the repetition.

This probe reads the published feed rather than the queue. What was drafted, what
was approved and what a reader can actually see are three different sets, and only
the last one is the product.
"""
from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET

from . import config as C
from .finding import (GAUGE, INFO, MAJOR, MINOR, PASS, RED, SEAT_NONE, Finding,
                      blind, stable_key)
from .http import Unreachable, fetch
from .probe_data import FUTURE_TOLERANCE_H, NOW, parse_when

FEED_PATH = "/press/feed.xml"


def _items(root: ET.Element) -> list[dict]:
    """Extract items from RSS or Atom without caring which we were handed."""
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        rec = {}
        for child in it:
            ctag = child.tag.split("}")[-1].lower()
            if ctag in ("title", "pubdate", "published", "updated", "link", "guid"):
                rec[ctag] = (child.text or "").strip() or (
                    child.attrib.get("href", "").strip())
        out.append(rec)
    return out


def probe(findings: list[Finding]) -> None:
    url = f"{C.EDGE}{FEED_PATH}"
    try:
        status, _h, body = fetch(url, timeout=C.HTTP_TIMEOUT)
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("media", "feed"), surface="media", seat=SEAT_NONE,
            title="Media feed unobserved", why=str(e), basis=f"GET {url}"))
        return

    if status >= 400:
        findings.append(Finding(
            key=stable_key("media", "feed"), surface="media", seat=SEAT_NONE,
            title=f"Media feed returns HTTP {status}",
            verdict=RED, severity=MAJOR, value=status,
            evidence=f"GET {url} -> {status}, {len(body)}b",
            basis=f"anonymous GET {url}",
            red_when="the public media feed returns >=400 — everything the media "
                     "loop publishes is unreachable to readers and aggregators"))
        return

    try:
        root = ET.fromstring(body)
    except Exception as e:  # noqa: BLE001
        findings.append(Finding(
            key=stable_key("media", "feed"), surface="media", seat=SEAT_NONE,
            title="Media feed is not valid XML",
            verdict=RED, severity=MAJOR,
            evidence=f"{len(body)}b at {url} failed to parse: {type(e).__name__}: {e}",
            basis=f"anonymous GET {url}, parsed with ElementTree",
            red_when="the feed body does not parse as XML — every reader and "
                     "aggregator sees an error, not our stories"))
        return

    items = _items(root)
    if not items:
        findings.append(Finding(
            key=stable_key("media", "feed"), surface="media", seat=SEAT_NONE,
            title="Media feed parses but contains no items",
            verdict=RED, severity=MAJOR, value=0,
            evidence=f"{len(body)}b of valid XML at {url} with zero item/entry nodes",
            basis=f"anonymous GET {url}, RSS item / Atom entry nodes",
            red_when="the feed is well-formed but empty — publishing looks healthy "
                     "and reaches nobody"))
        return

    findings.append(Finding(
        key=stable_key("media", "feed"), surface="media", seat=SEAT_NONE,
        title=f"Media feed serves {len(items)} item(s)",
        verdict=PASS, severity=INFO, value=len(items),
        evidence=f"{len(items)} item(s), {len(body)}b of valid XML",
        basis=f"anonymous GET {url}",
        red_when="the feed 4xx/5xxs, fails to parse, or carries zero items"))

    _check_freshness(items, findings)
    _check_repetition(items, findings)
    _check_links_resolve(items, findings)


# How many item links to actually fetch. The feed carries 30; resolving all of
# them costs 30 round trips on every 4h run for a signal the first handful
# already gives. A scheme-wide break (which is the failure this exists to catch)
# shows up in the first item.
LINK_SAMPLE = 8


def _check_links_resolve(items: list[dict], findings: list[Finding]) -> None:
    """Do the published links actually go anywhere?

    ★ THE GAP THIS CLOSES. On 2026-08-05 every one of the 30 links in this feed
    returned 404 — the platform advertised /press/<slug> while the edge served
    /press-release/<slug> — and this probe was GREEN throughout, because it
    counted items, compared titles and checked dates and never once FOLLOWED a
    link. sitemap-press.xml published the correct form the whole time, so the
    feed and the sitemap disagreed about the site's own URL scheme and nothing
    compared them.

    Media is the platform's citation channel. A feed that parses, is fresh, is
    non-repetitive and leads nowhere is worse than a feed that is down: it looks
    healthy on every instrument while every reader, aggregator and AI crawler
    that follows it lands on an error.

    ★ No threshold is invented — >=400 is decidable, and the platform's own
    sitemap already asserts which shape should resolve.
    """
    links, seen = [], set()
    for it in items:
        u = (it.get("link") or it.get("guid") or "").strip()
        if u.startswith("http") and u not in seen:
            seen.add(u)
            links.append(u)
    if not links:
        findings.append(blind(
            key=stable_key("media", "item-links"), surface="media", seat=SEAT_NONE,
            title="Media item links unobserved",
            why="no absolute <link>/<guid> on any feed item",
            basis="RSS link / guid, Atom link@href"))
        return

    sample = links[:LINK_SAMPLE]
    dead, unobserved = [], 0
    for u in sample:
        try:
            st, _h, _b = fetch(u, timeout=C.HTTP_TIMEOUT)
        except Unreachable:
            # Transport failure is BLIND for that URL, never a dead link.
            unobserved += 1
            continue
        if st >= 400:
            dead.append((u, st))

    checked = len(sample) - unobserved
    if checked == 0:
        findings.append(blind(
            key=stable_key("media", "item-links"), surface="media", seat=SEAT_NONE,
            title="Media item links unobserved",
            why=f"all {len(sample)} sampled links were unreachable at the transport "
                "layer — cannot distinguish a dead link from a dead network",
            basis=f"anonymous GET of {len(sample)} item link(s)"))
        return

    if dead:
        findings.append(Finding(
            key=stable_key("media", "item-links"), surface="media", seat=SEAT_NONE,
            title=f"{len(dead)} of {checked} published story link(s) are dead",
            verdict=RED, severity=MAJOR, value=len(dead),
            evidence="; ".join(f"{u} -> HTTP {s}" for u, s in dead[:4])
                     + (f" (+{len(dead) - 4} more)" if len(dead) > 4 else "")
                     + f"; {len(links)} link(s) in feed, {checked} checked",
            basis=f"anonymous GET of the first {len(sample)} item <link>/<guid> "
                  f"URLs from {C.EDGE}{FEED_PATH}, following redirects",
            red_when="a URL this platform published in its own feed returns >=400 "
                     "to an anonymous reader",
            remedy="Compare the feed's URL shape against sitemap-press.xml — when "
                   "they disagree the sitemap has been right. Fix the emitter AND "
                   "301 the shape already in the wild; links are cited by people "
                   "and crawlers we cannot re-notify."))
        return

    findings.append(Finding(
        key=stable_key("media", "item-links"), surface="media", seat=SEAT_NONE,
        title=f"All {checked} sampled story link(s) resolve",
        verdict=PASS, severity=INFO, value=checked,
        evidence=f"{checked} of {len(links)} feed link(s) checked, all <400; "
                 f"first: {sample[0]}",
        basis=f"anonymous GET of the first {len(sample)} item link(s)",
        red_when="a URL this platform published in its own feed returns >=400"))


def _check_freshness(items: list[dict], findings: list[Finding]) -> None:
    """How old is the newest published story, and is anything dated ahead?"""
    dated = []
    for it in items:
        raw = it.get("pubdate") or it.get("published") or it.get("updated")
        when = parse_when(raw) if raw else None
        if when:
            dated.append((when, it.get("title", "")[:80], raw))
    if not dated:
        findings.append(blind(
            key=stable_key("media", "freshness"), surface="media", seat=SEAT_NONE,
            title="Media freshness unobserved",
            why="no parseable publication date on any feed item",
            basis="RSS pubDate / Atom published|updated"))
        return

    dated.sort(reverse=True)
    newest, title, raw = dated[0]
    age_d = (NOW - newest).total_seconds() / 86400.0
    future = [d for d in dated
              if d[0] > NOW + datetime.timedelta(hours=FUTURE_TOLERANCE_H)]

    if future:
        findings.append(Finding(
            key=stable_key("media", "future-dated"), surface="media", seat=SEAT_NONE,
            title=f"{len(future)} published story/stories are dated in the future",
            verdict=RED, severity=MAJOR, value=len(future),
            evidence="; ".join(f"{t!r} @ {r}" for _w, t, r in future[:4]),
            basis=f"{FEED_PATH} item dates, tolerance {FUTURE_TOLERANCE_H}h",
            red_when="a published story carries a future publication date — it "
                     "pins the feed's freshness forever and sorts above real news",
            remedy="Same class as the events row poisoning MAX(published_at) in "
                   "the news feed: guard the date at the producer."))

    # Age is a GAUGE: the media loop's cadence is a strategy choice, not a
    # contract, and inventing a "too old" threshold would manufacture a fault.
    findings.append(Finding(
        key=stable_key("media", "freshness"), surface="media", seat=SEAT_NONE,
        title=f"Newest published story is {age_d:.1f} days old",
        verdict=GAUGE, severity=INFO, value=round(age_d, 2),
        evidence=f"newest: {title!r} @ {raw}; {len(dated)} dated item(s) in the feed",
        basis=f"{FEED_PATH}, newest parseable item date",
        red_when="n/a — GAUGE: publication cadence is a strategy choice with no "
                 "platform-defined SLA, so a number is honest and a threshold "
                 "would be invented"))


def _check_repetition(items: list[dict], findings: list[Finding]) -> None:
    """Is the media loop republishing itself?

    An exact duplicate title among published stories is decidable and always
    wrong, so it is RED. The broader "how similar are these" question is a GAUGE:
    a run of related stories during a real news cycle is good journalism, and the
    known root cause of past repetition was lead SUPPLY drying up rather than the
    generator misbehaving — a distinction a similarity threshold cannot make.
    """
    titles = [(it.get("title") or "").strip() for it in items]
    titles = [t for t in titles if t]
    if len(titles) < 2:
        return

    seen: dict[str, int] = {}
    for t in titles:
        seen[t.lower()] = seen.get(t.lower(), 0) + 1
    dupes = {t: n for t, n in seen.items() if n > 1}
    unique_ratio = len(seen) / len(titles) * 100.0

    findings.append(Finding(
        key=stable_key("media", "duplicate-titles"), surface="media", seat=SEAT_NONE,
        title=(f"{len(dupes)} story title(s) published more than once"
               if dupes else "No duplicate story titles in the feed"),
        verdict=RED if dupes else PASS,
        severity=MAJOR if dupes else INFO, value=len(dupes),
        evidence=("; ".join(f"{t!r} x{n}" for t, n in list(dupes.items())[:4])
                  if dupes else
                  f"{len(titles)} item(s), {len(seen)} distinct titles "
                  f"({unique_ratio:.0f}% unique)"),
        basis=f"{FEED_PATH}, case-insensitive exact match on item titles",
        red_when="the same title appears more than once in the published feed — "
                 "verbatim republication, which is decidable and always a defect",
        remedy="Past repetition traced to lead SUPPLY running dry rather than the "
               "generator misfiring — check inbound lead volume before tuning "
               "the writer."))
