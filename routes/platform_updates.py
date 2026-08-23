"""Brain-picked platform announcements for /whats-new — PR-approved, file-backed.

WHY A FILE AND NOT A TABLE
--------------------------
The requirement is "brain picks them, I approve them in PR". A DB store would
need its own approval UI and — worse — could publish the instant a row landed.
The store here is ``data/platform_updates.json``, committed to this repo: the
brain opens a PR that adds an entry and MERGING THAT PR IS THE APPROVAL. There
is no write endpoint, no admin toggle, and no cron that mutates the file.

★ DEFAULT-INVISIBLE. ``_is_published`` demands the literal status "published".
A draft, a typo'd status, or no status at all is WITHHELD. A new entry can never
become visible by accident, which is the whole point of "nothing auto-publishes".

★ THE STORE CARRIES NO NUMBERS.
This module exists because the six cards it replaces were hardcoded HTML that
went stale in public: they said "36 grids" while the scoreboard ranked 46, and
"73 tools" / "tool #73" while the live catalogue was at 81. So a card body may
not contain a figure at all — ``_looks_like_bare_figure`` rejects one at load
time. A card that wants to show a number declares a metric TOKEN instead, and
the client binds the value from the canonical live source
(``/api/v1/canon/phrases``) at render time. A number that is never stored can
never go stale.

★ UNMEASURED IS A FIRST-CLASS ANSWER. A token with no live source —
``grid_zones_ranked``; DC Hub publishes no keyless grid-zone count, the REST
scoreboard is plan-gated — resolves to ``value: null`` plus a ``reason``. Never
0, never the literal that used to sit in the card.

★ PURE FILE READ. No DB, no egress, no self-request. This is spliced into the
public ``/api/v1/whats-new`` feed, so it must never be able to 500 that route or
blank the page: every failure path returns an empty card list plus a reason, and
the caller renders an empty state.

Endpoints:
  GET /api/v1/platform-updates   the published cards + what was withheld and why
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

platform_updates_bp = Blueprint("platform_updates", __name__)

# data/platform_updates.json, resolved off this file (routes/ → repo root).
STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "platform_updates.json")

# Where a client reads the live value for a metric token. ONE canonical source,
# the same one the frontend heal and the registry manifests pull from.
METRIC_SOURCE_URL = "/api/v1/canon/phrases"

# ★ WHERE A WITHHELD ITEM IS ACTUALLY DECIDED (2026-08-23, shell #65 lane 2).
# There is no approve endpoint here BY DESIGN — a card ships only when a merged
# PR sets its "status": "published". So the one-click decision URL is GitHub's
# editor on the store file: opening it starts the branch-and-PR that IS the
# approval. The feed used to publish {id, reason} and nothing else, which left a
# human able to see THAT something was withheld but not how long it had waited
# or where to go about it.
DECISION_URL = (os.environ.get("DCHUB_PLATFORM_DECISION_URL")
                or "https://github.com/azmartone67/dchub-backend/edit/main/"
                   "data/platform_updates.json")

# Tokens with a live, keyless, canonical source, mapped to the BASIS we publish
# alongside the number. House rule: never a figure without its basis.
#
# ★ `grid_zones_ranked` is deliberately ABSENT. The ranked-zone count only comes
#   back from the get_grid_scoreboard MCP tool; the REST /api/v1/grid/scoreboard
#   is plan-gated (anonymous GET returns error="plan_required", measured
#   2026-07-29), and the in-process EU zone list is the CONFIGURED count, which
#   is not the returned count. So there is nothing honest to bind, and the card
#   renders no grid figure at all rather than the "36" that sat stale for weeks.
METRIC_TOKENS = {
    "tools": "live MCP tools/list count, published at /api/v1/canon/phrases as `tools`",
    "facilities": ("COUNT(DISTINCT canonical_slug) over discovered_facilities, floored DOWN; "
                   "published at /api/v1/canon/phrases as `facilities`"),
    "markets": "DCPI market count, floored DOWN; published at /api/v1/canon/phrases as `markets`",
    "deals": "deduped M&A deal count, floored DOWN; published at /api/v1/canon/phrases as `deals`",
    "countries": "countries covered, floored DOWN; published at /api/v1/canon/phrases as `countries`",
}

# Flood cap: a runaway brain PR must not be able to turn the page into a wall.
# ★ A FLOOD cap, not an editorial window — raised 12→16 on 2026-08-17 when a
#   legitimate publication wave (nine cards + two) met the cap exactly and the
#   alternative was silently truncating real, approved stories. Keep it above
#   the size of any honest wave; the gate + merge-approval are the editorial
#   controls, this is only the runaway-bot backstop.
MAX_CARDS = 16

_TTL = 300.0
_cache: dict = {"ts": 0.0, "block": None}


def _is_published(entry) -> bool:
    """★ THE APPROVAL GATE. True only for the literal status "published".

    Default-invisible on purpose: a missing status, a null status, a "draft", a
    "ready", a typo — all False. Nothing reaches the public page except by a
    human merging the PR that set this field. NEVER raises.
    """
    try:
        if not isinstance(entry, dict):
            return False
        return str(entry.get("status") or "").strip().lower() == "published"
    except Exception:
        return False


def _looks_like_bare_figure(text) -> bool:
    """True when prose carries a hardcoded count — the exact staleness class
    this module replaces ("ranks 36 grids", "All 73 tools", "4,922 verified
    inside a 21,957 tracked frontier").

    The regex is compiled INSIDE the function on purpose: the unit test
    AST-extracts these helpers and executes them standalone, so a module-level
    pattern would be an unresolved free variable. NEVER raises.
    """
    import re
    try:
        # ★ A figure and its noun appear in BOTH orders, and not always
        # adjacently. The first version required "<number> <noun>" with nothing
        # between, so it caught "All 73 tools" and sailed straight past both
        # "73 live tools" (one adjective) and "tool #73" (reversed). A guard
        # with a too-narrow pattern is worse than none: it reports green while
        # the literal ships, which is exactly how "36 grids" and "tool #73"
        # stayed on the live page for weeks.
        #
        # Three branches, deliberately: number-then-noun with up to two
        # intervening words; noun#number; noun-then-number. The intervening
        # words are purely alphabetic and capped at two so identifiers like
        # "SMF-28 fibre floors" and "EIA-860M" still cannot trip it, and so a
        # number far away in the same sentence is not dragged in.
        # ★ The noun list is the fence's blind spot, not the regex shape. The
        # 2026-07-29 cards talk about SUBSTATIONS, FEEDS and SOURCES, and none
        # of those were listed — "cross-layer search across 126,840
        # substations" sailed straight through a guard that catches "36 grids".
        # Caught by a must-fail control in tests/test_platform_updates_gate.py,
        # not by review. Add the noun BEFORE writing a card that uses it.
        _n = (r"tools?|grids?|zones?|markets?|countries|countr\w*|"
              r"facilit\w*|data[\s-]?cent\w+|records?|deals?|verified|tracked|"
              r"substations?|feeds?|sources?|sites?|assets?|plants?|routes?|"
              r"entities|entit\w*")
        pat = re.compile(
            r"\b\d[\d,]*\+?\s*(?:[A-Za-z]+[\s-]+){0,2}(?:" + _n + r")\b"
            r"|\b(?:" + _n + r")\s*#\s*\d"
            r"|\b(?:" + _n + r")\s+\d[\d,]*\+?\b",
            re.I)
        return bool(pat.search(str(text or "")))
    except Exception:
        # Fail CLOSED: if the check itself breaks we withhold the card rather
        # than publish prose we could not inspect.
        return True


def _metric_spec(spec, allowed):
    """Normalise a declared metric into the shape the client binds a value into.

    The returned dict ALWAYS carries ``value: None`` — the store never holds a
    number. A known token gets a ``basis`` and a ``source_url`` so the client
    can fetch the live value; an unknown token gets ``value: None`` plus an
    ``unmeasured_reason`` and the card simply renders no figure.

    ★ Never 0. UNMEASURED emits null and a reason. NEVER raises.
    """
    try:
        if not isinstance(spec, dict):
            return None
        token = str(spec.get("token") or "").strip()
        label = str(spec.get("label") or token or "").strip()
        if not token:
            return {"token": None, "label": label, "value": None, "basis": None,
                    "source_url": None,
                    "unmeasured_reason": "UNMEASURED: metric declared with no token"}
        basis = (allowed or {}).get(token)
        if not basis:
            return {"token": token, "label": label, "value": None, "basis": None,
                    "source_url": None,
                    "unmeasured_reason": (
                        "UNMEASURED: no keyless live source publishes `%s`, so no "
                        "figure is shown (a stale literal is not an acceptable "
                        "substitute)" % token)}
        return {"token": token, "label": label, "value": None, "basis": basis,
                "source_url": METRIC_SOURCE_URL, "unmeasured_reason": None}
    except Exception:
        return None


def _card(entry, allowed):
    """(card, reject_reason). Returns (None, reason) for anything withheld.

    Order matters: the approval gate runs FIRST, so an unapproved entry is never
    even inspected for content. NEVER raises.
    """
    try:
        if not _is_published(entry):
            # ★ Name the ACTUAL status. "archived" is a decision already taken
            # (published, then deliberately retired); collapsing it into "not
            # approved" reported retired cards as an owner's outstanding queue.
            st = str((entry or {}).get("status") or "").strip().lower()
            if st == "archived":
                return None, ("retired (status is \"archived\") — previously "
                              "published, then deliberately archived; this is a "
                              "decision already taken, not one awaiting an owner")
            return None, "not approved (status is not \"published\")"
        cid = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("body") or "").strip()
        if not cid or not title or not body:
            return None, "incomplete entry (id, title and body are all required)"
        if _looks_like_bare_figure(title) or _looks_like_bare_figure(body):
            return None, ("hardcoded figure in prose — declare a metric token "
                          "instead so the number is bound live")
        link = entry.get("link") if isinstance(entry.get("link"), dict) else {}
        card = {
            "id": cid,
            "tag": str(entry.get("tag") or "").strip() or None,
            "title": title,
            "body": body,
            "code": str(entry.get("code") or "").strip() or None,
            "announced": str(entry.get("announced") or "").strip() or None,
            "link_href": str(link.get("href") or "").strip() or None,
            "link_label": str(link.get("label") or "").strip() or None,
            "metric": _metric_spec(entry.get("metric"), allowed),
        }
        return card, None
    except Exception as e:
        return None, "entry unreadable (%s)" % type(e).__name__


def _read_store(path):
    """Parse the JSON store. Returns (updates_list, error_or_None). NEVER raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            return [], "store is not a JSON object"
        ups = doc.get("updates")
        if not isinstance(ups, list):
            return [], "store has no `updates` array"
        return ups, None
    except FileNotFoundError:
        return [], "store file not found"
    except Exception as e:
        return [], "store unreadable (%s: %s)" % (type(e).__name__, str(e)[:80])


def _age_days(announced):
    """Whole days since an ISO `announced` date, or None when absent/unparseable.

    None is a real answer and must stay one: a withheld entry with no date
    publishes age_days: null, and shell #65 reads that as "carries no age" —
    which is the finding. Inventing 0 here would turn a missing date into a
    brand-new item. NEVER raises.
    """
    try:
        s = str(announced or "").strip()[:10]
        if not s:
            return None
        d = _dt.date.fromisoformat(s)
        return max(0, (_dt.date.today() - d).days)
    except Exception:
        return None


def _withheld_entry(entry, why: str) -> dict:
    """One withheld item, carrying an AGE and a DECISION URL.

    ★ `awaiting_decision` separates the two states the old feed conflated. Every
    non-published entry produced the single reason 'not approved', so nine cards
    that had been deliberately ARCHIVED on 2026-08-17 (PR #2804, "archive
    pre-August wave") were reported as nine items pending an owner's decision.
    Retired is a decision already taken; pending is one still owed. NEVER raises.
    """
    try:
        e = entry if isinstance(entry, dict) else {}
        status = str(e.get("status") or "").strip().lower() or None
        announced = str(e.get("announced") or "").strip() or None
        age_d = _age_days(announced)
        return {
            "id": e.get("id"),
            "reason": why,
            "status": status,
            "awaiting_decision": status != "archived",
            "announced": announced,
            "age_days": age_d,
            "age_hours": (age_d * 24 if age_d is not None else None),
            "decision_url": DECISION_URL,
        }
    except Exception:
        return {"id": None, "reason": why, "status": None,
                "awaiting_decision": True, "announced": None,
                "age_days": None, "age_hours": None,
                "decision_url": DECISION_URL}


def published_updates(force: bool = False) -> dict:
    """The block spliced into /api/v1/whats-new and served at
    /api/v1/platform-updates. Fail-soft by construction: on ANY failure it
    returns an empty `cards` list plus a reason, so the page shows its empty
    state instead of stale cards. NEVER raises."""
    try:
        now = time.time()
        if not force and _cache.get("block") and (now - _cache.get("ts", 0.0)) < _TTL:
            return _cache["block"]
        ups, err = _read_store(STORE_PATH)
        cards, withheld = [], []
        for e in ups:
            card, why = _card(e, METRIC_TOKENS)
            if card:
                cards.append(card)
            else:
                withheld.append(_withheld_entry(e, why))
        truncated = max(0, len(cards) - MAX_CARDS)
        cards = cards[:MAX_CARDS]
        block = {
            "ok": err is None,
            "cards": cards,
            "count": len(cards),
            "withheld_count": len(withheld),
            "awaiting_decision_count": sum(1 for w in withheld
                                           if w.get("awaiting_decision")),
            "retired_count": sum(1 for w in withheld
                                 if not w.get("awaiting_decision")),
            "withheld": withheld,
            "decision_url": DECISION_URL,
            "truncated": truncated,
            "reason": err,
            "source": "data/platform_updates.json",
            "approval": ("every card reached this page by a merged pull request; "
                         "nothing auto-publishes and there is no write endpoint"),
            "metric_source_url": METRIC_SOURCE_URL,
            "metric_contract": ("cards never store a figure — a card with a metric "
                                "carries a token whose value the client reads live "
                                "from metric_source_url; an unmeasured token stays "
                                "null with a reason, never 0"),
        }
        _cache["block"] = block
        _cache["ts"] = now
        return block
    except Exception as e:      # belt and braces — this feeds a public route
        logger.warning("platform_updates: %s", str(e)[:160])
        return {"ok": False, "cards": [], "count": 0, "withheld_count": 0,
                "awaiting_decision_count": 0, "retired_count": 0,
                "withheld": [], "truncated": 0, "decision_url": DECISION_URL,
                "reason": "platform updates unavailable (%s)" % type(e).__name__,
                "source": "data/platform_updates.json",
                "metric_source_url": METRIC_SOURCE_URL}


def resolve_card_metrics(block, canon):
    """Bind live metric VALUES into a COPY of `block`'s cards, server-side.

    Deliberately NOT inside published_updates(): that function's contract is a
    pure file read (no DB, no egress, no self-request), and this needs a canon
    map. So the caller resolves canon once and hands it in; this does dict
    arithmetic only. NEVER raises.

    ★ Copies before writing. published_updates() returns the cached block, so
    mutating a card in place would poison every later request with one
    request's numbers.

    WHY BIND SERVER-SIDE WHEN THE CLIENT ALSO BINDS: /api/v1/whats-new is read
    by MCP agents, not only by the page. A card whose value stays null in the
    JSON is a number an agent cannot see. Both sides bind the SAME token from
    the SAME canonical source, so they cannot disagree — and a token with no
    live value still resolves to null plus a reason, never 0.
    """
    try:
        cards = []
        for card in (block.get("cards") or []):
            card = dict(card)
            spec = card.get("metric")
            if isinstance(spec, dict) and spec.get("token"):
                spec = dict(spec)
                value = (canon or {}).get(spec.get("token"))
                # ★ canon publishes FLOORED PHRASE STRINGS, not raw ints —
                # markets is "300+", facilities is "15,500+"; only `tools` is a
                # bare int. An int-only guard here rejected the published form
                # and then stamped "no live value published", which is false:
                # a value IS published, in the citation-safe floored form the
                # page renders verbatim. Accept both; reject bool (a bool is an
                # int in Python and would render as "True") and blank.
                if isinstance(value, bool):
                    value = None
                elif isinstance(value, (int, float)):
                    pass
                elif isinstance(value, str) and value.strip():
                    value = value.strip()
                else:
                    value = None
                spec["value"] = value
                if value is None and not spec.get("unmeasured_reason"):
                    spec["unmeasured_reason"] = (
                        "no live value published for `%s` at %s"
                        % (spec.get("token"), METRIC_SOURCE_URL))
                card["metric"] = spec
            cards.append(card)
        return dict(block, cards=cards)
    except Exception as e:      # a binding failure must never blank the block
        logger.warning("resolve_card_metrics: %s", str(e)[:160])
        return block


def canon_values() -> dict:
    """The canonical token->value map, resolved IN-PROCESS.

    Same source /api/v1/canon/phrases serves, reached through the function
    rather than over HTTP — a public request must never self-request. Returns
    {} when canon is unavailable, which renders every card numberless rather
    than stale. NEVER raises.
    """
    try:
        from ai_surface_canon import resolve_canon
        c = resolve_canon() or {}
        pub = c.get("public") or {}
        out = {
            "tools": c.get("tools_advertised") or c.get("tools_live"),
            "deals": pub.get("deals"),
            "markets": pub.get("markets"),
            "facilities": pub.get("facilities"),
            "countries": pub.get("countries"),
        }
        return {k: v for k, v in out.items() if v is not None}
    except Exception as e:
        logger.warning("canon_values: %s", str(e)[:160])
        return {}


@platform_updates_bp.route("/api/v1/platform-updates", methods=["GET"])
def platform_updates():
    """PUBLIC: the approved platform announcements rendered on /whats-new."""
    body = published_updates()
    resp = jsonify(dict(body, ok=bool(body.get("ok")),
                        license="CC-BY-4.0",
                        source_name="DC Hub (dchub.cloud)"))
    resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
    return resp
