#!/usr/bin/env python3
"""tests/test_facility_dedup_v4.py — the rendered-identity deduper's decisions.

NO NETWORK, NO DB. plan_group is pure; _collect is driven with a stub cursor.

Each assertion below is a measured lesson from an earlier lane, not a
precaution. The two that matter most:

  ★ POINTER ONLY. `is_duplicate` is a VISIBILITY flag — setting it drops the
    row from every filtered COUNT and from the sitemap. Setting it on 2026-07-28
    left 57 of 58 slugs with NO keeper and was reverted. Consolidation is
    `duplicate_of_id` alone: the row stays live, counted, serving 200, and
    Google merges the two URLs itself. Suppression deletes a page; a canonical
    merges it.

  ★ A MISSED DUPLICATE IS SAFE; A FALSE MERGE HIDES A REAL SITE. 581 of the
    1,205 (name, city) groups v3 examined were Amazon IAD85 / IAD75 / IAD96 at
    Manassas — three distinct buildings under one generic name.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.facility_dedup_v4 import (      # noqa: E402
    DEDUP_METHOD, MAX_GROUP, TWIN_COL, plan_group, is_junk_slug, same_name,
    _collect)


def _df(i, slug, provider=None, lat=None, lon=None, pw=None,
        dup=None, merged=None, name="Acme DC1"):
    return {"table": "discovered_facilities", "id": i, "canonical_slug": slug,
            "name": name, "provider": provider, "latitude": lat,
            "longitude": lon, "power_mw": pw, "duplicate_of_id": dup,
            "merged_facility_id": merged, "discovered_twin_id": None}


def _lg(i, slug, provider=None, lat=None, lon=None, pw=None,
        name="Acme DC1", twin=None):
    return {"table": "facilities", "id": i, "canonical_slug": slug,
            "name": name, "provider": provider, "latitude": lat,
            "longitude": lon, "power_mw": pw, "duplicate_of_id": None,
            "merged_facility_id": None, "discovered_twin_id": twin}


# ── what it writes ───────────────────────────────────────────────────────

def test_two_discovered_rows_get_a_pointer_from_the_alternate_to_the_keeper():
    p = plan_group([_df(1, "a-11111111", pw=50), _df(2, "a-22222222", pw=5)])
    assert p["skip"] is None
    assert p["keeper"]["id"] == 1            # richest wins
    assert p["writes"] == [2]


def test_the_keeper_is_deterministic_when_capacity_ties():
    """The same group must always plan the same way, or two runs disagree about
    which URL Google is being pointed at."""
    rows = [_df(9, "a-99999999"), _df(3, "a-33333333")]
    assert plan_group(rows)["keeper"]["id"] == 3
    assert plan_group(list(reversed(rows)))["keeper"]["id"] == 3


def test_a_drain_fork_is_reported_and_NOT_written():
    """The legacy twin already consolidates through the drain's own
    merged_facility_id stamp (facility_profile_page._drained_twin_url +
    main._drained_keeper). Writing a second pointer here would create a rival
    answer that can disagree with it."""
    p = plan_group([_df(1, "a-11111111", merged="legacy-id"),
                    _lg("legacy-id", "a-22222222")])
    assert p["writes"] == []
    assert p["drain_fork"] == ["a-22222222"]
    assert p["name_mismatch"] == []


def test_an_unlinked_legacy_alternate_with_the_SAME_name_gets_a_twin_pointer():
    """facilities.duplicate_of_id is TEXT and addresses facilities.id, so it
    can never name a discovered keeper. facilities.discovered_twin_id is the
    third id space, added for exactly this class: an independently-ingested
    legacy row (PeeringDB/OSM/operator site) the drain never touched."""
    p = plan_group([_df(1, "a-11111111", name="Equinix FR5"),
                    _lg("other-id", "a-22222222", name="Equinix FR5")])
    assert p["writes"] == []
    assert p["drain_fork"] == []
    assert p["name_mismatch"] == []
    assert p["twin_writes"] == ["other-id"]


def test_the_name_gate_covers_DISCOVERED_pairs_too_not_only_the_legacy_class():
    """★★★ THE REGRESSION THIS EXISTS TO STOP. #4101's apply wrote 26
    discovered->discovered pointers on rendered identity ALONE; 25 of them had
    DIFFERENT names and all 26 were reverted on 2026-09-07. Every row below is
    one of those real pairs, with its real measured separation — and every one
    passes the 2 km coordinate veto, which is why the veto is not the guard
    here and the name gate is.

    Gating only the cross-table class (as this lane first did) leaves THIS
    branch writing the false merges."""
    cases = [
        ("SecureIT DCB1.2", "SecureIT DCB1.1", 0.000),
        ("noris network AG ING1 ITA", "noris network AG ING1 ITB", 0.027),
        ("Equinix FR8.2", "Equinix FR8.1", 0.045),
        ("RIC1 DC2", "RIC1 DC1", 0.131),
        ("RIC1 DC3", "RIC1 DC1", 0.279),
        ("Equinix FR2.6", "Equinix Frankfurt FR2", 0.117),
    ]
    for alt_name, keeper_name, km in cases:
        # co-located, exactly as measured — the veto cannot see these
        p = plan_group([_df(1, "a-11111111", name=keeper_name,
                            lat=50.1109, lon=8.6821),
                        _df(2, "a-22222222", name=alt_name,
                            lat=50.1109, lon=8.6821)])
        assert p["skip"] is None, (alt_name, p)
        assert p["writes"] == [], f"{alt_name!r} ({km} km) would be MERGED into {keeper_name!r}"
        assert p["name_mismatch"] == ["a-22222222"], (alt_name, p)


def test_the_coordinate_veto_does_not_fire_on_the_class_the_gate_catches():
    """The veto is kept but is INERT here, and that must be visible rather than
    assumed: these two rows are 0.000 km apart, so `coords_far_apart` never
    triggers and the group is decided entirely by the name."""
    p = plan_group([_df(1, "a-11111111", name="SecureIT DCB1.1",
                        lat=49.5, lon=6.1),
                    _df(2, "a-22222222", name="SecureIT DCB1.2",
                        lat=49.5, lon=6.1)])
    assert p["skip"] is None            # the veto did NOT stop this group
    assert p["writes"] == []            # the NAME did
    assert p["name_mismatch"] == ["a-22222222"]


def test_a_DIFFERENT_name_under_one_rendered_h1_is_refused():
    """★ THE GATE. Measured 2026-09-07: 202 pairs render an identical <h1> from
    DIFFERENT names, and 197 of them do so only because
    util.facility_site_code.site_code_headline rewrites the <h1> down to
    "<Operator> <CODE> — <City> Data Center" and drops the tail. These two are
    real rows from that set — two halls of one campus, 0.00 km apart, one <h1>.
    Merging them would hide a real site, which is the failure this whole family
    of lanes exists to avoid."""
    p = plan_group([_df(1, "a-11111111", name="SecureIT DCB1.1"),
                    _lg("other-id", "a-22222222", name="SecureIT DCB1.2")])
    assert p["twin_writes"] == []
    assert p["name_mismatch"] == ["a-22222222"]


def test_the_name_gate_folds_case_and_whitespace_only():
    """Folded the way identity_key folds the h1 — "Orange Business Services"
    and "orange  business services" are one name. Nothing else is folded: the
    gate must not start normalising away the very tails it exists to keep."""
    assert same_name("Orange Business Services", "orange  business services")
    assert same_name(" Equinix FR5 ", "Equinix FR5")
    assert not same_name("noris network AG ING1 ITA", "noris network AG ING1 ITB")
    assert not same_name("Equinix FR2", "Equinix FR2.6")
    assert not same_name("CoreSite - Denver (DE2)", "CoreSite DE2")


def test_a_twin_pointer_already_set_is_not_re_reported_as_outstanding():
    """v3 spent 2026-08-16 re-counting its own output as a 12x over-report
    because its scan could not see what it had already done."""
    p = plan_group([_df(1, "a-11111111", name="Equinix FR5"),
                    _lg("other-id", "a-22222222", name="Equinix FR5",
                        twin=1)])
    assert p["twin_writes"] == []
    assert p["name_mismatch"] == []
    assert p["twin_done"] == ["a-22222222"]


def test_a_drain_fork_outranks_the_twin_pointer():
    """The drain's own merged_facility_id stamp is evidence the house did not
    invent; discovered_twin_id is an inference. Where both could apply the
    drain link wins — and main._build_sitemap_sections applies the same
    precedence with setdefault, so the sitemap and the rel=canonical can never
    name different keepers for one slug."""
    p = plan_group([_df(1, "a-11111111", name="Equinix FR5",
                        merged="legacy-id"),
                    _lg("legacy-id", "a-22222222", name="Equinix FR5")])
    assert p["drain_fork"] == ["a-22222222"]
    assert p["twin_writes"] == []


def test_it_never_overwrites_another_lanes_pointer():
    """v2/v3 verdicts outrank ours. The WHERE re-asserts this at write time
    too, so a pointer landing between analyze and apply is still safe."""
    p = plan_group([_df(1, "a-11111111", pw=50),
                    _df(2, "a-22222222", pw=5, dup=7)])
    assert p["writes"] == []


def test_a_row_that_already_points_elsewhere_is_never_elected_keeper():
    """A keeper that points onward is not a canonical target — that is how a
    canonical CHAIN starts. Row 1 is the richest and would win on capacity, but
    it already points at 7, so the keeper is row 2 and row 3 is sent there."""
    p = plan_group([_df(1, "a-11111111", pw=99, dup=7),
                    _df(2, "a-22222222"), _df(3, "a-33333333")])
    assert p["skip"] is None
    assert p["keeper"]["id"] == 2
    assert p["writes"] == [3]


def test_a_group_already_consolidated_by_another_lane_is_a_no_op():
    """Nothing left to write is not a failure and must not be re-reported as
    outstanding work — v3 spent 2026-08-16 re-counting its own output as a 12x
    over-report because its scan could not see what it had already done."""
    p = plan_group([_df(1, "a-11111111", pw=99, dup=7), _df(2, "a-22222222")])
    assert p["skip"] == "nothing_to_do"
    assert p["writes"] == []


# ── what it refuses ──────────────────────────────────────────────────────

def test_coordinates_veto_a_merge():
    """Coordinates VETO, never justify. Two rows 5km apart are not one
    building however their pages read."""
    p = plan_group([_df(1, "a-11111111", lat=40.0, lon=-70.0),
                    _df(2, "a-22222222", lat=40.05, lon=-70.0)])
    assert p["skip"] == "coords_far_apart"
    assert p["writes"] == []


def test_a_missing_coordinate_does_not_block():
    """A missing coordinate is not evidence of distance — the drain forks
    routinely carry none at all, and vetoing on absence would refuse the entire
    population this lane exists for."""
    p = plan_group([_df(1, "a-11111111", lat=40.0, lon=-70.0),
                    _df(2, "a-22222222")])
    assert p["skip"] is None and p["writes"] == [2]


def test_a_group_larger_than_the_cap_is_refused():
    """Live histogram is {2: 3,976 · 3: 11 · 5: 1 · 6: 1}. Anything bigger is a
    generic-name collision, not a facility.

    ★ The size is a LITERAL, not MAX_GROUP + 1. Written the obvious way this
    test read the module's own tunable, so raising MAX_GROUP to 10,000 moved
    the fixture with it and the mutation survived — a guard that cannot fail.
    The cap is pinned separately below, so changing it stays a deliberate,
    visible edit rather than a silent widening."""
    rows = [_df(i, f"a-{i:08d}") for i in range(1, 7)]      # 6 distinct URLs
    assert plan_group(rows)["skip"] == "group_too_large"
    # ...and 4 is still accepted, so the cap is a boundary and not an off switch
    ok = plan_group([_df(i, f"a-{i:08d}") for i in range(1, 5)])
    assert ok["skip"] is None and ok["writes"] == [2, 3, 4]


def test_the_group_cap_is_four():
    """Pinned so a widening is a code review, not a side effect."""
    assert MAX_GROUP == 4


def test_a_group_with_no_discovered_row_is_refused():
    """duplicate_of_id addresses discovered_facilities.id and nothing else."""
    p = plan_group([_lg("x", "a-11111111"), _lg("y", "a-22222222")])
    assert p["skip"] == "no_discovered_keeper"


def test_one_url_is_not_a_group():
    """Two ROWS sharing one canonical_slug are one URL — the 6,846-of-7,157
    lesson: 'this slug belongs to a duplicate' is not 'this URL is
    redundant'."""
    assert plan_group([_df(1, "a-11111111"),
                       _df(2, "a-11111111")])["skip"] == "single_url"


