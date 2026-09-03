#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Публикует табличный дашборд и карту в локальный git-репозиторий (клон GitHub
Pages) и отправляет изменения на GitHub — так они становятся доступны по
постоянной публичной ссылке вида https://<username>.github.io/<repo>/.

Вызывается автоматически из poll.py (если в config.json задан publish_repo_dir),
либо вручную:
    python3 publish.py

Требования:
- В publish_repo_dir должен лежать уже склонированный git-репозиторий с
  настроенным remote (см. README, раздел "GitHub Pages").
- Аутентификация для git push должна быть уже настроена (Personal Access Token
  прописан в URL remote или через git credential helper) — publish.py сам
  токены не запрашивает и не хранит.

ВАЖНО про имена файлов: GitHub Pages показывает корневую страницу сайта
только если файл называется index.html. Поэтому "dashboard_filename" в
config.json должен быть выставлен в "index.html" (см. config.example.json) —
тогда локальный файл и опубликованный совпадают по имени, и ссылки
"Таблица"/"Карта" в шапках страниц (сгенерированные dashboard.py и
map_dashboard.py) корректно ведут друг на друга что локально, что на сайте.
Файлы копируются под ТЕМИ ЖЕ именами, что и локально — никакого
переименования на этом шаге больше не происходит.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


class PublishError(RuntimeError):
    pass


def _run_git(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise PublishError(f"git {' '.join(args)} завершился с ошибкой:\n{result.stderr.strip()}")
    return result.stdout.strip()


def publish_dashboard(
    repo_dir: str,
    dashboard_filename: str = "index.html",
    map_filename: str = "map.html",
) -> bool:
    """
    Копирует локальные dashboard_filename и map_filename (если есть) в
    repo_dir под ТЕМИ ЖЕ именами, коммитит и пушит, если есть изменения.
    Возвращает True, если что-то было опубликовано, False — если изменений
    не было (нечего пушить).
    Бросает PublishError при сбое git-команды — вызывающий код решает, как на
    это реагировать (poll.py просто логирует и продолжает работу).
    """
    repo_path = Path(repo_dir).expanduser()
    if not repo_path.exists():
        raise PublishError(f"Папка {repo_path} не существует — сначала склонируйте репозиторий (см. README).")
    if not (repo_path / ".git").exists():
        raise PublishError(f"{repo_path} не похожа на git-репозиторий (нет папки .git).")

    dashboard_path = BASE_DIR / dashboard_filename
    map_path = BASE_DIR / map_filename

    if not dashboard_path.exists():
        raise PublishError(f"{dashboard_filename} ещё не создан — сначала запустите dashboard.py или poll.py.")

    shutil.copy(dashboard_path, repo_path / dashboard_filename)
    if map_path.exists():
        shutil.copy(map_path, repo_path / map_filename)

    status = _run_git(repo_path, "status", "--porcelain")
    if not status:
        return False  # нечего публиковать, файлы не изменились с прошлого раза

    _run_git(repo_path, "add", "-A")
    _run_git(repo_path, "commit", "-m", "Обновление дашборда")
    _run_git(repo_path, "push")
    return True


def main():
    if not CONFIG_PATH.exists():
        print("Не найден config.json", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    repo_dir = config.get("publish_repo_dir")
    if not repo_dir:
        print("В config.json не задан publish_repo_dir — публикация на GitHub Pages не настроена.")
        print("См. README, раздел «GitHub Pages», чтобы включить.")
        return

    dashboard_filename = config.get("dashboard_filename", "index.html")
    map_filename = config.get("map_filename", "map.html")

    try:
        published = publish_dashboard(repo_dir, dashboard_filename, map_filename)
    except PublishError as e:
        print(f"Ошибка публикации: {e}", file=sys.stderr)
        sys.exit(2)

    if published:
        print("Дашборд опубликован и отправлен на GitHub.")
    else:
        print("Изменений нет — публикация не потребовалась.")


if __name__ == "__main__":
    main()

