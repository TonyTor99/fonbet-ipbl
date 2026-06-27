"""SQLite: один сигнал — одна строка. Никакой истории тоталов.

Таблицы:
  signals    — отправленные/зафиксированные сигналы (дедуп по strategy+event_id)
  bot_config — на стратегию: chat_id и окно работы (work_start/work_end "HH:MM")
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from config import BANKROLL_START, STAKE

DB_PATH = os.getenv("IPBL_DB_PATH", str(Path(__file__).parent / "ipbl.db"))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy      TEXT NOT NULL,
            event_id      INTEGER NOT NULL,
            league        TEXT NOT NULL,
            division      TEXT NOT NULL,
            team1         TEXT NOT NULL,
            team2         TEXT NOT NULL,
            side          TEXT NOT NULL,          -- ТМ (инфо: '—')
            line          REAL,                   -- NULL = сигнал без линии (void)
            odds          REAL,
            half_total    INTEGER NOT NULL,       -- сумма очков к перерыву
            formula_value REAL,                   -- 2*half_total - line
            qualified     INTEGER NOT NULL DEFAULT 0,  -- 1 = прошёл формулу (сигнал), 0 = просто снимок перерыва
            in_window     INTEGER NOT NULL DEFAULT 1,  -- 1 = в окне работы (идёт в статистику), 0 = пауза (только анализ)
            totals_snapshot TEXT,                 -- весь блок ТМ на перерыве "219.5@2.1|220.5@1.87|..."
            fixed_score1  INTEGER NOT NULL,
            fixed_score2  INTEGER NOT NULL,
            fixed_quarters TEXT,                  -- "18:29 | 18:18" (текст, Q1|Q2 на сигнале)
            q1            TEXT,                   -- счёт Q1 "24:24" (на сигнале)
            q2            TEXT,                   -- счёт Q2 (на сигнале)
            q3            TEXT,                   -- счёт Q3 (заполняется на финале)
            q4            TEXT,                   -- счёт Q4 (заполняется на финале)
            line_move     TEXT,                   -- движение линии ставки "188,5 → 185,5"
            line_prematch REAL,                   -- первая увиденная (лайв-старт) линия ТМ
            chat_id       INTEGER,
            message_id    INTEGER,
            status        TEXT NOT NULL,          -- sent / not_sent / info
            result        TEXT,                   -- Выигрыш / Проигрыш / NULL
            final_score   TEXT,
            final_total   INTEGER,
            profit        REAL,                   -- ₽ по этому сигналу (NULL пока не дорассчитан)
            created_at    TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sig_unique ON signals(strategy, event_id);
        CREATE INDEX IF NOT EXISTS idx_sig_event ON signals(event_id);

        CREATE TABLE IF NOT EXISTS bot_config (
            strategy   TEXT PRIMARY KEY,
            chat_id    INTEGER,
            windows    TEXT,                        -- "10:00-12:00,16:00-18:00" или NULL = круглосуточно
            updated_at TEXT
        );
    """)
    conn.commit()
    # миграции для уже существующей БД
    for col, ddl in [
        ("qualified", "ALTER TABLE signals ADD COLUMN qualified INTEGER NOT NULL DEFAULT 0"),
        ("in_window", "ALTER TABLE signals ADD COLUMN in_window INTEGER NOT NULL DEFAULT 1"),
        ("totals_snapshot", "ALTER TABLE signals ADD COLUMN totals_snapshot TEXT"),
        ("line_move", "ALTER TABLE signals ADD COLUMN line_move TEXT"),
        ("q1", "ALTER TABLE signals ADD COLUMN q1 TEXT"),
        ("q2", "ALTER TABLE signals ADD COLUMN q2 TEXT"),
        ("q3", "ALTER TABLE signals ADD COLUMN q3 TEXT"),
        ("q4", "ALTER TABLE signals ADD COLUMN q4 TEXT"),
        ("line_prematch", "ALTER TABLE signals ADD COLUMN line_prematch REAL"),
        ("windows", "ALTER TABLE bot_config ADD COLUMN windows TEXT"),
    ]:
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            pass
    conn.close()


# --- конфиг стратегий ------------------------------------------------------

def _ensure_config_row(conn, strategy: str):
    conn.execute("INSERT OR IGNORE INTO bot_config (strategy) VALUES (?)", (strategy,))


def get_chat_id(strategy: str) -> int | None:
    conn = _conn()
    row = conn.execute("SELECT chat_id FROM bot_config WHERE strategy=?", (strategy,)).fetchone()
    conn.close()
    return row["chat_id"] if row else None


def set_chat_id(strategy: str, chat_id: int):
    conn = _conn()
    _ensure_config_row(conn, strategy)
    conn.execute(
        "UPDATE bot_config SET chat_id=?, updated_at=? WHERE strategy=?",
        (chat_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), strategy),
    )
    conn.commit()
    conn.close()


