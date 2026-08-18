"""r-definition-one-writer (2026-08-18) — A PUBLISHED VALUE MAY NOT BE RESTATED.

WHAT WENT WRONG. `human_acted`'s definition is published by the funnel at
`definitions.human_acted.{definition_version, definition_changelog}`. It has
been redefined four times. Each redefinition updated that block — and left
FOUR other surfaces describing whatever version was current when they were
written:

  public /ai card        "Human acted is DEFINITION v3: it reads BOTH human
                          artifacts"                       (API was on v4)
  adoption shell         docstring "DEFINITION v3", cv_gate detail
                         "definition v2"                   (two stale answers,
                                                            same file)
  handoff-truth shell    "[2026-08-16: the funnel is at DEFINITION v3 …]",
                         lane-A verdict "v3 since 2026-08-16", state
                         "definition v3 since 2026-08-16"
  funnel envelope        excluded.basis "human_acted (v4) subtracts …"
                                                           (right today, typed)

Worse than stale: the adoption shell's cv_gate published a `human_view_first_
opened_at IS NOT NULL` count — the v2 instrument — under the label "HUMAN
ACTED", so the board's headline handoff number and the funnel's disagreed by
construction while both used the canonical name.

SAME CLASS AS THE 2026-08-17 VALUE-PINNED GUARDS. mpp-consent pinned an arity,
mpp-undercap pinned a literal recipe string, test_handoff_truth_shell pinned
'"definition_version": 2'. Restating a value the system already publishes is
the defect; the copy has no link to the original, so it rots in silence.

WHAT THIS GUARD PINS — three properties, none of them a number:

  1. NO LITERAL VERSION IN A USER-FACING STRING. A `DEFINITION v<N>` /
     `definition v<N>` literal inside a string CONSTANT (what reaches a JSON
     payload or rendered HTML) fails. Comments and docstrings are exempt on
     purpose: this file, and the modules it guards, must be free to narrate
     "it used to say v3" without tripping their own fence. Interpolating —
     `"DEFINITION v%d" % version` — is the fix and passes, because there is no
     digit in the source to go stale.

  2. EVERY SURFACE RENDERS THE MODULE'S VERSION. The funnel payload, both
     shells and the sentence builder must all report exactly
     handoff_definition's version. A surface that drifts fails even if it
     never typed a digit.

  3. THE TWO SQL ASSEMBLIES ARE BYTE-IDENTICAL. flask_mcp_endpoints builds the
     human_acted count inline; the adoption shell gets it from
     handoff_definition. Same house pattern as tests/test_funnel_health_deloop
     ("the two honest counts are byte-identical"): two writers are allowed only
     while a test proves they agree character for character.

MUTATION-VERIFIED (see test_the_fence_can_actually_fail): the scanner is run
against synthetic sources that reproduce each real regression, and it must go
red on them. A fence that cannot fail is the thing this whole file is about.
"""
import ast
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routes.handoff_definition import (  # noqa: E402
    HUMAN_ACTED_DEFINITION_CHANGELOG,
    HUMAN_ACTED_DEFINITION_VERSION,
    human_acted_count_sql,
    human_acted_definition,
    human_acted_sentence,
)

REPO = os.path.join(os.path.dirname(__file__), "..")

# The surfaces that publish or render the human_acted definition. Add a file
# here the moment it starts describing the stage — an unlisted surface is the
# failure mode this guard exists for (four of them were unlisted for weeks).
GUARDED = [
    "flask_mcp_endpoints.py",
    "routes/adoption_master_shell.py",
    "routes/handoff_truth_master_shell.py",
    "routes/handoff_definition.py",
]

