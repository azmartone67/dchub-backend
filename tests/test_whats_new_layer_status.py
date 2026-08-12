"""Guards for the honesty of the /whats-new layer board.

THE DEFECTS THESE PIN (operator-reported and measured live on 2026-08-07):

  1. THE PAGE LAGGED THE TABLES AND NOTHING SAID SO. /api/v1/whats-new reads
     counts from infra_growth_snapshot, never a live COUNT(*). The gas cap fix
     (#2330-2332) moved gas_pipelines 30,918 -> 33,769 at 08:27 UTC; the daily
     snapshot had run at 06:34. The board published 30,918 for the rest of the
     day, correct for its timestamp — and the timestamp was never published.
     Fixed two ways, both fenced here: every ingest workflow re-baselines the
     snapshot when it finishes, and the item carries count_captured_at.

  2. TWELVE OF FIFTEEN LAYERS RENDERED A BARE TOTAL UNDER "periodic"/"static".
     Those are CADENCE words. Shipped alone they read as "dead", and they were
     hiding three different situations that a reader cannot tell apart:
       (a) NOT MEASURED — subsea had no _LAYERS entry at all (SH52-059);
       (b) GENUINELY FROZEN — fiber's UNIQUE(name,provider) cap (SH52-054),
           substations' blocked upstream refresh (SH52-056);
       (c) EARNED IDLE — FCC BDC is semiannual, so 56 quiet days is on schedule.

  2b. THE (b) ANNOTATION OUTLIVED THE CAP IT DESCRIBED (live 2026-08-12).
     SH52-054's cap was fixed (#2544 un-capped the lane, #2622 re-keyed it on
     the upstream asset id), and the note kept asserting "The route count
     moves only on a bulk refresh". Because status/status_reason/added are
     measured per request and the note is hand-written, ONE /api/v1/whats-new
     response published status "growing", "+1,890 new rows in the last 7d" and
     added_1d 872 directly above a note saying the count could not move.
     A frozen FIGURE was already fenced; a frozen VERB was not. It is now:
     test_known_issue_notes_make_no_claim_about_the_count_moving. The note
     keeps the part that is structurally true and did NOT get deleted — the
     finding is still open, because the duplicate twins are frozen in place
     rather than pruned and most rows carry no upstream identity at all.

  3. THE FIRST DRAFT OF THE FIX WAS ITSELF DISHONEST, and test_* below is
     where that was caught. The "refreshed" reason asserted "this loader
     rewrites every row rather than appending, so a flat count is what a
     healthy run looks like" — true for gas/GEM, FALSE for metro_fiber_routes,
     which is flat because its inserts are discarded by a UNIQUE key. Both
     layers reach that branch. A derived status may state what was MEASURED;
     it may not explain a mechanism it did not measure.

  4. THE SNAPSHOT COLLIDED WITH THE SYNC. infra-growth-tracker fired at 06:09
     UTC, nine minutes into data-sync's 06:00 run, while the gas loader had
     deleted its source tag and not yet rewritten it — a torn count publishes
     an invented dip today and a matching spike tomorrow.

CI-SAFETY: the unit-tests job installs ONLY pytest, not requirements.txt.
routes/infra_growth.py imports psycopg2 and flask, so it is NEVER imported
here — the module is read with `ast`, and _layer_status is extracted and
executed against plain values. Nothing runs at module scope (a module-scope
sys.exit aborts collection and kills the whole session).

MUTATION-VERIFIED (11 mutations, all red, restored green — 25 passed):
  M1  drop the subsea entries from _LAYERS            -> subsea registration
  M2  subsea freshness column -> created_at           -> subsea registration
  M3  rename the emitted key to "reason_internal"     -> response-dict keys
  M4  restore the "rewrites every row / healthy" text -> refreshed-reason
  M5  cite SH52-999                                   -> dangling citation
  M6  put a figure in a known_issue note              -> frozen figure
  M7  disable the delta_window-is-None branch         -> unmeasured != zero
  M8  delete the gas re-baseline step                 -> workflow re-baseline
  M9  swallow the snapshot curl with `|| true`        -> silent-failure
  M10 restore cron '9 6 * * *'                        -> mid-sync collision
  M11 add a hardcoded per-layer health map            -> derived-status

TWO OF THESE TESTS WERE VACUOUS UNTIL A MUTATION SAID SO, which is the reason
the list above names its mutations instead of asserting "verified":
  - M3 walked through `'"status_reason"' in src`, because the route's own note
    string documents the field BY NAME — the substring survived in prose while
    the data was gone (#2062 class). Now the dict keys are read from the AST.
  - M9 walked through a line-by-line scan for `|| true` on the curl: the call
    spans three backslash-continuations, so the swallow parked on the
    `-H "X-Admin-Key: …"` line, which contains neither "curl" nor the endpoint.
    Continuations are joined into logical commands first.
"""
import ast
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GROWTH = "routes/infra_growth.py"


