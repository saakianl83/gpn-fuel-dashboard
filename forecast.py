#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Прогноз в обе стороны, на основе накопленной истории:
- когда ожидается ПОЯВЛЕНИЕ топлива там, где его сейчас нет;
- когда топливо, скорее всего, ЗАКОНЧИТСЯ там, где оно сейчас есть.

Использование:
    python3 forecast.py                        -- прогноз по всем станциям региона
    python3 forecast.py --city "Екатеринбург"  -- только один город
    python3 forecast.py --fuel "АИ-95"         -- только один вид топлива (по подстроке)
    python3 forecast.py --direction restock    -- только "когда появится"
    python3 forecast.py --direction depletion  -- только "когда закончится"

Как считается прогноз (важно понимать ограничения):
- Появление: берутся станции, где топлива СЕЙЧАС нет. Прогноз = момент, когда
  оно кончилось в этот раз, + медианная длительность прошлых периодов отсутствия.
- Окончание: берутся станции, где топливо СЕЙЧАС есть. Прогноз = момент
  последнего привоза + медианная длительность прошлых периодов наличия.
- Медиана взята вместо среднего, чтобы редкий аномально долгий случай (авария,
  ремонт) не сдвигал оценку для всех остальных.
- Показывается разброс (по минимальному/максимальному наблюдению) и количество
  наблюдений — чем их больше, тем прогноз надёжнее.
- Это СТАТИСТИЧЕСКАЯ ОЦЕНКА на основе прошлых паттернов, а не официальные данные
  о поставках. Точность растёт по мере накопления истории (нужны недели наблюдений
  для действительно устойчивых цифр).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from storage import Storage
from analysis import human_duration, compute_station_fuel_stats

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gpn_stations.db"
CONFIG_PATH = BASE_DIR / "config.json"

MIN_OBSERVATIONS = 2  # меньше — считаем прогноз слишком ненадёжным, не показываем


def format_forecast_time(dt: datetime, now: datetime) -> str:
    """Компактно показывает прогнозное время: сегодня/завтра HH:MM, либо дата."""
    delta_days = (dt.date() - now.date()).days
    time_part = dt.strftime("%H:%M")
    if delta_days == 0:
        return f"сегодня в {time_part}"
    if delta_days == 1:
        return f"завтра в {time_part}"
    if delta_days == -1:
        return f"вчера в {time_part} (прогноз уже в прошлом — вероятно, задержка)"
    if 2 <= delta_days <= 6:
        weekday = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"][dt.weekday()]
        return f"в {weekday} ({dt.strftime('%d.%m')}) около {time_part}"
    return dt.strftime("%d.%m в %H:%M")


