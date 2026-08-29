"""tests/test_sponsor_ops_and_accrual.py — the report must be PRODUCIBLE, and
its crawl table must be FED (2026-08-28).

TWO FAILURES THIS FENCES, both live before this change:

  1. #3285 built routes/sponsor_report.py — monthly_report() and render_text() —
     and nothing called either one. No route, no command, no job. /advertise
     commits publicly that "the first invoice goes out after the first monthly
     report exists", so a report nothing can invoke IS the revenue gate.
  2. crawls_from_snapshots() reads sponsor_crawl_daily, and snapshot_crawls() —
     its only writer — was never scheduled. Cloudflare REFUSES request-level
     analytics older than 8 days for this zone (measured: 30d and 14d refused,
     8d is the ceiling), so an unscheduled accrual is not "late". It destroys a
     day of coverage, permanently, every day it does not run.

The brand-mention detector this report prints is fenced separately, by
tests/test_sponsor_brand_mentions.py.

★ Structural assertions read AST STRING CONSTANTS, never file text: a comment
  satisfies grep, and this file's own docstring names the paths it checks. Every
  AST walk asserts it found something first — an empty walk passes vacuously.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPS = ROOT / "routes" / "sponsor_ops.py"
MAIN = ROOT / "main.py"
WORKFLOW = ROOT / ".github" / "workflows" / "sponsor-crawl-snapshot-daily.yml"


def _string_constants(path):
    tree = ast.parse(path.read_text())
    out = [n.value for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert out, f"no string constants parsed from {path} — vacuous assertion"
    return out


# ── the report can be produced at all ────────────────────────────────
def test_the_report_and_its_mention_scanner_both_import():
    """routes/sponsor_report.py imports routes.sponsor_mentions.brand_mentions.
    While that module did not exist the import raised on every report and the
    "YOUR BRAND IN AI ANSWERS" section rendered UNAVAILABLE — under the heading
    /advertise sells as the difference between $3,000 and $5,000."""
    from routes.sponsor_report import monthly_report, render_text  # noqa: F401
    from routes.sponsor_mentions import brand_mentions
    assert callable(brand_mentions)


def test_admin_routes_exist_as_real_route_strings():
    consts = _string_constants(OPS)
    for path in ("/api/v1/admin/sponsorships/<int:sid>/report",
                 "/api/v1/admin/sponsor-crawl/snapshot",
                 "/api/v1/admin/sponsor-crawl/coverage"):
        assert path in consts, f"{path} is not a route string in sponsor_ops.py"


def test_every_route_is_admin_gated():
    """An advertiser's delivery figures and our own crawl table are private."""
    tree = ast.parse(OPS.read_text())
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name in
           ("sponsorship_report", "sponsor_crawl_snapshot",
            "sponsor_crawl_coverage")]
    assert len(fns) == 3, [f.name for f in fns]
    for fn in fns:
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_admin_ok"]
        assert calls, f"{fn.name} does not check _admin_ok"


def test_the_blueprint_is_registered():
    """A blueprint nobody registers is the same as no route at all."""
    tree = ast.parse(MAIN.read_text())
    modules = [n.module for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) and n.module]
    assert modules, "no imports parsed from main.py — vacuous assertion"
    assert "routes.sponsor_ops" in modules
    registered = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "register_blueprint"
                  and any(isinstance(a, ast.Name) and a.id == "sponsor_ops_bp"
                          for a in n.args)]
    assert registered, "sponsor_ops_bp is imported but never registered"


# ── the accrual is actually scheduled ────────────────────────────────
def test_daily_snapshot_workflow_exists_and_is_scheduled():
    """★★★ THE ONE THAT MATTERS MOST. Cloudflare refuses request-level
    analytics older than 8 days, so an unscheduled accrual does not merely
    delay the crawl table — it destroys a day of it, permanently, every day. A
    function with no scheduled caller is not "shipped"."""
    assert WORKFLOW.exists(), "no daily workflow — coverage is lost one day per day"
    import yaml
    wf = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML parses the unquoted key `on:` as the boolean True.
    triggers = wf.get("on") or wf.get(True) or {}
    assert "schedule" in triggers, "workflow exists but nothing fires it"
    crons = [s.get("cron") for s in triggers["schedule"]]
    assert any(c and len(c.split()) == 5 for c in crons), crons


def test_workflow_calls_the_snapshot_endpoint_on_the_railway_origin():
    """Through the CF worker this would 503 at 15s (ROUTE_TIMEOUTS DEFAULT)
    while the origin kept working — a green-looking job that lost the day."""
    body = WORKFLOW.read_text()
    assert "/api/v1/admin/sponsor-crawl/snapshot" in body
    assert "railway.app" in body
    assert "dchub.cloud/api/v1/admin/sponsor-crawl" not in body


def test_workflow_fails_loudly_when_no_day_was_written():
    """A snapshot job that exits 0 having written nothing is the exact shape of
    a silent green: the report still renders, one day shorter, forever.

    ★ This assertion started life as `"days_written" in body` and `"exit 1" in
    body` and was VACUOUS — mutation testing deleted the entire zero-days branch
    and both strings survived elsewhere in the script. It now parses the run
    script, finds the variable the job reads days_written into, and requires a
    branch on that variable being zero whose body exits non-zero."""
    import re

    import yaml
    wf = yaml.safe_load(WORKFLOW.read_text())
    steps = wf["jobs"]["snapshot"]["steps"]
    script = "\n".join(s.get("run", "") for s in steps)
    assert script.strip(), "no run script parsed from the workflow — vacuous"
    m = re.search(r'(\w+)=\$\(echo "\$\{RESPONSE\}"[^\n]*days_written', script)
    assert m, "the job never reads days_written out of the response"
    var = m.group(1)
    guard = re.search(r'if \[ "\$\{' + var + r'\}" = "0" \][^\n]*\n(.*?)\n\s*fi',
                      script, re.S)
    assert guard, f"nothing branches on {var} == 0 — a zero-day run would pass"
    assert "exit 1" in guard.group(1), \
        "the zero-days branch does not fail the job"
