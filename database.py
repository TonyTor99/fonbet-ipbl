"""SQLite: один сигнал — одна строка. Никакой истории тоталов.

Таблицы:
  signals    — отправленные/зафиксированные сигналы (дедуп по strategy+event_id)
  bot_config — на стратегию: chat_id и окно работы (work_start/work_end "HH:MM")
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from config import (BANKROLL_START, STAKE, THRESHOLD,
                    IPBL_DIV_ORDER, ipbl_div_key)

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

        -- Наборы стратегии IPBL (signal_tm): у каждого свой чат + запасы по 4
        -- дивизионам (NULL = дивизион выключен для набора) + график + дни недели.
        CREATE TABLE IF NOT EXISTS ipbl_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            enabled     INTEGER NOT NULL DEFAULT 1,
            chat_id     INTEGER,                     -- свой чат рассылки (NULL = не задан, не шлём)
            zapas_pro    REAL, zapas_prow REAL,      -- запасы формулы по дивизионам (NULL = дивизион выкл)
            zapas_prime  REAL, zapas_primew REAL,
            windows     TEXT,                        -- 'HH:MM-HH:MM,...' (NULL = круглосуточно)
            weekdays    TEXT,                        -- CSV '0..6' Пн..Вс (NULL/'' = все дни)
            created_at  TEXT NOT NULL
        );
        -- Белый список пар набора (общий по набору; пусто = все пары).
        CREATE TABLE IF NOT EXISTS ipbl_rule_pairs (
            rule_id INTEGER NOT NULL,
            team_a  TEXT NOT NULL, team_b TEXT NOT NULL,
            PRIMARY KEY (rule_id, team_a, team_b),
            FOREIGN KEY (rule_id) REFERENCES ipbl_rules(id) ON DELETE CASCADE
        );
        -- Чёрный список пар ПО ДИВИЗИОНАМ (режет поверх белого).
        CREATE TABLE IF NOT EXISTS ipbl_rule_blacklist (
            rule_id INTEGER NOT NULL,
            div     TEXT NOT NULL,                   -- pro|prow|prime|primew
            team_a  TEXT NOT NULL, team_b TEXT NOT NULL,
            PRIMARY KEY (rule_id, div, team_a, team_b),
            FOREIGN KEY (rule_id) REFERENCES ipbl_rules(id) ON DELETE CASCADE
        );
        -- Реально отправленные сигналы наборов (дедуп + отчёты). Исторические снимки
        -- перерыва для гипотетической статистики — в основной таблице signals.
        CREATE TABLE IF NOT EXISTS ipbl_sent (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id     INTEGER NOT NULL,
            event_id    INTEGER NOT NULL,
            league      TEXT, div TEXT,
            team1       TEXT, team2 TEXT,
            line        REAL, odds REAL, formula_value REAL,
            score1      INTEGER, score2 INTEGER, quarters TEXT,
            chat_id     INTEGER, message_id INTEGER, status TEXT,
            result      TEXT, final_score TEXT, final_total INTEGER, profit REAL,
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ipbl_sent_unique ON ipbl_sent(rule_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_ipbl_sent_event ON ipbl_sent(event_id);

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

        -- Отдельная стратегия шорт-хоккея на ТОТАЛАХ. Одно правило = лига + минута
        -- (строго ==) + сторона (over/under) + диапазон ЛИНИИ тотала [line_min,
        -- line_max]. Срабатывание: на заданной минуте линия тотала матча попала в
        -- диапазон — шлём сигнал по выбранной стороне (кф берётся какой есть).
        CREATE TABLE IF NOT EXISTS sh_total_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            sport_name TEXT NOT NULL,               -- ПОЛНОЕ название лиги
            minute     INTEGER NOT NULL,            -- игровая минута сигнала (строго ==)
            side       TEXT NOT NULL,               -- 'over' (ТБ) | 'under' (ТМ)
            line_min   REAL NOT NULL,               -- нижняя граница линии тотала
            line_max   REAL NOT NULL,               -- верхняя граница линии тотала
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        -- Отправленные сигналы стратегии тоталов (дедуп по rule_id+event_id).
        CREATE TABLE IF NOT EXISTS sh_total_signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id      INTEGER NOT NULL,
            event_id     INTEGER NOT NULL,
            league       TEXT NOT NULL,
            team1        TEXT NOT NULL,
            team2        TEXT NOT NULL,
            rule_minute  INTEGER NOT NULL,          -- минута из правила
            fired_minute INTEGER NOT NULL,          -- фактическая игровая минута срабатывания
            side         TEXT NOT NULL,             -- over / under
            line         REAL,                      -- линия тотала на момент сигнала
            odds         REAL,                      -- кф стороны на момент сигнала
            line_min     REAL NOT NULL,
            line_max     REAL NOT NULL,
            score1       INTEGER NOT NULL,
            score2       INTEGER NOT NULL,
            chat_id      INTEGER,
            message_id   INTEGER,
            status       TEXT NOT NULL,             -- sent / no_chat
            result       TEXT,                      -- Выигрыш / Проигрыш / Возврат / NULL
            final_score  TEXT,
            final_total  INTEGER,
            created_at   TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shtot_unique ON sh_total_signals(rule_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_shtot_event ON sh_total_signals(event_id);

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


def daily_stats(day: str) -> dict:
    """Счётчики исходов (В/П/Возврат) и прибыль стратегии signal_tm за один день
    ('YYYY-MM-DD'). Тот же фильтр, что в отчётах о прибыли: сыгранные сигналы в
    окне работы при включённой лиге, группировка по дню создания (день матча, МСК)."""
    conn = _conn()
    base = f"FROM signals WHERE {_PROFIT_WHERE} AND date(created_at)=?"
    wins   = conn.execute(f"SELECT COUNT(*) {base} AND result='Выигрыш'", (day,)).fetchone()[0]
    losses = conn.execute(f"SELECT COUNT(*) {base} AND result='Проигрыш'", (day,)).fetchone()[0]
    pushes = conn.execute(f"SELECT COUNT(*) {base} AND result='Возврат'", (day,)).fetchone()[0]
    profit = conn.execute(f"SELECT COALESCE(SUM(profit), 0) {base}", (day,)).fetchone()[0]
    conn.close()
    return {"wins": wins, "losses": losses, "pushes": pushes, "profit": profit}


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
# Наборы стратегии IPBL (signal_tm): чат/запасы по дивизионам/график/дни/пары/ЧС.
# Хранилище — ipbl.db; список пар и гипотетическая статистика — из таблицы signals
# (каждый перерыв пишется туда как снимок с line/odds/formula_value/result).
# ===========================================================================

def _ipbl_norm(t1: str, t2: str) -> tuple[str, str]:
    """Нормализованная пара (порядок команд не важен)."""
    a, b = (t1 or "").strip(), (t2 or "").strip()
    return tuple(sorted((a, b), key=str.lower))


def ipbl_pair_label(a: str, b: str) -> str:
    return f"{a} — {b}"


def _ipbl_zapas_col(div: str) -> str:
    if div not in IPBL_DIV_ORDER:
        raise ValueError(f"bad div {div}")
    return f"zapas_{div}"


# --- наборы ----------------------------------------------------------------

def ipbl_get_rules() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM ipbl_rules ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ipbl_get_rule(rule_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM ipbl_rules WHERE id=?", (rule_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def ipbl_rules_count() -> int:
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM ipbl_rules").fetchone()[0]
    conn.close()
    return n


def ipbl_add_rule(chat_id: int | None = None, zapas: dict | None = None,
                  windows: str | None = None, weekdays: str | None = None) -> int:
    zapas = zapas or {}
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO ipbl_rules (enabled, chat_id, zapas_pro, zapas_prow, zapas_prime, "
        "zapas_primew, windows, weekdays, created_at) VALUES (1,?,?,?,?,?,?,?,?)",
        (chat_id, zapas.get("pro"), zapas.get("prow"), zapas.get("prime"),
         zapas.get("primew"), windows, weekdays,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def ipbl_update_rule_chat(rule_id: int, chat_id: int | None):
    conn = _conn()
    conn.execute("UPDATE ipbl_rules SET chat_id=? WHERE id=?", (chat_id, rule_id))
    conn.commit(); conn.close()


def ipbl_update_rule_windows(rule_id: int, windows: str | None):
    conn = _conn()
    conn.execute("UPDATE ipbl_rules SET windows=? WHERE id=?", (windows, rule_id))
    conn.commit(); conn.close()


def ipbl_update_rule_weekdays(rule_id: int, weekdays: str | None):
    conn = _conn()
    conn.execute("UPDATE ipbl_rules SET weekdays=? WHERE id=?", (weekdays or None, rule_id))
    conn.commit(); conn.close()


def ipbl_set_zapas(rule_id: int, div: str, value):
    """value = число (запас, обычно отрицательный) или None (дивизион выключить)."""
    col = _ipbl_zapas_col(div)
    conn = _conn()
    conn.execute(f"UPDATE ipbl_rules SET {col}=? WHERE id=?", (value, rule_id))
    conn.commit(); conn.close()


def ipbl_toggle_rule(rule_id: int) -> bool:
    conn = _conn()
    row = conn.execute("SELECT enabled FROM ipbl_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        conn.close(); return False
    new = 0 if row["enabled"] else 1
    conn.execute("UPDATE ipbl_rules SET enabled=? WHERE id=?", (new, rule_id))
    conn.commit(); conn.close()
    return bool(new)


def ipbl_delete_rule(rule_id: int):
    conn = _conn()
    conn.execute("DELETE FROM ipbl_rule_pairs WHERE rule_id=?", (rule_id,))
    conn.execute("DELETE FROM ipbl_rule_blacklist WHERE rule_id=?", (rule_id,))
    conn.execute("DELETE FROM ipbl_rules WHERE id=?", (rule_id,))
    conn.commit(); conn.close()


# --- белый список пар ------------------------------------------------------

def ipbl_get_pairs(rule_id: int) -> set:
    conn = _conn()
    rows = conn.execute(
        "SELECT team_a, team_b FROM ipbl_rule_pairs WHERE rule_id=?", (rule_id,)).fetchall()
    conn.close()
    return {(r["team_a"], r["team_b"]) for r in rows}


def ipbl_count_pairs(rule_id: int) -> int:
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM ipbl_rule_pairs WHERE rule_id=?", (rule_id,)).fetchone()[0]
    conn.close()
    return n


def ipbl_toggle_pair(rule_id: int, a: str, b: str) -> bool:
    conn = _conn()
    row = conn.execute("SELECT 1 FROM ipbl_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
                       (rule_id, a, b)).fetchone()
    if row:
        conn.execute("DELETE FROM ipbl_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
                     (rule_id, a, b)); new = False
    else:
        conn.execute("INSERT OR IGNORE INTO ipbl_rule_pairs (rule_id, team_a, team_b) VALUES (?,?,?)",
                     (rule_id, a, b)); new = True
    conn.commit(); conn.close()
    return new


def ipbl_set_pairs(rule_id: int, pairs: list, enabled: bool):
    conn = _conn()
    if enabled:
        conn.executemany("INSERT OR IGNORE INTO ipbl_rule_pairs (rule_id, team_a, team_b) VALUES (?,?,?)",
                         [(rule_id, a, b) for a, b in pairs])
    else:
        conn.executemany("DELETE FROM ipbl_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
                         [(rule_id, a, b) for a, b in pairs])
    conn.commit(); conn.close()


# --- чёрный список пар по дивизиону ----------------------------------------

def ipbl_get_blacklist(rule_id: int, div: str) -> set:
    conn = _conn()
    rows = conn.execute(
        "SELECT team_a, team_b FROM ipbl_rule_blacklist WHERE rule_id=? AND div=?",
        (rule_id, div)).fetchall()
    conn.close()
    return {(r["team_a"], r["team_b"]) for r in rows}


def ipbl_count_blacklist(rule_id: int, div: str) -> int:
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM ipbl_rule_blacklist WHERE rule_id=? AND div=?",
                     (rule_id, div)).fetchone()[0]
    conn.close()
    return n


def ipbl_toggle_blacklist(rule_id: int, div: str, a: str, b: str) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM ipbl_rule_blacklist WHERE rule_id=? AND div=? AND team_a=? AND team_b=?",
        (rule_id, div, a, b)).fetchone()
    if row:
        conn.execute("DELETE FROM ipbl_rule_blacklist WHERE rule_id=? AND div=? AND team_a=? AND team_b=?",
                     (rule_id, div, a, b)); new = False
    else:
        conn.execute("INSERT OR IGNORE INTO ipbl_rule_blacklist (rule_id, div, team_a, team_b) "
                     "VALUES (?,?,?,?)", (rule_id, div, a, b)); new = True
    conn.commit(); conn.close()
    return new


def ipbl_set_blacklist(rule_id: int, div: str, pairs: list, enabled: bool):
    conn = _conn()
    if enabled:
        conn.executemany("INSERT OR IGNORE INTO ipbl_rule_blacklist (rule_id, div, team_a, team_b) "
                         "VALUES (?,?,?,?)", [(rule_id, div, a, b) for a, b in pairs])
    else:
        conn.executemany("DELETE FROM ipbl_rule_blacklist WHERE rule_id=? AND div=? AND team_a=? AND team_b=?",
                         [(rule_id, div, a, b) for a, b in pairs])
    conn.commit(); conn.close()


def ipbl_pair_allowed(rule_id: int, div: str, pair: tuple) -> bool:
    """Белый список (пусто = все пары) + чёрный список дивизиона поверх (исключает)."""
    white = ipbl_get_pairs(rule_id)
    if white and pair not in white:
        return False
    if pair in ipbl_get_blacklist(rule_id, div):
        return False
    return True


# --- источник пар/команд (из истории перерывов signals, strategy='signal_tm') ---

def _ipbl_source_rows() -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT team1, team2, division, league FROM signals WHERE strategy='signal_tm' "
        "AND team1 IS NOT NULL AND team1<>'' AND team2 IS NOT NULL AND team2<>''").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ipbl_distinct_pairs(div: str | None = None) -> list[tuple]:
    pairs = set()
    for r in _ipbl_source_rows():
        if div is not None and ipbl_div_key(r["division"], r["league"]) != div:
            continue
        pairs.add(_ipbl_norm(r["team1"], r["team2"]))
    return sorted(pairs, key=lambda p: (p[0].lower(), p[1].lower()))


def ipbl_distinct_teams(div: str | None = None) -> list[str]:
    teams = set()
    for r in _ipbl_source_rows():
        if div is not None and ipbl_div_key(r["division"], r["league"]) != div:
            continue
        if r["team1"]:
            teams.add(r["team1"].strip())
        if r["team2"]:
            teams.add(r["team2"].strip())
    return sorted(teams, key=str.lower)


# --- гипотетическая статистика набора по истории перерывов (signals) --------

def _ipbl_empty_stats() -> dict:
    return {"signals": 0, "wins": 0, "losses": 0, "no_result": 0,
            "winrate": 0.0, "profit": 0.0, "staked": 0.0, "roi": 0.0}


def _ipbl_in_schedule(rule: dict, created_at: str) -> bool:
    """Попадает ли время снимка (created_at 'YYYY-MM-DD HH:MM:SS', МСК) в график
    работы набора: день недели в наборе (пусто/все 7 = все дни) И время в одном из
    окон (пусто = круглосуточно). Для гипотетической статистики — чтобы она совпадала
    с тем, что набор РЕАЛЬНО отправляет (и с бэктестом веб-панели)."""
    if not created_at:
        return True
    wd = rule.get("weekdays")
    days = {int(x) for x in (wd or "").split(",") if x.strip().isdigit()}
    if days and len(days) < 7:
        try:
            d = datetime.strptime(created_at[:10], "%Y-%m-%d")
        except ValueError:
            d = None
        if d is not None and d.weekday() not in days:
            return False
    wins = rule.get("windows")
    if wins:
        hm = created_at[11:16]                      # 'HH:MM' (строки zero-padded → лексикосравнение ок)
        if len(hm) < 5:
            return True
        inw = False
        for part in wins.split(","):
            part = part.strip()
            if "-" not in part:
                continue
            s, e = [x.strip() for x in part.split("-", 1)]
            if s <= e:
                if s <= hm <= e:
                    inw = True; break
            else:                                    # окно через полночь
                if hm >= s or hm <= e:
                    inw = True; break
        if not inw:
            return False
    return True


def ipbl_rule_stats_from_history(rule: dict) -> dict:
    """Бэктест набора по истории перерывов (таблица signals): берём рассчитанные
    снимки ТМ (result В/П, есть линия и кф), для каждого определяем дивизион, берём
    запас набора по этому дивизиону (None = дивизион выключен → пропуск), проверяем
    формулу (formula_value <= запас), фильтр пар (белый+чёрный) И график/дни набора
    (как при реальной отправке — чтобы совпадало с веб-панелью). Прибыль флэт STAKE."""
    conn = _conn()
    rows = conn.execute(
        "SELECT team1, team2, division, league, formula_value, odds, result, created_at "
        "FROM signals WHERE strategy='signal_tm' AND result IN ('Выигрыш','Проигрыш') "
        "AND line IS NOT NULL AND odds IS NOT NULL AND formula_value IS NOT NULL").fetchall()
    conn.close()
    white = ipbl_get_pairs(rule["id"])
    blk = {d: ipbl_get_blacklist(rule["id"], d) for d in IPBL_DIV_ORDER}
    wins = losses = 0
    profit = 0.0
    for r in rows:
        div = ipbl_div_key(r["division"], r["league"])
        zap = rule.get(f"zapas_{div}") if div in IPBL_DIV_ORDER else None
        if zap is None:
            continue
        if r["formula_value"] > zap:
            continue
        pair = _ipbl_norm(r["team1"], r["team2"])
        if white and pair not in white:
            continue
        if pair in blk.get(div, set()):
            continue
        if not _ipbl_in_schedule(rule, r["created_at"]):
            continue                                 # вне графика/дней набора — не в счёт
        if r["result"] == "Выигрыш":
            wins += 1
            profit += STAKE * (float(r["odds"]) - 1.0)
        else:
            losses += 1
            profit += -STAKE
    settled = wins + losses
    staked = settled * STAKE
    return {"signals": settled, "wins": wins, "losses": losses, "no_result": 0,
            "winrate": (wins / settled * 100) if settled else 0.0,
            "profit": profit, "staked": staked,
            "roi": (profit / staked * 100) if staked else 0.0}


def ipbl_overall_stats_from_history() -> dict:
    tot = _ipbl_empty_stats()
    for r in ipbl_get_rules():
        st = ipbl_rule_stats_from_history(r)
        for k in ("signals", "wins", "losses", "no_result", "profit", "staked"):
            tot[k] += st[k]
    settled = tot["wins"] + tot["losses"]
    tot["winrate"] = (tot["wins"] / settled * 100) if settled else 0.0
    tot["roi"] = (tot["profit"] / tot["staked"] * 100) if tot["staked"] else 0.0
    tot["balance"] = BANKROLL_START + tot["profit"]
    return tot


# --- реальная статистика набора по ОТПРАВЛЕННЫМ сигналам (ipbl_sent) --------

def ipbl_rule_stats(rule_id: int) -> dict:
    """Статистика набора по реально отправленным сигналам (таблица ipbl_sent,
    status='sent'). В отличие от ipbl_rule_stats_from_history (гипотетика по всем
    перерывам из signals) — только то, что реально ушло в Telegram."""
    conn = _conn()
    base = "FROM ipbl_sent WHERE rule_id=? AND status='sent'"
    total  = conn.execute(f"SELECT COUNT(*) {base}", (rule_id,)).fetchone()[0]
    wins   = conn.execute(f"SELECT COUNT(*) {base} AND result='Выигрыш'", (rule_id,)).fetchone()[0]
    losses = conn.execute(f"SELECT COUNT(*) {base} AND result='Проигрыш'", (rule_id,)).fetchone()[0]
    pushes = conn.execute(f"SELECT COUNT(*) {base} AND result='Возврат'", (rule_id,)).fetchone()[0]
    no_res = conn.execute(f"SELECT COUNT(*) {base} AND result IS NULL", (rule_id,)).fetchone()[0]
    profit = conn.execute(f"SELECT COALESCE(SUM(profit), 0) {base}", (rule_id,)).fetchone()[0]
    conn.close()
    settled = wins + losses
    staked = settled * STAKE
    return {"signals": total, "wins": wins, "losses": losses, "pushes": pushes,
            "no_result": no_res,
            "winrate": (wins / settled * 100) if settled else 0.0,
            "profit": profit, "staked": staked,
            "roi": (profit / staked * 100) if staked else 0.0}


def ipbl_overall_stats() -> dict:
    """Сумма ipbl_rule_stats по всем наборам — реальная статистика по отправленным."""
    tot = {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "no_result": 0,
           "profit": 0.0, "staked": 0.0}
    for r in ipbl_get_rules():
        st = ipbl_rule_stats(r["id"])
        for k in ("signals", "wins", "losses", "pushes", "no_result", "profit", "staked"):
            tot[k] += st[k]
    settled = tot["wins"] + tot["losses"]
    tot["winrate"] = (tot["wins"] / settled * 100) if settled else 0.0
    tot["roi"] = (tot["profit"] / tot["staked"] * 100) if tot["staked"] else 0.0
    tot["balance"] = BANKROLL_START + tot["profit"]
    return tot


# --- отправленные сигналы наборов (дедуп + отчёты) -------------------------

def ipbl_sent_exists(rule_id: int, event_id: int) -> bool:
    conn = _conn()
    row = conn.execute("SELECT 1 FROM ipbl_sent WHERE rule_id=? AND event_id=?",
                       (rule_id, event_id)).fetchone()
    conn.close()
    return row is not None


def ipbl_insert_sent(sig: dict) -> int | None:
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO ipbl_sent
                (rule_id, event_id, league, div, team1, team2, line, odds, formula_value,
                 score1, score2, quarters, chat_id, message_id, status,
                 result, final_score, final_total, profit, created_at)
            VALUES
                (:rule_id, :event_id, :league, :div, :team1, :team2, :line, :odds, :formula_value,
                 :score1, :score2, :quarters, :chat_id, :message_id, :status,
                 :result, :final_score, :final_total, :profit, :created_at)
        """, sig)
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def ipbl_get_sent_for_event(event_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM ipbl_sent WHERE event_id=?", (event_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ipbl_update_sent_result(sent_id: int, result: str | None, final_score: str,
                            final_total: int, profit: float | None):
    conn = _conn()
    conn.execute(
        "UPDATE ipbl_sent SET result=?, final_score=?, final_total=?, profit=? WHERE id=?",
        (result, final_score, final_total, profit, sent_id))
    conn.commit(); conn.close()