def _parse(relpath):
    """Parse a repo file to an AST, asserting the parse produced real nodes.

    ★ An empty parse satisfies every isinstance() filter downstream and makes
    the whole suite vacuously green. Assert the tree is non-trivial FIRST.
    """
    path = os.path.join(_ROOT, relpath)
    assert os.path.exists(path), f"{relpath} missing"
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert len(src) > 500, f"{relpath} suspiciously small ({len(src)}b)"
    tree = ast.parse(src)
    assert len(tree.body) > 3, f"{relpath} parsed to {len(tree.body)} top-level nodes"
    return tree, src


def _read(relpath):
    path = os.path.join(_ROOT, relpath)
    assert os.path.exists(path), f"{relpath} missing"
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _module_dict(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    assert isinstance(node.value, ast.Dict), f"{name} is not a dict literal"
                    return {k.value: v for k, v in zip(node.value.keys, node.value.values)
                            if isinstance(k, ast.Constant)}
    raise AssertionError(f"{name} not found at module level")


def _layers():
    tree, _ = _parse(_GROWTH)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_LAYERS":
                    assert isinstance(node.value, ast.List)
                    rows = [tuple(e.value if isinstance(e, ast.Constant) else None
                                  for e in elt.elts)
                            for elt in node.value.elts if isinstance(elt, ast.Tuple)]
                    assert rows, "_LAYERS parsed empty"
                    return rows
    raise AssertionError("_LAYERS not found")


def _load_status_fn():
    """Extract and execute _layer_status against plain values.

    Executed, not grepped: #2062's lesson is that a presence check goes green
    on a re-broken implementation. Every status branch below is really run.
    """
    tree, _ = _parse(_GROWTH)
    ns = {}
    found = False
    for node in tree.body:
        wanted = ((isinstance(node, ast.FunctionDef) and node.name == "_layer_status")
                  or (isinstance(node, ast.Assign)
                      and any(getattr(t, "id", None) == "_RELOAD_FRESH_DAYS"
                              for t in node.targets)))
        if wanted:
            found = found or isinstance(node, ast.FunctionDef)
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<status>", "exec"),
                 ns, ns)
    assert found, "_layer_status is gone — every layer would ship a bare cadence word"
    return ns["_layer_status"]


# ── 1. subsea is MEASURED, not relabelled (SH52-059) ───────────────────────
def test_subsea_layers_are_registered_with_a_real_freshness_column():
    """The (a) case: a layer absent from _LAYERS cannot show growth at all.

    subsea_cables / subsea_landing_points went 133 days with no writes because
    nothing drove the sync, and the board could not say so — it was not
    watching them. Registering the layer is the fix; a label would not have been.
    """
    rows = _layers()
    by_label = {r[0]: r for r in rows}
    for label, table in (("subsea_cables", "subsea_cables"),
                         ("subsea_landings", "subsea_landing_points")):
        assert label in by_label, (
            f"{label} is not in _LAYERS — /whats-new cannot distinguish "
            f"'this layer is not growing' from 'we never measured it' (SH52-059)")
        assert by_label[label][1] == table, (
            f"{label} points at {by_label[label][1]!r}, not the table the "
            f"subsea ingest actually writes ({table!r})")
        assert by_label[label][3], f"{label} has no staleness threshold — a dead driver would never flag"

    tree, _ = _parse(_GROWTH)
    fresh = _module_dict(tree, "_FRESH_COL")
    for label in ("subsea_cables", "subsea_landings"):
        assert label in fresh, f"{label} declares no freshness column"
        # created_at only ever records the FIRST write. Both subsea upserts set
        # updated_at=NOW() in their ON CONFLICT DO UPDATE, so updated_at is the
        # only column that moves when a refresh touches an existing row —
        # created_at would report a live loader as 133 days dead.
        assert fresh[label].value == "updated_at", (
            f"{label} reads {fresh[label].value!r}; the subsea upsert only "
            f"refreshes updated_at, so any other column freezes at first write")


