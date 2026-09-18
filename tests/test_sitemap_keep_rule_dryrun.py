"""The Step 2 keep-or-drop dry-run must never report a removal plan it could
not measure.

The failure this file exists to prevent is specific and it is silent: the
impression side of the rule lives in `seo_proven_pages`, and a table that is
missing, empty or simply not being refreshed looks EXACTLY like "no facility URL
earned an impression". Read that way, the rule drops every capacity-thin URL in
the sitemap — thousands of live pages — and the number would be quoted in a PR
body as a measurement.

So every test here is about a refusal firing, and each one is mutation-checked:
breaking the guard in the script must turn the matching test red.
"""
import datetime
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import sitemap_keep_rule_dryrun as kr  # noqa: E402
import check_sitemap_selfcanon as csc  # noqa: E402

TODAY = datetime.date.today()


class Cur:
    """Answers exactly the statements under test and RAISES on anything else.

    ★ A fake that answers whatever it is asked is how a suite goes green on a
      query Postgres would have rejected. This one is deliberately NARROWER than
      psycopg2, never wider: an unexpected statement is an error, not an empty
      result that reads as "nothing found".
    """

    def __init__(self, table_exists=True, rows=(), columns=()):
        self.table_exists = table_exists
        self.rows = list(rows)
        self.columns = set(columns)
        self.sql_seen = []
        self._out = []

    def execute(self, sql, args=None):
        s = " ".join(str(sql).split()).lower()
        self.sql_seen.append(s)
        if "to_regclass" in s:
            self._out = [("public.seo_proven_pages",)] if self.table_exists \
                else [(None,)]
        elif "from seo_proven_pages" in s:
            self._out = list(self.rows)
        elif "information_schema.columns" in s:
            wanted = set(args[1]) if args and len(args) > 1 else set()
            self._out = [(c,) for c in sorted(self.columns & wanted)]
        elif s.startswith("select '") and " from " in s:
            self._out = list(self.rows)
        else:
            raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchone(self):
        return self._out[0] if self._out else None

    def fetchall(self):
        return list(self._out)


def _fams(gated=0, ai=0, extra_ai_only=0):
    """Family sets shaped like the live artefact: gated is a STRICT SUBSET of
    the ungated AI family (#4459 pins that as a set comparison)."""
    g = {f"g{i}" for i in range(gated)}
    a = g | {f"a{i}" for i in range(extra_ai_only)}
    if ai:
        a |= {f"x{i}" for i in range(ai)}
    return {"gated": g, "ai": a, "other": set()}


# ── the impression side refuses to be read as absence ────────────────────

def test_a_missing_proven_table_is_not_zero_impressions():
    with pytest.raises(RuntimeError) as e:
        kr._proven_current(Cur(table_exists=False))
    assert "does not exist" in str(e.value)


def test_an_empty_proven_table_is_not_zero_impressions():
    with pytest.raises(RuntimeError) as e:
        kr._proven_current(Cur(rows=[]))
    assert "EMPTY" in str(e.value)


def test_a_stale_proven_table_refuses_to_classify():
    """The daily GSC refresh stopping is the live shape of this failure: the
    rows stay, so the table is neither missing nor empty, and every slug that
    has fallen out of the window silently reads as zero-impression."""
    old = TODAY - datetime.timedelta(days=kr.PROVEN_MAX_AGE_DAYS + 5)
    with pytest.raises(RuntimeError) as e:
        kr._proven_current(Cur(rows=[("slug-a", old, 12)]))
    assert "days old" in str(e.value)


def test_only_the_newest_refresh_counts_as_in_window():
    """`last_seen` is stamped CURRENT_DATE by every upsert, so the rows carrying
    MAX(last_seen) ARE the current 90-day pull. Rows from an older refresh are
    history and must not be read as current impressions."""
    stale = TODAY - datetime.timedelta(days=40)
    current, as_of, total, ratchet = kr._proven_current(
        Cur(rows=[("fresh", TODAY, 9), ("dropped-out", stale, 400)]))
    assert current == {"fresh"}
    assert as_of == TODAY and total == 2
    # the ratchet is still readable for ranking, and still not a 90-day figure
    assert ratchet["dropped-out"] == 400


# ── the rule itself ──────────────────────────────────────────────────────

