"""util/customer_testimonials.py — named HUMAN customer testimonials, from ONE source.

WHY THIS EXISTS
---------------
The single source of truth for human customer testimonials is the static file
the frontend serves at https://dchub.cloud/testimonials.json. Before this
module the backend hard-coded the one approved quote into the /enterprise
template, so a second approved quote (or a withdrawn one) would have had to be
edited in two repos by hand. Every backend surface that shows a customer quote
— /enterprise, why_dchub, /llms.txt, /llms-full.txt, ai-agents.json — reads it
through here instead.

HONESTY RULE
------------
These are PEOPLE who approved public use of their words. They are never mixed
with AI-assistant quotes (the ai_testimonials table, /api/v1/testimonials), and
every surface that renders them labels them as named human customers.

BEHAVIOUR
---------
* Fetch with a real User-Agent: Cloudflare answers 403 to urllib's default.
* Bounded: a short timeout, and an in-process cache so a page render costs no
  network on the hot path.
* On any error the last good list is kept; before the first success the list
  is empty and every caller renders its pointer-only / omitted form.
* Entries missing name, title, company or quote are dropped, never repaired.
* Never raises.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

TESTIMONIALS_PAGE_URL = "https://dchub.cloud/testimonials"
TESTIMONIALS_JSON_URL = "https://dchub.cloud/testimonials.json"
USER_AGENT = "DCHub-Backend/1.0 (+https://dchub.cloud)"
FETCH_TIMEOUT_S = 4
CACHE_TTL_S = 600
_REQUIRED = ("name", "title", "company", "quote")

HUMAN_CUSTOMER_NOTE = (
    "Named human customers who approved public use of their words. These are "
    "people, not AI assistants; quotes from AI assistants are published "
    "separately at https://dchub.cloud/api/v1/testimonials and are never "
    "mixed with these."
)

RETRY_AFTER_FAILURE_S = 60
# Set to "0" to disable the network fetch (the never-loaded path, []). The test
# suite sets it in tests/conftest.py so neither pytest nor the app it boots in a
# subprocess (scripts/app_contract_gate.py) ever reaches dchub.cloud.
FETCH_ENV = "DCHUB_CUSTOMER_TESTIMONIALS_FETCH"

_lock = threading.Lock()
# items: last good list (None until the first success).
# next_fetch_at: earliest time another fetch may start — set BEFORE fetching so
# concurrent renders never stampede the origin, and so a failing origin costs
# at most one bounded fetch per RETRY_AFTER_FAILURE_S rather than one per render.
_cache: dict = {"items": None, "next_fetch_at": 0.0}


def _fetch_raw():
    """Return the parsed JSON document. Raises on any failure (caller catches)."""
    if os.environ.get(FETCH_ENV, "1").strip() == "0":
        raise RuntimeError(FETCH_ENV + "=0: testimonials fetch disabled")
    req = urllib.request.Request(
        TESTIMONIALS_JSON_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _validate(doc) -> list:
    """Keep only entries carrying every required field as a non-empty string."""
    if not isinstance(doc, dict):
        raise ValueError("testimonials.json is not an object")
    rows = doc.get("customer_testimonials")
    if not isinstance(rows, list):
        raise ValueError("customer_testimonials is not a list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not all(isinstance(row.get(k), str) and row.get(k).strip() for k in _REQUIRED):
            continue
        clean = {k: row[k].strip() for k in _REQUIRED}
        clean["featured"] = row.get("featured") is True
        out.append(clean)
    return out


def _reset_cache_for_tests():
    with _lock:
        _cache["items"] = None
        _cache["next_fetch_at"] = 0.0


def get_customer_testimonials() -> list:
    """Named human customer testimonials, each {name, title, company, quote, featured}.

    Cached for CACHE_TTL_S. On a failed refresh the last good list is kept and
    the fetch is retried after RETRY_AFTER_FAILURE_S; [] if nothing has ever
    loaded. Never raises.
    """
    try:
        now = time.time()
        with _lock:
            do_fetch = now >= _cache["next_fetch_at"]
            if do_fetch:
                _cache["next_fetch_at"] = now + RETRY_AFTER_FAILURE_S
        if do_fetch:
            try:
                new_items = _validate(_fetch_raw())
            except Exception as exc:  # network, HTTP, JSON or shape
                logger.info("customer testimonials refresh failed, keeping last good: %s", exc)
                new_items = None
            if new_items is not None:
                with _lock:
                    _cache["items"] = new_items
                    _cache["next_fetch_at"] = time.time() + CACHE_TTL_S
        with _lock:
            items = _cache["items"]
        return [dict(r) for r in (items or [])]
    except Exception:
        return []


def featured_testimonial(items=None):
    """The entry marked featured, else the first, else None."""
    try:
        if items is None:
            items = get_customer_testimonials()
        for row in items:
            if row.get("featured") is True:
                return row
        return items[0] if items else None
    except Exception:
        return None


def public_fields(row: dict) -> dict:
    """The four fields agents are given for one testimonial."""
    return {k: row.get(k) for k in _REQUIRED}


def testimonials_pointer() -> dict:
    """Static pointer object for discovery manifests (no quote text)."""
    return {
        "page": TESTIMONIALS_PAGE_URL,
        "json": TESTIMONIALS_JSON_URL,
        "note": HUMAN_CUSTOMER_NOTE,
    }


def llms_txt_block() -> str:
    """Plain-text section for /llms.txt and /llms-full.txt.

    Appended AFTER canon_text() by the callers, so quote text is never scanned
    for {canon_*} placeholders.
    """
    lines = [
        "",
        "## Customer testimonials (named human customers)",
        "Quotes from named people at customer organisations who approved public",
        "use of their words. These are people, not AI assistants; AI-assistant",
        "quotes are published separately at https://dchub.cloud/api/v1/testimonials.",
        "- [Customer testimonials](" + TESTIMONIALS_PAGE_URL + "): human-readable page",
        "- [testimonials.json](" + TESTIMONIALS_JSON_URL + "): machine-readable source",
    ]
    try:
        row = featured_testimonial()
    except Exception:
        row = None
    if row:
        quote = " ".join(str(row["quote"]).split())
        lines.append("")
        lines.append("> “" + quote + "”")
        lines.append("> — " + row["name"] + ", " + row["title"] + ", " + row["company"])
    return "\n".join(lines) + "\n"
