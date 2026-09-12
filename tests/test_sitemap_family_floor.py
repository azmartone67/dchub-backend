#!/usr/bin/env python3
"""tests/test_sitemap_family_floor.py — one vanishing sitemap FAMILY must fail
the rebuild job, even though the TOTAL still clears its floor.

NO NETWORK. The step's own `urllib.request.urlopen` is stubbed.

★★★ THE DEFECT. .github/workflows/sitemap-snapshot.yml floors the rebuild on
`total_urls` summed across every shard family (MIN_URLS=4000). Measured from
the published artefact on 2026-09-11 that total is three families:

    fixed     static+markets+dcpi+press     1,604 URLs
    ranking   facilities-N                   6,897 URLs
    ai        ai-facilities-N               18,743 URLs

_rebuild_sitemap_snapshot is ALLOWED to drop the AI family on its own — it
wraps the ungated build in both a try/except and a superset guard, and both
paths log an error and then publish the generation with `ai_shard_keys = []`,
deliberately, so an AI failure cannot take the gated sitemap down with it.

Lose that family and the total is ~8,506. That clears 4,000. The job goes
GREEN while 18,743 URLs disappear from the published sitemap. The workflow's
"Verify the published sitemap" step cannot see it either: it asserts the index
lists >= 3 shards (five remain) and that the FIRST facilities shard is
non-empty — which is the GATED family, the one an AI failure leaves alone.

Until PR #4459 the superset guard compared `len(ai_fac) < len(fac)` and the
ungated set is the gated query minus one AND clause, so it essentially never
fired. #4459 compares SETS (`_missing_locs`) and CAN fire on a single URL.

★ THE STEP BODY IS EXTRACTED FROM THE YAML, NEVER RETYPED — including the
  floor values out of the step's `env:`. A retyped copy drifts from what runs
  and then tests a program nobody deploys. See
  [[feedback_test_the_code_not_a_mirror]].

Run standalone:   python3 tests/test_sitemap_family_floor.py
Run under pytest: pytest tests/test_sitemap_family_floor.py
"""
import io
import os
import re
import sys
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "sitemap-snapshot.yml")
STEP_NAME = "Per-family floors on the published sitemap"

# The published artefact, measured 2026-09-11 with
#   curl -sS https://dchub.cloud/sitemap-<shard>.xml | grep -c '<loc>'
# These are the numbers the shipped floors are two-thirds of.
LIVE = {
    "static": 522,
    "markets": 586,
    "dcpi": 333,
    "press": 163,
    "facilities-1": 6897,
    "ai-facilities-1": 10000,
    "ai-facilities-2": 8743,
}
LIVE_ORDER = ["static", "markets", "dcpi", "press", "facilities-1",
              "ai-facilities-1", "ai-facilities-2"]


# ── extracting the real step ────────────────────────────────────────────────

def _step():
    import yaml
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    for s in doc["jobs"]["rebuild"]["steps"]:
        if s.get("name") == STEP_NAME:
            return s
    raise AssertionError(f"no step named {STEP_NAME!r} in {WF}")


def _source():
    """The python the step actually runs, lifted out of its heredoc."""
    run = _step()["run"]
    m = re.search(r"<<'PY'\n(.*)\nPY\s*$", run, re.S)
    assert m, "the step no longer runs a <<'PY' heredoc — extraction is broken"
    src = m.group(1)
    compile(src, STEP_NAME, "exec")      # a syntax error here is a broken job
    return src


def _env():
    """The step's own env block — the shipped floors, not retyped ones."""
    return dict(_step().get("env") or {})


# ── the fake edge ───────────────────────────────────────────────────────────

def _index_xml(names):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(
                f'  <sitemap><loc>https://dchub.cloud/sitemap-{n}.xml</loc>'
                f'<lastmod>2026-09-11</lastmod></sitemap>' for n in names)
            + "\n</sitemapindex>")


def _shard_xml(n_urls):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "<url><loc>https://dchub.cloud/x</loc></url>\n" * n_urls
            + "</urlset>")


def _http(code):
    return urllib.error.HTTPError("https://dchub.cloud/x", code, "stub", {}, None)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _edge(counts=None, index=None, ai_index="absent", fail=None):
    """Build the routing table the stub serves.

    counts  shard -> URL count (or an Exception to raise for that shard)
    index   shard names listed in /sitemap.xml     (default: the live seven)
    ai_index what /sitemap-ai.xml does: "absent" (404), a list of names, or an
             Exception
    fail    shard -> Exception, merged over counts
    """
    counts = dict(LIVE if counts is None else counts)
    for k, v in (fail or {}).items():
        counts[k] = v
    names = LIVE_ORDER if index is None else index
    table = {"sitemap.xml": _index_xml(names)}
    if isinstance(ai_index, list):
        table["sitemap-ai.xml"] = _index_xml(ai_index)
    elif isinstance(ai_index, Exception):
        table["sitemap-ai.xml"] = ai_index
    else:
        table["sitemap-ai.xml"] = _http(404)
    for shard, val in counts.items():
        table[f"sitemap-{shard}.xml"] = (
            val if isinstance(val, Exception) else _shard_xml(val))
    return table


