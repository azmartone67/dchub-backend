#!/usr/bin/env python3
"""Surface 3 — DATA HONESTY, judged by effect rather than by self-report.

The platform's most expensive recurring bug is a status field that is
structurally incapable of saying red. ``/api/land-power/status`` returned a
hardcoded ``"healthy"`` while its sync log had been dead four months.
``get_backup_status`` reported 9/9 feeds healthy while the newest news record was
two months in the FUTURE. Both were "green" on every dashboard.

So this module never accepts a health field as evidence of health. It does two
things instead:

1. **Scans what callers actually receive for impossible dates.** A record dated
   in the future is wrong no matter what any status field claims, and it poisons
   every ``MAX(published_at)`` freshness check downstream — which is precisely how
   the news feed came to look fresh while being stale.

2. **Cross-examines each feed's self-report against its own published evidence.**
   The threshold is the feed's OWN declared ``refresh_interval``; nothing here
   invents a number. A feed claiming health while publishing no freshness
   timestamp at all is reported as a GAUGE — that is a missing instrument, not a
   proven failure, and the distinction is the whole discipline.
"""
from __future__ import annotations

import datetime
import email.utils
import re

from . import config as C
from .finding import (GAUGE, INFO, MAJOR, MINOR, PASS, RED, SEAT_ANON, Finding,
                      blind, stable_key)
from .http import MCPSession, Unreachable

NOW = datetime.datetime.now(datetime.timezone.utc)

# ★ Six hours of future tolerance, matching the existing ingest_runs ledger rule
# ("content date > 6h future" is already how /api/v1/ops/deadman flags a feed).
# Reusing the platform's own constant rather than inventing a competing one keeps
# this board and that board from disagreeing about the same row.
FUTURE_TOLERANCE_H = 6

# Tools sampled for the date scan. Chosen because they front dated, caller-visible
# content — the places where a poisoned date actually reaches an agent.
DATE_SCAN_TOOLS = [
    ("get_news", {"limit": 10}),
    ("get_changes", {}),
    ("hyperscaler_deals", {"limit": 10}),
]

_DATE_KEY_RE = re.compile(
    r"(date|_at$|_on$|updated|published|created|newest|oldest|last_run|timestamp)",
    re.I)


def parse_when(value) -> datetime.datetime | None:
    """Parse the date formats this platform actually emits.

    Three are in live use simultaneously: bare ISO dates (``2026-08-03``), ISO
    datetimes without a zone (``2026-09-21T11:00:00``) and RFC 2822
    (``Mon, 03 Aug 2026 10:17:55 GMT``). A naive-datetime is treated as UTC —
    assuming local time here would manufacture a spurious few hours of drift.
    """
    if not isinstance(value, str) or len(value) < 8 or len(value) > 40:
        return None
    txt = value.strip()
    try:
        dt = datetime.datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        try:
            dt = email.utils.parsedate_to_datetime(txt)
        except Exception:  # noqa: BLE001
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def walk_dates(node, path: str = "") -> list[tuple[str, str, datetime.datetime]]:
    """Yield (path, raw, parsed) for every date-ish leaf in a nested structure.

    Keyed on the FIELD NAME as well as parseability: a bare number-like string
    can accidentally parse, and flagging a version string as a future date would
    be exactly the wrong-evidence failure this tool exists to prevent.
    """
    out: list[tuple[str, str, datetime.datetime]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and _DATE_KEY_RE.search(str(k)):
                dt = parse_when(v)
                if dt:
                    out.append((p, v, dt))
            else:
                out.extend(walk_dates(v, p))
    elif isinstance(node, list):
        for i, v in enumerate(node[:50]):
            out.extend(walk_dates(v, f"{path}[{i}]"))
    return out


def probe(findings: list[Finding]) -> None:
    try:
        s = MCPSession(C.MCP_URL, api_key=C.SEATS.get("paid"),
                       timeout=C.MCP_TIMEOUT).open()
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("data", "session"), surface="data", seat=SEAT_ANON,
            title="Data-honesty probes unobserved — MCP session failed",
            why=str(e), basis=f"POST {C.MCP_URL} initialize"))
        return

    _scan_future_dates(s, findings)
    _cross_examine_feed_health(s, findings)


