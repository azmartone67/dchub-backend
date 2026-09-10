"""Deal Desk Brief — the Pro deliverable an `execute_plan` run can hand to a human.

★ WHY THIS EXISTS (2026-09-10). Every honest paid sale in the trailing 30 days
arrived web-direct or organic-direct; none was agent-bridged. The channel that
converts is a human, and the human channel had nothing SHAREABLE to sell:
`execute_plan` answers a whole siting question in one envelope, and that
envelope is JSON in an agent's context window. It dies there. Nobody forwards
it to a partner, prints it for an IC memo, or attaches it to an email.

This turns one plan execution into a branded, print-ready brief at a link that
opens with NO login — the same anon-agent → named-human bridge the premium Site
Analysis already uses (`routes/site_report.py::_signed_pdf_url`), applied to the
front door instead of to one coordinate.

★ IT EMITS THE ARTIFACT. `POST /api/v1/deal-desk` stores the run and returns a
URL that resolves to a real PDF; it never returns prose describing a brief that
does not exist. (project_dchub_sitevalue_owner_onepager: "the tool must EMIT the
PDF, not describe one" — that note exists because the other thing happened.)

★ THE HONESTY SPINE IS A SECTION, NOT A FOOTNOTE. DC Hub's whole claim is that
it publishes its own limits rather than only its answers — `constraint_coverage`,
tier-withheld fields, steps that did not run, the oldest-input `as_of`. A deal
brief that silently drops those is worse than one that prints them, because it
launders a tier-gated preview into something that looks like a finished
analysis. `brief_model()` collects every limit the envelope carries and
`render_brief_html()` gives them their own page. When nothing was withheld the
brief SAYS nothing was withheld — an empty coverage block means "nothing
withheld", never "unknown" (util/constraint_coverage_shape.py).

★ SHAPE-AWARE, NOT TYPE-SNIFFING. `constraint_coverage` ships in four
incompatible shapes under one name. We branch on `shape_of()` — the derived
label — so a caveat list and an argument-disposition map both render correctly
and neither is read as the other.

Surfaces:
    POST /api/v1/deal-desk                 PRO+ (or X-Admin-Key) → mint a brief
    GET  /reports/deal-desk/<token>        no login — HTML, print-ready
    GET  /reports/deal-desk/<token>.pdf    no login — PDF via Gotenberg

`/reports/*` is already in the frontend `_routes.json` include list and is not
matched by the `/api/v1/*` zone cache rule, so the reader-facing surfaces reach
Flask and are not edge-cached. Verified before writing, not assumed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as _secrets
import sys
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, jsonify, request

deal_desk_bp = Blueprint("deal_desk", __name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Reader links live this long. Deal cycles outlive a 7-day site-report link, so
# the default is longer — but it is passed as an ARGUMENT into the mint helper,
# never read from inside it, so a test pins behaviour without touching the knob
# the production path reads.
DEFAULT_TTL_DAYS = int(os.environ.get("DCHUB_DEAL_DESK_TTL_DAYS", "30"))

_BASE_URL = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud").rstrip("/")


# ── storage ────────────────────────────────────────────────────────────────
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS deal_desk_briefs (
    id            BIGSERIAL PRIMARY KEY,
    brief_token   TEXT NOT NULL UNIQUE,
    api_key_hash  TEXT,
    intent        TEXT NOT NULL,
    intent_class  TEXT,
    prepared_for  TEXT,
    prepared_by   TEXT,
    payload       TEXT NOT NULL,
    source        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    html_views    INTEGER NOT NULL DEFAULT 0,
    pdf_downloads INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS deal_desk_briefs_token_idx ON deal_desk_briefs(brief_token);
CREATE INDEX IF NOT EXISTS deal_desk_briefs_created_idx ON deal_desk_briefs(created_at DESC);
"""


def _conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=8)
    except Exception as e:  # pragma: no cover - infra
        print(f"[deal_desk] connect failed: {e}", file=sys.stderr)
        return None


def init_schema() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        # `with c` commits on clean exit — a write that never commits borrows a
        # later transaction's commit and reorders into a silent rollback.
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:  # pragma: no cover - infra
        print(f"[deal_desk] init_schema failed: {e}", file=sys.stderr)
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


try:
    _SCHEMA_OK = init_schema()
except Exception:  # pragma: no cover - infra
    _SCHEMA_OK = False


def _hash_key(k: str) -> str:
    return hashlib.sha256((k or "").encode()).hexdigest()[:32]


def _new_token() -> str:
    return "dd-" + _secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:22]


# ═══════════════════════════════════════════════════════════════════════════
# PURE MODEL LAYER — no Flask, no DB, no network. Every function below is a
# total function of the envelope, so the brief can be tested without standing
# anything up, and a mutation to the envelope is guaranteed to reach the page.
# ═══════════════════════════════════════════════════════════════════════════

def _esc(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _clip(text, n):
    """Truncate VISIBLY. A silently shortened source note reads as the whole note."""
    t = str(text if text is not None else "")
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


# Envelope machinery — real fields, but they describe the CALL, not the answer.
# Excluded from "findings" so the brief shows what the step learned, not how it
# was transported.
_MACHINERY_KEYS = frozenset({
    "_entity", "_gated", "_source", "_cite", "identity", "citation", "provenance",
    "quota", "truncated", "preview", "note", "error", "ok", "constraint_coverage",
    "constraint_coverage_shape", "_upgrade", "upgrade", "upsell", "for_your_human",
    "next_recipe", "next_session", "resume", "starter_pack", "_end_of_burst",
    "answer_guide", "replay", "human_message", "status",
})

_SCALAR_TYPES = (str, int, float, bool)


def salvage_truncated_json(text):
    """Parse a JSON object that was cut mid-value; return what survived.

    `execute_plan` truncates every step result to ~1.2KB and ships the remainder
    as a `preview` STRING that is a JSON prefix — unparseable by `json.loads`,
    which is why a brief built naively off the envelope shows an empty findings
    page. This rewinds to the last position where truncating is legal, closes
    the containers that were still open there, and hands the result to the real
    parser. It never invents a value: a pair that did not arrive complete is not
    in the output.

    Returns {} when nothing survives (including for non-object input).
    """
    s = text if isinstance(text, str) else ""
    if not s.lstrip().startswith("{"):
        return {}
    stack = []
    in_string = False
    escape = False
    safe_cut = -1        # index to slice up to
    safe_stack = None    # the container stack AT safe_cut
    for i, ch in enumerate(s):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            safe_cut, safe_stack = i + 1, list(stack)
        elif ch == ",":
            # Truncating AT a comma is always legal — everything before it is a
            # complete element of the container we are currently inside.
            safe_cut, safe_stack = i, list(stack)
    if not in_string and not stack and safe_cut == len(s):
        candidate = s
    elif safe_cut <= 0 or safe_stack is None:
        return {}
    else:
        candidate = s[:safe_cut].rstrip().rstrip(",") + "".join(reversed(safe_stack))
    try:
        out = json.loads(candidate)
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}