# "DEFINITION v3", "definition v2", "Definition V4" — a version pinned as text.
_PINNED_VERSION = re.compile(r"\bdefinitions?\s+v\s*\d+", re.I)


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _docstring_nodes(tree):
    """Every Constant node that IS a docstring, by identity.

    Docstrings are narration, not output. Excluding them by identity (not by
    value) means a module can quote its own retired copy without the fence
    firing, while an identical string used as a payload value still fails.
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def scan_source(src, label="<source>"):
    """Return [(line, text)] for pinned versions in USER-FACING string constants.

    Raises SyntaxError to the caller on unparseable input — a scanner that
    silently reports "no violations" for a file it could not read is a vacuous
    pass, which is the exact shape this suite is built to reject.
    """
    tree = ast.parse(src)
    skip = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        m = _PINNED_VERSION.search(node.value)
        if m:
            hits.append((getattr(node, "lineno", -1), m.group(0), label))
    return hits


# ── 1. no literal version in anything we publish ────────────────────────────

@pytest.mark.parametrize("rel", GUARDED)
def test_no_pinned_definition_version_in_user_facing_strings(rel):
    hits = scan_source(_read(rel), rel)
    assert not hits, (
        "%s restates the human_acted definition version in a string that "
        "reaches a consumer: %s. Build it from the published block instead — "
        "routes.handoff_definition.human_acted_sentence(), or "
        "'DEFINITION v%%d' %% HUMAN_ACTED_DEFINITION_VERSION. A typed version "
        "has no link to the canon and rots the next time the stage moves."
        % (rel, hits))


def test_the_fence_can_actually_fail():
    """MUST-FAIL CONTROLS — the three real regressions, reconstructed.

    Without these the fence is a grep nobody has watched go red. Each control
    is the literal text that shipped, and the negative controls are the forms
    that MUST keep passing or the fence would forbid its own fix.
    """
    # (a) the /ai card's sentence, as a payload string
    red_a = 'X = {"note": "Human acted is DEFINITION v3: it reads both."}\n'
    # (b) the adoption shell's cv_gate detail
    red_b = 'd = ("human_acted is definition v2 — the first-open stamp, ")\n'
    # (c) the handoff-truth shell's state field
    red_c = 'S = {"core_build": "funnel human_acted at definition v3 since X"}\n'
    for name, src in (("ai-card", red_a), ("cv_gate", red_b), ("state", red_c)):
        assert scan_source(src, name), \
            "the fence did NOT flag the real %s regression — it is vacuous" % name

    # NEGATIVE CONTROLS. These must stay green, or the fence blocks the repair.
    green = [
        # the fix: interpolated, no digit in the source
        'd = "DEFINITION v%d" % VERSION\n',
        'd = f"definition v{VERSION}"\n',
        # narration in a docstring — a module must be able to record what it
        # used to say. This is the case that makes identity-based docstring
        # exclusion load-bearing.
        '"""It used to say DEFINITION v3 here."""\nx = 1\n',
        'def f():\n    """was definition v2"""\n    return 1\n',
        # a comment is not in the AST at all
        '# human_acted DEFINITION v4: v3 minus self-traffic\nx = 1\n',
        # a version that is not a definition version
        'd = "schema v3"\n',
    ]
    for src in green:
        assert not scan_source(src, "negative"), \
            "the fence fires on a legitimate construct: %r" % src

    # And it must refuse to pass blind on input it cannot parse.
    with pytest.raises(SyntaxError):
        scan_source("def (:\n", "broken")


# ── 2. every surface renders the module's version ───────────────────────────

def test_funnel_payload_renders_the_module_version():
    """The endpoint may not carry its own copy of the block."""
    src = _read("flask_mcp_endpoints.py")
    assert re.search(r'"definitions":\s*\{"human_acted":\s*_human_acted_definition\(\)',
                     src), \
        "the funnel stopped publishing routes.handoff_definition's block — a " \
        "second copy of the definition is exactly what this guard removes"
    assert not re.search(r'"definition_changelog"\s*:\s*\{', src), \
        "a changelog dict literal is back in the endpoint"


def test_both_shells_derive_the_sentence():
    for rel in ("routes/adoption_master_shell.py",
                "routes/handoff_truth_master_shell.py"):
        src = _read(rel)
        assert "from routes.handoff_definition import" in src, \
            "%s no longer imports the canon" % rel
        assert re.search(r"_human_acted_sentence\(\)|_handoff_sentence\(\)", src), \
            "%s imports the canon but does not render it — an unused import " \
            "is how the prose gets to drift again" % rel


def test_the_rendered_sentence_carries_the_current_version_and_its_entry():
    """The sentence is the thing a human reads. It must name THIS version and
    quote THIS version's changelog entry — not the newest, not the longest."""
    s = human_acted_sentence()
    assert ("DEFINITION v%d" % HUMAN_ACTED_DEFINITION_VERSION) in s
    assert HUMAN_ACTED_DEFINITION_CHANGELOG[HUMAN_ACTED_DEFINITION_VERSION] in s
    # A version with no entry must say so rather than fall back to the previous
    # one — substituting the old description for a missing new one is how the
    # /ai card described v3 while serving v4.
    undescribed = human_acted_sentence(
        {"definition_version": 99, "definition_changelog":
         dict(HUMAN_ACTED_DEFINITION_CHANGELOG)})
    assert "UNDESCRIBED" in undescribed
    assert HUMAN_ACTED_DEFINITION_CHANGELOG[
        HUMAN_ACTED_DEFINITION_VERSION] not in undescribed


