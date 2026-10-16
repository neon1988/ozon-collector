"""Тесты сетевого контура: per-worker прокси (ревизия 2.9, ADR §18.7).

Контракт: дефолт OFF, индекс 1 = worker-1, креденшелы никогда не утекают
в маскированную строку, опечатка в URL = warning + запуск без прокси (не
падение всего сбора).
"""
import pathlib

import pytest

from ozon_collector import browser as B
from ozon_collector import cdp
from ozon_collector import net
from ozon_collector import parallel


def _clear(mp):
    for k in ("OZON_PROXY_URL", "OZON_PROXY_URLS"):
        mp.delenv(k, raising=False)


def test_no_proxy_by_default(monkeypatch):
    _clear(monkeypatch)
    assert net.proxy_url_for_worker(1) is None
    assert net.proxy_url_for_worker(12) is None
    assert net.proxy_for_profile_token("ozon-profile-worker-3") is None


def test_single_proxy_for_all_workers(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OZON_PROXY_URL", "http://p1.example:8080")
    for k in (1, 5, 12):
        assert net.proxy_url_for_worker(k) == "http://p1.example:8080"


def test_list_maps_one_proxy_per_worker(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OZON_PROXY_URLS",
                       "http://p1.example:8080,http://p2.example:8080;")
    assert net.proxy_url_for_worker(1) == "http://p1.example:8080"
    assert net.proxy_url_for_worker(2) == "http://p2.example:8080"
    # список короче параллели → хвост идёт напрямую (не падаем)
    assert net.proxy_url_for_worker(3) is None


def test_profile_token_to_worker(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OZON_PROXY_URLS", "http://p1:1,http://p2:2,http://p3:3")
    assert net.worker_index_from_profile_token("ozon-profile-worker-2") == 2
    assert net.worker_index_from_profile_token("ozon-profile") is None
    assert net.worker_index_from_profile_token(None) is None
    assert net.worker_index_from_profile_token("garbage") is None
    assert net.proxy_for_profile_token("ozon-profile-worker-3") == "http://p3:3"


@pytest.mark.parametrize("url,expect", [
    ("http://h:8080", "http://h:8080"),
    ("HTTP://H.example:3128", "http://h.example:3128"),
    ("socks5://1.2.3.4:1080", "socks5://1.2.3.4:1080"),
    ("socks5h://1.2.3.4:1080", "socks5h://1.2.3.4:1080"),
    ("https://h:443", "https://h:443"),
])
def test_validate_ok(url, expect):
    assert net.validate_proxy_url(url) == expect


@pytest.mark.parametrize("url", [
    "", "   ", "ftp://h:21", "http://h", "http://", "1.2.3.4:8080",
    "socks4://h:1080",
])
def test_validate_rejects(url):
    with pytest.raises(ValueError):
        net.validate_proxy_url(url)


def test_mask_never_leaks_password():
    masked = net.mask_proxy("http://user:sup3rsecret@h.example:8080")
    assert "sup3rsecret" not in masked
    assert "user:***@" in masked
    assert masked.endswith("h.example:8080")
    assert net.has_credentials("http://user:sup3rsecret@h.example:8080")
    assert not net.has_credentials("http://h.example:8080")


def test_mask_invalid_is_not_echoed():
    assert net.mask_proxy("ftp://user:pw@h:21") == "<invalid-proxy>"
    assert net.mask_proxy(None) is None


def test_chrome_proxy_arg():
    assert net.chrome_proxy_arg("http://h:8080") == \
        "--proxy-server=http://h:8080"
    assert net.chrome_proxy_arg("socks5://h:1080/") == \
        "--proxy-server=socks5://h:1080"
    with pytest.raises(ValueError):
        net.chrome_proxy_arg("nope")


class _PopenSpy:
    def __init__(self):
        self.env = {}

    def __call__(self, cmd, env=None, **kw):
        self.env.update(env or {})


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    spy = _PopenSpy()
    script = tmp_path / "launch_chrome.ps1"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(cdp.subprocess, "Popen", spy)
    monkeypatch.setattr(cdp, "_launch_script_path", lambda: script)
    monkeypatch.setattr(cdp, "_launch_script_name", lambda: script.name)
    _clear(monkeypatch)
    return spy


@pytest.mark.allow_browser_launch
def test_launcher_receives_proxy_env(launcher, monkeypatch):
    """cdp._launch_browser кладёт PROXY в env лаунчера только для воркера."""
    monkeypatch.setenv("OZON_PROXY_URLS", "http://p1:1,http://p2:2")
    cdp._launch_browser("http://127.0.0.1:9223", profile_path="p",
                        profile_token="ozon-profile-worker-2")
    assert launcher.env["PROXY"] == "http://p2:2"
    assert launcher.env["PORT"] == "9223"


@pytest.mark.allow_browser_launch
def test_launcher_skips_worker_without_proxy(launcher, monkeypatch):
    monkeypatch.setenv("OZON_PROXY_URLS", "http://p1:1,http://p2:2")
    cdp._launch_browser("http://127.0.0.1:9223", profile_path="p",
                        profile_token="ozon-profile-worker-5")
    assert "PROXY" not in launcher.env


@pytest.mark.allow_browser_launch
def test_launcher_skips_broken_proxy(launcher, monkeypatch):
    """Опечатка в прокси = warning + старт БЕЗ прокси, а не падение."""
    monkeypatch.setenv("OZON_PROXY_URL", "ftp://bad:21")
    cdp._launch_browser("http://127.0.0.1:9223", profile_path="p",
                        profile_token="ozon-profile-worker-1")
    assert "PROXY" not in launcher.env


@pytest.mark.allow_browser_launch
def test_main_profile_never_gets_proxy(launcher, monkeypatch):
    monkeypatch.setenv("OZON_PROXY_URLS", "http://p1:1")
    cdp._launch_browser("http://127.0.0.1:9222", profile_path="p",
                        profile_token="ozon-profile")
    assert "PROXY" not in launcher.env


@pytest.fixture
def browser_launcher(monkeypatch, tmp_path):
    """Шпион subprocess.Popen для browser._launch_script (recycle-путь)."""
    spy = _PopenSpy()
    script = tmp_path / "launch_chrome.ps1"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(B.subprocess, "Popen", spy)
    monkeypatch.setattr(B, "_launch_script_path", lambda: script)
    monkeypatch.setattr(B, "_launch_script_name", lambda: script.name)
    _clear(monkeypatch)
    return spy


@pytest.mark.allow_browser_launch
def test_browser_launch_script_receives_proxy_env(browser_launcher,
                                                  monkeypatch):
    """browser._launch_script (recycle через BrowserProcessManager) кладёт
    PROXY воркера — раньше прокси терялся при recycle (аудит 2026-10-15)."""
    monkeypatch.setenv("OZON_PROXY_URLS", "http://p1:1,http://p2:2")
    B._launch_script("http://127.0.0.1:9224", profile_path="p",
                     profile_token="ozon-profile-worker-2")
    assert browser_launcher.env["PROXY"] == "http://p2:2"
    assert browser_launcher.env["PORT"] == "9224"


@pytest.mark.allow_browser_launch
def test_browser_launch_script_main_profile_no_proxy(browser_launcher,
                                                     monkeypatch):
    monkeypatch.setenv("OZON_PROXY_URLS", "http://p1:1")
    B._launch_script("http://127.0.0.1:9222", profile_path="p",
                     profile_token="ozon-profile")
    assert "PROXY" not in browser_launcher.env


def test_process_manager_forwards_profile_token(monkeypatch, tmp_path):
    """BrowserProcessManager.launch обязан передать token в _launch_script,
    иначе per-worker прокси не применится при recycle (аудит 2026-10-15)."""
    seen = {}

    def spy_script(url, profile_path=None, profile_token=None):
        seen["token"] = profile_token
        return B.LaunchRun("", None)

    monkeypatch.setattr(B, "_launch_script", spy_script)
    monkeypatch.setattr(B.BrowserProcessManager, "_await_launch",
                        lambda self, *a, **kw: None)
    m = B.BrowserProcessManager("http://127.0.0.1:9223",
                                profile_token="ozon-profile-worker-2")
    assert m.launch("L1") is None
    assert seen["token"] == "ozon-profile-worker-2"


def test_worker_spec_exposes_masked_proxy(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OZON_PROXY_URLS", "http://user:secret@w1.example:8080")
    spec = parallel.worker_spec(1)
    assert spec["proxy"] == "http://user:***@w1.example:8080"
    assert "secret" not in repr(spec)
    assert "proxy" not in parallel.worker_spec(2)

# ---- ревизия 2.9 P0: санитизация целевого URL -------------------------------

LIVE_BAD = ("[https://www.ozon.ru/category/zvukoizolyatsiya-33302/"
            "?insulationinstallation=320869%2C320870]"
            "(https://www.ozon.ru/category/fonari-dlya-velosipedov-11061/"
            "?type=47720)")


class TestNormalizeTargetUrl:
    def test_live_markdown_link(self):
        """Реальный случай 2026-09-26: markdown-ссылка в --url демона."""
        assert net.normalize_target_url(LIVE_BAD) == (
            "https://www.ozon.ru/category/fonari-dlya-velosipedov-11061/"
            "?type=47720")

    def test_plain_url_unchanged(self):
        u = "https://www.ozon.ru/category/x-123/"
        assert net.normalize_target_url(u) == u

    def test_empty_path_gets_slash(self):
        assert net.normalize_target_url("https://www.ozon.ru") == \
            "https://www.ozon.ru/"

    def test_strips_smart_quotes_and_spaces(self):
        assert net.normalize_target_url(
            "  “https://www.ozon.ru/search/?text=шины”  ") == \
            "https://www.ozon.ru/search/?text=шины"

    def test_strips_trailing_punctuation(self):
        assert net.normalize_target_url(
            "https://www.ozon.ru/category/a-1/.") == \
            "https://www.ozon.ru/category/a-1/"

    def test_extracts_url_from_text(self):
        assert net.normalize_target_url(
            "вот ссылка https://www.ozon.ru/category/b-2/?x=1 (смотри)") == \
            "https://www.ozon.ru/category/b-2/?x=1"

    def test_markdown_in_middle_of_line(self):
        assert net.normalize_target_url(
            "цель [категория](https://www.ozon.ru/category/c-3/) тут") == \
            "https://www.ozon.ru/category/c-3/"

    def test_angle_brackets(self):
        assert net.normalize_target_url(
            "<https://www.ozon.ru/category/d-4/>") == \
            "https://www.ozon.ru/category/d-4/"

    def test_query_preserved(self):
        u = "https://www.ozon.ru/search/?text=%D1%88%D0%B8%D0%BD%D1%8B&page=2"
        assert net.normalize_target_url(u) == u

    def test_fragment_dropped(self):
        assert net.normalize_target_url(
            "https://www.ozon.ru/category/e-5/#tile") == \
            "https://www.ozon.ru/category/e-5/"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            net.normalize_target_url("   ")

    def test_rejects_no_scheme(self):
        with pytest.raises(ValueError):
            net.normalize_target_url("www.ozon.ru/category/")

    def test_rejects_js_scheme(self):
        with pytest.raises(ValueError):
            net.normalize_target_url("javascript:alert(1)")

    def test_rejects_no_host(self):
        with pytest.raises(ValueError):
            net.normalize_target_url("https:///category/")

    def test_is_valid_target_url_helper(self):
        assert net.is_valid_target_url(LIVE_BAD) is True
        assert net.is_valid_target_url("ftp://ozon.ru/") is False
