"""Отдельная БД сборщика рынков шорт-хоккея.

Одна строка = снимок матча на конкретной ИГРОВОЙ минуте (плюс одна строка
ДО начала матча, game_minute = PREMATCH_MINUTE). По каждому рынку матча храним
КРАЙНЮЮ (верхнюю) линию: линию, кф всех исходов и результат (В/П/Возврат)
каждого исхода — проставляется на финале матча.

Файл БД отдельный от сигналов и от сборщика Prime (sh_config.SH_COLLECTOR_DB).
"""
import os
import sqlite3
from pathlib import Path

from sh_config import SH_COLLECTOR_DB, PREMATCH_MINUTE

DB_PATH = os.getenv("SH_COLLECTOR_DB_PATH", str(Path(__file__).parent / SH_COLLECTOR_DB))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    INTEGER NOT NULL,
            sport_id    INTEGER,
            league      TEXT NOT NULL,
            team1       TEXT NOT NULL,
            team2       TEXT NOT NULL,
            snap_dt_msk TEXT NOT NULL,        -- дата-время МСК снимка
            is_prematch INTEGER NOT NULL DEFAULT 0,  -- 1 = строка до начала матча
            game_minute INTEGER NOT NULL,     -- игровая минута (timerSeconds // 60); prematch = -1
            period      INTEGER,              -- активный период (1..3), None до начала
            periods     TEXT,                 -- счёт по периодам, напр. "1-0 0-2"
            score1      INTEGER NOT NULL,
            score2      INTEGER NOT NULL,

            -- Исход 1X2
            win1_odds   REAL, draw_odds  REAL, win2_odds  REAL,
            -- Двойные шансы
            dc_1x_odds  REAL, dc_12_odds REAL, dc_x2_odds REAL,
            -- Фора (линия со стороны К1)
            fora_line   REAL, fora1_odds REAL, fora2_odds REAL,
            -- Тотал
            total_line  REAL, total_b_odds REAL, total_m_odds REAL,
            -- Инд. тоталы
            it1_line    REAL, it1_b_odds REAL, it1_m_odds REAL,
            it2_line    REAL, it2_b_odds REAL, it2_m_odds REAL,

            -- Результаты каждого исхода (Выигрыш/Проигрыш/Возврат)
            r_win1  TEXT, r_draw  TEXT, r_win2  TEXT,
            r_1x    TEXT, r_12    TEXT, r_x2    TEXT,
            r_fora1 TEXT, r_fora2 TEXT,
            r_total_b TEXT, r_total_m TEXT,
            r_it1_b TEXT, r_it1_m TEXT,
            r_it2_b TEXT, r_it2_m TEXT,

            final_score TEXT,
            final_total INTEGER,
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sh_unique
            ON market_snapshots(event_id, game_minute);
        CREATE INDEX IF NOT EXISTS idx_sh_event ON market_snapshots(event_id);
    """)
    conn.commit()
    conn.close()


# столбцы, которые пишет insert (в порядке VALUES)
_INSERT_COLS = (
    "event_id, sport_id, league, team1, team2, snap_dt_msk, is_prematch, "
    "game_minute, period, periods, score1, score2, "
    "win1_odds, draw_odds, win2_odds, dc_1x_odds, dc_12_odds, dc_x2_odds, "
    "fora_line, fora1_odds, fora2_odds, "
    "total_line, total_b_odds, total_m_odds, "
    "it1_line, it1_b_odds, it1_m_odds, it2_line, it2_b_odds, it2_m_odds, "
    "created_at"
)


def snapshot_exists(event_id: int, game_minute: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM market_snapshots WHERE event_id=? AND game_minute=?",
        (event_id, game_minute),
    ).fetchone()
    conn.close()
    return row is not None


def insert_snapshot(row: dict) -> int | None:
    """UNIQUE(event_id, game_minute) защищает от дублей минуты и prematch-строки."""
    ph = ", ".join(":" + c.strip() for c in _INSERT_COLS.split(","))
    conn = _conn()
    try:
        cur = conn.execute(
            f"INSERT INTO market_snapshots ({_INSERT_COLS}) VALUES ({ph})", row
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_event_rows(event_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM market_snapshots WHERE event_id=?", (event_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_results(snapshot_id: int, results: dict, final_score: str, final_total: int):
    sets = ", ".join(f"{k}=:{k}" for k in results)
    params = dict(results)
    params.update(sid=snapshot_id, fs=final_score, ft=final_total)
    conn = _conn()
    conn.execute(
        f"UPDATE market_snapshots SET {sets}, final_score=:fs, final_total=:ft WHERE id=:sid",
        params,
    )
    conn.commit()
    conn.close()


def all_rows() -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM market_snapshots ORDER BY event_id, game_minute"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def events_summary(limit: int = 15) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        """SELECT event_id, league, team1, team2, COUNT(*) AS minutes,
                  MAX(final_score) AS final_score
           FROM market_snapshots
           GROUP BY event_id
           ORDER BY MAX(id) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats() -> dict:
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    events = conn.execute("SELECT COUNT(DISTINCT event_id) FROM market_snapshots").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM market_snapshots WHERE final_score IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    return {"rows": total, "events": events, "resolved": resolved}


def clear_db():
    conn = _conn()
    conn.execute("DELETE FROM market_snapshots")
    conn.commit()
    conn.close()
    c2 = sqlite3.connect(DB_PATH)
    c2.execute("VACUUM")
    c2.close()
