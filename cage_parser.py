"""Сборщик рынков IPBL CAGE Division Fonbet (отдельный процесс).

Каждые CAGE_POLL_INTERVAL секунд:
  1) /events/listBase — находит live root-матчи лиги CAGE (sportId 146499)
  2) /events/event (параллельно) — рынки матча каждого события
  3) пишет одну строку ДО начала матча ("Не начался") и далее по строке на
     КАЖДОЙ ИГРОВОЙ минуте (0, 1, 2, ..., 40) в отдельную БД cage_markets.db
  4) на финале матча — дорасчёт результата каждого исхода по итоговому счёту

Отличия от киберспортивных сборщиков (FC 26 / NHL 26): это ЖИВОЙ баскетбол —
игровой таймер берётся из liveEventInfos.timerSeconds (фоллбэк eventMiscs),
формат 4×10 (4 четверти, границы 600/1200/1800/2400), лига определяется по
sportId, а не по префиксу названия.

Аргументы: --reset (очистить БД), --once (один цикл и выход).
"""
import argparse
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

import cage_collector_db as db
import cage_strat_db
import cage_strat_signals
from config import LINE_SERVERS, HEADERS, SCOPE_MARKET, MAX_WORKERS
from cage_config import (SPORT_ID, LEAGUE_NAME, CAGE_POLL_INTERVAL,
                         QUARTER_TS,
                         PREMATCH_MINUTE, PREMATCH_COMMENTS,
                         WIN1_FID, DRAW_FID, WIN2_FID, DC_1X_FID, DC_12_FID, DC_X2_FID,
                         FORA1_FIDS, FORA2_FIDS, TOTAL_B_FIDS, TOTAL_M_FIDS,
                         IT1_B_FIDS, IT1_M_FIDS, IT2_B_FIDS, IT2_M_FIDS,
                         BTTS_YES_FID, BTTS_NO_FID)

log = logging.getLogger("cage_parser")
MSK = timezone(timedelta(hours=3))

GRACE_CYCLES = 3
_SCORE_RE = re.compile(r"(\d+)-(\d+)")

_session = requests.Session()
_session.headers.update(HEADERS)

