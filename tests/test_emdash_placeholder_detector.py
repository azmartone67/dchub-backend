"""The healer's "— placeholder" detector must flag an EMPTY data cell, never an
em-dash used as a separator between two elements.

Measured 2026-09-23: /grid/PJM, /grid/CAISO and /grid/ERCOT were each filed as
"— placeholder" for one and the same line — the AI-agents footer,
`<a>https://dchub.cloud/mcp</a> — <code>get_grid_intelligence</code>` — because
`>\\s*—\\s*<` matches a dash with ANY tag on each side. The squasher queued all
three as defects and its agent lane spent a run proving the page was healthy.

Per CLAUDE.md, dchub_self_heal is not imported: the real pattern and the real
scan function are pulled out of the source with ast and run against stubs.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = (ROOT / "dchub_self_heal.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

# The exact footer line the three grid pages were filed for (live, 2026-09-23,
# after the scan's own attribute stripping).
FOOTER = ('<p>AI agents: this page live via the DC Hub MCP server at '
          '<a>https://dchub.cloud/mcp</a> — <code>get_grid_intelligence</code>. '
          'Also for this page: <code>get_iso_context</code></p>')


def _segment(tree, src, name):
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(src, n)
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == name for t in n.targets):
            return ast.get_source_segment(src, n)
    raise AssertionError(f"{name} not found — renamed? update this test")


def _patterns():
    ns = {"_hp_re": re}
    exec(_segment(_TREE, _SRC, "HTML_BAD_PATTERNS"), ns)
    return ns["HTML_BAD_PATTERNS"]


def _placeholder():
    return _patterns()["— placeholder"]


@pytest.mark.parametrize("html", [
    FOOTER,
    "<a>one</a> — <a>two</a>",
    "<span>a</span>—<span>b</span>",
    "<li><b>Queue</b> — 42 GW</li>",
    "<p>Open Platform — Free</p>",
])
def test_a_separator_between_elements_is_not_a_placeholder(html):
    assert not _placeholder().search(html), html


@pytest.mark.parametrize("html", [
    "<td>—</td>",
    "<span> — </span>",
    "<div>\n    —\n  </div>",
    "<b> — </b>",
    '<td class="val">—</td>',
    "<TD>—</td >",
])
def test_an_element_whose_whole_content_is_the_dash_is_a_placeholder(html):
    assert _placeholder().search(html), html


def test_the_real_scan_files_the_empty_cell_and_not_the_footer():
    """Runs fix_html_quality_scan itself — the pattern AND the stripping
    pipeline in front of it — on a page carrying both shapes."""
    page = ("<html><head><title>PJM — DC Hub</title></head><body>"
            "<table><tr><td>LMP</td><td>—</td></tr></table>"
            f"{FOOTER}</body></html>")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return page.encode()

    ns = {"_hp_re": re,
          "HTML_PROBE_URLS": ["https://dchub.cloud/grid/PJM"],
          "EMDASH_IGNORE_URLS": set(),
          "_urlopen_retry": lambda req, timeout: _Resp()}
    ns["HTML_BAD_PATTERNS"] = _patterns()
    exec(_segment(_TREE, _SRC, "fix_html_quality_scan"), ns)
    ok, _msg = ns["fix_html_quality_scan"]()
    assert ok
    hits = ns["_last_html_findings"]["https://dchub.cloud/grid/PJM"]
    assert hits == {"— placeholder": 1}, hits     # the <td>—</td>, not the footer


def test_footer_alone_files_nothing_through_the_real_scan():
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return f"<html><body>{FOOTER}</body></html>".encode()

    ns = {"_hp_re": re, "HTML_PROBE_URLS": ["https://dchub.cloud/grid/CAISO"],
          "EMDASH_IGNORE_URLS": set(), "_urlopen_retry": lambda r, t: _Resp()}
    ns["HTML_BAD_PATTERNS"] = _patterns()
    exec(_segment(_TREE, _SRC, "fix_html_quality_scan"), ns)
    ns["fix_html_quality_scan"]()
    assert "https://dchub.cloud/grid/CAISO" not in ns["_last_html_findings"]


def test_the_qa_crawler_copy_is_the_same_pattern():
    """scripts/dchub_qa_crawl.py keeps its own _PLACEHOLDER_RE. Two copies of
    one rule drift; this pins them together."""
    src = (ROOT / "scripts" / "dchub_qa_crawl.py").read_text(encoding="utf-8")
    ns = {"re": re}
    exec(_segment(ast.parse(src), src, "_PLACEHOLDER_RE"), ns)
    assert ns["_PLACEHOLDER_RE"].pattern == _placeholder().pattern
    assert ns["_PLACEHOLDER_RE"].flags == _placeholder().flags