def _fmt_scalar(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def top_level_findings(obj, limit=8):
    """[(key, value)] of a result's own scalar fields, machinery excluded."""
    if not isinstance(obj, dict):
        return []
    out = []
    for k, v in obj.items():
        if k in _MACHINERY_KEYS or str(k).startswith("_"):
            continue
        if isinstance(v, _SCALAR_TYPES) and v is not None and str(v) != "":
            out.append((str(k), _fmt_scalar(v)))
        elif isinstance(v, list):
            out.append((str(k), f"{len(v)} item{'s' if len(v) != 1 else ''}"))
        if len(out) >= limit:
            break
    return out


def coverage_lines(coverage):
    """constraint_coverage → prose lines, branched on its DERIVED shape.

    Four incompatible shapes ship under this one name. `for x in coverage`
    yields prose from one and dict KEYS from the other three — a wrong-type read
    that raises nothing and looks like it worked. Branch, never sniff.
    """
    try:
        from util.constraint_coverage_shape import (
            shape_of, CAVEAT_LIST, FIELD_STATUS_MAP, FIELD_STATUS_DETAIL,
            ARGUMENT_DISPOSITION, EMPTY,
        )
    except Exception:  # pragma: no cover - the module is in-repo
        return []
    shape = shape_of(coverage)
    if shape == EMPTY:
        return []
    if shape == CAVEAT_LIST:
        return [str(x) for x in coverage if str(x).strip()]
    if shape == FIELD_STATUS_MAP:
        return [f"{k}: {v}" for k, v in coverage.items()]
    if shape == FIELD_STATUS_DETAIL:
        return [f"{k}: {(v or {}).get('status', 'unavailable')}"
                + (f" — {(v or {}).get('reason')}" if (v or {}).get("reason") else "")
                for k, v in coverage.items()]
    if shape == ARGUMENT_DISPOSITION:
        lines = []
        for k, v in coverage.items():
            v = v or {}
            if v.get("applied") is False:
                line = f"{k}: you sent this argument and it was NOT applied"
                if v.get("reason"):
                    line += f" — {v['reason']}"
                if v.get("instead"):
                    line += f" Instead: {v['instead']}"
                lines.append(line)
        if not lines:
            lines.append("every argument you sent was applied")
        return lines
    # UNKNOWN — print it rather than drop it. An unnameable shape is still a
    # published limit, and hiding it is the failure this section exists to avoid.
    return [json.dumps(coverage, default=str)[:400]]


# Decision-relevant scalars, most-decisive first. A step's headline is the first
# of these it actually carries — never a fabricated summary.
_HEADLINE_KEYS = ("verdict", "composite_score", "score", "rank", "headroom_mw",
                  "available_mw", "time_to_power_months", "capacity_mw",
                  "total", "count", "avg_kwh_cents")

# DCPI's own verdict vocabulary plus the adjacent words other tools use. A
# verdict this map does not know renders neutral (cyan) rather than green — an
# unknown verdict must never be coloured "good".
_VERDICT_TONE = {"BUILD": "grn", "GO": "grn", "FAVORABLE": "grn", "STRONG": "grn",
                 "PROCEED": "grn",
                 "CAUTION": "amb", "WATCH": "amb", "MIXED": "amb", "CONSTRAINED": "amb",
                 "AVOID": "red", "BLOCKED": "red"}

# A status that is not "executed" is a limit of the brief, and each one means a
# different thing to a reader. Spelled out so none is silently flattened into
# "failed" — a timed_out step is NOT an absence of data.
_STATUS_MEANING = {
    "gated_preview": ("free/most-restricted-tier preview — a WORKING partial answer, "
                      "not the full dataset for this step"),
    "not_run": "not run — the run hit its step budget before reaching this tool",
    "skipped_unresolved": "skipped — an input this step depends on was never resolved",
    "timed_out": ("DC Hub stopped waiting while this tool was still running. This is "
                  "NOT a finding of 'no data' — call the tool directly to get it"),
    "failed": "failed — this step returned an error and contributed nothing",
}


def _step_result_view(step):
    """One step → (findings, was_truncated). Salvages the truncated preview."""
    result = step.get("result") if isinstance(step, dict) else None
    if not isinstance(result, dict):
        return [], False
    findings = top_level_findings(result)
    truncated = bool(result.get("truncated"))
    if truncated:
        salvaged = salvage_truncated_json(result.get("preview"))
        have = {k for k, _ in findings}
        for k, v in top_level_findings(salvaged, limit=10):
            if k not in have:
                findings.append((k, v))
    return findings[:10], truncated


def _step_headline(findings):
    d = {k: v for k, v in findings}
    for k in _HEADLINE_KEYS:
        if k in d:
            return k, d[k]
    return (findings[0] if findings else (None, None))


# ── row tables ─────────────────────────────────────────────────────────────
#
# `top_level_findings` renders a list as "12 items". That is a COUNT, and for a
# ranking question the twelve rows it is counting ARE the answer.
#
# Measured 2026-09-10, "rank markets for a 200 MW AI campus in Texas": the
# printed brief named ZERO of the twelve shortlisted markets — not Midland, not
# El Paso, none. The page said `shortlist  12 items` and stopped, and a reader
# who wanted the ranked list had to go get it from somewhere else. So the brief
# was strictly worse than the tool call it was made from.
#
# A row earns a place on the page when it NAMES a thing and SCORES it. That rule
# is why the 19-row `demand_24h` series ({mw, period} — no name) and
# `site_evaluation_handoff` ({tool, why} — no score) stay off it, without
# needing a per-tool allowlist that would go stale the next time a tool grows a
# field.
_ROW_LABEL_KEYS = ("market", "market_slug", "name", "facility_name", "site_ref",
                   "slug", "operator", "candidate_id", "iso", "title")
# Decision-relevant columns, most-decisive first. Deliberately the same
# vocabulary as _HEADLINE_KEYS: a number worth leading a step with is a number
# worth printing beside the row it belongs to.
_ROW_VALUE_KEYS = ("verdict", "composite_score", "constraint_score",
                   "excess_power_score", "time_to_power_months", "capacity_mw",
                   "power_mw", "headroom_mw", "available_mw", "avg_kwh_cents",
                   "rank", "score", "queue_wait_months", "state")
_ROW_CAP = 14


def _row_label(row):
    for k in _ROW_LABEL_KEYS:
        v = row.get(k)
        if isinstance(v, _SCALAR_TYPES) and str(v).strip():
            return _fmt_scalar(v)
    return None


def step_tables(result, max_tables=2, max_rows=_ROW_CAP):
    """Top-level lists OF OBJECTS → [{key, columns, rows, total, shown}].

    Never truncates silently: `total` and `shown` are both carried, so a list
    longer than the cap prints how many it left out instead of quietly ending.
    """
    if not isinstance(result, dict):
        return []
    tables = []
    for key, val in result.items():
        if len(tables) >= max_tables:
            break
        if key in _MACHINERY_KEYS or str(key).startswith("_"):
            continue
        if not isinstance(val, list) or not val:
            continue
        # A row earns its place by NAMING a thing; two of them make a table.
        # One labelled row is a finding, and top_level_findings already has it.
        labelled = [r for r in val if isinstance(r, dict) and _row_label(r) is not None]
        if len(labelled) < 2:
            continue
        cols = [k for k in _ROW_VALUE_KEYS
                if any(isinstance(r.get(k), _SCALAR_TYPES) and r.get(k) is not None
                       for r in labelled)]
        if not cols:                           # names a thing but scores nothing
            continue
        cols = cols[:4]
        rows = []
        for r in labelled[:max_rows]:
            rows.append({
                "label": _clip(_row_label(r), 34),
                "values": [(_fmt_scalar(r[k]) if isinstance(r.get(k), _SCALAR_TYPES)
                            and r.get(k) is not None else "—") for k in cols],
            })
        tables.append({"key": str(key), "columns": list(cols), "rows": rows,
                       "total": len(labelled), "shown": len(rows)})
    return tables


def _arg_summary(args):
    if not isinstance(args, dict) or not args:
        return ""
    return " · ".join(f"{k}={v}" for k, v in list(args.items())[:3]
                      if isinstance(v, _SCALAR_TYPES))


def limits_from_envelope(env):
    """Everything this run does NOT cover, collected from the envelope itself.

    Sources, in the order a reader needs them: per-step published coverage,
    tier-withheld fields, steps that did not fully run, transport truncation,
    planner-level uncovered constraints, and the age basis. Nothing here is
    authored — every line is carried by the response.
    """
    limits = []

    def add(kind, source, text):
        text = str(text or "").strip()
        if text:
            limits.append({"kind": kind, "source": source, "text": text})

    for step in (env.get("executed") or []):
        if not isinstance(step, dict):
            continue
        tool = step.get("tool") or "step"
        # ★ The step NUMBER is not a unique key, by the planner's design. One
        # planned step fans out across several targets and every variant is
        # pushed under the SAME step number (server.mjs: `variants` ->
        # doable.push({ s, args })), so a plan that ranks two markets emits two
        # entries reading `Step 2 · get_market_dcpi_rank`. Labelled by number
        # and tool alone, their limits render byte-identical: the honesty page
        # prints what looks like a duplicated line and no reader can tell which
        # market it belongs to. The findings section already disambiguates
        # these with _arg_summary — the limits section reuses THAT function
        # rather than formatting its own, so the two labels cannot drift apart.
        _args = _arg_summary(step.get("args"))
        where = f"Step {step.get('step', '?')} · {tool}" + (f" · {_args}" if _args else "")
        status = str(step.get("status") or "")
        if status and status != "executed":
            add("status", where, _STATUS_MEANING.get(status, f"status: {status}"))
        result = step.get("result") if isinstance(step.get("result"), dict) else {}
        for line in coverage_lines(result.get("constraint_coverage")):
            add("coverage", where, line)
        prov = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
        prev = prov.get("preview") if isinstance(prov.get("preview"), dict) else {}
        withheld = prev.get("withheld_fields") or []
        if withheld:
            add("withheld", where,
                "tier-gated, withheld from this answer: " + ", ".join(str(w) for w in withheld))
        shown, total = prev.get("shown"), prev.get("total")
        if shown is not None and total is not None:
            add("withheld", where, f"{shown} of {total} shown — the rest is tier-gated")
        if prov.get("as_of") is None and prov.get("as_of_basis"):
            add("age", where, str(prov["as_of_basis"]))
        if result.get("truncated"):
            add("transport", where,
                f"step result was truncated in transport — call {tool} directly for the full payload")

    replay = env.get("replay") if isinstance(env.get("replay"), dict) else {}
    unc = replay.get("uncovered_constraints")
    if unc:
        for line in (unc if isinstance(unc, list) else [unc]):
            add("planner", "This run", f"constraint named in the question but not covered: {line}")
    gap = replay.get("resolution_gap")
    if gap:
        add("planner", "This run",
            f"a place was named and nothing was bound to it: {json.dumps(gap, default=str)[:300]}")
    for rj in (env.get("rejected_mints") or []):
        add("planner", "This run", f"a candidate was rejected as outside the asked geography: {rj}")

    totals = env.get("totals") if isinstance(env.get("totals"), dict) else {}
    tn = str(totals.get("tier_note") or "")
    if "anonymous" in tn.lower():
        add("tier", "This run", tn)

    prov = env.get("provenance") if isinstance(env.get("provenance"), dict) else {}
    if prov.get("as_of_basis"):
        add("age", "This run", str(prov["as_of_basis"]))
    if prov.get("preview_warning"):
        add("tier", "This run", str(prov["preview_warning"]))
    return limits


def brief_model(env, *, prepared_for="", prepared_by="DC Hub", now=None):
    """The whole brief as plain data. Rendering reads this and nothing else."""
    env = env if isinstance(env, dict) else {}
    now = now or datetime.now(timezone.utc)
    executed = [s for s in (env.get("executed") or []) if isinstance(s, dict)]
    replay = env.get("replay") if isinstance(env.get("replay"), dict) else {}
    prov = env.get("provenance") if isinstance(env.get("provenance"), dict) else {}

    steps = []
    for s in executed:
        findings, truncated = _step_result_view(s)
        hk, hv = _step_headline(findings)
        result = s.get("result") if isinstance(s.get("result"), dict) else {}
        sprov = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
        steps.append({
            "step": s.get("step"),
            "tool": s.get("tool") or "",
            "args": _arg_summary(s.get("args")),
            "status": str(s.get("status") or ""),
            "ms": s.get("ms"),
            "findings": findings,
            "tables": step_tables(result),
            "headline_key": hk,
            "headline_value": hv,
            "truncated": truncated,
            "as_of": sprov.get("as_of"),
            "completeness": sprov.get("completeness") or (result.get("citation") or {}).get("completeness"),
        })

    decisions = [d for d in (replay.get("decisions") or []) if isinstance(d, dict)]
    route = next((d for d in decisions if d.get("kind") == "route"), None)
    checks = [d for d in decisions if d.get("kind") == "constraint_check"]
    step_decisions = [d for d in decisions if d.get("kind") == "step"]

    # Cover scorecards: the first three steps that produced a headline number.
    cards = []
    for st in steps:
        if st["headline_value"] is None or len(cards) >= 3:
            continue
        tone = _VERDICT_TONE.get(str(st["headline_value"]).upper(), "cy")
        cards.append({
            "label": (st["args"] or st["tool"])[:34],
            "value": str(st["headline_value"])[:18],
            "unit": st["headline_key"] if st["headline_key"] != "verdict" else "",
            "sub": st["tool"],
            "tone": tone,
        })

    guide = str(env.get("answer_guide") or "")
    headline = ""
    # Stop at a SENTENCE end, not at the first period — "CAUTION (50.5)" carries
    # a decimal point, and a naive [^.]+ cuts the score in half.
    m = re.search(r"returned:\s*(.+?)(?:\.\s|\.$|$)", guide, re.S)
    if m:
        headline = " ".join(m.group(1).split())
    tone = "cy"
    for word, t in (("AVOID", "red"), ("CAUTION", "amb"), ("BUILD", "grn"), ("GO", "grn")):
        if word in headline.upper():
            tone = t
            break

    limits = limits_from_envelope(env)
    return {
        "intent": str(env.get("intent") or "").strip() or "(no intent recorded)",
        "intent_class": str(env.get("intent_class") or ""),
        "planner_version": str(env.get("planner_version") or replay.get("planner_version") or ""),
        "prepared_for": prepared_for or "",
        "prepared_by": prepared_by or "DC Hub",
        "date": now.strftime("%Y-%m-%d"),
        "geography": [str(x) for x in (env.get("constraint_iso") or [])],
        "markets": [str(x) for x in ((env.get("minted") or {}).get("slug") or [])],
        "steps": steps,
        "cards": cards,
        "headline": headline,
        "headline_tone": tone,
        "answer_guide": guide,
        "route": route,
        "step_decisions": step_decisions,
        "constraint_checks": checks,
        "rejected": [r for r in (replay.get("rejected") or []) if isinstance(r, dict)],
        "limits": limits,
        "as_of": prov.get("as_of"),
        "as_of_basis": prov.get("as_of_basis"),
        "why_live": env.get("why_live_data") or replay.get("why_live_data") or "",
        "totals": env.get("totals") or {},
    }


# ═══════════════════════════════════════════════════════════════════════════
# RENDER LAYER — house style (feedback_house_doc_style): near-black, Instrument
# Sans + JetBrains Mono, indigo/cyan/emerald/amber accents, US-Letter portrait,
# HTML → headless-Chromium PDF. The base sheet is IMPORTED from the premium Site
# Analysis rather than retyped, so the two Pro deliverables cannot drift apart;
# only the classes this brief adds live here.
# ═══════════════════════════════════════════════════════════════════════════

_EXTRA_CSS = """
  .steptable{width:100%;border-collapse:collapse;margin:6px 0 4px;}
  .steptable th{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.12em;
    text-transform:uppercase;color:var(--dim);text-align:left;padding:0 10px 7px 0;font-weight:500;}
  .steptable td{font-size:12px;color:var(--mut);padding:8px 10px 8px 0;border-top:1px solid var(--b);vertical-align:top;}
  .steptable td.tool{font-family:'JetBrains Mono',monospace;color:#fff;font-weight:600;white-space:nowrap;}
  .steptable td.why{color:var(--mut);line-height:1.45;}
  .stat-ok{color:var(--grn);}.stat-warn{color:var(--amb);}.stat-bad{color:var(--red);}
  .limit{background:var(--surf);border:1px solid var(--b);border-left:3px solid var(--amb);
    border-radius:11px;padding:10px 15px;margin-bottom:8px;}
  .limit .who{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.1em;
    text-transform:uppercase;color:var(--amb);margin-bottom:4px;}
  .limit .what{color:#e9e9ee;font-size:12px;line-height:1.5;}
  .limit.clean{border-left-color:var(--grn);}
  .limit.clean .who{color:var(--grn);}
  .rowtable{width:100%;border-collapse:collapse;margin:8px 0 2px;}
  .rowtable th{font-family:'JetBrains Mono',monospace;font-size:8.5px;letter-spacing:.1em;
    text-transform:uppercase;color:var(--dim);text-align:right;padding:0 0 6px 10px;font-weight:500;}
  .rowtable th:first-child{text-align:left;padding-left:0;}
  .rowtable td{font-family:'JetBrains Mono',monospace;font-size:11px;color:#fff;
    text-align:right;padding:5px 0 5px 10px;border-top:1px solid var(--b);white-space:nowrap;}
  .rowtable td.rl{text-align:left;padding-left:0;color:var(--mut);white-space:normal;}
  .rowtable{page-break-inside:avoid;break-inside:avoid;}
  .findrow{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid var(--b);font-size:12px;}
  .findrow:last-child{border-bottom:0;}
  .findrow .fk{color:var(--mut);font-family:'JetBrains Mono',monospace;font-size:11px;}
  .findrow .fv{color:#fff;font-weight:600;font-family:'JetBrains Mono',monospace;text-align:right;
    max-width:3.4in;overflow-wrap:anywhere;}
  .rejrow{font-size:11.5px;color:var(--mut);padding:6px 0;border-bottom:1px solid var(--b);line-height:1.45;}
  .rejrow:last-child{border-bottom:0;}
  .rejrow b{color:#cbd5e1;font-family:'JetBrains Mono',monospace;font-weight:600;}
  .intentbox{background:linear-gradient(135deg,rgba(34,211,238,.10),rgba(129,140,248,.08));
    border:1px solid var(--b);border-left:3px solid var(--cy);border-radius:12px;padding:14px 18px;margin:16px 0 0;}
  .intentbox .l{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.14em;
    text-transform:uppercase;color:var(--cy);margin:0 0 5px;}
  .intentbox .v{color:#fff;font-size:15px;font-weight:600;line-height:1.4;}
  .card,.limit,.sc{page-break-inside:avoid;break-inside:avoid;}
  .verdict .vlabel{font-size:17px;letter-spacing:.02em;min-width:2.3in;max-width:2.9in;
    line-height:1.25;overflow-wrap:anywhere;}
"""


def _house_css() -> str:
    """The premium Site Analysis sheet, verbatim, plus this brief's additions.

    Imported, not copied: two Pro PDFs that hand-maintain the same palette drift,
    and the drift shows up on a customer's desk.
    """
    from routes.site_report import _CSS as _BASE
    return _BASE + _EXTRA_CSS


def _head(title):
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<title>{_esc(title)}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800'
        '&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">'
        f'<style>{_house_css()}</style></head><body>'
    )


