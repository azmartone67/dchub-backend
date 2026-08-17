"""utils/story_debt.py — SHIP-TO-STORY detector (pure stdlib, import-safe).

One question, answered deterministically: which products does the platform
itself flag as NEW that have no published card on /whats-new#platform?

The "what shipped" source is the frontend nav (js/dchub-nav.js, served
publicly by the edge): every entry carrying ``badge: 'NEW'`` is the platform's
OWN declaration of a fresh product — no invented registry, no curated list to
rot. The "what was told" source is data/platform_updates.json (the PR-approved
store behind /api/v1/platform-updates). An item is COVERED when a published
card's link points at the same path.

Path-grain on purpose: the three Land & Power views share
/land-power-map?view=table|chart|news — one card announcing the view switcher
covers all three, which is the editorially correct unit. Matching on full
querystrings would demand three near-identical cards.

Shared by BOTH consumers so their answers cannot drift:
  · routes/story_debt_master_shell.py  (the measuring lane)
  · tools/story_debt_author.py         (the CI author that stages draft cards)

House rules honored here:
  · a parse that finds ZERO nav entries is an instrument failure, never
    "no debt" — ship_vs_story_verdict() returns BLIND for it (the empty-parse
    =PASS trap; see the AST-guard lesson in the CI collection notes).
  · skeleton entries are staged with status="draft", which the store's
    _is_published gate withholds even if the PR merges — automerge can never
    publish copy nobody approved. tests/test_story_debt.py binds that contract
    against the REAL gate function, AST-extracted.
"""
from __future__ import annotations

import re

# Pages the nav may flag NEW that are not PRODUCT stories for the platform
# section. /whats-new is the page these cards render ON — a card about the
# page itself would be self-referential filler, so it can never be "debt".
EXCLUDE_PATHS = frozenset({"/whats-new"})

# One nav object literal. The nav is hand-maintained JS with single-quoted
# values; tolerate double quotes defensively. Non-greedy, no nesting — nav
# entries are flat one-line literals.
_NAV_OBJ = re.compile(r"\{[^{}]*?badge:\s*['\"]NEW['\"][^{}]*?\}", re.S)
_FIELD = re.compile(r"(label|href|desc):\s*(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")")


def parse_nav_new_items(js_text):
    """Extract [{'label','href','desc'}] for every nav entry with badge NEW.

    Returns [] for junk/empty input — callers MUST route an empty parse
    through ship_vs_story_verdict(), which files it as BLIND, never PASS.
    """
    items = []
    for m in _NAV_OBJ.finditer(str(js_text or "")):
        fields = {}
        for f in _FIELD.finditer(m.group(0)):
            fields[f.group(1)] = (f.group(2) if f.group(2) is not None else f.group(3)) or ""
        href = fields.get("href", "").strip()
        label = fields.get("label", "").strip()
        if href and label:
            items.append({"label": label, "href": href, "desc": fields.get("desc", "").strip()})
    return items


def href_path(href):
    """The path portion of an href — no query, no fragment, no origin."""
    h = str(href or "").strip()
    if not h:
        return ""
    h = re.sub(r"^https?://[^/]+", "", h)
    h = h.split("#", 1)[0].split("?", 1)[0]
    return h or ""


def published_link_paths(updates):
    """Paths a PUBLISHED card's link covers. Drafts cover nothing — a staged
    skeleton must keep its item IN debt until a human publishes real copy."""
    out = set()
    for e in updates or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("status") or "").strip().lower() != "published":
            continue
        p = href_path((e.get("link") or {}).get("href") if isinstance(e.get("link"), dict) else "")
        if p:
            out.add(p)
    return out


def compute_debt(new_items, updates):
    """NEW-badged nav items with no published card at their path.

    Deduped by path (the L&P views collapse to one entry, labels joined) and
    sorted by path for deterministic output — the author stages from this, so
    order stability keeps its branch diffs clean.
    """
    covered = published_link_paths(updates)
    by_path = {}
    for it in new_items or []:
        p = href_path(it.get("href"))
        if not p or p in EXCLUDE_PATHS or p in covered:
            continue
        slot = by_path.setdefault(p, {"path": p, "labels": [], "hrefs": [], "descs": []})
        if it.get("label") and it["label"] not in slot["labels"]:
            slot["labels"].append(it["label"])
        if it.get("href") and it["href"] not in slot["hrefs"]:
            slot["hrefs"].append(it["href"])
        if it.get("desc") and it["desc"] not in slot["descs"]:
            slot["descs"].append(it["desc"])
    return [by_path[p] for p in sorted(by_path)]


def ship_vs_story_verdict(parsed_count, debt):
    """(verdict, note). BLIND when the nav parse produced nothing — an
    unobserved surface is never evidence of zero debt."""
    if not parsed_count:
        return ("BLIND", "nav parse produced 0 entries — instrument failure, "
                         "not an empty backlog; nothing is asserted")
    if debt:
        return ("RED", "%d NEW-badged product(s) have no published story card" % len(debt))
    return ("PASS", "every NEW-badged nav item is covered by a published card")


def _slug(path):
    s = re.sub(r"[^a-z0-9]+", "-", str(path or "").lower()).strip("-")
    return s or "item"


def skeleton_entry(item, today_iso):
    """A staged draft for one debt item. status='draft' is the whole safety
    story: the store's gate withholds anything that is not the literal
    'published', so merging the author's PR publishes NOTHING by itself."""
    labels = " / ".join(item.get("labels") or []) or item.get("path", "")
    descs = [d for d in (item.get("descs") or []) if d]
    href = (item.get("hrefs") or [item.get("path", "")])[0]
    return {
        "id": "draft-%s" % _slug(item.get("path")),
        "status": "draft",
        "announced": str(today_iso),
        "tag": "Draft",
        "title": "[DRAFT] %s" % labels,
        "body": ("TODO — write the story (and mind the no-figures gate: no "
                 "number next to its noun; bind counts via a metric token "
                 "instead). What the nav says about it: %s" %
                 ("; ".join(descs) if descs else "(no nav description)")),
        "link": {"href": href, "label": "Open →"},
    }