def ipbl_clear_sent(rule_id: int | None = None):
    conn = _conn()
    if rule_id is None:
        conn.execute("DELETE FROM ipbl_sent")
    else:
        conn.execute("DELETE FROM ipbl_sent WHERE rule_id=?", (rule_id,))
    conn.commit(); conn.close()


def ipbl_profit_by_day_rule(rule_id: int, start: str, end: str) -> dict[str, float]:
    conn = _conn()
    rows = conn.execute(
        "SELECT date(created_at) AS d, COALESCE(SUM(profit),0) AS p FROM ipbl_sent "
        "WHERE status='sent' AND profit IS NOT NULL AND rule_id=? "
        "AND date(created_at) BETWEEN ? AND ? GROUP BY d", (rule_id, start, end)).fetchall()
    conn.close()
    return {r["d"]: r["p"] for r in rows}


def ipbl_profit_total_rule(rule_id: int, start: str, end: str) -> float:
    conn = _conn()
    v = conn.execute(
        "SELECT COALESCE(SUM(profit),0) FROM ipbl_sent WHERE status='sent' AND profit IS NOT NULL "
        "AND rule_id=? AND date(created_at) BETWEEN ? AND ?", (rule_id, start, end)).fetchone()[0]
    conn.close()
    return v


def ipbl_daily_counts_rule(rule_id: int, day: str) -> dict:
    """Счётчики исходов (В/П/Возврат) ОДНОГО набора IPBL за один день ('YYYY-MM-DD')
    по реально отправленным сигналам — для строки «N✅/N✖️/N♻️» в отчёте дня."""
    conn = _conn()
    base = "FROM ipbl_sent WHERE status='sent' AND rule_id=? AND date(created_at)=?"
    args = (rule_id, day)
    wins   = conn.execute(f"SELECT COUNT(*) {base} AND result='Выигрыш'", args).fetchone()[0]
    losses = conn.execute(f"SELECT COUNT(*) {base} AND result='Проигрыш'", args).fetchone()[0]
    pushes = conn.execute(f"SELECT COUNT(*) {base} AND result='Возврат'", args).fetchone()[0]
    conn.close()
    return {"wins": wins, "losses": losses, "pushes": pushes}


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


