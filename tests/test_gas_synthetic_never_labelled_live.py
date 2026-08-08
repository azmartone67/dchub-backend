"""Gas basis-labelling fence — 2026-08-07.

THE ONE SENTENCE
----------------
These tests FAIL if any gas field whose source is a synthetic / seed /
default constant is served with data_basis 'live'.

WHY THIS FILE EXISTS
--------------------
The DCGI gas index was withdrawn 2026-08-08 and its withdrawal notice points
analysts at /api/v1/markets/<slug>/gas-pricing as the surface that IS still
published. That designated safe harbour was itself mislabelled. Measured live
on phoenix, 2026-08-07:

    "basis_diff_usd_mmbtu": 0.65,
    "basis_source":         "synthetic_seed_basis",
    "data_basis":           "live",
    "fetched_at":           "2026-08-07T04:00:56Z"   (~26h old, no age given)

0.65 is a hardcoded entry in SYNTHETIC_BASIS_BY_HUB for the SoCal Border hub.
It has never been observed. The old envelope rule was

    data_basis = "live" if hh_src.startswith("eia_") and delivered else ...

which looked at layers 1 and 3 and ignored layer 2 entirely, so a live Henry
Hub read plus a live delivered tariff was enough to stamp the whole payload
"live" while the basis differential was a constant.

WHAT IS GUARDED
---------------
1. _source_basis() classifies every synthetic-marked source as "synthetic",
   never "live" — including the deceptive 'synthetic_seed_eia_unreachable',
   which is a seed served *because* a live read failed.
2. Derived fields inherit the WEAKEST label of their inputs, so a synthetic
   basis makes hub_spot_usd_mmbtu synthetic even when Henry Hub is live.
3. The envelope data_basis can never outrank its weakest published field.
4. A cached market_gas_pricing row carrying the OLD data_basis='live' column
   is relabelled at serve time — the fix cannot be defeated by stale rows.
5. The route actually calls the relabeller (a correct helper nobody calls is
   not a fix).
6. An AST sweep over the whole gas route family fails if any dict literal
   pairs a synthetic-marked *_source with a 'live' basis label.

WHY THE EXTRACTOR IS ASSERTED, NOT TRUSTED
------------------------------------------
This file pulls the shipped functions out of routes/powered_land_gas.py with
ast rather than importing it (importing drags in flask + util.gas_index).
A source extractor fails OPEN: rename a function and the extraction finds
nothing, every test silently degenerates, and the file goes green while the
defect is untouched. test_extraction_is_live() therefore runs the extractor
and asserts every required symbol was located, before any verdict is read.

MUTATION-VERIFIED — see MUTATIONS_APPLIED at the bottom of this file.
"""
import ast
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PLG_PATH = os.path.join(ROOT, "routes", "powered_land_gas.py")

# The gas route family this fence sweeps. This file is under tests/ and is
# deliberately NOT in the scan set — a guard that greps the pattern it bans
# would otherwise flag its own docstring.
GAS_ROUTE_FILES = (
    "powered_land_gas.py",
    "gas_intelligence.py",
    "dcgi.py",
    "gas_price_feeds.py",
    "gas_pipeline_ingest.py",
    "eu_gas_entsog.py",
)

REQUIRED_FUNCS = (
    "_source_basis",
    "_weakest",
    "_envelope_basis_from_sources",
    "_age_hours",
    "_apply_honest_basis",
)
REQUIRED_CONSTS = (
    "_SYNTHETIC_SOURCE_MARKERS",
    "_UNAVAILABLE_SOURCES",
    "_BASIS_RANK",
    "GAS_PRICING_STALE_AFTER_HOURS",
    "_FIELD_SOURCE_KEY",
    "_FIELD_DERIVED_FROM",
    "_FIELD_NOTES",
    "SYNTHETIC_BASIS_BY_HUB",
)

# Markers that make a source string a non-measurement. Held independently of
# the shipped tuple on purpose: if someone narrows the shipped markers to make
# a red test go green, this list still catches the source strings below.
BANNED_MARKERS = ("synthetic", "seed", "fallback", "default", "stub",
                  "placeholder")


# ── Extractor ───────────────────────────────────────────────────────────────

