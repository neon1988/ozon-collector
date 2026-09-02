import os

VERSION = "1.16.3"
SCHEMA_VERSION = "2.0"

# Жёсткий предел параллельных воркеров (--parallel). Переопределяется env
# OZON_PARALLEL_MAX — для стресс-тестов «сколько сессий выдержит Ozon».
# По умолчанию 8 (Фаза B, PROJECT_NOTES §18).
PARALLEL_MAX_WORKERS = int(os.environ.get("OZON_PARALLEL_MAX", "8"))

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