def test_junk_slugs_are_excluded_at_source():
    assert is_junk_slug("unknown-osm-dc-123-ab12cd34")
    assert is_junk_slug("data-center-343593591-ab12cd34")
    assert not is_junk_slug("equinix-dc5-ab12cd34")


# ── it never sets the visibility flag ────────────────────────────────────

def _updates(src):
    """Every `UPDATE <table> ... SET <clause>` in a source file, as
    (table, set_clause). The point of these tests is WHICH COLUMNS each
    statement assigns, and a substring search over a whole file cannot tell an
    assignment from a guard.

    ★ Read through the AST, not off the raw text. Every SQL string in this
      module is either implicit literal concatenation across lines or an
      f-string interpolating TWIN_COL; a regex over the source matches NEITHER
      and would report an empty list — a guard that passes because it looked at
      nothing. ast joins adjacent literals for us, and JoinedStr is walked so
      the f-strings read as the SQL they actually become.
    """
    import ast, re

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            out = []
            for part in node.values:
                if isinstance(part, ast.Constant):
                    out.append(str(part.value))
                elif isinstance(part, ast.FormattedValue) and \
                        isinstance(part.value, ast.Name):
                    out.append({"TWIN_COL": TWIN_COL}.get(part.value.id, "?"))
                else:
                    out.append("?")
            return "".join(out)
        return None

    found = []
    for node in ast.walk(ast.parse(src)):
        text = literal(node)
        if not text or "UPDATE " not in text:
            continue
        for m in re.finditer(
                r"UPDATE\s+(\w+)(?:\s+\w+)?\s+SET\s+(.*?)\s+WHERE", text, re.S):
            found.append((m.group(1), " ".join(m.group(2).split())))
    return found


