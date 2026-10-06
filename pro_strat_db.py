"""Отдельная БД стратегии Pro МУЖЧИНЫ (config.PRO_STRAT_DB).

Полный аналог prime_women_db.py, но ТОЛЬКО рынок ТМ (тотал матча меньше) и лига
Pro муж. Хранит настройки стратегии и РЕАЛЬНО ОТПРАВЛЕННЫЕ сигналы ТМ (с результатом
зашло/не зашло). Файл независим от сборщика (pro_markets.db) и от общей БД сигналов
(ipbl.db).

Таблицы:
  pro_rules       — набор: минута (строго ==) + вкл/выкл;
  pro_rule_pairs  — галочки пар набора (нормализованная пара команд);
  pro_signals     — отправленные сигналы ТМ + дорасчёт (result + won + profit).

Список пар/команд и статистику встреч берём из СБОРЩИКА (config.PRO_STRAT_SOURCE_DB).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from config import (PRO_STRAT_DB, PRO_STRAT_SOURCE_DB, BANKROLL_START, STAKE)

DIR = Path(__file__).parent


def _path(db: str) -> str:
    p = Path(db)
    return str(p if p.is_absolute() else DIR / p)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(PRO_STRAT_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --- нормализация пары (порядок команд не важен) ---------------------------

def norm_pair(t1: str, t2: str) -> tuple[str, str]:
    a, b = (t1 or "").strip(), (t2 or "").strip()
    return tuple(sorted((a, b), key=str.lower))


def pair_label(a: str, b: str) -> str:
    return f"{a} — {b}"


# --- инициализация ---------------------------------------------------------

def init_db():
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pro_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            minute     INTEGER NOT NULL,            -- игровая минута сигнала (строго ==)
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pro_rule_pairs (
            rule_id INTEGER NOT NULL,
            team_a  TEXT NOT NULL,
            team_b  TEXT NOT NULL,
            PRIMARY KEY (rule_id, team_a, team_b),
            FOREIGN KEY (rule_id) REFERENCES pro_rules(id) ON DELETE CASCADE
        );

        -- Только ОТПРАВЛЕННЫЕ сигналы. won: 1 = зашло, 0 = не зашло, NULL = возврат/нет итога.
        CREATE TABLE IF NOT EXISTS pro_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id     INTEGER NOT NULL,
            event_id    INTEGER NOT NULL,
            league      TEXT NOT NULL,
            minute      INTEGER NOT NULL,
            fired_minute INTEGER,
            team1       TEXT NOT NULL,
            team2       TEXT NOT NULL,
            line        REAL,
            odds        REAL,
            score1      INTEGER NOT NULL,
            score2      INTEGER NOT NULL,
            chat_id     INTEGER,
            message_id  INTEGER,
            status      TEXT NOT NULL,               -- sent | no_chat
            result      TEXT,
            won         INTEGER,
            final_score TEXT,
            final_total INTEGER,
            profit      REAL,
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pro_sig_unique
            ON pro_signals(rule_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_pro_sig_event ON pro_signals(event_id);
    """)
    conn.commit()
    conn.close()


def clear_signals():
    conn = _conn()
    conn.execute("DELETE FROM pro_signals")
    conn.commit()
    conn.close()


def clear_db():
    conn = _conn()
    conn.executescript(
        "DELETE FROM pro_signals; DELETE FROM pro_rule_pairs; DELETE FROM pro_rules;")
    conn.commit()
    conn.close()


# --- источник пар/команд (сборщик pro_markets.db) --------------------------

