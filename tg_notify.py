"""Тонкий синхронный клиент Telegram Bot API для процесса парсера (через requests)."""
import logging
import requests

from config import BOT_TOKEN

log = logging.getLogger("signals.tg")
_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send(chat_id: int, text: str) -> int | None:
    """Отправляет сообщение. Возвращает message_id или None при ошибке."""
    try:
        r = requests.post(
            f"{_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
        log.warning("sendMessage failed: %s", data)
    except Exception as e:
        log.warning("sendMessage error: %s", e)
    return None


def edit(chat_id: int, message_id: int, text: str) -> bool:
    try:
        r = requests.post(
            f"{_API}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            return True
        log.warning("editMessageText failed: %s", data)
    except Exception as e:
        log.warning("editMessageText error: %s", e)
    return False