def test_apply_writes_only_pointers_and_never_the_visibility_flag():
    """SOURCE-pinned because these UPDATEs are the only things here that touch
    production data. Every SET clause in the module is enumerated and checked,
    so a new statement cannot slip in unexamined — if `is_duplicate` ever
    appears on the SET side of any of them, 2026-07-28 repeats."""
    src = open(os.path.join(ROOT, "routes", "facility_dedup_v4.py"),
               encoding="utf-8").read()
    ups = _updates(src)
    # Sorted: ast.walk does not yield in source order, and the invariant is the
    # SET of statements, not their order in the file.
    assert sorted(ups) == sorted([
        ("discovered_facilities", "duplicate_of_id = %s"),        # _REPOINT_SQL
        ("facilities", f"{TWIN_COL} = %s"),                       # _TWIN_WRITE_SQL
        ("discovered_facilities",
         "duplicate_of_id = %s, dedup_method = %s"),              # apply
        ("discovered_facilities",
         "duplicate_of_id = NULL, dedup_method = NULL"),          # undo
        ("facilities", f"{TWIN_COL} = NULL"),                     # undo
    ]), ups
    for table, sets in ups:
        assert "is_duplicate" not in sets, (table, sets)
    assert "COALESCE(is_duplicate, 0) = 0" in src      # read as a guard only


