"""Уборка кэша профилей: инварианты безопасности (живой замер 2026-10-07).

Профиль озона раздувается НЕ куками, а кэшем: 9 профилей = 6738 МБ, 92.3%
воспроизводимо, а состояние сессии — 78.6 МБ. Уборка удаляет кэш, поэтому
ошибка здесь = потеря кук и 18+ проверки, то есть холодная сессия и пазлы.
Отсюда и набор тестов: удаляется строго белый список, состояние не трогается,
живой Chromium блокирует уборку, сбой уборки не роняет сбор.
"""
from __future__ import annotations

import os

import pytest

from ozon_collector import parallel as PL
from ozon_collector import profile_gc


def _mkjunk(profile_dir: str) -> None:
    """Профиль с кэшем: каждый JUNK_DIR набит 1 КБ, STATE_DIR — тоже."""
    for rel in profile_gc.JUNK_DIRS:
        p = os.path.join(profile_dir, rel.replace("/", os.sep))
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "blob.bin"), "wb") as fh:
            fh.write(b"\0" * 1024)
    for rel in profile_gc.STATE_DIRS:
        p = os.path.join(profile_dir, rel.replace("/", os.sep))
        if rel.endswith(("Cookies", "Preferences", "Web Data")):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(b"state")
        else:
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, "keep.bin"), "wb") as fh:
                fh.write(b"KEEP")


class TestWhitelistSafety:
    def test_state_dirs_never_in_junk_list(self):
        """Инвариант безопасности: Cookies/Local Storage/Sessions/IndexedDB/
        Storage/Preferences/Network/Web Data не могут попасть в удаляемое."""
        overlap = set(profile_gc.STATE_DIRS) & set(profile_gc.JUNK_DIRS)
        assert not overlap, f"состояние сессии попало под удаление: {overlap}"

    def test_network_cookies_not_under_any_junk_dir(self):
        """`Default/Network` и `Default/Cookies` лежат по соседству с кэшем —
        путь в JUNK_DIRS не должен быть их префиксом (иначе rmtree снесёт их)."""
        for junk in profile_gc.JUNK_DIRS:
            for state in profile_gc.STATE_DIRS:
                assert not junk.startswith(state + "/"), \
                    f"{junk} вложен в {state}"

    def test_apply_keeps_state_files(self, tmp_path, monkeypatch):
        _mkjunk(str(tmp_path))
        monkeypatch.setattr("ozon_collector.cdp.profile_browser_alive",
                            lambda token: False)
        profile_gc.clean_profile(str(tmp_path), apply=True)
        for rel in profile_gc.STATE_DIRS:
            p = os.path.join(str(tmp_path), rel.replace("/", os.sep))
            if rel.endswith(("Cookies", "Preferences", "Web Data")):
                assert os.path.isfile(p), f"{rel} удалён уборкой"
            else:
                assert os.path.isdir(p), f"{rel} удалён уборкой"
                assert os.path.isfile(os.path.join(p, "keep.bin")), \
                    f"содержимое {rel} удалено уборкой"

    def test_apply_removes_every_junk_dir(self, tmp_path, monkeypatch):
        _mkjunk(str(tmp_path))
        monkeypatch.setattr("ozon_collector.cdp.profile_browser_alive",
                            lambda token: False)
        profile_gc.clean_profile(str(tmp_path), apply=True)
        for rel in profile_gc.JUNK_DIRS:
            p = os.path.join(str(tmp_path), rel.replace("/", os.sep))
            assert not os.path.isdir(p), f"{rel} остался после уборки"

    def test_dry_run_deletes_nothing(self, tmp_path, monkeypatch):
        _mkjunk(str(tmp_path))
        monkeypatch.setattr("ozon_collector.cdp.profile_browser_alive",
                            lambda token: False)
        res = profile_gc.clean_profile(str(tmp_path), apply=False)
        assert res["dirs"] == len(profile_gc.JUNK_DIRS)
        for rel in profile_gc.JUNK_DIRS:
            assert os.path.isdir(
                os.path.join(str(tmp_path), rel.replace("/", os.sep))), \
                f"dry-run удалил {rel}"