def _run(table):
    """Execute the extracted step against `table`. Returns (exit_code, stdout)."""
    seen = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        key = url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        seen.append(key)
        val = table.get(key)
        if val is None:
            raise _http(404)
        if isinstance(val, Exception):
            raise val
        return _Resp(val.encode("utf-8"))

    env = {k: str(v) for k, v in _env().items()}
    env["PATH"] = os.environ.get("PATH", "")
    buf = io.StringIO()
    code = 0
    with mock.patch.object(urllib.request, "urlopen", fake_urlopen), \
            mock.patch("time.sleep", lambda *_: None), \
            mock.patch.dict(os.environ, env, clear=True):
        try:
            with redirect_stdout(buf):
                exec(compile(_source(), STEP_NAME, "exec"),
                     {"__name__": "__main__"})
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue(), seen


# ── 1. the motivating defect ────────────────────────────────────────────────

def test_the_AI_FAMILY_VANISHING_fails_the_job():
    """★★★ THE WHOLE POINT. The gated sitemap is intact, every other family is
    at its live size, the total is 8,506 — over MIN_URLS=4000 — and 18,743 URLs
    are gone. This must be RED."""
    gone = [n for n in LIVE_ORDER if not n.startswith("ai-")]
    code, out, _ = _run(_edge(
        counts={n: LIVE[n] for n in gone}, index=gone, ai_index="absent"))
    surviving = sum(LIVE[n] for n in gone)
    assert surviving > 4000, (
        f"fixture is not the real failure: {surviving} URLs survive, which "
        f"would already trip MIN_URLS=4000 and prove nothing")
    assert code == 1, (
        f"the AI family is GONE and the job passed (exit {code}). "
        f"{surviving} URLs survive, which clears MIN_URLS=4000.\n{out}")
    assert "::error" in out, "a failure with no ::error:: annotation is invisible"
    assert re.search(r"::error[^\n]*family `ai` published 0 URLs", out), (
        f"the failure does not NAME the ai family:\n{out}")
    assert "SHORT BY 12495" in out, (
        f"the failure does not say BY HOW MUCH:\n{out}")


def test_the_error_names_the_family_and_the_shortfall_for_any_family():
    """Not just ai. A ranking collapse must name `ranking` and its shortfall."""
    code, out, _ = _run(_edge(counts=dict(LIVE, **{"facilities-1": 100})))
    assert code == 1, f"ranking collapsed to 100 URLs and the job passed\n{out}"
    assert re.search(r"family `ranking` published 100 URLs", out), out
    assert "SHORT BY 4498" in out, f"4598 - 100 = 4498 not reported:\n{out}"
    assert "`ai`" not in out.split("::error")[1], (
        "the ai family is healthy and is being blamed")


def test_the_live_artefact_passes():
    """The floors must not red a healthy sitemap. Fixture = 2026-09-11 live."""
    code, out, _ = _run(_edge())
    assert code == 0, f"the live artefact fails its own floors:\n{out}"
    assert "::error" not in out, out
    assert "::warning" not in out, out
    assert "All 3 families MEASURED and within floor." in out, out


# ── 2. could not measure is NOT measured-and-breached ───────────────────────

def test_an_unreachable_shard_warns_and_does_not_fail():
    """★ The dangerous direction is the other one, but this one matters too: a
    timeout on a 1.7 MB shard is not 18,743 missing URLs, and a red X for a
    Cloudflare hiccup is how a real alarm gets ignored."""
    code, out, _ = _run(_edge(fail={"ai-facilities-2": TimeoutError("timed out")}))
    assert code == 0, f"a transport timeout was reported as breakage:\n{out}"
    assert "::warning" in out, f"an unmeasured family must still be LOUD:\n{out}"
    assert "::error" not in out, out
    assert "COULD NOT MEASURE" in out, out


def test_an_unreachable_index_warns_and_does_not_fail():
    code, out, _ = _run(_edge(fail={}) | {"sitemap.xml": TimeoutError("nope")})
    assert code == 0, f"an unreadable index was reported as breakage:\n{out}"
    assert "::warning" in out and "::error" not in out, out


