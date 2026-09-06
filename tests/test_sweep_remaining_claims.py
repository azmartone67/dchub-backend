"""The last six findings from the 2026-09-06 claim sweep.

All six are the same family as the seven already fixed: a message that stands
for more conditions than it names, or a value that doubles as its own absence.

  dcpi_alerts.subscribe      confirmed slugs the daily check can never match
  outreach_cron no-provider  a missing API key marked the whole backlog done
  outreach_cron /status      already_sent counted leads deliberately not mailed
  winback no_pitches_found   also meant timeout / non-200
  winback cooldown           a FAILED send suppressed the retry for 7 days
  winback {x:,} with '?'     a fallback that can only raise

★ THE LAST ONE WAS REFUTED BY THE ADVERSARIAL PASS AND THE REFUTATION WAS
WRONG. It was killed on the reasoning that the pitch endpoint always sets the
key, so the default never fires. But the default cannot render even in
principle -- format('?', ',') raises -- and it was found by CALLING the
renderer with a pitch that omits the key, not by reading it. Reading agreed
with reading; running disagreed with both.
"""
import ast
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import winback_outreach as wb  # noqa: E402
from routes import outreach_cron as oc  # noqa: E402
from routes import dcpi_alerts as da  # noqa: E402


# ── winback: the fallback that could only crash ───────────────────────

def test_a_missing_count_renders_rather_than_raising():
    assert wb._count(None) == "?"
    assert wb._count("nonsense") == "?"
    assert wb._count(98213) == "98,213"
    assert wb._count("4210") == "4,210"


@pytest.mark.parametrize("pitch", [
    {"platform": "Acme", "contact": "https://x.io/c"},                 # no key
    {"platform": "Acme", "contact": "https://x.io/c",
     "total_prior_calls": None},                                       # null
    {"platform": "Acme", "contact": "https://x.io/c",
     "total_prior_calls": 98213},                                      # normal
])
def test_the_briefing_renders_for_every_shape_of_that_field(pitch):
    """It raised ValueError on the first two. One of the three call sites is
    inside the send loop, so that took the whole run, not one pitch."""
    html = wb._render_operator_briefing_html(pitch)
    assert "historical calls" in html


