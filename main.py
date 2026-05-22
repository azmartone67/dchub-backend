"""
main.py — Application entry point for gunicorn.

Imports the Flask app from api_server and re-exports the database
helpers from database.py so that any legacy code doing
  `from main import get_db`
continues to work without circular imports.

The circular-import chain that was breaking startup:
  main.py (initialising) → registers routes → routes do
  `from main import get_db` → main.py not yet fully loaded → ImportError

Fix: get_db and the pool primitives now live in database.py which has
no imports from main.py.  main.py simply re-exports them.
"""

# Re-export DB helpers so legacy `from main import get_db` still works.
from database import (  # noqa: F401
    get_db,
    get_pg_connection,
    return_pg_connection,
    try_get_pg_connection,
    _record_circuit_failure,
)

# Import the Flask application object — gunicorn uses `main:app`.
from api_server import app  # noqa: F401
