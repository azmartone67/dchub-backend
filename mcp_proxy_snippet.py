"""
MCP Proxy for main.py — Streamable HTTP Transport
==================================================

PASTE THIS INTO main.py, replacing the existing mcp_proxy() function.

This proxy forwards /mcp requests to the internal MCP server on port 8888.
It handles:
  - POST /mcp  → JSON-RPC tool calls (initialize, tools/list, tools/call)
  - GET  /mcp  → SSE streaming (if server sends event streams)
  - DELETE /mcp → Session cleanup
  - OPTIONS /mcp → CORS preflight

The key difference from the old SSE proxy: Streamable HTTP uses a SINGLE
endpoint (/mcp) for everything. No more /sse + /messages split.
"""

# ===========================================================================
# ADD/REPLACE IN main.py — MCP Proxy Routes
# ===========================================================================

# Make sure these imports exist at the top of main.py:
# import requests as http_requests   (or whatever alias you use)
# from flask import request, jsonify, Response

# AUTO-REPAIR: duplicate route '/mcp' also in main.py:5022 — review and remove one
# AUTO-REPAIR: duplicate route '/mcp/' also in main.py:5033 — review and remove one
@app.route('/mcp', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
@app.route('/mcp/', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
def mcp_proxy():
    """
    Proxy to MCP Streamable HTTP server on port 8888.
    
    Streamable HTTP uses a single /mcp endpoint for all operations:
    - POST: JSON-RPC requests (initialize, tools/list, tools/call)
    - GET: Optional SSE stream for server-initiated messages
    - DELETE: Session termination
    - OPTIONS: CORS preflight
    """
    import requests as http_req
    
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = app.make_default_options_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept, Mcp-Session-Id'
        response.headers['Access-Control-Expose-Headers'] = 'Mcp-Session-Id'
        return response
    
    target = 'http://127.0.0.1:8888/mcp'
    
    # Build headers — forward everything except Host
    fwd_headers = {}
    for key, value in request.headers:
        if key.lower() not in ('host', 'transfer-encoding'):
            fwd_headers[key] = value
    
    try:
        # For GET requests (SSE streams), use streaming
        if request.method == 'GET':
            resp = http_req.get(
                target,
                headers=fwd_headers,
                params=request.args,
                stream=True,
                timeout=60,
            )
            
            # If it's an SSE stream, proxy it as streaming
            if 'text/event-stream' in resp.headers.get('Content-Type', ''):
                def generate():
                    try:
                        for chunk in resp.iter_content(chunk_size=None):
                            if chunk:
                                yield chunk
                    except Exception:
                        pass
                
                proxy_resp = Response(
                    generate(),
                    status=resp.status_code,
                    content_type=resp.headers.get('Content-Type', 'text/event-stream'),
                )
                proxy_resp.headers['Cache-Control'] = 'no-cache'
                proxy_resp.headers['Connection'] = 'keep-alive'
                proxy_resp.headers['Access-Control-Allow-Origin'] = '*'
                proxy_resp.headers['Access-Control-Expose-Headers'] = 'Mcp-Session-Id'
                # Forward session ID if present
                if 'Mcp-Session-Id' in resp.headers:
                    proxy_resp.headers['Mcp-Session-Id'] = resp.headers['Mcp-Session-Id']
                return proxy_resp
            else:
                # Non-streaming GET response
                excluded = {'transfer-encoding', 'content-encoding', 'connection'}
                headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in excluded
                }
                headers['Access-Control-Allow-Origin'] = '*'
                headers['Access-Control-Expose-Headers'] = 'Mcp-Session-Id'
                return (resp.content, resp.status_code, headers)
        
        # POST and DELETE — standard proxy
        resp = http_req.request(
            method=request.method,
            url=target,
            headers=fwd_headers,
            data=request.get_data(),
            params=request.args,
            timeout=30,
        )
        
        # Check if POST response is SSE (streaming tool results)
        content_type = resp.headers.get('Content-Type', '')
        if 'text/event-stream' in content_type:
            def generate():
                try:
                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                except Exception:
                    pass
            
            proxy_resp = Response(
                generate(),
                status=resp.status_code,
                content_type='text/event-stream',
            )
            proxy_resp.headers['Cache-Control'] = 'no-cache'
            proxy_resp.headers['Access-Control-Allow-Origin'] = '*'
            proxy_resp.headers['Access-Control-Expose-Headers'] = 'Mcp-Session-Id'
            if 'Mcp-Session-Id' in resp.headers:
                proxy_resp.headers['Mcp-Session-Id'] = resp.headers['Mcp-Session-Id']
            return proxy_resp
        
        # Standard JSON response
        excluded = {'transfer-encoding', 'content-encoding', 'connection'}
        headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in excluded
        }
        headers['Access-Control-Allow-Origin'] = '*'
        headers['Access-Control-Expose-Headers'] = 'Mcp-Session-Id'
        return (resp.content, resp.status_code, headers)
    
    except http_req.ConnectionError:
        return jsonify({
            "jsonrpc": "2.0",
            "error": {
                "code": -32000,
                "message": "MCP server is not running. The internal MCP process on port 8888 is unavailable."
            },
            "id": None
        }), 502
    
    except http_req.Timeout:
        return jsonify({
            "jsonrpc": "2.0",
            "error": {
                "code": -32000,
                "message": "MCP server timed out. Try again in a moment."
            },
            "id": None
        }), 504
    
    except Exception as e:
        return jsonify({
            "jsonrpc": "2.0",
            "error": {
                "code": -32603,
                "message": f"MCP proxy error: {str(e)}"
            },
            "id": None
        }), 502


# ===========================================================================
# ALSO ADD: Remove old SSE proxy routes if they exist
# ===========================================================================
# DELETE these routes from main.py if they exist (they conflict):
#   @app.route('/sse', ...)
#   @app.route('/messages', ...)
#   @app.route('/messages/', ...)
# The Streamable HTTP transport only uses /mcp — no /sse or /messages needed.