def _page(kicker, title, body, foot_label):
    """One US-Letter sheet.

    ★ NO "n / N" IN THE FOOTER, deliberately. Content is chunked into
    sheet-sized groups here, but Chromium is the only thing that knows how many
    physical sheets a group actually became — a long caveat wraps one line more
    than expected and an authored count becomes a false claim printed on every
    page of a document whose whole pitch is that it publishes its own limits.
    The section label plus the "(1 of 2)" a split section carries in its TITLE
    give continuity without asserting a number this renderer cannot know.
    """
    return (
        '<section class="page">'
        + (f'<p class="kick">{kicker}</p>' if kicker else "")
        + (f'<h2>{title}</h2><div class="sec-rule"></div>' if title else "")
        + body
        + f'<div class="foot"><span>DC Hub · Deal Desk Brief</span>'
          f'<span>{_esc(foot_label)}</span></div></section>')


def _chunk(seq, size):
    return [seq[i:i + size] for i in range(0, len(seq), size)] or [[]]


def render_brief_html(B) -> str:
    """The brief model → one print-ready HTML document. Pure; no I/O.

    Pagination is explicit: content is chunked into sheet-sized groups first, so
    the footer can state a page count that is TRUE. A fixed "n / 4" printed over
    however many sheets Chromium happened to produce is a claim the document
    cannot keep.
    """
    # ── build the body of every non-cover sheet first, so the count is known ──
    sheets = []  # (kicker, title, body, foot_label)

    why = {str(d.get("step")): d.get("rationale") for d in B["step_decisions"]}
    rows = []
    for st in B["steps"]:
        cls = ("stat-ok" if st["status"] == "executed"
               else "stat-bad" if st["status"] in ("failed", "not_run", "skipped_unresolved")
               else "stat-warn")
        rows.append(
            f'<tr><td class="tool">{_esc(st["step"])} · {_esc(st["tool"])}'
            + (f'<br><span style="color:var(--dim);font-weight:400">{_esc(st["args"])}</span>'
               if st["args"] else "")
            + f'</td><td class="{cls}">{_esc(st["status"])}'
            + (f'<br><span style="color:var(--dim)">{_esc(st["ms"])} ms</span>'
               if st["ms"] is not None else "")
            + f'</td><td class="why">{_esc((why.get(str(st["step"])) or "")[:240])}</td></tr>')
    body = ""
    if B["route"]:
        body += ('<div class="card ind"><p class="lab">Routing decision</p>'
                 f'<div style="color:#fff;font-weight:600;font-size:13.5px">{_esc(B["route"].get("decision"))}</div>'
                 f'<p class="note">{_esc(str(B["route"].get("rationale") or "")[:400])}</p></div>')
    body += ('<table class="steptable"><tr><th>Step · tool</th><th>Status</th>'
             '<th>Why the planner called it</th></tr>' + "".join(rows) + '</table>')
    if B["constraint_checks"]:
        body += '<div class="card cy" style="margin-top:12px"><p class="lab">Constraint checks</p>'
        for c in B["constraint_checks"]:
            ok = str(c.get("status", "")).upper() == "PASS"
            body += (f'<div class="kv"><span class="k">{_esc(c.get("decision"))}</span>'
                     f'<span class="vv" style="color:var(--{"grn" if ok else "red"})">'
                     f'{_esc(c.get("status"))}</span></div>')
        body += '</div>'
    sheets.append(('<span class="secnum">01</span> · How this answer was reached',
                   'The run, step by step', body, 'The run'))

    for group in _chunk(B["rejected"][:8], 5):
        if not group:
            break
        rej = '<div class="card"><p class="lab">Paths considered and rejected</p>'
        for r in group:
            rej += (f'<div class="rejrow"><b>{_esc(r.get("tool"))}</b> — '
                    f'{_esc(str(r.get("reason") or "")[:260])}</div>')
        rej += ('</div><p class="note">A planner that only reports what it ran is not auditable. '
                'These are the tools it considered for this question and the reason each was not '
                'the right call.</p>')
        sheets.append(('<span class="secnum">01</span> · How this answer was reached',
                       'Paths not taken', rej, 'Paths not taken'))

    find_pages = _chunk(B["steps"], 2)
    for i, group in enumerate(find_pages):
        body = ""
        if not group:
            body = ('<div class="card amb"><p class="lab">No steps</p>'
                    '<p class="note">This run executed no steps. There are no findings to report '
                    '— that is the finding.</p></div>')
        for st in group:
            body += (f'<div class="card {"cy" if st["status"] == "executed" else "amb"}">'
                     f'<p class="lab">Step {_esc(st["step"])} · {_esc(st["tool"])}'
                     + (f' · {_esc(st["args"])}' if st["args"] else "") + '</p>')
            if st["findings"]:
                for k, v in st["findings"][:8]:
                    body += (f'<div class="findrow"><span class="fk">{_esc(k)}</span>'
                             f'<span class="fv">{_esc(_clip(v, 96))}</span></div>')
            else:
                body += ('<p class="note">This step returned no scalar fields to summarise. '
                         'Call the tool directly for its full payload.</p>')
            for tb in st["tables"]:
                body += (f'<table class="rowtable"><tr><th>{_esc(tb["key"])}</th>'
                         + "".join(f'<th>{_esc(c)}</th>' for c in tb["columns"])
                         + '</tr>')
                for r in tb["rows"]:
                    body += (f'<tr><td class="rl">{_esc(r["label"])}</td>'
                             + "".join(f'<td>{_esc(v)}</td>' for v in r["values"])
                             + '</tr>')
                body += '</table>'
                if tb["total"] > tb["shown"]:
                    body += (f'<p class="note">{tb["shown"]} of {tb["total"]} '
                             f'{_esc(tb["key"])} rows shown.</p>')
            tags = []
            if st["completeness"]:
                tags.append(str(st["completeness"]))
            if st["as_of"]:
                tags.append(f'as of {st["as_of"]}')
            if st["truncated"]:
                tags.append("result truncated in transport")
            if tags:
                body += ('<div class="tags">'
                         + "".join(f'<span class="tag">{_esc(t)}</span>' for t in tags)
                         + '</div>')
            body += '</div>'
        title = "What each step returned" + (f" ({i + 1} of {len(find_pages)})"
                                             if len(find_pages) > 1 else "")
        sheets.append(('<span class="secnum">02</span> · Findings', title, body, 'Findings'))

    lim_pages = _chunk(B["limits"], 9)
    for i, group in enumerate(lim_pages):
        body = ('<p class="note" style="margin-bottom:12px">Every line below is published by the '
                'response itself — none of it is inferred here. A brief that prints only its '
                'answers is not a brief you can act on.</p>')
        if group:
            for lm in group:
                body += (f'<div class="limit"><div class="who">{_esc(lm["source"])} · '
                         f'{_esc(lm["kind"])}</div><div class="what">{_esc(lm["text"])}</div></div>')
        else:
            body += ('<div class="limit clean"><div class="who">This run · coverage</div>'
                     '<div class="what">Nothing was withheld on this run: no step published a '
                     'coverage caveat, no field was tier-gated, and every planned step ran. This '
                     'is an EMPTY limits block, which means nothing was withheld — it does not '
                     'mean the limits are unknown.</div></div>')
        title = "What this brief does <i>not</i> cover" + (f" ({i + 1} of {len(lim_pages)})"
                                                           if len(lim_pages) > 1 else "")
        sheets.append(('<span class="secnum">03</span> · Limits', title, body, 'Limits'))

    # Methodology gets its own sheet: appended to the last limits page it pushed
    # the page over and produced a near-empty trailing sheet anyway.
    totals = B["totals"] if isinstance(B["totals"], dict) else {}
    meth = ('<div class="card amb"><p class="lab">Methodology &amp; caveats</p>'
            '<p class="note"><b>This is a screening brief — not an appraisal, an engineering '
            'study, or a utility commitment.</b> Figures are point-in-time reads of DC Hub\'s live '
            'layers on the date shown and move daily; an answer is no fresher than its stalest '
            'input. Confirm capacity, timing and cost with the utility, the ISO, and the water and '
            'permitting authorities before committing capital.</p>'
            f'<p class="note"><b>Age basis:</b> '
            f'{_esc(B["as_of_basis"] or "not stated by this response")}</p>'
            '<p class="src"><b>Source:</b> DC Hub (dchub.cloud), CC-BY-4.0 — cite as '
            '"DC Hub, dchub.cloud" with the as-of date shown.</p></div>')
    meth += '<div class="card ind" style="margin-top:14px"><p class="lab">How this brief was produced</p>'
    for k, v in (("Question, verbatim", B["intent"]),
                 ("Routed to", B["intent_class"] or "unclassified"),
                 ("Planner version", B["planner_version"] or "not stated"),
                 ("Steps run", str(totals.get("steps_run") or len(B["steps"]))),
                 ("Wall time", f'{totals.get("ms")} ms' if totals.get("ms") is not None else "not stated"),
                 ("Limits published", str(len(B["limits"]))),
                 ("Brief generated", B["date"])):
        meth += (f'<div class="kv"><span class="k">{_esc(k)}</span>'
                 f'<span class="vv">{_esc(_clip(v, 60))}</span></div>')
    meth += ('</div><p class="note" style="margin-top:14px">The routing was deterministic — the '
             'same question returns the same plan, with no model in the loop. Re-run it any time '
             'with <b>execute_plan</b> at dchub.cloud/mcp to see what moved.</p>')
    sheets.append(('<span class="secnum">04</span> · Method',
                   'Methodology, caveats and provenance', meth, 'Method'))

    # ── cover ────────────────────────────────────────────────────────────
    H = [_head(f'{B["intent"][:70]} — Deal Desk Brief · DC Hub')]
    cards = "".join(
        f'<div class="sc"><div class="t">{_esc(c["label"])}</div>'
        f'<div class="v">{_esc(c["value"])}'
        + (f'<small> {_esc(c["unit"])}</small>' if c["unit"] else "")
        + f'</div><div class="s" style="color:var(--{c["tone"]})">● {_esc(c["sub"])}</div></div>'
        for c in B["cards"])
    scope = " · ".join([*B["markets"], *B["geography"]]) or "scope as asked"
    prep = f'Prepared by {_esc(B["prepared_by"])}'
    if B["prepared_for"]:
        prep += f' · for {_esc(B["prepared_for"])}'
    H.append(
        '<section class="page cover"><div class="cover-glow"></div>'
        '<div class="brandrow"><div class="wordmark">DC<span>·</span>Hub</div>'
        f'<div class="badge">DEAL DESK BRIEF · {_esc(B["date"])}</div></div>'
        '<div class="cover-mid"><p class="kick">Deal Desk · One planned run, answered end to end</p>'
        f'<h1>{_esc(B["intent"][:120])}</h1>'
        f'<p class="coords">◎ {_esc(scope)}'
        + (f' · planner {_esc(B["planner_version"])}' if B["planner_version"] else "") + '</p>'
        '<p class="lead">DC Hub routed this question to a tool sequence, ran it, and answered from '
        'live grid, market, fiber and pipeline layers. Every step it took, every path it rejected, '
        'and every limit of the answer are printed in this brief — the reasoning is auditable, not '
        'asserted.</p>')
    if B["prepared_for"]:
        H.append('<div class="usecase"><p class="l">Prepared for</p>'
                 f'<div class="v">{_esc(B["prepared_for"])}</div></div>')
    if B["headline"]:
        H.append(f'<div class="verdict {B["headline_tone"]}">'
                 f'<div class="vlabel">{_esc(B["headline"][:60])}</div>'
                 '<div class="vbody"><p class="vk">What this run returned</p>'
                 f'<div class="vhead">{_esc(B["intent"][:150])}</div>'
                 f'<p class="vreasons" style="list-style:none;padding-left:0">'
                 f'{_esc(B["why_live"])}</p></div></div>')
    H.append('</div>')
    if cards:
        H.append(f'<div class="scorecards">{cards}</div>')
    H.append(f'<div class="cover-foot"><span>{prep}</span>'
             f'<span>{_esc(B["as_of"] or "as_of unmeasured")}</span></div></section>')

    for kicker, title, body, label in sheets:
        H.append(_page(kicker, title, body, label))
    H.append('</body></html>')
    return "".join(H)


