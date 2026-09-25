"""routes/brain_lessons.py — the compiler is pure, so every rule is a test.

Each test names the mutation it exists to kill.
"""
import os

import pytest

from routes import brain_lessons as bl

DSN = os.environ.get("DCHUB_PG_TEST_DSN", "")


def _ev(fam, oc, file="", note=""):
    return {"family": fam, "outcome": oc, "file": file, "note": note}


# ── family ───────────────────────────────────────────────────────────
def test_family_folds_sites_into_one_problem():
    """MUTATION: return the whole label → WACM and WAUW become two families
    and neither ever reaches MIN_GRADED."""
    assert bl.family_of("iso_metric_count_zero_24h:WACM") == "iso_metric_count_zero_24h"
    assert bl.family_of("ISO_metric_count_zero_24h:wauw") == "iso_metric_count_zero_24h"
    assert bl.family_of("  csp_violation_recurring /pricing") == "csp_violation_recurring"
    assert bl.family_of("") == "" and bl.family_of(None) == ""
    assert bl.family_of(":::") == ""


# ── verdicts ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("w,f,v", [
    (0, 0, "thin"), (1, 1, "thin"),          # MUTATION: MIN_GRADED 3 → 0
    (0, 3, "fails"), (1, 3, "fails"),        # 25% ≤ 30%
    (3, 0, "works"), (7, 3, "works"),        # 70% ≥ 70%
    (2, 2, "mixed"), (5, 5, "mixed"),
])
def test_verdict_bands(w, f, v):
    assert bl.verdict_for(w, f) == v


def test_a_failing_family_says_do_not_repeat_and_names_the_files():
    events = [_ev("hardcoded_hero_stat:/p", "failed", "static/a.js", "still firing")] * 3 \
        + [_ev("hardcoded_hero_stat:/q", "failed", "static/b.js")]
    l = bl.compile_lessons(events)["hardcoded_hero_stat"]
    assert l["verdict"] == "fails" and l["graded"] == 4
    assert l["success_rate"] == 0.0
    assert "did NOT hold 4 of 4" in l["guidance"]
    assert "Do not repeat" in l["guidance"]
    assert l["files_failed"] == ["static/a.js", "static/b.js"]   # de-duplicated
    assert "still firing" in l["guidance"]


def test_a_working_family_says_keep_the_shape():
    l = bl.compile_lessons([_ev("fam_x", "worked", "a.py")] * 3)["fam_x"]
    assert l["verdict"] == "works" and "keep the same shape" in l["guidance"]
    assert l["files_worked"] == ["a.py"] and l["files_failed"] == []


def test_human_rejection_is_carried_even_on_a_thin_family():
    """MUTATION: gate the rejection line on verdict != thin → the one real
    human 'no' this family has is silently dropped."""
    l = bl.compile_lessons([_ev("fam_y", "rejected", note="wrong file")])["fam_y"]
    assert l["verdict"] == "thin"
    assert "A human closed 1 proposal(s)" in l["guidance"]
    assert "wrong file" in l["guidance"]


def test_guard_refusals_need_two_before_they_teach():
    one = bl.compile_lessons([_ev("fam_z", "refused")])["fam_z"]
    two = bl.compile_lessons([_ev("fam_z", "refused")] * 2)["fam_z"]
    assert one["guidance"] == ""
    assert "refused by the deterministic guards" in two["guidance"]


def test_a_decline_is_not_told_it_hallucinated():
    """The first live compile told 64 inspector_l22_handoff DECLINES (the
    model returned an empty edit) that they had claimed a syntax/SQLite bug.
    MUTATION: map 'refused' to REFUSED again."""
    l = bl.compile_lessons([_ev("inspector_l22_handoff", "declined")] * 3)
    g = l["inspector_l22_handoff"]["guidance"]
    assert "declined 3 times" in g and "config, data or product" in g
    assert "syntax" not in g and "SQLite" not in g
    assert bl._GUARD_OUTCOME["refused"] == "declined"
    assert bl._GUARD_OUTCOME["rejected_false_syntax_claim"] == "refused"
    assert bl._GUARD_OUTCOME["rejected_sqlite_hallucination"] == "refused"