def test_the_twin_column_is_written_by_this_lane_alone():
    """undo clears facilities.discovered_twin_id with NO method filter, which is
    only correct while this module is the column's sole writer. If another
    module starts writing it, that undo silently rolls back its decisions."""
    import subprocess
    hits = sorted(os.path.relpath(f, ROOT) for f in subprocess.run(
        ["grep", "-rl", "--include=*.py", TWIN_COL, ROOT],
        capture_output=True, text=True).stdout.split())
    # ★ FLOOR. A repo-wide scan that finds nothing passes every assertion
    # below it. These three files are known to name the column — if the scan
    # stops seeing them it is broken, not clean.
    for known in ("routes/facility_dedup_v4.py", "routes/facility_profile_page.py",
                  "main.py"):
        assert known in hits, (known, hits)
    writers = []
    for f in hits:
        body = open(os.path.join(ROOT, f), encoding="utf-8").read()
        for _table, sets in _updates(body):
            if TWIN_COL in sets:
                writers.append(f)
    assert sorted(set(writers)) == ["routes/facility_dedup_v4.py"], writers


def test_the_method_stamp_is_unique_to_this_lane():
    """undo clears rows stamped by THIS lane only — a v4 undo must never roll
    back a v2 or v3 decision."""
    assert DEDUP_METHOD == "rendered-identity/v4"
    for other in ("brand+site_token/v2", "anon-provider-variant/v3",
                  "geo_crosscountry"):
        assert DEDUP_METHOD != other


