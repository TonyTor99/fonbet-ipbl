"""Стратегия АУТОВ (вброс аутов) — отдельный процесс-поллер.

Каждые AUTS_POLL_INTERVAL секунд тянет /events/listBase (все футбольные матчи дня)
и передаёт данные движку auts_signals:
  • process() — новые сигналы ТМ аутов по кандидатам с открытым рынком аутов;
  • track()   — живой счётчик аутов подписанных матчей и дорасчёт завершённых.

Один запрос listBase на цикл — без веера по событиям (всё нужное в нём, см.
ref_fonbet_football_auts_api). Аргументы: --once (один цикл и выход).
"""
import argparse
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

import auts_db
import auts_signals
from auts_config import AUTS_POLL_INTERVAL
from config import LINE_SERVERS, HEADERS, SCOPE_MARKET

log = logging.getLogger("auts_parser")
MSK = timezone(timedelta(hours=3))

_session = requests.Session()
_session.headers.update(HEADERS)


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


def run_cycle() -> dict:
    data = get_listbase()
    if not data:
        raise RuntimeError("listBase недоступен")
    try:
        cands = auts_signals.scan(data)
    except Exception as e:
        log.warning("scan err: %s", e)
        cands = []
    auts_signals.process(data)
    auts_signals.track(data)
    return {"candidates": cands, "pending": auts_db.pending_count()}


def print_cycle(info: dict, elapsed: float, it: int):
    now = datetime.now(MSK).strftime("%H:%M:%S")
    cands = info["candidates"]
    print("\n" + "=" * 72)
    print(f"[#{it}] {now} МСК | кандидатов с открытым рынком аутов: {len(cands)} | "
          f"ждут дорасчёта: {info['pending']} | цикл {elapsed:.1f}с")
    for c in cands[:20]:
        print(f"  [{auts_signals.fmt_start(c['start_time'])}] {c['team1']} — {c['team2']} "
              f"| {c['league']} | ТМ {c['line']} @{c['odds']} "
              f"(X@{c['draw_odds']} ТБ2.5@{c['over25_odds']})")


def main():
    ap = argparse.ArgumentParser(description="Fonbet football throw-ins (ауты) strategy poller")
    ap.add_argument("--once", action="store_true", help="один цикл и выход")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    auts_db.init_db()

    print(f"Fonbet АУТЫ (вброс аутов) | интервал={AUTS_POLL_INTERVAL}с")
    print("Ctrl+C для остановки\n")

    it = 0
    while True:
        it += 1
        t0 = time.time()
        try:
            info = run_cycle()
            print_cycle(info, time.time() - t0, it)
        except KeyboardInterrupt:
            print("\nОстановлено.")
            break
        except Exception as e:
            log.error("цикл #%s: %s", it, e)
        if args.once:
            break
        try:
            time.sleep(max(0, AUTS_POLL_INTERVAL - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\nОстановлено.")
            break


if __name__ == "__main__":
    main()
