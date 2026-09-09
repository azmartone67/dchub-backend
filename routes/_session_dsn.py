"""_session_dsn.py — a session you OWN, for connections that set session state.

★ WHY THIS EXISTS. DATABASE_URL, NEON_DATABASE_URL and NEON_REPLICA_URL all
  point at Neon's `-pooler` endpoint: PgBouncer in TRANSACTION pooling. A
  SESSION-scoped setting — `conn.set_session(readonly=True)`, which issues
  SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY — survives the client
  disconnect on the shared SERVER backend. Every later client handed that
  backend inherits it and cannot write.

  MEASURED 2026-09-09, deployment 025707ba, by the probe in #4283/#4287/#4291:

      SESSION table=discovered_platforms tx_read_only=off default_read_only=off pid=16711
      SESSION table=discovered_platforms tx_read_only=ON  default_read_only=ON  pid=16714
      SESSION table=agent_requests       tx_read_only=off default_read_only=off pid=16728
      SESSION table=discovered_platforms tx_read_only=ON  default_read_only=ON  pid=16714

  One backend read-only every time it surfaced, its neighbours fine, and
  dsn_options='' — so it was a runtime SET stuck to that backend, not the DSN.
  Writes to surface_telemetry, agent_requests, discovered_platforms,
  discovery_hits and email_events raised 25006 whenever they landed on it, and
  every one of those failures is swallowed by note_swallowed_write. email_events
  is the one that hurts: it is the only independent proof an email was
  delivered.

  It also SURVIVES OUR REDEPLOYS, because the state lives in PgBouncer rather
  than in our process — which is why it looked like it could not be our code.

★ THE RULE: session-scoped state requires a session you own. Connect to the
  DIRECT endpoint, where the backend dies with the connection and can poison
  nobody. Never call set_session(readonly=...) on a pooled DSN.

  Not a workaround for a connection you share — a statement that this
  connection is not shared.
"""
from urllib.parse import urlparse, urlunparse

_POOLER_SUFFIX = "-pooler"


def direct_dsn(url):
    """The non-pooled twin of a Neon DSN. Any other URL is returned unchanged.

    Rewrites the HOST only. A naive url.replace('-pooler.', '.') would also
    corrupt a password that happens to contain that text, and passwords here are
    generated — assuming they cannot is not a safety argument.
    """
    if not url or not isinstance(url, str):
        return url
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001 — a DSN we cannot parse is left alone
        return url
    host = p.hostname or ""
    if _POOLER_SUFFIX not in host:
        return url
    new_host = host.replace(_POOLER_SUFFIX, "", 1)
    # Rebuild netloc, preserving userinfo and port exactly as given.
    userinfo = ""
    if p.username is not None:
        userinfo = p.username
        if p.password is not None:
            userinfo += ":" + p.password
        userinfo += "@"
    port = "" if p.port is None else ":" + str(p.port)
    return urlunparse(p._replace(netloc=userinfo + new_host + port))
