"""tax_incentives.py — the one read path for state data-center tax incentives.

★ WHY (2026-09-21). Two stores answer "what does state X offer a data center":

  * the REGISTRY — DEFAULT_INCENTIVES in tax_incentives_routes.py, plus admin
    overrides. The source of truth since #4757: reviewed in git, and where a
    statutory change gets recorded. REST /api/v1/tax-incentives serves it, and
    so does the public MCP tool (dchub-mcp-server proxies that REST route).
  * the SNAPSHOT — Postgres `tax_incentives_neon`, 50 rows. Every row has
    last_updated 2026-03-17 and source_url NULL, and its only writer
    (scripts/tax_20_states.py) is insert-only. Nine backend readers queried it
    directly.

By 2026-09-21 nine states' programs had changed — AZ IL NE OH paused to new
applicants, NJ repealed, MN NC WA partially repealed, OK restricted — and the
snapshot recorded none of it. It still said OH "100% sales tax exempt" and MN
"100% sales tax exempt on electricity" after that exemption was repealed, so
the same question got opposite answers depending on which surface asked.

★ PRECEDENCE. A registry row that has been checked against a primary source
(it carries `last_verified` or a `status`) SUPERSEDES the snapshot for that
state. Every field the registry asserts wins; its narrative, term, benefit and
source fields replace the snapshot's outright, because for exactly these
states the snapshot's are the stale ones. A program flag the registry is
silent on (None: never recorded) falls back to the snapshot's value, and a
status that closes the program to new applicants turns every flag False —
these readers price a NEW site, and a paused exemption cannot be had by one.
All other states read the snapshot unchanged, labelled `provenance:
"snapshot"` with `last_verified: None`.

★ Why not repoint every state at the registry. For the unverified states the
registry is SPARSER than the snapshot, not better sourced: the snapshot marks
a property-tax abatement in 35 states, the registry records one in 12, and the
other 23 are absent, not False. Reading absence as "no" would move site-value
premiums and simulator offsets for ~40 states on no evidence. Verifying a
state in the registry is what moves it over, and every reader follows at once.

★ Why not UPDATE the snapshot rows. That makes a second hand-maintained copy
of the registry — the drift that caused this — in a table with no status or
verification columns. The brain RAG corpus over the table is insert-only
(last_updated is TEXT, so it has no fresh_col), so an in-place UPDATE would
not reach its embedded text either; snapshot_serve_gate() covers that surface.
"""
import re

from util.db_honesty import unpoison

SNAPSHOT_TABLE = "tax_incentives_neon"

# Statuses under which the program still takes new applicants, in whatever
# (possibly narrowed) form the registry's flags describe. ★ Anything else —
# including a status value nobody has classified yet — is treated as CLOSED:
# under-claiming a benefit is the recoverable direction. The registry's
# vocabulary is pinned by tests/test_tax_incentive_read_path.py.
STILL_OPEN = frozenset({"active", "partially_repealed", "restricted"})

# registry key -> snapshot column, for the four program flags.
_FLAGS = (
    ("sales_tax", "sales_tax_exempt"),
    ("property_tax", "property_tax_abatement"),
    ("electricity_tax", "energy_incentive"),
    ("has_incentive", "data_center_specific"),
)

_SNAPSHOT_COLS = (
    "state_abbr", "state_name", "sales_tax_exempt", "property_tax_abatement",
    "energy_incentive", "data_center_specific", "incentive_details",
    "qualifying_investment", "qualifying_jobs", "duration_years", "max_benefit",
    "source_url", "last_updated",
)

# "Up to 20 years" -> 20, "10–20 years" -> 10 (the floor of a stated range),
# "20 yr + permanent" -> 20. "Varies", "Through 2050/2065" -> None.
_TERM_RE = re.compile(r"(\d{1,2})\s*(?:[–-]\s*\d{1,2}\s*)?(?:\+\s*)?(?:yr|year)", re.I)

_ABBR_RE = re.compile(r"^[A-Z]{2}$")


def registry():
    """abbr -> registry row, exactly as REST serves it. Raises if the registry
    cannot be loaded: falling back to the snapshot alone would silently
    republish the stale programs this module exists to stop."""
    from tax_incentives_routes import served_incentives
    return served_incentives()


def is_verified(row) -> bool:
    return bool(row and (row.get("last_verified") or row.get("status")))


def is_open(row) -> bool:
    status = (row or {}).get("status")
    return not status or status in STILL_OPEN