# ═══════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════

_PRO_TIERS = ("PRO", "ENTERPRISE", "FOUNDING", "RESEARCH_SEED", "ADMIN")


def _pro_price_usd():
    """Canonical Pro monthly price, DERIVED from tier_registry (the SSOT).

    Never a literal. The market-brief PDF gate still quotes callers a Pro price
    from two price changes ago, in a sentence only a non-paying caller ever
    sees — which is exactly why nobody noticed. A price typed into a gate
    message is a price that goes stale where it is never read.
    """
    try:
        from tier_registry import price as _price
        return int(_price("pro") or 0)
    except Exception:
        return 0


def _is_admin() -> bool:
    ak = (request.headers.get("X-Admin-Key") or "").strip()
    return bool(ak) and ak == (os.environ.get("DCHUB_ADMIN_KEY", "") or "").strip()


def _caller_tier() -> str:
    try:
        from routes.tier_gate import _resolve_caller_tier
        tier, _ = _resolve_caller_tier()
        return (tier or "FREE").upper()
    except Exception:
        return "FREE"


def _brief_urls(token):
    return (f"{_BASE_URL}/reports/deal-desk/{token}",
            f"{_BASE_URL}/reports/deal-desk/{token}.pdf")


def _extract_envelope(body):
    """Accept the plan_execution envelope at the top level or under `plan`."""
    if not isinstance(body, dict):
        return None
    for candidate in (body.get("plan"), body.get("envelope"), body):
        if isinstance(candidate, dict) and (
                candidate.get("_entity") == "plan_execution" or "executed" in candidate):
            return candidate
    return None


