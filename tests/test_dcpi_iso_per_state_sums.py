"""GUARD — /api/v1/dcpi/iso/<ISO> must not multiply a per-state figure by the
market count.

The defect (live 2026-08-08, found by the analyst-grade audit): the ISO
aggregate ran `SUM(COALESCE(queue_capacity_mw, 0))` over MARKETS, but
queue_capacity_mw and gen_additions_12mo_mw are written per STATE — every
market in a state carries that state's whole figure. ERCOT published
total_queue_capacity_mw = 8,946,976 MW (8,947 GW, more than world installed
capacity) from 19 markets over 3 states, anonymously and keylessly.

These tests exercise the pure dedup rule directly, with no database, so the
arithmetic is checkable in CI. Mutation-tested: replacing
_iso_state_unique_totals' per-state key with a per-market key (i.e. restoring
the old behaviour) fails test_one_state_many_markets_is_not_multiplied.
"""
import routes.dcpi as dcpi


def _m(slug, iso, state, qcap, gadd=None):
    return {"market_slug": slug, "iso": iso, "state": state,
            "queue_capacity_mw": qcap, "gen_additions_12mo_mw": gadd}


def test_one_state_many_markets_is_not_multiplied():
    """THE regression. 19 Texas markets each carrying Texas's 470,893 MW
    active queue must total 470,893 — not 19x it."""
    tx = 470893.0
    rows = [_m(f"tx-market-{i}", "ERCOT", "TX", tx) for i in range(19)]
    out = dcpi._iso_state_unique_totals(rows)
    assert out["ERCOT"]["total_queue_capacity_mw"] == tx
    assert out["ERCOT"]["queue_states_counted"] == 1
    # And explicitly: nowhere near the multiplied figure that shipped.
    assert out["ERCOT"]["total_queue_capacity_mw"] < tx * 2


def test_multi_state_iso_counts_each_state_once():
    """ERCOT's real shape: 19 markets, 3 states. The total is the sum of the
    three STATE figures, each counted once regardless of market count."""
    rows = ([_m(f"tx-{i}", "ERCOT", "TX", 470893.0) for i in range(17)]
            + [_m("nm-1", "ERCOT", "NM", 12000.0)]
            + [_m("la-1", "ERCOT", "LA", 8000.0)])
    out = dcpi._iso_state_unique_totals(rows)
    assert out["ERCOT"]["total_queue_capacity_mw"] == 470893.0 + 12000.0 + 8000.0
    assert out["ERCOT"]["queue_states_counted"] == 3


def test_gen_additions_deduped_on_the_same_rule():
    """gen_additions_12mo_mw is the excess-side twin — same per-state writer,
    same defect, so it must dedup identically."""
    rows = [_m(f"tx-{i}", "ERCOT", "TX", 470893.0, 20546.5) for i in range(19)]
    out = dcpi._iso_state_unique_totals(rows)
    assert out["ERCOT"]["total_gen_additions_12mo_mw"] == 20546.5


def test_stateless_markets_stay_individually_counted():
    """International markets carry state IS NULL. They must NOT collapse into
    one bucket (that would hide all but one of them), so each is keyed by its
    own slug."""
    rows = [_m("tokyo", "UNKNOWN", None, 100.0),
            _m("singapore", "UNKNOWN", None, 250.0),
            _m("frankfurt", "UNKNOWN", None, 75.0)]
    out = dcpi._iso_state_unique_totals(rows)
    assert out["UNKNOWN"]["total_queue_capacity_mw"] == 425.0
    assert out["UNKNOWN"]["queue_states_counted"] == 3


def test_same_state_in_two_isos_is_counted_in_both():
    """Documented, deliberate: a state served by two ISOs contributes its whole
    figure to each, because the feed is metered per state and cannot be split.
    That is exactly why the totals are published as NOT additive across ISOs —
    STATE_SUM_BASIS_NOTE has to say so."""
    rows = [_m("dallas", "ERCOT", "TX", 470893.0),
            _m("amarillo", "SPP", "TX", 470893.0)]
    out = dcpi._iso_state_unique_totals(rows)
    assert out["ERCOT"]["total_queue_capacity_mw"] == 470893.0
    assert out["SPP"]["total_queue_capacity_mw"] == 470893.0
    assert "not additive across isos" in dcpi.STATE_SUM_BASIS_NOTE.lower()


def test_nulls_do_not_become_zero_states_or_crash():
    rows = [_m("a", "PJM", "VA", None), _m("b", "PJM", "VA", 500.0),
            _m("c", "PJM", "OH", None)]
    out = dcpi._iso_state_unique_totals(rows)
    # VA has a real figure on one row; OH has none and contributes 0.
    assert out["PJM"]["total_queue_capacity_mw"] == 500.0
    assert out["PJM"]["queue_states_counted"] == 2


def test_aggregate_sql_no_longer_sums_the_per_state_columns():
    """Belt-and-braces on the SQL itself: the two per-state columns must not
    reappear inside a SUM() in the ISO aggregate. Comment lines are stripped
    first so a mention in a comment cannot satisfy — or trip — this check."""
    import inspect
    src = inspect.getsource(dcpi._aggregate_iso_stats)
    code = "\n".join(ln for ln in src.splitlines()
                     if "--" not in ln and not ln.strip().startswith("#"))
    assert code.strip(), "comment-stripping ate the whole function"
    for col in dcpi._PER_STATE_COLS:
        assert f"SUM(COALESCE({col}" not in code, (
            f"{col} is a per-STATE column; SUM()ing it over markets multiplies "
            f"it by the market count")


def test_iso_mask_hides_the_paid_aggregates_but_keeps_counts():
    """The deep-dive route had no gate at all. The shared masker must null the
    numeric aggregates while leaving identity + counts + the basis string."""
    row = {"iso": "ERCOT", "iso_name": "ERCOT", "market_count": 19,
           "build_count": 1, "total_queue_capacity_mw": 470893.0,
           "avg_kwh_cents": 10.31, "queue_states_counted": 3,
           "state_totals_basis": "sum over distinct states, not over markets"}
    dcpi._mask_iso_rows_inplace([row])
    assert row["total_queue_capacity_mw"] is None
    assert row["avg_kwh_cents"] is None
    assert row["locked"] is True
    assert row["market_count"] == 19          # free breadth hook survives
    assert row["build_count"] == 1
    assert row["queue_states_counted"] == 3   # *_count survives
    assert row["state_totals_basis"]          # basis is never a paid field