def test_could_not_measure_never_claims_the_family_is_FINE():
    """★★★ Mutate the unknown branch toward permissive and this is the test
    that has to notice: an unmeasured family must not be counted as one that
    MEASURED within floor. Treating unknown as success is how a broken system
    gets reported healthy."""
    code, out, _ = _run(_edge(fail={"ai-facilities-1": TimeoutError("x")}))
    assert code == 0, out
    assert "All 3 families MEASURED" not in out, (
        f"one family was never measured and the step claims all three were:\n{out}")
    assert "2 family/families measured and within floor; 1 NOT measured." in out, out


def test_a_404_from_the_origin_is_MEASURED_not_unmeasured():
    """★ A 404 or a 503 is the origin SAYING the row is not in this generation.
    Rounding that up to 'could not measure' is how the defect hides behind the
    grace the other branch gets."""
    code, out, _ = _run(_edge(fail={"ai-facilities-2": _http(404)}))
    assert code == 1, (
        f"a shard listed in the index 404d and the job passed (exit {code}):\n{out}")
    assert "is LISTED in a published index and is not served" in out, out
    code, out, _ = _run(_edge(fail={"ai-facilities-2": _http(503)}))
    assert code == 1, f"a 503 snapshot miss on a listed shard passed:\n{out}"


# ── 3. the fallback, and the shape a summed floor cannot see ────────────────

def test_ai_only_under_its_own_index_is_not_a_breach():
    """main.py's r-ai-in-index block says 'reverse by deleting this block'. If
    someone does, the AI shards leave /sitemap.xml and live only under
    /sitemap-ai.xml. That is a deliberate change, not 18,743 missing URLs."""
    gated = [n for n in LIVE_ORDER if not n.startswith("ai-")]
    ai = [n for n in LIVE_ORDER if n.startswith("ai-")]
    code, out, _ = _run(_edge(index=gated, ai_index=ai))
    assert code == 0, f"the documented reversal was reported as breakage:\n{out}"
    assert "fallback lists 2 AI shards" in out, out


def test_ai_absent_from_both_indexes_is_a_breach():
    gated = [n for n in LIVE_ORDER if not n.startswith("ai-")]
    code, out, _ = _run(_edge(counts={n: LIVE[n] for n in gated},
                              index=gated, ai_index=_http(503)))
    assert code == 1, f"the family is in NEITHER index and the job passed:\n{out}"
    assert "NOT PUBLISHED" in out, out


def test_ai_absent_from_the_index_and_its_own_index_unreachable_warns():
    gated = [n for n in LIVE_ORDER if not n.startswith("ai-")]
    code, out, _ = _run(_edge(counts={n: LIVE[n] for n in gated},
                              index=gated, ai_index=TimeoutError("x")))
    assert code == 0, f"an unreachable AI index was reported as breakage:\n{out}"
    assert "::warning" in out and "::error" not in out, out


def test_a_single_EMPTY_fixed_shard_fails_even_though_the_sum_clears():
    """★ A summed floor catches collapse toward zero and is BLIND to a minority
    member. press is 163 of fixed's 1,604: drop it and fixed reads 1,441
    against a floor of 1,069. So each fixed shard is named and required
    non-empty. See [[feedback_scan_that_can_find_nothing_needs_a_floor]]."""
    code, out, _ = _run(_edge(counts=dict(LIVE, press=0)))
    assert sum(LIVE[n] for n in ("static", "markets", "dcpi")) > int(
        _env()["MIN_FIXED_URLS"]), (
        "fixture is not the real failure — the sum floor would catch this alone")
    assert code == 1, f"press is published and EMPTY and the job passed:\n{out}"
    assert "`sitemap-press.xml` is published and EMPTY" in out, out


def test_a_fixed_shard_missing_from_the_index_fails():
    names = [n for n in LIVE_ORDER if n != "dcpi"]
    code, out, _ = _run(_edge(index=names))
    assert code == 1, f"dcpi left the index and the job passed:\n{out}"
    assert "`sitemap-dcpi.xml` is not listed in /sitemap.xml at all" in out, out


# ── 4. the classifier ───────────────────────────────────────────────────────

def test_an_ai_shard_is_never_counted_as_a_ranking_shard():
    """`ai-facilities-1` against `facilities-\\d+` — main.py's
    serve_sitemap_shard carries the same trap and
    tests/test_sitemap_ai_family.py pins it there."""
    _, out, _ = _run(_edge())
    assert "**ranking**: 6897 URLs across 1 shards" in out, (
        f"ranking absorbed the AI shards — the pattern is not anchored:\n{out}")
    assert "**ai**: 18743 URLs across 2 shards" in out, out


def test_an_unknown_shard_is_reported_and_counted_nowhere():
    code, out, _ = _run(_edge(counts=dict(LIVE, **{"news": 5}),
                              index=LIVE_ORDER + ["news"]))
    assert code == 0, out
    assert "unclassified shard `news`" in out, out
    assert "**ranking**: 6897" in out, "an unknown shard leaked into a family"