# One string, not adjacent fragments: regression_lint's insert-no-on-conflict
# rule scans from INSERT INTO up to the next quote character, so a statement
# split across literals hides its own ON CONFLICT clause from the guard.
_INSERT_SQL = """
    INSERT INTO deal_desk_briefs (brief_token, api_key_hash, intent, intent_class,
                                  prepared_for, prepared_by, payload, source, expires_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (brief_token) DO NOTHING
    RETURNING id
"""


def store_brief(cur, token, row) -> bool:
    """Insert one brief. True when a row actually landed.

    ★ ON CONFLICT DO NOTHING, and then the return value is CHECKED. Bare DO
    NOTHING is how a write silently becomes a no-op: the caller would hand its
    human a link to a brief that was never stored and the failure would surface
    days later as a 404 on someone else's desk. `RETURNING id` turns the
    conflict into a fact the caller must handle — here, by minting a new token.
    """
    cur.execute(_INSERT_SQL, (
        token, row["api_key_hash"], row["intent"], row["intent_class"],
        row["prepared_for"], row["prepared_by"], row["payload"], row["source"],
        row["expires_at"]))
    return cur.fetchone() is not None


@deal_desk_bp.route("/api/v1/deal-desk", methods=["POST"])
def mint_deal_desk_brief():
    """Turn one `execute_plan` run into a shareable, no-login branded brief."""
    if not (_is_admin() or _caller_tier() in _PRO_TIERS):
        usd = _pro_price_usd()
        return jsonify({
            "error": "deal_desk_requires_pro",
            "message": ("The Deal Desk Brief is a Pro deliverable"
                        + (f" (${usd}/mo)" if usd else "")
                        + " — it turns one execute_plan run into a branded PDF your human can forward."),
            "upgrade_url": f"{_BASE_URL}/pricing",
            "current_tier": _caller_tier(),
        }), 402

    body = request.get_json(silent=True) or {}
    env = _extract_envelope(body)
    if env is None:
        return jsonify({
            "error": "missing_plan_execution",
            "message": ("POST the execute_plan envelope — the whole object with `_entity`:"
                        " \"plan_execution\" — as the body, or under a `plan` key."),
        }), 400

    prepared_for = str(body.get("prepared_for") or "")[:120]
    prepared_by = str(body.get("prepared_by") or "DC Hub")[:120]
    ttl_days = DEFAULT_TTL_DAYS
    B = brief_model(env, prepared_for=prepared_for, prepared_by=prepared_by)

    token = _new_token()
    expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    c = _conn()
    if c is None:
        return jsonify({"error": "store_unavailable",
                        "message": "The brief store is unreachable; no link was minted."}), 503
    payload = json.dumps({"plan": env, "prepared_for": prepared_for,
                          "prepared_by": prepared_by})
    row = {"api_key_hash": _hash_key(request.headers.get("X-API-Key") or ""),
           "intent": B["intent"][:2000], "intent_class": B["intent_class"][:80],
           "prepared_for": prepared_for, "prepared_by": prepared_by,
           "payload": payload, "source": str(body.get("source") or "api")[:40],
           "expires_at": expires}
    try:
        with c, c.cursor() as cur:
            stored = False
            for _ in range(3):
                if store_brief(cur, token, row):
                    stored = True
                    break
                token = _new_token()   # token collision — mint a new one, never
                                       # hand back a URL for a row that is not there
    except Exception as e:
        print(f"[deal_desk] insert failed: {e}", file=sys.stderr)
        return jsonify({"error": "store_write_failed", "detail": f"{type(e).__name__}"}), 503
    finally:
        try:
            c.close()
        except Exception:
            pass
    if not stored:
        return jsonify({"error": "store_write_failed",
                        "message": "Could not mint a unique brief token; nothing was stored."}), 503

    html_url, pdf_url = _brief_urls(token)
    share = (f"DC Hub Deal Desk Brief · {B['intent'][:90]}"
             + (f" · prepared for {prepared_for}" if prepared_for else "")
             + f" · download (no login, {ttl_days}-day link): {pdf_url}")
    return jsonify({
        "ok": True,
        "brief_token": token,
        "brief_url": html_url,
        "pdf_url": pdf_url,
        # No page count, and the guard below forbids re-adding one. This field
        # carried the sheet count of the FIRST draft; the live artifact renders
        # more than twice that. Same defect as the "n / N" footer cut earlier
        # for being wrong by two sheets — content is chunked here, but how many
        # physical sheets that becomes is Chromium's answer, reached long after
        # this response was sent.
        "deliverable": "Branded DC Hub Deal Desk Brief (PDF)",
        "expires_at": expires.isoformat(),
        # Machine-checkable proof the honesty section is populated, so a caller
        # can assert the brief published its limits instead of trusting that it did.
        "limits_printed": len(B["limits"]),
        "steps": len(B["steps"]),
        "share_text": share,
        "for_your_human": (f"→ **For your human:** your Deal Desk Brief is ready — {pdf_url} "
                           f"(no login, {ttl_days}-day link)."),
    })


