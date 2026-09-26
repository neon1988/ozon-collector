#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Установка автономного Chrome-for-Testing для ozon-collector.

Скачивает Chrome-for-Testing (stable, win64) с официального
storage.googleapis.com в локальный каталог и печатает путь chrome.exe.
Автономный браузер НЕ обновляется сам (без Google Update) — версия меняется
только явным перезапуском скрипта с --reset. Это даёт независимость сбора от
системного Chrome и его апдейтов (Omaha).

Так же подходит для Edge: OZON_CHROME_PATH может указывать на msedge.exe —
любой Chromium-семейство даёт тот же CDP/окна/anti-automation паттерн.

Пример:
    python scripts/setup_bundled_chrome.py
    # затем (Windows PowerShell):
    $env:OZON_CHROME_PATH = "C:\\...\\chrome-win64\\chrome.exe"
    # или (bash):
    export OZON_CHROME_PATH="/path/.../chrome-win64/chrome.exe"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

# fmt: off
# Список версий Chrome-for-Testing отдаётся с двух официальных хостов:
# storage.googleapis.com (основной) и googlechromelabs.github.io (зеркало).
# На части сетей/в отдельные дни первый отдаёт 404 — скрипт пробует по порядку.
KNOWN_GOOD_URLS = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    "last-known-good-versions-with-downloads.json",
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json",
)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ozon-collector/setup-bundled"
DEFAULT_BASE = (
    os.environ.get("LOCALAPPDATA") or str(Path.home())
)  # ...\opencode\browsers\chrome-for-testing
# fmt: on


def default_dir() -> Path:
    return (Path(DEFAULT_BASE) / "opencode" / "browsers"
            / "chrome-for-testing")


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_versions() -> dict:
    """Версии CfT с первого отвечающего хоста (или None)."""
    for url in KNOWN_GOOD_URLS:
        try:
            return _http_json(url)
        except Exception as e:
            print(f"[..] {url} недоступен: {e}", file=sys.stderr)
    return None


def chrome_url(payload: dict, channel: str, platform: str = "win64") -> str | None:
    ch = payload.get("channels", {}).get(channel, {})
    for item in ch.get("downloads", {}).get("chrome", []):
        if item.get("platform") == platform:
            return item.get("url")
    return None


def ensure_chrome(dest: Path, channel: str, version: str, url: str,
                  reset: bool) -> Path:
    app = dest / "chrome-win64"
    exe = app / "chrome.exe"
    if exe.exists() and not reset:
        return exe
    if reset:
        shutil.rmtree(app, ignore_errors=True)
    (dest / "_downloads").mkdir(parents=True, exist_ok=True)
    zip_path = dest / "_downloads" / f"chrome-{version}.zip"
    print(f"[..] скачиваю Chrome-for-Testing {channel} {version}")
    print(f"     {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=900) as r, \
            open(zip_path, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 256)
    print(f"[..] распаковываю: {zip_path.name} ({os.path.getsize(zip_path) / 2**20:.1f} MB)")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest)
    zip_path.unlink(missing_ok=True)
    if not exe.exists():
        raise RuntimeError(f"не найден chrome.exe в архиве: {exe}")
    return exe


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Установка автономного Chrome-for-Testing")
    ap.add_argument("--dir", default=None,
                    help="каталог установки (default: %(default)s)")
    ap.add_argument("--channel", default="Stable",
                    choices=["Stable", "Beta", "Dev"],
                    help="канал Chrome-for-Testing (default: %(default)s)")
    ap.add_argument("--reset", action="store_true",
                    help="пересоздать браузер, даже если уже установлен")
    args = ap.parse_args()

    dest = Path(args.dir) if args.dir else default_dir()
    print("[..] запрашиваю актуальные версии Chrome-for-Testing…")
    payload = fetch_versions()
    if payload is None:
        print("[FAIL] не получен список версий (пробовал все зеркала)",
              file=sys.stderr)
        return 1
    version = (payload.get("channels", {}).get(args.channel) or {}
               ).get("version")
    url = chrome_url(payload, args.channel)
    if not version or not url:
        print(f"[FAIL] канал {args.channel}: не найдена win64-сборка",
              file=sys.stderr)
        return 1
    try:
        exe = ensure_chrome(dest, args.channel, version, url, args.reset)
    except Exception as e:
        print(f"[FAIL] установка прервана: {e}", file=sys.stderr)
        return 1
    exe = exe.resolve()
    print(f"[OK] Chrome-for-Testing {args.channel} {version}")
    print(f"     chrome.exe: {exe}")
    print("Set in Windows PowerShell:")
    print(f'     $env:OZON_CHROME_PATH = "{exe}"')
    print("Set in bash:")
    print(f'     export OZON_CHROME_PATH="{exe}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())