# ── 5. the floors themselves are the tuning, so they are pinned ─────────────
#
# [[feedback_mutate_the_guards_own_tunable]]: breaking the code a guard
# protects does not test the guard's own threshold. Lower a floor to 0 and
# every test above still passes — the family check goes blind and nothing says
# so. These bind each floor to the measurement it was derived from.

def test_each_floor_is_two_thirds_of_its_measured_basis():
    env = _env()
    fixed_live = sum(LIVE[n] for n in ("static", "markets", "dcpi", "press"))
    ai_live = LIVE["ai-facilities-1"] + LIVE["ai-facilities-2"]
    for key, basis in (("MIN_FIXED_URLS", fixed_live),
                       ("MIN_RANKING_URLS", LIVE["facilities-1"]),
                       ("MIN_AI_URLS", ai_live)):
        want = basis * 2 // 3
        got = int(env[key])
        assert got == want, (
            f"{key} is {got}; two-thirds of the measured {basis} is {want}. "
            f"Re-measure and restate the basis in the step comment rather than "
            f"moving the floor on its own.")


def test_the_floors_are_ordered_by_family_size():
    """A transposition (ai's floor on ranking) would leave every test above
    green and make the big family's floor trivially clearable."""
    env = _env()
    assert (int(env["MIN_FIXED_URLS"]) < int(env["MIN_RANKING_URLS"])
            < int(env["MIN_AI_URLS"])), env


def test_the_fixed_shard_list_is_main_pys_own():
    """The step requires these shards by name; main.py decides what they are."""
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    m = re.search(r"^_SITEMAP_FIXED_SECTIONS\s*=\s*\(([^)]*)\)", src, re.M)
    assert m, "_SITEMAP_FIXED_SECTIONS is gone from main.py"
    canon = tuple(re.findall(r"'([a-z0-9-]+)'", m.group(1)))
    want = tuple(s for s in _env()["REQUIRE_FIXED_SHARDS"].split(",") if s)
    assert canon == want, (
        f"main.py builds {canon} and the workflow requires {want}")


# ── 6. wiring ───────────────────────────────────────────────────────────────

def test_the_floor_step_runs_on_the_cron_path_too():
    """Data drift removes URLs without a deploy, which is what the 4-hourly
    cron exists for. A push-only floor would not watch it."""
    assert "if" not in _step(), (
        "the per-family floor is conditional — it must run on every trigger, "
        "like the rebuild and the read-back")


def test_the_floor_step_runs_after_the_rebuild():
    import yaml
    steps = yaml.safe_load(open(WF, encoding="utf-8"))["jobs"]["rebuild"]["steps"]
    names = [s.get("name") or s.get("uses") for s in steps]
    assert names.index(STEP_NAME) > names.index("Rebuild sitemap snapshot"), (
        "the floor measures the artefact the rebuild just published")


def test_the_fetch_fails_closed_on_a_missing_shard():
    """★ Plain `curl -sS` EXITS 0 on a 404 and writes the error page to the
    output file — so a shard that is GONE arrives as a successful download of
    zero <loc> elements, indistinguishable from an empty shard, and the family
    total silently drops. That is the shape tests/test_workflow_curl_guard.py
    exists to catch. urlopen raises instead, and this pins that it stays that
    way if anyone reaches for curl here."""
    # Comments stripped first: the step's own commentary EXPLAINS the curl
    # trap by naming it, and matching our own postmortem would fail a healthy
    # step — the mirror of the false pass tests/test_workflow_curl_guard.py
    # strips comments to avoid. See
    # [[feedback_comment_explaining_drift_quotes_the_drift]].
    code_only = re.sub(r"(?m)^\s*#.*$", "", _source())
    assert "urllib.request.urlopen" in code_only, (
        "the step no longer uses urlopen — if it now shells out to curl, a 404 "
        "exits 0 and a missing shard reads as an empty one")
    assert "curl" not in code_only, "curl in the fetch path: a 404 would exit 0"
    # and behaviourally: a 404 on a listed shard is a defect, not a zero count
    code, out, _ = _run(_edge(fail={"facilities-1": _http(404)}))
    assert code == 1 and "is not served" in out, out


def test_the_step_reads_the_artefact_not_the_endpoints_report():
    run = _step()["run"]
    assert "https://dchub.cloud" in run, "the step does not read the edge"
    assert "/tmp/out.json" not in run, (
        "the floor must not read the rebuild endpoint's own report — it has no "
        "per-family breakdown and it reports on its own write")
    assert "date +%s" in run or "time.time()" in run, (
        "the read must be cache-busted or it can measure a cached generation")


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
