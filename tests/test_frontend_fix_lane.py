"""The frontend fix lane must refuse more than it applies.

The squasher's queue is 52 rows and this lane patches four of them. That ratio
IS the subject: the other 48 are not blocked by plumbing, they are rows whose
"suggested fix" would be wrong to apply, and a lane that shipped them would put
bugs into the frontend at machine speed with a brain-authored PR on top.

The eight `js_field_fallback_missing` lines below are the REAL lines from live
dchub-frontend main (read 2026-09-18), not invented fixtures. Four are value
reads with an existing fallback; four sit inside a condition where adding
`|| .company` changes what a filter matches or what a guard admits.

Stdlib + pytest; no DB, no network.
"""
import pytest

from routes import brain_frontend_fix_lane as lane

APPLY = "apply"


def _f(line_no, detail_snippet=None, issue=lane._AUTOFIXABLE_ISSUE):
    detail = f"Snippet: {detail_snippet}" if detail_snippet else ""
    return {"issue": issue, "path": "capacity-pipeline.html", "line": line_no,
            "detail": detail, "url": f"dchub-frontend/capacity-pipeline.html:{line_no}"}


def _classify_one(src_line, **kw):
    return lane._classify(_f(1, **kw), [src_line])


# ── the four that MUST be patched ────────────────────────────────────────────
@pytest.mark.parametrize("src,expect", [
    ("        const operatorInitials = (item.operator || 'UN').substring(0, 2).toUpperCase();",
     "(item.operator || item.company || 'UN')"),
    ("        <span class=\"operator-name\">${item.operator || 'Unknown'}</span>",
     "${item.operator || item.company || 'Unknown'}"),
    ("            item.operator || 'Unknown',",
     "item.operator || item.company || 'Unknown',"),
    ("      OPERATOR: item.operator || item.name,",
     "item.operator || item.company || item.name,"),
])
def test_value_reads_get_one_more_link_in_the_existing_chain(src, expect):
    new, disp, why = _classify_one(src)
    assert disp == APPLY, why
    assert expect in new
    # the final fallback is untouched -- that is what makes this safe
    assert new.count("||") == src.count("||") + 1


# ── the four that MUST NOT be patched ────────────────────────────────────────
@pytest.mark.parametrize("src", [
    "        if (item.operator && item.operator !== 'Unknown' && item.operator !== 'null') {",
    "        if (operator && item.operator !== operator) return false;",
    "            if (operator && item.operator !== operator) return false;",
    "        if (item.operator || item.location) {",
])
def test_predicate_reads_are_refused_with_a_reason(src):
    """Adding a fallback here changes what the filter matches. The detector's
    regex cannot tell these from a value read -- this lane can."""
    new, disp, why = _classify_one(src)
    assert new is None
    assert disp == "predicate_read", f"{disp}: {why}"
    assert "behaviour change" in why or "change what this filter" in why


def test_the_live_eight_split_four_and_four():
    """The whole design rests on this ratio. If a future edit makes the lane
    greedier, this is the test that says so."""
    live = [
        "        if (item.operator && item.operator !== 'Unknown' && item.operator !== 'null') {",
        "        if (operator && item.operator !== operator) return false;",
        "        const operatorInitials = (item.operator || 'UN').substring(0, 2).toUpperCase();",
        "        <span class=\"operator-name\">${item.operator || 'Unknown'}</span>",
        "        if (operator && item.operator !== operator) return false;",
        "            item.operator || 'Unknown',",
        "        if (item.operator || item.location) {",
        "      OPERATOR: item.operator || item.name,",
    ]
    d = [_classify_one(s)[1] for s in live]
    assert d.count(APPLY) == 4, d
    assert d.count("predicate_read") == 4, d


# ── the 44 that are not this lane's business ────────────────────────────────
def test_hero_stat_rows_are_never_auto_fixable():
    """severity='polish', 'may be intentional', and a suggested fix that is a
    CHOICE. Mechanically picking one would em-dash live numbers on index.html."""
    new, disp, why = lane._classify(
        _f(1, issue="bug_squash:hardcoded_hero_stat"),
        ['<span id="s-countries">140+</span>'])
    assert new is None
    assert disp == "not_auto_fixable"
    assert "polish" in why