def _load_plg_namespace():
    """Exec the target functions + constants from routes/powered_land_gas.py
    against stubs. Returns (namespace, missing_symbols)."""
    src = open(PLG_PATH, encoding="utf-8").read()
    tree = ast.parse(src)

    wanted_funcs = set(REQUIRED_FUNCS)
    wanted_consts = set(REQUIRED_CONSTS)
    chunks = []
    found_funcs, found_consts = set(), set()

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            chunks.append(ast.get_source_segment(src, node))
            found_funcs.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            hit = [n for n in names if n in wanted_consts]
            if hit:
                chunks.append(ast.get_source_segment(src, node))
                found_consts.update(hit)

    ns = {
        "datetime": datetime, "timezone": timezone,
        "Optional": Optional, "Any": Any,
        "__builtins__": __builtins__,
    }
    if chunks:
        exec(compile("\n\n".join(chunks), "<plg-extract>", "exec"), ns)

    missing = sorted((wanted_funcs - found_funcs) | (wanted_consts - found_consts))
    return ns, missing


NS, MISSING = _load_plg_namespace()


def _fn(name):
    fn = NS.get(name)
    assert callable(fn), f"{name} was not extracted from routes/powered_land_gas.py"
    return fn


# ── 0. The extractor itself ─────────────────────────────────────────────────

def test_extraction_is_live():
    """Fail LOUDLY if the shipped symbols moved, instead of degenerating into
    a green file that asserts nothing."""
    assert not MISSING, (
        "Could not extract from routes/powered_land_gas.py: "
        + ", ".join(MISSING)
        + ". These tests would silently pass on nothing. Re-point the "
          "extractor at the renamed symbols."
    )


# ── 1. No synthetic source may ever classify as live ────────────────────────

# Every source string routes/powered_land_gas.py can actually emit for a
# synthetic value, transcribed from the shipped fetchers.
SYNTHETIC_SOURCE_STRINGS = (
    "synthetic_seed",
    "synthetic_seed_basis",
    "synthetic_seed_basis_fallback",
    "synthetic_seed_eia_unreachable",
    "synthetic_seed_eia_empty",
    "synthetic_seed_exception",
    "SYNTHETIC_SEED_BASIS",          # case must not matter
    "hub_model_default",
    "seeded_constant",
)

LIVE_SOURCE_STRINGS = (
    "eia_v2_natural-gas/pri/fut",
    "eia_gas_prices",
    "eia_gas_prices (electric-power sector)",
)


@pytest.mark.parametrize("src", SYNTHETIC_SOURCE_STRINGS)
def test_synthetic_source_never_classifies_live(src):
    assert _fn("_source_basis")(src) != "live", (
        f"source {src!r} classified as LIVE. A seed constant served because a "
        f"live read failed is still a seed constant."
    )
    assert _fn("_source_basis")(src) == "synthetic"


@pytest.mark.parametrize("src", LIVE_SOURCE_STRINGS)
def test_real_eia_source_still_classifies_live(src):
    """The fence must not fail closed onto everything — a genuine EIA read
    has to keep its honest 'live' label, or the label means nothing."""
    assert _fn("_source_basis")(src) == "live"


def test_shipped_basis_fetcher_still_emits_a_synthetic_marker():
    """_basis_for_hub() is the source of the mislabelled 0.65. If Phase 2
    wires a live basis feed this assertion is the thing that tells us to
    revisit the fence rather than leaving a stale 'synthetic' note behind."""
    src = open(PLG_PATH, encoding="utf-8").read()
    m = re.search(r"def _basis_for_hub\(.*?\n(?=\ndef |\n# )", src, re.S)
    assert m, "_basis_for_hub() not found — the basis source moved."
    body = m.group(0)
    returns = re.findall(r'return \([^)]*?,\s*"([^"]+)"\)', body)
    assert returns, f"_basis_for_hub returns no labelled source: {body!r}"
    for r in returns:
        assert any(mk in r.lower() for mk in BANNED_MARKERS), (
            f"_basis_for_hub now returns source {r!r} with no synthetic marker. "
            "If a real basis differential is now available, label it live "
            "honestly and update this fence; do not leave it ambiguous."
        )


# ── 2 & 3. Payload-level: the exact live phoenix shape ──────────────────────