def test_json_string_keys_resolve():
    """A consumer that received the block over HTTP has string keys (JSON has
    no integer keys). The builder must not report UNDESCRIBED for a payload it
    just fetched — that would make the honest path look broken and push the
    next author back to typing the sentence."""
    over_the_wire = {
        "definition_version": HUMAN_ACTED_DEFINITION_VERSION,
        "definition_changelog": {
            str(k): v for k, v in HUMAN_ACTED_DEFINITION_CHANGELOG.items()},
    }
    s = human_acted_sentence(over_the_wire)
    assert "UNDESCRIBED" not in s
    assert HUMAN_ACTED_DEFINITION_CHANGELOG[HUMAN_ACTED_DEFINITION_VERSION] in s


def test_every_version_has_a_changelog_entry():
    """A bump with no explanation is a silent redefinition — the thing all of
    this exists to prevent. Derived from the version, never pinned to a number.
    """
    for v in range(1, HUMAN_ACTED_DEFINITION_VERSION + 1):
        assert HUMAN_ACTED_DEFINITION_CHANGELOG.get(v), \
            "definition_version %d has no changelog entry" % v
    assert max(HUMAN_ACTED_DEFINITION_CHANGELOG) == HUMAN_ACTED_DEFINITION_VERSION, \
        "the changelog describes a version the block does not declare"


def test_the_current_entry_declares_the_self_traffic_exclusion():
    """INVARIANT, NOT VALUE. v4's whole point is that the subtraction is
    declared; if a later version drops the operator exclusion without saying so
    in its own entry, the first non-zero on this stage becomes unreadable
    again."""
    entry = HUMAN_ACTED_DEFINITION_CHANGELOG[HUMAN_ACTED_DEFINITION_VERSION]
    assert "self_traffic_session_prefixes" in entry or "self-traffic" in entry, \
        "the current definition no longer explains the operator exclusion"


# ── 3. the two SQL assemblies agree, character for character ────────────────

def _shipped_funnel_sql(iv, include_self_traffic=False):
    """The funnel's inline assembly, reproduced from mcp_calls_deloop exactly
    as flask_mcp_endpoints builds it."""
    from mcp_calls_deloop import (external_session_predicate,
                                  real_ua_predicate)
    hv = real_ua_predicate("s.human_view_first_ua")
    ro = real_ua_predicate("ro.user_agent")
    body = ("from mcp_high_intent_sessions s "
            "where s.first_hit_at > now() - interval '%s' and "
            "((s.human_view_first_opened_at is not null and "
            "s.human_view_first_ua is not null and " + hv + ") "
            "or exists (select 1 from relay_opens ro where "
            "ro.session_id = s.mcp_session_id and ro.session_id <> '' "
            "and " + ro + "))")
    sql = "select count(distinct s.mcp_session_id) " + body
    if not include_self_traffic:
        sql += " and " + external_session_predicate("s.mcp_session_id")
    return sql % iv


@pytest.mark.parametrize("iv", ["24 hours", "7 days", "30 days"])
def test_shell_and_funnel_count_the_same_sessions(iv):
    """Two writers are tolerable only while a test proves they agree. The
    adoption shell used to count the v2 instrument under the canonical name;
    this is what stops that from coming back through a well-meaning edit to
    either side."""
    assert human_acted_count_sql(iv) == _shipped_funnel_sql(iv)
    assert (human_acted_count_sql(iv, include_self_traffic=True)
            == _shipped_funnel_sql(iv, include_self_traffic=True))


def test_the_v4_exclusion_is_the_only_difference():
    """The v3 diagnostic must stay published beside v4 — a silent subtraction
    is the original defect in a new coat — and it must differ ONLY by the
    self-traffic predicate."""
    from mcp_calls_deloop import external_session_predicate
    v4 = human_acted_count_sql("30 days")
    v3 = human_acted_count_sql("30 days", include_self_traffic=True)
    assert v4 == v3 + " and " + external_session_predicate("s.mcp_session_id")


def test_the_count_sql_carries_no_literal_percent():
    """The %-trap this endpoint has already been taken down by: a `NOT LIKE
    'x%'` predicate inside SQL the caller later runs through `sql % iv`
    produced 'not enough arguments for format string' and 500'd the live
    funnel inside one deploy. Both predicates must stay in their regex form."""
    for iv in ("30 days",):
        for flag in (False, True):
            sql = human_acted_count_sql(iv, include_self_traffic=flag)
            assert "%" not in sql, sql


def test_the_definition_block_is_a_copy():
    """A consumer that edits what it renders must not edit the canon for every
    other request in the same worker process."""
    a = human_acted_definition()
    a["definition_changelog"][1] = "tampered"
    a["definition_version"] = 99
    assert HUMAN_ACTED_DEFINITION_CHANGELOG[1] != "tampered"
    assert human_acted_definition()["definition_version"] == \
        HUMAN_ACTED_DEFINITION_VERSION
