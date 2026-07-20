"""Сборщик рынков шорт-хоккея Fonbet (отдельный процесс).

Каждые POLL_INTERVAL секунд:
  1) /events/listBase — находит все live root-матчи лиг "Шорт-хоккей."
  2) /events/event (параллельно) — рынки матча каждого события
  3) пишет одну строку ДО начала матча ("Не начался") и далее раз в игровую
     минуту крайнюю линию каждого рынка в отдельную БД
  4) на финале матча — дорасчёт результата каждого исхода

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

import sh_collector_db as db
from config import LINE_SERVERS, HEADERS, SCOPE_MARKET, POLL_INTERVAL, MAX_WORKERS
from sh_config import (LEAGUE_PREFIX, PREMATCH_MINUTE, PREMATCH_COMMENT,
                       WIN1_FID, DRAW_FID, WIN2_FID, DC_1X_FID, DC_12_FID, DC_X2_FID,
                       FORA1_FIDS, FORA2_FIDS, TOTAL_B_FIDS, TOTAL_M_FIDS,
                       IT1_B_FIDS, IT1_M_FIDS, IT2_B_FIDS, IT2_M_FIDS)

log = logging.getLogger("sh_parser")
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
_last_minute: dict[int, int] = {}     # дедуп игровой минуты
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


def extract_markets(factors: list[dict]) -> dict:
    """Крайняя (верхняя) линия каждого рынка матча."""
    m = {
        "win1_odds": None, "draw_odds": None, "win2_odds": None,
        "dc_1x_odds": None, "dc_12_odds": None, "dc_x2_odds": None,
        "fora_line": None, "fora1_odds": None, "fora2_odds": None,
        "total_line": None, "total_b_odds": None, "total_m_odds": None,
        "it1_line": None, "it1_b_odds": None, "it1_m_odds": None,
        "it2_line": None, "it2_b_odds": None, "it2_m_odds": None,
    }
    # Исход + двойные шансы
    m["win1_odds"] = _odds(factors, WIN1_FID)
    m["draw_odds"] = _odds(factors, DRAW_FID)
    m["win2_odds"] = _odds(factors, WIN2_FID)
    m["dc_1x_odds"] = _odds(factors, DC_1X_FID)
    m["dc_12_odds"] = _odds(factors, DC_12_FID)
    m["dc_x2_odds"] = _odds(factors, DC_X2_FID)

    # Фора: крайняя = самая отрицательная линия К1; парный К2 на -line
    f1 = _by_line(factors, FORA1_FIDS)
    f2 = _by_line(factors, FORA2_FIDS)
    if f1:
        line = min(f1)
        m["fora_line"] = line
        m["fora1_odds"] = f1.get(line)
        m["fora2_odds"] = f2.get(round(-line, 1))

    # Тотал / инд.тоталы: крайняя = минимальная линия, где есть обе стороны
    for pref, b_fids, m_fids in (
        ("total", TOTAL_B_FIDS, TOTAL_M_FIDS),
        ("it1", IT1_B_FIDS, IT1_M_FIDS),
        ("it2", IT2_B_FIDS, IT2_M_FIDS),
    ):
        bb = _by_line(factors, b_fids)
        mm = _by_line(factors, m_fids)
        both = sorted(set(bb) & set(mm))
        if both:
            line = both[0]
            m[f"{pref}_line"] = line
            m[f"{pref}_b_odds"] = bb.get(line)
            m[f"{pref}_m_odds"] = mm.get(line)
    return m


def has_any_market(markets: dict) -> bool:
    return any(v is not None for v in markets.values())


# ---------------------------------------------------------------------------
# Состояние матча
# ---------------------------------------------------------------------------

def parse_periods(comment: str) -> list[tuple[int, int]]:
    """'(1-0 0-2)' / 'ИТОГ (3-1 0-3 0-3)' -> [(1,0),(0,2)] (завершённые периоды)."""
    if not comment:
        return []
    mt = re.search(r"\(([^)]+)\)", comment)
    if not mt:
        return []
    return [(int(a), int(b)) for a, b in _SCORE_RE.findall(mt.group(1))]


def is_prematch(comment: str) -> bool:
    return (comment or "").strip().lower() == PREMATCH_COMMENT.lower()


def is_final_comment(comment: str) -> bool:
    return "ИТОГ" in (comment or "").upper()


# ---------------------------------------------------------------------------
# Запись снимка
# ---------------------------------------------------------------------------

def _write_snapshot(state: dict, markets: dict, *, prematch: bool):
    eid = state["event_id"]
    periods = state["periods"]
    row = {
        "event_id": eid,
        "sport_id": state.get("sport_id"),
        "league": state["league"],
        "team1": state.get("team1") or "?",
        "team2": state.get("team2") or "?",
        "snap_dt_msk": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "is_prematch": 1 if prematch else 0,
        "game_minute": PREMATCH_MINUTE if prematch else state["minute"],
        "period": None if prematch else (len(periods) + 1),
        "periods": " ".join(f"{a}-{b}" for a, b in periods) or None,
        "score1": state["score1"],
        "score2": state["score2"],
        "created_at": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
        **markets,
    }
    sid = db.insert_snapshot(row)
    if sid is not None:
        tag = "prematch" if prematch else f"min={state['minute']}"
        log.info("collect ev=%s %s score=%s:%s tot=%s fora=%s",
                 eid, tag, row["score1"], row["score2"],
                 markets["total_line"], markets["fora_line"])
    return sid


def process(state: dict, api_data):
    """Пишет предматчевую строку (один раз) и далее ≤1 строку в игровую минуту."""
    eid = state["event_id"]
    factors = _root_factors(api_data, eid)
    if not factors:
        return
    markets = extract_markets(factors)
    if not has_any_market(markets):
        return

    if state["prematch"]:
        if eid in _prematch_done or db.snapshot_exists(eid, PREMATCH_MINUTE):
            _prematch_done.add(eid)
            return
        if _write_snapshot(state, markets, prematch=True) is not None:
            _prematch_done.add(eid)
        return

    minute = state["minute"]
    if _last_minute.get(eid) == minute:
        return
    if db.snapshot_exists(eid, minute):
        _last_minute[eid] = minute
        return
    _write_snapshot(state, markets, prematch=False)
    _last_minute[eid] = minute


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


def _resolve_row(r: dict, s1: int, s2: int) -> dict:
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

    fl = r.get("fora_line")
    if fl is not None:
        res["r_fora1"], res["r_fora2"] = _handicap(s1, s2, fl)

    tl = r.get("total_line")
    if tl is not None:
        res["r_total_b"], res["r_total_m"] = _ou(s1 + s2, tl)
    i1 = r.get("it1_line")
    if i1 is not None:
        res["r_it1_b"], res["r_it1_m"] = _ou(s1, i1)
    i2 = r.get("it2_line")
    if i2 is not None:
        res["r_it2_b"], res["r_it2_m"] = _ou(s2, i2)
    return res


def resolve(event_id: int, s1: int, s2: int):
    final_score = f"{s1}:{s2}"
    final_total = s1 + s2
    try:
        for r in db.get_event_rows(event_id):
            if r.get("final_score") is not None:
                continue
            res = _resolve_row(r, s1, s2)
            if res:
                db.update_results(r["id"], res, final_score, final_total)
        log.info("resolve ev=%s %s (тотал %s)", event_id, final_score, final_total)
    except Exception as e:
        log.warning("resolve err ev=%s: %s", event_id, e)
    finally:
        _last_minute.pop(event_id, None)
        _prematch_done.discard(event_id)


# ---------------------------------------------------------------------------
# Цикл
# ---------------------------------------------------------------------------

def _finalize(eid: int, comment: str = ""):
    s1, s2 = _last_score.get(eid, (0, 0))
    resolve(eid, s1, s2)
    _known.pop(eid, None)
    _last_score.pop(eid, None)
    _last_comment.pop(eid, None)
    _miss.pop(eid, None)


def run_cycle() -> list[dict]:
    data = get_listbase()
    if not data:
        raise RuntimeError("listBase недоступен")

    sport_by_id = {s["id"]: s for s in data.get("sports", [])}
    miscs = {m["id"]: m for m in data.get("eventMiscs", [])}
    leis = {l["eventId"]: l for l in data.get("liveEventInfos", [])}

    def is_sh(sid) -> Optional[str]:
        nm = (sport_by_id.get(sid, {}).get("name") or "")
        return nm if nm.upper().startswith(LEAGUE_PREFIX) else None

    live_roots: list[int] = []
    for ev in data.get("events", []):
        if ev.get("level") != 1:
            continue
        league = is_sh(ev.get("sportId"))
        if not league:
            continue
        eid = ev["id"]
        if eid not in miscs:
            continue  # не live
        live_roots.append(eid)
        if eid not in _known:
            _known[eid] = {
                "sport_id": ev.get("sportId"), "league": league,
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
                _finalize(eid, misc.get("comment", ""))
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

        prematch = is_prematch(comment)
        periods = parse_periods(comment)
        state = {
            "event_id": eid, "sport_id": meta["sport_id"], "league": meta["league"],
            "team1": meta["team1"] or "?", "team2": meta["team2"] or "?",
            "score1": s1, "score2": s2, "ts": ts, "comment": comment,
            "prematch": prematch, "periods": periods, "minute": ts // 60,
        }
        try:
            process(state, api_map.get(eid))
        except Exception as e:
            log.warning("process err ev=%s: %s", eid, e)
        results.append(state)
    return results


# ---------------------------------------------------------------------------
# Консоль
# ---------------------------------------------------------------------------

def _fmt_ts(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def _short_league(name: str) -> str:
    return name.replace("Шорт-хоккей.", "").strip(" .")


def print_cycle(results: list[dict], elapsed: float, it: int):
    now = datetime.now(MSK).strftime("%H:%M:%S")
    print("\n" + "=" * 72)
    print(f"[#{it}] {now} МСК | матчей: {len(results)} | цикл {elapsed:.1f}с")
    if not results:
        print("  нет live матчей шорт-хоккея")
        return
    for r in results:
        lg = _short_league(r["league"])
        st = db.snapshot_exists(r["event_id"], PREMATCH_MINUTE)
        flag = "🟡 ДО МАТЧА" if r["prematch"] else f"⏱ {_fmt_ts(r['ts'])} · P{len(r['periods']) + 1}"
        pm = " [pre✓]" if st and not r["prematch"] else ""
        print(f"  [{lg}] {r['team1']} — {r['team2']} | счёт {r['score1']}:{r['score2']} | {flag}{pm}")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Fonbet short-hockey market collector")
    ap.add_argument("--reset", action="store_true", help="очистить БД перед запуском")
    ap.add_argument("--once", action="store_true", help="один цикл и выход")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    db.init_db()
    if args.reset:
        db.clear_db()
        print("БД шорт-хоккея очищена.")

    print(f"Fonbet Short-Hockey Collector | интервал={POLL_INTERVAL}с")
    print(f"Лиги: все '{LEAGUE_PREFIX}...' (авто-подхват)")
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
            time.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\nОстановлено.")
            break


if __name__ == "__main__":
    main()
