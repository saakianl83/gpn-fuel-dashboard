# -*- coding: utf-8 -*-
"""
Клиент к неофициальному API gpnbonus.ru (карта заправок «Нам по пути»).

Используются два эндпоинта, которые видны в браузере через DevTools -> Network:

1. POST /api/stations/list
   Отдаёт список всех станций сети со сводным полем "oils" (id вида топлива -> true/false,
   есть ли он сейчас в продаже). Полезно, чтобы получить список станций региона и их id.

2. POST /api/stations/{id}
   Отдаёт детальный статус по каждому виду топлива конкретной станции:
   {
     "data": [
       {
         "id": 12,
         "product": {"title": "Бензин АИ-95", ...},
         "price": {"price": 68.13, ...},
         "rest": {"avail": false, "since": "18:48", "delivery": "yes"}
       },
       ...
     ]
   }
   avail    -- есть топливо в продаже (true/false)
   delivery -- "yes"/"no": едет ли сейчас бензовоз с этим топливом
   since    -- время (ЧЧ:ММ), с которого действует текущий статус avail

Эндпоинт (2) — источник статуса «бензовоз в пути», ради которого всё затевалось.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests

LIST_URL = "https://gpnbonus.ru/api/stations/list"
STATION_URL_TMPL = "https://gpnbonus.ru/api/stations/{station_id}"

DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "content-type": "application/json",
    "origin": "https://gpnbonus.ru",
    "referer": "https://gpnbonus.ru/fuel/refuel-map",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
    "x-requested-with": "XMLHttpRequest",
}


class GpnAuthError(RuntimeError):
    """Куки/csrf-токен протухли — нужно обновить config.json."""


class GpnApiError(RuntimeError):
    """Любая другая ошибка запроса к API."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Station:
    id: int
    gpnazsid: int
    name: str
    city: str
    address: str
    latitude: float
    longitude: float
    region_id: int
    oils_summary: dict  # {fuel_id(str): bool} -- сводный статус из /list

    @property
    def full_address(self) -> str:
        parts = [p for p in (self.city, self.address) if p]
        return ", ".join(parts)


@dataclass
class FuelStatus:
    fuel_id: int
    title: str
    short_title: str
    avail: bool
    delivery: bool
    since: str  # "18:48" -- как отдаёт API, без даты
    price: float | None


class GpnClient:
    def __init__(self, cookies: str, x_csrftoken: str, timeout: int = 25):
        self.session = requests.Session()
        headers = dict(DEFAULT_HEADERS)
        headers["x-csrftoken"] = x_csrftoken
        headers["Cookie"] = cookies
        self.session.headers.update(headers)
        self.timeout = timeout

    def _check_auth(self, resp: requests.Response) -> None:
        if resp.status_code in (401, 403):
            raise GpnAuthError(
                f"Сервер вернул {resp.status_code} — похоже, cookies или "
                f"x-csrftoken в config.json устарели. Обновите их (см. README)."
            )
        if resp.status_code >= 500:
            raise GpnApiError(f"Сервер вернул {resp.status_code} (временная проблема на стороне gpnbonus.ru)")
        if resp.status_code != 200:
            raise GpnApiError(f"Неожиданный статус {resp.status_code}: {resp.text[:300]}")

    def fetch_all_stations(self) -> list[Station]:
        """Тянет полный список станций (все регионы) через /api/stations/list."""
        body = {
            "open": False,
            "wash": False,
            "AZSShopTypeID": False,
            "services": {"car": {}, "payment": {}, "person": {}, "station": {}},
        }
        resp = self.session.post(LIST_URL, data=json.dumps(body), timeout=self.timeout)
        self._check_auth(resp)
        try:
            payload = resp.json()
        except ValueError as e:
            raise GpnApiError(f"Ответ /list не является JSON: {e}") from e

        stations = []
        for raw in payload.get("stations", []):
            try:
                stations.append(
                    Station(
                        id=int(raw["id"]),
                        gpnazsid=int(raw.get("GPNAZSID") or 0),
                        name=(raw.get("name") or "").strip(),
                        city=(raw.get("city") or "").strip(),
                        address=(raw.get("address") or "").strip(),
                        latitude=float(raw.get("latitude") or 0),
                        longitude=float(raw.get("longitude") or 0),
                        region_id=int(raw.get("region_id") or 0),
                        oils_summary=raw.get("oils") or {},
                    )
                )
            except (KeyError, ValueError, TypeError):
                # пропускаем станции с кривыми данными, не валим весь опрос
                continue
        return stations

    def fetch_station_detail(self, station_id: int, retries: int = 4) -> list[FuelStatus]:
        """
        Тянет детальный статус (avail/delivery/since) по одной станции.

        Делает повторные попытки при:
        - сетевых сбоях (таймаут, обрыв соединения);
        - ограничении частоты запросов сервером (HTTP 429) или временных 5xx —
          в этом случае пауза перед повтором заметно больше, чем при обычном сбое сети,
          так как сайт, судя по всему, ограничивает частые запросы подряд.

        401/403 (протухшие cookies/токен) не повторяются — это фатально для всего опроса.
        """
        url = STATION_URL_TMPL.format(station_id=station_id)
        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                resp = self.session.post(url, data=b"", timeout=self.timeout)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_error = e
                if attempt < retries:
                    time.sleep(1.5 * attempt)
                    continue
                raise GpnApiError(
                    f"Не удалось подключиться к /stations/{station_id} после "
                    f"{retries} попыток: {e}"
                ) from e

            if resp.status_code in (401, 403):
                self._check_auth(resp)  # выбросит GpnAuthError

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = GpnApiError(
                    f"/stations/{station_id}: сервер вернул {resp.status_code} "
                    f"(похоже на ограничение частоты запросов)",
                    status_code=resp.status_code,
                )
                if attempt < retries:
                    time.sleep(4 * attempt)  # 4s, 8s, 12s -- заметно длиннее паузы
                    continue
                raise last_error

            if resp.status_code != 200:
                raise GpnApiError(
                    f"/stations/{station_id}: неожиданный статус {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )

            try:
                payload = resp.json()
            except ValueError as e:
                raise GpnApiError(f"Ответ /stations/{station_id} не является JSON: {e}") from e
            break
        else:
            raise GpnApiError(f"Не удалось получить /stations/{station_id}: {last_error}")

        result = []
        for item in payload.get("data", []):
            product = item.get("product") or {}
            rest = item.get("rest") or {}
            price = item.get("price") or {}
            result.append(
                FuelStatus(
                    fuel_id=int(item.get("id")),
                    title=product.get("title") or "",
                    short_title=product.get("shortTitle") or "",
                    avail=bool(rest.get("avail")),
                    delivery=(rest.get("delivery") == "yes"),
                    since=rest.get("since") or "",
                    price=price.get("price"),
                )
            )
        return result

    def fetch_region_details(
        self, stations: list[Station], delay_sec: float = 0.6
    ):
        """
        Генератор: по очереди тянет детальный статус для списка станций,
        с паузой между запросами, чтобы не долбить сервер частыми запросами.
        Отдаёт тройки (station, list[FuelStatus] | None, error_message | None).
        При ошибке по одной станции — пишет предупреждение и идёт дальше,
        чтобы одна сбойная станция не остановила весь опрос.
        """
        for st in stations:
            try:
                detail = self.fetch_station_detail(st.gpnazsid)
                yield st, detail, None
            except GpnAuthError:
                raise  # это фатально для всего опроса, пробрасываем выше
            except GpnApiError as e:
                yield st, None, str(e)
            time.sleep(delay_sec)