# ── end to end over a stub cursor ────────────────────────────────────────

class _Cur:
    """Answers the two scan queries in the order _collect issues them.

    The information_schema probe for the twin column is answered separately —
    reporting it ABSENT, so these fixtures exercise the same fail-open path a
    deploy that has not yet taken an admin hit runs on."""

    def __init__(self, discovered, legacy):
        self._q = [discovered, legacy]
        self._rows = []
        self._probe = False

    def execute(self, sql, params=None):
        self._probe = "information_schema" in sql
        if not self._probe:
            self._rows = self._q.pop(0) if self._q else []
        return self

    def fetchone(self):
        return None if self._probe else (self._rows[0] if self._rows else None)

    def fetchall(self):
        return self._rows


def test_collect_groups_across_the_two_tables_on_the_rendered_identity():
    """The whole point: the pair disagrees about `provider` — that is WHY the
    slugs differ — and is still one group."""
    # (tbl, id, slug, name, provider, city, state, country, lat, lon, pw,
    #  duplicate_of_id, merged_facility_id, discovered_twin_id)
    discovered = [("discovered_facilities", "12300071",
                   "007-hebergement-paris-a8b78433", "007 Hebergement Paris",
                   None, "Paris", None, "FR", None, None, None, None,
                   "007-hebergement-paris-paris-fr", None)]
    legacy = [("facilities", "007-hebergement-paris-paris-fr",
               "007-hebergement-paris-d128fc26", "007 Hebergement Paris",
               "007 Hebergement Paris", "Paris", None, "FR",
               None, None, None, None, None, None)]
    plans, stats = _collect(_Cur(discovered, legacy))
    assert len(plans) == 1, plans
    assert plans[0]["keeper_slug"] == "007-hebergement-paris-a8b78433"
    assert plans[0]["drain_fork"] == ["007-hebergement-paris-d128fc26"]
    assert stats.get("drain_fork_no_write") == 1


def test_collect_does_not_group_two_distinct_buildings():
    """Amazon IAD85 / IAD75 at one address: same provider, same city, DIFFERENT
    names, so different <h1>s and different facilities. A lane that merged
    these would hide a real site."""
    discovered = [
        ("discovered_facilities", "1", "amazon-iad85-11111111", "Amazon IAD85",
         "Amazon", "Manassas", "VA", "US", 38.779, -77.542, None, None, None,
         None),
        ("discovered_facilities", "2", "amazon-iad75-22222222", "Amazon IAD75",
         "Amazon", "Manassas", "VA", "US", 38.779, -77.543, None, None, None,
         None),
    ]
    plans, _ = _collect(_Cur(discovered, []))
    assert plans == []