def main():
    parser = argparse.ArgumentParser(description="Прогноз наличия топлива на заправках gpnbonus.ru")
    parser.add_argument("--city", type=str, help="фильтр по городу")
    parser.add_argument("--fuel", type=str, help="фильтр по виду топлива (подстрока, например 'АИ-95')")
    parser.add_argument("--direction", choices=["restock", "depletion", "both"], default="both",
                         help="restock — только 'когда появится', depletion — только "
                              "'когда закончится', both — оба (по умолчанию)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("База данных ещё пустая — сначала дайте боту поработать хотя бы несколько дней "
              "(poll.py по расписанию), иначе прогнозировать не на чем.")
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    region_id = config["region_id"]
    config_city = (config.get("city") or "").strip() or None

    store = Storage(DB_PATH)
    city_filter = args.city or config_city
    rows = store.current_status_for_region(region_id, city=city_filter)

    if not rows:
        print("Нет данных по текущим статусам — запустите poll.py хотя бы один раз.")
        store.close()
        return

    now = datetime.now()
    restock_predictions = []
    depletion_predictions = []
    no_data_restock = 0
    no_data_depletion = 0

    for station_id, name, city, address, latitude, longitude, fuel_id, fuel_title, avail, delivery, updated_at in rows:
        if args.fuel and args.fuel.lower() not in (fuel_title or "").lower():
            continue

        history_rows = store.fuel_history_for_station(station_id, fuel_id)
        stats = compute_station_fuel_stats(history_rows)

        if not avail:
            # --- прогноз ПОЯВЛЕНИЯ ---
            if args.direction not in ("restock", "both"):
                continue
            if not stats or len(stats["outage_durations_sec"]) < MIN_OBSERVATIONS:
                no_data_restock += 1
                continue
            outages = stats["outage_durations_sec"]
            median_sec = stats["median_outage_before_restock"]
            since_out = datetime.fromisoformat(updated_at)
            predicted_time = since_out + timedelta(seconds=median_sec)
            restock_predictions.append({
                "name": name, "city": city, "address": address,
                "fuel_title": fuel_title, "delivery": bool(delivery),
                "predicted_time": predicted_time,
                "already_waiting_sec": (now - since_out).total_seconds(),
                "median_sec": median_sec, "min_sec": min(outages), "max_sec": max(outages),
                "observations": len(outages),
                "is_overdue": predicted_time < now,
            })
        else:
            # --- прогноз ОКОНЧАНИЯ ---
            if args.direction not in ("depletion", "both"):
                continue
            if not stats or len(stats["availability_durations_sec"]) < MIN_OBSERVATIONS:
                no_data_depletion += 1
                continue
            if not stats["last_restock_time"]:
                no_data_depletion += 1
                continue
            avail_durations = stats["availability_durations_sec"]
            median_sec = stats["median_availability_duration"]
            since_restock = stats["last_restock_time"]
            predicted_time = since_restock + timedelta(seconds=median_sec)
            depletion_predictions.append({
                "name": name, "city": city, "address": address,
                "fuel_title": fuel_title,
                "predicted_time": predicted_time,
                "already_available_sec": (now - since_restock).total_seconds(),
                "median_sec": median_sec, "min_sec": min(avail_durations), "max_sec": max(avail_durations),
                "observations": len(avail_durations),
                "is_overdue": predicted_time < now,  # держится уже дольше обычного
            })

    store.close()

    if not restock_predictions and not depletion_predictions:
        msg = "Нет станций, по которым можно построить прогноз"
        extra = []
        if no_data_restock:
            extra.append(f"{no_data_restock} с отсутствующим топливом — недостаточно истории отсутствий")
        if no_data_depletion:
            extra.append(f"{no_data_depletion} с топливом в наличии — недостаточно истории наличия")
        if extra:
            msg += " (" + "; ".join(extra) + ")"
        print(msg + ".")
        return

    if restock_predictions:
        restock_predictions.sort(key=lambda p: p["predicted_time"])
        print(f"📥 Прогноз ПОЯВЛЕНИЯ топлива ({len(restock_predictions)} позиций):\n")
        for p in restock_predictions:
            addr = f"{p['city']}, {p['address']}" if p['address'] else p['city']
            overdue_note = " ⚠️ прогноз уже прошёл — вероятно, задержка подвоза" if p["is_overdue"] else ""
            delivery_note = " (🚛 бензовоз уже в пути)" if p["delivery"] else ""
            print(f"⛽ {p['name']} — {addr}")
            print(f"   {p['fuel_title']}{delivery_note}")
            print(f"   Ожидается: {format_forecast_time(p['predicted_time'], now)}{overdue_note}")
            print(f"   (на основе {p['observations']} прошлых случаев; "
                  f"разброс от {human_duration(p['min_sec'])} до {human_duration(p['max_sec'])} простоя; "
                  f"уже ждёт {human_duration(p['already_waiting_sec'])})")
            print()

    if depletion_predictions:
        depletion_predictions.sort(key=lambda p: p["predicted_time"])
        print(f"📤 Прогноз ОКОНЧАНИЯ топлива ({len(depletion_predictions)} позиций):\n")
        for p in depletion_predictions:
            addr = f"{p['city']}, {p['address']}" if p['address'] else p['city']
            overdue_note = " ⏳ держится уже дольше обычного" if p["is_overdue"] else ""
            print(f"⛽ {p['name']} — {addr}")
            print(f"   {p['fuel_title']}")
            print(f"   Ожидается закончится: {format_forecast_time(p['predicted_time'], now)}{overdue_note}")
            print(f"   (на основе {p['observations']} прошлых случаев; "
                  f"разброс от {human_duration(p['min_sec'])} до {human_duration(p['max_sec'])} наличия; "
                  f"уже в наличии {human_duration(p['already_available_sec'])})")
            print()


if __name__ == "__main__":
    main()
