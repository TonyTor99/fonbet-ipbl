"""Отдельная БД стратегии ЧЕТВЕРТИ Pro Жен (config.PQ_STRAT_DB).

Хранит ТОЛЬКО настройки стратегии и РЕАЛЬНО ОТПРАВЛЕННЫЕ сигналы (с результатом
Выигрыш/Проигрыш/Возврат). Файл независим от сборщика четвертей
(pro_women_periods.db) и от общей БД сигналов (ipbl.db).

Таблицы:
  pq_rules       — набор: сторона (over|under) + список минут (CSV, напр. "7,17,27")
                   + вкл/выкл. На каждой минуте — тотал ТЕКУЩЕЙ четверти.
  pq_rule_pairs  — галочки пар набора (нормализованная пара команд);
  pq_signals     — отправленные сигналы + дорасчёт. Дедуп UNIQUE(rule_id,event_id,minute)
                   — одно правило может дать несколько сигналов на матч (по минуте).

Список пар/команд для галочек берётся из СБОРЩИКА четвертей Pro жен
(config.PQ_STRAT_SOURCE_DB) — см. distinct_pairs()/distinct_teams().
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from config import (PQ_STRAT_DB, PQ_STRAT_SOURCE_DB, BANKROLL_START, STAKE)

DIR = Path(__file__).parent


def _path(db: str) -> str:
    p = Path(db)
    return str(p if p.is_absolute() else DIR / p)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(PQ_STRAT_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --- минуты набора (CSV <-> список) ----------------------------------------

def parse_minutes(raw: str) -> list[int] | None:
    """'7 17 27' или '7,17,27' -> [7,17,27] (по возрастанию, без дублей) или None."""
    parts = [p for p in raw.replace(",", " ").split() if p]
    if not parts:
        return None
    out = set()
    for p in parts:
        try:
            v = int(p)
        except ValueError:
            return None
        if v < 0:
            return None
        out.add(v)
    return sorted(out)


def minutes_to_csv(minutes: list[int]) -> str:
    return ",".join(str(m) for m in minutes)


def minutes_list(rule: dict) -> list[int]:
    raw = rule.get("minutes") or ""
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


def minutes_label(rule: dict) -> str:
    ml = minutes_list(rule)
    return ",".join(str(m) for m in ml) if ml else "—"


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
        CREATE TABLE IF NOT EXISTS pq_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            side       TEXT NOT NULL,               -- 'over' (ТБ) | 'under' (ТМ)
            minutes    TEXT NOT NULL,               -- список игровых минут CSV, напр. '7,17,27'
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pq_rule_pairs (
            rule_id INTEGER NOT NULL,
            team_a  TEXT NOT NULL,
            team_b  TEXT NOT NULL,
            PRIMARY KEY (rule_id, team_a, team_b),
            FOREIGN KEY (rule_id) REFERENCES pq_rules(id) ON DELETE CASCADE
        );

        -- Только ОТПРАВЛЕННЫЕ сигналы. won: 1 зашло / 0 не зашло / NULL возврат/нет итога.
        CREATE TABLE IF NOT EXISTS pq_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id     INTEGER NOT NULL,
            event_id    INTEGER NOT NULL,
            league      TEXT NOT NULL,
            side        TEXT NOT NULL,               -- over | under
            minute      INTEGER NOT NULL,            -- игровая минута срабатывания (из набора)
            quarter     INTEGER NOT NULL,            -- номер четверти (на этой минуте)
            team1       TEXT NOT NULL,
            team2       TEXT NOT NULL,
            line        REAL,                        -- линия тотала четверти на момент сигнала
            odds        REAL,                        -- кф стороны на момент сигнала
            score1      INTEGER NOT NULL,            -- счёт МАТЧА на момент сигнала
            score2      INTEGER NOT NULL,
            q_live      TEXT,                        -- живой счёт четверти на момент сигнала 'a:b'
            chat_id     INTEGER,
            message_id  INTEGER,
            status      TEXT NOT NULL,               -- sent | no_chat
            result      TEXT,                        -- Выигрыш | Проигрыш | Возврат | NULL
            won         INTEGER,                     -- 1 / 0 / NULL
            q_final     TEXT,                        -- итоговый счёт четверти 'a:b'
            q_total     INTEGER,                     -- итоговый тотал четверти
            profit      REAL,
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pq_sig_unique
            ON pq_signals(rule_id, event_id, minute);
        CREATE INDEX IF NOT EXISTS idx_pq_sig_event ON pq_signals(event_id);
    """)
    conn.commit()
    conn.close()


def clear_signals():
    conn = _conn()
    conn.execute("DELETE FROM pq_signals")
    conn.commit()
    conn.close()


def clear_db():
    conn = _conn()
    conn.executescript(
        "DELETE FROM pq_signals; DELETE FROM pq_rule_pairs; DELETE FROM pq_rules;")
    conn.commit()
    conn.close()


# --- источник пар/команд (сборщик pro_women_periods.db) --------------------

