"""SQLite: один сигнал — одна строка. Никакой истории тоталов.

Таблицы:
  signals    — отправленные/зафиксированные сигналы (дедуп по strategy+event_id)
  bot_config — на стратегию: chat_id и окно работы (work_start/work_end "HH:MM")
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from config import BANKROLL_START, STAKE, THRESHOLD

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
            muted         INTEGER NOT NULL DEFAULT 0,  -- 1 = лига выключена кнопкой: строку пишем как обычно, но в TG не шлём и в статистику не берём
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
            threshold  REAL,                        -- порог формулы (NULL = дефолт из config.THRESHOLD)
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS league_config (
            sport_id   INTEGER PRIMARY KEY,         -- sportId лиги из config.LEAGUES
            enabled    INTEGER NOT NULL DEFAULT 1,  -- 1 = шлём в TG, 0 = выключена (пишем в БД, но в TG не шлём и в статистику не берём)
            threshold  REAL,                        -- запас формулы для ЭТОЙ лиги (NULL = дефолт config.THRESHOLD)
            updated_at TEXT
        );

        -- Стратегия шорт-хоккея: правила по лигам. Одно правило = лига + минута +
        -- исход (win1/draw/win2) + диапазон кф. Правил сколько угодно (одну лигу
        -- можно завести несколькими правилами). Читается движком sh_signals каждый
        -- цикл из общей WAL-базы — изменения из бота подхватываются без рестарта.
        CREATE TABLE IF NOT EXISTS sh_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            sport_name TEXT NOT NULL,               -- ПОЛНОЕ название лиги ("Шорт-хоккей. ...") для точного матча
            minute     INTEGER NOT NULL,            -- игровая минута сигнала (строго ==)
            outcome    TEXT NOT NULL,               -- 'win1' | 'draw' | 'win2'
            kf_min     REAL NOT NULL,
            kf_max     REAL NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        -- Отправленные сигналы стратегии шорт-хоккея (дедуп по rule_id+event_id).
        CREATE TABLE IF NOT EXISTS sh_strat_signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id      INTEGER NOT NULL,
            event_id     INTEGER NOT NULL,
            league       TEXT NOT NULL,
            team1        TEXT NOT NULL,
            team2        TEXT NOT NULL,
            rule_minute  INTEGER NOT NULL,          -- минута из правила
            fired_minute INTEGER NOT NULL,          -- фактическая игровая минута срабатывания
            outcome      TEXT NOT NULL,             -- win1 / draw / win2
            odds         REAL,                      -- кф исхода на момент сигнала
            kf_min       REAL NOT NULL,
            kf_max       REAL NOT NULL,
            score1       INTEGER NOT NULL,
            score2       INTEGER NOT NULL,
            chat_id      INTEGER,
            message_id   INTEGER,
            status       TEXT NOT NULL,             -- sent / no_chat
            result       TEXT,                      -- Выигрыш / Проигрыш / NULL
            final_score  TEXT,
            created_at   TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shsig_unique ON sh_strat_signals(rule_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_shsig_event ON sh_strat_signals(event_id);

        CREATE TABLE IF NOT EXISTS report_state (
            kind       TEXT PRIMARY KEY,            -- 'weekly' | 'monthly'
            marker     TEXT,                        -- за какой период уже отправлен ('YYYY-MM-DD' понедельника / 'YYYY-MM')
            updated_at TEXT
        );
    """)
    conn.commit()
    # миграции для уже существующей БД
    for col, ddl in [
        ("qualified", "ALTER TABLE signals ADD COLUMN qualified INTEGER NOT NULL DEFAULT 0"),
        ("in_window", "ALTER TABLE signals ADD COLUMN in_window INTEGER NOT NULL DEFAULT 1"),
        ("muted", "ALTER TABLE signals ADD COLUMN muted INTEGER NOT NULL DEFAULT 0"),
        ("totals_snapshot", "ALTER TABLE signals ADD COLUMN totals_snapshot TEXT"),
        ("line_move", "ALTER TABLE signals ADD COLUMN line_move TEXT"),
        ("q1", "ALTER TABLE signals ADD COLUMN q1 TEXT"),
        ("q2", "ALTER TABLE signals ADD COLUMN q2 TEXT"),
        ("q3", "ALTER TABLE signals ADD COLUMN q3 TEXT"),
        ("q4", "ALTER TABLE signals ADD COLUMN q4 TEXT"),
        ("line_prematch", "ALTER TABLE signals ADD COLUMN line_prematch REAL"),
        ("windows", "ALTER TABLE bot_config ADD COLUMN windows TEXT"),
        ("threshold", "ALTER TABLE bot_config ADD COLUMN threshold REAL"),
        ("lc_threshold", "ALTER TABLE league_config ADD COLUMN threshold REAL"),
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


def get_threshold(strategy: str = "signal_tm") -> float:
    """Порог формулы (2*сумма-линия <= порог). NULL в БД -> дефолт config.THRESHOLD."""
    conn = _conn()
    row = conn.execute("SELECT threshold FROM bot_config WHERE strategy=?", (strategy,)).fetchone()
    conn.close()
    if not row or row["threshold"] is None:
        return float(THRESHOLD)
    return float(row["threshold"])


def set_threshold(strategy: str, value: float):
    conn = _conn()
    _ensure_config_row(conn, strategy)
    conn.execute(
        "UPDATE bot_config SET threshold=?, updated_at=? WHERE strategy=?",
        (float(value), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), strategy),
    )
    conn.commit()
    conn.close()


