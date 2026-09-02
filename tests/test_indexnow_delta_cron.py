"""F7 (2026-09-02): the IndexNow cron submits CHANGED facilities, not the same
4,000 URLs every 6 hours.

MEASURED: the last 4 runs of indexnow.yml each reported
{"endpoint":"https://www.bing.com/indexnow","ok":true,"status":200,
"submitted":4000} — `?facilities=1&recent=1&n=2000` is the newest 2,000
facilities BY ID (routes/indexnow.py _recent_facility_urls, `ORDER BY id
DESC`) plus the 2,000 most-recent sitemap URLs, ~16K resubmissions/day of
unchanged pages. IndexNow's guidance is changed-URLs-only; repeated identical
batches are de-prioritised, and 93% of AI-search referrals ride Bing/Copilot.

The cursor path already existed and was unused by the cron:
`?delta=1` -> ping_new_facilities (routes/indexnow.py): rows newer than the
last successfully pinged id, taken FOR UPDATE, cursor advanced only on a 2xx.
This pins (a) the cron now uses it, with a SMALL recent net for news/press,
and (b) the endpoint behaviour the cron relies on.
"""
import ast
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "indexnow.yml"
SRC = ROOT / "routes" / "indexnow.py"


def _uncommented(text):
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def _submit_step():
    d = yaml.safe_load(WF.read_text())
    steps = d["jobs"]["ping"]["steps"]
    assert steps, "indexnow.yml has no steps"
    return _uncommented(steps[0]["run"])


def test_cron_submits_the_facility_delta_not_the_newest_2000_by_id():
    run = _submit_step()
    assert re.search(r"delta=1&n=\d+", run), "the facility submit must use the cursor delta path"
    assert "facilities=1" not in run, (
        "facilities=1 is the newest-2000-by-id backfill mode — it resubmits the same "
        "unchanged URLs every run")
    assert "X-Admin-Key" in run, "delta is admin-only"


def test_recent_net_is_small_and_separate():
    run = _submit_step()
    m = re.search(r"recent=1&n=(\d+)", run)
    assert m, "keep a small recent-URL net for news/press"
    assert int(m.group(1)) <= 500, f"recent net is {m.group(1)} URLs — that is the old firehose"
    assert not re.search(r"delta=1[^\"]*recent=1|recent=1[^\"]*delta=1", run), (
        "delta and recent must be separate calls — delta returns its own JSON, not a URL list")


def test_the_delta_endpoint_still_does_what_the_cron_relies_on():
    src = SRC.read_text()
    tree = ast.parse(src)
    fn = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    ep = ast.get_source_segment(src, fn["indexnow_endpoint"])
    assert 'if _wants("delta"):' in ep, "the endpoint no longer dispatches ?delta=1"
    assert "ping_new_facilities(" in ep
    body = ast.get_source_segment(src, fn["ping_new_facilities"])
    assert "FOR UPDATE" in body, "the cursor row must be locked — overlapping runs otherwise double-submit"
    assert "indexnow_cursor" in body and "submit_to_indexnow(urls)" in body
    # The cursor advances ONLY on a successful submit, so a failed ping retries.
    ok_idx = body.index('if res.get("ok"):')
    upd_idx = body.index("UPDATE indexnow_cursor SET last_fac_id", ok_idx)
    assert upd_idx > ok_idx and "conn.rollback()" in body[upd_idx:]
    assert "COALESCE(is_duplicate, 0) = 0" in body, "duplicates must never be submitted"