def test_no_numeric_format_spec_still_carries_a_string_default():
    """★ AST, not text: the shape is `<expr with '?'>:,` inside an f-string.
    Bound to the FormattedValue node so a reflow cannot hide one."""
    tree = ast.parse(open(wb.__file__, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FormattedValue):
            continue
        spec = node.format_spec
        spec_txt = "".join(v.value for v in getattr(spec, "values", [])
                           if isinstance(v, ast.Constant)) if spec else ""
        if "," not in spec_txt:
            continue
        src = ast.unparse(node.value)
        if "'?'" in src or '"?"' in src:
            bad.append((node.lineno, src[:70]))
    assert not bad, f"numeric format spec with an unrenderable default: {bad}"


# ── winback: the ellipsis that claimed a truncation ───────────────────

def test_the_ellipsis_appears_only_when_the_url_was_actually_cut():
    short = wb._render_operator_briefing_html(
        {"platform": "A", "contact": "https://x.io/c"})
    long = wb._render_operator_briefing_html(
        {"platform": "A", "contact": "https://example.com/" + "p" * 90})
    s_line = [l for l in short.splitlines() if "contact form" in l][0]
    l_line = [l for l in long.splitlines() if "contact form" in l][0]
    assert "…" not in s_line, s_line
    assert "…" in l_line, l_line[:120]


# ── winback: the schedule the copy asserted ───────────────────────────

def test_the_stated_send_time_matches_the_workflow_cron():
    """★ The copy said 14:45 while the workflow fired at 14:48. Pinned to the
    cron itself, so the two cannot drift again."""
    wf = os.path.join(_ROOT, ".github/workflows/winback-weekly.yml")
    cron = re.search(r"cron:\s*'(\S+)\s+(\S+)\s+\S+\s+\S+\s+(\S+)'",
                     open(wf, encoding="utf-8").read())
    assert cron, "no cron line in winback-weekly.yml"
    minute, hour = cron.group(1), cron.group(2)
    assert wb._SCHEDULE_UTC == f"{int(hour):02d}:{int(minute):02d}", (
        f"copy says {wb._SCHEDULE_UTC}, workflow fires at {hour}:{minute}")
    html = wb._render_operator_briefing_html({"platform": "A", "contact": "x"})
    assert wb._SCHEDULE_UTC in html


# ── winback: the cooldown a failed send started ───────────────────────

def test_a_failed_send_does_not_start_the_cooldown():
    """The row is still written -- the record was never the problem -- but a
    send_failed row must not suppress the retry."""
    seen = {}

    class _Cur:
        def execute(self, sql, params=None):
            seen["sql"] = " ".join(sql.split())
            seen["params"] = params

        def fetchone(self):
            return None

    wb._platform_was_recently_sent(_Cur(), "acme", days=7)
    assert "send_failed" in seen["sql"], (
        "the cooldown counts rows regardless of whether the mail arrived")
    assert "<>" in seen["sql"] or "!=" in seen["sql"], seen["sql"]


def test_the_insert_still_records_the_failure():
    """The fix must not silence the record it reads."""
    src = open(wb.__file__, encoding="utf-8").read()
    assert '"sent" if ok else "send_failed"' in src


# ── winback: one reason per condition ─────────────────────────────────

def test_the_pitch_fetch_names_which_condition_it_hit():
    src = ast.parse(open(wb.__file__, encoding="utf-8").read())
    consts = {n.value for n in ast.walk(src)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert any("pitch_endpoint_http_" in c for c in consts), (
        "a non-200 from the pitch endpoint still reports as an empty week")
    assert any("pitch_endpoint_unreachable" in c for c in consts), (
        "an unreachable pitch endpoint still reports as an empty week")


# ── outreach_cron: a missing key must not burn the backlog ────────────

def test_no_provider_does_not_mark_the_lead_handled():
    """★ It set outreach_sent = TRUE 'so we don't keep re-evaluating', which
    permanently retired every pending lead the moment an API key went missing
    -- and /status reported them as sent."""
    # By NAME, not "the first function mentioning provider" -- that matched
    # the _provider() helper, and the test failed for a reason unrelated to
    # the defect it guards.
    fn = next(n for n in ast.walk(ast.parse(open(oc.__file__, encoding="utf-8").read()))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "process_pending")
    src = ast.unparse(fn)
    i = src.find("if not provider:")
    assert i != -1, "the no-provider branch is gone from process_pending"
    branch = src[i:i + 700]
    assert "outreach_sent = TRUE" not in branch, (
        "a missing API key still marks leads as handled: " + branch[:200])


def test_status_separates_mailed_from_merely_marked():
    src = open(oc.__file__, encoding="utf-8").read()
    for key in ("already_sent", "marked_skipped_not_mailed", "marked_handled"):
        assert f'out["{key}"]' in src, key
    i = src.index('out["already_sent"]')
    assert "sent_via_" in src[i - 200:i + 200], (
        "already_sent is still the raw outreach_sent=TRUE total")


# ── dcpi_alerts: confirm only what can fire ───────────────────────────

def test_subscribe_reports_slugs_that_can_never_match():
    src = open(da.__file__, encoding="utf-8").read()
    i = src.index("def subscribe(")
    body = src[i:src.index("\n@", i + 1)] if "\n@" in src[i:] else src[i:]
    assert "unknown_markets" in body, (
        "subscribe still confirms every slug it was handed")
    assert "published = TRUE" in body, (
        "the check does not consult the published score set the cron reads")
    assert "markets_verified" in body, (
        "the response does not say whether verification was possible")


def test_subscribe_names_what_the_cap_dropped():
    src = open(da.__file__, encoding="utf-8").read()
    assert "dropped_over_cap" in src, (
        "markets past the free-tier cap are still discarded silently; cap_note "
        "is an upsell, not a receipt")


def test_verification_failure_is_not_reported_as_verified():
    """If the lookup itself fails, the response must claim nothing."""
    src = open(da.__file__, encoding="utf-8").read()
    i = src.index("_verified = bool(_known)")
    assert "_known = set()" in src[i - 400:i], src[i - 200:i]
    j = src.index('_resp["markets_verified"] = False')
    assert "if not _verified" in src[j - 120:j]
