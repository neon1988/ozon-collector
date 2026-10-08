"""Уборка кэша профилей Chromium (OZON_PROFILE_BASE).

Мотивация (живой замер 2026-10-07): профиль озона раздувается НЕ куками, а
кэшем. 9 профилей = 6738 МБ, из них 92.3% воспроизводимо: `Default/Cache`
(448 МБ на воркер) + `Code Cache` (138 МБ) + BrowserMetrics (124 МБ) +
component_crx_cache (40) + Safe Browsing (37) + WasmTtsEngine (22).
Состояние сессии — Cookies/Local Storage/Sessions/IndexedDB/Preferences/
Network — это 78.6 МБ на 8 воркеров (6-13 МБ на профиль), и оно НЕ трогается.

`--disk-cache-size` (launch_chrome.ps1) ограничивает только `Default/Cache`,
поэтому без уборки папка всё равно растёт примерно до 490 МБ на воркер.

Модуль существует, а не живёт в scripts/, потому что список каталогов нужен
ДВУМ вызывающим: демону (preflight, между прогонами) и ручному скрипту
`scripts/clean_profile_cache.py`. Дублировать список опаснее лишней строчки.

Инварианты:
  - удаляется ТОЛЬКО белый список; Cookies/Local Storage/Sessions/IndexedDB/
    Storage/Preferences/Network/Web Data в него не входят НИКОГДА;
  - на профиле с живым Chromium уборка молча пропускается (удаление файлов
    у живого Chromium ломает профиль — следующий запуск увидит битый кэш);
  - отказ удаления не роняет сбор: функция возвращает результат, вызывающий
    решает, что с ним делать.
"""
from __future__ import annotations

import os
import shutil

from . import process as P

#: Каталоги внутри профиля Chromium, которые Chromium пересоздаёт сам.
#: Относительные пути от корня профиля, разделитель "/" (приводится к os.sep).
JUNK_DIRS: tuple[str, ...] = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnWebGPUCache",
    "Default/DawnGraphiteCache",
    "Default/GrShaderCache",
    "Default/ShaderCache",
    "BrowserMetrics",
    "component_crx_cache",
    "Safe Browsing",
    "WasmTtsEngine",
    "OnDeviceHeadSuggestModel",
    "OptimizationHints",
    "segmentation_platform",
)

#: Каталоги с состоянием сессии. Присутствуют здесь как УТВЕРЖДЕНИЕ, что они
#: не пересекаются с JUNK_DIRS: проверяется тестом (инвариант безопасности).
STATE_DIRS: tuple[str, ...] = (
    "Default/Network",
    "Default/Cookies",
    "Default/Local Storage",
    "Default/Sessions",
    "Default/IndexedDB",
    "Default/Storage",
    "Default/Preferences",
    "Default/Web Data",
)


def dir_size_mb(path: str) -> float:
    """Размер каталога в МБ (рекурсивно). Ошибки чтения игнорируются."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / (1024 * 1024)


def junk_size_mb(profile_dir: str) -> float:
    """Сколько МБ кэша в профиле (сумма существующих JUNK_DIRS)."""
    total = 0.0
    for rel in JUNK_DIRS:
        path = os.path.join(profile_dir, rel.replace("/", os.sep))
        if os.path.isdir(path):
            total += dir_size_mb(path)
    return total


def clean_profile(profile_dir: str, apply: bool = False) -> dict:
    """Почистить один профиль. apply=False — только посчитать.

    Возвращает {freed_mb, dirs, skipped, remained_mb}. skipped=True означает
    «профиль занят живым Chromium, ничего не трогали» — вызывающий решает.
    """
    if not os.path.isdir(profile_dir):
        return {"freed_mb": 0.0, "dirs": 0, "skipped": False,
                "remained_mb": 0.0}
    token = os.path.basename(profile_dir).replace(".firefox", "")
    try:
        from .cdp import profile_browser_alive
        alive = profile_browser_alive(token)
    except Exception:
        # Не смогли определить живость — НЕ удаляем на всякий случай.
        alive = True
    if alive:
        return {"freed_mb": 0.0, "dirs": 0, "skipped": True,
                "remained_mb": dir_size_mb(profile_dir)}

    freed = 0.0
    removed = 0
    for rel in JUNK_DIRS:
        path = os.path.join(profile_dir, rel.replace("/", os.sep))
        if not os.path.isdir(path):
            continue
        freed += dir_size_mb(path)
        removed += 1
        if apply:
            shutil.rmtree(path, ignore_errors=True)
    return {"freed_mb": freed, "dirs": removed, "skipped": False,
            "remained_mb": dir_size_mb(profile_dir) if apply else 0.0}


def clean_profiles(tokens, apply: bool = False) -> dict:
    """Почистить набор профилей по их токенам (P.worker_profile_path и т.п.).

    Токены, а не пути: путь обязан считаться тем же кодом, что и в
    bootstrap_worker_profiles, иначе уборка поедет мимо профилей.
    """
    out: dict[str, dict] = {}
    for token in tokens:
        path = P.profile_path(token)
        res = clean_profile(path, apply=apply)
        res["path"] = path
        out[token] = res
    return out