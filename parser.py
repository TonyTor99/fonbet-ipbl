"""Fonbet IPBL-баскетбол — парсер live + детект большого перерыва.

Каждые POLL_INTERVAL секунд:
  1) /events/listBase — находит live-матчи 4 лиг IPBL + их состояние (счёт, comment, таймер)
  2) /events/event (параллельно) — тоталы матча (ТМ/ТБ) для каждого матча
  3) на перерыве (таймер замер на 1200/1440) отдаёт матч в signals
  4) при завершении матча — дорасчёт сигналов

Консоль: компактный мониторинг каждого цикла (матчи, счёт, таймер, тоталы, статус перерыва).
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

import database
import signals
import collector
import collector_db
from config import (LINE_SERVERS, HEADERS, SCOPE_MARKET, POLL_INTERVAL, MAX_WORKERS,
                    LEAGUES, HALFTIME_TS, HALFTIME_TOL_AFTER, MATCH_TOTAL_FIDS,
                    COLLECTOR_LEAGUE)

log = logging.getLogger("parser")
MSK = timezone(timedelta(hours=3))

GRACE_CYCLES = 3
_SCORE_RE = re.compile(r"(\d+)-(\d+)")

_session = requests.Session()
_session.headers.update(HEADERS)

# --- состояние в памяти ---
_known: dict[int, dict] = {}        # event_id -> {league, division, team1, team2, sportId}
_last_score: dict[int, tuple] = {}  # event_id -> (s1, s2)
_last_comment: dict[int, str] = {}
_miss: dict[int, int] = {}          # event_id -> циклов отсутствия


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
# Разбор
# ---------------------------------------------------------------------------

def parse_quarters(comment: str) -> list[tuple[int, int]]:
    """'(24-24 31-32 0-0)' -> [(24,24),(31,32),(0,0)]"""
    if not comment:
        return []
    m = re.search(r"\(([^)]+)\)", comment)
    if not m:
        return []
    return [(int(a), int(b)) for a, b in _SCORE_RE.findall(m.group(1))]


def is_final_comment(comment: str) -> bool:
    return "ИТОГ" in (comment or "").upper()


def extract_totals(api_data: Optional[dict], root_id: int) -> list[dict]:
    """Тоталы МАТЧА (ТМ/ТБ) из root customFactors."""
    if not api_data:
        return []
    out, seen = [], set()
    for cf in api_data.get("customFactors", []):
        if cf.get("e") != root_id:
            continue
        for f in cf.get("factors", []):
            fid = f.get("f")
            if fid not in MATCH_TOTAL_FIDS:
                continue
            side = MATCH_TOTAL_FIDS[fid]
            line = f.get("p", 0) / 100.0
            odds = f.get("v")
            if odds is None or line <= 0:
                continue
            key = (side, line)
            if key in seen:
                continue
            seen.add(key)
            out.append({"side": side, "line": line, "odds": odds})
    return out


# ---------------------------------------------------------------------------
# Цикл
# ---------------------------------------------------------------------------

def _finalize(eid: int):
    s1, s2 = _last_score.get(eid, (0, 0))
    final_total = s1 + s2
    final_score = f"{s1}:{s2}"
    log.info("finalize ev=%s %s (тотал %s)", eid, final_score, final_total)
    signals.resolve(eid, final_score, final_total)
    collector.resolve(eid, s1, s2)
    _known.pop(eid, None)
    _last_score.pop(eid, None)
    _last_comment.pop(eid, None)
    _miss.pop(eid, None)


def run_cycle() -> list[dict]:
    data = get_listbase()
    if not data:
        raise RuntimeError("listBase недоступен")

    miscs = {m["id"]: m for m in data.get("eventMiscs", [])}
    leis = {l["eventId"]: l for l in data.get("liveEventInfos", [])}

    # live root-матчи лиг IPBL
    live_roots: list[int] = []
    for ev in data.get("events", []):
        if ev.get("level") != 1:
            continue
        sid = ev.get("sportId")
        if sid not in LEAGUES:
            continue
        eid = ev["id"]
        if eid not in miscs:
            continue  # не live
        live_roots.append(eid)
        if eid not in _known:
            name, division = LEAGUES[sid]
            _known[eid] = {
                "league": name, "division": division, "sportId": sid,
                "team1": ev.get("team1", ""), "team2": ev.get("team2", ""),
            }

    # тоталы по каждому матчу (параллельно)
    api_map = fetch_events_parallel(live_roots)

    # завершённые / пропавшие
    for eid in list(_known.keys()):
        if eid in live_roots:
            _miss.pop(eid, None)
            if miscs[eid].get("finished") or is_final_comment(miscs[eid].get("comment", "")):
                s1 = int(miscs[eid].get("score1", 0) or 0)
                s2 = int(miscs[eid].get("score2", 0) or 0)
                _last_score[eid] = (s1, s2)
                _finalize(eid)
        else:
            _miss[eid] = _miss.get(eid, 0) + 1
            if _miss[eid] >= GRACE_CYCLES:
                _finalize(eid)

    # обработка активных
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

        quarters = parse_quarters(comment)
        division = meta["division"]
        target = HALFTIME_TS[division]

        if len(quarters) >= 2:
            half_total = quarters[0][0] + quarters[0][1] + quarters[1][0] + quarters[1][1]
        else:
            half_total = s1 + s2

        at_break = (len(quarters) >= 2 and target <= ts <= target + HALFTIME_TOL_AFTER
                    and not misc.get("finished"))

        totals = extract_totals(api_map.get(eid), eid)

        state = {
            "event_id": eid, "league": meta["league"], "division": division,
            "team1": meta["team1"] or "?", "team2": meta["team2"] or "?",
            "score1": s1, "score2": s2, "ts": ts, "comment": comment,
            "quarters": quarters, "totals": totals,
            "half_total": half_total, "at_break": at_break,
        }
        try:
            signals.process_match(state)
        except Exception as e:
            log.warning("process_match err ev=%s: %s", eid, e)

        if meta["sportId"] == COLLECTOR_LEAGUE:
            try:
                collector.process(state, api_map.get(eid))
            except Exception as e:
                log.warning("collector err ev=%s: %s", eid, e)

        results.append(state)
    return results


# ---------------------------------------------------------------------------
# Консольный мониторинг
# ---------------------------------------------------------------------------

def _fmt_ts(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def print_cycle(results: list[dict], elapsed: float, it: int):
    now = datetime.now(MSK).strftime("%H:%M:%S")
    print("\n" + "=" * 72)
    print(f"[#{it}] {now} МСК | матчей: {len(results)} | цикл {elapsed:.1f}с")
    if not results:
        print("  нет live IPBL-матчей")
        return
    for r in results:
        div = r["division"].upper()
        tm_block = [t for t in r["totals"] if t["side"] == "ТМ"]
        tm_block.sort(key=lambda t: t["line"])
        chosen = signals.pick_tm_line(r["totals"])
        brk = "🟢 ПЕРЕРЫВ" if r["at_break"] else "—"
        print(f"\n  [{div}] {r['team1']} — {r['team2']}")
        print(f"    счёт {r['score1']}:{r['score2']} | ⏱ {_fmt_ts(r['ts'])} "
              f"| четв.: {r['comment'] or '—'} | {brk}")
        if tm_block:
            blk = " | ".join(f"{signals.fmt_line(t['line'])}@{signals.fmt_odds(t['odds'])}"
                             for t in tm_block)
            mark = f"  → линия {signals.fmt_line(chosen['line'])}@{signals.fmt_odds(chosen['odds'])}" if chosen else "  → нет линии в 1.95-2.1"
            print(f"    ТМ: {blk}{mark}")
        else:
            print("    ТМ: нет данных")
        if r["at_break"] and chosen:
            f = 2 * r["half_total"] - chosen["line"]
            ok = "✅ СИГНАЛ" if f <= signals.THRESHOLD else "❌ не проходит"
            print(f"    формула: 2×{r['half_total']} − {signals.fmt_line(chosen['line'])} "
                  f"= {f:.1f} (порог {signals.THRESHOLD}) {ok}")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Fonbet IPBL basketball parser")
    ap.add_argument("--reset", action="store_true", help="очистить БД перед запуском")
    ap.add_argument("--once", action="store_true", help="один цикл и выход")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    database.init_db()
    collector_db.init_db()
    if args.reset:
        database.clear_db()
        collector_db.clear_db()
        print("БД очищена (сигналы + сборщик).")

    print(f"Fonbet IPBL Parser | интервал={POLL_INTERVAL}с | расписание по МСК")
    print("Лиги:", ", ".join(n for n, _ in LEAGUES.values()))
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
