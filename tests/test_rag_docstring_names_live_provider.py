"""The module docstring must name the provider the code actually defaults to.

`routes/brain_rag.py` opened with "pgvector + Cohere embeddings" and an
`Embed:` block describing Cohere's asymmetric search_document/search_query
model — for ten weeks after r-rag-mistral (2026-07-06) made **mistral-embed**
the default because the live COHERE_API_KEY is a trial key that exhausts into
429s. Both are 1024-d, so nothing failed; the file just told every reader the
wrong thing about what was writing its vectors.

The expectation is READ FROM THE CODE, never restated here: hardcoding
"mistral" would make this pass for a file that says mistral and a
`_embed_provider` that returns cohere — the exact drift it exists to catch.

Stdlib + pytest; no DB, no network.
"""
import ast
import pathlib
import re

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "routes/brain_rag.py"
_TREE = ast.parse(_SRC.read_text())
_PROVIDERS = ("mistral", "cohere")


def _default_provider() -> str:
    """`_embed_provider()`'s fallback, from the AST.

    Not by import (the module body runs) and not by text match (the docstring
    under test names both providers). This is the real default."""
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        head, *tail = node.values
        if "RAG_EMBED_PROVIDER" not in ast.dump(head):
            continue
        if len(tail) == 1 and isinstance(tail[0], ast.Constant):
            return str(tail[0].value).strip().lower()
    raise AssertionError("no `RAG_EMBED_PROVIDER or <default>` in the module")


def _doc() -> str:
    d = ast.get_docstring(_TREE)
    assert d, "brain_rag.py lost its module docstring"
    return d


def test_the_default_is_one_of_the_providers_we_know_about():
    """A floor. If the default becomes something neither branch below models,
    the two tests after this would silently check nothing."""
    assert _default_provider() in _PROVIDERS, _default_provider()


def test_the_opening_line_names_the_live_provider():
    """The one line a reader skims. It said Cohere while Mistral wrote every
    vector."""
    want = _default_provider()
    head = _doc().split("\n")[0]
    m = re.search(r"pgvector \+ (\w+) embeddings", head)
    assert m, f"no `pgvector + <provider> embeddings` in: {head!r}"
    assert m.group(1).lower() == want, (
        f"opening line says {m.group(1)!r}, code defaults to {want!r}")


def test_the_embed_block_leads_with_the_live_provider():
    """Naming BOTH providers is correct — one is selectable. Which one comes
    FIRST is what a reader takes as the live answer, so the default has to
    open the block."""
    want = _default_provider()
    other = next(p for p in _PROVIDERS if p != want)
    doc = _doc()
    block = doc[doc.index("Embed:"):]
    block = block[:block.index("Recall:")] if "Recall:" in block else block
    at_want = block.lower().find(want)
    at_other = block.lower().find(other)
    assert at_want != -1, f"the Embed: block never names {want!r}"
    assert at_other == -1 or at_want < at_other, (
        f"the Embed: block leads with {other!r}, but the code defaults to {want!r}")


def test_the_docstring_says_the_choice_is_configurable():
    """Without the env var a reader cannot tell a default from a hard wiring,
    and the revert path (which needs a full re-embed) is invisible."""
    assert "RAG_EMBED_PROVIDER" in _doc()
