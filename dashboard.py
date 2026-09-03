#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Дашборд: HTML-страница с наличием топлива по каждой станции, сгруппированная
по городам, с автообновлением.

Использование:
    python3 dashboard.py                        -- сгенерировать и открыть в браузере
    python3 dashboard.py --no-open              -- только сгенерировать файл, не открывать

Файл dashboard.html перезаписывается КАЖДЫЙ РАЗ, когда отрабатывает poll.py
(вызывается автоматически в конце опроса) — значит, если у вас настроен cron на
poll.py, дашборд сам обновляется каждые 15 минут. Страница содержит авто-обновление
раз в 60 секунд (просто перечитывает тот же файл с диска), так что если открыть её
в браузере один раз и оставить вкладку — она будет "живой" сама по себе.
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from datetime import datetime
from pathlib import Path

from storage import Storage
from analysis import compute_station_fuel_stats, human_duration

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gpn_stations.db"
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_PATH = BASE_DIR / "dashboard.html"

AUTO_REFRESH_SEC = 60


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def generate_dashboard(
    store: Storage, region_id: int, city: str | None,
    output_path: Path = OUTPUT_PATH, fuel_filter: list[str] | None = None,
    map_filename: str = "map.html",
) -> Path:
    """
    Строит HTML-дашборд из текущих статусов в базе и сохраняет в output_path.
    Вызывается и из poll.py (после каждого опроса), и напрямую из этого файла.

    map_filename: имя файла карты — используется только для ссылки "Карта" в
    шапке страницы (сам файл этой функцией не трогается).

    fuel_filter: список подстрок (например ["95", "100"]) — если задан, в дашборд
    попадают только виды топлива, чьё название содержит хотя бы одну из них.
    Нужен, чтобы старые записи в базе (сделанные до того, как фильтр был включён
    в poll.py) не "просачивались" обратно в дашборд как лишние колонки.
    """
    rows = store.current_status_for_region(region_id, city=city)
    now = datetime.now()

    if fuel_filter:
        rows = [
            r for r in rows
            if any(substr.lower() in (r[7] or "").lower() for substr in fuel_filter)
        ]

    # Группируем по станции, собираем набор видов топлива по порядку появления
    stations: dict[int, dict] = {}
    fuel_order: list[str] = []
    for station_id, name, st_city, address, latitude, longitude, fuel_id, fuel_title, avail, delivery, updated_at in rows:
        if station_id not in stations:
            stations[station_id] = {
                "name": name, "city": st_city, "address": address,
                "latitude": latitude, "longitude": longitude,
                "fuels": {}, "latest_update": updated_at,
            }
        stations[station_id]["fuels"][fuel_title] = {
            "avail": bool(avail), "delivery": bool(delivery), "updated_at": updated_at,
            "fuel_id": fuel_id,
        }
        if updated_at > stations[station_id]["latest_update"]:
            stations[station_id]["latest_update"] = updated_at
        if fuel_title not in fuel_order:
            fuel_order.append(fuel_title)

    fuel_order.sort()  # стабильный порядок колонок
    default_fuel_checked = {
        f for f in fuel_order if any(sub in f.lower() for sub in ("95", "100"))
    } or set(fuel_order)

    total = len(stations)
    with_any_fuel = sum(
        1 for s in stations.values() if any(f["avail"] for f in s["fuels"].values())
    )

    # Сортируем станции: город -> название
    sorted_station_ids = sorted(
        stations.keys(), key=lambda sid: (stations[sid]["city"] or "", stations[sid]["name"] or "")
    )

    rows_html = []
    for sid in sorted_station_ids:
        s = stations[sid]
        cells = []
        for fuel in fuel_order:
            info = s["fuels"].get(fuel)
            fuel_attr = _esc(fuel)
            if info is None:
                cells.append(f'<td class="cell na" data-fuel="{fuel_attr}" data-status="na">—</td>')
                continue
            if info["avail"]:
                cls, icon, label = "avail", "✅", "есть"
            elif info["delivery"]:
                cls, icon, label = "delivery", "🚛", "в пути"
            else:
                cls, icon, label = "out", "❌", "нет"

            # Время последнего привоза (когда топливо последний раз ПОЯВИЛОСЬ
            # после отсутствия) — по истории конкретно этого вида топлива на
            # этой станции. Если истории мало (первый опрос и т.п.) — просто
            # не показываем подпись, не гадаем.
            sub_html = ""
            history_rows = store.fuel_history_for_station(sid, info["fuel_id"])
            fuel_stats = compute_station_fuel_stats(history_rows)
            if fuel_stats and fuel_stats["last_restock_time"]:
                ago = human_duration((now - fuel_stats["last_restock_time"]).total_seconds())
                sub_html = f'<div class="cell-sub">привезли {ago} назад</div>'

            cells.append(
                f'<td class="cell {cls}" data-fuel="{fuel_attr}" data-status="{cls}">'
                f'<span class="icon">{icon}</span>{label}{sub_html}</td>'
            )

        lat = s.get("latitude") or ""
        lon = s.get("longitude") or ""
        rows_html.append(
            f'<tr data-lat="{lat}" data-lon="{lon}">'
            f'<td class="station-name">{_esc(s["name"])}</td>'
            f'<td class="station-addr">{_esc(s["city"])}, {_esc(s["address"])}</td>'
            + "".join(cells)
            + f'<td class="distance-cell">—</td>'
            + f'<td class="updated">{_time_ago(s["latest_update"], now)}</td>'
            "</tr>"
        )

    fuel_headers = "".join(f'<th data-fuel="{_esc(f)}">{_esc(f)}</th>' for f in fuel_order)

    city_label = f" — {_esc(city)}" if city else ""
    generated_at = now.strftime("%d.%m.%Y %H:%M:%S")
    fuel_order_json = json.dumps(fuel_order, ensure_ascii=False)
    default_fuel_checked_json = json.dumps(sorted(default_fuel_checked), ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{AUTO_REFRESH_SEC}">
<title>Наличие топлива{city_label}</title>
<style>
  :root {{
    --bg: #f7f8fa; --card: #ffffff; --border: #e3e6ea; --text: #1c2530;
    --muted: #6b7480; --avail: #1e8e3e; --avail-bg: #e6f4ea;
    --out: #c53929; --out-bg: #fce8e6; --delivery: #1a6fc4; --delivery-bg: #e8f0fe;
  }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  body {{
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  h1 {{ font-size: 20px; margin: 0 0 4px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .nav-link {{
    font-size: 12px; font-weight: 500; color: #1a6fc4; text-decoration: none;
    border: 1px solid #1a6fc4; border-radius: 6px; padding: 3px 10px; white-space: nowrap;
  }}
  .nav-link:hover {{ background: #e8f0fe; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 20px; }}
  .summary {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
  .stat-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 18px; min-width: 140px; flex: 1 1 140px;
  }}
  .stat-card .num {{ font-size: 22px; font-weight: 600; }}
  .stat-card .label {{ font-size: 12px; color: var(--muted); }}
  input#search {{
    width: 100%; max-width: 320px; padding: 10px 12px; border: 1px solid var(--border);
    border-radius: 8px; font-size: 16px; margin-bottom: 16px; background: var(--card);
  }}
  .table-scroll {{
    width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch;
    border: 1px solid var(--border); border-radius: 10px; background: var(--card);
  }}
  table {{
    width: 100%; border-collapse: collapse; font-size: 13px;
  }}
  th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }}
  th {{
    background: #fafbfc; font-weight: 600; color: var(--muted);
    text-transform: uppercase; font-size: 11px; letter-spacing: 0.03em;
    position: sticky; top: 0;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #fafbfc; }}
  .station-name {{
    font-weight: 600; white-space: nowrap;
    position: sticky; left: 0; background: var(--card); z-index: 1;
  }}
  tr:hover .station-name {{ background: #fafbfc; }}
  .station-addr {{ color: var(--muted); white-space: nowrap; }}
  .cell {{ white-space: nowrap; font-weight: 500; }}
  .cell .icon {{ margin-right: 4px; }}
  .cell-sub {{ font-weight: 400; font-size: 11px; color: var(--muted); white-space: nowrap; margin-top: 1px; }}
  .cell.avail {{ color: var(--avail); background: var(--avail-bg); }}
  .cell.out {{ color: var(--out); background: var(--out-bg); }}
  .cell.delivery {{ color: var(--delivery); background: var(--delivery-bg); }}
  .cell.na {{ color: var(--muted); }}
  .distance-cell {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
  .updated {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
  .footer {{ margin-top: 16px; color: var(--muted); font-size: 12px; }}
  .scroll-hint {{ display: none; color: var(--muted); font-size: 11px; margin-bottom: 8px; }}

  .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 12px; }}
  #geo-btn {{
    font-size: 13px; font-weight: 500; color: var(--text); background: var(--card);
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; cursor: pointer;
  }}
  #geo-btn:hover {{ background: #f0f2f4; }}
  #geo-status {{ font-size: 12px; color: var(--muted); }}

  #filters {{
    display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 16px; padding: 12px 14px;
    background: var(--card); border: 1px solid var(--border); border-radius: 10px; font-size: 12px;
  }}
  .filter-group b {{ display: block; margin-bottom: 4px; color: var(--text); font-size: 12px; }}
  .filter-group label {{ display: inline-flex; align-items: center; gap: 4px; margin-right: 12px; cursor: pointer; white-space: nowrap; padding: 2px 0; }}
  .filter-group input {{ cursor: pointer; width: 15px; height: 15px; }}

  @media (max-width: 640px) {{
    body {{ padding: 12px; }}
    h1 {{ font-size: 17px; }}
    .subtitle {{ margin-bottom: 14px; }}
    .stat-card {{ padding: 10px 12px; min-width: 100px; }}
    .stat-card .num {{ font-size: 18px; }}
    .scroll-hint {{ display: block; }}
    table {{ font-size: 12px; }}
    th, td {{ padding: 8px 9px; }}
    #filters {{ display: none; }}
    #filters.open {{ display: flex; }}
    #filters-toggle {{ display: inline-block !important; }}
    .filter-group {{ width: 100%; }}
  }}
</style>
</head>
<body>
  <h1>⛽ Наличие топлива{city_label} <a class="nav-link" href="{_esc(map_filename)}">🗺️ Карта</a></h1>
  <div class="subtitle">Обновлено: {generated_at} · автообновление каждые {AUTO_REFRESH_SEC} сек</div>

  <div class="summary">
    <div class="stat-card"><div class="num">{total}</div><div class="label">станций</div></div>
    <div class="stat-card"><div class="num">{with_any_fuel}</div><div class="label">с топливом хоть какого-то вида</div></div>
    <div class="stat-card"><div class="num" id="visible-count">{total}</div><div class="label">показано с текущими фильтрами</div></div>
  </div>

  <div class="toolbar">
    <input type="text" id="search" placeholder="Поиск по названию или адресу..." style="margin-bottom:0;">
    <button id="geo-btn" type="button">📍 Ближайшие ко мне</button>
    <button id="filters-toggle" type="button" style="display:none;">⚙️ Фильтры</button>
    <span id="geo-status"></span>
  </div>

  <div id="filters">
    <div class="filter-group">
      <b>Показывать колонки топлива:</b>
      <span id="fuel-checkboxes"></span>
    </div>
    <div class="filter-group">
      <b>Показывать станции со статусом (по выбранным видам топлива):</b>
      <label><input type="checkbox" id="show-avail" checked> ✅ Есть</label>
      <label><input type="checkbox" id="show-delivery" checked> 🚛 В пути</label>
      <label><input type="checkbox" id="show-out" checked> ❌ Нет</label>
    </div>
  </div>

  <div class="scroll-hint">← Проведите пальцем по таблице, чтобы увидеть остальные колонки →</div>

  <div class="table-scroll">
    <table id="dashboard-table">
      <thead>
        <tr>
          <th>Станция</th>
          <th>Адрес</th>
          {fuel_headers}
          <th>Расстояние</th>
          <th>Обновлено</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows_html) if rows_html else '<tr><td colspan="99">Нет данных — запустите poll.py</td></tr>'}
      </tbody>
    </table>
  </div>

  <div class="footer">Данные собраны ботом мониторинга gpnbonus.ru. Это не официальная информация сети АЗС.</div>

<script>
  const fuelOrder = {fuel_order_json};
  const defaultFuelChecked = new Set({default_fuel_checked_json});
  const rows = Array.from(document.querySelectorAll('#dashboard-table tbody tr'));
  const tbody = document.querySelector('#dashboard-table tbody');
  const visibleCountEl = document.getElementById('visible-count');

  // Строим чекбоксы видов топлива
  const fuelBox = document.getElementById('fuel-checkboxes');
  fuelOrder.forEach(function(title) {{
    const id = 'fuel-' + title.replace(/[^a-zA-Zа-яА-Я0-9]/g, '_');
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = id;
    cb.checked = defaultFuelChecked.has(title);
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

  function applyFilters() {{
    const q = document.getElementById('search').value.trim().toLowerCase();
    const selectedFuels = selectedFuelTypes();
    const showAvail = document.getElementById('show-avail').checked;
    const showDelivery = document.getElementById('show-delivery').checked;
    const showOut = document.getElementById('show-out').checked;

    // Скрыть/показать колонки топлива
    fuelOrder.forEach(function(title) {{
      const visible = selectedFuels.has(title);
      document.querySelectorAll('[data-fuel="' + CSS.escape(title) + '"]').forEach(function(el) {{
        el.style.display = visible ? '' : 'none';
      }});
    }});

    let visibleCount = 0;
    rows.forEach(function(r) {{
      const textMatch = !q || r.textContent.toLowerCase().includes(q);

      // Общий статус станции по выбранным видам топлива (та же логика, что на
      // карте): есть > в пути > нет. Так же интуитивно понятно поведение
      // чекбоксов "Есть/В пути/Нет", как и на странице с картой.
      let hasAvail = false, hasDelivery = false, anyRelevant = false;
      const cells = r.querySelectorAll('td.cell');
      cells.forEach(function(td) {{
        if (!selectedFuels.has(td.dataset.fuel)) return;
        const st = td.dataset.status;
        if (st === 'na') return;
        anyRelevant = true;
        if (st === 'avail') hasAvail = true;
        if (st === 'delivery') hasDelivery = true;
      }});

      let visible;
      if (!anyRelevant) {{
        // У станции нет данных ни по одному из выбранных видов топлива —
        // не прячем её из-за статуса, только по текстовому поиску.
        visible = textMatch;
      }} else if (hasAvail) {{
        visible = textMatch && showAvail;
      }} else if (hasDelivery) {{
        visible = textMatch && showDelivery;
      }} else {{
        visible = textMatch && showOut;
      }}
      r.style.display = visible ? '' : 'none';
      if (visible) visibleCount++;
    }});

    visibleCountEl.textContent = visibleCount;
  }}

  document.getElementById('search').addEventListener('input', applyFilters);
  document.querySelectorAll('#filters input[type=checkbox]').forEach(function(cb) {{
    cb.addEventListener('change', applyFilters);
  }});
  // Чекбоксы видов топлива создаются динамически — вешаем обработчик через делегирование
  fuelBox.addEventListener('change', applyFilters);

  const filtersToggle = document.getElementById('filters-toggle');
  const filtersPanel = document.getElementById('filters');
  filtersToggle.style.display = window.innerWidth <= 640 ? 'inline-block' : 'none';
  filtersToggle.addEventListener('click', function() {{
    filtersPanel.classList.toggle('open');
  }});

  applyFilters();

  // Геолокация: сортировка по расстоянию
  function haversineKm(lat1, lon1, lat2, lon2) {{
    const R = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
              Math.sin(dLon / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }}

  const geoBtn = document.getElementById('geo-btn');
  const geoStatus = document.getElementById('geo-status');
  geoBtn.addEventListener('click', function() {{
    if (!navigator.geolocation) {{
      geoStatus.textContent = 'Геолокация не поддерживается этим браузером.';
      return;
    }}
    geoStatus.textContent = 'Определяем ваше местоположение...';
    navigator.geolocation.getCurrentPosition(function(pos) {{
      const userLat = pos.coords.latitude, userLon = pos.coords.longitude;
      rows.forEach(function(r) {{
        const lat = parseFloat(r.dataset.lat), lon = parseFloat(r.dataset.lon);
        const cell = r.querySelector('.distance-cell');
        if (!lat || !lon) {{
          r.dataset.distanceKm = '';
          cell.textContent = '—';
          return;
        }}
        const d = haversineKm(userLat, userLon, lat, lon);
        r.dataset.distanceKm = d;
        cell.textContent = d.toFixed(1) + ' км';
      }});
      const sorted = rows.slice().sort(function(a, b) {{
        const da = parseFloat(a.dataset.distanceKm);
        const db = parseFloat(b.dataset.distanceKm);
        const va = isNaN(da) ? Infinity : da;
        const vb = isNaN(db) ? Infinity : db;
        return va - vb;
      }});
      sorted.forEach(function(r) {{ tbody.appendChild(r); }});
      geoStatus.textContent = 'Отсортировано по расстоянию от вас.';
    }}, function(err) {{
      geoStatus.textContent = 'Не удалось определить местоположение (' + err.message + ').';
    }});
  }});
</script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Дашборд наличия топлива по станциям")
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
    path = generate_dashboard(store, region_id, city, fuel_filter=config.get("fuel_filter"),
                               map_filename=config.get("map_filename", "map.html"))
    store.close()

    print(f"Дашборд сохранён: {path}")
    if not args.no_open:
        webbrowser.open(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
