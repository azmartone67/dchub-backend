"""
routes/brain_proposal_dedup.py — STATEFUL proposal dedup (2026-07-16).

THE PROBLEM (verified live): the enhancement-proposal / self-agenda pipelines
draft the SAME recommendation over and over. 33 of 37 stored
brain_enhancement_proposals were the SAME condition ("[data_coverage] N
verified vs M tracked facilities"), 31 of them still open/ungraded; the
self-agenda held 14 more of it. Each duplicate burned a full draft +
adversarial-refutation cycle (~48s of model time) because the drafting loops
compared EXACT titles — and the titles embed live counts that drift every
cycle ("4923 verified" -> "4924 verified"), so nothing ever matched.

THE FIX: before drafting, compute a stable FINGERPRINT of the underlying
CONDITION (area + the number-stripped signal, falling back to the normalized
question), then:
  · an OPEN same-fingerprint proposal exists            -> SKIP drafting;
  · the latest same-fingerprint proposal is younger than
    BRAIN_PROPOSAL_REDRAFT_DAYS (default 7)             -> SKIP (cooldown);
  · older than the cooldown -> re-draft ONLY when the condition's measured
    numbers moved materially (>20% on any figure, or the figure set changed);
    when no figures are accessible the cooldown alone governs.

This module holds the PURE decision helpers (no DB, no Flask) so both drafting
pipelines — routes/brain_enhancer.py (brain_enhancement_proposals) and
routes/brain_self_director.py (brain_self_agenda) — share ONE fingerprint and
ONE re-fire rule (the brain_findings_writer "single canonical writer" pattern).
Each pipeline keeps its own table lookup and stamps the fingerprint on the
rows it stores.

Kill switch: BRAIN_PROPOSAL_DEDUP=0 disables everything (default ON).
Every caller treats a missing/None prior as "draft" — the dedup FAILS OPEN so
a DB hiccup can only ever cost a duplicate draft, never block proposing.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

# Volatile live counts inside a signal/title ("4,924 verified vs 21,959
# tracked") — stripped for fingerprinting, extracted for the material-change
# test. Mirrors brain_self_director._DIGITS_RE.
_DIGITS_RE = re.compile(r"\d[\d,\.]*")

# A re-draft after the cooldown needs a >20% move on some measured figure (or
# a change in which figures the condition carries) to be worth another cycle.
MATERIAL_CHANGE_PCT = 0.20


def dedup_enabled() -> bool:
    """Kill switch — BRAIN_PROPOSAL_DEDUP=0 disables the dedup (default ON)."""
    return os.environ.get("BRAIN_PROPOSAL_DEDUP", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def redraft_days() -> int:
    """Cooldown before a same-fingerprint condition may be re-drafted.
    Env BRAIN_PROPOSAL_REDRAFT_DAYS, default 7."""
    try:
        return max(0, int(os.environ.get("BRAIN_PROPOSAL_REDRAFT_DAYS", "7")))
    except Exception:
        return 7


def lookback_days() -> int:
    """How far back a pipeline should load prior fingerprints when deciding.
    Wide enough that the cooldown + material-change rule always sees the latest
    prior row; bounded so the lookup stays one cheap indexed query."""
    return max(30, 2 * redraft_days())


def norm_condition(text) -> str:
    """Normalize condition text for fingerprinting: lowercase, collapse every
    number run to '#' (live counts drift every cycle), collapse whitespace."""
    s = _DIGITS_RE.sub("#", str(text or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def condition_fingerprint(area, signal, question="") -> str:
    """Stable fingerprint of the UNDERLYING CONDITION a proposal addresses.

    Prefers area + the number-stripped SIGNAL — for finding-sourced
    opportunities the signal embeds the detector/finding_type + entity (e.g.
    'brain_findings: data_freshness_sla_breach @ table:usgs_water_stress
    (seen x2847)'), which is exactly the condition identity. Falls back to the
    first ~200 normalized chars of the QUESTION when there is no signal.
    Number-stripping makes '4924 verified vs 21959 tracked' and '4923 verified
    vs 21958 tracked' hash identically."""
    base = norm_condition(signal)
    if not base:
        base = norm_condition(str(question or "")[:200])
    key = f"{str(area or '').strip().lower()}|{base}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:32]


def signal_numbers(text) -> list:
    """The measured figures a condition text carries, in order — the values the
    material-change test compares across drafts. [] when none parse."""
    out = []
    for m in _DIGITS_RE.findall(str(text or "")):
        try:
            out.append(float(m.replace(",", "").rstrip(".")))
        except Exception:
            continue
    return out


def materially_changed(prev_text, cur_text,
                       pct: float = MATERIAL_CHANGE_PCT) -> Optional[bool]:
    """Did the condition's measured value move MATERIALLY between two drafts?

      True  -> some figure moved > pct (relative to the prior figure), or the
               set of figures changed shape (a state change).
      False -> figures present on both sides and all within pct.
      None  -> figures not accessible on one/both sides — the caller lets the
               cooldown alone govern (per the re-fire rule)."""
    prev = signal_numbers(prev_text)
    cur = signal_numbers(cur_text)
    if not prev or not cur:
        return None
    if len(prev) != len(cur):
        return True
    for a, b in zip(prev, cur):
        if a == b:
            continue
        base = abs(a)
        if base <= 0:
            return True  # 0 -> nonzero is a state change
        if abs(b - a) / base > pct:
            return True
    return False


def should_skip_redraft(prior: Optional[dict], current_text) -> tuple:
    """THE re-fire decision, shared by both drafting pipelines.

    `prior` is the pipeline's most recent same-fingerprint row as
    {"age_days": float, "open": bool, "text": str} — or None when there is no
    prior (or the lookup failed: fail-OPEN).

    Returns (skip, reason):
      · no prior                          -> (False, None)  draft
      · prior younger than the cooldown   -> (True, "open_duplicate") when the
        prior is still open/undecided, else (True, "cooldown")
      · prior older than the cooldown     -> re-draft ONLY on a material move
        of the measured figures; unchanged -> (True, "unchanged"); figures not
        accessible -> (False, None) — cooldown alone governs."""
    if not isinstance(prior, dict):
        return False, None
    try:
        age = float(prior.get("age_days") or 0.0)
    except Exception:
        age = 0.0
    if age < redraft_days():
        return True, ("open_duplicate" if prior.get("open") else "cooldown")
    changed = materially_changed(prior.get("text") or "", current_text or "")
    if changed is False:
        return True, "unchanged"
    return False, None
