"""The HF Space entry must live in what SERVES /llms.txt, not in a decoy.

2026-09-08. dchub-frontend#1423 added this entry to dchub-frontend/llms.txt and
merged, and the served document did not change — because the CF worker routes
`/llms.txt` to Railway ("Discovery paths -> Railway", worker.js), so the frontend
file is not what serves. main.py already records the same trap for a different
decoy: "FALSE POSITIVE -- static/llms.txt is NOT what serves /llms.txt. The live
file is rendered by ai_discovery_routes.serve_llms_txt()".

There are FIVE llms.txt files across the two repos and none of them is the one
that serves. This test pins the entry to the renderer, so the next person who
edits a decoy gets a red build instead of a merged no-op.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
RENDERER = ROOT / "ai_discovery_routes.py"
_HF_SPACE = "huggingface.co/spaces/dchubcloud/dchub"
_HF_MCP = "dchubcloud-dchub.hf.space/gradio_api/mcp/sse"


def test_the_renderer_names_the_hugging_face_space():
    """If this passes only because a decoy file was edited, it is worthless —
    so it reads the renderer and nothing else."""
    src = RENDERER.read_text(encoding="utf-8")
    assert _HF_SPACE in src, (
        "the HF Space is missing from ai_discovery_routes.py, which is what "
        "actually renders /llms.txt. Editing dchub-frontend/llms.txt, "
        "./llms.txt, ./static/llms.txt or ./static/llms-full.txt changes "
        "NOTHING that is served.")
    assert _HF_MCP in src, "the Space's MCP endpoint is not named"


def test_it_is_named_as_a_subset_not_a_replacement():
    """7 curated tools against 88. A client that can reach the full server must
    not be steered to the Space by our own discovery document."""
    src = RENDERER.read_text(encoding="utf-8")
    i = src.index(_HF_SPACE)
    entry = src[i:i + 600]
    assert "curated subset" in entry
    assert "not a replacement" in entry
    assert "Prefer the full MCP Server" in entry


def test_the_decoys_are_still_decoys():
    """A record, not a rule: these files exist and do not serve. If one of them
    ever DOES start serving, this test is where the assumption is written down.
    """
    decoys = [ROOT / "llms.txt", ROOT / "static" / "llms.txt"]
    present = [p for p in decoys if p.exists()]
    assert present, "the decoys vanished — re-check which file serves /llms.txt"
