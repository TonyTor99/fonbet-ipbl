"""Тонкий синхронный клиент VK API для отправки сигналов (через requests).

VK — дополнительный канал: шлём ТОЛЬКО первичный сигнал, без обновления результата
(в отличие от Telegram, где итог правится через editMessageText). Токен и глобальный
выключатель берём из БД (задаются кнопками бота), версия API — из config. Текст
сигналов приходит в HTML (для Telegram), здесь чистим его до plain-text для VK.
"""
import html
import logging
import random
import re

import requests

import database
from config import VK_API_VERSION

log = logging.getLogger("signals.vk")
_API = "https://api.vk.com/method/{method}"

_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    """HTML-подпись Telegram → чистый текст для VK (ссылки как «текст (url)»)."""
    if not s:
        return ""
    s = re.sub(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', r"\2 (\1)", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    return html.unescape(s)


def _call(method: str, params: dict, token: str, timeout: int = 15):
    payload = {**params, "access_token": token, "v": VK_API_VERSION}
    r = requests.post(_API.format(method=method), data=payload, timeout=timeout)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"VK {method}: {data['error'].get('error_msg')}")
    return data["response"]


def send(peer_id, text_html: str) -> int | None:
    """Отправляет текст сигнала в VK-беседу peer_id. Возвращает message_id или None.
    Уважает глобальный выключатель VK и наличие токена/peer_id."""
    if peer_id is None:
        return None
    if not database.get_vk_enabled():
        return None
    token = database.get_vk_token()
    if not token:
        return None
    try:
        return _call("messages.send", {
            "peer_id": peer_id,
            "message": html_to_text(text_html),
            "random_id": random.randint(1, 2_000_000_000),
            "dont_parse_links": 1,
        }, token)
    except Exception as e:
        log.warning("VK send peer=%s error: %s", peer_id, e)
        return None


def list_conversations(count: int = 100, timeout: int = 20) -> list[dict]:
    """Список бесед/чатов VK: [{'title','peer_id','type'}] — чтобы взять peer_id."""
    token = database.get_vk_token()
    if not token:
        raise RuntimeError("VK-токен не задан")
    resp = _call("messages.getConversations",
                 {"count": count, "extended": 1, "filter": "all"}, token, timeout)
    profiles = {p["id"]: p for p in resp.get("profiles", [])}
    groups = {g["id"]: g for g in resp.get("groups", [])}
    out: list[dict] = []
    for item in resp.get("items", []):
        conv = item.get("conversation", {})
        peer = conv.get("peer", {})
        pid = peer.get("id")
        ptype = peer.get("type")
        if ptype == "chat":
            title = (conv.get("chat_settings") or {}).get("title") or f"Беседа {pid}"
        elif ptype == "user":
            p = profiles.get(pid, {})
            title = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or f"User {pid}"
        elif ptype == "group":
            g = groups.get(abs(pid) if pid else 0, {})
            title = g.get("name") or f"Group {pid}"
        else:
            title = f"{ptype} {pid}"
        out.append({"title": title, "peer_id": pid, "type": ptype})
    return out
