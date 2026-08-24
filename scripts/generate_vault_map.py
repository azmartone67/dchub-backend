#!/usr/bin/env python3
"""scripts/generate_vault_map.py — generate the Obsidian architecture map.

WHY THIS EXISTS
---------------
On 2026-08-11 an audit of this codebase re-proposed THREE already-shipped
capabilities as missing work (the #49 loop graph, fix-history recall, the
authenticated any-corpus RAG retrieve) because there was no map — 758 route
modules, 68 master shells and 23 numbered brain layers, navigable only by grep.
The Obsidian vault held five notes, none about the architecture.

★THE MAP IS GENERATED, NOT WRITTEN. A hand-written map is accurate for one day
and then lies quietly, which is worse than no map — the whole point of this
series of fixes is to stop failures from being rendered as benign values, and a
stale map is exactly that. Every fact below is read out of the tree at run time;
re-run the script and the map is true again.

WHAT IT WRITES (into the vault, one note each)
  Architecture Map     hub note: counts + links, the entry point
  Master Shells        every *_master_shell.py: purpose, route, kill switch,
                       whether it is registered, whether a cron drives it
  Brain Layers         the numbered L*-layers and what each one is for
  Loop Graph           probed loops, declared edges, typed source nodes

It does NOT overwrite hand-written notes (Context Integrity, Admin Cache Leak,
Traps): it only writes files it owns, each stamped `generated: true`.

USAGE
  python3 scripts/generate_vault_map.py                 # default vault path
  python3 scripts/generate_vault_map.py --vault ~/path  # elsewhere
  python3 scripts/generate_vault_map.py --check         # exit 1 if stale

`--check` regenerates into memory and compares; it is how CI can prove the map
still matches the tree without committing churn.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROUTES = os.path.join(_REPO, "routes")
_DEFAULT_VAULT = os.path.expanduser("~/Documents/DCHUB")

# ★2026-08-12 — the IN-REPO copy is what CI can actually check.
# The vault is a local Obsidian directory outside the repo, so a checkout has no
# copy of it and `--check --vault ~/Documents/DCHUB` can never run on a runner.
# Shipping `--check` while claiming "CI can prove the map matches the tree" would
# have been the exact thing this codebase keeps being bitten by: a guard that
# reads as wired and enforces nothing. The generator therefore writes BOTH — the
# vault for humans, docs/architecture/ for the machine — and --check compares the
# in-repo copy by default.
_REPO_DOCS = os.path.join(_REPO, "docs", "architecture")

# Notes this script owns. Anything else in the vault is hand-written and is
# never touched.
_OWNED = ("Architecture Map.md", "Master Shells.md", "Brain Layers.md",
          "Loop Graph.md")


# ── helpers ───────────────────────────────────────────────────────────

def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except Exception:
        return ""


def _docline(src: str) -> str:
    """First meaningful sentence of the module docstring.

    Skips the `path.py — ` prefix these modules conventionally open with, and
    stops at the first blank line so a multi-paragraph rationale does not land
    in a table cell."""
    try:
        doc = ast.get_docstring(ast.parse(src)) or ""
    except Exception:
        return ""
    lines = [l.strip() for l in doc.splitlines()]
    body = []
    for l in lines:
        if not l:
            if body:
                break
            continue
        body.append(l)
    text = " ".join(body)
    text = re.sub(r"^[\w/.\-]+\.py\s*[—-]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:260]


def _first(pattern: str, src: str, group: int = 1) -> str:
    m = re.search(pattern, src)
    return m.group(group) if m else ""


def _esc(s: str) -> str:
    """Table-cell safe: a raw pipe would split the column."""
    return (s or "").replace("|", "\\|")


# ── collectors ────────────────────────────────────────────────────────

def collect_shells() -> list:
    main_src = _read(os.path.join(_REPO, "main.py"))
    cron_src = _read(os.path.join(_ROUTES, "cron_heartbeat.py"))
    out = []
    for fn in sorted(os.listdir(_ROUTES)):
        if not fn.endswith("_master_shell.py"):
            continue
        src = _read(os.path.join(_ROUTES, fn))
        stem = fn[:-len("_master_shell.py")]
        bp = _first(r"Blueprint\(\s*[\"']([A-Za-z0-9_]+)[\"']", src) or fn[:-3]
        # Registration can happen in main.py OR cron_heartbeat.py — assuming
        # main.py alone is what made lane 3 of shell #63 report a live shell as
        # dead code on 2026-08-12.
        where = []
        if fn[:-3] in main_src:
            where.append("main.py")
        if fn[:-3] in cron_src:
            where.append("cron_heartbeat.py")
        route = _first(r"[\"'](/admin/[a-z0-9\-]+)[\"']", src)
        kill = _first(r"os\.environ\.get\(\s*[\"']([A-Z0-9_]*DISABLE[A-Z0-9_]*)[\"']", src)
        crond = bool(re.search(re.escape("/admin/" + (route.split("/")[-1] or stem)),
                               cron_src)) if route else False
        out.append({
            "file": fn, "stem": stem, "blueprint": bp,
            "purpose": _docline(src), "route": route,
            "kill": kill, "registered": ", ".join(where) or "NOT REGISTERED",
            "cron": "yes" if crond else "no",
        })
    return out


def collect_layers() -> list:
    out = []
    for fn in sorted(os.listdir(_ROUTES)):
        m = re.match(r"brain_layer(\d+)_(.+)\.py$", fn)
        if not m:
            continue
        src = _read(os.path.join(_ROUTES, fn))
        out.append({
            "n": int(m.group(1)),
            "file": fn,
            "name": m.group(2).replace("_", " "),
            "purpose": _docline(src),
        })
    return sorted(out, key=lambda r: (r["n"], r["file"]))


def collect_loops() -> dict:
    loops_src = _read(os.path.join(_ROUTES, "system_loops.py"))
    graph_src = _read(os.path.join(_ROUTES, "graph_master_shell.py"))
    # ★Derive loops from the PROBE FUNCTIONS, not from a regex over `"name":`.
    # The first cut scraped every `"name": "..."` in the file and reported 8
    # loops, because a docstring in _iso_cadence_hours quotes heartbeat's
    # `{"name": "iso_metrics", "stale_hours": 12}` as an example. A map that
    # invents an eighth loop out of a comment is exactly the silent-staleness
    # this generator exists to prevent — so the source of truth is the set of
    # functions that actually run.
    # ★The reported name, read from inside each probe body — NOT the function
    # name and NOT a file-wide regex.
    #   · file-wide `"name":` scraping reported 8 loops, inventing one out of a
    #     docstring that quotes heartbeat's {"name": "iso_metrics"} as an example
    #   · the function name alone gives `auto_press`, while the board actually
    #     reports `auto_press_daily` — a map keyed differently from the thing it
    #     maps sends you looking for a loop that does not exist under that name
    probes = []
    for m in re.finditer(r"^def (_probe_[a-z_]+)\s*\(", loops_src, re.M):
        body = loops_src[m.end():]
        nxt = re.search(r"^def ", body, re.M)
        body = body[:nxt.start()] if nxt else body
        nm = re.search(r'"name":\s*"([a-z_]+)"', body)
        probes.append(nm.group(1) if nm else m.group(1)[len("_probe_"):])
    edges, sources = [], []
    try:
        sys.path.insert(0, _REPO)
        from routes.graph_master_shell import (  # noqa: E402
            LOOP_EDGES, LOOP_SOURCE_PRODUCERS)
        edges = list(LOOP_EDGES or ())
        sources = list(LOOP_SOURCE_PRODUCERS or ())
    except Exception as e:
        print("  warn: could not import the graph constants: %s" % e,
              file=sys.stderr)
    return {"probes": sorted(set(probes)), "edges": edges,
            "sources": sources, "has_graph_src": bool(graph_src)}


# ── renderers ─────────────────────────────────────────────────────────

_FRONT = ("---\ntags: [dchub, architecture, generated]\ngenerated: true\n"
          "source: scripts/generate_vault_map.py\n---\n\n")

_STALE = ("> [!warning] Generated file — do not edit by hand\n"
          "> Re-run `python3 scripts/generate_vault_map.py` after any change "
          "to the tree. Hand edits are overwritten, and a hand-maintained map "
          "goes stale silently, which is the failure mode this whole map "
          "exists to prevent.\n\n")


def render_shells(shells: list) -> str:
    unreg = [s for s in shells if s["registered"] == "NOT REGISTERED"]
    stems = {}
    for s in shells:
        for o in shells:
            if s is o:
                continue
            a, b = s["stem"], o["stem"]
            if a != b and (a.endswith(b) or b.endswith(a)):
                stems.setdefault(min(a, b, key=len), set()).add(max(a, b, key=len))
    body = [_FRONT, "# Master Shells\n\n", _STALE,
            "%d shells. A *master shell* is a read-only diagnostic with lanes; "
            "each lane names its actuator and fires nothing.\n\n" % len(shells)]
    body.append("| shell | purpose | route | registered in | cron | kill |\n")
    body.append("|---|---|---|---|---|---|\n")
    for s in shells:
        body.append("| `%s` | %s | %s | %s | %s | `%s` |\n" % (
            s["stem"], _esc(s["purpose"])[:150] or "—",
            "`%s`" % s["route"] if s["route"] else "—",
            s["registered"], s["cron"], s["kill"] or "—"))
    if unreg:
        body.append("\n> [!danger] %d shell(s) not registered anywhere\n> %s\n"
                    % (len(unreg), ", ".join("`%s`" % u["file"] for u in unreg)))
    if stems:
        body.append("\n## Overlapping name stems\n\nNot proof of duplication — "
                    "where to *look* for it.\n\n")
        for k, v in sorted(stems.items()):
            body.append("- `%s` ↔ %s\n" % (k, ", ".join("`%s`" % x for x in sorted(v))))
    body.append("\nRelated: [[Architecture Map]], [[Loop Graph]]\n")
    return "".join(body)


def render_layers(layers: list) -> str:
    body = [_FRONT, "# Brain Layers\n\n", _STALE,
            "%d numbered layers. Each reports in isolation; L14 reads ACROSS "
            "them, L18 consolidates outcomes into lessons, and L16 tracks "
            "whether past predictions came true.\n\n" % len(layers)]
    body.append("| layer | module | purpose |\n|---|---|---|\n")
    for l in layers:
        body.append("| **L%d** | `%s` | %s |\n"
                    % (l["n"], l["file"], _esc(l["purpose"])[:190] or "—"))
    body.append("\nRelated: [[Architecture Map]], [[Context Integrity]]\n")
    return "".join(body)


def render_loops(g: dict) -> str:
    body = [_FRONT, "# Loop Graph\n\n", _STALE,
            "Shell #49's graph. Every row of `/api/v1/system/loops` carries "
            "`input_status`, so a loop running on dead input can no longer "
            "report a green board.\n\n"]
    body.append("## Probed loops\n\n%s\n\n"
                % ", ".join("`%s`" % p for p in g["probes"]))
    body.append("## Declared edges (producer → consumer)\n\n")
    if g["edges"]:
        body.append("| producer | consumer | kind | evidence |\n|---|---|---|---|\n")
        for e in g["edges"]:
            body.append("| `%s` | `%s` | %s | %s |\n" % (
                e.get("producer"), e.get("consumer"),
                e.get("kind"), e.get("evidence")))
    else:
        body.append("_none readable_\n")
    body.append("\n## Source nodes (producer is OUTSIDE the board)\n\n")
    body.append("> [!note] A root is not a gap\n> These have no upstream loop "
                "and never will. They must never be given an edge — an invented "
                "edge would be trusted exactly as much as a proven one.\n\n")
    if g["sources"]:
        body.append("| loop | external producer |\n|---|---|\n")
        for s in g["sources"]:
            body.append("| `%s` | %s |\n" % (s.get("loop"),
                                             _esc(str(s.get("producer")))))
    else:
        body.append("_none declared_\n")
    body.append("\nRelated: [[Architecture Map]], [[Master Shells]]\n")
    return "".join(body)


def render_hub(shells: list, layers: list, g: dict, nroutes: int) -> str:
    return (
        _FRONT + "# Architecture Map\n\n" + _STALE +
        "Entry point for the DC Hub backend. Generated from the tree, so it "
        "cannot quietly go stale.\n\n"
        "| | count |\n|---|---|\n"
        "| route modules | %d |\n| master shells | %d |\n"
        "| numbered brain-layer modules | %d |\n| probed loops | %d |\n"
        "| declared loop edges | %d |\n| typed source nodes | %d |\n\n"
        "_Layer modules outnumber layer numbers — L14, L15 and L22 each ship "
        "more than one module._\n\n"
        "## Notes\n\n"
        "- [[Master Shells]] — what each shell is for, and whether anything runs it\n"
        "- [[Brain Layers]] — L4…L23 and their jobs\n"
        "- [[Loop Graph]] — producers, consumers, and roots\n"
        "- [[Context Integrity]] — the envelope, the meter, and the open findings\n"
        "- [[Admin Cache Leak]] — why `/api/v1/*` reads must be cache-busted\n\n"
        "## Before you propose new work\n\n"
        "> [!important] Query the fix history first\n"
        "> ```bash\n"
        "> curl -sS -H \"X-Admin-Key: $DCHUB_ADMIN_KEY\" \\\n"
        ">   \"https://dchub.cloud/api/v1/admin/brain/rag/retrieve?q=YOUR+QUESTION&k=5&corpus=fix_history\"\n"
        "> ```\n"
        "> Closed issues, fix commits and resolved findings are embedded there. "
        "An audit on 2026-08-11 re-proposed three already-shipped capabilities "
        "because it grepped the repo instead. Also check "
        "`routes/brain_capability_ledger.py`.\n"
        % (nroutes, len(shells), len(layers), len(g["probes"]),
           len(g["edges"]), len(g["sources"])))


# ── main ──────────────────────────────────────────────────────────────

# ★ `routes/_proposed_*.py` ARE NOT ROUTE MODULES. The brain writes them as
# draft proposals attached to a strategic-draft PR; not one is registered as a
# blueprint in main.py (2026-08-24: 27 files, 0 registrations — the closest
# matches are the `brain_proposed_*` tables and routes/brain_proposed_debug.py,
# which is a different module, and main.py line ~2949 describes one of these
# drafts as "a 501"). Counting them inflated `route modules` by 27.
#
# That is wrong on this map's OWN terms before it is a CI problem: this file
# exists because "758 route modules were navigable only by grep" let an audit
# re-propose three already-shipped capabilities. Padding that number with the
# very drafts that caused the confusion makes the map less navigable, not more.
#
# It also made every brain-l6 strategic-draft PR born red. Such a PR adds one
# routes/_proposed_*.py, which bumped the count, which made the committed map
# stale, which failed test_the_in_repo_copy_is_current — on a file the bot has
# no reason to know it must regenerate. Promotion to a real route drops the
# prefix, so a promoted module starts counting the moment it stops being a draft.
_DRAFT_ROUTE_PREFIX = "_proposed_"


def live_route_modules() -> list:
    """Every .py in routes/ that is not an unregistered brain draft."""
    return sorted(f for f in os.listdir(_ROUTES)
                  if f.endswith(".py")
                  and not f.startswith(_DRAFT_ROUTE_PREFIX))


def build() -> dict:
    shells = collect_shells()
    layers = collect_layers()
    g = collect_loops()
    nroutes = len(live_route_modules())
    return {
        "Architecture Map.md": render_hub(shells, layers, g, nroutes),
        "Master Shells.md": render_shells(shells),
        "Brain Layers.md": render_layers(layers),
        "Loop Graph.md": render_loops(g),
    }


def _write_into(target: str, notes: dict) -> int:
    os.makedirs(target, exist_ok=True)
    for name, text in notes.items():
        with open(os.path.join(target, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    print("  wrote %d notes into %s" % (len(notes), target))
    return len(notes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=_DEFAULT_VAULT,
                    help="Obsidian vault (skipped silently if absent — CI has none)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the in-repo copy differs from the tree")
    ap.add_argument("--check-target", default=_REPO_DOCS,
                    help="directory --check compares against")
    a = ap.parse_args()
    notes = build()
    if a.check:
        stale = []
        for name, text in notes.items():
            cur = _read(os.path.join(a.check_target, name))
            if cur.strip() != text.strip():
                stale.append(name)
        if stale:
            print("STALE: %s\nRe-run: python3 scripts/generate_vault_map.py"
                  % ", ".join(stale), file=sys.stderr)
            return 1
        print("architecture map matches the tree (%d notes)" % len(notes))
        return 0
    # In-repo copy first: it is the one CI verifies, so it must never be the
    # step that gets skipped.
    _write_into(_REPO_DOCS, notes)
    if os.path.isdir(a.vault):
        _write_into(a.vault, notes)
    else:
        print("  vault not found (%s) — in-repo copy written anyway" % a.vault)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
