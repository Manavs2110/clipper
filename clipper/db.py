import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    broadcaster TEXT,
    title TEXT,
    url TEXT,
    views INTEGER,
    status TEXT,          -- rendered | failed
    output_path TEXT,
    error TEXT,
    processed_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute(SCHEMA)

    def seen(self, clip_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM clips WHERE id=?", (clip_id,)).fetchone() is not None

    def record(self, clip: dict, status: str, output_path: str = "", error: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO clips (id, broadcaster, title, url, views, status, output_path, error)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (clip["id"], clip["broadcaster_name"], clip["title"], clip["url"],
             clip["view_count"], status, output_path, error),
        )
        self.conn.commit()

    def forget(self, status: str) -> int:
        n = self.conn.execute("DELETE FROM clips WHERE status=?", (status,)).rowcount
        self.conn.commit()
        return n