def test_collect_skips_nameless_rows():
    """_render_profile defaults a NULL name to "Data Center", so nameless rows
    would collapse into one enormous false cluster and merge unrelated sites."""
    discovered = [
        ("discovered_facilities", "1", "unknown-osm-dc-1-11111111", None,
         None, "Paris", None, "FR", None, None, None, None, None, None),
        ("discovered_facilities", "2", "unknown-osm-dc-2-22222222", None,
         None, "Paris", None, "FR", None, None, None, None, None, None),
    ]
    plans, _ = _collect(_Cur(discovered, []))
    assert plans == []


# ── apply(), end to end over a recording connection ──────────────────────
#
# Everything above is pure. These drive the real endpoint so the ORDER and the
# PARAMETERS of the writes are covered, not just the strings they are made of —
# the chain fix is a claim about sequencing, and a source-pinned test cannot
# make it.

class _RecCur:
    def __init__(self, log):
        self.log = log
        self.rowcount = 1
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        one = " ".join(str(sql).split())
        self.log.append((one, params))
        self._probe = "information_schema" in one
        return self
    def fetchone(self):
        # the twin column already exists -> ensure_twin_schema issues no ALTER
        return (1,) if getattr(self, "_probe", False) else None
    def fetchall(self): return []
    def close(self): pass


class _RecConn:
    def __init__(self): self.log = []
    def cursor(self): return _RecCur(self.log)
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


