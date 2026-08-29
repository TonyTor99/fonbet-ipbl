"""Отдельная БД стратегии PRIME (config.PRIME_STRAT_DB).

Хранит ТОЛЬКО настройки стратегии и РЕАЛЬНО ОТПРАВЛЕННЫЕ сигналы (с результатом
зашло/не зашло). Файл независим от сборщика рынков (prime_markets.db) и от общей
БД сигналов (ipbl.db).

Таблицы:
  prime_rules       — набор: рынок (tm|it1) + минута + вкл/выкл;
  prime_rule_pairs  — галочки пар набора (нормализованная пара команд);
  prime_signals     — отправленные сигналы + дорасчёт (result + won + profit).

Список пар/команд для галочек берётся из СБОРЩИКА (config.PRIME_STRAT_SOURCE_DB),
а не отсюда — см. distinct_pairs()/distinct_teams().
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from config import (PRIME_STRAT_DB, PRIME_STRAT_SOURCE_DB, BANKROLL_START, STAKE)

DIR = Path(__file__).parent


def _path(db: str) -> str:
    p = Path(db)
    return str(p if p.is_absolute() else DIR / p)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(PRIME_STRAT_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --- нормализация пары (порядок команд не важен) ---------------------------

def norm_pair(t1: str, t2: str) -> tuple[str, str]:
    """('Вулвз','Догс') и ('Догс','Вулвз') -> один и тот же кортеж (a, b)."""
    a, b = (t1 or "").strip(), (t2 or "").strip()
    return tuple(sorted((a, b), key=str.lower))


def pair_label(a: str, b: str) -> str:
    return f"{a} — {b}"


# --- инициализация ---------------------------------------------------------

def init_db():
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prime_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            market     TEXT NOT NULL,               -- 'tm' | 'it1'
            minute     INTEGER NOT NULL,            -- игровая минута сигнала (строго ==)
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prime_rule_pairs (
            rule_id INTEGER NOT NULL,
            team_a  TEXT NOT NULL,                   -- нормализованная пара (см. norm_pair)
            team_b  TEXT NOT NULL,
            PRIMARY KEY (rule_id, team_a, team_b),
            FOREIGN KEY (rule_id) REFERENCES prime_rules(id) ON DELETE CASCADE
        );

        -- Только ОТПРАВЛЕННЫЕ сигналы. won: 1 = зашло, 0 = не зашло, NULL = возврат/нет итога.
        CREATE TABLE IF NOT EXISTS prime_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id     INTEGER NOT NULL,
            event_id    INTEGER NOT NULL,
            league      TEXT NOT NULL,
            market      TEXT NOT NULL,               -- 'tm' | 'it1' (копия на момент сигнала)
            minute      INTEGER NOT NULL,            -- минута правила
            fired_minute INTEGER,                    -- фактическая игровая минута отправки
            team1       TEXT NOT NULL,               -- команды как в матче (исходный порядок)
            team2       TEXT NOT NULL,
            line        REAL,                        -- линия рынка на момент сигнала
            odds        REAL,                        -- кф на момент сигнала
            score1      INTEGER NOT NULL,
            score2      INTEGER NOT NULL,
            chat_id     INTEGER,
            message_id  INTEGER,
            status      TEXT NOT NULL,               -- sent
            result      TEXT,                        -- Выигрыш | Проигрыш | Возврат | NULL
            won         INTEGER,                     -- 1 зашло / 0 не зашло / NULL
            final_score TEXT,
            final_total INTEGER,
            profit      REAL,                        -- ₽ по сигналу (NULL пока не дорассчитан)
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_prime_sig_unique
            ON prime_signals(rule_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_prime_sig_event ON prime_signals(event_id);
    """)
    conn.commit()
    conn.close()


def clear_signals():
    conn = _conn()
    conn.execute("DELETE FROM prime_signals")
    conn.commit()
    conn.close()


def clear_db():
    """Полный сброс: сигналы + наборы + галочки."""
    conn = _conn()
    conn.executescript(
        "DELETE FROM prime_signals; DELETE FROM prime_rule_pairs; DELETE FROM prime_rules;")
    conn.commit()
    conn.close()


