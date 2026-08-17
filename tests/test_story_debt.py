"""Ship-to-story detector — the contracts that keep the author lane safe.

The three that matter most:
  1. an EMPTY nav parse is BLIND, never PASS (empty-parse=PASS is how a dead
     instrument reads as a clean bill);
  2. a staged skeleton is INVISIBLE to the real loader gate — bound against
     _is_published AST-extracted from routes/platform_updates.py, not against
     a copy of the rule — because this repo auto-merges PRs and the only
     thing standing between a bot-staged draft and the public page is that
     status word;
  3. a draft covers NOTHING — debt stays open until a human publishes.

utils/story_debt.py is pure stdlib and import-safe (tests never import
main.py; nothing here touches flask or the network).
"""
from __future__ import annotations

import ast
import os

from utils.story_debt import (
    EXCLUDE_PATHS,
    compute_debt,
    href_path,
    parse_nav_new_items,
    published_link_paths,
    skeleton_entry,
    ship_vs_story_verdict,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Verbatim lines from the LIVE js/dchub-nav.js (2026-08-17) — the real quoting
# and field order the parser must survive, plus one non-NEW entry that must be
# ignored and one double-quoted variant.
NAV_FIXTURE = """
const items = [
  { label: 'Grid Radar',       href: '/radar',              desc: 'Daily grid-transition briefing — live & cited, re-voiced for a different audience each day', badge: 'NEW' },
  { label: 'L&P · Table view',   href: '/land-power-map?view=table', desc: 'Every loaded map layer as a sortable table — CSV export, and it names the layers it covers', badge: 'NEW' },
  { label: 'L&P · Chart view',   href: '/land-power-map?view=chart', desc: 'Loaded features per layer, charted for the current viewport — a layer toggled off reads "off", never zero', badge: 'NEW' },
  { label: 'L&P · News view',    href: '/land-power-map?view=news',  desc: 'Live infrastructure news beside the map — a failed fetch states the reason, never an empty list', badge: 'NEW' },
  { id: 'datacenters', label: 'Data Centers', type: 'link', href: '/database', badge: "NEW" },
  { label: 'Pricing', href: '/pricing', desc: 'not new, must be ignored' },
  { label: "What's New", href: '/whats-new', badge: 'NEW' },
];
"""


def _published(href):
    return {"status": "published", "link": {"href": href}}


# ── parsing ──────────────────────────────────────────────────────────────

def test_parse_extracts_only_new_badged_entries():
    items = parse_nav_new_items(NAV_FIXTURE)
    labels = [i["label"] for i in items]
    assert "Grid Radar" in labels
    assert "L&P · Table view" in labels
    assert "Data Centers" in labels          # double-quoted badge variant
    assert "Pricing" not in labels           # no badge → not shipped-as-NEW
    assert len(items) == 6                   # 5 products + /whats-new (excluded later)


def test_parse_of_junk_is_empty_and_blind_not_pass():
    assert parse_nav_new_items("") == []
    assert parse_nav_new_items("<html>edge error page</html>") == []
    verdict, note = ship_vs_story_verdict(0, [])
    assert verdict == "BLIND"
    # The mutation that matters: an empty parse must NEVER read as a clean
    # backlog. If someone "simplifies" the verdict to PASS, this line fails.
    assert verdict != "PASS"
    assert "instrument" in note


# ── path grain ───────────────────────────────────────────────────────────

def test_href_path_strips_query_fragment_and_origin():
    assert href_path("/land-power-map?view=table") == "/land-power-map"
    assert href_path("https://dchub.cloud/radar#top") == "/radar"
    assert href_path("/api/v1/sites/cross-layer?lat=39&lon=-77") == "/api/v1/sites/cross-layer"
    assert href_path("") == ""


def test_three_lp_views_collapse_to_one_debt_entry():
    items = parse_nav_new_items(NAV_FIXTURE)
    debt = compute_debt(items, [])
    paths = [d["path"] for d in debt]
    assert paths.count("/land-power-map") == 1
    lp = next(d for d in debt if d["path"] == "/land-power-map")
    assert len(lp["labels"]) == 3            # all three view labels merged


# ── coverage ─────────────────────────────────────────────────────────────

def test_published_card_at_path_clears_debt_and_draft_does_not():
    items = parse_nav_new_items(NAV_FIXTURE)
    covered = [_published("/radar"),
               _published("/land-power-map?view=table"),
               _published("/database")]
    debt = compute_debt(items, covered)
    assert debt == []                        # /whats-new excluded, rest covered

    draft = {"status": "draft", "link": {"href": "/radar"}}
    debt2 = compute_debt(items, [draft, _published("/land-power-map"),
                                 _published("/database")])
    assert any(d["path"] == "/radar" for d in debt2), (
        "a DRAFT must not clear debt — only a published card is a story")


def test_whats_new_itself_is_never_debt():
    assert "/whats-new" in EXCLUDE_PATHS
    items = parse_nav_new_items("{ label: 'What&#x27;s New', href: '/whats-new', badge: 'NEW' }")
    # even when it parses, it never lands in debt
    assert compute_debt([{"label": "x", "href": "/whats-new"}], []) == []


def test_verdicts_red_on_debt_pass_on_none():
    red, _ = ship_vs_story_verdict(5, [{"path": "/radar"}])
    ok, _ = ship_vs_story_verdict(5, [])
    assert red == "RED" and ok == "PASS"


# ── the skeleton is invisible to the REAL gate ───────────────────────────

def _real_is_published():
    """AST-extract _is_published from routes/platform_updates.py so this test
    binds the shipped rule, not a re-statement of it."""
    src = open(os.path.join(ROOT, "routes", "platform_updates.py"),
               encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_is_published":
            ns = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "platform_updates.py", "exec"), ns)
            return ns["_is_published"]
    raise AssertionError("_is_published not found in routes/platform_updates.py")


def test_skeleton_is_draft_and_withheld_by_the_real_gate():
    is_published = _real_is_published()
    sk = skeleton_entry({"path": "/radar", "labels": ["Grid Radar"],
                         "hrefs": ["/radar"], "descs": ["daily briefing"]},
                        "2026-08-17")
    assert sk["status"] == "draft"
    assert is_published(sk) is False, (
        "a staged skeleton MUST be invisible to the loader — this is the "
        "auto-merge safety; if this fails, do not ship the author lane")
    # and the control: the same entry with the literal status IS visible,
    # so this test cannot pass vacuously against a broken extractor.
    assert is_published({**sk, "status": "published"}) is True


def test_skeleton_id_is_stable_and_path_derived():
    a = skeleton_entry({"path": "/land-power-map", "labels": ["x"],
                        "hrefs": ["/land-power-map?view=table"], "descs": []},
                       "2026-08-17")
    b = skeleton_entry({"path": "/land-power-map", "labels": ["y"],
                        "hrefs": ["/land-power-map?view=chart"], "descs": []},
                       "2026-08-18")
    assert a["id"] == b["id"] == "draft-land-power-map"


def test_published_link_paths_ignores_drafts_and_junk():
    got = published_link_paths([
        _published("/radar"),
        {"status": "draft", "link": {"href": "/database"}},
        {"status": "published", "link": "not-a-dict"},
        "not-a-dict",
    ])
    assert got == {"/radar"}
