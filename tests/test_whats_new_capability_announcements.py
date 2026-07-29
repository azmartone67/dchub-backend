"""Guard tests for the brain-staged / owner-approved platform announcements on
/api/v1/whats-new (routes/capability_announcements.py + routes/infra_growth.py).

THE DEFECT THESE PIN: the "New platform capabilities" cards on /whats-new were
hardcoded HTML with frozen numbers and went stale — the page still said
"36 grids" (live scoreboard: 46+) and "tool #73" / "All 73 tools" (live
tools/list: 81). Cards are data now, every number binds live at serve time, and
a draft can never reach the public payload.

WHAT IS PINNED
  1. the registry module exists and PARSES (assert the parse produced nodes —
     an empty parse passes every isinstance filter and is vacuously green)
  2. only status == "approved" is ever rendered; a pending draft is invisible
  3. a card that cannot resolve a live figure is WITHHELD with a reason —
     never rendered with a 0 and never with a frozen number
  4. no frozen number literal (\\d{2,}) in any title/body — counts are
     {placeholders} bound to a declared resolver
  5. every resolver declares its basis + the public endpoint and field an agent
     can call to verify the figure
  6. floors round DOWN and never exceed the live value
  7. no operational-MW / pipeline-GW / dollar aggregate in any announcement
  8. /api/v1/whats-new publishes the block fail-soft: platform is NULL + a
     reason when unavailable (never [] , which reads as "nothing shipped"), and
     the call sits in its own try/except so items[] can never be taken down
  9. the drift-PR append marker occurs EXACTLY ONCE in the file (drift_pr_writer
     silently SKIPS an edit whose `find` matches 0 or 2+ times)

CI-SAFETY: the unit-tests job installs ONLY pytest, not requirements.txt.
routes/capability_announcements.py is stdlib-only by design, so everything here
runs with no third-party deps. Nothing runs at module scope; nothing imports
main.py.

EXPECTED COUNTS
  unpatched (before this change): 12 failed, 0 passed — the module does not
    exist and routes/infra_growth.py serves no platform block.
  patched: 12 passed.
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD_PATH = os.path.join(ROOT, "routes", "capability_announcements.py")
GROWTH_PATH = os.path.join(ROOT, "routes", "infra_growth.py")


# ── helpers (never run at import time) ───────────────────────────────────────

def _read(path):
    assert os.path.exists(path), f"missing file: {path}"
    with open(path, encoding="utf-8") as f:
        return f.read()


def _tree(path):
    """Parse a source file and PROVE the parse produced nodes."""
    tree = ast.parse(_read(path))
    assert isinstance(tree, ast.Module), f"{path} did not parse to a Module"
    assert tree.body, f"{path} parsed to an EMPTY module — the assertions below "\
                      f"would pass vacuously"
    return tree


def _mod():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    assert os.path.exists(MOD_PATH), f"missing module: {MOD_PATH}"
    import routes.capability_announcements as m
    return m


class _FakeCur:
    """Minimal DB cursor: maps a substring of the SQL to a scalar, or to an
    Exception instance to simulate an unmeasurable figure."""

    def __init__(self, answers):
        self._answers = answers
        self._row = None
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        for needle, val in self._answers.items():
            if needle in sql:
                if isinstance(val, Exception):
                    raise val
                self._row = [val]
                return
        raise AssertionError("unexpected SQL: " + sql[:120])

    def fetchone(self):
        return self._row


def _all_live():
    """Every resolver answers with a plausible live value."""
    m = _mod()
    return {spec["sql"]: 12345 + i * 7
            for i, spec in enumerate(m._RESOLVERS.values())}


# ── 1. module exists + parses ────────────────────────────────────────────────

def test_registry_module_exists_and_parses():
    tree = _tree(MOD_PATH)
    names = {t.id for n in tree.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
    for required in ("ANNOUNCEMENTS", "_RESOLVERS", "STATUS_APPROVED", "STATUS_PENDING"):
        assert required in names, f"{required} not assigned at module scope"


# ── 2. approval gate: a draft is NEVER served ────────────────────────────────

def test_only_approved_entries_are_served():
    m = _mod()
    saved = m.ANNOUNCEMENTS
    field = next(iter(m._RESOLVERS))
    try:
        m.ANNOUNCEMENTS = [
            {"key": "served_one", "status": m.STATUS_APPROVED, "tag": "T",
             "title": "Approved card", "body": "live: {" + field + "}",
             "cta_href": "/x", "cta_label": "go", "shipped_at": "2026-07-29"},
            {"key": "draft_one", "status": m.STATUS_PENDING, "tag": "T",
             "title": "Draft card", "body": "live: {" + field + "}",
             "cta_href": "/x", "cta_label": "go", "shipped_at": "2026-07-29"},
            {"key": "no_status", "tag": "T", "title": "Unmarked card",
             "body": "live: {" + field + "}", "cta_href": "/x", "cta_label": "go"},
        ]
        out = m.capability_announcement_cards(_FakeCur(_all_live()))
        assert out["ok"] is True
        keys = [c["key"] for c in out["cards"]]
        assert keys == ["served_one"], f"a non-approved entry was served: {keys}"
        assert out["approved_count"] == 1 and out["staged_count"] == 2
        blob = repr(out)
        assert "Draft card" not in blob and "Unmarked card" not in blob
    finally:
        m.ANNOUNCEMENTS = saved


# ── 3. a card that cannot cite a live figure does not render ─────────────────

def test_unresolvable_figure_withholds_the_card_never_zero():
    m = _mod()
    field = next(iter(m._RESOLVERS))
    sql = m._RESOLVERS[field]["sql"]
    saved = m.ANNOUNCEMENTS
    try:
        m.ANNOUNCEMENTS = [
            {"key": "needs_figure", "status": m.STATUS_APPROVED, "tag": "T",
             "title": "Card", "body": "live: {" + field + "}",
             "cta_href": "/x", "cta_label": "go", "shipped_at": "2026-07-29"},
        ]
        # (a) the query blows up
        out = m.capability_announcement_cards(_FakeCur({sql: RuntimeError("boom")}))
        assert out["ok"] is True and out["cards"] == []
        assert out["withheld"] and out["withheld"][0]["key"] == "needs_figure"
        assert out["withheld"][0]["reason"], "withheld without a reason"
        # (b) the query returns zero — an unmeasured figure must not publish as 0
        out0 = m.capability_announcement_cards(_FakeCur({sql: 0}))
        assert out0["cards"] == [], "a zero figure was published as a card"
        assert "0" not in [c.get("body") for c in out0["cards"]]
    finally:
        m.ANNOUNCEMENTS = saved


def test_every_shipped_entry_binds_a_declared_live_resolver():
    m = _mod()
    assert m.ANNOUNCEMENTS, "registry is empty"
    for e in m.ANNOUNCEMENTS:
        assert e.get("status") in (m.STATUS_APPROVED, m.STATUS_PENDING), \
            f"{e.get('key')}: unknown status {e.get('status')!r}"
        for k in ("key", "tag", "title", "body", "cta_href", "cta_label", "shipped_at"):
            assert e.get(k), f"{e.get('key')}: missing {k}"
        bound = [mm.group(1) for mm in m._PLACEHOLDER.finditer(e["body"])]
        assert bound, f"{e['key']}: names no live figure"
        for f in bound:
            assert f in m._RESOLVERS, f"{e['key']}: binds unknown resolver {f!r}"


# ── 4. no frozen numbers in the copy (the "36 grids" / "73 tools" defect) ────

def test_no_frozen_number_literal_in_any_announcement_copy():
    m = _mod()
    frozen = re.compile(r"\d{2,}")
    for e in m.ANNOUNCEMENTS:
        for k in ("title", "body"):
            stripped = m._PLACEHOLDER.sub("", e[k])
            hit = frozen.search(stripped)
            assert not hit, (f"{e['key']}.{k} contains the frozen literal "
                             f"{hit.group(0)!r} — bind it to a resolver instead")
        assert m.validate_copy(e["title"], e["body"]) is None, \
            f"{e['key']}: shipped copy fails validate_copy"


def test_staging_refuses_frozen_numbers_and_publishes_nothing():
    m = _mod()
    field = next(iter(m._RESOLVERS))
    assert m.validate_copy("t", "now ranks 36 grids {" + field + "}") is not None
    assert m.validate_copy("t", "no figure here at all") is not None
    assert m.validate_copy("t", "binds {not_a_real_resolver}") is not None
    before = list(m.ANNOUNCEMENTS)
    res = m.stage_announcement_pr(
        key="frozen_probe", tag="T", title="Probe",
        body="the surface is now 73 live tools", cta_href="/x", cta_label="go")
    assert res.get("ok") is False, "staging accepted a frozen number"
    assert m.ANNOUNCEMENTS == before, "staging mutated the served registry"


# ── 5. every figure ships its own verification ──────────────────────────────

def test_every_resolver_declares_basis_and_a_verify_endpoint():
    m = _mod()
    assert m._RESOLVERS, "no resolvers registered"
    for name, spec in m._RESOLVERS.items():
        for k in ("sql", "basis", "verify_endpoint", "verify_field", "floor_step"):
            assert spec.get(k), f"{name}: resolver missing {k}"
        assert spec["verify_endpoint"].startswith("/api/"), \
            f"{name}: verify_endpoint must be a callable public API path"
        # a literal % in a psycopg2 query string is a live 500
        assert "%" not in spec["sql"], f"{name}: literal % in SQL"
        assert spec["sql"].strip().upper().startswith("SELECT"), f"{name}: not a SELECT"


def test_rendered_card_carries_the_field_it_resolved_from():
    m = _mod()
    out = m.capability_announcement_cards(_FakeCur(_all_live()))
    assert out["ok"] is True and out["cards"], "no card rendered against live figures"
    for card in out["cards"]:
        assert card["figures"], f"{card['key']}: rendered with no figures[]"
        assert card["verify"], f"{card['key']}: rendered with no verify endpoint"
        for fig in card["figures"]:
            assert fig["field"] in m._RESOLVERS
            assert fig["verify_field"] and fig["basis"]
            assert isinstance(fig["value"], int) and fig["value"] > 0
            assert fig["rendered"] in card["body"], \
                f"{card['key']}: figure {fig['field']} not rendered into the body"
        assert "{" not in card["body"], f"{card['key']}: unrendered placeholder left in body"


# ── 6. floors round DOWN ────────────────────────────────────────────────────

def test_floor_rounds_down_and_never_exceeds_live():
    m = _mod()
    for n, step in ((15209, 1000), (23082, 1000), (306, 100), (186, 10), (1000, 1000)):
        text = m._floor(n, step)
        assert text and text.endswith("+")
        val = int(text[:-1].replace(",", ""))
        assert val <= n, f"floor {text} exceeds live {n}"
        assert n - val < step
    assert m._floor(0, 1000) is None
    assert m._floor(999, 1000) is None, "a floor of 0+ must never publish"


# ── 7. banned figures ───────────────────────────────────────────────────────

def test_no_mw_gw_or_dollar_aggregate_in_any_announcement():
    m = _mod()
    bad = re.compile(r"(?i)(\bMW\b|\bGW\b|megawatt|gigawatt|\$)")
    for e in m.ANNOUNCEMENTS:
        for k in ("title", "body"):
            assert not bad.search(e[k]), f"{e['key']}.{k} carries a banned MW/GW/$ figure"


# ── 8. the public route publishes it fail-soft ──────────────────────────────

def test_whats_new_publishes_the_block_fail_soft():
    src = _read(GROWTH_PATH)
    tree = _tree(GROWTH_PATH)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "whats_new"), None)
    assert fn is not None, "whats_new() not found in routes/infra_growth.py"
    body = ast.get_source_segment(src, fn) or ""
    for token in ("capability_announcement_cards", "platform_unavailable_reason",
                  "platform_withheld", "platform_as_of"):
        assert token in body, f"whats_new() does not publish {token}"
    # the call must live inside its own try/except so it can never 500 the route
    guarded = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            seg = ast.get_source_segment(src, node) or ""
            if "capability_announcement_cards" in seg and node.handlers:
                guarded = True
                break
    assert guarded, ("the capability_announcement_cards() call is not wrapped in its "
                     "own try/except — a card-store error would 500 the public feed")
    # unavailable -> null + a reason, NEVER an empty list ("nothing shipped")
    assert "platform = plat.get(\"cards\") if _plat_ok else None" in body, \
        "platform must publish None (not []) when the announcement source is unavailable"


# ── 9. the draft-PR append marker must match EXACTLY ONCE ───────────────────

def test_append_marker_occurs_exactly_once():
    m = _mod()
    src = _read(MOD_PATH)
    n = src.count(m._APPEND_MARKER)
    assert n == 1, (f"the drift-PR append marker occurs {n}x in the file — "
                    "drift_pr_writer applies an edit only when `find` matches "
                    "exactly once, so staging would silently no-op")