def _source_rows() -> list[sqlite3.Row]:
    try:
        conn = sqlite3.connect(_path(PQ_STRAT_SOURCE_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT team1, team2 FROM period_snapshots").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return rows


def distinct_pairs() -> list[tuple[str, str]]:
    pairs = {norm_pair(r["team1"], r["team2"]) for r in _source_rows()
             if r["team1"] and r["team2"]}
    return sorted(pairs, key=lambda p: (p[0].lower(), p[1].lower()))


def distinct_teams() -> list[str]:
    teams = set()
    for r in _source_rows():
        if r["team1"]:
            teams.add(r["team1"].strip())
        if r["team2"]:
            teams.add(r["team2"].strip())
    return sorted(teams, key=str.lower)


# --- наборы (правила) ------------------------------------------------------

def get_rules() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM pq_rules ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rule(rule_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM pq_rules WHERE id=?", (rule_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_rule(side: str, minutes: list[int]) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO pq_rules (side, minutes, enabled, created_at) VALUES (?, ?, 1, ?)",
        (side, minutes_to_csv(minutes), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_rule(rule_id: int, side: str, minutes: list[int]):
    conn = _conn()
    conn.execute("UPDATE pq_rules SET side=?, minutes=? WHERE id=?",
                 (side, minutes_to_csv(minutes), rule_id))
    conn.commit()
    conn.close()


def delete_rule(rule_id: int):
    conn = _conn()
    conn.execute("DELETE FROM pq_rule_pairs WHERE rule_id=?", (rule_id,))
    conn.execute("DELETE FROM pq_rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()


def toggle_rule(rule_id: int) -> bool:
    conn = _conn()
    row = conn.execute("SELECT enabled FROM pq_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    new_state = 0 if row["enabled"] else 1
    conn.execute("UPDATE pq_rules SET enabled=? WHERE id=?", (new_state, rule_id))
    conn.commit()
    conn.close()
    return bool(new_state)


# --- галочки пар набора ----------------------------------------------------

def get_rule_pairs(rule_id: int) -> set[tuple[str, str]]:
    conn = _conn()
    rows = conn.execute(
        "SELECT team_a, team_b FROM pq_rule_pairs WHERE rule_id=?", (rule_id,)).fetchall()
    conn.close()
    return {(r["team_a"], r["team_b"]) for r in rows}


def count_pairs(rule_id: int) -> int:
    conn = _conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM pq_rule_pairs WHERE rule_id=?", (rule_id,)).fetchone()[0]
    conn.close()
    return n


def toggle_pair(rule_id: int, a: str, b: str) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM pq_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
        (rule_id, a, b)).fetchone()
    if row:
        conn.execute("DELETE FROM pq_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
                     (rule_id, a, b))
        new_state = False
    else:
        conn.execute("INSERT OR IGNORE INTO pq_rule_pairs (rule_id, team_a, team_b) "
                     "VALUES (?, ?, ?)", (rule_id, a, b))
        new_state = True
    conn.commit()
    conn.close()
    return new_state


def set_pairs(rule_id: int, pairs: list[tuple[str, str]], enabled: bool):
    conn = _conn()
    if enabled:
        conn.executemany(
            "INSERT OR IGNORE INTO pq_rule_pairs (rule_id, team_a, team_b) VALUES (?, ?, ?)",
            [(rule_id, a, b) for a, b in pairs])
    else:
        conn.executemany(
            "DELETE FROM pq_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
            [(rule_id, a, b) for a, b in pairs])
    conn.commit()
    conn.close()


# --- сигналы ---------------------------------------------------------------

def signal_exists(rule_id: int, event_id: int, minute: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM pq_signals WHERE rule_id=? AND event_id=? AND minute=?",
        (rule_id, event_id, minute)).fetchone()
    conn.close()
    return row is not None


def insert_signal(sig: dict) -> int | None:
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO pq_signals
                (rule_id, event_id, league, side, minute, quarter, team1, team2,
                 line, odds, score1, score2, q_live, chat_id, message_id, status,
                 result, won, q_final, q_total, profit, created_at)
            VALUES
                (:rule_id, :event_id, :league, :side, :minute, :quarter, :team1, :team2,
                 :line, :odds, :score1, :score2, :q_live, :chat_id, :message_id, :status,
                 :result, :won, :q_final, :q_total, :profit, :created_at)
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
        "SELECT * FROM pq_signals WHERE event_id=?", (event_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_signal_result(signal_id: int, result: str | None, won: int | None,
                         q_final: str, q_total: int, profit: float | None):
    conn = _conn()
    conn.execute(
        "UPDATE pq_signals SET result=?, won=?, q_final=?, q_total=?, profit=? WHERE id=?",
        (result, won, q_final, q_total, profit, signal_id))
    conn.commit()
    conn.close()


# --- статистика ------------------------------------------------------------

def rule_stats(rule_id: int) -> dict:
    conn = _conn()
    base = "FROM pq_signals WHERE rule_id=? AND status='sent'"
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


# --- прибыль для отчётов ----------------------------------------------------

def profit_by_day(start: str, end: str) -> dict[str, float]:
    conn = _conn()
    rows = conn.execute(
        "SELECT date(created_at) AS d, COALESCE(SUM(profit), 0) AS p "
        "FROM pq_signals WHERE status='sent' AND profit IS NOT NULL "
        "AND date(created_at) BETWEEN ? AND ? GROUP BY d", (start, end)).fetchall()
    conn.close()
    return {r["d"]: r["p"] for r in rows}


def profit_total(start: str, end: str) -> float:
    conn = _conn()
    v = conn.execute(
        "SELECT COALESCE(SUM(profit), 0) FROM pq_signals "
        "WHERE status='sent' AND profit IS NOT NULL AND date(created_at) BETWEEN ? AND ?",
        (start, end)).fetchone()[0]
    conn.close()
    return v


def signals_for_export() -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM pq_signals WHERE status='sent' ORDER BY created_at, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]