def test_subsea_layers_have_friendly_names_and_provenance():
    """A layer with no _FRIENDLY entry renders its raw table name to the public."""
    tree, _ = _parse(_GROWTH)
    friendly = _module_dict(tree, "_FRIENDLY")
    prov = _module_dict(tree, "_PROVENANCE")
    for label in ("subsea_cables", "subsea_landings"):
        assert label in friendly, f"{label} would render as a raw layer key on the page"
        assert label in prov, (
            f"{label} has no provenance entry, so it defaults to a generic "
            f"'public data' credit — TeleGeography must be named")


# ── 2. every layer publishes a status, and it is DERIVED ───────────────────
@pytest.mark.parametrize("func", ["_summary", "whats_new"])
def test_the_response_dicts_actually_carry_status_and_reason(func):
    """The keys must be in the emitted DICTS, not merely somewhere in the file.

    ★ THIS TEST WAS VACUOUS AND A MUTATION CAUGHT IT. The first version asserted
    `'"status_reason"' in src`. Renaming the real key to "reason_internal" and
    deleting the /whats-new passthrough left that assertion GREEN — because the
    route's own `note` string documents the field by name, so the substring
    survived in prose while the data disappeared. That is the #2062 class
    exactly: a presence check satisfied by a mention. So this reads the keys of
    the dict literals inside the two functions that build the response.
    """
    tree, _ = _parse(_GROWTH)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == func), None)
    assert fn is not None, f"{func}() is gone from {_GROWTH}"
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            keys |= {k.value for k in node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # whats_new passes some fields through jsonify(**kwargs) too.
        elif isinstance(node, ast.keyword) and node.arg:
            keys.add(node.arg)
    for field in ("status", "status_reason", "known_issue", "count_captured_at"):
        assert field in keys, (
            f"{func}() no longer emits {field!r} — a cadence chip would ship "
            f"alone again, and 'periodic'/'static' read to a visitor as 'dead'")


def test_status_is_computed_by_calling_the_derived_function():
    tree, _ = _parse(_GROWTH)
    summary = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_summary")
    calls = {getattr(n.func, "id", None) for n in ast.walk(summary)
             if isinstance(n, ast.Call)}
    assert "_layer_status" in calls, (
        "_summary no longer calls _layer_status — the status field would be "
        "populated by something other than the measured signals")


def test_status_is_derived_from_measurements_not_a_hardcoded_health_map():
    """No dict may map a layer label to a health word.

    A hand-maintained health label is wrong the moment a loader changes and
    nobody notices — which is how this board came to publish a frozen layer and
    an on-schedule quiet one under the same chip.
    """
    tree, _ = _parse(_GROWTH)
    words = {"growing", "refreshed", "on_cadence", "overdue", "unjudged",
             "measuring", "unmeasurable"}
    fn_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_layer_status" in fn_names
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        name = next((t.id for t in node.targets if isinstance(t, ast.Name)), "?")
        vals = {v.value for v in node.value.values if isinstance(v, ast.Constant)}
        assert not (vals & words), (
            f"{name} hardcodes health word(s) {sorted(vals & words)} per layer; "
            f"status must be derived in _layer_status from measured signals")


@pytest.mark.parametrize("delta_window,window_days,ingest_age,stale,expected,want", [
    # measured live 2026-08-07/08 on the real board
    (1368, 7,    1,   14,  "daily",      "growing"),      # data centers
    (0,    7,    1,  130,  "quarterly",  "refreshed"),    # gas: written, count flat
    (0,    7,   56,  230,  "semiannual", "on_cadence"),   # FCC: earned idle (c)
    (0,    7,  159, None,  "adhoc",      "unjudged"),     # discovered plants
    (None, None,  4,  120, "quarterly",  "measuring"),    # transmission: no history yet
    (0,    7, None,  120,  "quarterly",  "unmeasurable"), # no timestamp column
    (0,    7,  400,  230,  "semiannual", "overdue"),      # a genuinely broken loader
    (0,    7,  133,   10,  "weekly",     "overdue"),      # subsea, driver dead again
])
def test_layer_status_classifies_every_real_case(delta_window, window_days,
                                                 ingest_age, stale, expected, want):
    got, reason = _load_status_fn()(delta_window, window_days, ingest_age,
                                    stale, expected)
    assert got == want, f"expected {want}, got {got} ({reason})"
    assert reason and len(reason) > 20, f"status {got} shipped without a usable reason"


def test_an_unmeasured_delta_never_reads_as_no_growth():
    """FRESH != GROWTH, and its converse: UNMEASURED != ZERO.

    transmission_lines and power_plants_eia were repointed on 2026-08-07, so
    _HISTORY_FROM discards their older snapshots and delta_window comes back
    None. None formats as "—" on the page. If that fell through to a freshness
    branch it would state the layer was merely being refreshed, when the truth
    is that growth has not been measured yet.
    """
    status, reason = _load_status_fn()(None, None, 4, 120, "quarterly")
    assert status == "measuring", (
        f"an unmeasured delta classified as {status!r} — it must have its own "
        f"status, not borrow a freshness verdict")
    assert "not measured" in reason.lower()
    for banned in ("no growth", "no new rows", "did not move"):
        assert banned not in reason.lower(), (
            f"the unmeasured-delta reason says {banned!r}, which states a "
            f"measurement that was never taken")


def test_refreshed_reason_states_the_measurement_not_a_mechanism():
    """The bug this file's own first draft shipped.

    The "refreshed" branch is reached by EVERY layer whose table was written
    recently and whose count did not move, and those layers are flat for
    unrelated reasons: gas_pipelines because its loader genuinely rewrites
    every row, and — when this guard was written — metro_fiber_routes because
    a UNIQUE(name, provider) key discarded its inserts (SH52-054). Asserting
    the reload mechanism here was therefore a reassuring falsehood for the
    capped layer.

    SH52-054's cap has since been fixed, so fiber is no longer the live
    counterexample. THE PROHIBITION IS NOT RELAXED WITH IT. The branch is
    still shared by loaders whose flatness has causes this function never
    measured — a cap, a dead upstream, a filter, a silent write failure — and
    it cannot tell them apart, which is the whole reason it may state only
    what was measured. Naming today's capped layer in the assertion would make
    this guard expire the next time one gets fixed; it deliberately does not.
    WHY a given layer is flat belongs in known_issue, cited.
    """
    _, reason = _load_status_fn()(0, 7, 1, 130, "quarterly")
    low = reason.lower()
    for claim in ("rewrites every row", "healthy", "what a healthy run looks like",
                  "full-reload", "full reload"):
        assert claim not in low, (
            f"the 'refreshed' reason claims {claim!r}. This branch is shared "
            f"by layers that are flat for reasons this function did not "
            f"measure (a discarded-insert cap is the documented case, "
            f"SH52-054) — asserting the reload mechanism publishes a "
            f"reassurance over them")
    assert "count did not move" in low or "did not move" in low


# ── 3. known_issue citations must resolve to real audit findings ───────────
def test_every_known_issue_cites_a_finding_that_exists_in_the_registry():
    """A dangling SH52 reference is a citation to nothing.

    The annotation is the only hand-written part of the status block, so it is
    the part that rots. This resolves each id against REGISTRY in
    routes/audit_closure_master_shell.py (read with ast — that module imports
    flask, which CI does not install).
    """
    tree, _ = _parse(_GROWTH)
    known = _module_dict(tree, "_KNOWN_ISSUE")
    assert known, "_KNOWN_ISSUE is empty — the frozen layers lost their citations"

    reg_tree, _ = _parse("routes/audit_closure_master_shell.py")
    ids = set()
    for node in reg_tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "REGISTRY" for t in node.targets):
            for elt in node.value.elts:
                if isinstance(elt, ast.Tuple) and elt.elts and isinstance(elt.elts[0], ast.Constant):
                    ids.add(elt.elts[0].value)
    assert len(ids) > 100, f"REGISTRY parsed only {len(ids)} findings — parse is wrong"

    labels = {r[0] for r in _layers()}
    for label, val in known.items():
        assert label in labels, f"_KNOWN_ISSUE annotates {label!r}, which is not a layer"
        assert isinstance(val, ast.Tuple) and len(val.elts) == 2, (
            f"_KNOWN_ISSUE[{label}] is not a (finding_id, note) pair")
        ref = val.elts[0].value
        assert ref in ids, (
            f"_KNOWN_ISSUE[{label}] cites {ref!r}, which is not in the shell #52 "
            f"registry — the page would show a citation pointing at nothing")