def superseded_states() -> list:
    """States whose snapshot row the registry supersedes, sorted."""
    return sorted(a for a, r in registry().items() if is_verified(r))


def snapshot_serve_gate() -> str:
    """SQL predicate over the snapshot table: rows the RAG may still serve.

    The corpus embedded each row's text at insert time and never re-embeds,
    so a superseded state's stale text can only be stopped at serve time."""
    states = superseded_states()
    bad = [a for a in states if not _ABBR_RE.match(a)]
    if bad:
        raise ValueError(f"unexpected state keys in the registry: {bad}")
    if not states:
        return "TRUE"
    return "state_abbr NOT IN (%s)" % ", ".join("'%s'" % a for a in states)


def term_years(text):
    m = _TERM_RE.search(text or "")
    return int(m.group(1)) if m else None


def _snapshot(cur, abbr=None) -> dict:
    sql = "SELECT %s FROM %s" % (", ".join(_SNAPSHOT_COLS), SNAPSHOT_TABLE)
    try:
        if abbr:
            cur.execute(sql + " WHERE state_abbr = %s", (abbr,))
        else:
            cur.execute(sql)
        rows = cur.fetchall()
    except Exception:
        unpoison(cur)
        raise
    out = {}
    for r in rows:
        # Positional for tuple rows: this function wrote the SELECT list, so
        # its order is known without trusting the driver's cur.description.
        rec = dict(r) if isinstance(r, dict) else dict(zip(_SNAPSHOT_COLS, r))
        out[(rec.get("state_abbr") or "").upper()] = rec
    return out


def _from_snapshot(snap) -> dict:
    rec = {c: snap.get(c) for c in _SNAPSHOT_COLS if c != "last_updated"}
    rec.update({
        "status": None,
        "status_as_of": None,
        "status_note": None,
        "open_to_new_projects": None,
        "last_verified": None,
        "provenance": "snapshot",
        "as_of": str(snap.get("last_updated") or "")[:10] or None,
    })
    return rec


def _from_registry(reg, snap=None) -> dict:
    open_ = is_open(reg)
    rec = {
        "state_abbr": reg.get("abbr"),
        "state_name": reg.get("name") or (snap or {}).get("state_name"),
    }
    for rkey, col in _FLAGS:
        val = reg.get(rkey)
        if val is None and snap is not None:
            val = snap.get(col)
        rec[col] = (None if val is None else bool(val)) if open_ else False
    rec.update({
        "incentive_details": reg.get("summary"),
        "qualifying_investment": reg.get("min_investment"),
        "qualifying_jobs": reg.get("jobs_required"),
        "duration_years": term_years(reg.get("duration")),
        "max_benefit": None,
        "source_url": reg.get("source_url"),
        "status": reg.get("status"),
        "status_as_of": reg.get("status_as_of"),
        "status_note": reg.get("status_note"),
        "open_to_new_projects": open_,
        "last_verified": reg.get("last_verified"),
        "provenance": "registry",
        "as_of": reg.get("last_verified") or reg.get("status_as_of"),
    })
    return rec


def state_incentive(cur, abbr):
    """One state's incentive record, in the snapshot's column names plus
    status / open_to_new_projects / last_verified / provenance / as_of.

    None when no store knows the state. A superseded state never touches the
    database unless the registry is silent on one of its flags. A snapshot read
    error is re-raised after rolling the connection back, so the caller's
    other reads on it still work."""
    abbr = (abbr or "").strip().upper()
    if not abbr:
        return None
    reg = registry().get(abbr)
    if reg is not None and is_verified(reg):
        silent = any(reg.get(k) is None for k, _ in _FLAGS)
        snap = _snapshot(cur, abbr).get(abbr) if silent and is_open(reg) else None
        return _from_registry(reg, snap)
    snap = _snapshot(cur, abbr).get(abbr)
    if snap is not None:
        return _from_snapshot(snap)
    return _from_registry(reg) if reg is not None else None


def all_state_incentives(cur) -> list:
    """Every state either store knows, sorted by abbreviation. One read."""
    snaps = _snapshot(cur)
    reg = registry()
    out = []
    for abbr in sorted(set(snaps) | set(reg)):
        r, s = reg.get(abbr), snaps.get(abbr)
        if r is not None and (is_verified(r) or s is None):
            out.append(_from_registry(r, s if is_verified(r) else None))
        else:
            out.append(_from_snapshot(s))
    return out