# --- источник пар/команд (сборщик prime_markets.db) ------------------------

def _source_conn():
    conn = sqlite3.connect(_path(PRIME_STRAT_SOURCE_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def distinct_pairs() -> list[tuple[str, str]]:
    """Уникальные нормализованные пары из сборщика (порядок команд не важен)."""
    try:
        conn = _source_conn()
        rows = conn.execute(
            "SELECT DISTINCT team1, team2 FROM market_snapshots").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    pairs = {norm_pair(r["team1"], r["team2"]) for r in rows
             if r["team1"] and r["team2"]}
    return sorted(pairs, key=lambda p: (p[0].lower(), p[1].lower()))


def distinct_teams() -> list[str]:
    """Уникальные команды из сборщика (для фильтра пар по команде)."""
    try:
        conn = _source_conn()
        rows = conn.execute(
            "SELECT team1, team2 FROM market_snapshots").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    teams = set()
    for r in rows:
        if r["team1"]:
            teams.add(r["team1"].strip())
        if r["team2"]:
            teams.add(r["team2"].strip())
    return sorted(teams, key=str.lower)


# --- наборы (правила) ------------------------------------------------------

def get_rules() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM prime_rules ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rule(rule_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM prime_rules WHERE id=?", (rule_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_rule(market: str, minute: int) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO prime_rules (market, minute, enabled, created_at) VALUES (?, ?, 1, ?)",
        (market, int(minute), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_rule(rule_id: int, market: str, minute: int):
    conn = _conn()
    conn.execute("UPDATE prime_rules SET market=?, minute=? WHERE id=?",
                 (market, int(minute), rule_id))
    conn.commit()
    conn.close()


def delete_rule(rule_id: int):
    conn = _conn()
    conn.execute("DELETE FROM prime_rule_pairs WHERE rule_id=?", (rule_id,))
    conn.execute("DELETE FROM prime_rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()


def toggle_rule(rule_id: int) -> bool:
    conn = _conn()
    row = conn.execute("SELECT enabled FROM prime_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    new_state = 0 if row["enabled"] else 1
    conn.execute("UPDATE prime_rules SET enabled=? WHERE id=?", (new_state, rule_id))
    conn.commit()
    conn.close()
    return bool(new_state)


# --- галочки пар набора ----------------------------------------------------

def get_rule_pairs(rule_id: int) -> set[tuple[str, str]]:
    conn = _conn()
    rows = conn.execute(
        "SELECT team_a, team_b FROM prime_rule_pairs WHERE rule_id=?", (rule_id,)).fetchall()
    conn.close()
    return {(r["team_a"], r["team_b"]) for r in rows}


def count_pairs(rule_id: int) -> int:
    conn = _conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM prime_rule_pairs WHERE rule_id=?", (rule_id,)).fetchone()[0]
    conn.close()
    return n


def pair_enabled(rule_id: int, a: str, b: str) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM prime_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
        (rule_id, a, b)).fetchone()
    conn.close()
    return row is not None


def toggle_pair(rule_id: int, a: str, b: str) -> bool:
    """Ставит/снимает галочку пары. Возвращает новое состояние (True = отмечена)."""
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM prime_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
        (rule_id, a, b)).fetchone()
    if row:
        conn.execute("DELETE FROM prime_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
                     (rule_id, a, b))
        new_state = False
    else:
        conn.execute("INSERT OR IGNORE INTO prime_rule_pairs (rule_id, team_a, team_b) "
                     "VALUES (?, ?, ?)", (rule_id, a, b))
        new_state = True
    conn.commit()
    conn.close()
    return new_state


def set_pairs(rule_id: int, pairs: list[tuple[str, str]], enabled: bool):
    """Массово отметить/снять список пар (для «отметить/снять все в фильтре»)."""
    conn = _conn()
    if enabled:
        conn.executemany(
            "INSERT OR IGNORE INTO prime_rule_pairs (rule_id, team_a, team_b) VALUES (?, ?, ?)",
            [(rule_id, a, b) for a, b in pairs])
    else:
        conn.executemany(
            "DELETE FROM prime_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
            [(rule_id, a, b) for a, b in pairs])
    conn.commit()
    conn.close()


# --- сигналы ---------------------------------------------------------------

def signal_exists(rule_id: int, event_id: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM prime_signals WHERE rule_id=? AND event_id=?",
        (rule_id, event_id)).fetchone()
    conn.close()
    return row is not None


def insert_signal(sig: dict) -> int | None:
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO prime_signals
                (rule_id, event_id, league, market, minute, fired_minute, team1, team2,
                 line, odds, score1, score2, chat_id, message_id, status,
                 result, won, final_score, final_total, profit, created_at)
            VALUES
                (:rule_id, :event_id, :league, :market, :minute, :fired_minute, :team1, :team2,
                 :line, :odds, :score1, :score2, :chat_id, :message_id, :status,
                 :result, :won, :final_score, :final_total, :profit, :created_at)
        """, sig)
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_signals_for_event(event_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM prime_signals WHERE event_id=?", (event_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_signal_result(signal_id: int, result: str | None, won: int | None,
                         final_score: str, final_total: int, profit: float | None):
    conn = _conn()
    conn.execute(
        "UPDATE prime_signals SET result=?, won=?, final_score=?, final_total=?, profit=? "
        "WHERE id=?",
        (result, won, final_score, final_total, profit, signal_id))
    conn.commit()
    conn.close()


# --- статистика ------------------------------------------------------------

def rule_stats(rule_id: int) -> dict:
    conn = _conn()
    base = "FROM prime_signals WHERE rule_id=? AND status='sent'"
    total  = conn.execute(f"SELECT COUNT(*) {base}", (rule_id,)).fetchone()[0]
    wins   = conn.execute(f"SELECT COUNT(*) {base} AND result='Выигрыш'", (rule_id,)).fetchone()[0]
    losses = conn.execute(f"SELECT COUNT(*) {base} AND result='Проигрыш'", (rule_id,)).fetchone()[0]
    pushes = conn.execute(f"SELECT COUNT(*) {base} AND result='Возврат'", (rule_id,)).fetchone()[0]
    no_res = conn.execute(f"SELECT COUNT(*) {base} AND result IS NULL", (rule_id,)).fetchone()[0]
    profit = conn.execute(f"SELECT COALESCE(SUM(profit), 0) {base}", (rule_id,)).fetchone()[0]
    conn.close()
    settled = wins + losses
    winrate = (wins / settled * 100) if settled else 0.0
    staked = settled * STAKE
    roi = (profit / staked * 100) if staked else 0.0
    return {"signals": total, "wins": wins, "losses": losses, "pushes": pushes,
            "no_result": no_res, "winrate": winrate,
            "profit": profit, "staked": staked, "roi": roi}


def overall_stats() -> dict:
    tot = {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "no_result": 0,
           "profit": 0.0, "staked": 0.0}
    for r in get_rules():
        st = rule_stats(r["id"])
        for k in ("signals", "wins", "losses", "pushes", "no_result", "profit", "staked"):
            tot[k] += st[k]
    settled = tot["wins"] + tot["losses"]
    tot["winrate"] = (tot["wins"] / settled * 100) if settled else 0.0
    tot["roi"] = (tot["profit"] / tot["staked"] * 100) if tot["staked"] else 0.0
    tot["balance"] = BANKROLL_START + tot["profit"]
    return tot


# --- прибыль для отчётов (день/неделя/месяц) -------------------------------

def profit_by_day(start: str, end: str) -> dict[str, float]:
    conn = _conn()
    rows = conn.execute(
        "SELECT date(created_at) AS d, COALESCE(SUM(profit), 0) AS p "
        "FROM prime_signals WHERE status='sent' AND profit IS NOT NULL "
        "AND date(created_at) BETWEEN ? AND ? GROUP BY d", (start, end)).fetchall()
    conn.close()
    return {r["d"]: r["p"] for r in rows}


def profit_total(start: str, end: str) -> float:
    conn = _conn()
    v = conn.execute(
        "SELECT COALESCE(SUM(profit), 0) FROM prime_signals "
        "WHERE status='sent' AND profit IS NOT NULL AND date(created_at) BETWEEN ? AND ?",
        (start, end)).fetchone()[0]
    conn.close()
    return v