def _phoenix_payload():
    """The shape measured live on 2026-08-07: Henry Hub live, delivered
    tariff live, basis a seed constant, envelope stamped 'live'."""
    return {
        "market_slug": "phoenix",
        "state": "AZ",
        "pricing_hub_key": "socal_border",
        "henry_hub_spot_usd_mmbtu": 2.95,
        "basis_diff_usd_mmbtu": 0.65,
        "hub_spot_usd_mmbtu": 3.60,
        "delivered_industrial_usd_mmbtu": 4.12,
        "delivered_electric_usd_mmbtu": None,
        "hh_source": "eia_v2_natural-gas/pri/fut",
        "basis_source": "synthetic_seed_basis",
        "delivered_source": "eia_gas_prices",
        "data_basis": "live",                     # ← the stale cached label
        "fetched_at": "2026-08-07T04:00:56.943468+00:00",
    }


def test_phoenix_envelope_is_not_live():
    out = _fn("_apply_honest_basis")(_phoenix_payload())
    assert out["data_basis"] != "live", (
        "phoenix still stamped data_basis 'live' while basis_source is "
        "synthetic_seed_basis — this is the reported defect, unfixed."
    )
    assert out["data_basis"] == "mixed"


def test_phoenix_basis_field_labelled_per_field():
    out = _fn("_apply_honest_basis")(_phoenix_payload())
    fb = out["field_basis"]
    assert fb["basis_diff_usd_mmbtu"]["basis"] == "synthetic"
    assert "note" in fb["basis_diff_usd_mmbtu"], (
        "the synthetic basis field must say so in its own entry, not only at "
        "the envelope"
    )
    assert "basis_diff_usd_mmbtu" in out["synthetic_fields"]
    # The genuinely-live layers keep their honest label.
    assert fb["henry_hub_spot_usd_mmbtu"]["basis"] == "live"
    assert fb["delivered_industrial_usd_mmbtu"]["basis"] == "live"


def test_derived_hub_spot_inherits_the_synthetic_basis():
    out = _fn("_apply_honest_basis")(_phoenix_payload())
    assert out["field_basis"]["hub_spot_usd_mmbtu"]["basis"] == "synthetic", (
        "hub_spot = henry_hub + basis. A live Henry Hub plus a seeded basis "
        "is a modelled hub price, not a measured settle."
    )


# ── THE REQUIRED INVARIANT ──────────────────────────────────────────────────

def _source_matrix():
    """Every combination of live / synthetic / missing across the three
    source columns, so the invariant is checked on the whole space and not
    just on phoenix."""
    opts = ["eia_v2_natural-gas/pri/fut", "synthetic_seed_basis",
            "synthetic_seed_eia_unreachable", "eia_gas_prices", "", "no_state"]
    for hh in opts:
        for basis in opts:
            for dl in opts:
                yield hh, basis, dl