@pytest.mark.parametrize("label", ["https://dchub.cloud/x", "dchub", "table",
                                   "ai_interconnection.py", "static/app_main.js",
                                   "ops_runbook.md"])
def test_urls_bare_words_and_filenames_are_not_families(label):
    """MUTATION: drop the underscore / extension filter."""
    assert bl.family_of(label) == ""


def test_nothing_to_teach_is_empty_not_filler():
    """A thin family with one success says NOTHING — an always-present hint
    trains the drafter to skim past it."""
    assert bl.compile_lessons([_ev("fam_q", "worked")])["fam_q"]["guidance"] == ""


def test_unplaceable_events_do_not_move_a_verdict():
    """MUTATION: count unknown outcomes as failed."""
    events = [_ev("fam_a", "worked")] * 3 + [_ev("fam_a", "banana"), _ev("", "failed"),
                                         None, {"outcome": "failed"}]
    l = bl.compile_lessons(events)
    assert list(l) == ["fam_a"] and l["fam_a"]["counts"]["failed"] == 0


def test_notes_are_only_taken_from_failures_and_rejections():
    l = bl.compile_lessons([_ev("fam_n", "worked", note="SUCCESS NOTE")] * 3
                           + [_ev("fam_n", "rejected", note="human said no")])["fam_n"]
    assert "SUCCESS NOTE" not in l["guidance"]
    assert "human said no" in l["guidance"]


# ── the read path ────────────────────────────────────────────────────
def test_lessons_for_formats_a_hint_for_the_labels_family(monkeypatch):
    monkeypatch.setattr(bl, "_load_guidance",
                        lambda: {"csp_violation_recurring": "Do not repeat X."})
    h = bl.lessons_for("csp_violation_recurring:/pricing")
    assert "LESSONS FROM PAST ATTEMPTS ON `csp_violation_recurring`" in h
    assert "Do not repeat X." in h
    assert bl.lessons_for("other:/x") == ""


def test_lessons_for_fails_open(monkeypatch):
    """MUTATION: let the exception escape → a lessons outage breaks L5."""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(bl, "_load_guidance", boom)
    assert bl.lessons_for("csp_violation_recurring") == ""


def test_kill_switch(monkeypatch):
    monkeypatch.setattr(bl, "_load_guidance", lambda: {"a": "g"})
    monkeypatch.setenv("BRAIN_LESSONS_DISABLE", "1")
    assert bl.lessons_for("a") == ""


def test_agent_pr_closed_is_not_double_counted():
    """The squasher lane writes a closed PR to brain_review_decisions; if the
    agent source ALSO mapped pr_closed, every human 'no' would count twice."""
    assert "pr_closed" not in bl._AGENT_OUTCOME


