"""Движок стратегии АУТОВ (вброс аутов) — футбол, прематч.

Поток (вызывается из auts_parser.py каждый цикл над данными listBase):
  • process(data) — найти кандидатов, у кого ОТКРЫТ рынок тотала аутов, и, если
    условия по кэфам выполнены, отправить сигнал ТМ аутов (один на матч, дедуп в БД).
  • track(data)   — вести подписанные матчи до финала: снимать живой счётчик аутов,
    на финале — дорасчёт зашло/не зашло (ТМ: факт аутов < линия).

Условия кандидата (в момент открытия рынка аутов): ничья(f922) ≥ DRAW_MIN И
ТБ 2.5 голов (over по pt=2.5) ≤ OVER25_MAX. Линия — ТМ аутов с наибольшим кэфом в
окне [KF_MIN, KF_MAX], иначе ближайшая к KF_TARGET. Блэклист лиг — по подстроке.

Резолв берёт факт аутов из liveEventInfos дочернего события «вброс аутов» (счёт
аутов живьём). См. ref_fonbet_football_auts_api.
"""
import html
import logging
import time
from datetime import datetime, timezone, timedelta

import auts_db
import tg_notify
import database
from auts_config import (
    FOOTBALL_ROOT_SPORT_ID, AUT_EVENT_NAME, DRAW_FID,
    TOTAL_B_FIDS, TOTAL_M_FIDS, GOALS_TOTAL_LINE,
    DRAW_MIN, OVER25_MAX, KF_MIN, KF_MAX, KF_TARGET,
    AUTS_STRAT_CODE, STAKE,
)

log = logging.getLogger("auts_signals")
MSK = timezone(timedelta(hours=3))

# Матч считаем завершённым, если он пропал из фида и с его старта прошло столько сек
# (футбол + перерыв + добавленное + запас). После этого дорасчёт по последнему счётчику.
FINISH_GRACE_SEC = 3 * 3600 + 1800        # 3.5 ч


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


# --- дерево видов спорта / футбол -------------------------------------------

def football_sport_ids(data: dict) -> set:
    """Множество sportId всех узлов-потомков корня 'Футбол' (включая лиги)."""
    sports = data.get("sports", [])
    by_id = {s["id"]: s for s in sports}
    roots = {s["id"] for s in sports
             if (s.get("name") or "").strip().lower() == "футбол"} or {FOOTBALL_ROOT_SPORT_ID}
    out = set()
    for s in sports:
        seen = set()
        cur = s
        while cur is not None and cur.get("id") not in seen:
            seen.add(cur.get("id"))
            if cur.get("id") in roots:
                out.add(s["id"])
                break
            cur = by_id.get(cur.get("parentId"))
    return out


# --- разбор факторов --------------------------------------------------------

def _odds(factors, fid):
    for f in factors:
        if f.get("f") == fid:
            return f.get("v")
    return None


def _ladder(factors, fids) -> dict:
    """{линия: кф} по семейству factorId (линия — p/100)."""
    out = {}
    for f in factors:
        if f.get("f") in fids and f.get("p") is not None and f.get("v") is not None:
            out[round(f["p"] / 100.0, 1)] = f["v"]
    return out


def goals_over_at(factors, line: float):
    """Кэф ТБ голов на заданной линии (из семейства «больше»)."""
    return _ladder(factors, TOTAL_B_FIDS).get(round(line, 1))


def pick_under_line(ladder: dict):
    """Выбор линии ТМ аутов. Наибольший кэф в окне [KF_MIN, KF_MAX]; если ни одна
    линия в окно не попала — ближайшая к KF_TARGET. (линия, кф) или (None, None)."""
    if not ladder:
        return None, None
    inwin = {ln: od for ln, od in ladder.items() if KF_MIN <= od <= KF_MAX}
    if inwin:
        ln = max(inwin, key=lambda l: inwin[l])   # наибольший кф в окне
        return ln, inwin[ln]
    ln = min(ladder, key=lambda l: abs(ladder[l] - KF_TARGET))
    return ln, ladder[ln]


# --- блэклист ---------------------------------------------------------------

def is_blacklisted(league: str, tokens: list[str]) -> bool:
    low = (league or "").lower()
    return any(tok.lower() in low for tok in tokens if tok.strip())