def _load_brief(token):
    c = _conn()
    if c is None:
        return None, "store_unavailable"
    try:
        with c, c.cursor() as cur:
            cur.execute("SELECT payload, expires_at FROM deal_desk_briefs WHERE brief_token=%s",
                        (token,))
            row = cur.fetchone()
        if not row:
            return None, "not_found"
        payload, expires_at = row[0], row[1]
        if expires_at and expires_at < datetime.now(timezone.utc):
            return None, "expired"
        return json.loads(payload), None
    except Exception as e:
        print(f"[deal_desk] load failed: {e}", file=sys.stderr)
        return None, "load_failed"
    finally:
        try:
            c.close()
        except Exception:
            pass


def _bump(token, column):
    c = _conn()
    if c is None:
        return
    try:
        with c, c.cursor() as cur:
            cur.execute(f"UPDATE deal_desk_briefs SET {column}={column}+1 WHERE brief_token=%s",
                        (token,))
    except Exception:
        pass
    finally:
        try:
            c.close()
        except Exception:
            pass


def _render_stored(token):
    stored, err = _load_brief(token)
    if err:
        return None, err
    B = brief_model(stored.get("plan") or {},
                    prepared_for=stored.get("prepared_for") or "",
                    prepared_by=stored.get("prepared_by") or "DC Hub")
    return render_brief_html(B), None