def _source_conn():
    conn = sqlite3.connect(_path(PRO_STRAT_SOURCE_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def distinct_pairs() -> list[tuple[str, str]]:
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
    rows = conn.execute("SELECT * FROM pro_rules ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rule(rule_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM pro_rules WHERE id=?", (rule_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_rule(minute: int) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO pro_rules (minute, enabled, created_at) VALUES (?, 1, ?)",
        (int(minute), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_rule(rule_id: int, minute: int):
    conn = _conn()
    conn.execute("UPDATE pro_rules SET minute=? WHERE id=?", (int(minute), rule_id))
    conn.commit()
    conn.close()


def delete_rule(rule_id: int):
    conn = _conn()
    conn.execute("DELETE FROM pro_rule_pairs WHERE rule_id=?", (rule_id,))
    conn.execute("DELETE FROM pro_rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()


def toggle_rule(rule_id: int) -> bool:
    conn = _conn()
    row = conn.execute("SELECT enabled FROM pro_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    new_state = 0 if row["enabled"] else 1
    conn.execute("UPDATE pro_rules SET enabled=? WHERE id=?", (new_state, rule_id))
    conn.commit()
    conn.close()
    return bool(new_state)


# --- галочки пар набора ----------------------------------------------------

def get_rule_pairs(rule_id: int) -> set[tuple[str, str]]:
    conn = _conn()
    rows = conn.execute(
        "SELECT team_a, team_b FROM pro_rule_pairs WHERE rule_id=?", (rule_id,)).fetchall()
    conn.close()
    return {(r["team_a"], r["team_b"]) for r in rows}


def count_pairs(rule_id: int) -> int:
    conn = _conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM pro_rule_pairs WHERE rule_id=?", (rule_id,)).fetchone()[0]
    conn.close()
    return n


def toggle_pair(rule_id: int, a: str, b: str) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM pro_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
        (rule_id, a, b)).fetchone()
    if row:
        conn.execute("DELETE FROM pro_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
                     (rule_id, a, b))
        new_state = False
    else:
        conn.execute("INSERT OR IGNORE INTO pro_rule_pairs (rule_id, team_a, team_b) "
                     "VALUES (?, ?, ?)", (rule_id, a, b))
        new_state = True
    conn.commit()
    conn.close()
    return new_state


def set_pairs(rule_id: int, pairs: list[tuple[str, str]], enabled: bool):
    conn = _conn()
    if enabled:
        conn.executemany(
            "INSERT OR IGNORE INTO pro_rule_pairs (rule_id, team_a, team_b) VALUES (?, ?, ?)",
            [(rule_id, a, b) for a, b in pairs])
    else:
        conn.executemany(
            "DELETE FROM pro_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
            [(rule_id, a, b) for a, b in pairs])
    conn.commit()
    conn.close()


# --- сигналы ---------------------------------------------------------------

def signal_exists(rule_id: int, event_id: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM pro_signals WHERE rule_id=? AND event_id=?",
        (rule_id, event_id)).fetchone()
    conn.close()
    return row is not None


def insert_signal(sig: dict) -> int | None:
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO pro_signals
                (rule_id, event_id, league, minute, fired_minute, team1, team2,
                 line, odds, score1, score2, chat_id, message_id, status,
                 result, won, final_score, final_total, profit, created_at)
            VALUES
                (:rule_id, :event_id, :league, :minute, :fired_minute, :team1, :team2,
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
        "SELECT * FROM pro_signals WHERE event_id=?", (event_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_signal_result(signal_id: int, result: str | None, won: int | None,
                         final_score: str, final_total: int, profit: float | None):
    conn = _conn()
    conn.execute(
        "UPDATE pro_signals SET result=?, won=?, final_score=?, final_total=?, profit=? "
        "WHERE id=?",
        (result, won, final_score, final_total, profit, signal_id))
    conn.commit()
    conn.close()


# --- статистика ------------------------------------------------------------

def rule_stats(rule_id: int) -> dict:
    conn = _conn()
    base = "FROM pro_signals WHERE rule_id=? AND status='sent'"
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


# ТМ-поля в сборщике (крайняя линия матча): линия / кф «меньше» / результат «меньше».
_COLLECTOR_FIELDS = ("total_line", "total_m_odds", "r_total_m")


def pair_stats_from_collector(team1: str, team2: str, minute: int) -> dict:
    """Статистика встреч пары (ТМ) по СБОРЩИКУ (pro_markets.db) — для строки в тексте
    сигнала. По ВСЕМ матчам пары на той же игровой минуте, что и правило: берём
    последний снимок минуты (MAX(id)) с рассчитанным исходом ТМ и считаем
    гипотетическую флэт-ставку STAKE по кф снимка. Возврат/нерасчёт не в счёт.
    Порядок команд не важен (norm_pair). Возвращает count/wins/profit/roi."""
    line_f, odds_f, res_f = _COLLECTOR_FIELDS
    target = norm_pair(team1, team2)
    try:
        conn = _source_conn()
        rows = conn.execute(
            f"SELECT team1, team2, {odds_f} AS odds, {res_f} AS res, MAX(id) "
            "FROM market_snapshots "
            f"WHERE game_minute=? AND {res_f} IN ('Выигрыш', 'Проигрыш') "
            "GROUP BY event_id", (minute,)).fetchall()
        conn.close()
    except sqlite3.Error:
        return {"count": 0, "wins": 0, "profit": 0.0, "roi": 0.0}
    count = wins = 0
    profit = 0.0
    for r in rows:
        if norm_pair(r["team1"], r["team2"]) != target:
            continue
        count += 1
        if r["res"] == "Выигрыш":
            wins += 1
            if r["odds"] is not None:
                profit += STAKE * (float(r["odds"]) - 1.0)
        else:
            profit += -STAKE
    staked = count * STAKE
    roi = (profit / staked * 100) if staked else 0.0
    return {"count": count, "wins": wins, "profit": profit, "roi": roi}


def rule_stats_from_collector(rule: dict) -> dict:
    """Гипотетическая статистика набора по СБОРЩИКУ (pro_markets.db).

    По ВСЕМ матчам отмеченных пар на игровой минуте набора берём последний снимок
    минуты (MAX(id)) с рассчитанным исходом ТМ и считаем флэт STAKE по кф снимка:
    Выигрыш -> +STAKE*(кф-1), Проигрыш -> -STAKE, Возврат/нерасчёт — не в прибыль.
    В отличие от rule_stats() (реально отправленные сигналы), даёт бэктест набора."""
    empty = {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "no_result": 0,
             "winrate": 0.0, "profit": 0.0, "staked": 0.0, "roi": 0.0}
    line_f, odds_f, res_f = _COLLECTOR_FIELDS
    sel = get_rule_pairs(rule["id"])
    try:
        conn = _source_conn()
        rows = conn.execute(
            f"SELECT team1, team2, {odds_f} AS odds, {res_f} AS res, MAX(id) "
            "FROM market_snapshots "
            "WHERE game_minute=? GROUP BY event_id", (rule["minute"],)).fetchall()
        conn.close()
    except sqlite3.Error:
        return empty
    wins = losses = pushes = no_result = 0
    profit = 0.0
    for r in rows:
        if norm_pair(r["team1"], r["team2"]) not in sel:
            continue
        if r["odds"] is None:
            continue                       # ТМ не котировался на минуте — не учитываем
        res = r["res"]
        if res == "Выигрыш":
            wins += 1
            profit += STAKE * (float(r["odds"]) - 1.0)
        elif res == "Проигрыш":
            losses += 1
            profit += -STAKE
        elif res == "Возврат":
            pushes += 1
        else:
            no_result += 1
    settled = wins + losses
    staked = settled * STAKE
    return {"signals": wins + losses + pushes + no_result,
            "wins": wins, "losses": losses, "pushes": pushes, "no_result": no_result,
            "winrate": (wins / settled * 100) if settled else 0.0,
            "profit": profit, "staked": staked,
            "roi": (profit / staked * 100) if staked else 0.0}


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


def overall_stats_from_collector() -> dict:
    tot = {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "no_result": 0,
           "profit": 0.0, "staked": 0.0}
    for r in get_rules():
        st = rule_stats_from_collector(r)
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
        "FROM pro_signals WHERE status='sent' AND profit IS NOT NULL "
        "AND date(created_at) BETWEEN ? AND ? GROUP BY d", (start, end)).fetchall()
    conn.close()
    return {r["d"]: r["p"] for r in rows}


def profit_total(start: str, end: str) -> float:
    conn = _conn()
    v = conn.execute(
        "SELECT COALESCE(SUM(profit), 0) FROM pro_signals "
        "WHERE status='sent' AND profit IS NOT NULL AND date(created_at) BETWEEN ? AND ?",
        (start, end)).fetchone()[0]
    conn.close()
    return v


def signals_for_export() -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM pro_signals WHERE status='sent' ORDER BY created_at, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]