def test_ai_only_urls_without_an_impression_are_the_drop_set():
    fams = _fams(gated=2100, extra_ai_only=300)
    proven = {"a0", "a1", "g0"}
    plan = kr.classify(fams, proven)
    assert plan["drop"] == {f"a{i}" for i in range(2, 300)}
    assert "g0" not in plan["drop"], "a capacity-carrying URL is never dropped"
    assert plan["keep_impression"] == proven


def test_a_gated_url_is_never_dropped_even_with_no_impressions():
    """The owner-approved bar is capacity-thin AND zero impressions. A URL the
    capacity gate publishes has passed the content bar by definition."""
    fams = _fams(gated=2100)
    assert kr.classify(fams, set())["drop"] == set()


def test_a_short_sitemap_is_a_broken_fetch_not_a_small_site():
    with pytest.raises(RuntimeError) as e:
        kr.classify(_fams(gated=10, extra_ai_only=5), set())
    assert "floor" in str(e.value)
    assert csc.MIN_URLS == 2000, "the floor has one owner: the daily checker"


def test_a_drop_share_above_the_ceiling_is_refused_as_an_input_failure():
    """An empty proven read would classify nearly the whole AI family as drop.
    That is a lost input, and it must not reach a PR body as a number."""
    fams = _fams(gated=10, extra_ai_only=2100)
    with pytest.raises(RuntimeError) as e:
        kr.classify(fams, set())
    assert "refusal line" in str(e.value)


# ── evidence collection degrades loudly, never silently ──────────────────

def test_the_group_detail_probe_names_absent_columns_instead_of_failing():
    """`facilities.discovered_twin_id` only exists once v4's schema hook has
    run. Selecting it unprobed fails the whole query and costs every residual
    group its evidence; the probe must report it absent and carry on."""
    cur = Cur(columns={"address"})
    detail, missing = kr._group_detail(cur, ["some-slug"])
    assert missing["facilities"] == ["discovered_twin_id"]
    assert missing["discovered_facilities"] == ["merged_facility_id"]
    assert any("null as discovered_twin_id" in s for s in cur.sql_seen)
    assert detail == {}


def test_hub_pages_are_not_counted_as_published_facility_urls(monkeypatch):
    """`_FAC` matches everything under /facilities/, and the static shard
    carries the HUB pages (/facilities/in/<market>, /facilities/<country>/<n>).
    They have no facility row, so counting them inflates the published total and
    lands every one of them in `unresolved`. Measured on the first live run:
    19,326 published against a walked 19,016, and unresolved_slugs 311 — which
    is exactly the hub-page count, not 311 broken URLs.
    """
    pages = {
        "https://dchub.cloud/sitemap.xml":
            "<loc>https://dchub.cloud/sitemap-static.xml</loc>"
            "<loc>https://dchub.cloud/sitemap-ai-facilities-1.xml</loc>",
        "https://dchub.cloud/sitemap-static.xml":
            "<loc>https://dchub.cloud/facilities/in/us-virginia</loc>"
            "<loc>https://dchub.cloud/facilities/us/2</loc>"
            "<loc>https://dchub.cloud/facilities/real-profile-aabbccdd</loc>",
        "https://dchub.cloud/sitemap-ai-facilities-1.xml":
            "<loc>https://dchub.cloud/facilities/another-one-11223344</loc>",
    }
    monkeypatch.setattr(csc, "_get", lambda url, timeout=60: pages[url])
    fams = kr._shard_membership("https://dchub.cloud/sitemap.xml")
    assert fams["other"] == {"real-profile-aabbccdd"}
    assert fams["ai"] == {"another-one-11223344"}
    assert not any("/" in s for fam in fams.values() for s in fam)


def test_the_summary_states_the_drop_count():
    """The workflow renders this string into the step summary, and a summary
    that omits the number the removal PR quotes is worse than none."""
    out = {
        "published_facility_urls": 19016,
        "families": {"gated": 6924, "ai": 19015, "ai_only": 12091},
        "impressions": {"as_of": "2026-09-15", "in_window_slugs": 21199,
                        "rows_in_table": 22000},
        "thin": {"contentless_published": 0, "contentless_slugs_total": 1480},
        "duplicates": {"groups": 35, "surplus_urls": 41, "unresolved_slugs": 2,
                       "detail_error": None},
        "rule": {"keep_by_impression": 5000,
                 "drop_candidates_capacity_thin_zero_impression": 9100,
                 "keep_total_after_drop": 9916},
    }
    md = kr.markdown_summary(out)
    assert "9100" in md and "19016" in md and "35" in md


