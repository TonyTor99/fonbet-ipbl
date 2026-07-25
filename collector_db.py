"""Отдельная БД сборщика рынков IPBL (по одному файлу на лигу).

Одна строка = снимок матча на конкретной ИГРОВОЙ минуте.
По каждому рынку полного матча храним КРАЙНЮЮ (верхнюю) линию: значение линии,
кф обоих исходов и результат (В/П) каждого исхода — проставляется на финале матча.

Все функции принимают путь к файлу БД (`db`), т.к. каждая лига IPBL пишет в свой
файл (config.COLLECTOR_LEAGUES). Так поминутный объём одной лиги не мешает другим
и легко выгружается/чистится независимо.
"""
import sqlite3
from pathlib import Path

DIR = Path(__file__).parent

# Исходы (для результатов и колонок Excel). side -> человекочитаемо.
OUTCOMES = ["fora1", "fora2", "total_b", "total_m",
            "it1_b", "it1_m", "it2_b", "it2_m", "win1", "win2"]


def resolve_path(db: str) -> str:
    """Имя файла БД -> абсолютный путь рядом с проектом (абсолютный путь не трогаем)."""
    p = Path(db)
    return str(p if p.is_absolute() else DIR / p)


def _conn(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(resolve_path(db), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db: str):
    conn = _conn(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    INTEGER NOT NULL,
            league      TEXT NOT NULL,
            team1       TEXT NOT NULL,
            team2       TEXT NOT NULL,
            snap_dt_msk TEXT NOT NULL,        -- дата-время МСК снимка
            game_minute INTEGER NOT NULL,     -- игровая минута (timerSeconds // 60)
            quarter     INTEGER,              -- номер четверти (1..4+)
            score1      INTEGER NOT NULL,
            score2      INTEGER NOT NULL,

            fora_line     REAL,               -- линия форы со стороны К1 (напр. -7.5)
            fora1_odds    REAL,               -- кф Фора К1
            fora2_odds    REAL,               -- кф Фора К2
            total_line    REAL,
            total_b_odds  REAL,
            total_m_odds  REAL,
            it1_line      REAL,
            it1_b_odds    REAL,
            it1_m_odds    REAL,
            it2_line      REAL,
            it2_b_odds    REAL,
            it2_m_odds    REAL,
            win1_odds     REAL,
            win2_odds     REAL,

            r_fora1   TEXT, r_fora2   TEXT,    -- результат каждого исхода (В/П)
            r_total_b TEXT, r_total_m TEXT,
            r_it1_b   TEXT, r_it1_m   TEXT,
            r_it2_b   TEXT, r_it2_m   TEXT,
            r_win1    TEXT, r_win2    TEXT,

            final_score TEXT,
            final_total INTEGER,
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_unique
            ON market_snapshots(event_id, game_minute);
        CREATE INDEX IF NOT EXISTS idx_ms_event ON market_snapshots(event_id);
    """)
    conn.commit()
    conn.close()


def snapshot_exists(db: str, event_id: int, game_minute: int) -> bool:
    conn = _conn(db)
    row = conn.execute(
        "SELECT 1 FROM market_snapshots WHERE event_id=? AND game_minute=?",
        (event_id, game_minute),
    ).fetchone()
    conn.close()
    return row is not None


def insert_snapshot(db: str, row: dict) -> int | None:
    """UNIQUE(event_id, game_minute) защищает от дублей минуты."""
    cols = (
        "event_id, league, team1, team2, snap_dt_msk, game_minute, quarter, score1, score2, "
        "fora_line, fora1_odds, fora2_odds, total_line, total_b_odds, total_m_odds, "
        "it1_line, it1_b_odds, it1_m_odds, it2_line, it2_b_odds, it2_m_odds, win1_odds, win2_odds, "
        "created_at"
    )
    ph = ", ".join(":" + c.strip() for c in cols.split(","))
    conn = _conn(db)
    try:
        cur = conn.execute(
            f"INSERT INTO market_snapshots ({cols}) VALUES ({ph})", row
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_event_rows(db: str, event_id: int) -> list[dict]:
    conn = _conn(db)
    rows = conn.execute(
        "SELECT * FROM market_snapshots WHERE event_id=?", (event_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_results(db: str, snapshot_id: int, results: dict, final_score: str, final_total: int):
    """results: {'r_fora1': 'Выигрыш', ...}. Обновляет все результаты строки."""
    sets = ", ".join(f"{k}=:{k}" for k in results)
    params = dict(results)
    params.update(sid=snapshot_id, fs=final_score, ft=final_total)
    conn = _conn(db)
    conn.execute(
        f"UPDATE market_snapshots SET {sets}, final_score=:fs, final_total=:ft WHERE id=:sid",
        params,
    )
    conn.commit()
    conn.close()


def all_rows(db: str) -> list[dict]:
    conn = _conn(db)
    rows = conn.execute(
        "SELECT * FROM market_snapshots ORDER BY event_id, game_minute"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def events_summary(db: str, limit: int = 15) -> list[dict]:
    """Сводка по матчам: команды, число собранных игр. минут, итог (или None)."""
    conn = _conn(db)
    rows = conn.execute(
        """SELECT event_id, team1, team2, COUNT(*) AS minutes,
                  MAX(final_score) AS final_score
           FROM market_snapshots
           GROUP BY event_id
           ORDER BY MAX(id) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats(db: str) -> dict:
    conn = _conn(db)
    total = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    events = conn.execute("SELECT COUNT(DISTINCT event_id) FROM market_snapshots").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM market_snapshots WHERE final_score IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    return {"rows": total, "events": events, "resolved": resolved}


def clear_db(db: str):
    conn = _conn(db)
    conn.execute("DELETE FROM market_snapshots")
    conn.commit()
    conn.close()
    c2 = sqlite3.connect(resolve_path(db))
    c2.execute("VACUUM")
    c2.close()
