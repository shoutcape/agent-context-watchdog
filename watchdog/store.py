import json
import sqlite3


class Store:
    """One daemon owns the database; Core serializes access under its lock."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        row = self.db.execute("SELECT value FROM state WHERE id=1").fetchone()
        self.data = json.loads(row[0]) if row else {"observations": {}, "handoffs": {}}

    def save(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(self.data),))

    def close(self):
        self.db.close()
