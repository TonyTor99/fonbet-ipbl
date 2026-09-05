"""Отдельная БД сборщика рынков киберфутбола FC 26.

Одна строка market_snapshots = снимок матча на конкретной 5-минутной отметке
ИГРОВОГО времени (плюс одна строка ДО начала матча, game_minute = PREMATCH_MINUTE).

Рынки с ОДНОЙ линией (1X2, двойные шансы, основная фора, обе забьют) лежат прямо
в market_snapshots. Тоталы и индивидуальные тоталы имеют по НЕСКОЛЬКО линий
одновременно (0.5 / 1.5 / 2.5 ...), поэтому вынесены в дочернюю таблицу
total_lines (снимок × вид × линия). Результат каждого исхода (В/П/Возврат)
проставляется на финале матча.

Файл БД отдельный от остальных сборщиков (cyber_config.CYBER_COLLECTOR_DB).
"""
import os
import sqlite3
from pathlib import Path

from cyber_config import CYBER_COLLECTOR_DB

DB_PATH = os.getenv("CYBER_COLLECTOR_DB_PATH", str(Path(__file__).parent / CYBER_COLLECTOR_DB))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
            game_minute INTEGER NOT NULL,     -- 5-мин отметка игрового времени; prematch = -1
            ts          INTEGER,              -- реальный timerSeconds на момент снимка
            half        INTEGER,              -- тайм (1/2), None до начала
            half_score  TEXT,                 -- счёт ТЕКУЩЕГО тайма, напр. "1-0"
            periods     TEXT,                 -- счёт завершённых таймов, напр. "0-1 1-0"
            score1      INTEGER NOT NULL,     -- общий счёт К1
            score2      INTEGER NOT NULL,     -- общий счёт К2

            -- Исход 1X2
            win1_odds   REAL, draw_odds  REAL, win2_odds  REAL,
            -- Двойные шансы
            dc_1x_odds  REAL, dc_12_odds REAL, dc_x2_odds REAL,
            -- Фора (основная линия, со стороны К1)
            fora_line   REAL, fora1_odds REAL, fora2_odds REAL,
            -- Обе забьют
            btts_yes_odds REAL, btts_no_odds REAL,

            -- Результаты одиночных исходов (Выигрыш/Проигрыш/Возврат)
            r_win1  TEXT, r_draw  TEXT, r_win2  TEXT,
            r_1x    TEXT, r_12    TEXT, r_x2    TEXT,
            r_fora1 TEXT, r_fora2 TEXT,
            r_btts_yes TEXT, r_btts_no TEXT,

            final_score TEXT,
            final_total INTEGER,
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cyber_unique
            ON market_snapshots(event_id, game_minute);
        CREATE INDEX IF NOT EXISTS idx_cyber_event ON market_snapshots(event_id);

        CREATE TABLE IF NOT EXISTS total_lines (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            event_id    INTEGER NOT NULL,
            kind        TEXT NOT NULL,        -- 'total' | 'it1' | 'it2'
            line        REAL NOT NULL,
            b_odds      REAL,                 -- Больше
            m_odds      REAL,                 -- Меньше
            r_b         TEXT,                 -- результат Больше
            r_m         TEXT,                 -- результат Меньше
            FOREIGN KEY(snapshot_id) REFERENCES market_snapshots(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cyber_tl_unique
            ON total_lines(snapshot_id, kind, line);
        CREATE INDEX IF NOT EXISTS idx_cyber_tl_snap ON total_lines(snapshot_id);
    """)
    conn.commit()
    conn.close()


# столбцы market_snapshots, которые пишет insert (в порядке VALUES)
_INSERT_COLS = (
    "event_id, sport_id, league, team1, team2, snap_dt_msk, is_prematch, "
    "game_minute, ts, half, half_score, periods, score1, score2, "
    "win1_odds, draw_odds, win2_odds, dc_1x_odds, dc_12_odds, dc_x2_odds, "
    "fora_line, fora1_odds, fora2_odds, btts_yes_odds, btts_no_odds, "
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


def insert_snapshot(row: dict, total_lines: list[dict] | None = None) -> int | None:
    """Пишет строку снимка + её линии тоталов одной транзакцией.

    UNIQUE(event_id, game_minute) защищает от дублей отметки и prematch-строки.
    total_lines — список dict(kind, line, b_odds, m_odds).
    """
    ph = ", ".join(":" + c.strip() for c in _INSERT_COLS.split(","))
    conn = _conn()
    try:
        cur = conn.execute(
            f"INSERT INTO market_snapshots ({_INSERT_COLS}) VALUES ({ph})", row
        )
        sid = cur.lastrowid
        for tl in (total_lines or []):
            conn.execute(
                "INSERT OR IGNORE INTO total_lines "
                "(snapshot_id, event_id, kind, line, b_odds, m_odds) "
                "VALUES (?,?,?,?,?,?)",
                (sid, row["event_id"], tl["kind"], tl["line"],
                 tl.get("b_odds"), tl.get("m_odds")),
            )
        conn.commit()
        return sid
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


def get_total_lines(snapshot_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM total_lines WHERE snapshot_id=?", (snapshot_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_results(snapshot_id: int, results: dict, final_score: str, final_total: int):
    """Обновляет одиночные исходы строки снимка."""
    conn = _conn()
    if results:
        sets = ", ".join(f"{k}=:{k}" for k in results)
        params = dict(results)
        params.update(sid=snapshot_id, fs=final_score, ft=final_total)
        conn.execute(
            f"UPDATE market_snapshots SET {sets}, final_score=:fs, final_total=:ft WHERE id=:sid",
            params,
        )
    else:
        conn.execute(
            "UPDATE market_snapshots SET final_score=?, final_total=? WHERE id=?",
            (final_score, final_total, snapshot_id),
        )
    conn.commit()
    conn.close()


def update_total_result(line_id: int, r_b: str, r_m: str):
    conn = _conn()
    conn.execute(
        "UPDATE total_lines SET r_b=?, r_m=? WHERE id=?", (r_b, r_m, line_id)
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


def all_total_lines() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM total_lines").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def distinct_lines() -> dict[str, list[float]]:
    """{kind: [линии по возрастанию]} по всем снимкам — для колонок Excel."""
    conn = _conn()
    rows = conn.execute(
        "SELECT DISTINCT kind, line FROM total_lines ORDER BY kind, line"
    ).fetchall()
    conn.close()
    out: dict[str, list[float]] = {}
    for r in rows:
        out.setdefault(r["kind"], []).append(r["line"])
    return out


def events_summary(limit: int = 15) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        """SELECT event_id, league, team1, team2, COUNT(*) AS marks,
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
    conn.execute("DELETE FROM total_lines")
    conn.execute("DELETE FROM market_snapshots")
    conn.commit()
    conn.close()
    c2 = sqlite3.connect(DB_PATH)
    c2.execute("VACUUM")
    c2.close()
