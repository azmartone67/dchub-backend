"""Platform attribution — the artifact for a licence conversation (2026-08-03).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

The paid base is 37 developer keys and 3 enterprise seats against ~1,236 tool
calls a day. A $49 key is the wrong instrument for a platform answering user
questions at volume — no agent holds a credit card. This artifact is what a
licence conversation is built on, which means every number in it will be
re-derived by someone who is not on our side.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── classification: the inflation this exists to avoid ────────────────

def test_tooling_is_never_an_assistant():
    """curl and postman are developer tooling, not a platform answering user
    questions. Folding them in inflates exactly the number a counterparty
    checks first."""
    from routes.platform_attribution import classify_platform
    for name in ("curl", "postman", "insomnia", "mcp-inspector", "node-script"):
        assert classify_platform(name) == "tooling", name
    for name in ("meta-ai", "chatgpt", "claude", "perplexity"):
        assert classify_platform(name) == "assistant", name


def test_an_unclassified_agent_is_unknown_not_assistant():
    """★A user-agent we have not classified is not evidence of a platform
    integration. A total that quietly absorbs it falls apart under scrutiny."""
    from routes.platform_attribution import classify_platform
    assert classify_platform("some-new-crawler-9000") == "unknown"
    assert classify_platform("") == "unknown"
    assert classify_platform(None) == "unknown"


def test_our_own_traffic_is_never_demand():
    from routes.platform_attribution import classify_platform
    assert classify_platform("internal-dchub") == "internal"


def test_there_is_no_grand_total():
    """Summing assistants, tooling and untagged into one headline is the
    inflation this artifact exists to avoid — totals are per-KIND only."""
    src = _src("routes", "platform_attribution.py")
    assert '"by_kind"' in src
    assert '"total_calls"' not in src and '"grand_total"' not in src


# ── the numbers ───────────────────────────────────────────────────────

class _Cur:
    def __init__(self, rows, has_view=True):
        self._rows, self._has, self._mode = rows, has_view, None

    def execute(self, sql, args=None):
        self._mode = "col" if "information_schema" in sql else "rows"

    def fetchone(self):
        return (1 if self._has else 0,) if self._mode == "col" else None

    def fetchall(self):
        return self._rows


def _row(platform, calls=100, agents=5, tools=4):
    return (platform, calls, agents, tools, "2026-07-04", "2026-08-02", 21)


def test_rows_are_classified_and_totalled_by_kind():
    from routes.platform_attribution import platform_rows
    out = platform_rows(_Cur([_row("meta-ai", 5000), _row("curl", 900)]), days=30)
    assert out["ok"] is True
    kinds = {p["platform"]: p["kind"] for p in out["platforms"]}
    assert kinds == {"meta-ai": "assistant", "curl": "tooling"}
    assert out["by_kind"]["assistant"]["calls"] == 5000
    assert out["by_kind"]["tooling"]["calls"] == 900


def test_agents_are_reported_as_a_floor_and_labelled_as_one():
    """★Identity hashes the first X-Forwarded-For hop, so a platform's users
    behind shared egress collapse into one agent. Claiming it as a user count
    would be an overstatement we cannot defend."""
    from routes.platform_attribution import platform_rows
    out = platform_rows(_Cur([_row("meta-ai")]), days=30)
    assert "agents_floor" in out["platforms"][0]
    assert "agents" not in out["platforms"][0]
    assert "FLOOR" in out["methodology"]["agents_floor"]


def test_no_fallback_to_raw_ip_when_the_canon_is_missing():
    """★A number quoted to a counterparty comes from the canonical grain or it
    does not exist. A second-best answer that looks canonical is how every
    surface in this codebase drifted."""
    from routes.platform_attribution import platform_rows
    out = platform_rows(_Cur([], has_view=False), days=30)
    assert out["ok"] is False
    assert "does NOT fall back" in out["error"]


def test_unmeasurable_is_not_zero():
    from routes.platform_attribution import platform_rows

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("no db")

        def fetchone(self):
            raise RuntimeError("no db")

        def fetchall(self):
            raise RuntimeError("no db")

    out = platform_rows(_Boom(), days=30)
    assert out["ok"] is False and "UNMEASURED" in out["error"]


# ── the statement handed to the counterparty ──────────────────────────

def test_the_statement_names_the_floor_and_the_exclusions():
    """An artifact that overstates once is never trusted again."""
    from routes.platform_attribution import platform_rows, statement_for
    out = platform_rows(_Cur([_row("meta-ai", 5000, 12, 9)]), days=30)
    s = statement_for("meta-ai", out["platforms"][0], 30)
    assert "5,000" in s
    assert "FLOOR" in s and "not a user count" in s
    assert "Cloudflare POP" in s
    assert "exact" in s.lower()


def test_no_traffic_says_so_plainly():
    from routes.platform_attribution import statement_for
    s = statement_for("meta-ai", None, 30)
    assert "No attributable" in s


def test_the_module_writes_nothing():
    src = _src("routes", "platform_attribution.py")
    body = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE"):
        assert verb not in body.upper(), f"attribution should be read-only: {verb}"


def test_blueprint_is_registered_in_main():
    src = _src("main.py")
    assert "from routes.platform_attribution import platform_attribution_bp" in src


# ── phase 4: the dead gate stays dead until someone decides ───────────

def test_the_unwired_tier_config_says_so_in_its_first_lines():
    """★Its header states the problem the business still has, written in March.
    Read as live code it says that problem is handled. It is not — and that
    reading is traceable to five months of 'we already gate that'."""
    src = _src("mcp_tier_config.py")
    head = src[:1600]
    assert "NOT WIRED" in head
    assert "tier_registry.py" in head, "the header must name the LIVE gate"


def test_the_dead_gate_is_still_unreachable_from_boot():
    """A pin, not a preference: if someone wires it, this fails and they have
    to do it deliberately rather than by importing a patch script."""
    src = _src("main.py")
    assert "mcp_qa_fixes_v7" not in src
    assert "crawler_scheduler_patch" not in src
    assert "gate_response" not in src