# --- отбор кандидатов -------------------------------------------------------

def scan(data: dict) -> list[dict]:
    """Чистый отбор: список матчей, у кого ОТКРЫТ рынок аутов И выполнены условия.
    Дедуп/отправка — выше по стеку (process). Блэклист применяется здесь."""
    now = int(time.time())
    fb_ids = football_sport_ids(data)
    sports = {s["id"]: s for s in data.get("sports", [])}
    events = data.get("events", [])
    ev_by_id = {e["id"]: e for e in events}
    cf_by_e = {b.get("e"): b.get("factors", []) for b in data.get("customFactors", [])}
    tokens = auts_db.get_blacklist()

    # дочерние «вброс аутов» по матчу-родителю
    aut_child = {}
    for e in events:
        if (e.get("name") or "").strip().lower() == AUT_EVENT_NAME and e.get("parentId"):
            aut_child[e["parentId"]] = e["id"]

    out = []
    for e in events:
        eid = e["id"]
        if e.get("sportId") not in fb_ids:
            continue
        if e.get("place") != "line":            # только прематч (не live/notActive)
            continue
        if (e.get("name") or "") != "":         # только корневой матч (у статсобытий есть name)
            continue
        if not e.get("startTime") or e["startTime"] <= now:
            continue
        child_id = aut_child.get(eid)
        if child_id is None:
            continue                            # рынок аутов ещё не открыт — ждём
        child_f = cf_by_e.get(child_id, [])
        under = _ladder(child_f, TOTAL_M_FIDS)
        if not under:
            continue                            # ладдер аутов ещё пустой
        league = sports.get(e.get("sportId"), {}).get("name") or "?"
        if is_blacklisted(league, tokens):
            continue
        main_f = cf_by_e.get(eid, [])
        draw = _odds(main_f, DRAW_FID)
        over25 = goals_over_at(main_f, GOALS_TOTAL_LINE)
        if draw is None or over25 is None:
            continue                            # нет нужных рынков матча
        if not (draw >= DRAW_MIN and over25 <= OVER25_MAX):
            continue
        line, odds = pick_under_line(under)
        if line is None:
            continue
        out.append({
            "event_id": eid,
            "aut_child_id": child_id,
            "league": league,
            "team1": e.get("team1") or "?",
            "team2": e.get("team2") or "?",
            "start_time": e["startTime"],
            "line": line,
            "odds": odds,
            "draw_odds": draw,
            "over25_odds": over25,
        })
    return out


# --- рендер -----------------------------------------------------------------

def fmt_num(o) -> str:
    if o is None:
        return "—"
    return f"{float(o):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_start(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), MSK).strftime("%d.%m %H:%M")
    except Exception:
        return "—"


def render_signal(sig: dict) -> str:
    lines = [
        "🥅 <b>АУТЫ · СИГНАЛ</b>",
        f"⚔️<code>{html.escape(sig['team1'])} - {html.escape(sig['team2'])}</code>",
        f"🏆 {html.escape(sig.get('league', '—'))}",
        f"🕒 Начало {fmt_start(sig.get('start_time'))} МСК",
        "",
        f"∅ Ничья @{fmt_num(sig.get('draw_odds'))} · ТБ 2.5 @{fmt_num(sig.get('over25_odds'))}",
        f"🎯 <b>ТМ аутов {fmt_num(sig['line'])} @{fmt_num(sig['odds'])}</b>",
    ]
    if sig.get("result"):
        fa = sig.get("final_auts")
        lines.append("")
        lines.append(f"🏁 <b>Аутов: {fa if fa is not None else '—'}</b>")
        if sig["result"] == "Выигрыш":
            lines.append("✅ <b>Зашло</b>")
        elif sig["result"] == "Проигрыш":
            lines.append("❌ <b>Не зашло</b>")
        elif sig["result"] == "Возврат":
            lines.append("↩️ <b>Возврат</b>")
    return "\n".join(lines)


# --- отправка ---------------------------------------------------------------

