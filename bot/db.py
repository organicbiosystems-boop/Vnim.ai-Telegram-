"""SQLite-хранилище: новости, решения админов, сообщения у админов."""
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,
    url TEXT,
    title TEXT,
    source TEXT,
    raw_summary TEXT,
    created_at REAL NOT NULL,
    -- seen | skipped | pending | published | rejected | error
    status TEXT NOT NULL,
    tier TEXT,                 -- breaking | regular | skip
    approve_prob INTEGER,      -- 0..100, прогноз модели «одобрят ли владельцы»
    reason TEXT,
    post_title TEXT,
    post_body TEXT,
    publish_mode TEXT,         -- auto_breaking | auto_learned | manual
    human_label INTEGER,       -- 1 = одобрено человеком, 0 = отклонено/удалено
    decided_by INTEGER,
    channel_message_id INTEGER,
    published_at REAL
);
CREATE INDEX IF NOT EXISTS idx_news_status ON news(status);

CREATE TABLE IF NOT EXISTS admin_msgs (
    news_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_msgs_news ON admin_msgs(news_id);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- kv ---
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # --- news ---
    def has_guid(self, guid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM news WHERE guid=?", (guid,)).fetchone() is not None

    def insert_news(self, guid: str, url: str, title: str, source: str,
                    raw_summary: str, status: str) -> int | None:
        try:
            cur = self.conn.execute(
                "INSERT INTO news(guid, url, title, source, raw_summary, created_at, status) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (guid, url, title, source, raw_summary, time.time(), status),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def get(self, news_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM news WHERE id=?", (news_id,)).fetchone()

    def update(self, news_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE news SET {cols} WHERE id=?", (*fields.values(), news_id))
        self.conn.commit()

    def add_admin_msg(self, news_id: int, chat_id: int, message_id: int) -> None:
        self.conn.execute(
            "INSERT INTO admin_msgs(news_id, chat_id, message_id) VALUES(?, ?, ?)",
            (news_id, chat_id, message_id),
        )
        self.conn.commit()

    def admin_msgs(self, news_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM admin_msgs WHERE news_id=?", (news_id,)
        ).fetchall()

    # --- данные для обучения ---
    def examples(self, label: int, limit: int) -> list[sqlite3.Row]:
        """Последние примеры, которые люди одобрили (1) или отклонили (0)."""
        return self.conn.execute(
            "SELECT title, post_title, source FROM news "
            "WHERE human_label=? ORDER BY COALESCE(published_at, created_at) DESC LIMIT ?",
            (label, limit),
        ).fetchall()

    def recent_titles(self, hours: int = 72, limit: int = 50) -> list[str]:
        since = time.time() - hours * 3600
        rows = self.conn.execute(
            "SELECT COALESCE(post_title, title) AS t FROM news "
            "WHERE status IN ('pending', 'published') AND created_at>=? "
            "ORDER BY created_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        return [r["t"] for r in rows if r["t"]]

    def labeled_predictions(self) -> list[tuple[int, int]]:
        """Пары (прогноз модели, решение человека) для калибровки порога.

        Срочные новости (auto_breaking) не учитываются: они публикуются по
        другому правилу. Авто-опубликованные по обученному порогу учитываются:
        если вы их не удалили — это считается одобрением.
        """
        rows = self.conn.execute(
            "SELECT approve_prob, human_label FROM news "
            "WHERE approve_prob IS NOT NULL AND human_label IS NOT NULL "
            "AND COALESCE(publish_mode, '') != 'auto_breaking'"
        ).fetchall()
        return [(r["approve_prob"], r["human_label"]) for r in rows]

    def stats(self) -> dict:
        def count(where: str) -> int:
            return self.conn.execute(f"SELECT COUNT(*) FROM news WHERE {where}").fetchone()[0]

        return {
            "seen_total": count("1=1"),
            "pending": count("status='pending'"),
            "published": count("status='published'"),
            "auto_breaking": count("publish_mode='auto_breaking'"),
            "auto_learned": count("publish_mode='auto_learned'"),
            "manual": count("publish_mode='manual'"),
            "approved": count("human_label=1"),
            "rejected": count("human_label=0"),
            "skipped": count("status='skipped'"),
        }
