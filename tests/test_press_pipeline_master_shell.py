"""Press pipeline stage-to-stage loss (2026-08-10).

Pins the shell that would have caught the five-day publish outage, plus the
three fixes that shipped with it. Measured live 2026-08-10:

    5.6d since last publish · 1 of 20 reviews written · 2 approved releases
    with no row · 'midland-odessa' 19 of 33 attempts (58%) · 4 held drafts
    posted to social

Behaviour-tested where the code runs without a DB (story_stem, _verdict),
source-pinned where it needs one (the master-tick's SQL, the drain SELECTs),
in the same style as tests/test_seo_index_hygiene.py.

★ A master shell has TWO independently-wireable layers and either can be
  silently absent: the blueprint mount and the scheduled tick. A shell with a
  tick nobody drives is a dead scoreboard that still returns 200 when a human
  opens it. test_shell_is_wired_at_both_layers is the one that matters.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.press_pipeline_master_shell as sh  # noqa: E402

MAIN_SRC = (ROOT / "main.py").read_text()
HEARTBEAT_SRC = (ROOT / "routes" / "cron_heartbeat.py").read_text()
PUBLISHER_SRC = (ROOT / "content_publisher.py").read_text()
MARKETING_SRC = (ROOT / "routes" / "marketing_engine.py").read_text()
GATE_SRC = (ROOT / "routes" / "media_editorial_gate.py").read_text()


# ── the shell must actually be driven ────────────────────────────────────

def test_shell_is_wired_at_both_layers():
    """Mount AND driver. A tick nobody fires is a dead scoreboard."""
    assert "press_pipeline_master_shell_bp" in MAIN_SRC, "blueprint never mounted"
    assert "app.register_blueprint(press_pipeline_master_shell_bp)" in MAIN_SRC
    assert "/api/v1/admin/press-pipeline/master-tick" in HEARTBEAT_SRC, (
        "tick has no driver in cron_heartbeat._DISPATCH — it would only ever "
        "run when a human opened the dashboard")


def test_tick_runs_after_the_composer_window():
    """The 24h ratios must see a finished day. The 08-09 storm ran
    15:34-17:44 UTC, so an early-afternoon tick would sample it half-done."""
    i = HEARTBEAT_SRC.index("press_pipeline_master_tick_daily")
    window = HEARTBEAT_SRC[i:i + 400]
    assert "now.hour == 19" in window, "tick is not on the post-composer slot"


# ── lane D: the stem is what makes the repeat storm visible ──────────────

MEASURED_0809_SLUGS = [
    "2026-08-09-midland-odessa-86-excess-power-constraint-23",
    "2026-08-09-midland-odessa-tops-dcpi-excess-power-index",
    "2026-08-09-midland-odessa-86-excess-power-dcpi",
    "2026-08-09-midland-odessa-86-excess-power-lowest-constraint",
    "2026-08-09-midland-odessa-dcpi-86-excess-power-time-to-power",
    "midland-odessa-86-excess-power-constraint-bottleneck-2026-08-09",
    "2026-08-09-midland-odessa-tops-dcpi-excess-power-86",
    "midland-odessa-86-excess-power-2026-08-09",
    "auto-2026-08-09-midland-odessa-86-excess-power",
    "auto-2026-08-09-midland-odessa-dual-strength-ercot",
    "2026-08-09-midland-odessa-86-excess-power-ercot-constraint-advantage",
    "2026-08-09-midland-odessa-excess-power-leader",
]


def test_story_stem_collapses_the_measured_repeat_storm():
    """All twelve are the same story under different slugs. Anything that
    reports more than one stem here hands the metric back to the noise."""
    stems = {sh.story_stem(s) for s in MEASURED_0809_SLUGS}
    assert stems == {"midland-odessa"}, f"storm split into {stems}"


def test_story_stem_separates_genuinely_different_stories():
    """Non-vacuity: a stem that collapsed everything would pass the test
    above and make lane D permanently red."""
    distinct = {
        sh.story_stem("auto-2026-08-09-japan-korea-brazil-grid-scoreboard"),
        sh.story_stem("2026-08-09-perplexity-validates-dcpi-daily-refresh"),
        sh.story_stem("2026-08-04-coreweave-360mw-indonesia-offshore-grid"),
        sh.story_stem("2026-08-08-meta-15573-mw-92-facilities"),
        sh.story_stem("midland-odessa-86-excess-power-2026-08-09"),
    }
    assert len(distinct) == 5, f"distinct stories collapsed: {distinct}"
    assert "" not in distinct


@pytest.mark.parametrize("slug", [
    "2026-08-09-midland-odessa-86-excess-power",
    "midland-odessa-86-excess-power-2026-08-09",
    "auto-2026-08-09-midland-odessa-86-excess-power",
    "auto_20260809_midland_odessa_86_excess_power",
])
def test_story_stem_is_insensitive_to_date_position_and_prefix(slug):
    """The composer varies exactly these. If the stem is not invariant to
    them the storm reads as N distinct stories."""
    assert sh.story_stem(slug) == "midland-odessa"


# ── lane scoring ─────────────────────────────────────────────────────────

def _healthy():
    return {
        "publish_silence_days": 0.4, "reviews_24h": 3, "press_rows_24h": 3,
        "write_ratio_24h": 1.0, "approved_unwritten_7d": 0, "attempts_7d": 12,
        "distinct_stems_7d": 11, "distinct_ratio_7d": 0.917,
        "top_repeated_stem": "cheyenne-wecc", "top_repeated_count": 2,
        "top_stem_share_7d": 0.167, "draft_social_leak_7d": 0, "unmeasured": [],
    }


def test_healthy_pipeline_is_green():
    """Non-vacuity for every RED test below."""
    assert sh.tier2_score(_healthy())["verdict"] == "green"


def test_the_measured_0809_shape_is_red_on_all_four_counts():
    m = _healthy()
    m.update({
        "publish_silence_days": 5.6, "reviews_24h": 20, "press_rows_24h": 1,
        "write_ratio_24h": 0.05, "approved_unwritten_7d": 2, "attempts_7d": 33,
        "distinct_stems_7d": 13, "distinct_ratio_7d": 0.394,
        "top_repeated_stem": "midland-odessa", "top_repeated_count": 19,
        "top_stem_share_7d": 0.576, "draft_social_leak_7d": 4,
    })
    sc = sh.tier2_score(m)
    assert sc["verdict"] == "red"
    joined = " | ".join(sc["reasons"])
    assert "since last publish" in joined
    assert "transaction loss" in joined
    assert "approved release" in joined
    assert "composer stuck" in joined
    assert "posted to social" in joined


@pytest.mark.parametrize("mutation,marker", [
    ({"publish_silence_days": 5.6}, "since last publish"),
    ({"reviews_24h": 20, "press_rows_24h": 1, "write_ratio_24h": 0.05},
     "transaction loss"),
    ({"approved_unwritten_7d": 1}, "approved release"),
    ({"top_repeated_count": 19, "top_stem_share_7d": 0.576}, "composer stuck"),
    ({"draft_social_leak_7d": 1}, "posted to social"),
])
def test_each_lane_reddens_independently(mutation, marker):
    """One broken stage must be enough. Requiring several to coincide is how
    a single-stage outage hides inside an aggregate score."""
    m = _healthy()
    m.update(mutation)
    sc = sh.tier2_score(m)
    assert sc["verdict"] == "red", f"{mutation} did not go red"
    assert marker in " | ".join(sc["reasons"])


def test_unmeasured_is_never_green():
    """A lane that could not run must not read as a lane that passed — the
    exact lie that let a five-day outage look healthy."""
    m = _healthy()
    m["unmeasured"] = ["compose_to_write"]
    assert sh.tier2_score(m)["verdict"] == "amber"


def test_thin_windows_do_not_trip_the_ratios():
    """Two attempts in a quiet week is not a repetition problem."""
    m = _healthy()
    m.update({"attempts_7d": 2, "distinct_stems_7d": 1,
              "distinct_ratio_7d": 0.5, "top_repeated_count": 2,
              "top_stem_share_7d": 1.0, "reviews_24h": 1,
              "press_rows_24h": 0, "write_ratio_24h": 0.0})
    assert sh.tier2_score(m)["verdict"] == "green"


# ── the three fixes ──────────────────────────────────────────────────────

def test_composer_memory_reads_attempts_not_only_successes():
    """auto_press_releases gains a row only when the write COMMITS, so on a
    day when 19 of 20 writes roll back it is nearly empty and the composer
    re-proposes the same story. The review table survives the rollback."""
    assert "_recent_attempt_titles" in MARKETING_SRC
    assert "FROM media_editorial_reviews" in MARKETING_SRC
    # both consumers of the do-not-repeat context, not just one
    i = MARKETING_SRC.index("def _recent_titles")
    assert "_recent_attempt_titles(" in MARKETING_SRC[i:i + 2000]
    j = MARKETING_SRC.index("def _recent_market_names")
    assert "_recent_attempt_titles(" in MARKETING_SRC[j:j + 2600]


def test_editorial_gate_records_the_attempted_title():
    """The title is what the composer reads back; a slug-only row forces a
    lossy de-slugify."""
    assert "ADD COLUMN IF NOT EXISTS title TEXT" in GATE_SRC
    assert "title=title" in GATE_SRC
    # every _record_review call site carries it — a missed one silently
    # writes NULL and that attempt drops out of the composer's memory
    assert GATE_SRC.count("title=title") == GATE_SRC.count(
        "_record_review(slug_for_log")


def test_write_failure_is_no_longer_silent():
    """press_releases + its integrity review + the audit row are ONE
    transaction; landing in the except discards all three, so a failed write
    leaves no trace in any table a dashboard reads."""
    i = MARKETING_SRC.index("def _write_release")
    body = MARKETING_SRC[i:i + 14000]
    assert 'note_swallowed_write("press_releases"' in body
    assert "marketing_engine._write_release" in body
    # the error string must name the exception TYPE, not just its message
    assert "type(e).__name__" in body


def test_held_drafts_are_not_queued_for_social():
    i = MARKETING_SRC.index("def _write_release")
    body = MARKETING_SRC[i:i + 14000]
    assert "_queue_distribution_posts" in body
    q = body.index("_queue_distribution_posts(rel, press_id, today)")
    guard = body[max(0, q - 1400):q]
    assert "if not _publish" in guard, "distribution queued regardless of verdict"
    assert "PRESS_DRAFT_SOCIAL_FANOUT" in guard, "no operator escape hatch"


def test_every_social_drain_carries_the_published_gate():
    """All four candidate SELECTs — LinkedIn platform-scoped, the LinkedIn
    any-platform fallback, Twitter and Bluesky. Patching three of four leaves
    the leak open on the fourth."""
    # 5 candidate SELECTs + 6 queue-depth probes.
    # ★ The fifth candidate SELECT is the one a human review misses: Bluesky's
    #   cross-platform FALLBACK ("any approved post not yet on bluesky"),
    #   which re-uses LinkedIn-targeted rows and so re-opens the leak the
    #   platform-scoped gate just closed. The exhaustive scan below is what
    #   found it, not the count.
    # ★ The probes matter too: an ungated depth alarm would count rows the
    #   gated drain will never take and warn "N approved posts going DARK"
    #   forever — a false alarm created by the fix itself, which is the same
    #   class of misleading metric this shell exists to kill.
    assert PUBLISHER_SRC.count("press_release_id IS NULL OR EXISTS") == 11
    # every gate resolves against press_releases.published, not some other
    # column that merely looks like a publish flag
    assert PUBLISHER_SRC.count("press_release_id IS NULL OR EXISTS") == \
        PUBLISHER_SRC.count("AND p.published = TRUE))")
    # every `status = 'approved'` read carries one — a missed site is a leak
    # (candidate SELECT) or a false alarm (depth probe)
    import re as _re
    # WHERE only — `SET status = 'approved'` is the human-approve write path
    for m in _re.finditer(r"WHERE status = 'approved'", PUBLISHER_SRC):
        tail = PUBLISHER_SRC[m.start():m.start() + 460]
        assert "press_release_id IS NULL OR EXISTS" in tail, (
            "ungated approved-read at offset "
            f"{m.start()}: {PUBLISHER_SRC[m.start():m.start() + 120]!r}")
    # non-press posts (evergreen/showcase) must keep flowing — gating them
    # too would silence the feed entirely rather than just the held drafts
    assert "press_release_id IS NULL OR" in PUBLISHER_SRC


def test_shell_is_read_only():
    """No actuator. This shell may record a red snapshot; it must never
    compose, publish or send."""
    import re as _re
    src = (ROOT / "routes" / "press_pipeline_master_shell.py").read_text()
    body = src[src.index("def tier1_measure"):]
    for forbidden in ("DELETE ", "publish-now", "_fire(", "requests.post"):
        assert forbidden not in body, f"shell has an actuator: {forbidden!r}"
    # A bare `UPDATE <table>` is an actuator; the `DO UPDATE SET` half of an
    # upsert into the shell's OWN snapshot table is not. Match the statement
    # form, not the word.
    assert not _re.search(r"(?<!DO )UPDATE\s+(?!SET)\w+", body), \
        "shell has a bare UPDATE statement"
    # the one write it is allowed is its own snapshot row (count STATEMENTS,
    # not prose — the module's comments discuss SQL and would inflate a raw
    # substring count)
    code = "\n".join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert code.count("INSERT INTO") == 1
    assert "INSERT INTO press_pipeline_snapshots" in body
    for tbl in ("press_releases", "social_media_posts",
                "media_editorial_reviews", "auto_press_releases"):
        assert f"INTO {tbl}" not in body, f"shell writes to {tbl}"


def test_snapshot_is_idempotent_within_the_heartbeat_window():
    """★ cron_heartbeat's window is `now.hour == 19 and now.minute < 55`, which
    fires ~11 times inside that hour BY DESIGN (narrow windows were unreliable
    under GH-cron latency, so idempotency is the endpoint's job). A plain
    append would write ~11 identical snapshots a day and turn "how many red
    days this week" into a count of heartbeats. Verified against a temp mirror
    of the real DDL: three ticks in one hour leave one row."""
    src = (ROOT / "routes" / "press_pipeline_master_shell.py").read_text()
    assert "ix_pps_hour" in src, "no unique key for the upsert to arbitrate on"
    assert "ON CONFLICT (snapshot_hour) DO UPDATE" in src
    # The hour key is BOUND, never computed inline with a quoted SQL literal:
    # regression-lint's insert-no-on-conflict scan stops at the first single
    # quote, so an inline truncation call hides the ON CONFLICT clause from it
    # and the rule fires on a correct upsert.
    assert "date_trunc(" not in src
    assert "minute=0, second=0, microsecond=0" in src
    # the arbiter column and the index must agree, or Postgres raises
    # "no unique or exclusion constraint matching the ON CONFLICT spec"
    i = src.index("CREATE UNIQUE INDEX IF NOT EXISTS ix_pps_hour")
    assert "press_pipeline_snapshots(snapshot_hour)" in src[i:i + 200]


# ── the root cause of the five-day outage ────────────────────────────────

def _lift_json_helper():
    """The SHIPPED helper, without importing marketing_engine (it pulls flask
    plus the whole engine).

    The body moved to util/json_column.py on 2026-08-10 — 23 sites repo-wide
    had the same sliced-blob-into-a-jsonb-column bug, so one implementation
    now serves all of them. util.json_column is a leaf module (stdlib json
    only), so this imports the real shipped code rather than re-exec'ing a
    copy. _asserts_delegation below pins that marketing_engine still routes
    through it — without that, these tests could pass against a helper the
    press path no longer calls."""
    import re as _re
    from util.json_column import json_for_column

    keep = _re.search(r"_PRESS_KEEP_KEYS\s*=\s*\(([^)]*)\)", MARKETING_SRC)
    assert keep, "_PRESS_KEEP_KEYS not found in marketing_engine"
    keys = tuple(k.strip().strip('"\'') for k in keep.group(1).split(",") if k.strip())
    return lambda payload, max_chars=8000: json_for_column(
        payload, max_chars, keep_keys=keys)


def test_press_helper_delegates_to_the_shared_one():
    """Non-vacuity for _lift_json_helper: if _json_for_column ever grew its own
    body again, the tests below would stop describing the shipped press path."""
    import re as _re
    body = _re.search(r"^def _json_for_column.*?\n(?=\n\ndef )",
                      MARKETING_SRC, _re.S | _re.M)
    assert body, "_json_for_column not found in marketing_engine"
    assert "json_for_column(payload, max_chars, keep_keys=_PRESS_KEEP_KEYS)" \
        in body.group(0), "press helper no longer delegates to util.json_column"


def test_oversize_payload_still_serialises_to_VALID_json():
    """★ THE ROOT CAUSE. auto_press_releases.source_data is jsonb and the old
    code sliced the SERIALISED STRING, so a cut landing mid-token produced

        InvalidTextRepresentation: invalid input syntax for type json
        DETAIL: Token ""Midland\\u2...

    That INSERT is the last statement of _write_release's single transaction,
    so its failure discarded the press_releases row and the integrity review
    with it. Reproduced verbatim in production 2026-08-10.
    """
    import json as _json
    f = _lift_json_helper()
    # em-dash forces …-style escapes near the cut, as in the live payload
    payload = {"as_of": "2026-08-10", "daily_topic": "dcpi_leader",
               "markets": [{"name": "Midland–Odessa " + "x" * 40, "i": i}
                           for i in range(200)]}
    assert len(_json.dumps(payload)) > 8000, "fixture must exceed the cap"
    out = f(payload)
    parsed = _json.loads(out)          # the assertion that matters
    assert parsed["_truncated"] is True
    assert parsed["as_of"] == "2026-08-10"
    assert parsed["_original_chars"] > 8000


def test_small_payload_is_passed_through_whole():
    """Non-vacuity: a helper that always returned the stub would pass the
    test above while destroying every audit row."""
    import json as _json
    f = _lift_json_helper()
    payload = {"as_of": "2026-08-10", "markets": [1, 2, 3]}
    assert _json.loads(f(payload)) == payload


def test_unserialisable_payload_never_raises():
    """This runs inside the composer's transaction — raising here would
    recreate the outage through a different door."""
    import json as _json
    f = _lift_json_helper()
    out = f({"conn": object()})
    _json.loads(out)


def test_no_sliced_json_string_reaches_the_audit_row():
    """The bug shape, pinned: a slice applied to the RESULT of json.dumps.
    A slice INSIDE the call truncates DATA and is always safe."""
    import ast as _ast
    tree = _ast.parse(MARKETING_SRC)
    i = MARKETING_SRC.index("def _write_release")
    j = MARKETING_SRC.index("def _queue_distribution_posts")
    bad = []
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Subscript) and isinstance(n.slice, _ast.Slice) \
           and isinstance(n.value, _ast.Call) \
           and isinstance(n.value.func, _ast.Attribute) \
           and n.value.func.attr == "dumps":
            off = sum(len(l) + 1 for l in MARKETING_SRC.splitlines()[:n.lineno - 1])
            if i <= off <= j:
                bad.append(n.lineno)
    assert not bad, f"sliced json.dumps() back in _write_release at {bad}"