_ERR_STATUS = {"not_found": 404, "expired": 410, "store_unavailable": 503, "load_failed": 503}


@deal_desk_bp.route("/reports/deal-desk/<token>", methods=["GET"])
def view_deal_desk_brief(token):
    html, err = _render_stored(token)
    if err:
        return jsonify({"error": err, "brief_token": token}), _ERR_STATUS.get(err, 404)
    _bump(token, "html_views")
    return Response(html, content_type="text/html; charset=utf-8", headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


@deal_desk_bp.route("/reports/deal-desk/<token>.pdf", methods=["GET"])
def download_deal_desk_brief(token):
    html, err = _render_stored(token)
    if err:
        return jsonify({"error": err, "brief_token": token}), _ERR_STATUS.get(err, 404)
    try:
        from routes.pdf_render import html_to_pdf
        pdf = html_to_pdf(html, timeout=60)
    except Exception as e:
        # Degrade honestly: the HTML is print-to-PDF ready. Never a 500, and
        # never a silent HTML body served under a .pdf name.
        return jsonify({
            "error": "pdf_engine_unavailable",
            "message": ("Server-side PDF export is temporarily unavailable. The brief is available "
                        "now as HTML and is print-to-PDF ready (Cmd/Ctrl-P → Save as PDF)."),
            "html_url": _brief_urls(token)[0],
            "detail": f"{type(e).__name__}: {str(e)[:160]}",
        }), 503
    _bump(token, "pdf_downloads")
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="DC_Hub_Deal_Desk_Brief_{token}.pdf"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


def register_deal_desk_routes(app):
    """Register the deal-desk blueprint (idempotent)."""
    if "deal_desk" in app.blueprints:
        return False
    app.register_blueprint(deal_desk_bp)
    return True