def get_windows(strategy: str) -> list[tuple[str, str]]:
    """Список окон работы [(start,end), ...] из строки 'HH:MM-HH:MM,HH:MM-HH:MM'.
    Пустой список = круглосуточно."""
    conn = _conn()
    row = conn.execute("SELECT windows FROM bot_config WHERE strategy=?", (strategy,)).fetchone()
    conn.close()
    if not row or not row["windows"]:
        return []
    out = []
    for part in row["windows"].split(","):
        part = part.strip()
        if "-" in part:
            s, e = part.split("-", 1)
            out.append((s.strip(), e.strip()))
    return out


def set_windows(strategy: str, windows: str | None):
    """windows — нормализованная строка 'HH:MM-HH:MM,...' или None = круглосуточно."""
    conn = _conn()
    _ensure_config_row(conn, strategy)
    conn.execute(
        "UPDATE bot_config SET windows=?, updated_at=? WHERE strategy=?",
        (windows, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), strategy),
    )
    conn.commit()
    conn.close()


# --- сигналы ---------------------------------------------------------------

def signal_exists(strategy: str, event_id: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM signals WHERE strategy=? AND event_id=?", (strategy, event_id)
    ).fetchone()
    conn.close()
    return row is not None


def insert_signal(sig: dict) -> int | None:
    """UNIQUE(strategy,event_id) защищает от дублей. None если дубль."""
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO signals
                (strategy, event_id, league, division, team1, team2, side, line, odds,
                 half_total, formula_value, qualified, in_window, totals_snapshot,
                 fixed_score1, fixed_score2, fixed_quarters, q1, q2, q3, q4,
                 line_move, line_prematch,
                 chat_id, message_id, status, result, final_score, final_total, profit, created_at)
            VALUES
                (:strategy, :event_id, :league, :division, :team1, :team2, :side, :line, :odds,
                 :half_total, :formula_value, :qualified, :in_window, :totals_snapshot,
                 :fixed_score1, :fixed_score2, :fixed_quarters, :q1, :q2, :q3, :q4,
                 :line_move, :line_prematch,
                 :chat_id, :message_id, :status, :result, :final_score, :final_total, :profit, :created_at)
        """, sig)
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_signals_for_event(event_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM signals WHERE event_id=?", (event_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_signals(strategy: str = "signal_tm") -> list[dict]:
    """Все записи стратегии для выгрузки в Excel (прошедшие формулу + снимки перерыва),
    новые сверху."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM signals WHERE strategy=? ORDER BY created_at DESC, id DESC",
        (strategy,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_signal_result(signal_id: int, result: str | None,
                         final_score: str, final_total: int, profit: float | None,
                         q3: str | None = None, q4: str | None = None):
    conn = _conn()
    conn.execute(
        "UPDATE signals SET result=?, final_score=?, final_total=?, profit=?, q3=?, q4=? WHERE id=?",
        (result, final_score, final_total, profit, q3, q4, signal_id),
    )
    conn.commit()
    conn.close()


def bot_stats(strategy: str) -> dict:
    """Статистика стратегии. Считаются ТОЛЬКО матчи в окне работы (in_window=1);
    перерывы во время паузы в статистику не идут. `matches` = снимки перерыва (в окне),
    `signals` = прошедшие формулу. Win/loss/прибыль — по прошедшим (qualified=1)."""
    conn = _conn()
    q = "SELECT COUNT(*) FROM signals WHERE strategy=? AND in_window=1"
    qq = q + " AND qualified=1"
    total   = conn.execute(q, (strategy,)).fetchone()[0]
    signals = conn.execute(qq, (strategy,)).fetchone()[0]
    wins    = conn.execute(qq + " AND result='Выигрыш'", (strategy,)).fetchone()[0]
    losses  = conn.execute(qq + " AND result='Проигрыш'", (strategy,)).fetchone()[0]
    no_res  = conn.execute(qq + " AND result IS NULL", (strategy,)).fetchone()[0]
    void    = conn.execute(qq + " AND line IS NULL", (strategy,)).fetchone()[0]
    profit  = conn.execute(
        "SELECT COALESCE(SUM(profit),0) FROM signals WHERE strategy=? AND qualified=1 AND in_window=1",
        (strategy,)).fetchone()[0]
    conn.close()
    settled = wins + losses
    winrate = (wins / settled * 100) if settled else 0.0
    staked = settled * STAKE                       # поставлено по рассчитанным сигналам
    roi = (profit / staked * 100) if staked else 0.0
    return {
        "matches": total, "signals": signals, "wins": wins, "losses": losses,
        "no_result": no_res, "void": void, "profit": profit,
        "balance": BANKROLL_START + profit, "winrate": winrate,
        "staked": staked, "roi": roi,
    }


def active_count() -> int:
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM signals WHERE result IS NULL AND line IS NOT NULL").fetchone()[0]
    conn.close()
    return n


def clear_db():
    conn = _conn()
    conn.execute("DELETE FROM signals")
    conn.commit()
    conn.close()
    conn2 = sqlite3.connect(DB_PATH)
    conn2.execute("VACUUM")
    conn2.close()
