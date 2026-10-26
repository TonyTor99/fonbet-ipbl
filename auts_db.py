"""Отдельная БД стратегии АУТОВ (auts_config.AUTS_DB).

Хранит ТОЛЬКО настройки стратегии (вкл/выкл, блэклист лиг) и РЕАЛЬНО отправленные
сигналы с результатом (зашло/не зашло). Чат рассылки — в общей bot_config (ipbl.db)
через database.get_chat_id/set_chat_id("auts").

Таблицы:
  auts_sent      — отправленные сигналы ТМ аутов + дорасчёт по факту аутов;
  auts_blacklist — лиги-исключения (подстрока имени, регистронезависимо);
  auts_settings  — пары ключ/значение (enabled).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from auts_config import AUTS_DB, DEFAULT_BLACKLIST, BANKROLL_START, STAKE

DIR = Path(__file__).parent


def _path(db: str) -> str:
    p = Path(db)
    return str(p if p.is_absolute() else DIR / p)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(AUTS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --- инициализация ----------------------------------------------------------

def init_db():
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS auts_sent (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     INTEGER NOT NULL UNIQUE,
            aut_child_id INTEGER,
            league       TEXT,
            team1        TEXT,
            team2        TEXT,
            start_time   INTEGER,
            line         REAL NOT NULL,
            odds         REAL NOT NULL,
            draw_odds    REAL,
            over25_odds  REAL,
            chat_id      INTEGER,
            message_id   INTEGER,
            status       TEXT NOT NULL DEFAULT 'sent',   -- 'sent' | 'no_data'
            result       TEXT,                           -- Выигрыш | Проигрыш | Возврат | NULL
            won          INTEGER,                        -- 1 | 0 | NULL
            final_auts   INTEGER,
            last_auts    INTEGER,
            live_seen    INTEGER NOT NULL DEFAULT 0,
            profit       REAL,
            created_at   TEXT NOT NULL,
            resolved_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS auts_blacklist (
            token TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS auts_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    # дефолты
    if conn.execute("SELECT COUNT(*) c FROM auts_settings WHERE key='enabled'").fetchone()["c"] == 0:
        conn.execute("INSERT INTO auts_settings (key, value) VALUES ('enabled','1')")
    if conn.execute("SELECT COUNT(*) c FROM auts_settings WHERE key='seeded'").fetchone()["c"] == 0:
        for tok in DEFAULT_BLACKLIST:
            conn.execute("INSERT OR IGNORE INTO auts_blacklist (token) VALUES (?)", (tok,))
        conn.execute("INSERT INTO auts_settings (key, value) VALUES ('seeded','1')")
    conn.commit()
    conn.close()


# --- настройки --------------------------------------------------------------

def is_enabled() -> bool:
    conn = _conn()
    row = conn.execute("SELECT value FROM auts_settings WHERE key='enabled'").fetchone()
    conn.close()
    return not row or row["value"] == "1"


def set_enabled(on: bool):
    conn = _conn()
    conn.execute("INSERT INTO auts_settings (key, value) VALUES ('enabled', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if on else "0",))
    conn.commit()
    conn.close()


# --- блэклист ---------------------------------------------------------------

def get_blacklist() -> list[str]:
    conn = _conn()
    rows = conn.execute("SELECT token FROM auts_blacklist ORDER BY token").fetchall()
    conn.close()
    return [r["token"] for r in rows]


def add_blacklist(token: str) -> bool:
    token = (token or "").strip()
    if not token:
        return False
    conn = _conn()
    try:
        conn.execute("INSERT OR IGNORE INTO auts_blacklist (token) VALUES (?)", (token,))
        conn.commit()
        return True
    finally:
        conn.close()


def remove_blacklist(token: str) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM auts_blacklist WHERE token=?", (token,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# --- сигналы ----------------------------------------------------------------

def signal_exists(event_id: int) -> bool:
    conn = _conn()
    row = conn.execute("SELECT 1 FROM auts_sent WHERE event_id=?", (event_id,)).fetchone()
    conn.close()
    return row is not None


def get_by_event(event_id: int):
    conn = _conn()
    row = conn.execute("SELECT * FROM auts_sent WHERE event_id=?", (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_sent(sig_id: int, chat_id: int, message_id: int):
    """Проставить чат/message_id уже записанному, но ещё не отправленному сигналу."""
    conn = _conn()
    conn.execute("UPDATE auts_sent SET chat_id=?, message_id=? WHERE id=?",
                 (chat_id, message_id, sig_id))
    conn.commit()
    conn.close()


def insert_signal(sig: dict):
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO auts_sent (event_id, aut_child_id, league, team1, team2,
                start_time, line, odds, draw_odds, over25_odds, chat_id, message_id,
                status, result, won, final_auts, last_auts, live_seen, profit, created_at)
            VALUES (:event_id, :aut_child_id, :league, :team1, :team2, :start_time,
                :line, :odds, :draw_odds, :over25_odds, :chat_id, :message_id,
                :status, :result, :won, :final_auts, :last_auts, :live_seen, :profit, :created_at)
        """, sig)
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None                 # дубль по event_id
    finally:
        conn.close()


def get_unresolved() -> list[dict]:
    """Отправленные сигналы без результата (ждут дорасчёта)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM auts_sent WHERE status='sent' AND result IS NULL"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_tracking(sig_id: int, last_auts: int):
    conn = _conn()
    conn.execute("UPDATE auts_sent SET last_auts=?, live_seen=1 WHERE id=?",
                 (int(last_auts), sig_id))
    conn.commit()
    conn.close()


def update_result(sig_id: int, result: str, won, final_auts: int, profit):
    conn = _conn()
    conn.execute(
        "UPDATE auts_sent SET result=?, won=?, final_auts=?, profit=?, resolved_at=? WHERE id=?",
        (result, won, int(final_auts), profit, _now(), sig_id))
    conn.commit()
    conn.close()


def mark_no_data(sig_id: int):
    conn = _conn()
    conn.execute("UPDATE auts_sent SET status='no_data', resolved_at=? WHERE id=?",
                 (_now(), sig_id))
    conn.commit()
    conn.close()


# --- статистика / отчёты ----------------------------------------------------

def get_all() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM auts_sent ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats_between(start: str, end: str) -> dict:
    """Агрегат по created_at в [start, end): count/wins/losses/push/profit/roi%."""
    conn = _conn()
    rows = conn.execute(
        "SELECT won, result, profit FROM auts_sent "
        "WHERE status='sent' AND result IS NOT NULL AND created_at>=? AND created_at<?",
        (start, end)).fetchall()
    conn.close()
    wins = sum(1 for r in rows if r["won"] == 1)
    losses = sum(1 for r in rows if r["won"] == 0)
    push = sum(1 for r in rows if r["result"] == "Возврат")
    profit = sum((r["profit"] or 0.0) for r in rows)
    settled = wins + losses                      # возвраты не влияют на винрейт/ROI
    roi = (profit / (settled * STAKE) * 100.0) if settled else 0.0
    winrate = (wins / settled * 100.0) if settled else 0.0
    return {"count": len(rows), "wins": wins, "losses": losses, "push": push,
            "profit": profit, "roi": roi, "winrate": winrate}


def pending_count() -> int:
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) c FROM auts_sent WHERE status='sent' AND result IS NULL").fetchone()["c"]
    conn.close()
    return n
