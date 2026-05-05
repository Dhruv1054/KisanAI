"""
database.py — SQLite setup, queries, and context manager for KisanAI
All database logic is isolated here. Nothing else should import sqlite3 directly.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from config import DB_PATH


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS farmers (
            phone            TEXT PRIMARY KEY,
            name             TEXT,
            crop             TEXT,
            district         TEXT,
            state            TEXT    DEFAULT '',
            language         TEXT    DEFAULT 'hi',
            lat              REAL    DEFAULT 28.6139,
            lon              REAL    DEFAULT 77.2090,
            onboarding_step  INTEGER DEFAULT 0,
            onboarded        INTEGER DEFAULT 0,
            created_at       TEXT,
            last_active      TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            phone      TEXT    NOT NULL,
            direction  TEXT    NOT NULL,   -- 'user' | 'bot'
            message    TEXT    NOT NULL,
            created_at TEXT    DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations (phone);
        CREATE INDEX IF NOT EXISTS idx_farmers_onboarded ON farmers (onboarded);
    """)
    conn.commit()
    conn.close()


@contextmanager
def get_db():
    """Yields a committed, closed SQLite connection with Row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ——— Farmer helpers ———

def get_farmer(phone: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM farmers WHERE phone = ?", (phone,)
        ).fetchone()
        return dict(row) if row else None


def create_farmer(phone: str) -> None:
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO farmers (phone, onboarding_step, created_at, last_active) "
            "VALUES (?, 0, ?, ?)",
            (phone, now, now),
        )


def update_farmer(phone: str, **fields) -> None:
    """Generic field updater — always refreshes last_active."""
    if not fields:
        return
    fields["last_active"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [phone]
    with get_db() as conn:
        conn.execute(f"UPDATE farmers SET {set_clause} WHERE phone = ?", values)


def advance_step(phone: str, step: int) -> None:
    """Move onboarding_step forward and refresh last_active."""
    update_farmer(phone, onboarding_step=step)


def touch_farmer(phone: str) -> None:
    """Just update last_active — for onboarded message handlers."""
    update_farmer(phone)


# ——— Conversation helpers ———

def save_message(phone: str, direction: str, message: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO conversations (phone, direction, message) VALUES (?, ?, ?)",
            (phone, direction, message[:600]),
        )


def get_recent_messages(phone: str, limit: int = 4) -> list[dict]:
    """Returns last N messages oldest-first for LLM context."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT direction, message FROM conversations "
            "WHERE phone = ? ORDER BY id DESC LIMIT ?",
            (phone, limit),
        ).fetchall()
    return [{"direction": r["direction"], "message": r["message"]} for r in reversed(rows)]


# ——— Stats ———

def get_stats() -> dict:
    with get_db() as conn:
        return {
            "total_farmers": conn.execute("SELECT COUNT(*) FROM farmers").fetchone()[0],
            "onboarded": conn.execute("SELECT COUNT(*) FROM farmers WHERE onboarded = 1").fetchone()[0],
            "total_messages": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        }


def get_all_onboarded() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT phone, name, crop, district, lat, lon "
            "FROM farmers WHERE onboarded = 1"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_farmers() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT phone, name, crop, district, lat, lon, "
            "onboarding_step, language, created_at, last_active, onboarded "
            "FROM farmers ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_farmer(phone: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM farmers WHERE phone = ?", (phone,))
        conn.execute("DELETE FROM conversations WHERE phone = ?", (phone,))