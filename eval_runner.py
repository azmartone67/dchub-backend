"""
DC Hub MCP - Golden Prompt Evaluation Runner

Runs every prompt in dchub-mcp-golden-eval.jsonl against Claude with the
DC Hub MCP server connected, records which tools Claude called, and scores
each prompt PASS / PARTIAL / FAIL.

Requires these environment variables (set them in Replit Secrets):
  ANTHROPIC_API_KEY  - your Anthropic API key
  DCHUB_MCP_URL      - your MCP endpoint, e.g. https://dchub.cloud/mcp
  DCHUB_MCP_TOKEN    - (optional) bearer token if your server requires auth
  EVAL_MODEL         - (optional) model string, default: claude-opus-4-5

Expects these files in the same directory:
  dchub-mcp-golden-eval.jsonl

Run:
  pip install anthropic
  python eval_runner.py
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("Install the Anthropic SDK first:  pip install anthropic", file=sys.stderr)
    sys.exit(1)


MODEL = os.getenv("EVAL_MODEL", "claude-opus-4-5")
HERE = Path(__file__).parent
EVAL_FILE = HERE / "dchub-mcp-golden-eval.jsonl"
RESULTS_DIR = HERE / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# MCP beta header. If Anthropic changes this, update the single string here.
MCP_BETA = "mcp-client-2025-04-04"


def load_prompts():
    if not EVAL_FILE.exists():
        print(f"Eval file not found: {EVAL_FILE}", file=sys.stderr)
        sys.exit(1)
    with open(EVAL_FILE) as f:
        return [json.loads(line) for line in f if line.strip()]


def build_mcp_server_config():
    url = os.environ.get("DCHUB_MCP_URL")
    if not url:
        print("Set DCHUB_MCP_URL (your MCP endpoint) in env.", file=sys.stderr)
        sys.exit(1)
    cfg = {"type": "url", "url": url, "name": "dchub"}
    token = os.getenv("DCHUB_MCP_TOKEN")
    if token:
        cfg["authorization_token"] = token
    return cfg


def run_prompt(client, prompt_text, mcp_server):
    """One API call with DC Hub MCP connected. Returns the raw response."""
    return client.beta.messages.create(
        model=MODEL,
        max_tokens=4096,
        mcp_servers=[mcp_server],
        betas=[MCP_BETA],
        messages=[{"role": "user", "content": prompt_text}],
    )


def extract_tool_calls(response):
    """Return the list of DC Hub tool names Claude invoked in this response."""
    calls = []
    for block in getattr(response, "content", []) or []:
        t = getattr(block, "type", None)
        if t in ("mcp_tool_use", "tool_use"):
            name = getattr(block, "name", "") or ""
            # Strip the MCP server prefix so we get the bare tool name.
            if "__" in name:
                name = name.split("__")[-1]
            calls.append(name)
    return calls


def score_entry(entry, actual_calls):
    expected_primary = entry["expected_primary"]
    expected_secondary = entry.get("expected_secondary") or []
    pass_criterion = entry.get("pass_criterion")

    # Control prompts: correctness = no DC Hub call.
    if expected_primary == "NONE":
        if not actual_calls:
            return "PASS", "no tool called (as expected)"
        return "FAIL", f"false positive: {actual_calls}"

    # Multi-tool prompts use an explicit pass_criterion string.
    if pass_criterion:
        distinct = list(dict.fromkeys(actual_calls))  # preserve order, dedupe
        n = len(distinct)
        if ">= 3" in pass_criterion:
            needed = 3
        elif ">= 2" in pass_criterion:
            needed = 2
        else:
            needed = 1
        if n >= needed:
            return "PASS", f"chained {n} tool(s): {distinct}"
        if n >= 1:
            return "PARTIAL", f"called {distinct}, needed >= {needed}"
        return "FAIL", "no tool called"

    # Single-tool prompts.
    if expected_primary in actual_calls:
        return "PASS", f"primary tool {expected_primary} called"
    if any(t in actual_calls for t in expected_secondary):
        hit = [t for t in actual_calls if t in expected_secondary]
        return "PARTIAL", f"secondary instead of primary: {hit}"
    if actual_calls:
        return "PARTIAL", f"wrong tool(s): {actual_calls}"
    return "FAIL", "no tool called"


def main():
    client = anthropic.Anthropic()
    mcp_server = build_mcp_server_config()
    prompts = load_prompts()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results_path = RESULTS_DIR / f"eval-{stamp}.jsonl"
    results = []

    print(f"Running {len(prompts)} prompts against {MODEL} -> {mcp_server['url']}")
    print(f"Results will be written to {results_path}\n")

    for entry in prompts:
        pid = entry["id"]
        prompt_text = entry["prompt"]
        print(f"[{pid}] {prompt_text[:90]}{'...' if len(prompt_text) > 90 else ''}")
        actual_calls = []
        try:
            response = run_prompt(client, prompt_text, mcp_server)
            actual_calls = extract_tool_calls(response)
            verdict, note = score_entry(entry, actual_calls)
        except anthropic.APIStatusError as e:
            verdict, note = "ERROR", f"API {e.status_code}: {e.message}"
        except Exception as e:
            verdict, note = "ERROR", f"{type(e).__name__}: {e}"

        row = {
            "id": pid,
            "persona": entry["persona"],
            "difficulty": entry.get("difficulty", ""),
            "prompt": prompt_text,
            "expected_primary": entry["expected_primary"],
            "expected_secondary": entry.get("expected_secondary", []),
            "actual_calls": actual_calls,
            "verdict": verdict,
            "note": note,
        }
        results.append(row)
        print(f"    -> {verdict}: {note}")

        # Append each row as we go so a mid-run crash doesn't lose progress.
        with open(results_path, "a") as f:
            f.write(json.dumps(row) + "\n")

        # Light rate limiting - tune to your tier.
        time.sleep(0.5)

    # ---------- Summary ----------
    main_set = [r for r in results if r["persona"] != "control_negative"]
    controls = [r for r in results if r["persona"] == "control_negative"]

    def pct(n, d):
        return f"{100 * n / d:.0f}%" if d else "0%"

    n_pass = sum(1 for r in main_set if r["verdict"] == "PASS")
    n_partial = sum(1 for r in main_set if r["verdict"] == "PARTIAL")
    n_fail = sum(1 for r in main_set if r["verdict"] == "FAIL")
    n_err = sum(1 for r in main_set if r["verdict"] == "ERROR")
    n_fp = sum(1 for r in controls if r["verdict"] == "FAIL")

    print("\n" + "=" * 60)
    print(f"MODEL: {MODEL}")
    print(f"Main set ({len(main_set)} prompts):")
    print(f"  PASS    {n_pass:>3}  ({pct(n_pass, len(main_set))})")
    print(f"  PARTIAL {n_partial:>3}  ({pct(n_partial, len(main_set))})")
    print(f"  FAIL    {n_fail:>3}  ({pct(n_fail, len(main_set))})")
    print(f"  ERROR   {n_err:>3}")
    print(f"Controls ({len(controls)} prompts):")
    print(f"  False positives: {n_fp} ({pct(n_fp, len(controls))})")

    # Per-tool trigger breakdown on the single-tool subset.
    single_tool = [r for r in main_set if r["expected_primary"] not in ("NONE",)
                   and not any(c in r["prompt"] for c in ["Shortlist", "operating cost", "happening in the Texas", "Sustainability report:", "Should I buy"])]
    by_tool = {}
    for r in single_tool:
        t = r["expected_primary"]
        by_tool.setdefault(t, {"expected": 0, "hit": 0})
        by_tool[t]["expected"] += 1
        if r["verdict"] == "PASS":
            by_tool[t]["hit"] += 1

    print("\nPer-tool trigger rate (single-tool prompts only):")
    for tool, stats in sorted(by_tool.items(), key=lambda x: (x[1]["hit"] / max(1, x[1]["expected"]))):
        e, h = stats["expected"], stats["hit"]
        print(f"  {tool:<32} {h}/{e}  ({pct(h, e)})")

    print(f"\nResults written to {results_path}\n")


if __name__ == "__main__":
    main()