class TestLiveBrowserGuard:
    def test_skipped_when_browser_alive(self, tmp_path, monkeypatch):
        """Удалять кэш у живого Chromium нельзя: следующий запуск увидит битый
        профиль. Поэтому — пропуск, а не удаление."""
        _mkjunk(str(tmp_path))
        monkeypatch.setattr("ozon_collector.cdp.profile_browser_alive",
                            lambda token: True)
        res = profile_gc.clean_profile(str(tmp_path), apply=True)
        assert res["skipped"] is True
        assert res["freed_mb"] == 0.0
        assert os.path.isdir(
            os.path.join(str(tmp_path), "Default", "Cache")), "удалено при живом"

    def test_alive_check_failure_treated_as_alive(self, tmp_path, monkeypatch):
        """Не смогли определить живость → не удаляем на всякий случай."""
        _mkjunk(str(tmp_path))

        def _boom(token):
            raise RuntimeError("WMI down")

        monkeypatch.setattr("ozon_collector.cdp.profile_browser_alive", _boom)
        res = profile_gc.clean_profile(str(tmp_path), apply=True)
        assert res["skipped"] is True
        assert os.path.isdir(
            os.path.join(str(tmp_path), "Default", "Cache")), \
            "удалено при неопределённой живости"

    def test_missing_profile_is_not_skipped(self, tmp_path):
        res = profile_gc.clean_profile(str(tmp_path / "net-takogo"), apply=True)
        assert res["skipped"] is False and res["dirs"] == 0


class TestPreflightHook:
    """Уборка в preflight: точка вызова выбрана по инварианту — осиротевшие
    браузеры уже убиты, новые ещё не подняты."""

    def _spec(self, token, path, kind=None):
        s = {"worker_id": 1, "profile_token": token, "profile_path": path,
             "cdp_url": "http://127.0.0.1:9223", "port": 9223}
        if kind:
            s["kind"] = kind
        return s

    def test_cleans_dirty_profile(self, tmp_path, monkeypatch):
        prof = tmp_path / "ozon-profile-worker-1"
        _mkjunk(str(prof))
        monkeypatch.setattr("ozon_collector.cdp.profile_browser_alive",
                            lambda token: False)
        monkeypatch.setattr(PL.P, "profile_path", lambda tok: str(prof))
        monkeypatch.setattr(PL, "GC_MIN_MB", 0.0)
        touched = PL._gc_profile_caches([self._spec("ozon-profile-worker-1",
                                                    str(prof))])
        assert "ozon-profile-worker-1" in touched
        assert not os.path.isdir(os.path.join(str(prof), "Default", "Cache"))

    def test_skips_clean_profile(self, tmp_path, monkeypatch):
        """Чистый профиль не трогаем: пустой кэш = уже убрали, повторная уборка
        только жжёт время на обход каталогов."""
        prof = tmp_path / "ozon-profile-worker-2"
        os.makedirs(str(prof))
        monkeypatch.setattr("ozon_collector.cdp.profile_browser_alive",
                            lambda token: False)
        monkeypatch.setattr(PL.P, "profile_path", lambda tok: str(prof))
        monkeypatch.setattr(PL, "GC_MIN_MB", 300.0)
        assert PL._gc_profile_caches([self._spec("ozon-profile-worker-2",
                                                  str(prof))]) == {}

    def test_failure_does_not_raise(self, tmp_path, monkeypatch):
        """Ошибка уборки не должна ронять сбор: диск не критичен, профили да."""
        prof = tmp_path / "ozon-profile-worker-3"
        _mkjunk(str(prof))
        monkeypatch.setattr(PL.P, "profile_path", lambda tok: str(prof))
        monkeypatch.setattr(PL, "GC_MIN_MB", 0.0)

        def _boom(path, apply=False):
            raise RuntimeError("диск занят")

        monkeypatch.setattr(PL.profile_gc, "clean_profile", _boom)
        assert PL._gc_profile_caches([self._spec("ozon-profile-worker-3",
                                                  str(prof))]) == {}

    def test_firefox_skipped(self, tmp_path, monkeypatch):
        """Firefox-профили не чистим: белый список из хромиумных каталогов."""
        prof = tmp_path / "ozon-profile-worker-1.firefox"
        _mkjunk(str(prof))
        monkeypatch.setattr(PL.P, "profile_path", lambda tok: str(prof))
        monkeypatch.setattr(PL, "GC_MIN_MB", 0.0)
        monkeypatch.setattr(PL.profile_gc, "clean_profile",
                            lambda *a, **k: pytest.fail("firefox трогать нельзя"))
        assert PL._gc_profile_caches(
            [self._spec("ozon-profile-worker-1.firefox", str(prof),
                        kind="firefox")]) == {}

    def test_preflight_calls_gc(self, tmp_path, monkeypatch):
        """Уборка подключена именно к preflight, а не «где-то в модуле»."""
        calls = []
        monkeypatch.setattr(PL, "_gc_profile_caches", lambda specs: calls.append(1))
        monkeypatch.setattr(PL, "_port_free", lambda port: True)
        monkeypatch.setattr(PL.P, "browser_processes", lambda *a, **k: [])
        monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
        monkeypatch.setattr(PL.P, "orphaned_worker_browser_processes",
                            lambda *a, **k: [])
        assert PL.preflight_check([]) == []
        assert calls, "preflight_check не вызывает уборку кэша"