# --- состояние в памяти ---
_known: dict[int, dict] = {}          # event_id -> {sport_id, league, team1, team2}
_last_score: dict[int, tuple] = {}
_last_comment: dict[int, str] = {}
_miss: dict[int, int] = {}            # циклов отсутствия
_last_mark: dict[int, int] = {}       # дедуп игровой минуты
_prematch_done: set[int] = set()      # для каких событий предматч уже записан


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def get_listbase() -> Optional[dict]:
    path = f"/events/listBase?lang=ru&scopeMarket={SCOPE_MARKET}"
    for srv in LINE_SERVERS:
        try:
            r = _session.get(f"{srv}{path}", timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("events"):
                return data
        except Exception:
            continue
    return None


def get_event(eid: int) -> Optional[dict]:
    path = f"/events/event?lang=ru&version=0&eventId={eid}&scopeMarket={SCOPE_MARKET}"
    for srv in LINE_SERVERS:
        try:
            r = _session.get(f"{srv}{path}", timeout=8)
            r.raise_for_status()
            data = r.json()
            if data.get("events"):
                return data
        except Exception:
            continue
    return None


def fetch_events_parallel(ids: list[int]) -> dict[int, Optional[dict]]:
    res: dict[int, Optional[dict]] = {i: None for i in ids}
    if not ids:
        return res
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut = {pool.submit(get_event, i): i for i in ids}
        try:
            for f in as_completed(fut, timeout=25):
                try:
                    res[fut[f]] = f.result()
                except Exception:
                    pass
        except TimeoutError:
            pass
    return res


# ---------------------------------------------------------------------------
# Разбор факторов
# ---------------------------------------------------------------------------

def _root_factors(api_data, event_id) -> list[dict]:
    if not api_data:
        return []
    for cf in api_data.get("customFactors", []):
        if cf.get("e") == event_id:
            return cf.get("factors", [])
    return []


def _by_line(factors, fids) -> dict[float, float]:
    """{линия: кф} по факторам семейства (линия — числовое p/100)."""
    out = {}
    for f in factors:
        if f.get("f") in fids and f.get("p") is not None and f.get("v") is not None:
            out[round(f["p"] / 100.0, 1)] = f["v"]
    return out


def _odds(factors, fid):
    for f in factors:
        if f.get("f") == fid:
            return f.get("v")
    return None


def extract_single_markets(factors: list[dict]) -> dict:
    """Одиночные рынки: 1X2, двойные шансы, ОСНОВНАЯ фора, обе забьют."""
    m = {
        "win1_odds": None, "draw_odds": None, "win2_odds": None,
        "dc_1x_odds": None, "dc_12_odds": None, "dc_x2_odds": None,
        "fora_line": None, "fora1_odds": None, "fora2_odds": None,
        "btts_yes_odds": None, "btts_no_odds": None,
    }
    m["win1_odds"] = _odds(factors, WIN1_FID)
    m["draw_odds"] = _odds(factors, DRAW_FID)
    m["win2_odds"] = _odds(factors, WIN2_FID)
    m["dc_1x_odds"] = _odds(factors, DC_1X_FID)
    m["dc_12_odds"] = _odds(factors, DC_12_FID)
    m["dc_x2_odds"] = _odds(factors, DC_X2_FID)
    m["btts_yes_odds"] = _odds(factors, BTTS_YES_FID)
    m["btts_no_odds"] = _odds(factors, BTTS_NO_FID)

    # Фора: крайняя = самая отрицательная линия К1; парный К2 на -line.
    f1 = _by_line(factors, FORA1_FIDS)
    f2 = _by_line(factors, FORA2_FIDS)
    if f1:
        line = min(f1)
        m["fora_line"] = line
        m["fora1_odds"] = f1.get(line)
        m["fora2_odds"] = f2.get(round(-line, 1))
    return m


def extract_total_lines(factors: list[dict]) -> list[dict]:
    """ВСЕ линии тоталов матча и инд. тоталов (где есть обе стороны Б/М)."""
    out = []
    for kind, b_fids, m_fids in (
        ("total", TOTAL_B_FIDS, TOTAL_M_FIDS),
        ("it1", IT1_B_FIDS, IT1_M_FIDS),
        ("it2", IT2_B_FIDS, IT2_M_FIDS),
    ):
        bb = _by_line(factors, b_fids)
        mm = _by_line(factors, m_fids)
        for line in sorted(set(bb) & set(mm)):
            out.append({"kind": kind, "line": line,
                        "b_odds": bb.get(line), "m_odds": mm.get(line)})
    return out


def has_any_market(markets: dict, totals: list[dict]) -> bool:
    return any(v is not None for v in markets.values()) or bool(totals)


# ---------------------------------------------------------------------------
# Состояние матча
# ---------------------------------------------------------------------------

def parse_quarters(comment: str) -> list[tuple[int, int]]:
    """'(24-24)' / '(24-24 31-32 0-0)' -> список завершённых четвертей."""
    if not comment:
        return []
    mt = re.search(r"\(([^)]+)\)", comment)
    if not mt:
        return []
    return [(int(a), int(b)) for a, b in _SCORE_RE.findall(mt.group(1))]


def is_prematch(comment: str) -> bool:
    return (comment or "").strip().lower() in PREMATCH_COMMENTS


def is_final_comment(comment: str) -> bool:
    return "ИТОГ" in (comment or "").upper()


def _quarter_info(ts: int, s1: int, s2: int, quarters: list[tuple[int, int]]):
    """(номер четверти 1..4, счёт текущей четверти 'a-b').

    Номер — по числу завершённых четвертей из comment (надёжнее таймера в паузах),
    но не выше расчётного по игровому времени. Счёт текущей четверти = общий минус
    сумма завершённых."""
    q_by_time = min(4, ts // QUARTER_TS + 1)
    q = max(len(quarters) + 1, q_by_time) if quarters else q_by_time
    q = min(4, q)
    done1 = sum(a for a, _ in quarters)
    done2 = sum(b for _, b in quarters)
    cur1 = max(0, s1 - done1)
    cur2 = max(0, s2 - done2)
    return q, f"{cur1}-{cur2}"


# ---------------------------------------------------------------------------
# Запись снимка
# ---------------------------------------------------------------------------

def _write_snapshot(state: dict, markets: dict, totals: list[dict], *, prematch: bool):
    eid = state["event_id"]
    quarters = state["quarters"]
    if prematch:
        quarter = None
        quarter_score = None
    else:
        quarter, quarter_score = _quarter_info(state["ts"], state["score1"], state["score2"], quarters)
    row = {
        "event_id": eid,
        "sport_id": state.get("sport_id"),
        "league": state["league"],
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
        "snap_dt_msk": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "is_prematch": 1 if prematch else 0,
        "game_minute": PREMATCH_MINUTE if prematch else state["mark"],
        "ts": None if prematch else state["ts"],
        "quarter": quarter,
        "quarter_score": quarter_score,
        "periods": " ".join(f"{a}-{b}" for a, b in quarters) or None,
        "score1": state["score1"],
        "score2": state["score2"],
        "created_at": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
        **markets,
    }
    sid = db.insert_snapshot(row, totals)
    if sid is not None:
        tag = "prematch" if prematch else f"min={state['mark']}"
        log.info("collect ev=%s %s score=%s:%s totals=%s",
                 eid, tag, row["score1"], row["score2"], len(totals))
    return sid


def process(state: dict, api_data):
    """Пишет предматчевую строку (один раз) и далее ≤1 строку на игровую минуту."""
    eid = state["event_id"]
    factors = _root_factors(api_data, eid)
    if not factors:
        return
    markets = extract_single_markets(factors)
    totals = extract_total_lines(factors)
    if not has_any_market(markets, totals):
        return

    if state["prematch"]:
        if eid in _prematch_done or db.snapshot_exists(eid, PREMATCH_MINUTE):
            _prematch_done.add(eid)
            return
        if _write_snapshot(state, markets, totals, prematch=True) is not None:
            _prematch_done.add(eid)
        return

    mark = state["mark"]
    if mark is None:
        return
    if _last_mark.get(eid) == mark:
        return
    if db.snapshot_exists(eid, mark):
        _last_mark[eid] = mark
        return
    _write_snapshot(state, markets, totals, prematch=False)
    _last_mark[eid] = mark


# ---------------------------------------------------------------------------
# Дорасчёт результата на финале
# ---------------------------------------------------------------------------

def _wl(win: bool) -> str:
    return "Выигрыш" if win else "Проигрыш"


def _ou(value: float, line: float) -> tuple[str, str]:
    """(результат Больше, результат Меньше) с учётом пуша на целой линии."""
    if value > line:
        return "Выигрыш", "Проигрыш"
    if value < line:
        return "Проигрыш", "Выигрыш"
    return "Возврат", "Возврат"


def _handicap(s1: int, s2: int, line: float) -> tuple[str, str]:
    """(результат Фора К1, результат Фора К2). line — со стороны К1."""
    adj = s1 + line
    if adj > s2:
        return "Выигрыш", "Проигрыш"
    if adj < s2:
        return "Проигрыш", "Выигрыш"
    return "Возврат", "Возврат"


def _resolve_singles(r: dict, s1: int, s2: int) -> dict:
    res = {}
    if r.get("win1_odds") is not None:
        res["r_win1"] = _wl(s1 > s2)
    if r.get("draw_odds") is not None:
        res["r_draw"] = _wl(s1 == s2)
    if r.get("win2_odds") is not None:
        res["r_win2"] = _wl(s2 > s1)
    if r.get("dc_1x_odds") is not None:
        res["r_1x"] = _wl(s1 >= s2)
    if r.get("dc_x2_odds") is not None:
        res["r_x2"] = _wl(s2 >= s1)
    if r.get("dc_12_odds") is not None:
        res["r_12"] = _wl(s1 != s2)
    if r.get("fora_line") is not None:
        res["r_fora1"], res["r_fora2"] = _handicap(s1, s2, r["fora_line"])
    if r.get("btts_yes_odds") is not None:
        res["r_btts_yes"] = _wl(s1 > 0 and s2 > 0)
    if r.get("btts_no_odds") is not None:
        res["r_btts_no"] = _wl(not (s1 > 0 and s2 > 0))
    return res


def _resolve_total_line(kind: str, line: float, s1: int, s2: int) -> tuple[str, str]:
    if kind == "it1":
        return _ou(s1, line)
    if kind == "it2":
        return _ou(s2, line)
    return _ou(s1 + s2, line)   # total


def resolve(event_id: int, s1: int, s2: int):
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    try:
        for r in db.get_event_rows(event_id):
            if r.get("final_score") is not None:
                continue
            res = _resolve_singles(r, s1, s2)
            db.update_results(r["id"], res, final_score, final_total)
            for tl in db.get_total_lines(r["id"]):
                rb, rm = _resolve_total_line(tl["kind"], tl["line"], s1, s2)
                db.update_total_result(tl["id"], rb, rm)
        log.info("resolve ev=%s %s (тотал %s)", event_id, final_score, final_total)
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
    finally:
        _last_mark.pop(event_id, None)
        _prematch_done.discard(event_id)


# ---------------------------------------------------------------------------
# Цикл
# ---------------------------------------------------------------------------

def _finalize(eid: int):
    s1, s2 = _last_score.get(eid, (0, 0))
    resolve(eid, s1, s2)
    try:
        cage_strat_signals.resolve(eid, s1, s2)
    except Exception as e:
        log.warning("cage_strat resolve err ev=%s: %s", eid, e)
    _known.pop(eid, None)
    _last_score.pop(eid, None)
    _last_comment.pop(eid, None)
    _miss.pop(eid, None)


def _mark_for(ts: int) -> Optional[int]:
    """Игровая минута снимка: 0,1,2,...,40 (ts // 60). Раньше собирали по 5-мин
    отметкам — теперь строка на каждой игровой минуте (как collector.py)."""
    return ts // 60


def run_cycle() -> list[dict]:
    data = get_listbase()
    if not data:
        raise RuntimeError("listBase недоступен")

    sport_by_id = {s["id"]: s for s in data.get("sports", [])}
    miscs = {m["id"]: m for m in data.get("eventMiscs", [])}
    leis = {l["eventId"]: l for l in data.get("liveEventInfos", [])}

    live_roots: list[int] = []
    for ev in data.get("events", []):
        if ev.get("level") != 1:
            continue
        if ev.get("sportId") != SPORT_ID:
            continue
        eid = ev["id"]
        if eid not in miscs:
            continue  # не live
        live_roots.append(eid)
        if eid not in _known:
            league = (sport_by_id.get(SPORT_ID, {}).get("name") or LEAGUE_NAME)
            _known[eid] = {
                "sport_id": SPORT_ID, "league": league,
                "team1": ev.get("team1", ""), "team2": ev.get("team2", ""),
            }

    api_map = fetch_events_parallel(live_roots)

    # завершённые / пропавшие
    for eid in list(_known.keys()):
        if eid in live_roots:
            _miss.pop(eid, None)
            misc = miscs[eid]
            if misc.get("finished") or is_final_comment(misc.get("comment", "")):
                s1 = int(misc.get("score1", 0) or 0)
                s2 = int(misc.get("score2", 0) or 0)
                _last_score[eid] = (s1, s2)
                _finalize(eid)
        else:
            _miss[eid] = _miss.get(eid, 0) + 1
            if _miss[eid] >= GRACE_CYCLES:
                _finalize(eid)

    results = []
    for eid in live_roots:
        if eid not in _known:
            continue
        meta = _known[eid]
        misc = miscs[eid]
        lei = leis.get(eid)

        s1 = int(misc.get("score1", 0) or 0)
        s2 = int(misc.get("score2", 0) or 0)
        comment = misc.get("comment", "") or ""
        ts = lei.get("timerSeconds") if lei else misc.get("timerSeconds")
        ts = int(ts or 0)
        _last_score[eid] = (s1, s2)
        _last_comment[eid] = comment

        prematch = is_prematch(comment) or ts == 0
        quarters = parse_quarters(comment)
        state = {
            "event_id": eid, "sport_id": meta["sport_id"], "league": meta["league"],
            "team1": meta["team1"] or "?", "team2": meta["team2"] or "?",
            "score1": s1, "score2": s2, "ts": ts, "comment": comment,
            "prematch": prematch, "quarters": quarters, "mark": _mark_for(ts),
        }
        try:
            process(state, api_map.get(eid))
        except Exception as e:
            log.warning("process err ev=%s: %s", eid, e)
        try:
            cage_strat_signals.process_match(state, api_map.get(eid))
        except Exception as e:
            log.warning("cage_strat err ev=%s: %s", eid, e)
        results.append(state)
    return results


# ---------------------------------------------------------------------------
# Консоль
# ---------------------------------------------------------------------------

def _fmt_ts(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def _short_league(name: str) -> str:
    return name.replace("Россия. IPBL.", "").strip(" .")


def print_cycle(results: list[dict], elapsed: float, it: int):
    now = datetime.now(MSK).strftime("%H:%M:%S")
    print("\n" + "=" * 72)
    print(f"[#{it}] {now} МСК | матчей: {len(results)} | цикл {elapsed:.1f}с")
    if not results:
        print("  нет live матчей IPBL CAGE Division")
        return
    for r in results:
        lg = _short_league(r["league"])
        q = min(4, r["ts"] // QUARTER_TS + 1)
        flag = "🟡 ДО МАТЧА" if r["prematch"] else f"⏱ {_fmt_ts(r['ts'])} · четв {q} · отм {r['mark']}"
        print(f"  [{lg}] {r['team1']} — {r['team2']} | счёт {r['score1']}:{r['score2']} | {flag}")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Fonbet IPBL CAGE Division market collector")
    ap.add_argument("--reset", action="store_true", help="очистить БД перед запуском")
    ap.add_argument("--once", action="store_true", help="один цикл и выход")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    db.init_db()
    cage_strat_db.init_db()   # БД стратегии CAGE (сигналы) — на случай автономного запуска
    if args.reset:
        db.clear_db()
        print("БД CAGE Division очищена.")

    print(f"Fonbet IPBL CAGE Division Collector | интервал={CAGE_POLL_INTERVAL}с")
    print(f"Лига: {LEAGUE_NAME} (sportId {SPORT_ID})")
    print("Ctrl+C для остановки\n")

    it = 0
    while True:
        it += 1
        t0 = time.time()
        try:
            results = run_cycle()
            print_cycle(results, time.time() - t0, it)
        except KeyboardInterrupt:
            print("\nОстановлено.")
            break
        except Exception as e:
            log.error("цикл #%s: %s", it, e)
        if args.once:
            break
        try:
            time.sleep(max(0, CAGE_POLL_INTERVAL - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\nОстановлено.")
            break


if __name__ == "__main__":
    main()