def _scan_future_dates(s: MCPSession, findings: list[Finding]) -> None:
    """Any caller-visible record dated in the future is wrong, full stop.

    This is generic on purpose. The specific instance it was written against — an
    events-page row whose ``published_at`` is the EVENT date, poisoning
    MAX(published_at) for the whole news feed — was diagnosed in July 2026 and is
    the kind of thing that returns under a different feed once the one-off fix
    ages out. A scan over what callers receive catches the class, not the case.
    """
    for tool, args in DATE_SCAN_TOOLS:
        key = stable_key("data", "future-dates", tool)
        try:
            env = s.call(tool, args)
        except Unreachable as e:
            findings.append(blind(key=key, surface="data", seat=SEAT_ANON,
                                  title=f"{tool} date scan unobserved", why=str(e),
                                  basis=f"MCP tools/call {tool}"))
            continue

        dates = walk_dates(env.get("structuredContent") or {})
        cutoff = NOW + datetime.timedelta(hours=FUTURE_TOLERANCE_H)
        future = [(p, raw, dt) for p, raw, dt in dates if dt > cutoff]

        if not dates:
            findings.append(Finding(
                key=key, surface="data", seat=SEAT_ANON,
                title=f"{tool} exposes no dated records to scan",
                verdict=GAUGE, severity=INFO, value=0,
                evidence="no date-shaped fields found in the response envelope",
                basis=f"MCP tools/call {tool}, recursive scan of structuredContent "
                      "for date-named fields",
                red_when="n/a — gauge; nothing dated was returned to examine"))
            continue

        if future:
            worst = max(future, key=lambda t: t[2])
            ahead = (worst[2] - NOW).total_seconds() / 86400.0
            findings.append(Finding(
                key=key, surface="data", seat=SEAT_ANON,
                title=f"{tool} serves {len(future)} FUTURE-dated record(s)",
                verdict=RED, severity=MAJOR, value=len(future),
                evidence=f"worst: {worst[0]} = {worst[1]!r}, {ahead:.0f} days in "
                         f"the future; {len(future)} of {len(dates)} dated fields "
                         f"are ahead of now+{FUTURE_TOLERANCE_H}h",
                basis=f"MCP tools/call {tool}; recursive scan of every date-named "
                      f"field in structuredContent, tolerance {FUTURE_TOLERANCE_H}h "
                      "(the same rule /api/v1/ops/deadman already applies)",
                red_when=f"any caller-visible date-named field is more than "
                         f"{FUTURE_TOLERANCE_H}h in the future",
                remedy="A future date does not just look odd — it becomes "
                       "MAX(published_at) and makes the whole feed report fresh "
                       "while it rots. Guard at the PRODUCER, and separate event "
                       "dates from publication dates."))
        else:
            newest = max(dates, key=lambda t: t[2])
            findings.append(Finding(
                key=key, surface="data", seat=SEAT_ANON,
                title=f"{tool} dates are all in the past",
                verdict=PASS, severity=INFO, value=len(dates),
                evidence=f"{len(dates)} dated field(s) scanned; newest "
                         f"{newest[0]}={newest[1]!r}",
                basis=f"MCP tools/call {tool}, recursive date scan",
                red_when=f"any date-named field more than {FUTURE_TOLERANCE_H}h "
                         "in the future"))


_INTERVAL_RE = re.compile(r"(\d+)\s*(minute|hour|day|week)", re.I)
_WORD_INTERVALS = {"hourly": 1, "daily": 24, "weekly": 168, "monthly": 720}


def parse_interval_hours(text: str) -> float | None:
    """Turn a feed's own ``refresh_interval`` prose into hours.

    Returns None when the feed does not state one — which is itself the finding,
    and must not be silently replaced with a default.
    """
    if not isinstance(text, str):
        return None
    m = _INTERVAL_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return n * {"minute": 1 / 60, "hour": 1, "day": 24, "week": 168}[unit]
    for word, hours in _WORD_INTERVALS.items():
        if word in text.lower():
            return float(hours)
    return None