def _run_apply(monkeypatch, plans):
    """POST /apply with _collect stubbed. Returns (json, executed_log)."""
    import flask
    from routes import facility_dedup_v4 as v4

    conn = _RecConn()
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    monkeypatch.delenv("FACILITY_DEDUP_V4_DISABLE", raising=False)
    monkeypatch.setattr(v4, "_conn", lambda write=False: conn)
    monkeypatch.setattr(v4, "_collect", lambda cur, limit=None: (plans, {}))

    app = flask.Flask(__name__)
    app.register_blueprint(v4.facility_dedup_v4_bp)
    r = app.test_client().post(
        "/api/v1/admin/facility-dedup-v4/apply?confirm=1",
        headers={"X-Admin-Key": "k"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json(), conn.log


def _plan(keeper_id=100, writes=(), twin_writes=()):
    return {"h1": "acme dc1", "keeper_id": keeper_id, "keeper_slug": "k-1",
            "writes": list(writes), "twin_writes": list(twin_writes),
            "drain_fork": [], "twin_done": [], "name_mismatch": []}


def test_apply_repoints_chain_rows_BEFORE_it_makes_the_alternate():
    """★ THE CHAIN FIX. plan_group already refuses to elect a keeper that points
    onward; nothing checked whether rows pointed AT the row it makes an
    alternate. Measured after #4101: pointer_chains 350 -> 359, all nine a v2
    row X -> Y where v4 had just written Y -> Z.

    ORDER IS LOAD-BEARING, so it is asserted rather than assumed: X -> keeper is
    correct whichever statement fails, but only in this order. Reversed, a crash
    between the two leaves X -> Y -> Z — the very chain this closes."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        out, log = _run_apply(mp, [_plan(keeper_id=100, writes=[7, 8])])
    finally:
        mp.undo()
    sql = [q for q, _ in log if q.startswith("UPDATE")]
    assert len(sql) == 2, sql
    assert sql[0].startswith("UPDATE discovered_facilities SET duplicate_of_id = %s "
                             "WHERE duplicate_of_id = ANY(%s)"), sql[0]
    assert "SET duplicate_of_id = %s, dedup_method = %s" in sql[1], sql[1]

    params = [p for q, p in log if q.startswith("UPDATE")]
    # repoint: everything pointing at 7 or 8 moves to the keeper, and the
    # keeper is excluded so it can never be made its own duplicate
    assert params[0] == (100, [7, 8], 100)
    assert params[1] == (100, DEDUP_METHOD, [7, 8], 100)
    assert out["chain_rows_repointed"] == 1


def test_apply_writes_the_twin_pointer_for_an_unlinked_legacy_row():
    """The write is gated on the keeper still being usable — the same four
    preconditions facility_profile_page._twin_pointer_url reads it under, so a
    pointer can never name a keeper that page would refuse."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        out, log = _run_apply(mp, [_plan(keeper_id=100, twin_writes=["lg-1"])])
    finally:
        mp.undo()
    ups = [(q, p) for q, p in log if q.startswith("UPDATE")]
    assert len(ups) == 1, ups          # no discovered writes planned
    q, p = ups[0]
    assert q.startswith(f"UPDATE facilities f SET {TWIN_COL} = %s")
    assert f"f.{TWIN_COL} IS NULL" in q            # never overwrite
    assert "COALESCE(d.is_duplicate, 0) = 0" in q  # keeper not suppressed
    assert "d.duplicate_of_id IS NULL" in q        # keeper points at nobody
    assert "d.canonical_slug <> ''" in q           # keeper has a real slug
    assert p == (100, ["lg-1"], 100)
    assert out["twin_pointers_written"] == 1


def test_apply_does_not_repoint_when_it_makes_no_alternate():
    """A group whose only work is a twin pointer must not issue the chain
    UPDATE — _REPOINT_SQL with an empty id list would still scan, and an
    UPDATE that runs for nothing is how a lane grows a cost it cannot see."""
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        _out, log = _run_apply(mp, [_plan(keeper_id=100, twin_writes=["lg-1"])])
    finally:
        mp.undo()
    assert not [q for q, _ in log if "WHERE duplicate_of_id = ANY" in q]



def test_the_ddl_runs_in_a_real_transaction_so_lock_timeout_applies():
    """★ `SET LOCAL lock_timeout` is a NO-OP under autocommit: each statement
    becomes its own transaction, so the setting is gone before the ALTER runs
    and the 2s bound silently does not exist. _conn(write=True) returns an
    autocommit connection, so ensure_twin_schema must turn it off around the
    DDL — and turn it back on, because the connection is pooled.

    Asserted on the ORDER of what the connection saw, not on the source text."""
    from routes import facility_dedup_v4 as v4

    events = []

    class _C:
        rowcount = 0
        def __init__(self, ev): self.ev = ev
        def execute(self, sql, params=None):
            self.ev.append(("sql", " ".join(str(sql).split())))
            self._probe = "information_schema" in sql
            return self
        def fetchone(self):
            return None if getattr(self, "_probe", False) else None

    class _Conn:
        def __init__(self, ev):
            self.ev = ev
            self._ac = True
        @property
        def autocommit(self): return self._ac
        @autocommit.setter
        def autocommit(self, v):
            self._ac = v
            self.ev.append(("autocommit", v))
        def cursor(self): return _C(self.ev)
        def commit(self): self.ev.append(("commit", None))
        def rollback(self): self.ev.append(("rollback", None))

    conn = _Conn(events)
    added = v4.ensure_twin_schema(conn)
    assert added == [f"facilities.{TWIN_COL}"], added

    kinds = [e[0] for e in events]
    sqls = [e[1] for e in events if e[0] == "sql"]
    # autocommit off BEFORE any DDL, SET LOCAL before the ALTER, commit after
    assert events[0] == ("autocommit", False), events[:3]
    i_set = next(i for i, q in enumerate(sqls) if "SET LOCAL lock_timeout" in q)
    i_alter = next(i for i, q in enumerate(sqls) if q.startswith("ALTER TABLE facilities"))
    assert i_set < i_alter, sqls
    assert "commit" in kinds
    # ...and the pooled connection is handed back with autocommit RESTORED
    assert events[-1] == ("autocommit", True), events[-3:]
    assert conn.autocommit is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
