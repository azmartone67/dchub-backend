"""
DC Hub Combined Launcher — Flask + MCP Server
===============================================
Runs both your existing Flask app (main.py) and the real MCP server
in a single Replit process.

STRATEGY:
  - Flask runs in a background thread on an internal port (127.0.0.1:5001)
  - MCP server runs as the main process on the Replit-exposed port
  - MCP tools proxy to Flask internally via localhost
  - External clients see ONE URL that handles both REST API and MCP

WHY:
  Replit only exposes one port. This lets both Flask and MCP share it
  by putting the MCP server (ASGI) in front and proxying Flask requests.

USAGE:
  Replace your Replit "Run" command with:
    python run_combined.py

  Or in .replit:
    run = "python run_combined.py"
"""

import os
import sys
import threading
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dchub-combined")


def run_flask():
    """Run the Flask app on an internal port."""
    # Set the port Flask will use (internal only, not exposed to internet)
    os.environ["FLASK_PORT"] = "5001"
    os.environ["PORT"] = "5001"  # Some Replit apps check this

    logger.info("Starting Flask backend on 127.0.0.1:5001 (internal)...")

    try:
        # Import your existing main.py
        # This should start the Flask app
        import main  # noqa: F401

        # If main.py doesn't auto-start, try:
        # from main import app
        # app.run(host='127.0.0.1', port=5001, debug=False)
    except Exception as e:
        logger.error(f"Flask startup failed: {e}")
        raise


def run_mcp():
    """Run the MCP server on the Replit-exposed port."""
    # Get the port Replit assigns (usually via PORT env var)
    # We need to use this for the MCP server since it's the external-facing one
    port = int(os.environ.get("REPLIT_PORT", os.environ.get("PORT", "8080")))

    # Point MCP tools at the internal Flask instance
    os.environ["DCHUB_API_BASE"] = "http://127.0.0.1:5001"
    os.environ["MCP_PORT"] = str(port)

    logger.info(f"Starting MCP server on 0.0.0.0:{port} (external)...")
    logger.info(f"  → MCP endpoint: http://0.0.0.0:{port}/mcp")
    logger.info(f"  → Proxying to Flask at http://127.0.0.1:5001")

    # Import and run the MCP server
    from dchub_mcp_server import mcp

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        path="/mcp",
    )


if __name__ == "__main__":
    # Save the original PORT so we can use it for MCP
    original_port = os.environ.get("PORT", "8080")
    os.environ["REPLIT_PORT"] = original_port

    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Give Flask a moment to start
    logger.info("Waiting for Flask to initialize...")
    time.sleep(3)

    # Run MCP server as the main process (blocking)
    run_mcp()
