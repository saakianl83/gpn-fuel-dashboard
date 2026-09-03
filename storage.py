# -*- coding: utf-8 -*-
"""
Хранилище на SQLite.

Таблицы:
  stations       -- справочник станций (обновляется при каждом опросе)
  current_status -- текущий известный статус по каждой (станция, вид топлива)
  status_history -- журнал ИЗМЕНЕНИЙ статуса (пишем строку только когда что-то поменялось)

Вся статистика («когда привезли», «сколько продержалось») считается из status_history.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY,
    gpnazsid INTEGER,
    name TEXT,
    city TEXT,
    address TEXT,
    latitude REAL,
    longitude REAL,
    region_id INTEGER
);

CREATE TABLE IF NOT EXISTS current_status (
    station_id INTEGER NOT NULL,
    fuel_id INTEGER NOT NULL,
    fuel_title TEXT,
    avail INTEGER NOT NULL,
    delivery INTEGER NOT NULL,
    api_since TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station_id, fuel_id)
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL,
    fuel_id INTEGER NOT NULL,
    fuel_title TEXT,
    avail INTEGER NOT NULL,
    delivery INTEGER NOT NULL,
    api_since TEXT,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_station_fuel_time
    ON status_history (station_id, fuel_id, recorded_at);
"""


@dataclass
class Change:
    station_id: int
    fuel_id: int
    fuel_title: str
    old_avail: bool | None
    new_avail: bool
    old_delivery: bool | None
    new_delivery: bool
    since_prev_change: str | None  # сколько длился предыдущий статус, текстом


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- станции ----------

    def upsert_station(self, station) -> None:
        self.conn.execute(
            """
            INSERT INTO stations (id, gpnazsid, name, city, address, latitude, longitude, region_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                gpnazsid=excluded.gpnazsid, name=excluded.name, city=excluded.city,
                address=excluded.address, latitude=excluded.latitude,
                longitude=excluded.longitude, region_id=excluded.region_id
            """,
            (
                station.id, station.gpnazsid, station.name, station.city,
                station.address, station.latitude, station.longitude, station.region_id,
            ),
        )

    def get_station(self, station_id: int):
        cur = self.conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,))
        return cur.fetchone()

    # ---------- статусы ----------

    def get_current(self, station_id: int, fuel_id: int):
        cur = self.conn.execute(
            "SELECT avail, delivery, api_since, updated_at FROM current_status "
            "WHERE station_id=? AND fuel_id=?",
            (station_id, fuel_id),
        )
        return cur.fetchone()

    def apply_status(
        self, station_id: int, fuel_id: int, fuel_title: str,
        avail: bool, delivery: bool, api_since: str, now: datetime,
    ) -> Change | None:
        """
        Сравнивает новый статус с сохранённым. Если что-то изменилось (avail или delivery) —
        пишет строку в историю, обновляет current_status и возвращает объект Change.
        Если ничего не изменилось — возвращает None.
        """
        now_iso = now.isoformat(timespec="seconds")
        prev = self.get_current(station_id, fuel_id)

        if prev is None:
            # первая запись по этой станции/топливу — просто фиксируем базовую линию,
            # без уведомления (не с чем сравнивать)
            self.conn.execute(
                "INSERT INTO current_status (station_id, fuel_id, fuel_title, avail, delivery, api_since, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (station_id, fuel_id, fuel_title, int(avail), int(delivery), api_since, now_iso),
            )
            self._append_history(station_id, fuel_id, fuel_title, avail, delivery, api_since, now_iso)
            return None

        prev_avail, prev_delivery, prev_since, prev_updated = prev
        prev_avail = bool(prev_avail)
        prev_delivery = bool(prev_delivery)

        if prev_avail == avail and prev_delivery == delivery:
            # статус не поменялся, просто освежим updated_at, без записи в историю
            self.conn.execute(
                "UPDATE current_status SET updated_at=?, api_since=? WHERE station_id=? AND fuel_id=?",
                (now_iso, api_since, station_id, fuel_id),
            )
            return None

        # статус изменился
        duration_text = self._human_duration(prev_updated, now_iso)

        self.conn.execute(
            "UPDATE current_status SET avail=?, delivery=?, api_since=?, updated_at=? "
            "WHERE station_id=? AND fuel_id=?",
            (int(avail), int(delivery), api_since, now_iso, station_id, fuel_id),
        )
        self._append_history(station_id, fuel_id, fuel_title, avail, delivery, api_since, now_iso)

        return Change(
            station_id=station_id,
            fuel_id=fuel_id,
            fuel_title=fuel_title,
            old_avail=prev_avail,
            new_avail=avail,
            old_delivery=prev_delivery,
            new_delivery=delivery,
            since_prev_change=duration_text,
        )

    def _append_history(self, station_id, fuel_id, fuel_title, avail, delivery, api_since, now_iso):
        self.conn.execute(
            "INSERT INTO status_history (station_id, fuel_id, fuel_title, avail, delivery, api_since, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (station_id, fuel_id, fuel_title, int(avail), int(delivery), api_since, now_iso),
        )

    @staticmethod
    def _human_duration(iso_from: str, iso_to: str) -> str:
        try:
            t0 = datetime.fromisoformat(iso_from)
            t1 = datetime.fromisoformat(iso_to)
        except ValueError:
            return ""
        delta = t1 - t0
        total_min = int(delta.total_seconds() // 60)
        if total_min < 1:
            return "меньше минуты"
        days, rem_min = divmod(total_min, 24 * 60)
        hours, minutes = divmod(rem_min, 60)
        parts = []
        if days:
            parts.append(f"{days} дн")
        if hours:
            parts.append(f"{hours} ч")
        if minutes and not days:
            parts.append(f"{minutes} мин")
        return " ".join(parts) if parts else "меньше минуты"

    def commit(self):
        self.conn.commit()

    # ---------- статистика ----------

    def stations_in_region(self, region_id: int):
        cur = self.conn.execute(
            "SELECT id, name, city, address FROM stations WHERE region_id = ? ORDER BY city, name",
            (region_id,),
        )
        return cur.fetchall()

    def fuel_history_for_station(self, station_id: int, fuel_id: int):
        cur = self.conn.execute(
            "SELECT avail, delivery, api_since, recorded_at FROM status_history "
            "WHERE station_id=? AND fuel_id=? ORDER BY recorded_at",
            (station_id, fuel_id),
        )
        return cur.fetchall()

    def current_status_for_region(self, region_id: int, city: str | None = None):
        """
        Текущий статус по всем (станция, вид топлива) в регионе — для прогноза
        и для дашбордов (таблица и карта).
        Возвращает строки: station_id, name, city, address, latitude, longitude,
        fuel_id, fuel_title, avail, delivery, updated_at.
        """
        query = (
            "SELECT s.id, s.name, s.city, s.address, s.latitude, s.longitude, "
            "cs.fuel_id, cs.fuel_title, cs.avail, cs.delivery, cs.updated_at "
            "FROM current_status cs "
            "JOIN stations s ON s.id = cs.station_id "
            "WHERE s.region_id = ?"
        )
        params = [region_id]
        if city:
            query += " AND s.city = ?"
            params.append(city)
        query += " ORDER BY s.city, s.name"
        cur = self.conn.execute(query, params)
        return cur.fetchall()

    def distinct_fuels_for_station(self, station_id: int):
        cur = self.conn.execute(
            "SELECT DISTINCT fuel_id, fuel_title FROM status_history WHERE station_id=?",
            (station_id,),
        )
        return cur.fetchall()
