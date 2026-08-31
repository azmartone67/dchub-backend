"""Agent-facing surfaces must not sell the withdrawn DCGI as a live score.

★2026-08-22 sweep of what an arriving agent reads. The DCGI composite and the
gas-to-grid $/MWh were withdrawn 2026-08-08 (#2385) and the MCP tool
descriptions say so — but three backend surfaces still taught the old door:

  /api/v1/agent/cookbook   two recipes told agents to call get_gas_index for a
                           "DCGI 0-100 score" and carried a sample answer
                           "DCGI Texas: 91/100 — GAS-ADVANTAGED … ~$28/MWh"
                           that no tool can produce (11 DCGI mentions, 0
                           "withdrawn").
  /AGENTS.md               skill 7 "gas_intelligence — DCGI per-state
                           suitability (0–100) with a … verdict".
  /api/v1/openapi.json     the /.well-known/mcp.json description said
                           "29 tools" (live: 82).

Plus /api/v1/agent — the most guessable path — 404'd while every sibling 200'd.

NO NETWORK, NO DB. routes.agent_concierge is imported (the existing matching
tests do the same); main.py is read as text, never imported.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

_SCORE_CLAIM = re.compile(r"DCGI[^\n]{0,40}\d{1,3}\s*/\s*100|GAS-ADVANTAGED")


def _recipes():
    import routes.agent_concierge as ac
    return ac._COOKBOOK


class TestCookbook:
    def test_no_recipe_routes_to_the_withdrawn_get_gas_index(self):
        bad = [(r["id"], t["tool"]) for r in _recipes() for t in r.get("tools", [])
               if t.get("tool") == "get_gas_index"]
        assert bad == [], f"recipes still send agents to the withdrawn score: {bad}"

    def test_no_sample_answer_quotes_a_dcgi_score_or_verdict(self):
        bad = [r["id"] for r in _recipes()
               if _SCORE_CLAIM.search(r.get("sample_answer", ""))]
        assert bad == [], f"sample answers still quote a DCGI score/verdict: {bad}"

    def test_every_dcgi_mention_in_the_cookbook_says_withdrawn(self):
        # The word is allowed — an agent asking for the DCGI must land on the
        # honest answer — but never without the withdrawal next to it.
        for r in _recipes():
            blob = " ".join([r.get("problem", ""), r.get("sample_answer", ""),
                             r.get("citation", "")]
                            + [t.get("why", "") for t in r.get("tools", [])])
            if "DCGI" in blob:
                assert "withdraw" in blob.lower(), \
                    f"{r['id']} names the DCGI without saying it was withdrawn"

    def test_the_gas_recipes_still_exist_and_still_catch_dcgi_questions(self):
        # Keep the door: a question about the gas index must still match a
        # recipe (and get the honest answer) rather than fall through.
        ids = {r["id"]: r for r in _recipes()}
        assert "gas-btm-screening" in ids and "gas-vs-grid-economics" in ids
        assert "dcgi" in ids["gas-btm-screening"]["keywords"]
        tools = {t["tool"] for t in ids["gas-btm-screening"]["tools"]}
        assert "get_gas_intelligence" in tools


class TestAgentsMd:
    def test_gas_skill_line_describes_inputs_not_a_score(self):
        # ★ 2026-08-30: this read the SOURCE and asserted the literal word
        #   "withdrawn". The copy became dynamic — the DCGI's state is now
        #   resolved at serve time from util.gas_index, so the template holds a
        #   token and the sentence only exists in the RENDERED output. Reading
        #   the template tested a string no agent ever receives. It now renders
        #   AGENTS.md and asserts the served line carries whatever the switch
        #   currently says, which is correct in BOTH positions and cannot go
        #   stale the way the hardcoded assertion did.
        import routes.agents_md_fallback as amd
        from util.gas_index import gas_index_copy
        md = amd._render_agents_md()
        line = next(l for l in md.splitlines() if "**gas_intelligence**" in l)
        assert gas_index_copy() in line, (
            "the gas skill line does not carry the authoritative DCGI state "
            "sentence; it must come from util.gas_index, not a hardcoded copy")
        assert "@@GAS_INDEX_STATE@@" not in line, "unresolved token served"
        # The skill line describes INPUTS. It may never assert a score itself.
        assert "0–100" not in line and "GAS-ADVANTAGED" not in line
        assert "get_gas_intelligence" in line


class TestApiV1AgentAlias:
    def test_the_alias_redirects_to_the_canonical_machine_map(self):
        flask = pytest.importorskip("flask")
        import routes.agent_concierge as ac
        app = flask.Flask(__name__)
        app.register_blueprint(ac.agent_concierge_bp)
        r = app.test_client().get("/api/v1/agent")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/api/v1/ai-agents.json")

    def test_the_trailing_slash_form_is_not_a_404_either(self):
        flask = pytest.importorskip("flask")
        import routes.agent_concierge as ac
        app = flask.Flask(__name__)
        app.register_blueprint(ac.agent_concierge_bp)
        r = app.test_client().get("/api/v1/agent/")
        assert r.status_code in (301, 302, 308)


class TestOpenApiManifestDescription:
    def test_the_manifest_endpoint_docstring_carries_no_tool_count(self):
        src = (ROOT / "main.py").read_text()
        start = src.index("def well_known_mcp():")
        end = src.index("def _canonical_mcp_manifest():", start)
        doc = src[start:end]
        assert not re.search(r"\b\d{1,3} tools\b", doc), \
            "the docstring is exported as the OpenAPI description; a literal count rots"
