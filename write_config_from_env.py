#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Собирает config.json из переменных окружения — используется ТОЛЬКО внутри
GitHub Actions, где секреты (cookies, токены) приходят как переменные
окружения, а не лежат в файле репозитория (репозиторий публичный).

Не запускайте этот файл вручную на своём компьютере — там используется
обычный config.json, который вы редактируете напрямую.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def env(name: str, default: str = "") -> str:
    """
    Читает переменную окружения. Важно: в GitHub Actions переменная,
    привязанная к несозданному секрету (${{ secrets.НЕСУЩЕСТВУЮЩИЙ }}),
    всё равно ПРИСУТСТВУЕТ в окружении, но как ПУСТАЯ СТРОКА — а не
    отсутствует полностью. Поэтому os.environ.get() тут не подходит:
    он вернул бы default только при полном отсутствии переменной, а не
    при пустом значении. Здесь пустая строка тоже считается "не задано".
    """
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def main():
    fuel_filter_raw = env("GPN_FUEL_FILTER")  # например "95,100" или пусто
    fuel_filter = [s.strip() for s in fuel_filter_raw.split(",") if s.strip()] or None

    config = {
        "cookies": env("GPN_COOKIES"),
        "x_csrftoken": env("GPN_X_CSRFTOKEN"),
        "region_id": int(env("GPN_REGION_ID", "2612765")),
        "region_name": env("GPN_REGION_NAME", "Свердловская область"),
        "city": env("GPN_CITY") or None,
        "fuel_filter": fuel_filter,
        "telegram_bot_token": env("GPN_TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": env("GPN_TELEGRAM_CHAT_ID"),
        "poll_delay_between_stations_sec": float(env("GPN_POLL_DELAY", "0.6")),
        "dashboard_filename": env("GPN_DASHBOARD_FILENAME", "index.html"),
        "yandex_maps_api_key": env("GPN_YANDEX_MAPS_API_KEY") or None,
    }

    missing = [k for k in ("cookies", "x_csrftoken") if not config[k]]
    if missing:
        raise SystemExit(
            f"Не заданы обязательные секреты: {missing}. "
            f"Проверьте GPN_COOKIES и GPN_X_CSRFTOKEN в настройках репозитория "
            f"(Settings → Secrets and variables → Actions)."
        )

    config_path = BASE_DIR / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"config.json собран из переменных окружения ({config_path}).")


if __name__ == "__main__":
    main()
