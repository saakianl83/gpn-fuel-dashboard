#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Один проход опроса: получить список станций региона, для каждой станции узнать
детальный статус топлива, сравнить с сохранённым, уведомить в Telegram при изменениях.

Запускать по расписанию (cron / Планировщик заданий), например каждые 10-15 минут:

    */15 * * * * cd /path/to/gpn_bot && /usr/bin/python3 poll.py >> poll.log 2>&1

Не держит процесс постоянно запущенным — один запуск = один опрос, скрипт завершается.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from gpn_api import GpnClient, GpnAuthError, GpnApiError
from storage import Storage
import telegram

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "gpn_stations.db"

FUEL_EMOJI = {"gazoline": "⛽", "dizel": "🛢", "gaz": "🔥"}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(
            f"Не найден {CONFIG_PATH}. Скопируйте config.example.json в config.json "
            f"и заполните cookies/токены (см. README).",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def format_change_message(station, change) -> str:
    """Собирает читаемое сообщение для Telegram по одному изменению статуса."""
    addr = station.full_address if hasattr(station, "full_address") else ""
    name = station.name if hasattr(station, "name") else f"АЗС #{change.station_id}"

    lines = [f"⛽️ <b>{name}</b>"]
    if addr:
        lines.append(addr)

    fuel = change.fuel_title or f"топливо {change.fuel_id}"

    # avail изменился
    if change.old_avail != change.new_avail:
        if change.new_avail:
            lines.append(f"✅ {fuel}: появилось в продаже")
            if change.since_prev_change:
                lines.append(f"   (отсутствовало {change.since_prev_change})")
        else:
            lines.append(f"❌ {fuel}: закончилось")
            if change.since_prev_change:
                lines.append(f"   (было в наличии {change.since_prev_change})")

    # delivery изменился (и не покрыт изменением avail выше)
    if change.old_delivery != change.new_delivery:
        if change.new_delivery:
            lines.append(f"🚛 {fuel}: бензовоз в пути")
        else:
            lines.append(f"🚛 {fuel}: подвоз завершён/отменён")

    return "\n".join(lines)


def main():
    config = load_config()
    client = GpnClient(cookies=config["cookies"], x_csrftoken=config["x_csrftoken"])
    store = Storage(DB_PATH)

    region_id = config["region_id"]
    now = datetime.now()

    print(f"[{now.isoformat(timespec='seconds')}] Получаю список станций...")
    try:
        all_stations = client.fetch_all_stations()
    except GpnAuthError as e:
        print(f"ОШИБКА АВТОРИЗАЦИИ: {e}", file=sys.stderr)
        try:
            telegram.send_message(
                config["telegram_bot_token"], config["telegram_chat_id"],
                f"⚠️ Бот не смог авторизоваться на gpnbonus.ru — нужно обновить cookies в config.json.\n{e}",
            )
        except Exception:
            pass
        sys.exit(2)
    except GpnApiError as e:
        print(f"ОШИБКА API: {e}", file=sys.stderr)
        sys.exit(3)

    region_stations = [s for s in all_stations if s.region_id == region_id]

    city_filter = (config.get("city") or "").strip()
    if city_filter:
        region_stations = [s for s in region_stations if s.city.strip().lower() == city_filter.lower()]
    label = f"{region_id}" + (f" / город: {city_filter}" if city_filter else "")
    print(f"Станций в регионе {label}: {len(region_stations)}")

    if not region_stations:
        print("Внимание: не найдено ни одной станции с этим region_id/city. "
              "Проверьте значения region_id и city в config.json.", file=sys.stderr)
        sys.exit(4)

    for st in region_stations:
        store.upsert_station(st)
    store.commit()

    changes_to_notify = []
    errors = 0
    error_samples = []

    fuel_filter = config.get("fuel_filter")  # список подстрок, например ["95", "100"]; None/[] = все виды

    def fuel_matches(fs) -> bool:
        if not fuel_filter:
            return True
        text = f"{fs.title} {fs.short_title}".lower()
        return any(substr.lower() in text for substr in fuel_filter)

    delay = config.get("poll_delay_between_stations_sec", 0.6)
    for station, detail, err in client.fetch_region_details(region_stations, delay_sec=delay):
        if detail is None:
            errors += 1
            if len(error_samples) < 3:
                error_samples.append(f"{station.name} ({station.city}): {err}")
            continue
        now = datetime.now()
        for fs in detail:
            if not fuel_matches(fs):
                continue
            change = store.apply_status(
                station_id=station.id,
                fuel_id=fs.fuel_id,
                fuel_title=fs.title or fs.short_title,
                avail=fs.avail,
                delivery=fs.delivery,
                api_since=fs.since,
                now=now,
            )
            if change is not None:
                changes_to_notify.append((station, change))
        store.commit()

    print(f"Изменений статуса: {len(changes_to_notify)}. Ошибок опроса станций: {errors}.")
    if error_samples:
        print("Примеры ошибок (для диагностики):")
        for s in error_samples:
            print(f"  - {s}")

    bot_token = config.get("telegram_bot_token")
    chat_id = config.get("telegram_chat_id")
    for station, change in changes_to_notify:
        msg = format_change_message(station, change)
        print("---\n" + msg)
        if bot_token and chat_id and "вставьте" not in bot_token:
            try:
                telegram.send_message(bot_token, chat_id, msg)
            except Exception as e:
                print(f"Не удалось отправить уведомление в Telegram: {e}", file=sys.stderr)

    # Обновляем HTML-дашборд после каждого опроса, чтобы файл всегда отражал
    # самые свежие данные (если открыт в браузере — подхватит через
    # автообновление страницы, см. dashboard.py). Имя файла настраивается
    # через "dashboard_filename" в config.json — по умолчанию dashboard.html,
    # но при запуске в GitHub Actions используется index.html (см. README раздел
    # "GitHub Actions"), чтобы файл сразу лежал там, где его ожидает GitHub Pages.
    dashboard_filename = config.get("dashboard_filename", "dashboard.html")
    map_filename = config.get("map_filename", "map.html")

    try:
        from dashboard import generate_dashboard, BASE_DIR as DASHBOARD_DIR
        city_filter = config.get("city")
        output_path = DASHBOARD_DIR / dashboard_filename
        generate_dashboard(store, region_id, city_filter, output_path=output_path,
                            fuel_filter=config.get("fuel_filter"), map_filename=map_filename)
    except Exception as e:
        print(f"Не удалось обновить дашборд: {e}", file=sys.stderr)

    # Карта — та же логика, отдельный файл (по умолчанию map.html, либо
    # "map_filename" из config.json). Ошибка карты не должна мешать остальному.
    try:
        from map_dashboard import generate_map_dashboard, BASE_DIR as MAP_DIR
        city_filter = config.get("city")
        map_output_path = MAP_DIR / map_filename
        generate_map_dashboard(store, region_id, city_filter, output_path=map_output_path,
                                fuel_filter=config.get("fuel_filter"),
                                yandex_maps_api_key=config.get("yandex_maps_api_key"),
                                dashboard_filename=dashboard_filename)
    except Exception as e:
        print(f"Не удалось обновить карту: {e}", file=sys.stderr)

    publish_repo_dir = config.get("publish_repo_dir")
    if publish_repo_dir:
        try:
            from publish import publish_dashboard, PublishError
            if publish_dashboard(publish_repo_dir, dashboard_filename, map_filename):
                print("Дашборд опубликован на GitHub Pages.")
        except PublishError as e:
            print(f"Не удалось опубликовать дашборд на GitHub Pages: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Неожиданная ошибка публикации: {e}", file=sys.stderr)

    store.close()
    print("Готово.")


if __name__ == "__main__":
    main()
