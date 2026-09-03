# -*- coding: utf-8 -*-
"""Простая отправка сообщений в Telegram через Bot API (без сторонних библиотек)."""
from __future__ import annotations

import requests


class TelegramError(RuntimeError):
    pass


def send_message(bot_token: str, chat_id: str, text: str, timeout: int = 10) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise TelegramError(f"Telegram API вернул {resp.status_code}: {resp.text[:300]}")
