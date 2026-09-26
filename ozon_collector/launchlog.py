r"""Общий хелпер для логов автозапуска браузера (cdp.py + browser.py).

Почему не tempfile + «вернуть путь, не забудь удалить»: оба вызывающих
забывали (2 мусорных файла в %TEMP% за прогон тестов). Владение файлом —
источник багов. Поэтому лог ЖИВЁТ по фиксированному пути на порт
(`%TEMP%\ozon_launch_<port>.log`), перезаписывается при каждом запуске и не
нуждается в уборке: после прогона он остаётся как улика для разбора.
"""
import os
import tempfile

LOG_PREFIX = "ozon_launch_"
LOG_DIR_ENV = "OZON_LAUNCH_LOG_DIR"


def launcher_log_dir() -> str:
    """Каталог логов: реальный %TEMP% либо override из env (тесты)."""
    override = os.environ.get(LOG_DIR_ENV)
    if not override:
        return tempfile.gettempdir()
    os.makedirs(override, exist_ok=True)
    return override


def launcher_log_path(port: int | str) -> str:
    """Путь лога автозапуска для CDP-порта (один на порт, truncate при старте)."""
    return os.path.join(launcher_log_dir(), f"{LOG_PREFIX}{port}.log")


def read_log_tail(path: str | None, limit: int = 12) -> str:
    """Хвост лога одной строкой; пусто/битый файл → явная подсказка.

    Пустой вывод = самый частый случай поломки (обнулённый launch-скрипт),
    поэтому он проговаривается словами, а не молчанием.
    """
    if not path:
        return ""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    if not raw.strip():
        return " (лаунчер не вывел ничего — проверь, что файл не пуст/не бит)"
    text = raw.decode("utf-8", errors="replace").replace("\r", "")
    lines = [ln for ln in text.splitlines() if ln.strip()][-limit:]
    return " | " + " | ".join(ln.strip() for ln in lines)