# ── effect, not intent ───────────────────────────────────────────────────
#
# ★★★ THE GAP THIS CLOSES. This script measured what the rule WOULD drop and
#     stayed green for two days while the rule was dead code in production —
#     a NameError inside its own fail-open (be#4722). Both numbers were already
#     in one JSON and nothing subtracted them:
#
#         2026-09-17 10:45Z   families.ai 19,171   drop_candidates 5,795
#         2026-09-18 05:22Z   production: "keep rule NOT applied"
#
#     "DROP candidates: 5,795" is what a healthy run printed BEFORE #4641
#     merged — the pending win. The number's MEANING changed when the rule
#     shipped; the instrument did not, so the alarm still read as the to-do
#     list. See [[feedback_measured_one_pattern_shipped_another]].

def _out(effect):
    """The smallest `out` markdown_summary will render, carrying a real effect
    block. Deliberately NOT a copy of measure()'s literal: this asserts the
    summary reads the verdict FIELD, so the words and the workflow's exit
    status cannot drift apart."""
    return {
        "published_facility_urls": 19172,
        "families": {"gated": 6942, "ai": effect["served_ai_family"],
                     "other_shards": 0, "ai_only": 12229,
                     "gated_outside_ai": 1},
        "impressions": {"as_of": "2026-09-17", "in_window_slugs": 25458,
                        "rows_in_table": 26339},
        "thin": {"contentless_published": 0, "contentless_slugs_total": 1559},
        "duplicates": {"groups": 0, "surplus_urls": 0, "unresolved_slugs": 0,
                       "detail_error": None},
        "rule": {"keep_by_impression": 12691,
                 "drop_candidates_capacity_thin_zero_impression":
                     effect["drop_candidates_still_served"],
                 "keep_total_after_drop": 13377},
        "effect": effect,
    }


def test_a_served_artefact_full_of_drop_candidates_means_the_rule_is_not_running():
    """The live 2026-09-18 shape: 19,145 served, 5,795 of them droppable."""
    ai = {f"a{i}" for i in range(19145)}
    drop = {f"a{i}" for i in range(5795)}
    e = kr.effect_block(ai, drop)
    assert e["rule_appears_applied"] is False
    assert e["drop_candidates_still_served"] == 5795
    assert e["expected_ai_family_after_rule"] == 19145 - 5795
    assert "THE RULE IS NOT RUNNING" in kr.markdown_summary(_out(e))


def test_an_applied_rule_leaves_no_drop_candidates_in_the_artefact():
    """Production emits gated ∪ proven, so once it runs the served artefact
    structurally cannot contain a capacity-thin zero-impression URL."""
    ai = {f"a{i}" for i in range(13377)}
    e = kr.effect_block(ai, set())
    assert e["rule_appears_applied"] is True
    assert e["expected_ai_family_after_rule"] == 13377
    assert "rule IS applied" in kr.markdown_summary(_out(e))


def test_predicate_skew_below_the_ceiling_is_not_an_outage():
    """This script's `proven` read and main.py's are derived separately, and a
    rebuild is up to 4h stale. A small residual must not page anyone."""
    ai = {f"a{i}" for i in range(13377)}
    drop = {f"a{i}" for i in range(kr.EFFECT_MAX_STILL_SERVED)}
    assert kr.effect_block(ai, drop)["rule_appears_applied"] is True
    # ★ the extra member must be IN the served family — a drop candidate that
    #   is not served proves nothing about whether the rule ran.
    drop.add(f"a{kr.EFFECT_MAX_STILL_SERVED}")
    assert kr.effect_block(ai, drop)["rule_appears_applied"] is False


def test_the_ceiling_sits_between_skew_and_the_known_defect():
    """A ceiling at or above the defect signature could never have caught it;
    one at zero would fire on ordinary churn. Pin both sides."""
    assert kr.EFFECT_MAX_STILL_SERVED < 5795, "blind to the 2026-09-16 defect"
    assert kr.EFFECT_MAX_STILL_SERVED > 0, "would fire on any predicate skew"


def test_a_drop_candidate_outside_the_ai_family_is_not_counted_as_served():
    """Only what is ACTUALLY in the served shards can prove the rule is off."""
    e = kr.effect_block({"a0", "a1"}, {"not-served-at-all"})
    assert e["drop_candidates_still_served"] == 0
    assert e["rule_appears_applied"] is True