# ── refusals that keep a re-run honest ──────────────────────────────────────
def test_a_line_that_moved_is_stale_not_patched_blind():
    new, disp, why = lane._classify(
        _f(1, detail_snippet="item.operator || 'Unknown',"),
        ["            item.somethingElse || 'Unknown',"])
    assert new is None and disp == "stale_finding", (disp, why)


def test_line_past_eof_is_stale():
    new, disp, why = lane._classify(_f(99), ["one line only"])
    assert new is None and disp == "stale_finding"
    assert "past EOF" in why


def test_rerunning_after_a_fix_is_a_noop():
    new, disp, _ = _classify_one("            item.operator || item.company || 'Unknown',")
    assert new is None and disp == "already_fixed"


def test_two_occurrences_on_one_line_is_ambiguous_not_a_guess():
    new, disp, why = _classify_one(
        "      a = item.operator || 'x'; b = item.operator || 'y';")
    assert new is None and disp == "ambiguous_line", (disp, why)


def test_a_bare_read_beside_a_chained_one_is_ambiguous_not_first_match_wins():
    """★ The case the OTHER ambiguity check exists for, and the one a
    two-chained-reads test never reaches. Here exactly ONE read is followed by
    `||`, so the hit count is 1 and the first guard passes -- but the line holds
    a second, BARE `item.operator`, and `str.replace(old, new, 1)` rewrites the
    FIRST occurrence, which is the bare one. Without the count check the lane
    silently patches a different expression than the finding named.

    Found by mutation M6: deleting the count check left the suite green,
    because the only test covering it was answered by the hit-count guard."""
    new_line, disp, why = _classify_one(
        "      const a = item.operator; const b = item.operator || 'x';")
    assert new_line is None, f"patched the wrong occurrence: {new_line}"
    assert disp == "ambiguous_line", (disp, why)
    assert "2x" in why


def test_a_bare_read_with_no_existing_fallback_is_not_touched():
    """Only an EXISTING chain is extended. A bare read has no declared
    'this may be missing', so rewriting it is a guess about intent."""
    new, disp, _ = _classify_one("      const name = item.operator;")
    assert new is None and disp == "no_value_read"


# ── the lane must not assume it can write ───────────────────────────────────
def test_write_capability_reads_the_push_permission_not_the_status_code(monkeypatch):
    class R:
        status_code = 200
        def json(self): return {"permissions": {"pull": True, "push": False}}
    monkeypatch.setattr(lane, "_gh", lambda *a, **k: R())
    cap = lane._write_capability()
    assert cap["can_write"] is False
    assert cap["reason"] == "token_is_read_only"
    assert "DCHUB_FRONTEND_RO_TOKEN" in cap["detail"]


def test_a_404_is_reported_as_invisible_not_as_read_only(monkeypatch):
    class R:
        status_code = 404
        text = "Not Found"
        def json(self): return {}
    monkeypatch.setattr(lane, "_gh", lambda *a, **k: R())
    cap = lane._write_capability()
    assert cap["can_write"] is False
    assert cap["reason"] == "repo_not_visible_to_token"


def test_push_true_is_the_only_thing_that_grants_write(monkeypatch):
    class R:
        status_code = 200
        def json(self): return {"permissions": {"pull": True, "push": True}}
    monkeypatch.setattr(lane, "_gh", lambda *a, **k: R())
    assert lane._write_capability()["can_write"] is True


# ── the gate must agree with the key the operator actually holds ────────────
def _call_gate(gate, env, header, monkeypatch):
    """Run an _admin_ok() with a controlled env + header, via a real request
    context so request.headers behaves as it does in production."""
    import main  # noqa: F401  (imported for its app factory side effects)
    for k in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "BRAIN_ADMIN_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/", headers={"X-Admin-Key": header}):
        return gate()


def test_this_lane_and_the_squasher_accept_the_same_admin_key(monkeypatch):
    """★ The regression this pins. The lane's first gate read BRAIN_ADMIN_KEY
    first and the sibling squasher gate reads DCHUB_ADMIN_KEY. Both names exist
    on the service with DIFFERENT values, so the operative key opened
    /bug-squash/actionable (200) and was refused here (401): the endpoint
    shipped gated SHUT while "admin-gated" read as correct in review.

    A gate being PRESENT is not the property. Agreeing with the key the
    operator holds is."""
    from routes.brain_bug_squash import _admin_ok as squasher_gate

    env = {"DCHUB_ADMIN_KEY": "the-operative-key",
           "BRAIN_ADMIN_KEY": "a-different-stale-key"}
    assert _call_gate(squasher_gate, env, "the-operative-key", monkeypatch) is True
    assert _call_gate(lane._admin_ok, env, "the-operative-key", monkeypatch) is True, (
        "the lane refuses the key its sibling accepts")


