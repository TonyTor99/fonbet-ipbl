"""Отдельная БД стратегии CAGE (config.CAGE_STRAT_DB).

Хранит ТОЛЬКО настройки стратегии и РЕАЛЬНО ОТПРАВЛЕННЫЕ сигналы ТМ (с результатом
зашло/не зашло). Файл независим от сборщика рынков (cage_markets.db) и от общей
БД сигналов (ipbl.db).

Таблицы:
  cage_rules       — набор: рынок (tm|it1|it2) + момент входа (минута; -1 = прематч)
                     + флаг «все пары» + вкл/выкл.
  cage_rule_pairs  — галочки пар набора (нормализованная пара команд).
  cage_signals     — отправленные сигналы + дорасчёт (result + won + profit).

Список пар/команд для галочек берётся из СБОРЩИКА (config.CAGE_STRAT_SOURCE_DB),
а не отсюда — см. distinct_pairs()/distinct_teams().
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from config import (CAGE_STRAT_DB, CAGE_STRAT_SOURCE_DB, BANKROLL_START, STAKE,
                    CAGE_STRAT_KF_MIN, CAGE_STRAT_KF_MAX)

DIR = Path(__file__).parent


def _path(db: str) -> str:
    p = Path(db)
    return str(p if p.is_absolute() else DIR / p)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(CAGE_STRAT_DB), timeout=10)
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
        CREATE TABLE IF NOT EXISTS cage_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            market     TEXT NOT NULL DEFAULT 'tm',   -- 'tm' | 'it1' | 'it2'
            minute     INTEGER NOT NULL,            -- игровая минута сигнала; -1 = прематч
            all_pairs  INTEGER NOT NULL DEFAULT 0,  -- 1 = ловить ЛЮБУЮ пару (игнор галочек)
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cage_rule_pairs (
            rule_id INTEGER NOT NULL,
            team_a  TEXT NOT NULL,                   -- нормализованная пара (см. norm_pair)
            team_b  TEXT NOT NULL,
            PRIMARY KEY (rule_id, team_a, team_b),
            FOREIGN KEY (rule_id) REFERENCES cage_rules(id) ON DELETE CASCADE
        );

        -- Только ОТПРАВЛЕННЫЕ сигналы. won: 1 = зашло, 0 = не зашло, NULL = возврат/нет итога.
        CREATE TABLE IF NOT EXISTS cage_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id     INTEGER NOT NULL,
            event_id    INTEGER NOT NULL,
            league      TEXT NOT NULL,
            market      TEXT NOT NULL DEFAULT 'tm',  -- 'tm' | 'it1' | 'it2' (копия на момент сигнала)
            minute      INTEGER NOT NULL,            -- минута правила (-1 = прематч)
            fired_minute INTEGER,                    -- фактическая игровая минута отправки (прематч = -1)
            team1       TEXT NOT NULL,               -- команды как в матче (исходный порядок)
            team2       TEXT NOT NULL,
            line        REAL,                        -- «ровная» линия ТМ на момент сигнала
            odds        REAL,                        -- кф ТМ на момент сигнала
            score1      INTEGER NOT NULL,
            score2      INTEGER NOT NULL,
            chat_id     INTEGER,
            message_id  INTEGER,
            status      TEXT NOT NULL,               -- sent | no_chat
            result      TEXT,                        -- Выигрыш | Проигрыш | Возврат | NULL
            won         INTEGER,                     -- 1 зашло / 0 не зашло / NULL
            final_score TEXT,
            final_total INTEGER,
            profit      REAL,                        -- ₽ по сигналу (NULL пока не дорассчитан)
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cage_sig_unique
            ON cage_signals(rule_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_cage_sig_event ON cage_signals(event_id);
    """)
    # Миграция старых БД (созданных до добавления рынка): дозаливаем колонку market.
    for table in ("cage_rules", "cage_signals"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "market" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN market TEXT NOT NULL DEFAULT 'tm'")
    conn.commit()
    conn.close()


def clear_signals(market: str | None = None):
    """Удаляет отправленные сигналы. market=None — все; иначе только этого рынка."""
    conn = _conn()
    if market is None:
        conn.execute("DELETE FROM cage_signals")
    else:
        conn.execute("DELETE FROM cage_signals WHERE market=?", (market,))
    conn.commit()
    conn.close()


def clear_db():
    """Полный сброс: сигналы + наборы + галочки."""
    conn = _conn()
    conn.executescript(
        "DELETE FROM cage_signals; DELETE FROM cage_rule_pairs; DELETE FROM cage_rules;")
    conn.commit()
    conn.close()


# --- источник пар/команд (сборщик cage_markets.db) -------------------------

def _source_conn():
    conn = sqlite3.connect(_path(CAGE_STRAT_SOURCE_DB), timeout=10)
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

def get_rules(market: str | None = None) -> list[dict]:
    conn = _conn()
    if market is None:
        rows = conn.execute("SELECT * FROM cage_rules ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM cage_rules WHERE market=? ORDER BY id", (market,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rule(rule_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM cage_rules WHERE id=?", (rule_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_rule(market: str, minute: int) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO cage_rules (market, minute, all_pairs, enabled, created_at) "
        "VALUES (?, ?, 0, 1, ?)",
        (market, int(minute), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_rule(rule_id: int, market: str, minute: int):
    conn = _conn()
    conn.execute("UPDATE cage_rules SET market=?, minute=? WHERE id=?",
                 (market, int(minute), rule_id))
    conn.commit()
    conn.close()


def delete_rule(rule_id: int):
    conn = _conn()
    conn.execute("DELETE FROM cage_rule_pairs WHERE rule_id=?", (rule_id,))
    conn.execute("DELETE FROM cage_rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()


def toggle_rule(rule_id: int) -> bool:
    conn = _conn()
    row = conn.execute("SELECT enabled FROM cage_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    new_state = 0 if row["enabled"] else 1
    conn.execute("UPDATE cage_rules SET enabled=? WHERE id=?", (new_state, rule_id))
    conn.commit()
    conn.close()
    return bool(new_state)


def toggle_all_pairs(rule_id: int) -> bool:
    """Переключает режим «все пары». Возвращает новое состояние (True = все пары)."""
    conn = _conn()
    row = conn.execute("SELECT all_pairs FROM cage_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        conn.close()
        return False
    new_state = 0 if row["all_pairs"] else 1
    conn.execute("UPDATE cage_rules SET all_pairs=? WHERE id=?", (new_state, rule_id))
    conn.commit()
    conn.close()
    return bool(new_state)


# --- галочки пар набора ----------------------------------------------------

def get_rule_pairs(rule_id: int) -> set[tuple[str, str]]:
    conn = _conn()
    rows = conn.execute(
        "SELECT team_a, team_b FROM cage_rule_pairs WHERE rule_id=?", (rule_id,)).fetchall()
    conn.close()
    return {(r["team_a"], r["team_b"]) for r in rows}


def count_pairs(rule_id: int) -> int:
    conn = _conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM cage_rule_pairs WHERE rule_id=?", (rule_id,)).fetchone()[0]
    conn.close()
    return n


def toggle_pair(rule_id: int, a: str, b: str) -> bool:
    """Ставит/снимает галочку пары. Возвращает новое состояние (True = отмечена)."""
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM cage_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
        (rule_id, a, b)).fetchone()
    if row:
        conn.execute("DELETE FROM cage_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
                     (rule_id, a, b))
        new_state = False
    else:
        conn.execute("INSERT OR IGNORE INTO cage_rule_pairs (rule_id, team_a, team_b) "
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
            "INSERT OR IGNORE INTO cage_rule_pairs (rule_id, team_a, team_b) VALUES (?, ?, ?)",
            [(rule_id, a, b) for a, b in pairs])
    else:
        conn.executemany(
            "DELETE FROM cage_rule_pairs WHERE rule_id=? AND team_a=? AND team_b=?",
            [(rule_id, a, b) for a, b in pairs])
    conn.commit()
    conn.close()


# --- сигналы ---------------------------------------------------------------

def signal_exists(rule_id: int, event_id: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM cage_signals WHERE rule_id=? AND event_id=?",
        (rule_id, event_id)).fetchone()
    conn.close()
    return row is not None


def insert_signal(sig: dict) -> int | None:
    conn = _conn()
    try:
        cur = conn.execute("""
            INSERT INTO cage_signals
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
        "SELECT * FROM cage_signals WHERE event_id=?", (event_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_signal_result(signal_id: int, result: str | None, won: int | None,
                         final_score: str, final_total: int, profit: float | None):
    conn = _conn()
    conn.execute(
        "UPDATE cage_signals SET result=?, won=?, final_score=?, final_total=?, profit=? "
        "WHERE id=?",
        (result, won, final_score, final_total, profit, signal_id))
    conn.commit()
    conn.close()


# --- статистика ------------------------------------------------------------

def rule_stats(rule_id: int) -> dict:
    conn = _conn()
    base = "FROM cage_signals WHERE rule_id=? AND status='sent'"
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


def pair_stats(team1: str, team2: str) -> dict:
    """Статистика встреч пары по ОТПРАВЛЕННЫМ сигналам — для строки в тексте сигнала.

    Учитываются только РАССЧИТАННЫЕ сигналы (Выигрыш|Проигрыш): count — их число,
    roi/profit — по ним же. Возврат и нерассчитанные (result IS NULL) не входят.
    Порядок команд не важен (нормализация norm_pair). Текущий сигнал попадёт в
    статистику только после дорасчёта итога (при отправке его ещё нет в БД).
    """
    target = norm_pair(team1, team2)
    conn = _conn()
    rows = conn.execute(
        "SELECT team1, team2, result, profit FROM cage_signals "
        "WHERE status='sent' AND result IN ('Выигрыш', 'Проигрыш')").fetchall()
    conn.close()
    count = wins = 0
    profit = 0.0
    for r in rows:
        if norm_pair(r["team1"], r["team2"]) != target:
            continue
        count += 1
        if r["result"] == "Выигрыш":
            wins += 1
        if r["profit"] is not None:
            profit += r["profit"]
    staked = count * STAKE
    roi = (profit / staked * 100) if staked else 0.0
    return {"count": count, "wins": wins, "profit": profit, "roi": roi}


def overall_stats(market: str | None = None) -> dict:
    tot = {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "no_result": 0,
           "profit": 0.0, "staked": 0.0}
    for r in get_rules(market):
        st = rule_stats(r["id"])
        for k in ("signals", "wins", "losses", "pushes", "no_result", "profit", "staked"):
            tot[k] += st[k]
    settled = tot["wins"] + tot["losses"]
    tot["winrate"] = (tot["wins"] / settled * 100) if settled else 0.0
    tot["roi"] = (tot["profit"] / tot["staked"] * 100) if tot["staked"] else 0.0
    tot["balance"] = BANKROLL_START + tot["profit"]
    return tot


# --- статистика ПО СБОРЩИКУ (гипотетический бэктест по настройкам набора) ----
# Экран статистики набора показывает не только реально отправленные сигналы, а
# гипотетический результат по ВСЕМ матчам сборщика, подходящим под набор (та же
# игровая минута + пары/все пары + рынок), по «ровной» линии (кф ≈ 2.0), как при
# отправке сигнала. Прибыль флэт STAKE по кф снимка. По аналогии с prime.

_KIND_BY_MARKET = {"tm": "total", "it1": "it1", "it2": "it2"}


def _pick_even_line(lines: list) -> dict | None:
    """«Ровная» линия из total_lines одного снимка одного вида: наибольший кф
    «меньше» в окне CAGE_STRAT_KF_MIN..MAX, иначе наибольший доступный."""
    items = [ln for ln in lines if ln["m_odds"] is not None]
    if not items:
        return None
    in_range = [ln for ln in items
                if CAGE_STRAT_KF_MIN <= ln["m_odds"] <= CAGE_STRAT_KF_MAX]
    pool = in_range or items
    return max(pool, key=lambda ln: ln["m_odds"])


def rule_stats_from_collector(rule: dict) -> dict:
    """Гипотетическая статистика набора по СБОРЩИКУ (cage_markets.db).

    По каждому матчу берём последний снимок его игровой минуты (== rule['minute'];
    -1 = прематч), «ровную» линию нужного вида (total/it1/it2) и её результат «меньше».
    Флэт STAKE по кф снимка: Выигрыш -> +STAKE*(кф-1), Проигрыш -> -STAKE, Возврат/
    нерасчёт (r_m NULL) — не в прибыль. Пары фильтруем по набору (или «все пары»).
    """
    empty = {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "no_result": 0,
             "winrate": 0.0, "profit": 0.0, "staked": 0.0, "roi": 0.0}
    kind = _KIND_BY_MARKET.get(rule.get("market", "tm"))
    if kind is None:
        return empty
    sel = None if rule.get("all_pairs") else get_rule_pairs(rule["id"])
    try:
        conn = _source_conn()
        rows = conn.execute(
            """SELECT ms.event_id AS event_id, ms.team1 AS team1, ms.team2 AS team2,
                      tl.line AS line, tl.m_odds AS m_odds, tl.r_m AS r_m
               FROM market_snapshots ms
               JOIN (SELECT event_id, MAX(id) AS mid FROM market_snapshots
                     WHERE game_minute=? GROUP BY event_id) last ON ms.id = last.mid
               JOIN total_lines tl ON tl.snapshot_id = ms.id AND tl.kind = ?""",
            (rule["minute"], kind)).fetchall()
        conn.close()
    except sqlite3.Error:
        return empty
    by_event: dict[int, dict] = {}
    for r in rows:
        e = by_event.setdefault(r["event_id"],
                                {"team1": r["team1"], "team2": r["team2"], "lines": []})
        e["lines"].append(r)
    wins = losses = pushes = no_result = 0
    profit = 0.0
    for ev in by_event.values():
        if sel is not None and norm_pair(ev["team1"], ev["team2"]) not in sel:
            continue
        ln = _pick_even_line(ev["lines"])
        if ln is None:
            continue                       # рынок этого вида не котировался — не учитываем
        res = ln["r_m"]
        if res == "Выигрыш":
            wins += 1
            if ln["m_odds"] is not None:
                profit += STAKE * (float(ln["m_odds"]) - 1.0)
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


def pair_stats_from_collector(team1: str, team2: str, market: str, minute: int) -> dict:
    """Статистика встреч пары по СБОРЩИКУ (cage_markets.db) — для строки в тексте
    сигнала (аналог prime_women_db.pair_stats_from_collector, но с учётом рынка).

    По ВСЕМ матчам пары на той же игровой минуте (== minute; -1 = прематч) и в том же
    рынке (tm/it1/it2): берём последний снимок минуты (MAX(id)), «ровную» линию нужного
    вида (кф ≈ 2.0) и её результат «меньше», считаем гипотетическую флэт-ставку STAKE по
    кф снимка. Возврат/нерасчёт не в счёт. Порядок команд не важен (norm_pair).
    Возвращает count/wins/profit/roi. Даёт более полную выборку, чем pair_stats()
    (реально отправленные сигналы), — по всем встречам пары из сборщика."""
    empty = {"count": 0, "wins": 0, "profit": 0.0, "roi": 0.0}
    kind = _KIND_BY_MARKET.get(market or "tm")
    if kind is None:
        return empty
    target = norm_pair(team1, team2)
    try:
        conn = _source_conn()
        rows = conn.execute(
            """SELECT ms.event_id AS event_id, ms.team1 AS team1, ms.team2 AS team2,
                      tl.line AS line, tl.m_odds AS m_odds, tl.r_m AS r_m
               FROM market_snapshots ms
               JOIN (SELECT event_id, MAX(id) AS mid FROM market_snapshots
                     WHERE game_minute=? GROUP BY event_id) last ON ms.id = last.mid
               JOIN total_lines tl ON tl.snapshot_id = ms.id AND tl.kind = ?""",
            (minute, kind)).fetchall()
        conn.close()
    except sqlite3.Error:
        return empty
    by_event: dict[int, dict] = {}
    for r in rows:
        e = by_event.setdefault(r["event_id"],
                                {"team1": r["team1"], "team2": r["team2"], "lines": []})
        e["lines"].append(r)
    count = wins = 0
    profit = 0.0
    for ev in by_event.values():
        if norm_pair(ev["team1"], ev["team2"]) != target:
            continue
        ln = _pick_even_line(ev["lines"])
        if ln is None:
            continue                       # рынок этого вида не котировался — не учитываем
        res = ln["r_m"]
        if res == "Выигрыш":
            count += 1
            wins += 1
            if ln["m_odds"] is not None:
                profit += STAKE * (float(ln["m_odds"]) - 1.0)
        elif res == "Проигрыш":
            count += 1
            profit += -STAKE
        # Возврат / нерасчёт (r_m NULL) — не в счёт
    staked = count * STAKE
    roi = (profit / staked * 100) if staked else 0.0
    return {"count": count, "wins": wins, "profit": profit, "roi": roi}


def overall_stats_from_collector(market: str | None = None) -> dict:
    tot = {"signals": 0, "wins": 0, "losses": 0, "pushes": 0, "no_result": 0,
           "profit": 0.0, "staked": 0.0}
    for r in get_rules(market):
        st = rule_stats_from_collector(r)
        for k in ("signals", "wins", "losses", "pushes", "no_result", "profit", "staked"):
            tot[k] += st[k]
    settled = tot["wins"] + tot["losses"]
    tot["winrate"] = (tot["wins"] / settled * 100) if settled else 0.0
    tot["roi"] = (tot["profit"] / tot["staked"] * 100) if tot["staked"] else 0.0
    tot["balance"] = BANKROLL_START + tot["profit"]
    return tot


# --- прибыль для отчётов (день/неделя/месяц) -------------------------------

def profit_by_day(start: str, end: str, market: str | None = None) -> dict[str, float]:
    conn = _conn()
    q = ("SELECT date(created_at) AS d, COALESCE(SUM(profit), 0) AS p "
         "FROM cage_signals WHERE status='sent' AND profit IS NOT NULL "
         "AND date(created_at) BETWEEN ? AND ?")
    args = [start, end]
    if market is not None:
        q += " AND market=?"
        args.append(market)
    q += " GROUP BY d"
    rows = conn.execute(q, args).fetchall()
    conn.close()
    return {r["d"]: r["p"] for r in rows}


def profit_total(start: str, end: str, market: str | None = None) -> float:
    conn = _conn()
    q = ("SELECT COALESCE(SUM(profit), 0) FROM cage_signals "
         "WHERE status='sent' AND profit IS NOT NULL AND date(created_at) BETWEEN ? AND ?")
    args = [start, end]
    if market is not None:
        q += " AND market=?"
        args.append(market)
    v = conn.execute(q, args).fetchone()[0]
    conn.close()
    return v


def daily_counts(day: str, market: str | None = None) -> dict:
    """Счётчики исходов (В/П/Возврат) за один день ('YYYY-MM-DD') по отправленным
    сигналам (опц. фильтр по рынку) — для строки «N✅/N✖️/N♻️» в отчёте дня."""
    conn = _conn()
    base = "FROM cage_signals WHERE status='sent' AND date(created_at)=?"
    tail = " AND market=?" if market is not None else ""
    def cnt(result: str) -> int:
        args = [day] + ([market] if market is not None else [])
        return conn.execute(
            f"SELECT COUNT(*) {base}{tail} AND result=?", args + [result]).fetchone()[0]
    wins, losses, pushes = cnt("Выигрыш"), cnt("Проигрыш"), cnt("Возврат")
    conn.close()
    return {"wins": wins, "losses": losses, "pushes": pushes}


def signals_for_export(market: str | None = None) -> list[dict]:
    """Отправленные сигналы стратегии для Excel-выгрузки. market — фильтр по рынку."""
    conn = _conn()
    q = "SELECT * FROM cage_signals WHERE status='sent'"
    args: list = []
    if market is not None:
        q += " AND market=?"
        args.append(market)
    q += " ORDER BY created_at, id"
    rows = conn.execute(q, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]
