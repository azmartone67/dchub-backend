"""brain_null_signal_detector.py — signals that CANNOT fire (2026-09-08).

Every one of the eight defects found in the 2026-09-07/08 brain sweep was the
same shape: **a reader keyed on something the writer never emitted, and it
failed as SILENCE rather than as an error.** A gate that rejects 45 of 45. A
heartbeat key that is simply absent. A counter pinned at a confident 0. A
workflow whose twelve most recent runs are all `skipped`, rendered neutral
grey. A NULL column feeding a self-tuning threshold.

The brain runs ~78 master shells and they all point OUTWARD at the product —
ISO metrics, page 5xx, media quality. **None of them ask whether the brain's
own signals are capable of firing at all.** That is the gap this closes.

CHECK 1 — DEAD TABLE PROBES (implemented here)
    `if to_regclass('public.X')` is the house idiom for an optional feature.
    When X does not exist the branch is silently skipped: no key, no error,
    no log. That is exactly how the layer-5 heartbeat block went blind — it
    probed `brain_proposed_code` while the real table is
    `brain_proposed_code_fixes`, so the vitals said nothing about the brain's
    own code-proposal arm and nothing anywhere said why.
    First live run: **8 of 65 probes were dead.**

Deliberately ONE check, not four. The 2026-09-07 sweep's own lesson is that
this system has more half-built machinery than it can keep honest; a second
check earns its place after this one has caught something in the wild. The
spec for the others (can't-fail signatures, computed-but-unread, overridden
output) is in the handoff.

★★★ THE FLOOR IS THE POINT. A detector the brain writes about itself has the
same failure mode as everything it hunts: if it silently scans nothing it
reports a clean bill of health forever, and it becomes defect number nine.
So every run self-tests THREE ways before its findings are allowed to mean
anything (`_self_test`):

  · scan floor  — the source scan must find at least _MIN_PROBES call sites.
                  A broken regex finds zero and would otherwise report
                  "no dead probes" — the most dangerous possible green.
  · negative    — a name that cannot exist must be reported dead. Catches a
                  comparison that always passes.
  · positive    — a name known to exist must NOT be reported dead. Catches an
                  empty/failed table list, which would flag all 65.

If any leg fails the response is `ok: false` with `self_test.passed: false`
and the findings are withheld, because an unverified scanner's output is
worse than no output. cf feedback_scan_that_can_find_nothing_needs_a_floor.

Auth: X-Admin-Key (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY). Read-only — this
module never writes to the database and never opens a PR.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from flask import Blueprint, jsonify, request

brain_null_signal_bp = Blueprint("brain_null_signal", __name__)

# Repo root: this file lives in <root>/routes/.
_ROOT = Path(__file__).resolve().parent.parent

# `to_regclass('public.foo')` / `to_regclass('foo')`, single or double quoted.
_PROBE_RE = re.compile(r"""to_regclass\(\s*['"]([A-Za-z_][A-Za-z0-9_.]*)['"]\s*\)""")

# Scan floor. 65 distinct probes existed on 2026-09-08; well below that means
# the regex or the walk broke, not that the idiom fell out of use.
_MIN_PROBES = 40

# Self-test canaries.
_CANARY_MISSING = "public.__null_signal_canary_absent__"
_CANARY_PRESENT = "brain_proposed_code_fixes"   # the table defect #2 should have named

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pg_migration_backups",
              "venv", ".venv", "dchub-venv", "tests"}


def _admin_ok() -> bool:
    exp = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    if not exp:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and got == exp


def _conn():
    import psycopg2
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not url:
        return None
    c = psycopg2.connect(url, connect_timeout=8)
    c.autocommit = True
    return c


def scan_table_probes(root: Path | None = None) -> dict:
    """Every `to_regclass(...)` call site in the tree → {name: [locations]}.

    Tests are excluded on purpose: a test may probe a fixture table that does
    not exist in production, and flagging it would train people to ignore this.
    """
    root = Path(root or _ROOT)
    found: dict[str, list[str]] = {}
    _self = Path(__file__).name
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        # ★ 2026-09-08: skip THIS module. Its first live run reported three
        # findings -- `public.X`, `public.foo`, `foo` -- all from its own
        # docstring and the comment above _PROBE_RE, where the idiom is
        # written out to explain it. A scanner that reads its own prose as
        # evidence is the same class of defect it hunts: 3 of 12 findings
        # were noise it manufactured. cf the corpora_missing sensor that
        # fired on ~100% of healthy traffic.
        if path.name == _self:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "to_regclass" not in text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            # A comment is documentation, not a probe. Handles the common
            # case; a docstring EXAMPLE in another module would still be
            # read as real, which is why the finding carries file:line --
            # a human triaging it can see prose at a glance.
            if line.lstrip().startswith("#"):
                continue
            for m in _PROBE_RE.finditer(line):
                rel = str(path.relative_to(root))
                found.setdefault(m.group(1), []).append(f"{rel}:{i}")
    return found