# ── real Postgres: the SQL actually runs and the sources join ────────
@pytest.fixture
def pg(monkeypatch):
    if not DSN:
        pytest.skip("set DCHUB_PG_TEST_DSN to a disposable Postgres")
    if any(h in DSN for h in ("neon.tech", "railway", "rlwy", "amazonaws")):
        pytest.fail("DCHUB_PG_TEST_DSN looks managed — point it at a throwaway")
    import psycopg2
    # Production's raw connector is AUTOCOMMIT (routes/ai_reach.py); the
    # module's own _conn must undo that or every SAVEPOINT fails.
    from routes import ai_reach

    def _autocommit():
        c = psycopg2.connect(DSN)
        c.autocommit = True
        return c
    monkeypatch.setattr(ai_reach, "_conn", _autocommit)
    c = psycopg2.connect(DSN)
    c.autocommit = True
    tables = ("brain_lessons", "brain_fix_outcomes", "brain_proposed_code_fixes",
              "brain_autopilot_actions",
              "brain_issue_persistence", "squasher_work_queue",
              "brain_review_decisions")
    with c.cursor() as cur:
        for t in tables:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        cur.execute("""CREATE TABLE brain_proposed_code_fixes (
            id BIGSERIAL PRIMARY KEY, loop_name TEXT NOT NULL,
            file_path TEXT NOT NULL, search_text TEXT NOT NULL DEFAULT '',
            finding_class TEXT, issue_key TEXT)""")
        cur.execute("""CREATE TABLE brain_fix_outcomes (
            id BIGSERIAL PRIMARY KEY, proposal_id BIGINT,
            proposal_kind TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            checked_at TIMESTAMPTZ, still_broken BOOLEAN, evidence_note TEXT)""")
        cur.execute("""CREATE TABLE brain_autopilot_actions (
            id BIGSERIAL PRIMARY KEY, finding_issue TEXT NOT NULL,
            pattern_name TEXT NOT NULL)""")
        cur.execute("""CREATE TABLE brain_issue_persistence (
            id BIGSERIAL PRIMARY KEY, issue_label TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '', last_seen_at TIMESTAMPTZ NOT NULL
            DEFAULT NOW(), last_outcome TEXT)""")
        cur.execute("""CREATE TABLE squasher_work_queue (
            id BIGSERIAL PRIMARY KEY, finding_key TEXT NOT NULL,
            reason TEXT, requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            agent_state TEXT, agent_finished_at TIMESTAMPTZ,
            agent_summary TEXT)""")
        cur.execute("""CREATE TABLE brain_review_decisions (
            id BIGSERIAL PRIMARY KEY, proposal_kind TEXT NOT NULL,
            issue_hash TEXT NOT NULL, issue_label TEXT, decision TEXT NOT NULL,
            reviewer_note TEXT,
            decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    yield c
    with c.cursor() as cur:
        for t in tables:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
    c.close()


def test_refresh_compiles_all_four_sources_on_postgres(pg):
    with pg.cursor() as cur:
        for i, broken in enumerate((True, True, True, False)):
            cur.execute("INSERT INTO brain_proposed_code_fixes (loop_name,"
                        " file_path, finding_class) VALUES ('l', %s,"
                        " 'shadowed_route') ON CONFLICT DO NOTHING RETURNING id", (f"routes/r{i}.py",))
            pid = cur.fetchone()[0]
            cur.execute("INSERT INTO brain_fix_outcomes (proposal_id,"
                        " proposal_kind, checked_at, still_broken) VALUES"
                        " (%s, 'code', NOW(), %s)", (pid, broken))
        # a PR-ledger row with the same id must NOT join (kind != code)
        cur.execute("INSERT INTO brain_fix_outcomes (proposal_id, proposal_kind,"
                    " checked_at, still_broken) VALUES (1, 'pr', NOW() ON CONFLICT DO NOTHING, TRUE)")
        # autopilot outcomes: proposal_id is brain_autopilot_actions.id — the
        # id space the code-only join never reached (173 of 189 live rows).
        for held in (True, True, True, False):
            cur.execute("INSERT INTO brain_autopilot_actions (finding_issue,"
                        " pattern_name) VALUES ('competitor_announcement:dchawk',"
                        " 'flag_for_review') ON CONFLICT DO NOTHING RETURNING id")
            aid = cur.fetchone()[0]
            cur.execute("INSERT INTO brain_fix_outcomes (proposal_id,"
                        " proposal_kind, checked_at, still_broken) VALUES"
                        " (%s, 'autopilot', NOW(), %s)", (aid, not held))
        cur.execute("INSERT INTO brain_issue_persistence (issue_label,"
                    " last_outcome) VALUES ('boot_syntax:x', 'refused') ON CONFLICT DO NOTHING,"
                    " ('boot_syntax:y', 'rejected_false_syntax_claim'),"
                    " ('boot_syntax:z', 'pr_opened')")
        cur.execute("INSERT INTO squasher_work_queue (finding_key, agent_state,"
                    " agent_finished_at, agent_summary) VALUES"
                    " ('js_field_fallback_missing:/a', 'merged_unverified', NOW(), 'still live'),"
                    " ('js_field_fallback_missing:/b', 'pr_closed', NOW(), 'x')")
        cur.execute("INSERT INTO brain_review_decisions (proposal_kind,"
                    " issue_hash, issue_label, decision, reviewer_note) VALUES"
                    " ('code', 'h', 'js_field_fallback_missing:/b', 'reject', 'no'),"
                    " ('code', 'h', 'js_field_fallback_missing:/c', 'approve', '')")
    out = bl.refresh()
    assert out["ok"], out
    assert out["sources"] == {"l5_fix_outcomes": 4, "autopilot_fix_outcomes": 4,
                              "l5_guard_refusals": 2, "squasher_agent": 2,
                              "human_rejections": 1}
    cov = out["outcome_coverage"]
    # 4 code + 4 autopilot joined; the 'pr' row is graded but no source reads it
    assert (cov["joined"], cov["graded_total"]) == (8, 9), cov
    assert cov["unjoined_kinds"] == ["pr"]
    with pg.cursor() as cur:
        cur.execute("SELECT family, verdict, graded, counts FROM brain_lessons"
                    " ORDER BY family")
        rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert rows["shadowed_route"][:2] == ("fails", 4)
    # MUTATION: drop the autopilot source → this family never appears
    assert rows["competitor_announcement"][:2] == ("works", 4)
    bs = rows["boot_syntax"][2]
    assert (bs["refused"], bs["declined"]) == (1, 1)
    js = rows["js_field_fallback_missing"][2]
    assert js["failed"] == 1 and js["rejected"] == 1   # not 2: no double count
    # the cache was cleared, so the read path sees the new page
    assert "did NOT hold 3 of 4" in bl.lessons_for("shadowed_route /api/x")


def test_refresh_keeps_last_pages_when_every_source_is_unreadable(pg):
    """MUTATION: drop the all-None guard → an outage empties the lessons."""
    with pg.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS brain_lessons (family TEXT"
                    " PRIMARY KEY, verdict TEXT NOT NULL, graded INTEGER NOT"
                    " NULL DEFAULT 0, success_rate REAL, counts JSONB NOT NULL"
                    " DEFAULT '{}'::jsonb, guidance TEXT NOT NULL DEFAULT '',"
                    " detail JSONB NOT NULL DEFAULT '{}'::jsonb, compiled_at"
                    " TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        cur.execute("INSERT INTO brain_lessons (family, verdict, guidance)"
                    " VALUES ('keep', 'fails', 'g')")
        for t in ("brain_fix_outcomes", "brain_issue_persistence",
                  "squasher_work_queue", "brain_review_decisions"):
            cur.execute(f"DROP TABLE {t}")
    out = bl.refresh()
    assert out["ok"] is False and "kept last pages" in out["error"]
    with pg.cursor() as cur:
        cur.execute("SELECT family FROM brain_lessons")
        assert cur.fetchall() == [("keep",)]


# ── coverage: an unjoined id space must be visible ───────────────────
def test_coverage_counts_what_the_joins_reached():
    cov = bl.outcome_coverage({"code": 16, "autopilot": 173},
                              {"l5_fix_outcomes": 16, "autopilot_fix_outcomes": 170})
    assert (cov["joined"], cov["graded_total"], cov["pct"]) == (186, 189, 98.4)
    assert cov["unjoined_kinds"] == []


def test_coverage_names_a_kind_no_source_reads():
    """The 2026-09-24 shape: 'autopilot' graded, nothing joining it.
    MUTATION: drop 'autopilot' from joined_by → it must be named here."""
    cov = bl.outcome_coverage({"code": 16, "autopilot": 173, "text": 2},
                              {"l5_fix_outcomes": 16, "autopilot_fix_outcomes": None})
    assert cov["joined"] == 16 and cov["unjoined_kinds"] == ["text"]


def test_coverage_is_none_when_unreadable():
    assert bl.outcome_coverage(None, {}) is None
    assert bl.outcome_coverage({}, {})["pct"] is None
