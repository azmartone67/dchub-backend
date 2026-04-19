"""DC Hub Backend Patches - March 31, 2026"""
import re, sys, os, time

POOLED_CONNECTION_CLASS = '''
class PooledConnection:
    """Wrapper that returns connection to pool on close() instead of destroying it."""
    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool
        self._closed = False
        self._checkout_time = time.time()

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.rollback()
        except Exception:
            pass
        try:
            if self._pool:
                self._pool.putconn(self._conn)
                _pool_stats['returned'] = _pool_stats.get('returned', 0) + 1
            else:
                self._conn.close()
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __del__(self):
        if not self._closed:
            self.close()
'''

def apply_patches(path):
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        return False

    with open(path, 'r') as f:
        content = f.read()

    original = content
    changes = []

    # PATCH 1: Add PooledConnection class
    if 'class PooledConnection' not in content:
        target = 'def get_pg_connection(retries=3, pool_type=None):'
        if target in content:
            content = content.replace(target, POOLED_CONNECTION_CLASS + '\n' + target)
            changes.append("Added PooledConnection wrapper class")

    # PATCH 1b: Wrap return
    p = re.compile(r"(_pool_stats\['acquired'\]\s*[\+\=]+\s*1\s*\n\s*)(return conn)", re.MULTILINE)
    if p.search(content):
        content = p.sub(r"\1return PooledConnection(conn, _pg_pool_obj)", content)
        changes.append("Wrapped pool return in PooledConnection")

    # PATCH 2: Pool timeouts
    if "_POOL_ACQUIRE_TIMEOUT = 10" in content:
        content = content.replace("_POOL_ACQUIRE_TIMEOUT = 10", "_POOL_ACQUIRE_TIMEOUT = 5       # fail fast")
        changes.append("Pool acquire timeout 10s -> 5s")

    if "_CONN_MAX_HOLD_SECONDS = 60" in content:
        content = content.replace("_CONN_MAX_HOLD_SECONDS = 60", "_CONN_MAX_HOLD_SECONDS = 30      # reclaim faster")
        changes.append("Max hold time 60s -> 30s")

    # PATCH 3: Seed ON CONFLICT
    ip = re.compile(
        r"(INSERT\s+INTO\s+(?:mcp_connections|mcp_platforms|ecosystem_companies|ai_platforms)[^;]*?)(VALUES\s*\([^)]+\))\s*(?!ON\s+CONFLICT)",
        re.IGNORECASE | re.DOTALL
    )
    new = ip.sub(lambda m: m.group(0) + "\n            ON CONFLICT DO NOTHING", content)
    if new != content:
        content = new
        changes.append("Added ON CONFLICT DO NOTHING to seed INSERTs")

    if content != original:
        backup = path + '.backup-' + str(int(time.time()))
        with open(backup, 'w') as f:
            f.write(original)
        print(f"Backup: {backup}")

        with open(path, 'w') as f:
            f.write(content)

        print(f"\nApplied {len(changes)} patches:")
        for c in changes:
            print(f"  + {c}")

        print("\nALSO RUN THIS SQL ON NEON:")
        print("CREATE INDEX IF NOT EXISTS idx_mcp_tool_calls_created_at ON mcp_tool_calls (created_at DESC);")
        print("CREATE INDEX IF NOT EXISTS idx_mcp_connections_platform ON mcp_connections (platform, created_at DESC);")
        print("CREATE INDEX IF NOT EXISTS idx_ambassador_broadcasts_created ON ambassador_broadcasts (created_at DESC);")
        return True
    else:
        print("No patches applied (already patched or patterns not found)")
        return False

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python dchub-backend-patches.py main.py")
        sys.exit(0)
    apply_patches(sys.argv[1])