def live_relations(cur) -> set[str]:
    """Bare names of every table and view in the public schema."""
    cur.execute("""
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
        UNION
        SELECT viewname  FROM pg_views  WHERE schemaname = 'public'
    """)
    return {r[0] for r in cur.fetchall() if r and r[0]}


# ── Suppression ──────────────────────────────────────────────────────
#
# 2026-09-08. The first run reported 12 dead probes of which 2 were real
# defects. A sensor that fires on healthy input is not a sensor — the same
# failure as the corpora_missing sensor that fired on ~100% of healthy
# traffic. These three rules are the ones earned by hand-triaging that run.
#
# Only STRUCTURAL facts are inferred. Rule 2 and rule 3 are decidable from
# the tree. Anything that needs a human's judgement about intent must be
# written down as rule 1 rather than guessed at, because the guess this
# detector most wants to make is exactly the one that already misfired:
# "its else-branch logs an error, so it must be live". A dead module logs
# too. An error message proves someone anticipated the failure, not that
# the code runs — free_tier_limiter.py is 744 lines of never-executed
# proof of that.

_ANNOT_RE = re.compile(r"#\s*null-signal:\s*(\S.*?)\s*$")

# How far above a probe site to look for its annotation.
_ANNOT_LOOKBACK = 3


def _file_lines(root: Path, rel: str) -> list[str]:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _annotation_for(root: Path, site: str) -> str | None:
    """rule 1 — an explicit `# null-signal: <reason>` at or just above the site.

    Deliberate opt-out, written by whoever knows the intent, and greppable:
    `grep -rn "null-signal:"` lists every suppression in the tree.
    """
    rel, _, lineno = site.rpartition(":")
    if not lineno.isdigit():
        return None
    lines = _file_lines(root, rel)
    i = int(lineno) - 1
    for j in range(i, max(-1, i - _ANNOT_LOOKBACK - 1), -1):
        if 0 <= j < len(lines):
            m = _ANNOT_RE.search(lines[j])
            if m:
                return m.group(1)
    return None


def _creates_it(root: Path, rel: str, bare: str) -> bool:
    """rule 2 — lazy-create: the same file CREATEs the table it probes.

    `if to_regclass(...) is None: CREATE TABLE IF NOT EXISTS ...` is the
    idiom for a table that is supposed to be absent until first use. The
    probe returning NULL is the designed path, not a defect.
    """
    text = "\n".join(_file_lines(root, rel))
    return bool(re.search(
        r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(public\.)?" + re.escape(bare) + r"\b",
        text, re.I))


def _is_unreachable_module(root: Path, rel: str) -> bool:
    """rule 3 — nothing outside this file mentions the module at all.

    A probe inside code that never executes cannot be a live defect. The
    check is deliberately blunt: ANY mention of the module stem anywhere
    else in the tree (import, string, blueprint registration, dynamic
    loader) disqualifies it, so a module reached by a route this scan does
    not understand is never suppressed by mistake.
    """
    stem = Path(rel).stem
    if not stem:
        return False
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if str(path.relative_to(root)) == rel:
            continue
        try:
            if re.search(r"\b" + re.escape(stem) + r"\b",
                         path.read_text(encoding="utf-8", errors="ignore")):
                return False
        except OSError:
            continue
    return True


def classify(root: Path, probed: str, sites: list[str]) -> dict | None:
    """None => report it. Otherwise the reason it is suppressed.

    Every site must be suppressed for the probe to be suppressed: one live
    call site reading a table that does not exist is still a real defect,
    however many dead ones sit beside it.
    """
    bare = probed.split(".")[-1]
    reasons = []
    for site in sites:
        rel = site.rpartition(":")[0]
        note = _annotation_for(root, site)
        if note:
            reasons.append({"site": site, "rule": "annotated", "why": note})
            continue
        if _creates_it(root, rel, bare):
            reasons.append({"site": site, "rule": "lazy_create",
                            "why": f"{rel} creates {bare} when the probe "
                                   f"returns NULL"})
            continue
        if _is_unreachable_module(root, rel):
            reasons.append({"site": site, "rule": "unreachable_module",
                            "why": f"nothing outside {rel} mentions "
                                   f"{Path(rel).stem} — the probe cannot run"})
            continue
        return None
    return {"rules": sorted({r["rule"] for r in reasons}), "sites": reasons}