def process(data: dict):
    """Найти кандидатов и отправить новые сигналы (один на матч)."""
    if not auts_db.is_enabled():
        return
    try:
        cands = scan(data)
    except Exception as e:
        log.warning("scan err: %s", e)
        return
    chat_id = database.get_chat_id(AUTS_STRAT_CODE)
    for c in cands:
        existing = auts_db.get_by_event(c["event_id"])
        if existing is not None:
            # Сигнал уже записан. Если он ещё НЕ отправлен (чата не было в момент
            # записи), а теперь чат появился и матч всё ещё прематч — досылаем.
            if (existing.get("message_id") is None and existing.get("status") == "sent"
                    and chat_id is not None):
                mid = tg_notify.send(chat_id, render_signal(existing))
                if mid is not None:
                    auts_db.mark_sent(existing["id"], chat_id, mid)
                    log.info("AUTS late-send ev=%s chat=%s", c["event_id"], chat_id)
            continue
        sig = dict(c)
        sig.update(chat_id=chat_id, message_id=None, status="sent",
                   result=None, won=None, final_auts=None, last_auts=None,
                   live_seen=0, profit=None, created_at=_now())
        if chat_id is not None:
            sig["message_id"] = tg_notify.send(chat_id, render_signal(sig))
        sid = auts_db.insert_signal(sig)
        if sid is None:
            continue
        log.info("AUTS ev=%s %s-%s line=%s odds=%s draw=%s over25=%s chat=%s",
                 c["event_id"], c["team1"], c["team2"], c["line"], c["odds"],
                 c["draw_odds"], c["over25_odds"], chat_id)


# --- резолв -----------------------------------------------------------------

def _aut_count(data: dict, child_id: int):
    """Живой счётчик аутов из liveEventInfos дочернего события (сумма сторон)."""
    for l in data.get("liveEventInfos", []):
        if l.get("eventId") == child_id:
            sc = l.get("scores") or []
            try:
                cell = sc[0][0]
                return int(cell.get("c1", 0)) + int(cell.get("c2", 0))
            except Exception:
                return None
    return None


def _resolve(sig: dict, final_auts):
    """Дорасчёт ТМ аутов: зашло, если факт < линия; пуш — если равно."""
    line = sig["line"]
    if final_auts is None:
        # нет данных по факту аутов — оставляем нерешённым, помечаем no_data
        auts_db.mark_no_data(sig["id"])
        log.warning("AUTS resolve no-data ev=%s", sig["event_id"])
        return
    if final_auts == line:
        result, won, profit = "Возврат", None, 0.0
    elif final_auts < line:
        result, won, profit = "Выигрыш", 1, STAKE * (float(sig["odds"]) - 1.0)
    else:
        result, won, profit = "Проигрыш", 0, float(-STAKE)
    auts_db.update_result(sig["id"], result, won, int(final_auts), profit)
    if sig.get("status") == "sent" and sig.get("message_id") and sig.get("chat_id") is not None:
        s2 = dict(sig)
        s2.update(result=result, final_auts=int(final_auts))
        tg_notify.edit(sig["chat_id"], sig["message_id"], render_signal(s2))
    log.info("AUTS resolve ev=%s auts=%s line=%s -> %s", sig["event_id"], final_auts, line, result)


def track(data: dict):
    """Снять живой счётчик аутов подписанных матчей и дорасчитать завершённые."""
    now = int(time.time())
    ev_ids = {e["id"] for e in data.get("events", [])}
    miscs = {m["id"]: m for m in data.get("eventMiscs", [])}
    for sig in auts_db.get_unresolved():
        eid = sig["event_id"]
        child = sig.get("aut_child_id")
        if eid in ev_ids:
            cnt = _aut_count(data, child) if child else None
            if cnt is not None:
                auts_db.update_tracking(sig["id"], cnt)
                sig["last_auts"] = cnt
                sig["live_seen"] = 1
            misc = miscs.get(eid)
            if misc and (misc.get("finished") or "ИТОГ" in (misc.get("comment", "") or "").upper()):
                _resolve(sig, sig.get("last_auts"))
        else:
            # матч пропал из фида — если точно отыгран по времени, дорасчёт
            if sig.get("start_time") and now > sig["start_time"] + FINISH_GRACE_SEC:
                _resolve(sig, sig.get("last_auts"))
