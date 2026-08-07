"""
routes/operator_spotlight.py — the daily OPERATOR lane (2026-08-06).

★ WHY THIS LANE DID NOT EXIST, AND WHY THAT WAS THE PROBLEM.

Every one of the media desk's six story types talks about DC Hub or about a
market: capability_spotlight, energy_narrative, dcpi_scoop, shipped_this_week,
market_anomaly, agent_demand. Not one of them is about an OPERATOR. So the feed
has been talking to the industry about ourselves, and the people we most want
reading it — the operators building and buying this infrastructure — have no
reason to look. A post that names an operator's actual build, sourced from data
we already hold, gives them one.

Operator directive 2026-07-02 stands and this lane is built inside it: POSITIVE
results and enhancements only. That directive retired `hyperscaler_drama` —
COMMENTARY and reactions to third-party news. This lane is the opposite of
commentary: it publishes what our own tracked records show an operator has
built, with no opinion attached. No downgrades, no rankings-against, no takes.
If the only honest thing to say about an operator is negative, we say nothing.

★ THE PREREQUISITE NOBODY WOULD HAVE SEEN COMING — READ BEFORE EDITING.

`discovered_facilities.provider` is free text, and 165 operator groups are
tracked under MORE THAN ONE spelling:

    Equinix        543 + Equinix, Inc.  223                   =  766 buildings
    DataBank       153 + DataBank, Ltd.  63 + Databank    6   =  222
    Flexential     120 + Flexential Corp. 40                  =  160
    CyrusOne       118 + CyrusOne Inc.    33 + Cyrusone   1   =  152
    Vantage Data Centers 134 + Vantage     4                  =  138

Publishing "Equinix operates 543 buildings" is not a rounding error — it is a
30% undercount, published AT Equinix, who can count their own estate. The single
fastest way to lose the audience this lane exists to win is to be visibly wrong
about them. So every number here is computed over the CANONICAL group, never
over one spelling, and `canonical_operator()` is a pure function with its own
tests.

★ NEVER FABRICATE A SPOTLIGHT. pick_spotlight() returns None when there is no
real material, and None must render as "no post today", never as a generic
operator profile. A daily cadence is a reason to have good material every day,
not a reason to invent it on a slow one — and the industry reads this feed.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Fleet filter — the canonical one. COALESCE because is_duplicate is nullable
# and `is_duplicate = 0` silently drops every NULL row.
FLEET = "COALESCE(is_duplicate,0)=0"

# Corporate suffixes and decorations that do not distinguish two companies.
# `data centers?` is here because "Vantage Data Centers" and "Vantage" are one
# operator; it is NOT here for "Digital Realty" vs "Digital Bridge", which are
# genuinely different and must stay apart.
_NOISE = re.compile(
    r"[,\.]|\b("
    r"inc|llc|l\.l\.c|ltd|limited|corp|corporation|company|co|plc|sa|nv|ag|"
    r"gmbh|bv|pte|pty|holdings?|group|"
    # ★ "STACK Infrastructure" and "Stack" split into two keys and published
    # TWO posts about one company with contradicting fleet counts — 64
    # facilities/7,140 MW one day, 36 facilities/797 MW the next. Generic
    # industry descriptors are decoration, not identity.
    r"infrastructure|technologies|technology|solutions|services|"
    r"data\s?centers?|datacenters?|dc"
    r")\b",
    re.IGNORECASE,
)

# Distinct legal entities whose normalized forms collide, or trading names the
# normalizer cannot know. Only add an entry you have CHECKED — a wrong merge
# publishes one operator's estate under another's name, which is worse than the
# split it fixes.
_FORCE_DISTINCT = {
    # normalized -> keep as-is; documents that the collision was considered.
    "digital realty", "digital bridge", "digital edge",
}

# name a human would recognise, keyed by normalized form. Without this the
# display name is whichever spelling happened to be most common.
_DISPLAY = {
    "equinix": "Equinix",
    "databank": "DataBank",
    "cyrusone": "CyrusOne",
    "flexential": "Flexential",
    "vantage": "Vantage Data Centers",
    "amazon web services": "Amazon Web Services",
    "digital realty": "Digital Realty",
    "stack": "STACK Infrastructure",
        "aligned": "Aligned Data Centers",
    "qts": "QTS Realty Trust",
    "nlighten": "nLighten",
}

# Never spotlight these — not operators, or not a company at all.
_NOT_AN_OPERATOR = {
    "", "unknown", "n/a", "na", "none", "null", "tbd", "undisclosed",
    "confidential", "various", "multiple", "private", "colocation",
}

# ★ BOTH SOURCE COLUMNS CARRY MIS-PARSED HEADLINES, and the first live run of
#   this module duly produced:
#
#     "50 MW: State Pauses Projects Over added a new site in NY…"
#
#   `discovered_facilities.provider` and `deals.buyer` are both populated by an
#   extraction pipeline that sometimes lifts a fragment of the article headline
#   instead of the company. The top five deals by value are buyers named
#   "Should You", "Billion Debt", "billion deal", "that would" and "Rocket Lab"
#   (only the last is real). Publishing any of those AT the industry is worse
#   than publishing nothing.
#
#   Fleet size cannot be the filter: most single-building providers are
#   legitimate international operators — Talex S.A., Wingu Africa, Teledata
#   Mozambique — and excluding them would quietly make this a US-megacap lane.
#   So the test is NAME SHAPE plus, for deals, CROSS-VALIDATION against our own
#   provider fleet: a company we have never tracked a single building for is not
#   an operator we should be writing about.
_HEADLINE_WORDS = re.compile(
    r"\b("
    r"should|would|could|will|says?|said|announces?|announced|pauses?|paused|"
    r"wraps?|buys?|bought|sells?|sold|plans?|planned|eyes?|eyeing|weighs?|"
    r"reportedly|amid|after|before|over|into|that|this|these|those|"
    r"billion|million|trillion|deal|wave|spending|debt|report|update"
    r")\b",
    re.IGNORECASE,
)


def looks_like_company(name: str | None) -> bool:
    """Reject sentence fragments lifted from article headlines.

    Deliberately conservative in the SAFE direction: a real operator wrongly
    skipped costs one day's post; a fragment published costs the audience.
    """
    if not name or not isinstance(name, str):
        return False
    s = name.strip()
    if len(s) < 2 or len(s) > 60:
        return False
    if _HEADLINE_WORDS.search(s):
        return False
    # A company name is not a sentence: no terminal punctuation, no verbs
    # hiding behind an apostrophe, not mostly lowercase function words.
    if s.endswith((":", ";", "?", "!")):
        return False
    words = [w for w in re.split(r"\s+", s) if w]
    if len(words) > 6:
        return False
    # ★ There is deliberately NO "must start with a capital" rule here. The
    # first version had one and rejected two real operators its own anti-wolf
    # test caught: "nLighten" (lowercase first letter, real company, 33
    # buildings) and the all-lowercase "amazon web services" spelling that
    # exists in the data alongside the title-case one. _HEADLINE_WORDS already
    # does the real work of rejecting fragments — "state pauses projects over"
    # is caught by `pauses` and `over`, not by its capitalisation — so a
    # casing rule adds no safety and quietly excludes companies that style
    # their own name in lowercase.
    if not any(ch.isalpha() for ch in s):
        return False
    return True


def canonical_operator(raw: str | None) -> tuple[str | None, str | None]:
    """(canonical_key, display_name) for a raw provider string.

    Pure — no I/O. Returns (None, None) for anything that is not an operator
    name, so a caller can never spotlight the "Unknown" bucket, which is the
    single largest provider value in the table (1,775 buildings).
    """
    if not raw or not isinstance(raw, str):
        return None, None
    s = raw.strip()
    if s.lower() in _NOT_AN_OPERATOR:
        return None, None
    if not looks_like_company(s):
        return None, None
    key = re.sub(r"\s+", " ", _NOISE.sub(" ", s.lower())).strip()
    key = key.strip(" -–—&/")
    if not key or key in _NOT_AN_OPERATOR:
        return None, None
    display = _DISPLAY.get(key)
    if not display:
        # Keep the operator's own capitalisation; only collapse whitespace.
        display = re.sub(r"\s+", " ", s).strip().rstrip(",.")
    return key, display


# ── material selection ───────────────────────────────────────────────────────
# Each angle is a (sql, builder) pair. Every one returns a headline that LEADS
# with a number adjacent to a unit media_editorial._HAS_METRIC accepts (MW,
# facilities, markets, deals) — a prose-led headline is silently dropped at
# rank_data_events and again at publish. Verified against the real regex in
# tests/test_operator_spotlight.py, not by eye.

_COOLDOWN_DAYS = 30   # do not spotlight the same operator twice in a month

# ★ THE CREDIBILITY BAR, and why it is not tunable downward without thought.
# The provider column also carries SITE names — "Frontier Oxnard", "Frontier
# Rochester", "Pipe Networks Pipe DC", "Iron Mountain Data Centers Va" — each
# attached to 4-5 buildings. Calling one of those "the operator that added the
# most sites" is a visibly wrong statement about a real company. Requiring a
# real portfolio behind the name filters them out without needing a hand-kept
# roster of every operator on earth. Nlighten (33) and STACK (64) clear it;
# every site-shaped name measured on 2026-08-06 did not.
_MIN_FLEET = 8        # a name with fewer tracked buildings is likely a SITE
_MIN_ADDED = 3        # and it must have genuinely moved this month

# A single transaction of 20,000 MW is a parse error, not a deal — the largest
# real data-center transactions are low single-digit GW. Publishing one would be
# an obvious falsehood to every reader in the industry.
_MAX_DEAL_MW = 5000


def _rows(cur, sql, args=None):
    try:
        cur.execute(sql, args or ())
        return cur.fetchall() or []
    except Exception as e:
        logger.warning("[operator-spotlight] query failed: %s", e)
        return []


def _fleet_totals(cur, keys: list[str]) -> tuple[int, float]:
    """Canonical building count and MW for an operator, summed over EVERY
    spelling of its name. This is the whole reason canonical_operator exists."""
    if not keys:
        return 0, 0.0
    rows = _rows(cur, f"""
        SELECT COUNT(*), COALESCE(SUM(COALESCE(power_mw,0)),0)
          FROM discovered_facilities
         WHERE {FLEET} AND provider = ANY(%s)
    """, (keys,))
    if not rows:
        return 0, 0.0
    return int(rows[0][0] or 0), float(rows[0][1] or 0)


def _spellings(cur, canon_key: str) -> list[str]:
    """Every raw provider spelling that canonicalises to this key."""
    rows = _rows(cur, f"""
        SELECT DISTINCT provider FROM discovered_facilities
         WHERE {FLEET} AND provider IS NOT NULL AND provider <> ''
    """)
    return [r[0] for r in rows if canonical_operator(r[0])[0] == canon_key]


def _recent_builds(cur, days: int = 30) -> list[dict]:
    """Newly tracked buildings with a named operator, real capacity and a
    market. discovered_at is TEXT in this table — cast, never compare raw."""
    rows = _rows(cur, f"""
        SELECT provider, name, market, city, state, country,
               power_mw, status, discovered_at
          FROM discovered_facilities
         WHERE {FLEET}
           AND provider IS NOT NULL AND provider <> ''
           AND discovered_at::timestamptz > now() - (%s || ' days')::interval
         ORDER BY COALESCE(power_mw,0) DESC
         LIMIT 500
    """, (days,))
    out = []
    for p, name, market, city, state, country, mw, status, seen in rows:
        key, disp = canonical_operator(p)
        if not key:
            continue
        out.append({"key": key, "operator": disp, "name": name,
                    "market": market or city or state or country,
                    "mw": float(mw or 0), "status": status, "seen": seen})
    return out


def _tracked_operator_keys(cur) -> set:
    """Canonical keys of every operator we track at least one building for.
    A deal buyer absent from this set is almost always a mis-parsed headline."""
    rows = _rows(cur, f"""
        SELECT DISTINCT provider FROM discovered_facilities
         WHERE {FLEET} AND provider IS NOT NULL AND provider <> ''
    """)
    return {k for k in (canonical_operator(r[0])[0] for r in rows) if k}


# ★ `deals` HOLDS 2,868 QUARANTINED ROWS (4,711 raw vs 1,843 publishable), and
# an unguarded read republishes them. That is not theoretical here: the junk
# buyers this module tripped over on its first live run — "Should You",
# "Billion Debt", "billion deal", "that would" — are exactly those rows. The
# repo's deals guard (tests/test_deals_guard.py) caught the unguarded query
# before it shipped and is the reason this import exists.
# DEALS_OK is LEFT(data_flag,11) <> 'quarantine_' and carries NO literal % on
# purpose: callers interpolate it into queries that also pass params, and a
# literal % makes psycopg2 attempt substitution and 500 the route.
from util.deals import DEALS_OK  # noqa: E402


def _recent_deals(cur, days: int = 60) -> list[dict]:
    rows = _rows(cur, """
        SELECT buyer, seller, value, mw, market, region,
               COALESCE(deal_date, date::date) AS d
          FROM deals
         WHERE """ + DEALS_OK + """
           AND COALESCE(deal_date, date::date) > (now() - (%s || ' days')::interval)::date
           AND buyer IS NOT NULL AND buyer <> ''
           AND mw IS NOT NULL AND mw > 0 AND mw <= %s
         ORDER BY mw DESC
         LIMIT 120
    """, (days, _MAX_DEAL_MW))
    tracked = _tracked_operator_keys(cur)
    out = []
    for buyer, seller, value, mw, market, region, d in rows:
        key, disp = canonical_operator(buyer)
        if not key or key not in tracked:
            continue
        out.append({"key": key, "operator": disp, "seller": seller,
                    "value": float(value or 0), "mw": float(mw or 0),
                    "market": market or region, "date": d})
    return out


# deals.value is RAW DOLLARS, not millions. The first live run rendered a $50B
# Meta transaction as "$50,000M" because the formatter assumed millions.
def _money(v: float) -> str:
    v = float(v or 0)
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:,.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:,.0f}M"
    return f"${v:,.0f}"


# "in Global" / "in Unknown" read like a bug to any operator. Say nothing.
_VAGUE_PLACE = {"global", "unknown", "n/a", "na", "none", "various", "worldwide", ""}


def _where(market) -> str:
    m = (market or "").strip()
    return "" if m.lower() in _VAGUE_PLACE else f" in {m}"


# ★ UNKNOWN IS NOT ZERO. Most tracked buildings carry no power_mw, so a fleet
# sum of 0 means "we do not publish capacity for this operator", not "this
# operator has no capacity". "33 facilities and 0 MW" is a false statement about
# a real company, printed at that company.
def _mw_clause(mw) -> str:
    try:
        v = float(mw or 0)
    except (TypeError, ValueError):
        return ""
    return f" and {v:,.0f} MW" if v > 0 else ""


def _headline(angle: str, d: dict) -> str:
    """Number-led, positive, factual. No adjective may sit between the number
    and its unit — "1,920 tracked MW" fails the gate that "1,920 MW" passes."""
    op = d["operator"]
    if angle == "new_build":
        where = _where(d.get("market"))
        return (f"{d['mw']:,.0f} MW: {op} added a new site{where} to DC Hub's "
                f"tracked fleet — now {d['fleet_n']:,} facilities and "
                f"{d['fleet_mw']:,.0f} MW under the {op} name.")
    if angle == "portfolio_growth":
        return (f"{d['added']:,} facilities: {op} added the most sites to "
                f"DC Hub's tracked fleet in the last 30 days, reaching "
                f"{d['fleet_n']:,} facilities{_mw_clause(d['fleet_mw'])}.")
    if angle == "deal":
        # ★ NO MONEY FIGURE, EVER, FROM deals.value — the column is MIXED-UNIT:
        # 2,639 rows are on a millions scale and 36 on a dollars scale, so the
        # same formatter renders a $50B Meta transaction as either "$50,000" or
        # "$50.0B" depending on which row it lands on. MW is unambiguous.
        where = _where(d.get("market"))
        return (f"{d['mw']:,.0f} MW: {op} closed a data-center transaction"
                f"{where}. DC Hub tracks {d['fleet_n']:,} {op} "
                f"facilities{_mw_clause(d['fleet_mw'])}.")
    return ""


def pick_spotlight(conn, exclude_keys: set | None = None) -> dict | None:
    """Today's operator, or None when there is nothing true to say.

    None is a legitimate, expected answer. The caller must render it as "no
    operator post today" — never as a generic profile. See the module docstring.
    """
    exclude = set(exclude_keys or ())
    try:
        cur = conn.cursor()
    except Exception:
        return None
    try:
        builds = [b for b in _recent_builds(cur) if b["key"] not in exclude]
        deals = [d for d in _recent_deals(cur) if d["key"] not in exclude]

        # ★ ANGLE ORDER IS SET BY WHAT THE DATA ACTUALLY HOLDS, not by what
        # reads best in a design doc. Measured 2026-08-06: of 1,369 buildings
        # newly tracked in 30 days, 1,207 carry a valid operator and ZERO carry
        # power_mw. So a "largest new build by MW" angle can never fire, and
        # writing one would have produced a lane that silently never posts —
        # the failure mode this repo already has a name for.
        #
        # Angle 1 — portfolio growth. Abundant and always number-led:
        # Nlighten +33, STACK Infrastructure +27, Equinix +7 in the last 30d.
        if builds:
            by_op: dict = {}
            for b in builds:
                e = by_op.setdefault(b["key"], {"operator": b["operator"],
                                                "key": b["key"], "added": 0,
                                                "sites": []})
                e["added"] += 1
                if b.get("market"):
                    e["sites"].append(b["market"])
            best = max(by_op.values(), key=lambda e: e["added"])
            if best["added"] >= _MIN_ADDED:
                sp = _spellings(cur, best["key"])
                n, mw = _fleet_totals(cur, sp)
                if n >= _MIN_FLEET:
                    best.update({"fleet_n": n, "fleet_mw": mw, "spellings": sp})
                    return {"angle": "portfolio_growth",
                            "headline": _headline("portfolio_growth", best), **best}

        # Angle 2 — a closed transaction, sized in MW. 222 deals in 90d carry
        # MW. Money is deliberately absent; see _headline().
        if deals:
            top = max(deals, key=lambda d: d["mw"])
            sp = _spellings(cur, top["key"])
            n, mw = _fleet_totals(cur, sp)
            if n >= _MIN_FLEET:
                top.update({"fleet_n": n, "fleet_mw": mw, "spellings": sp})
                return {"angle": "deal", "headline": _headline("deal", top), **top}

        return None
    finally:
        try:
            cur.close()
        except Exception:
            pass