def test_the_gate_is_closed_when_no_key_is_configured(monkeypatch):
    assert _call_gate(lane._admin_ok, {}, "anything", monkeypatch) is False


def test_a_wrong_key_is_refused(monkeypatch):
    assert _call_gate(lane._admin_ok, {"DCHUB_ADMIN_KEY": "right"},
                      "wrong", monkeypatch) is False


def test_the_credential_is_never_accepted_from_the_query_string(monkeypatch):
    """The sibling accepts ?admin_key=. This lane opens pull requests, and a
    credential in a URL lands in access logs."""
    from flask import Flask
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "the-operative-key")
    app = Flask(__name__)
    with app.test_request_context("/?admin_key=the-operative-key"):
        assert lane._admin_ok() is False


# ── the lane may only ship files it can also cache-bust ─────────────────────
def _plan_with(monkeypatch, findings, files):
    """Run the real _plan() over a controlled finding set.

    `files` maps path -> source text; any path NOT in it must never be
    fetched, and _gh raises if it is."""
    import base64

    fetched = []

    class R:
        def __init__(self, text):
            self.status_code = 200
            self._text = text

        def json(self):
            return {"sha": "deadbeef",
                    "content": base64.b64encode(
                        self._text.encode("utf-8")).decode("ascii")}

    def fake_gh(method, path, body=None):
        fetched.append((method, path))
        for p, text in files.items():
            if f"/contents/{p}?" in path:
                return R(text)
        raise AssertionError(f"lane fetched a file it cannot ship: {path}")

    monkeypatch.setattr(lane, "_open_findings", lambda: (findings, None))
    monkeypatch.setattr(lane, "_gh", fake_gh)
    built, err = lane._plan()
    assert err is None, err
    return built, fetched


_HTML_SRC = "            item.operator || 'Unknown',"
_JS_SRC = "      OPERATOR: item.operator || item.name,"


def _row(path, line=1):
    return {"issue": lane._AUTOFIXABLE_ISSUE, "path": path, "line": line,
            "detail": "", "url": f"dchub-frontend/{path}:{line}"}


def test_an_immutable_asset_row_is_refused_and_does_not_strand_the_html_edit(
        monkeypatch):
    """★ dchub-frontend#1509, 2026-09-19. The lane rewrote one line of
    js/dchub-infrastructure.js and left the ?v= token in land-power-map.html
    on the pre-edit hash, so check-immutable-asset-versions.mjs blocked the
    PR — for four hours, and it took the capacity-pipeline.html edits with it,
    because the lane opens ONE PR for every planned file.

    Both halves are the property: the asset row is refused WITH a reason, and
    the served-document edit still ships."""
    built, _ = _plan_with(
        monkeypatch,
        [_row("capacity-pipeline.html"), _row("js/dchub-infrastructure.js")],
        {"capacity-pipeline.html": _HTML_SRC})

    assert [p["path"] for p in built["plan"]] == ["capacity-pipeline.html"], (
        "the served-document edit must still ship")
    assert len(built["plan"][0]["edits"]) == 1

    refused = [s for s in built["skipped"]
               if s["disposition"] == "cache_busted_asset"]
    assert len(refused) == 1, built["skipped"]
    assert refused[0]["url"].endswith("js/dchub-infrastructure.js:1")
    assert "check-immutable-asset-versions.mjs" in refused[0]["reason"]


def test_the_lane_never_fetches_a_file_it_cannot_ship(monkeypatch):
    """The refusal is a PATH decision, so it must land before the contents
    call. fake_gh raises on any other path, so reaching the fetch fails here
    rather than burning a GitHub call per run."""
    built, fetched = _plan_with(
        monkeypatch, [_row("js/dchub-infrastructure.js")], {})
    assert built["plan"] == []
    assert fetched == [], fetched