def test_known_issue_notes_carry_no_frozen_figures():
    """Prose only. Every number on this board is measured at request time.

    A count baked into an annotation is the frozen-figure class that
    qa-whats-new-fence.mjs exists to catch on the page itself; it must not
    sneak in through the API instead.
    """
    import re
    tree, _ = _parse(_GROWTH)
    known = _module_dict(tree, "_KNOWN_ISSUE")
    for label, val in known.items():
        note = val.elts[1].value
        # A finding id (SH52-054) and a date are references, not claims about
        # data volume; a bare multi-digit number in this prose is a figure.
        stripped = re.sub(r"SH52-\d+|\b\d{4}-\d{2}-\d{2}\b", "", note)
        nums = re.findall(r"\b\d[\d,]{1,}\b", stripped)
        assert not nums, (
            f"_KNOWN_ISSUE[{label}] hardcodes figure(s) {nums} — these notes "
            f"must be prose; measured numbers come from the request")


def test_known_issue_notes_make_no_claim_about_the_count_moving():
    """A frozen VERB is the same defect as a frozen figure, and it shipped.

    The figure fence above only catches digits. It did not catch the fiber
    note ending "The route count moves only on a bulk refresh" — no digits,
    and a claim about measured behaviour all the same. SH52-054's cap was
    fixed on 2026-08-10/08-12 and the sentence stayed, so one live response
    carried status "growing" and "+1,890 new rows in the last 7d" directly
    above a note saying the count could not move. The board contradicted
    itself inside a single payload.

    The split this enforces: status, status_reason and added are MEASURED per
    request and cannot go stale. _KNOWN_ISSUE is hand-written and always can.
    So the note owns what is structurally true — what the arbiter keys on,
    what is unidentified, what was left in place — and owns nothing about
    whether the number moved.

    Note what this does NOT ban: substations' "What still moves here is
    incidental per-row extraction, not the bulk source" stays legal. It
    attributes movement the measurement already found rather than predicting
    it away. What is banned is the note OVERRIDING the measurement.
    """
    tree, _ = _parse(_GROWTH)
    known = _module_dict(tree, "_KNOWN_ISSUE")
    assert known, "_KNOWN_ISSUE is empty — the frozen layers lost their citations"
    banned = (
        "moves only on", "only on a bulk refresh", "count moves only",
        "does not move", "will not move", "never moves", "cannot move",
        "cannot grow", "will not grow", "does not grow", "never grows",
        "no new rows", "count is frozen", "count stays flat", "stays flat",
        "count is capped", "no growth",
    )
    for label, val in known.items():
        low = val.elts[1].value.lower()
        for phrase in banned:
            assert phrase not in low, (
                f"_KNOWN_ISSUE[{label}] asserts {phrase!r} — that is a claim "
                f"about the count, which this response MEASURES. The note is "
                f"hand-written and the measurement is not, so the two "
                f"disagree the moment the cause is fixed: SH52-054 shipped "
                f"exactly this, publishing 'growing' over 'moves only on a "
                f"bulk refresh'. State the structural fact; leave movement to "
                f"status/status_reason/added")