def get_league_threshold(sport_id: int) -> float:
    """Запас формулы для КОНКРЕТНОЙ лиги. Приоритет:
    league_config.threshold (задан кнопкой на лигу) -> глобальный signal_tm -> config.THRESHOLD."""
    if sport_id is not None:
        conn = _conn()
        row = conn.execute(
            "SELECT threshold FROM league_config WHERE sport_id=?", (sport_id,)
        ).fetchone()
        conn.close()
        if row is not None and row["threshold"] is not None:
            return float(row["threshold"])
    return get_threshold("signal_tm")


def set_league_threshold(sport_id: int, value: float | None):
    """value=None -> сброс к дефолту (лига берёт глобальный/config.THRESHOLD)."""
    conn = _conn()
    conn.execute(
        "INSERT INTO league_config (sport_id, threshold, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(sport_id) DO UPDATE SET threshold=excluded.threshold, updated_at=excluded.updated_at",
        (sport_id, None if value is None else float(value),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


# --- вкл/выкл лиг ----------------------------------------------------------
# По умолчанию (строки в league_config нет) лига ВКЛЮЧЕНА. Выключенная лига
# пишется в БД как обычно (снимок, дорасчёт, прибыль), но сигнал в TG не
# отправляется и в статистику стратегии не идёт (столбец signals.muted=1).
# Читается парсером каждый цикл из общей WAL-базы — изменение из бота
# подхватывается сразу, без рестарта.

def league_enabled(sport_id: int) -> bool:
    if sport_id is None:
        return True
    conn = _conn()
    row = conn.execute("SELECT enabled FROM league_config WHERE sport_id=?", (sport_id,)).fetchone()
    conn.close()
    if row is None:
        return True
    return bool(row["enabled"])


def set_league_enabled(sport_id: int, enabled: bool):
    conn = _conn()
    conn.execute(
        "INSERT INTO league_config (sport_id, enabled, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(sport_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
        (sport_id, 1 if enabled else 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def toggle_league(sport_id: int) -> bool:
    """Переключает статус лиги, возвращает новое состояние (True=включена)."""
    new_state = not league_enabled(sport_id)
    set_league_enabled(sport_id, new_state)
    return new_state


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
                 half_total, formula_value, qualified, in_window, muted, totals_snapshot,
                 fixed_score1, fixed_score2, fixed_quarters, q1, q2, q3, q4,
                 line_move, line_prematch,
                 chat_id, message_id, status, result, final_score, final_total, profit, created_at)
            VALUES
                (:strategy, :event_id, :league, :division, :team1, :team2, :side, :line, :odds,
                 :half_total, :formula_value, :qualified, :in_window, :muted, :totals_snapshot,
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
    """Статистика стратегии. Считаются ТОЛЬКО реально отправленные сигналы: в окне
    работы (in_window=1) и не заглушённые выключенной лигой (muted=0); перерывы во
    время паузы и матчи выключенных лиг в статистику не идут. `matches` = снимки
    перерыва (в окне), `signals` = прошедшие формулу. Win/loss/прибыль — по прошедшим
    (qualified=1)."""
    conn = _conn()
    q = "SELECT COUNT(*) FROM signals WHERE strategy=? AND in_window=1 AND muted=0"
    qq = q + " AND qualified=1"
    total   = conn.execute(q, (strategy,)).fetchone()[0]
    signals = conn.execute(qq, (strategy,)).fetchone()[0]
    wins    = conn.execute(qq + " AND result='Выигрыш'", (strategy,)).fetchone()[0]
    losses  = conn.execute(qq + " AND result='Проигрыш'", (strategy,)).fetchone()[0]
    no_res  = conn.execute(qq + " AND result IS NULL", (strategy,)).fetchone()[0]
    void    = conn.execute(qq + " AND line IS NULL", (strategy,)).fetchone()[0]
    profit  = conn.execute(
        "SELECT COALESCE(SUM(profit),0) FROM signals WHERE strategy=? AND qualified=1 AND in_window=1 AND muted=0",
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


def bot_stats_by_league(strategy: str) -> dict[str, dict]:
    """То же, что bot_stats, но с разбивкой по лигам (GROUP BY league).
    Возвращает {league_name: {...та же структура, что у bot_stats...}}.
    Учитываются только реально отправленные сигналы (in_window=1, muted=0)."""
    conn = _conn()
    base = "FROM signals WHERE strategy=? AND in_window=1 AND muted=0"
    rows = conn.execute(f"""
        SELECT
            league,
            COUNT(*)                                          AS matches,
            SUM(CASE WHEN qualified=1 THEN 1 ELSE 0 END)      AS signals,
            SUM(CASE WHEN qualified=1 AND result='Выигрыш'  THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN qualified=1 AND result='Проигрыш' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN qualified=1 AND result IS NULL     THEN 1 ELSE 0 END) AS no_result,
            SUM(CASE WHEN qualified=1 AND line IS NULL       THEN 1 ELSE 0 END) AS void,
            COALESCE(SUM(CASE WHEN qualified=1 THEN profit ELSE 0 END), 0)      AS profit
        {base}
        GROUP BY league
    """, (strategy,)).fetchall()
    conn.close()
    out: dict[str, dict] = {}
    for r in rows:
        wins, losses = r["wins"], r["losses"]
        settled = wins + losses
        winrate = (wins / settled * 100) if settled else 0.0
        staked = settled * STAKE
        profit = r["profit"]
        roi = (profit / staked * 100) if staked else 0.0
        out[r["league"]] = {
            "matches": r["matches"], "signals": r["signals"], "wins": wins,
            "losses": losses, "no_result": r["no_result"], "void": r["void"],
            "profit": profit, "balance": BANKROLL_START + profit,
            "winrate": winrate, "staked": staked, "roi": roi,
        }
    return out


def active_count() -> int:
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM signals WHERE result IS NULL AND line IS NOT NULL").fetchone()[0]
    conn.close()
    return n


# --- отчёты о прибыли (недельный/месячный) ---------------------------------
# Считаем ту же прибыль, что и статистика стратегии «Сигнал ТМ»: только реально
# сыгранные сигналы (qualified=1, в окне работы, лига включена). Группировка по
# дню создания сигнала (created_at пишется в МСК) — это день матча.

_PROFIT_WHERE = ("strategy='signal_tm' AND qualified=1 AND in_window=1 AND muted=0")


def profit_by_day(start: str, end: str) -> dict[str, float]:
    """{'YYYY-MM-DD': прибыль_₽} по дням в диапазоне [start, end] включительно.
    Дни без сигналов в словаре отсутствуют (заполняются нулём вызывающей стороной)."""
    conn = _conn()
    rows = conn.execute(
        f"SELECT date(created_at) AS d, COALESCE(SUM(profit), 0) AS p "
        f"FROM signals WHERE {_PROFIT_WHERE} AND date(created_at) BETWEEN ? AND ? "
        f"GROUP BY d", (start, end)).fetchall()
    conn.close()
    return {r["d"]: r["p"] for r in rows}


def profit_total(start: str, end: str) -> float:
    """Суммарная прибыль ₽ за диапазон дат [start, end] включительно."""
    conn = _conn()
    v = conn.execute(
        f"SELECT COALESCE(SUM(profit), 0) FROM signals "
        f"WHERE {_PROFIT_WHERE} AND date(created_at) BETWEEN ? AND ?",
        (start, end)).fetchone()[0]
    conn.close()
    return v


def get_report_marker(kind: str) -> str | None:
    conn = _conn()
    row = conn.execute("SELECT marker FROM report_state WHERE kind=?", (kind,)).fetchone()
    conn.close()
    return row["marker"] if row else None


def set_report_marker(kind: str, marker: str):
    conn = _conn()
    conn.execute(
        "INSERT INTO report_state (kind, marker, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(kind) DO UPDATE SET marker=excluded.marker, updated_at=excluded.updated_at",
        (kind, marker, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def clear_db():
    conn = _conn()
    conn.execute("DELETE FROM signals")
    conn.commit()
    conn.close()
    conn2 = sqlite3.connect(DB_PATH)
    conn2.execute("VACUUM")
    conn2.close()


# ===========================================================================
# Стратегия шорт-хоккея: правила по лигам + отправленные сигналы.
# ===========================================================================
SH_OUTCOMES = ("win1", "draw", "win2")


def sh_get_rules() -> list[dict]:
    """Все правила стратегии (новые снизу — по порядку добавления)."""
    conn = _conn()
    rows = conn.execute("SELECT * FROM sh_rules ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sh_get_rule(rule_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM sh_rules WHERE id=?", (rule_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def sh_add_rule(sport_name: str, minute: int, outcome: str,
                kf_min: float, kf_max: float) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO sh_rules (sport_name, minute, outcome, kf_min, kf_max, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (sport_name, int(minute), outcome, float(kf_min), float(kf_max),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def sh_update_rule(rule_id: int, minute: int, outcome: str,
                   kf_min: float, kf_max: float):
    conn = _conn()
    conn.execute(
        "UPDATE sh_rules SET minute=?, outcome=?, kf_min=?, kf_max=? WHERE id=?",
        (int(minute), outcome, float(kf_min), float(kf_max), rule_id),
    )
    conn.commit()
    conn.close()


def sh_delete_rule(rule_id: int):
    conn = _conn()
    conn.execute("DELETE FROM sh_rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()


def sh_toggle_rule(rule_id: int) -> bool:
    """Переключает вкл/выкл правила, возвращает новое состояние."""
    conn = _conn()
    row = conn.execute("SELECT enabled FROM sh_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    new_state = 0 if row["enabled"] else 1
    conn.execute("UPDATE sh_rules SET enabled=? WHERE id=?", (new_state, rule_id))
    conn.commit()
    conn.close()
    return bool(new_state)


def sh_signal_exists(rule_id: int, event_id: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM sh_strat_signals WHERE rule_id=? AND event_id=?",
        (rule_id, event_id),
    ).fetchone()
    conn.close()
    return row is not None


def sh_insert_signal(sig: dict) -> int | None:
    """UNIQUE(rule_id, event_id) защищает от повторного сигнала по правилу на матч."""
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO sh_strat_signals
                (rule_id, event_id, league, team1, team2, rule_minute, fired_minute,
                 outcome, odds, kf_min, kf_max, score1, score2,
                 chat_id, message_id, status, result, final_score, created_at)
            VALUES
                (:rule_id, :event_id, :league, :team1, :team2, :rule_minute, :fired_minute,
                 :outcome, :odds, :kf_min, :kf_max, :score1, :score2,
                 :chat_id, :message_id, :status, :result, :final_score, :created_at)
        """, sig)
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def sh_get_signals_for_event(event_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM sh_strat_signals WHERE event_id=?", (event_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sh_update_signal_result(signal_id: int, result: str | None, final_score: str):
    conn = _conn()
    conn.execute(
        "UPDATE sh_strat_signals SET result=?, final_score=? WHERE id=?",
        (result, final_score, signal_id),
    )
    conn.commit()
    conn.close()


def sh_rule_stats(rule_id: int) -> dict:
    """Статистика по правилу: отправлено сигналов, побед/поражений, прибыль/ROI.
    Прибыль виртуальная — как у баскетбольных стратегий: ставка STAKE на каждый
    сигнал, выигрыш = STAKE*(кф−1), проигрыш = −STAKE. Считаем только реально
    отправленные (status='sent')."""
    conn = _conn()
    base = "FROM sh_strat_signals WHERE rule_id=? AND status='sent'"
    total  = conn.execute(f"SELECT COUNT(*) {base}", (rule_id,)).fetchone()[0]
    wins   = conn.execute(f"SELECT COUNT(*) {base} AND result='Выигрыш'", (rule_id,)).fetchone()[0]
    losses = conn.execute(f"SELECT COUNT(*) {base} AND result='Проигрыш'", (rule_id,)).fetchone()[0]
    no_res = conn.execute(f"SELECT COUNT(*) {base} AND result IS NULL", (rule_id,)).fetchone()[0]
    sum_win_odds = conn.execute(
        f"SELECT COALESCE(SUM(odds), 0) {base} AND result='Выигрыш'", (rule_id,)).fetchone()[0]
    conn.close()
    settled = wins + losses
    winrate = (wins / settled * 100) if settled else 0.0
    # STAKE*(кф−1) по выигрышам − STAKE по проигрышам = STAKE*(Σкф_побед − wins − losses)
    profit = STAKE * (sum_win_odds - wins - losses)
    staked = settled * STAKE
    roi = (profit / staked * 100) if staked else 0.0
    return {"signals": total, "wins": wins, "losses": losses,
            "no_result": no_res, "winrate": winrate,
            "profit": profit, "staked": staked, "roi": roi}


def sh_overall_stats() -> dict:
    """Свод по всей стратегии хоккея (сумма по правилам)."""
    tot = {"signals": 0, "wins": 0, "losses": 0, "no_result": 0,
           "profit": 0.0, "staked": 0.0}
    for r in sh_get_rules():
        st = sh_rule_stats(r["id"])
        tot["signals"] += st["signals"]; tot["wins"] += st["wins"]
        tot["losses"] += st["losses"]; tot["no_result"] += st["no_result"]
        tot["profit"] += st["profit"]; tot["staked"] += st["staked"]
    settled = tot["wins"] + tot["losses"]
    tot["winrate"] = (tot["wins"] / settled * 100) if settled else 0.0
    tot["roi"] = (tot["profit"] / tot["staked"] * 100) if tot["staked"] else 0.0
    tot["balance"] = BANKROLL_START + tot["profit"]
    return tot


def sh_clear_signals():
    conn = _conn()
    conn.execute("DELETE FROM sh_strat_signals")
    conn.commit()
    conn.close()