def test_the_same_line_is_still_APPLY_in_isolation(monkeypatch):
    """The two questions stay separate: _classify answers 'is this edit safe
    on its own' (it is -- it is one of the live four), _plan answers 'can this
    lane ship it' (it cannot). If this ever stops being APPLY, the refusal
    above is passing for the wrong reason."""
    new, disp, why = lane._classify(_row("js/dchub-infrastructure.js"),
                                    [_JS_SRC])
    assert disp == APPLY, (disp, why)
    assert "item.operator || item.company || item.name" in new


def test_a_css_or_static_row_is_refused_too(monkeypatch):
    built, _ = _plan_with(monkeypatch, [_row("static/app.css")], {})
    assert built["plan"] == []
    assert [s["disposition"] for s in built["skipped"]] == ["cache_busted_asset"]


def test_html_is_matched_case_insensitively(monkeypatch):
    built, _ = _plan_with(monkeypatch, [_row("Capacity-Pipeline.HTML")],
                          {"Capacity-Pipeline.HTML": _HTML_SRC})
    assert [p["path"] for p in built["plan"]] == ["Capacity-Pipeline.HTML"]


# ── a row the lane itself fixed is resolved, not stale ──────────────────────
_LIVE_VALUE_READS = [
    "        const operatorInitials = (item.operator || 'UN').substring(0, 2).toUpperCase();",
    "        <span class=\"operator-name\">${item.operator || 'Unknown'}</span>",
    "            item.operator || 'Unknown',",
    "      OPERATOR: item.operator || item.name,",
]


@pytest.mark.parametrize("src", _LIVE_VALUE_READS)
def test_the_lanes_own_landed_fix_reports_already_fixed_not_stale(src):
    """★ 2026-09-19. After dchub-frontend#1509 merged, all four rows came back
    as `stale_finding` -- "re-scan before patching" -- because the lane's OWN
    edit is what stopped the recorded snippet matching. That reads as "the file
    moved under you" for a row that is simply done, and it devalues the label
    on rows where something really did move.

    The round trip runs through the real transform: patch the line the way the
    lane does, then re-classify it against the ORIGINAL snippet."""
    patched, disp, _ = lane._classify(_f(1, detail_snippet=src), [src])
    assert disp == APPLY

    new, disp2, why = lane._classify(_f(1, detail_snippet=src), [patched])
    assert new is None
    assert disp2 == "already_fixed", f"{disp2}: {why}"
    assert "the fix landed" in why


@pytest.mark.parametrize("line_on_disk", [
    # .company, but inserted ahead of the read instead of after it -- not our
    # edit, so we cannot claim the row is resolved.
    "            item.company || item.operator || 'Unknown',",
    # our link IS there, but the final fallback was changed too -- something
    # else moved on this line and a human should look.
    "            item.operator || item.company || 'Somebody Else',",
    # a different field entirely gained the fallback
    "            item.owner || item.company || 'Unknown',",
])
def test_a_company_that_arrived_any_other_way_is_still_stale(line_on_disk):
    """The negative that proves this is not just a reordering of the checks.
    If `already_fixed` were hoisted above the snippet test, every line here
    would wrongly report resolved -- each one contains `.company`."""
    assert ".company" in line_on_disk, "fixture must carry the tempting token"
    new, disp, why = lane._classify(
        _f(1, detail_snippet="item.operator || 'Unknown',"), [line_on_disk])
    assert new is None
    assert disp == "stale_finding", f"{disp}: {why}"
    assert "does not account for the difference" in why


def test_classify_and_the_already_fixed_check_share_one_transform():
    """Both paths call _rewrite. If someone re-implements the edit in either
    place, the two drift and this goes red: the check would stop recognising
    the very line _classify produces."""
    src = "            item.operator || 'Unknown',"
    patched, disp, _ = lane._classify(_f(1), [src])
    assert disp == APPLY
    direct, disp2, _ = lane._rewrite(src)
    assert disp2 == APPLY
    assert direct == patched


def test_a_row_with_no_recorded_snippet_still_reports_already_fixed():
    """The pre-existing path, unchanged: with no snippet to compare, a line
    that already names .company is resolved on that evidence alone."""
    new, disp, _ = _classify_one(
        "            item.operator || item.company || 'Unknown',")
    assert new is None and disp == "already_fixed"