# ===========================================================================
# Стратегия ТОТАЛОВ шорт-хоккея (ТБ/ТМ). Отдельные правила/сигналы/статистика.
# ===========================================================================

def sh_total_get_rules() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM sh_total_rules ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sh_total_get_rule(rule_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM sh_total_rules WHERE id=?", (rule_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def sh_total_add_rule(sport_name: str, minute: int, side: str,
                      line_min: float, line_max: float) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO sh_total_rules (sport_name, minute, side, line_min, line_max, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (sport_name, int(minute), side, float(line_min), float(line_max),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def sh_total_update_rule(rule_id: int, minute: int, side: str,
                         line_min: float, line_max: float):
    conn = _conn()
    conn.execute(
        "UPDATE sh_total_rules SET minute=?, side=?, line_min=?, line_max=? WHERE id=?",
        (int(minute), side, float(line_min), float(line_max), rule_id),
    )
    conn.commit()
    conn.close()


def sh_total_delete_rule(rule_id: int):
    conn = _conn()
    conn.execute("DELETE FROM sh_total_rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()


def sh_total_toggle_rule(rule_id: int) -> bool:
    conn = _conn()
    row = conn.execute("SELECT enabled FROM sh_total_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    new_state = 0 if row["enabled"] else 1
    conn.execute("UPDATE sh_total_rules SET enabled=? WHERE id=?", (new_state, rule_id))
    conn.commit()
    conn.close()
    return bool(new_state)


def sh_total_signal_exists(rule_id: int, event_id: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM sh_total_signals WHERE rule_id=? AND event_id=?",
        (rule_id, event_id),
    ).fetchone()
    conn.close()
    return row is not None


def sh_total_insert_signal(sig: dict) -> int | None:
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO sh_total_signals
                (rule_id, event_id, league, team1, team2, rule_minute, fired_minute,
                 side, line, odds, line_min, line_max, score1, score2,
                 chat_id, message_id, status, result, final_score, final_total, created_at)
            VALUES
                (:rule_id, :event_id, :league, :team1, :team2, :rule_minute, :fired_minute,
                 :side, :line, :odds, :line_min, :line_max, :score1, :score2,
                 :chat_id, :message_id, :status, :result, :final_score, :final_total, :created_at)
        """, sig)
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def sh_total_get_signals_for_event(event_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM sh_total_signals WHERE event_id=?", (event_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sh_total_update_signal_result(signal_id: int, result: str | None,
                                  final_score: str, final_total: int):
    conn = _conn()
    conn.execute(
        "UPDATE sh_total_signals SET result=?, final_score=?, final_total=? WHERE id=?",
        (result, final_score, final_total, signal_id),
    )
    conn.commit()
    conn.close()


def sh_total_rule_stats(rule_id: int) -> dict:
    """Как sh_rule_stats, но с учётом Возврата (пуш на целой линии): возврат не
    считается в staked и в прибыль (ставка вернулась)."""
    conn = _conn()
    base = "FROM sh_total_signals WHERE rule_id=? AND status='sent'"
    total   = conn.execute(f"SELECT COUNT(*) {base}", (rule_id,)).fetchone()[0]
    wins    = conn.execute(f"SELECT COUNT(*) {base} AND result='Выигрыш'", (rule_id,)).fetchone()[0]
    losses  = conn.execute(f"SELECT COUNT(*) {base} AND result='Проигрыш'", (rule_id,)).fetchone()[0]
    pushes  = conn.execute(f"SELECT COUNT(*) {base} AND result='Возврат'", (rule_id,)).fetchone()[0]
    no_res  = conn.execute(f"SELECT COUNT(*) {base} AND result IS NULL", (rule_id,)).fetchone()[0]
    sum_win_odds = conn.execute(
        f"SELECT COALESCE(SUM(odds), 0) {base} AND result='Выигрыш'", (rule_id,)).fetchone()[0]
    conn.close()
    settled = wins + losses
    winrate = (wins / settled * 100) if settled else 0.0
    profit = STAKE * (sum_win_odds - wins - losses)
    staked = settled * STAKE
    roi = (profit / staked * 100) if staked else 0.0
    return {"signals": total, "wins": wins, "losses": losses, "pushes": pushes,
            "no_result": no_res, "winrate": winrate,
            "profit": profit, "staked": staked, "roi": roi}


def sh_total_overall_stats() -> dict:
    tot = {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "no_result": 0,
           "profit": 0.0, "staked": 0.0}
    for r in sh_total_get_rules():
        st = sh_total_rule_stats(r["id"])
        tot["signals"] += st["signals"]; tot["wins"] += st["wins"]
        tot["losses"] += st["losses"]; tot["pushes"] += st["pushes"]
        tot["no_result"] += st["no_result"]
        tot["profit"] += st["profit"]; tot["staked"] += st["staked"]
    settled = tot["wins"] + tot["losses"]
    tot["winrate"] = (tot["wins"] / settled * 100) if settled else 0.0
    tot["roi"] = (tot["profit"] / tot["staked"] * 100) if tot["staked"] else 0.0
    tot["balance"] = BANKROLL_START + tot["profit"]
    return tot


def sh_total_clear_signals():
    conn = _conn()
    conn.execute("DELETE FROM sh_total_signals")
    conn.commit()
    conn.close()