# ── 4. the snapshot is re-baselined by the jobs that move the tables ───────
@pytest.mark.parametrize("workflow", [
    ".github/workflows/data-sync.yml",          # substations/facilities/gas/fiber/tx/subsea, 4x/day
    ".github/workflows/gas-pipeline-ingest.yml",# gas_pipelines, weekly
    ".github/workflows/gem-refresh.yml",        # gem_*, monthly
])
def test_ingest_workflows_rebaseline_the_growth_snapshot(workflow):
    """Without this the page publishes a pre-ingest count until the next day.

    Measured 2026-08-07: gas moved 30,918 -> 33,769 at 08:27 UTC and /whats-new
    served 30,918 until the following morning's snapshot.
    """
    src = _read(workflow)
    assert "/api/v1/admin/infra-growth/snapshot" in src, (
        f"{workflow} rewrites tables the /whats-new board reports on but never "
        f"re-baselines the snapshot — its totals stay pre-ingest until the "
        f"daily cron fires (up to ~23h for a weekly job)")


@pytest.mark.parametrize("workflow", [
    ".github/workflows/data-sync.yml",
    ".github/workflows/gas-pipeline-ingest.yml",
    ".github/workflows/gem-refresh.yml",
])
def test_the_rebaseline_call_cannot_fail_silently(workflow):
    """The swallow this repo keeps re-learning.

    gem-refresh.yml shipped this call as `curl -s ... | head -c 400 || true`:
    no status check, and `|| true` forcing green. It also pointed at
    dchub.cloud, where admin POSTs are cut off at the 15s ROUTE_TIMEOUTS
    default while this endpoint COUNT(*)s tables up to 2.6M rows — so the most
    likely outcome was a timeout reported as success, with the stale flag it
    exists to clear still set.
    """
    src = _read(workflow)
    idx = src.index("/api/v1/admin/infra-growth/snapshot")
    # Window the surrounding shell, not the whole file: another step's `|| true`
    # is none of this test's business.
    block = src[max(0, idx - 400):idx + 900]
    # ★ SCOPED TO THE CURL COMMAND, NOT THE BLOCK. Banning `|| true` anywhere
    # nearby failed on the legitimate `head -c 400 /tmp/snap.json || true` that
    # prints the body inside the failure branch — a guard forbidding the
    # diagnostic printed right before `exit 1`. What must never be swallowed is
    # the CALL.
    #
    # ★★ AND THE CURL IS MULTI-LINE. A first version scanned raw lines and a
    # mutation walked straight through it: this curl spans three
    # backslash-continuations, so `|| true` parked on the `-H "X-Admin-Key: …"`
    # line belongs to the curl but sits on a line containing neither "curl" nor
    # the endpoint. Join continuations into logical commands FIRST, or the
    # swallow simply moves one line down and the guard reports clean.
    logical, buf = [], ""
    for raw in block.splitlines():
        buf += raw.rstrip()
        if buf.endswith("\\"):
            buf = buf[:-1] + " "
            continue
        logical.append(buf)
        buf = ""
    if buf:
        logical.append(buf)
    for cmd in logical:
        if "curl" in cmd and "infra-growth/snapshot" in cmd:
            assert "|| true" not in cmd, (
                f"{workflow}'s snapshot call itself is swallowed by `|| true` — "
                f"a failed re-baseline reports success and the board stays "
                f"stale:\n    {cmd.strip()[:200]}")
            break
    else:
        raise AssertionError(
            f"{workflow} has no single curl command that calls the snapshot "
            f"endpoint — this test cannot see the call it is meant to guard")
    assert "exit 1" in block, (
        f"{workflow} never exits non-zero on a failed re-baseline; the job "
        f"would go green with /whats-new still publishing pre-ingest counts")
    assert "%{http_code}" in block or "http_code" in block, (
        f"{workflow} does not read the snapshot's status code; a 401 or 503 "
        f"would pass unnoticed")
    assert "dchub-backend-production.up.railway.app" in block, (
        f"{workflow} posts the snapshot through the edge; admin POSTs there "
        f"hit the 15s ROUTE_TIMEOUTS default and this call counts millions of "
        f"rows — it must go to the Railway origin")


