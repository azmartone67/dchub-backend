"""Guards for routes/registry_submission_state.py.

The invariant: `absent` may only be concluded when we ACTUALLY searched our
submission history and found nothing. On 2026-08-29 a README-only probe
concluded "absent" for two registries that already had our work in flight
(one merged in July, one an open PR), and two duplicate PRs were filed.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from routes import registry_submission_state as rss  # noqa: E402

NOW = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
LIVE = {"archived": False}


def _pr(number, state, merged=False, days_ago=0):
    return {"number": number, "url": f"https://x/{number}", "state": state,
            "merged": merged,
            "created_at": (NOW - timedelta(days=days_ago)).isoformat()}


# ── THE central invariant ─────────────────────────────────────────────
def test_unsearched_history_is_unknown_never_absent():
    v = rss.classify(LIVE, 200, None, now=NOW)
    assert v["state"] == rss.STATE_UNKNOWN, (
        "not searching our PR history must read `unknown`, never `absent` — "
        "concluding absence from a probe that cannot see submissions is the "
        "bug this module exists to prevent")
    assert v["state"] != rss.STATE_ABSENT


def test_absent_requires_an_actual_empty_search():
    v = rss.classify(LIVE, 200, [], now=NOW)
    assert v["state"] == rss.STATE_ABSENT


# ── Real observations from 2026-08-29, encoded ────────────────────────
def test_archived_repo_has_no_door():
    """appcypher/awesome-mcp-servers, archived 2026-08-01."""
    v = rss.classify({"archived": True}, 200, [], now=NOW)
    assert v["state"] == rss.STATE_NO_DOOR and v["kind"] == "archived"


def test_prs_disabled_has_no_door():
    """wong2/awesome-mcp-servers — GET /pulls returns 404."""
    v = rss.classify(LIVE, 404, None, now=NOW)
    assert v["state"] == rss.STATE_NO_DOOR and v["kind"] == "prs_disabled"


def test_archived_beats_a_stale_pr_record():
    v = rss.classify({"archived": True}, 200, [_pr(1, "open", days_ago=30)],
                     now=NOW)
    assert v["state"] == rss.STATE_NO_DOOR


def test_merged_pr_reads_listed():
    """TensorBlock #1136, merged 2026-07-12 into docs/ — invisible to a
    README grep, which is precisely why content probes are advisory."""
    v = rss.classify(LIVE, 200, [_pr(1136, "closed", merged=True)], now=NOW)
    assert v["state"] == rss.STATE_LISTED
    assert v["pr_number"] == 1136


def test_open_pr_reads_pending_with_days_waiting():
    """YuzeHao #378, open since 2026-07-27."""
    v = rss.classify(LIVE, 200, [_pr(378, "open", days_ago=33)], backlog=232,
                     now=NOW)
    assert v["state"] == rss.STATE_PENDING
    assert v["days_waiting"] == 33
    assert v["open_pr_backlog"] == 232
    assert "378" in v["evidence"] and "232" in v["evidence"]


def test_merged_wins_over_open_when_both_exist():
    v = rss.classify(LIVE, 200,
                     [_pr(9, "open", days_ago=1), _pr(1, "closed", merged=True)],
                     now=NOW)
    assert v["state"] == rss.STATE_LISTED


def test_oldest_open_pr_is_the_one_reported():
    v = rss.classify(LIVE, 200,
                     [_pr(9, "open", days_ago=2), _pr(3, "open", days_ago=40)],
                     now=NOW)
    assert v["pr_number"] == 3 and v["days_waiting"] == 40


def test_unreadable_repo_is_unknown():
    v = rss.classify(None, 0, None, now=NOW)
    assert v["state"] == rss.STATE_UNKNOWN


# ── Structural guards ─────────────────────────────────────────────────
def test_scan_refuses_to_write_without_a_github_token(monkeypatch):
    monkeypatch.delenv("PR_SUBMIT_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv(rss.KILL_SWITCH_ENV, raising=False)
    out = rss.run_scan()
    assert out["ok"] is False and out["error"] == "no_github_token"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv(rss.KILL_SWITCH_ENV, "1")
    assert rss.run_scan()["disabled"] is True


def test_module_does_not_conclude_from_readme_content():
    """No content-grep path may exist — it is what produced the duplicates."""
    src = open("routes/registry_submission_state.py").read()
    for needle in ("raw.githubusercontent", "README.md\"", "search/code"):
        assert needle not in src, (
            f"{needle} implies a content probe; content is advisory only and "
            "must never decide `absent`")


def test_every_state_is_one_of_the_five():
    declared = {rss.STATE_LISTED, rss.STATE_PENDING, rss.STATE_ABSENT,
                rss.STATE_NO_DOOR, rss.STATE_UNKNOWN}
    cases = [(None, 0, None), (LIVE, 404, None), ({"archived": True}, 200, []),
             (LIVE, 200, None), (LIVE, 200, []),
             (LIVE, 200, [_pr(1, "open")]),
             (LIVE, 200, [_pr(1, "closed", merged=True)])]
    for meta, st, prs in cases:
        assert rss.classify(meta, st, prs, now=NOW)["state"] in declared
