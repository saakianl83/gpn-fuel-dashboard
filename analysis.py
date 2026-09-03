# -*- coding: utf-8 -*-
"""
Общая логика разбора истории статусов — используется и stats.py, и forecast.py.

compute_station_fuel_stats() превращает сырую историю (avail/delivery по времени)
в списки длительностей: сколько раз топливо кончалось и появлялось снова, и сколько
конкретно длился каждый такой период. Дальше stats.py показывает средние значения,
а forecast.py использует эти же списки для прогноза (медиана устойчивее среднего
к редким аномально долгим простоям).
"""
from __future__ import annotations

from datetime import datetime
from statistics import mean, median


def human_duration(delta_seconds: float) -> str:
    total_min = int(delta_seconds // 60)
    if total_min < 1:
        return "меньше минуты"
    days, rem_min = divmod(total_min, 24 * 60)
    hours, minutes = divmod(rem_min, 60)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes and not days:
        parts.append(f"{minutes}м")
    return " ".join(parts) if parts else "меньше минуты"


def extract_delivery_events(rows):
    """
    Строит список завершённых "циклов поставки" по истории одного (станция,
    вид топлива): каждый раз, когда топливо кончалось и затем снова появлялось —
    одна запись с полным набором меток времени.

    rows: список (avail, delivery, api_since, recorded_at), отсортированный по времени.

    Возвращает list of dict:
      ran_out_at            -- datetime, когда топливо закончилось
      restocked_at          -- datetime, когда снова появилось в продаже
      outage_duration_sec   -- сколько длился простой целиком (restocked - ran_out)
      delivery_announced_at -- datetime первого замеченного "бензовоз в пути" в
                                течение ЭТОГО простоя, либо None, если объявления
                                о подвозе не было замечено (например, бот не успел
                                опросить сайт в нужный момент, или сеть сразу
                                показала уже готовое топливо)
      lead_time_sec         -- restocked_at - delivery_announced_at, только если
                                delivery_announced_at известен, иначе None

    Первый цикл, если топливо УЖЕ отсутствовало в самом начале наблюдений
    (ran_out_at неизвестен точно), не попадает в список — нет смысла считать
    время простоя, если неизвестно, когда он начался.
    """
    if not rows:
        return []

    events = []
    for avail, delivery, api_since, recorded_at in rows:
        events.append((datetime.fromisoformat(recorded_at), bool(avail), bool(delivery)))

    results = []
    prev_time, prev_avail, prev_delivery = events[0]

    tracking = False           # отслеживаем простой, начавшийся ПОСЛЕ начала наблюдений
    ran_out_at = None
    delivery_announced_at = None
    if not prev_avail and prev_delivery:
        # на самой первой точке уже был замечен подвоз — запомним, но точный
        # момент начала простоя всё равно неизвестен (tracking остаётся False)
        delivery_announced_at = prev_time

    for i in range(1, len(events)):
        cur_time, cur_avail, cur_delivery = events[i]

        if prev_avail and not cur_avail:
            # топливо только что закончилось — начинаем отслеживать новый простой
            ran_out_at = cur_time
            delivery_announced_at = None
            tracking = True

        if cur_delivery and not prev_delivery and not cur_avail and delivery_announced_at is None:
            # первое замеченное "бензовоз в пути" в течение текущего простоя
            delivery_announced_at = cur_time

        if not prev_avail and cur_avail:
            # топливо снова появилось
            if tracking and ran_out_at is not None:
                outage_sec = (cur_time - ran_out_at).total_seconds()
                lead_sec = (
                    (cur_time - delivery_announced_at).total_seconds()
                    if delivery_announced_at else None
                )
                results.append({
                    "ran_out_at": ran_out_at,
                    "restocked_at": cur_time,
                    "outage_duration_sec": outage_sec,
                    "delivery_announced_at": delivery_announced_at,
                    "lead_time_sec": lead_sec,
                })
            tracking = False
            ran_out_at = None
            delivery_announced_at = None

        prev_time, prev_avail, prev_delivery = cur_time, cur_avail, cur_delivery

    return results


def compute_station_fuel_stats(rows):
    """
    rows: список (avail, delivery, api_since, recorded_at) по одной станции+топливу,
    отсортированный по времени (так и приходит из storage).

    Возвращает dict:
      restocks                    -- сколько раз топливо появлялось после отсутствия
      outage_durations_sec        -- список длительностей ВСЕХ отсутствий (сек), сырые данные
      availability_durations_sec  -- список длительностей ВСЕХ периодов наличия (сек)
      avg_outage_before_restock   -- среднее по outage_durations_sec (или None)
      median_outage_before_restock-- медиана по outage_durations_sec (устойчивее к выбросам)
      avg_availability_duration   -- среднее по availability_durations_sec (или None)
      median_availability_duration-- медиана по availability_durations_sec
      current_avail, current_delivery, current_since -- последнее известное состояние
      last_restock_time            -- datetime последнего момента, когда топливо
                                       ПОЯВИЛОСЬ после отсутствия (или момент первой
                                       записи, если оно было в наличии с самого начала
                                       наблюдений); None, если реального привоза
                                       (переход false→true) в истории ещё не было
      delivery_events               -- полный список циклов поставки, см.
                                        extract_delivery_events()
      lead_time_samples_sec         -- список lead_time_sec из delivery_events,
                                        где он известен (подвоз был замечен заранее)
      avg_lead_time_sec / median_lead_time_sec -- среднее/медиана по ним, либо None
      history_points               -- сколько точек истории всего
    """
    if not rows:
        return None

    events = []
    for avail, delivery, api_since, recorded_at in rows:
        events.append((datetime.fromisoformat(recorded_at), bool(avail), bool(delivery)))

    outage_durations_sec = []       # сколько времени НЕ было топлива перед тем как привезли
    availability_durations_sec = []  # сколько времени топливо ДЕРЖАЛОСЬ перед тем как закончилось
    restocks = 0
    last_restock_time = events[0][0] if events[0][1] else None  # если уже было в наличии с начала наблюдений

    # Между двумя событиями смены avail может быть промежуточное событие (например,
    # только "бензовоз выехал", без смены avail). Поэтому меряем длительность не между
    # соседними строками истории, а между моментами, когда avail реально поменялся.
    last_avail_change_time, last_avail_value = events[0][0], events[0][1]

    for i in range(1, len(events)):
        cur_time, cur_avail, _ = events[i]
        if cur_avail == last_avail_value:
            continue
        delta = (cur_time - last_avail_change_time).total_seconds()
        if not last_avail_value and cur_avail:
            restocks += 1
            outage_durations_sec.append(delta)
            last_restock_time = cur_time
        elif last_avail_value and not cur_avail:
            availability_durations_sec.append(delta)
        last_avail_change_time, last_avail_value = cur_time, cur_avail

    last_time, last_avail, last_delivery = events[-1]

    delivery_events = extract_delivery_events(rows)
    lead_time_samples_sec = [
        e["lead_time_sec"] for e in delivery_events if e["lead_time_sec"] is not None
    ]

    return {
        "restocks": restocks,
        "outage_durations_sec": outage_durations_sec,
        "availability_durations_sec": availability_durations_sec,
        "avg_outage_before_restock": mean(outage_durations_sec) if outage_durations_sec else None,
        "median_outage_before_restock": median(outage_durations_sec) if outage_durations_sec else None,
        "avg_availability_duration": mean(availability_durations_sec) if availability_durations_sec else None,
        "median_availability_duration": median(availability_durations_sec) if availability_durations_sec else None,
        "current_avail": last_avail,
        "current_delivery": last_delivery,
        "current_since": last_time,
        "last_restock_time": last_restock_time,
        "delivery_events": delivery_events,
        "lead_time_samples_sec": lead_time_samples_sec,
        "avg_lead_time_sec": mean(lead_time_samples_sec) if lead_time_samples_sec else None,
        "median_lead_time_sec": median(lead_time_samples_sec) if lead_time_samples_sec else None,
        "history_points": len(events),
    }