def test_the_daily_snapshot_does_not_fire_inside_a_data_sync_window():
    """A snapshot taken mid-sync records a torn count.

    data-sync's sync-infrastructure job runs at 00/06/12/18 UTC. The tracker
    used to fire at 06:09 — nine minutes in, with the gas loader having deleted
    its source tag and not yet rewritten it. That publishes a dip today and a
    matching spike tomorrow: invented deltas on the board whose entire purpose
    is to say whether a layer really grew.
    """
    import re
    src = _read(".github/workflows/infra-growth-tracker.yml")
    crons = re.findall(r"^\s*-\s*cron:\s*'([^']+)'", src, re.M)
    assert crons, "infra-growth-tracker lost its schedule — the backstop is gone"
    for cron in crons:
        minute, hour = cron.split()[0], cron.split()[1]
        assert hour.isdigit() and minute.isdigit(), f"unexpected cron shape: {cron}"
        h, m = int(hour), int(minute)
        # sync starts on the hour at 0/6/12/18 and the job's timeout is 15min.
        if h % 6 == 0:
            assert m > 20, (
                f"tracker cron '{cron}' fires {m} minutes into a data-sync run "
                f"(00/06/12/18 UTC, 15-minute job) — it will snapshot loaders "
                f"mid-flight and record torn counts")