def test_no_field_with_a_synthetic_source_is_ever_served_live():
    """★ The invariant the whole fix exists for.

    Across every source combination the endpoint can produce: no field may
    carry basis 'live' if the source string that decided it is synthetic, and
    the envelope may not read 'live' while any published field is synthetic.
    """
    apply = _fn("_apply_honest_basis")
    for hh, basis, dl in _source_matrix():
        payload = {
            "henry_hub_spot_usd_mmbtu": 2.95,
            "basis_diff_usd_mmbtu": 0.65,
            "hub_spot_usd_mmbtu": 3.60,
            "delivered_industrial_usd_mmbtu": 4.12,
            "delivered_electric_usd_mmbtu": 4.01,
            "hh_source": hh, "basis_source": basis, "delivered_source": dl,
            "data_basis": "live",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        out = apply(dict(payload))
        srcs = {"hh_source": hh, "basis_source": basis, "delivered_source": dl}

        synthetic_seen = False
        for field, entry in out["field_basis"].items():
            if entry.get("source") == "derived":
                inputs = entry["derived_from"]
                dirty = [k for k in inputs
                         if any(m in (srcs[k] or "").lower() for m in BANNED_MARKERS)]
                if dirty:
                    synthetic_seen = True
                    assert entry["basis"] != "live", (
                        f"derived field {field} served LIVE while its inputs "
                        f"{dirty} are synthetic ({srcs})"
                    )
            else:
                s = (entry.get("source") or "").lower()
                if any(m in s for m in BANNED_MARKERS):
                    synthetic_seen = True
                    assert entry["basis"] != "live", (
                        f"field {field} served data_basis LIVE with "
                        f"source {entry['source']!r}"
                    )

        if synthetic_seen:
            assert out["data_basis"] != "live", (
                f"envelope data_basis 'live' with synthetic fields "
                f"{out['synthetic_fields']} (sources {srcs})"
            )
            assert out["synthetic_fields"], (
                "synthetic fields present but synthetic_fields is empty"
            )
            assert "data_basis_note" in out


def test_envelope_reads_live_only_when_everything_is_measured():
    """Fail-closed is not the goal — honest is. An all-live payload must
    still be allowed to say 'live', or the label carries no information."""
    out = _fn("_apply_honest_basis")({
        "henry_hub_spot_usd_mmbtu": 2.95,
        "basis_diff_usd_mmbtu": 0.65,
        "hub_spot_usd_mmbtu": 3.60,
        "delivered_industrial_usd_mmbtu": 4.12,
        "hh_source": "eia_v2_natural-gas/pri/fut",
        "basis_source": "ice_regional_spot_settle",
        "delivered_source": "eia_gas_prices",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })
    assert out["data_basis"] == "live"
    assert out["synthetic_fields"] == []


def test_write_path_envelope_never_live_with_a_synthetic_source():
    """★ _envelope_basis_from_sources is the WRITE path — it decides the
    data_basis COLUMN the nightly cron persists into market_gas_pricing, which
    /api/v1/markets/gas-pricing/status then counts. Fixing only the serve-time
    relabeller would leave 'live' sitting in the database.

    (This test was added because a mutation flipping this function's 'mixed'
    to 'live' initially SURVIVED — the serve path was covered, the write path
    was not.)
    """
    env = _fn("_envelope_basis_from_sources")
    for hh, basis, dl in _source_matrix():
        got = env(hh, basis, dl)
        dirty = [s for s in (hh, basis, dl)
                 if any(m in (s or "").lower() for m in BANNED_MARKERS)]
        if dirty:
            assert got != "live", (
                f"cron would WRITE data_basis 'live' into market_gas_pricing "
                f"with synthetic sources {dirty} (hh={hh!r} basis={basis!r} "
                f"delivered={dl!r})"
            )
    # and it must still be able to say 'live' when everything is measured
    assert env("eia_v2_natural-gas/pri/fut", "ice_regional_spot",
               "eia_gas_prices") == "live"


def test_all_synthetic_reads_synthetic_seed():
    out = _fn("_apply_honest_basis")({
        "henry_hub_spot_usd_mmbtu": 2.95,
        "basis_diff_usd_mmbtu": 0.65,
        "hub_spot_usd_mmbtu": 3.60,
        "hh_source": "synthetic_seed",
        "basis_source": "synthetic_seed_basis",
        "delivered_source": "no_state",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })
    assert out["data_basis"] == "synthetic_seed"


# ── 4. Stale cached rows cannot smuggle the old label out ───────────────────

def test_cached_live_column_is_overwritten_at_serve_time():
    """market_gas_pricing rows written before 2026-08-07 carry data_basis
    'live' in the COLUMN. Serving them unchanged reproduces the defect even
    with the compute path fixed."""
    row = _phoenix_payload()
    assert row["data_basis"] == "live"          # precondition, as stored
    out = _fn("_apply_honest_basis")(row)
    assert out["data_basis"] == "mixed"


# ── 5. Freshness: an age and a stated threshold ─────────────────────────────

def test_age_hours_is_published_and_matches_fetched_at():
    ts = datetime.now(timezone.utc) - timedelta(hours=26)
    out = _fn("_apply_honest_basis")({
        "henry_hub_spot_usd_mmbtu": 2.95,
        "basis_diff_usd_mmbtu": 0.65,
        "hh_source": "eia_v2_natural-gas/pri/fut",
        "basis_source": "synthetic_seed_basis",
        "delivered_source": "eia_gas_prices",
        "fetched_at": ts.isoformat(),
    })
    assert out["age_hours"] == pytest.approx(26.0, abs=0.1)
    assert out["as_of_age"] == "26.0h"
    assert out["staleness_threshold_hours"] == NS["GAS_PRICING_STALE_AFTER_HOURS"]
    assert out["is_stale"] is False              # 26h < 36h threshold
    assert str(int(NS["GAS_PRICING_STALE_AFTER_HOURS"])) in out["staleness_note"]


def test_row_past_the_threshold_is_flagged_stale():
    ts = datetime.now(timezone.utc) - timedelta(hours=50)
    out = _fn("_apply_honest_basis")({
        "henry_hub_spot_usd_mmbtu": 2.95,
        "hh_source": "eia_v2_natural-gas/pri/fut",
        "basis_source": "synthetic_seed_basis",
        "delivered_source": "eia_gas_prices",
        "fetched_at": ts.isoformat(),
    })
    assert out["is_stale"] is True
    assert "STALE" in out["staleness_note"]


def test_unparseable_timestamp_reports_unknown_not_zero():
    out = _fn("_apply_honest_basis")({
        "henry_hub_spot_usd_mmbtu": 2.95,
        "hh_source": "eia_v2_natural-gas/pri/fut",
        "basis_source": "synthetic_seed_basis",
        "delivered_source": "eia_gas_prices",
        "fetched_at": "not-a-timestamp",
    })
    assert out["age_hours"] is None
    assert out["as_of_age"] == "unknown"
    assert out["is_stale"] is None


# ── 6. The relabeller is actually wired into the routes ─────────────────────

def _route_node(fn_name):
    src = open(PLG_PATH, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return node, src
    return None, src


def _route_body(fn_name):
    node, src = _route_node(fn_name)
    return ast.get_source_segment(src, node) if node else None


def _calls_in(node):
    """Names of every function CALLED inside `node`. Deliberately AST, not a
    substring scan: the first draft of this test grepped the body for the
    string '_apply_honest_basis' and SURVIVED the mutation that deleted the
    call, because the explanatory comment two lines above it also contains
    the name. A guard that reads comments is not reading code."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


@pytest.mark.parametrize("route_fn", ["market_gas_pricing", "market_gas_to_grid"])
def test_route_calls_the_relabeller(route_fn):
    """A correct helper nobody calls is not a fix."""
    node, _ = _route_node(route_fn)
    assert node, f"route handler {route_fn} not found in routes/powered_land_gas.py"
    assert "_apply_honest_basis" in _calls_in(node), (
        f"{route_fn} serves a payload without CALLING _apply_honest_basis — "
        "the cached data_basis column goes out to the caller unchanged. "
        "(Mentioning the name in a comment does not count.)"
    )


def test_compute_payload_no_longer_hardcodes_live():
    """The original rule literally was `"live" if has_live_eia else ...`,
    which never looked at basis_source."""
    body = _route_body("_compute_market_payload")
    assert body, "_compute_market_payload not found"
    assert not re.search(r'"live"\s+if\s+has_live_eia', body), (
        "_compute_market_payload is back to deciding data_basis from layers "
        "1 and 3 only, ignoring the synthetic basis in layer 2."
    )


# ── 7. Sweep: no dict literal in the gas family pairs seed + live ───────────

def _string_values(dict_node):
    """(key, value) for every plain string-valued entry of a dict literal."""
    out = []
    for k, v in zip(dict_node.keys, dict_node.values):
        if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                and isinstance(v, ast.Constant) and isinstance(v.value, str)):
            out.append((k.value, v.value))
    return out


def test_no_gas_dict_literal_stamps_a_seed_constant_live():
    """Sibling sweep. Fails if any dict literal in the gas route family sets a
    *basis key to 'live' while a *source key in the SAME dict names a
    synthetic seed. This is the shape the phoenix payload had."""
    offenders = []
    for fname in GAS_ROUTE_FILES:
        path = os.path.join(ROOT, "routes", fname)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Dict):
                continue
            pairs = _string_values(node)
            live_keys = [k for k, v in pairs
                         if k.endswith("basis") and v.lower() == "live"]
            seed_keys = [
                (k, v) for k, v in pairs
                if k.endswith("source")
                and any(m in v.lower() for m in BANNED_MARKERS)
            ]
            if live_keys and seed_keys:
                offenders.append(
                    f"{fname}:{node.lineno} — {live_keys} == 'live' next to "
                    f"{seed_keys}"
                )
    assert not offenders, (
        "A synthetic/seed source is stamped 'live' in the same payload:\n  "
        + "\n  ".join(offenders)
    )


# ── MUTATIONS_APPLIED ───────────────────────────────────────────────────────
#
# Every guard above was watched fail. Each mutation was applied to the SHIPPED
# source (routes/powered_land_gas.py), the anchor asserted to appear exactly
# once first, the run observed, then reverted and the suite reconfirmed green.
# The exact mutations, the commands and the observed failures are recorded in
# the PR body for this change.
