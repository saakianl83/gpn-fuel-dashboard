#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Статистика по накопленной истории: когда привозили топливо и сколько оно держалось.

Использование:
    python3 stats.py                       -- сводка по всем станциям региона
    python3 stats.py --station 2481        -- подробно по одной станции (её id из БД)
    python3 stats.py --city "Екатеринбург" -- только станции этого города

Данные считаются из status_history: каждая смена avail false->true считается
"привозом", каждая смена true->false — "закончилось", разница по времени между
соседними такими событиями — это и есть "сколько продержалось" / "сколько не было".
Отдельно считается время от первого замеченного "бензовоз в пути" до момента,
когда топливо реально появилось в продаже.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from storage import Storage
from analysis import human_duration, compute_station_fuel_stats

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gpn_stations.db"
CONFIG_PATH = BASE_DIR / "config.json"


def main():
    parser = argparse.ArgumentParser(description="Статистика по заправкам gpnbonus.ru")
    parser.add_argument("--station", type=int, help="id станции (внутренний id из БД)")
    parser.add_argument("--city", type=str, help="фильтр по городу")
    parser.add_argument("--log", action="store_true",
                         help="показать подробный журнал последних поставок по каждому виду топлива")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("База данных ещё пустая — сначала запустите poll.py хотя бы пару раз "
              "(нужно минимум 2 прохода, чтобы увидеть изменения).")
        return

    store = Storage(DB_PATH)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    region_id = config["region_id"]

    stations = store.stations_in_region(region_id)
    if args.station:
        stations = [s for s in stations if s[0] == args.station]
    if args.city:
        stations = [s for s in stations if args.city.lower() in (s[2] or "").lower()]

    if not stations:
        print("Станции не найдены по заданному фильтру.")
        return

    any_output = False
    for station_id, name, city, address in stations:
        fuels = store.distinct_fuels_for_station(station_id)
        if not fuels:
            continue
        station_header_printed = False
        for fuel_id, fuel_title in fuels:
            rows = store.fuel_history_for_station(station_id, fuel_id)
            stats = compute_station_fuel_stats(rows)
            if not stats or stats["history_points"] < 2:
                continue  # пока недостаточно данных для статистики по этому топливу

            if not station_header_printed:
                print(f"\n=== {name} — {city}, {address} (id={station_id}) ===")
                station_header_printed = True
                any_output = True

            status_txt = "✅ есть" if stats["current_avail"] else "❌ нет"
            delivery_txt = " (🚛 бензовоз в пути)" if stats["current_delivery"] else ""
            print(f"  {fuel_title}: сейчас {status_txt}{delivery_txt}")
            print(f"    привозов зафиксировано: {stats['restocks']}")
            if stats["avg_availability_duration"] is not None:
                print(f"    в среднем держится: {human_duration(stats['avg_availability_duration'])}")
            if stats["avg_outage_before_restock"] is not None:
                print(f"    в среднем отсутствует перед привозом: {human_duration(stats['avg_outage_before_restock'])}")
            if stats["avg_lead_time_sec"] is not None:
                n = len(stats["lead_time_samples_sec"])
                print(f"    от объявления \"бензовоз в пути\" до появления топлива: "
                      f"в среднем {human_duration(stats['avg_lead_time_sec'])} "
                      f"(по {n} {'случаю' if n == 1 else 'случаям'})")

            if args.log and stats["delivery_events"]:
                print(f"    журнал поставок (последние {min(5, len(stats['delivery_events']))}):")
                for e in stats["delivery_events"][-5:]:
                    ran_out = e["ran_out_at"].strftime("%d.%m %H:%M")
                    restocked = e["restocked_at"].strftime("%d.%m %H:%M")
                    outage = human_duration(e["outage_duration_sec"])
                    if e["lead_time_sec"] is not None:
                        announced = e["delivery_announced_at"].strftime("%H:%M")
                        lead = human_duration(e["lead_time_sec"])
                        print(f"      {ran_out} закончилось → {announced} бензовоз в пути → "
                              f"{restocked} привезли (простой {outage}, от объявления {lead})")
                    else:
                        print(f"      {ran_out} закончилось → {restocked} привезли "
                              f"(простой {outage}, объявления о подвозе не заметили)")

    if not any_output:
        print("Пока недостаточно данных для статистики — накопите историю (нужно минимум "
              "2-3 прохода poll.py с изменениями статуса, а лучше несколько дней наблюдений).")

    store.close()


if __name__ == "__main__":
    main()
