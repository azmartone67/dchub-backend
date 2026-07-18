"""
routes/_webmcp.py — shared WebMCP page-instrumentation helper (2026-07-18,
webmcp-proto).

Backend-served pages that are NOT in main.py's _webmcp_enable script-include
families (/markets|/facilities|/dcpi|/grid) get their WebMCP surface from
here instead: each page route passes its rendered HTML plus 2-3 page-tool
descriptors through webmcp_inject(), which adds

  (a) <meta http-equiv="origin-trial" content="<env token>"> — belt to the
      after_request Origin-Trial header's braces: the meta survives edge
      caches that re-serve HTML without our response headers (the 07-xx
      api.dchub.cloud stale-cache class), and Chrome accepts either;
  (b) one inline <script id="dchub-webmcp-page-tools"> that feature-detects
      the trial and registers the page tools.

API shape (verified 2026-07-18 against developer.chrome.com/docs/ai/webmcp
+ /imperative-api and the webmachinelearning/webmcp explainer — and matching
the live frontend js/dchub-webmcp.js?v=4):
  - entry point document.modelContext (Chrome 150+); navigator.modelContext
    (Chrome 149) is deprecated — detect both, prefer document;
  - mc.registerTool({name, description, inputSchema, annotations, execute});
  - execute receives ONE object matching inputSchema; returning a plain
    string is the most reliable shape in the current trial (149-156);
  - annotations.readOnlyHint marks safety; descriptive error STRINGS (not
    throws) let the model self-correct.

Everything is gated on env WEBMCP_ORIGIN_TRIAL_TOKEN: unset ⇒ inject() is
an identity function (no meta, no script — the whole lane is invisible),
mirroring main.py's _webmcp_enable. In non-trial browsers the emitted
script is a feature-detected no-op. Attribution: every bound fetch stamps
src=webmcp + X-DC-Source: webmcp so ai_tracking classifies the traffic as
platform 'webmcp' (same markers as js/dchub-webmcp.js?v=3).

Tool descriptor (plain dict, per page):
  {"name": str, "description": str,
   "schema": <JSON Schema dict for inputSchema>,
   "js_body": <JS statements for the execute body; `input` (the args object)
               and `api(path)` (attributed fetch → string) are in scope>}

New API paths bound here MUST also be added to
routes/webmcp_master_shell.BOUND_API_PATHS (the keyless drift check).
"""
from __future__ import annotations

import html as _html
import json
import os

# Marker doubles as the script tag id and the idempotency check.
_MARKER = "dchub-webmcp-page-tools"


def _token() -> str:
    return (os.environ.get("WEBMCP_ORIGIN_TRIAL_TOKEN") or "").strip()


def _js(v) -> str:
    """JSON literal safe inside an inline <script> block."""
    return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")


def webmcp_meta_tag() -> str:
    """<meta http-equiv="origin-trial"> for the env token, or '' when unset."""
    token = _token()
    if not token:
        return ""
    return ('<meta http-equiv="origin-trial" content="%s">'
            % _html.escape(token, quote=True))


def webmcp_script(tools: list[dict]) -> str:
    """Inline feature-detected registration script for `tools`, or '' when
    the env token is unset (without the trial the registration can never
    fire, so don't ship dead bytes)."""
    if not _token() or not tools:
        return ""
    entries = ",\n".join(
        "{name:%s,description:%s,inputSchema:%s,"
        "annotations:{readOnlyHint:true},"
        "execute:async function(input){%s}}" % (
            _js(t["name"]), _js(t["description"]),
            _js(t.get("schema") or {"type": "object", "properties": {}}),
            t["js_body"])
        for t in tools)
    return (
        '<script id="' + _MARKER + '">\n'
        "(function(){'use strict';\n"
        "var mc=(typeof document!=='undefined'&&document.modelContext)||"
        "(typeof navigator!=='undefined'&&navigator.modelContext)||null;\n"
        "if(!mc||typeof mc.registerTool!=='function')return;"
        "/* no-op outside the Chrome origin trial */\n"
        "var CITE=' | Source: DC Hub (dchub.cloud), CC-BY-4.0 — cite as "
        '"DC Hub, dchub.cloud".\';\n'
        "var MAX=6000;\n"
        "function clip(s){return (s.length<=MAX?s:s.slice(0,MAX)+"
        "'… [truncated — refine the query or lower limit]')+CITE;}\n"
        "async function api(path){try{"
        "var url=path+(path.indexOf('?')===-1?'?':'&')+'src=webmcp';"
        "var r=await fetch(url,{headers:{Accept:'application/json',"
        "'X-DC-Source':'webmcp'}});"
        "if(!r.ok){return 'DC Hub API returned HTTP '+r.status+' for '+path+"
        "' — check parameter spelling; full docs at https://dchub.cloud/api/docs.';}"
        "return clip(JSON.stringify(await r.json()));"
        "}catch(e){return 'DC Hub API fetch failed ('+(e&&e.message)+"
        "') — retry once; if it persists the data is at https://dchub.cloud/api/docs.';}}\n"
        "var tools=[\n" + entries + "\n];\n"
        "tools.forEach(function(t){try{var p=mc.registerTool(t);"
        "if(p&&typeof p.catch==='function')p.catch(function(){});}catch(e){}});\n"
        "})();\n"
        "</script>")


def webmcp_inject(page_html: str, tools: list[dict]) -> str:
    """Add the origin-trial meta + page-tool script to rendered HTML.

    Identity when the env token is unset, on any error, or if the page is
    already instrumented (idempotent). Handles both full documents
    (</head>/</body> present — /phx, /integrations/mcp) and head-less
    fragment pages (/radar: a leading <meta> is hoisted into the
    parser-built <head> because it precedes all body-forcing content)."""
    try:
        token = _token()
        if not token or not page_html or _MARKER in page_html:
            return page_html
        meta = webmcp_meta_tag()
        script = webmcp_script(tools)
        if "</head>" in page_html:
            page_html = page_html.replace("</head>", meta + "\n</head>", 1)
        else:
            page_html = meta + "\n" + page_html
        if "</body>" in page_html:
            page_html = page_html.replace("</body>", script + "\n</body>", 1)
        else:
            page_html = page_html + "\n" + script
        return page_html
    except Exception:  # pragma: no cover — page must always render
        return page_html
