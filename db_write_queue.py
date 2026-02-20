"""
Database Write Queue - Executes writes directly to PostgreSQL.
PostgreSQL handles concurrency natively, no serialization needed.
"""
import logging
from db_utils import get_db

logger = logging.getLogger(__name__)

class DatabaseWriteQueue:
    def __init__(self, db_path='dc_nexus.db', max_retries=5, retry_delay=2):
        self.running = True
        self.stats = {'queued': 0, 'success': 0, 'failed': 0}

    def start(self):
        self.running = True
        logger.info("✅ Database write queue started (PG-native, no SQLite serialization)")

    def stop(self):
        self.running = False

    def queue_write(self, sql, params=None, callback=None):
        self.stats['queued'] += 1
        try:
            conn = get_db()
            conn.execute(sql, params)
            conn.commit()
            conn.close()
            self.stats['success'] += 1
            if callback:
                callback(True)
        except Exception as e:
            self.stats['failed'] += 1
            logger.warning(f"Write queue PG write failed: {e}")
            if callback:
                callback(False)

    def queue_batch(self, operations):
        for op in operations:
            sql = op.get('sql', '')
            params = op.get('params', None)
            self.queue_write(sql, params)

    def get_stats(self):
        return {
            **self.stats,
            'pending': 0,
            'running': self.running
        }

write_queue = DatabaseWriteQueue()

def get_write_queue():
    return write_queue

def safe_db_write(sql, params=None):
    write_queue.queue_write(sql, params)