def _cross_examine_feed_health(s: MCPSession, findings: list[Finding]) -> None:
    """Put every feed's ``health`` claim on the stand against its own evidence.

    The threshold is the feed's OWN ``refresh_interval``, doubled — the same
    "overdue at 2x cadence" rule the dead-man ledger uses. Nothing is invented,
    so a disagreement here is the feed contradicting itself, which is not
    arguable.

    Three outcomes, deliberately distinct:
      * claims healthy, evidence agrees                       -> PASS
      * claims healthy, its own newest record is stale/future -> RED
      * claims healthy, publishes NO freshness evidence       -> GAUGE
    The third is a missing instrument. Calling it a failure would be inventing a
    fault; ignoring it would repeat the "health = count > 0" blind spot that let
    substations, fiber_routes and permits die together invisibly.
    """
    key_root = "data::feed-health"
    try:
        env = s.call("get_backup_status", {})
    except Unreachable as e:
        findings.append(blind(
            key=stable_key(key_root), surface="data", seat=SEAT_ANON,
            title="Feed self-reports unobserved", why=str(e),
            basis="MCP tools/call get_backup_status"))
        return

    feeds = ((env.get("structuredContent") or {}).get("feeds") or {})
    if not isinstance(feeds, dict) or not feeds:
        findings.append(blind(
            key=stable_key(key_root), surface="data", seat=SEAT_ANON,
            title="Feed self-reports unreadable",
            why="get_backup_status returned no feeds map",
            basis="MCP get_backup_status -> structuredContent.feeds"))
        return

    contradicted: list[str] = []   # claims healthy, own evidence says otherwise
    uninstrumented: list[str] = []  # claims healthy, publishes no evidence at all
    quiet: list[str] = []          # content older than the refresh interval — AMBIGUOUS
    corroborated = 0

    for name, meta in sorted(feeds.items()):
        if not isinstance(meta, dict):
            continue
        health = str(meta.get("health", "")).lower()
        if health not in ("healthy", "ok", "green"):
            continue  # already saying something other than fine — not our lie to catch

        raw = meta.get("newest_record") or meta.get("last_updated")
        when = parse_when(raw) if raw else None
        interval_txt = meta.get("refresh_interval") or ""
        cadence_h = parse_interval_hours(interval_txt)

        if when is None:
            uninstrumented.append(f"{name} (interval={interval_txt!r})")
            continue

        age_h = (NOW - when).total_seconds() / 3600.0
        if age_h < -FUTURE_TOLERANCE_H:
            # Unambiguous: no honest feed has content from the future.
            contradicted.append(
                f"{name}: reports healthy but its newest record {raw!r} is "
                f"{abs(age_h)/24:.0f}d in the FUTURE")
        elif cadence_h and age_h > 2 * cadence_h:
            # ★ AMBIGUOUS ON PURPOSE — a gauge, never a RED.
            # `newest_record` is the newest CONTENT date, not the last RUN time.
            # An event-driven feed legitimately goes quiet: deals refreshes every
            # 5 minutes, but no deal is signed every 5 minutes, and "no new data"
            # is an EARNED state here, not a failure. A first draft compared
            # content age against the refresh interval and accused `deals` of
            # being stale at 23h against a 5-minute cadence — a false accusation
            # built by conflating "the loop ran" with "the world changed".
            # Distinguishing the two needs a last-RUN timestamp this tool does
            # not publish, so the honest output is the number, not a verdict.
            quiet.append(f"{name}: newest content {age_h:.0f}h old vs a declared "
                         f"{interval_txt!r} refresh")
            corroborated += 1
        else:
            corroborated += 1

    if contradicted:
        findings.append(Finding(
            key=stable_key(key_root, "contradicted"), surface="data", seat=SEAT_ANON,
            title=f"{len(contradicted)} feed(s) report healthy while serving "
                  f"future-dated content",
            verdict=RED, severity=MAJOR, value=len(contradicted),
            evidence="; ".join(contradicted[:6]),
            basis="MCP get_backup_status: each feed's health field cross-examined "
                  "against the newest record IT reports. Only the future-dated "
                  "case is treated as a contradiction — see the `quiet` gauge for "
                  "why content age alone cannot convict a feed",
            red_when="a feed publishes health=healthy while the newest record it "
                     "itself reports is dated in the future",
            remedy="This is the get_backup_status class: a status field that "
                   "cannot say red. Derive health from the record dates instead "
                   "of asserting it."))
    else:
        findings.append(Finding(
            key=stable_key(key_root, "contradicted"), surface="data", seat=SEAT_ANON,
            title="No feed reports healthy while serving future-dated content",
            verdict=PASS, severity=INFO, value=corroborated,
            evidence=f"{corroborated} healthy feed(s) publish a newest_record that "
                     f"is not in the future",
            basis="MCP get_backup_status, health vs the record dates it publishes",
            red_when="a feed reports healthy while its own newest record is "
                     "future-dated"))

    if quiet:
        findings.append(Finding(
            key=stable_key(key_root, "quiet"), surface="data", seat=SEAT_ANON,
            title=f"{len(quiet)} feed(s) have content older than their declared "
                  f"refresh interval",
            verdict=GAUGE, severity=INFO, value=len(quiet),
            evidence="; ".join(quiet[:8]),
            basis="MCP get_backup_status: newest CONTENT date vs the declared "
                  "refresh interval",
            red_when="n/a — GAUGE by design, and the distinction matters: this "
                     "compares content age against RUN cadence, and the two are "
                     "not the same claim. An event-driven feed that ran fine but "
                     "found nothing new is in an EARNED no-new-data state, not a "
                     "failure. Convicting one needs a last-RUN timestamp that "
                     "get_backup_status does not publish."))

    if uninstrumented:
        findings.append(Finding(
            key=stable_key(key_root, "uninstrumented"), surface="data",
            seat=SEAT_ANON,
            title=f"{len(uninstrumented)} feed(s) report healthy with no freshness "
                  f"evidence at all",
            verdict=GAUGE, severity=INFO, value=len(uninstrumented),
            evidence="; ".join(uninstrumented[:8]),
            basis="MCP get_backup_status: feeds whose health is 'healthy' but "
                  "which publish neither newest_record nor last_updated",
            red_when="n/a — GAUGE by design. A missing instrument is not a proven "
                     "fault, and asserting one would be inventing evidence. It is "
                     "tracked because these feeds share an ingest job: with no "
                     "freshness surface they can die together invisibly."))
