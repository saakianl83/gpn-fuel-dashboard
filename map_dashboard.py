#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Интерактивная карта станций на Яндекс.Картах (альтернатива табличному
дашборду из dashboard.py).

Использование:
    python3 map_dashboard.py                        -- сгенерировать и открыть в браузере
    python3 map_dashboard.py --city "Екатеринбург"   -- только один город
    python3 map_dashboard.py --no-open

Требует API-ключ Яндекс.Карт (бесплатный, см. README раздел «Яндекс.Карты»).
Ключ берётся из "yandex_maps_api_key" в config.json.

На странице есть фильтры: чекбоксы видов топлива (выбираете, какие считать) и
статуса (показывать ли "есть"/"в пути"/"нет"). Цвет метки — зелёный, если
ХОТЯ БЫ ОДИН из выбранных видов топлива есть в продаже; синий — ни одного нет,
но хотя бы один едет; красный — нет ни одного и ничего не едет. Все данные по
всем видам топлива уже встроены в страницу, фильтрация происходит мгновенно в
браузере, без перезагрузки и без повторного запуска скрипта.

Файл может обновляться сам вместе с dashboard.py, если вызвать
generate_map_dashboard() из poll.py (см. README).
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from datetime import datetime
from pathlib import Path

from storage import Storage

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gpn_stations.db"
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_PATH = BASE_DIR / "map.html"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _time_ago(iso_str: str, now: datetime) -> str:
    try:
        t = datetime.fromisoformat(iso_str)
    except ValueError:
        return ""
    delta_min = int((now - t).total_seconds() // 60)
    if delta_min < 1:
        return "только что"
    if delta_min < 60:
        return f"{delta_min} мин назад"
    hours = delta_min // 60
    return f"{hours} ч назад"


def generate_map_dashboard(
    store: Storage, region_id: int, city: str | None,
    output_path: Path = OUTPUT_PATH, fuel_filter: list[str] | None = None,
    default_fuel_substrings: list[str] | None = None,
    yandex_maps_api_key: str | None = None,
    dashboard_filename: str = "dashboard.html",
) -> Path:
    """
    Строит HTML-карту (Яндекс.Карты) из текущих статусов в базе и сохраняет в
    output_path. Станции без координат пропускаются.

    dashboard_filename: имя файла табличного дашборда — используется только
    для ссылки "← Таблица" в шапке страницы (сам файл этой функцией не
    трогается).
    fuel_filter: ограничивает, какие виды топлива вообще попадают в данные
    (обычно из config.json).
    default_fuel_substrings: какие виды топлива отмечены галочкой по
    умолчанию (по умолчанию — содержащие "95" или "100" в названии).
    yandex_maps_api_key: обязателен — без него карта не отобразится
    (Яндекс.Карты требуют ключ). Получить: см. README, раздел «Яндекс.Карты».
    """
    rows = store.current_status_for_region(region_id, city=city)
    now = datetime.now()

    if fuel_filter:
        rows = [
            r for r in rows
            if any(substr.lower() in (r[7] or "").lower() for substr in fuel_filter)
        ]

    if default_fuel_substrings is None:
        default_fuel_substrings = ["95", "100"]

    stations: dict[int, dict] = {}
    all_fuel_titles: set[str] = set()
    for station_id, name, st_city, address, latitude, longitude, fuel_id, fuel_title, avail, delivery, updated_at in rows:
        if not latitude or not longitude:
            continue
        if station_id not in stations:
            stations[station_id] = {
                "name": name, "city": st_city, "address": address,
                "lat": latitude, "lon": longitude,
                "fuels": [], "latest_update": updated_at,
            }
        stations[station_id]["fuels"].append({
            "title": fuel_title, "avail": bool(avail), "delivery": bool(delivery),
        })
        all_fuel_titles.add(fuel_title)
        if updated_at > stations[station_id]["latest_update"]:
            stations[station_id]["latest_update"] = updated_at

    fuel_types_sorted = sorted(all_fuel_titles)
    default_checked = {
        t for t in fuel_types_sorted
        if any(sub.lower() in t.lower() for sub in default_fuel_substrings)
    } or set(fuel_types_sorted)

    markers_data = []
    for s in stations.values():
        fuels_html_parts = []
        for f in sorted(s["fuels"], key=lambda x: x["title"]):
            if f["avail"]:
                cls, icon, label = "avail", "✅", "есть"
            elif f["delivery"]:
                cls, icon, label = "delivery", "🚛", "в пути"
            else:
                cls, icon, label = "out", "❌", "нет"
            fuels_html_parts.append(
                f'<div class="fuel-row {cls}"><span class="icon">{icon}</span>'
                f'{_esc(f["title"])}: {label}</div>'
            )
        balloon_html = (
            f'<div class="popup"><b>{_esc(s["name"])}</b><br>'
            f'{_esc(s["city"])}, {_esc(s["address"])}<br>'
            f'<div class="fuels">{"".join(fuels_html_parts)}</div>'
            f'<div class="updated">Обновлено: {_time_ago(s["latest_update"], now)}</div></div>'
        )
        markers_data.append({
            "lat": s["lat"], "lon": s["lon"],
            "balloon": balloon_html,
            "name": s["name"], "address": s["address"],
            "fuels": s["fuels"],
        })

    total = len(markers_data)

    if markers_data:
        center_lat = sum(m["lat"] for m in markers_data) / len(markers_data)
        center_lon = sum(m["lon"] for m in markers_data) / len(markers_data)
    else:
        center_lat, center_lon = 56.8389, 60.6057

    markers_json = json.dumps(markers_data, ensure_ascii=False)
    fuel_types_json = json.dumps(fuel_types_sorted, ensure_ascii=False)
    default_checked_json = json.dumps(sorted(default_checked), ensure_ascii=False)
    city_label = f" — {_esc(city)}" if city else ""
    generated_at = now.strftime("%d.%m.%Y %H:%M:%S")
    api_key = yandex_maps_api_key or ""

    if not api_key:
        # Без ключа карта не заработает — отдаём понятную заглушку вместо
        # тихо-сломанной страницы.
        html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Карта заправок{city_label}</title></head>
<body style="font-family: -apple-system, sans-serif; padding: 24px; max-width: 600px; margin: 0 auto;">
<h2>⚠️ Не настроен API-ключ Яндекс.Карт</h2>
<p>Добавьте <code>"yandex_maps_api_key"</code> в config.json — получить ключ можно
бесплатно на <a href="https://developer.tech.yandex.ru/">developer.tech.yandex.ru</a>
(раздел «Карты» → «JavaScript API и HTTP Геокодер»). Подробности — в README,
раздел «Яндекс.Карты».</p>
<p><a href="{_esc(dashboard_filename)}">← Вернуться к таблице</a></p>
</body></html>"""
        output_path.write_text(html, encoding="utf-8")
        return output_path

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Карта заправок{city_label}</title>
<script src="https://api-maps.yandex.ru/2.1/?apikey={api_key}&lang=ru_RU" type="text/javascript"></script>
<style>
  :root {{
    --bg: #f7f8fa; --card: #ffffff; --border: #e3e6ea; --text: #1c2530; --muted: #6b7480;
  }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html, body {{ margin: 0; padding: 0; height: 100%; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ display: flex; flex-direction: column; }}
  #header {{
    flex: 0 0 auto;
    padding: 12px 20px; background: var(--card); border-bottom: 1px solid var(--border);
  }}
  #header h1 {{ font-size: 18px; margin: 0 0 2px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .nav-link {{
    font-size: 12px; font-weight: 500; color: #1a6fc4; text-decoration: none;
    border: 1px solid #1a6fc4; border-radius: 6px; padding: 3px 10px; white-space: nowrap;
  }}
  .nav-link:hover {{ background: #e8f0fe; }}
  #header .subtitle {{ color: var(--muted); font-size: 12px; }}
  .summary {{ display: flex; gap: 12px; margin-top: 6px; align-items: center; flex-wrap: wrap; }}
  .stat {{ font-size: 12px; color: var(--muted); }}
  .stat b {{ color: var(--text); font-size: 14px; }}
  #filters-toggle {{
    display: none; margin-left: auto; font-size: 12px; font-weight: 500; color: var(--text);
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 5px 10px; cursor: pointer;
  }}
  #search {{
    margin-top: 8px; width: 100%; max-width: 320px; padding: 8px 10px;
    border: 1px solid var(--border); border-radius: 8px; font-size: 16px;
  }}
  #filters {{
    display: flex; flex-wrap: wrap; gap: 18px; margin-top: 10px; padding-top: 10px;
    border-top: 1px solid var(--border); font-size: 12px;
  }}
  .filter-group b {{ display: block; margin-bottom: 4px; color: var(--text); font-size: 12px; }}
  .filter-group label {{ display: inline-flex; align-items: center; gap: 4px; margin-right: 12px; cursor: pointer; white-space: nowrap; padding: 2px 0; }}
  .filter-group input {{ cursor: pointer; width: 16px; height: 16px; }}
  #map {{ flex: 1 1 auto; position: relative; min-height: 0; }}
  .popup {{ font-size: 13px; min-width: 200px; }}
  .popup .fuels {{ margin-top: 6px; }}
  .fuel-row {{ padding: 2px 0; }}
  .fuel-row.avail {{ color: #1e8e3e; }}
  .fuel-row.out {{ color: #c53929; }}
  .fuel-row.delivery {{ color: #1a6fc4; }}
  .popup .updated {{ margin-top: 6px; color: var(--muted); font-size: 11px; }}
  .legend {{
    position: absolute; bottom: 20px; right: 20px; z-index: 1000;
    background: var(--card); padding: 8px 10px; border-radius: 8px; font-size: 12px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.15); line-height: 1.6;
  }}
  .legend .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}

  @media (max-width: 640px) {{
    #header {{ padding: 10px 14px; }}
    #header h1 {{ font-size: 15px; gap: 6px; }}
    .nav-link {{ font-size: 11px; padding: 2px 8px; }}
    #filters-toggle {{ display: inline-block; }}
    #filters {{ display: none; gap: 12px; }}
    #filters.open {{ display: flex; }}
    .filter-group {{ width: 100%; }}
    .legend {{ bottom: 10px; right: 10px; font-size: 10px; padding: 6px 8px; }}
    .legend .dot {{ width: 8px; height: 8px; }}
  }}
</style>
</head>
<body>
  <div id="header">
    <h1>🗺️ Карта заправок{city_label} <a class="nav-link" href="{_esc(dashboard_filename)}">📋 Таблица</a></h1>
    <div class="subtitle">Данные от: {generated_at} · чекбоксы ниже фильтруют карту мгновенно, без перезагрузки</div>
    <div class="summary">
      <div class="stat"><b>{total}</b> станций всего</div>
      <div class="stat" id="visible-count"></div>
      <button id="filters-toggle" type="button">⚙️ Фильтры</button>
    </div>
    <input type="text" id="search" placeholder="Поиск по названию или адресу...">
    <div id="filters">
      <div class="filter-group">
        <b>Вид топлива (зелёным — если есть хотя бы один из отмеченных):</b>
        <span id="fuel-checkboxes"></span>
      </div>
      <div class="filter-group">
        <b>Показывать станции со статусом:</b>
        <label><input type="checkbox" id="show-avail" checked> ✅ Есть</label>
        <label><input type="checkbox" id="show-delivery" checked> 🚛 В пути</label>
        <label><input type="checkbox" id="show-out" checked> ❌ Нет</label>
      </div>
    </div>
  </div>
  <div id="map"></div>
  <div class="legend">
    <span class="dot" style="background:#1e8e3e"></span>Есть хотя бы один из выбранных<br>
    <span class="dot" style="background:#1a6fc4"></span>Нет, но едет<br>
    <span class="dot" style="background:#c53929"></span>Нет ни одного
  </div>

<script>
  const filtersToggle = document.getElementById('filters-toggle');
  const filtersPanel = document.getElementById('filters');
  filtersToggle.addEventListener('click', function() {{
    filtersPanel.classList.toggle('open');
  }});

  const stations = {markers_json};
  const allFuelTypes = {fuel_types_json};
  const defaultChecked = new Set({default_checked_json});

  // Строим чекбоксы видов топлива
  const fuelBox = document.getElementById('fuel-checkboxes');
  allFuelTypes.forEach(function(title) {{
    const id = 'fuel-' + title.replace(/[^a-zA-Zа-яА-Я0-9]/g, '_');
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = id;
    cb.checked = defaultChecked.has(title);
    cb.dataset.fuelTitle = title;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(title));
    fuelBox.appendChild(label);
  }});

  function selectedFuelTypes() {{
    const checked = new Set();
    document.querySelectorAll('#fuel-checkboxes input[type=checkbox]').forEach(function(cb) {{
      if (cb.checked) checked.add(cb.dataset.fuelTitle);
    }});
    return checked;
  }}

  function stationStatus(station, selectedFuels) {{
    const relevant = station.fuels.filter(function(f) {{ return selectedFuels.has(f.title); }});
    if (relevant.length === 0) return null;
    if (relevant.some(function(f) {{ return f.avail; }})) return 'avail';
    if (relevant.some(function(f) {{ return f.delivery; }})) return 'delivery';
    return 'out';
  }}

  const statusColor = {{ avail: '#1e8e3e', delivery: '#1a6fc4', out: '#c53929' }};

  ymaps.ready(function() {{
    const map = new ymaps.Map('map', {{
      center: [{center_lat}, {center_lon}],
      zoom: 11,
      controls: ['zoomControl', 'geolocationControl', 'trafficControl'],
    }});

    let placemarks = [];  // текущие метки на карте

    function rebuildMarkers() {{
      placemarks.forEach(function(pm) {{ map.geoObjects.remove(pm); }});
      placemarks = [];

      const selectedFuels = selectedFuelTypes();
      const showAvail = document.getElementById('show-avail').checked;
      const showDelivery = document.getElementById('show-delivery').checked;
      const showOut = document.getElementById('show-out').checked;
      let visibleCount = 0;

      stations.forEach(function(s) {{
        const status = stationStatus(s, selectedFuels);
        if (status === null) return;
        if (status === 'avail' && !showAvail) return;
        if (status === 'delivery' && !showDelivery) return;
        if (status === 'out' && !showOut) return;

        const placemark = new ymaps.Placemark([s.lat, s.lon], {{
          balloonContent: s.balloon,
        }}, {{
          preset: 'islands#circleIcon',
          iconColor: statusColor[status],
        }});
        placemark._searchText = (s.name + ' ' + s.address).toLowerCase();
        placemark._latlon = [s.lat, s.lon];
        map.geoObjects.add(placemark);
        placemarks.push(placemark);
        visibleCount++;
      }});

      document.getElementById('visible-count').innerHTML =
        '<b>' + visibleCount + '</b> показано с текущими фильтрами';
    }}

    document.querySelectorAll('#filters input[type=checkbox]').forEach(function(cb) {{
      cb.addEventListener('change', rebuildMarkers);
    }});

    const searchInput = document.getElementById('search');
    searchInput.addEventListener('input', function() {{
      const q = searchInput.value.trim().toLowerCase();
      if (!q) return;
      const match = placemarks.find(function(pm) {{ return pm._searchText.includes(q); }});
      if (match) {{
        map.setCenter(match._latlon, 14);
        match.balloon.open();
      }}
    }});

    rebuildMarkers();
  }});
</script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Интерактивная карта заправок gpnbonus.ru (Яндекс.Карты)")
    parser.add_argument("--city", type=str, help="фильтр по городу (по умолчанию — как в config.json)")
    parser.add_argument("--no-open", action="store_true", help="не открывать браузер автоматически")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("База данных ещё пустая — сначала запустите poll.py хотя бы один раз.")
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    region_id = config["region_id"]
    city = args.city or (config.get("city") or "").strip() or None

    store = Storage(DB_PATH)
    path = generate_map_dashboard(
        store, region_id, city, fuel_filter=config.get("fuel_filter"),
        yandex_maps_api_key=config.get("yandex_maps_api_key"),
        dashboard_filename=config.get("dashboard_filename", "dashboard.html"),
    )
    store.close()

    print(f"Карта сохранена: {path}")
    if not config.get("yandex_maps_api_key"):
        print("ВНИМАНИЕ: yandex_maps_api_key не задан в config.json — карта покажет заглушку вместо самой карты.")
    if not args.no_open:
        webbrowser.open(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