def dead_probes(probes: dict, live: set[str]) -> list[dict]:
    """Probed names with no matching relation. `public.x` and `x` both match
    the bare relation name — to_regclass resolves through search_path."""
    out = []
    for name in sorted(probes):
        if name.split(".")[-1] not in live:
            out.append({"probed": name, "sites": sorted(probes[name])[:6],
                        "site_count": len(probes[name])})
    return out


def split_dead(dead: list[dict], root: Path | None = None) -> dict:
    """Partition dead probes into what to report and what is by design.

    Both halves are returned. The suppressed list is not swallowed — it is
    published with the rule that suppressed each one, so a wrong suppression
    is visible in the same response rather than silently absent.
    """
    root = Path(root or _ROOT)
    reported, suppressed = [], []
    for d in dead:
        why = classify(root, d["probed"], d["sites"])
        if why is None:
            reported.append(d)
        else:
            suppressed.append({**d, "suppressed_by": why})
    return {"reported": reported, "suppressed": suppressed}


def _self_test(probes: dict, live: set[str]) -> dict:
    """Three legs. All must pass or the findings are withheld."""
    legs = {}

    legs["scan_floor"] = {
        "found": len(probes), "min": _MIN_PROBES,
        "passed": len(probes) >= _MIN_PROBES,
        "why": ("a broken scan finds zero probes and reports a clean bill of "
                "health — the most dangerous possible green"),
    }

    neg = dead_probes({_CANARY_MISSING: ["<canary>"]}, live)
    legs["negative_canary"] = {
        "name": _CANARY_MISSING, "reported_dead": bool(neg),
        "passed": bool(neg),
        "why": "a comparison that always passes would miss every real defect",
    }

    pos = dead_probes({_CANARY_PRESENT: ["<canary>"]}, live)
    legs["positive_canary"] = {
        "name": _CANARY_PRESENT, "reported_dead": bool(pos),
        "passed": not pos,
        "why": ("an empty or failed relation list would flag every probe as "
                "dead and bury the real ones"),
    }

    return {"passed": all(l["passed"] for l in legs.values()), "legs": legs}


@brain_null_signal_bp.route("/api/v1/admin/brain/null-signals",
                            methods=["GET", "POST"])
def null_signals():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401

    probes = scan_table_probes()
    c = None
    try:
        c = _conn()
        if c is None:
            return jsonify(ok=False, error="no DATABASE_URL"), 200
        with c.cursor() as cur:
            live = live_relations(cur)
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    st = _self_test(probes, live)
    body = {
        "ok": st["passed"],
        "check": "dead_table_probes",
        "self_test": st,
        "probes_scanned": len(probes),
        "relations_live": len(live),
    }
    if not st["passed"]:
        # Withheld on purpose: an unverified scanner's output is worse than
        # none, because it will be believed.
        body["findings_withheld"] = ("self-test failed — the scan cannot be "
                                     "trusted, so its findings are not reported")
        return jsonify(body), 200

    dead = dead_probes(probes, live)
    split = split_dead(dead)
    body["dead_probe_count"] = len(split["reported"])
    body["dead_probes"] = split["reported"]
    # Published, not swallowed: a wrong suppression has to be visible in the
    # same response, or this becomes a way to hide findings instead of rank
    # them.
    body["suppressed_count"] = len(split["suppressed"])
    body["suppressed"] = split["suppressed"]
    body["dead_probe_count_unsuppressed"] = len(dead)
    body["note"] = ("each dead probe is an `if to_regclass(...)` branch that "
                    "never runs and emits no error — a feature that is off "
                    "with nothing anywhere saying so. `suppressed` lists the "
                    "ones that are by design, each with the rule that "
                    "suppressed it: annotated (an explicit `# null-signal:` "
                    "comment), lazy_create (the file creates the table it "
                    "probes), unreachable_module (nothing else in the tree "
                    "mentions the module).")
    return jsonify(body), 200
