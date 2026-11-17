"""Параллельный search-сбор: N изолированных воркер-процессов, каждый со своим Chromium.

Принцип (Фаза B, PROJECT_NOTES): параллельность = независимые воркер-процессы,
каждый владеет СВОИМ браузером (порт 9222+k, профиль ozon-profile-worker-{k}).
Воркер: attach_file → Session(свой) → шард-цикл (_collect_shard из search.py) →
payload по Pipe → exit 0. Parent: links в main-сессии, останов main-Chromium,
preflight (4 ресурса), spawn, join(timeout), merge по индексу.

Ownership-контракт: parent НЕ трогает браузер воркера (только cleanup по
зарегистрированному snapshot при hang). Всё через multiprocessing.get_context("spawn")
— единый путь и на Windows, и на Linux.
"""
from __future__ import annotations

import multiprocessing
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

from . import SCHEMA_VERSION, VERSION
from . import browser as B
from . import cache as K
from . import cdp
from . import collect as C
from . import log
from . import net
from . import process as P
from . import profile_gc
from . import search as S
from .browser import BrowserProcessManager
from .gather import run_gather
from .report import sort_digest_products

WORKER_LOG_DIR = "logs"

# Порог простоя воркера без heartbeat (2026-09-01): оркестратор следит за
# прогрессом воркера через {"t":"hb"} от worker_main (перехватывает
# collect._touch_watchdog). Если воркер жив, но дольше WORKER_STALL_S не
# сообщал о прогрессе — считаем его зависшим на CDP-вызове (goto/evaluate
# на заблокированном renderer'е висит без таймаута) и terminate'им с
# перезапуском. Единый контроль воркеров в оркестраторе и для parallel=1.
WORKER_STALL_S = 300           # 5 мин без heartbeat = зависание (в т.ч. параллель=1)

# Размер выдачи динамической раздачи (единиц товаров за один task_request).
# По умолчанию ОДИН товар: модель «грузчик берёт один ящик из стопки, относит,
# возвращается, берёт следующий» (решение владельца). Раздача пачками держалась
# на DEFAULT_BATCH_SIZE=5 только ради экономии handshake'ов, а handshake на фоне
# сбора товара (5-180 с) не вносит ничего: лишняя задержка ≤ poll (0.3 с) на
# товар — доли процента прогона, зато падение воркера теряет 1 товар, а не 5, и
# хвост раздачи выравнивается по фактической скорости каждого воркера.
# Явно переопределяется gather_cfg["batch_size"] (демон — run_cfg.batch_size).
DEFAULT_BATCH_SIZE = 1

# Старт воркеров не синхронный: N браузеров, открывших первые страницы в один
# миллисекунд, — сам по себе паттерн (п.6 ревью 2026-09-25). Разброс
# фиксированный (детерминированный seed), 0 — полностью отключить (тесты).
WORKER_START_STAGGER_S = 0.7
_stagger_rng = random.Random(0x0A11CE)


def _start_stagger_s(seed_key: int, base: float | None = None) -> float:
    """Пауза перед стартом воркера: uniform(0.5·base .. 1.5·base) секунд."""
    base = WORKER_START_STAGGER_S if base is None else float(base)
    if base <= 0:
        return 0.0
    _stagger_rng.seed(0x0A11CE + int(seed_key))
    return _stagger_rng.uniform(0.5 * base, 1.5 * base)


# ---- Спеки и сериализуемые структуры ---------------------------------------

def worker_spec(k: int) -> dict:
    """Спека воркера k: порт/профиль/лог. ЕДИНЫЙ источник имён (контракт).

    Порт = 9222 + k (k=1..N; база 9222 занята main-сессией). Профиль и путь —
    из process.worker_profile_* (тестируются отдельно).
    """
    k = int(k)
    browse = B.resolve_browser_for_parallel()
    spec = {
        "worker_id": k,
        "kind": browse,
        "profile_token": P.worker_profile_token(k),
        "profile_path": P.worker_profile_path(k),
        "log_path": _worker_log_path(k),
    }
    # Прокси воркера (ревизия 2.9, net.py): 1 профиль = 1 IP. Дефолт OFF.
    # В спек идёт ТОЛЬКО маскированный вид (host:port), без креденшелов.
    _proxy = net.proxy_url_for_worker(k)
    if _proxy:
        spec["proxy"] = net.mask_proxy(_proxy)
    if browse == "firefox":
        # stock Firefox + geckodriver/Selenium на своём persistent-профиле:
        # CDP-порт/окна не нужны вообще (см. cdp.Session._connect_firefox).
        # Живость оркестратор оценивает по процессу воркера + heartbeat,
        # а не по CDP.
        spec["port"] = None
        spec["cdp_url"] = None
    else:
        # Порт воркера — от РАЗРЕШЁННОЙ базы CDP, а не жёстко 9222+k. Живой
        # замер 2026-10-03: Windows взял диапазон 9172-9271 в исключения
        # (WSL/Hyper-V пересчитали резервации), поэтому 9222 И все 9223-9230
        # давали bind WSAEACCES, DevTools не поднимался, и каждый воркер падал
        # с «Chromium EXITED, CDP never came up» при полностью свободном порте.
        _wport = P.worker_cdp_port(k)
        spec["port"] = _wport
        spec["cdp_url"] = f"http://127.0.0.1:{_wport}"
    return spec


def _worker_log_path(k: int) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, WORKER_LOG_DIR, f"ozon_worker_{int(k)}.log")


def shard_items(items, parallel: int) -> list[list[tuple]]:
    """Round-robin по индексу: items[(idx,pid,url)] → shard[idx % parallel].

    Индекс — ЧАСТЬ задания (появляется в merge). Детерминизм: тот же links →
    те же шарды. Каждый товар ровно в одном шарде.
    """
    parallel = max(1, int(parallel))  # 0 → ZeroDivisionError в idx % parallel
    shards: list[list[tuple]] = [[] for _ in range(parallel)]
    for idx, pid, url in items:
        shards[idx % parallel].append((idx, pid, url))
    return shards


# ---- Preflight: 4 независимых ресурса ---------------------------------------

def _port_free(port: int) -> bool:
    """Свободен ли порт — по bind, а не по TCP-connect.

    Connect не ловил зарезервированный Windows excludedportrange: bind даёт
    WSAEACCES, а connect «не открывается» → ложное «свободен». Источник
    истины один — process.port_bindable, общий с запуском браузеров.
    """
    ok, _why = P.port_bindable(int(port))
    return ok


def preflight_check(specs: list[dict]) -> list[str]:
    """Проверка 4 ресурсов для каждого воркера; [] = готов к старту.

    PORT (не открывается), PROCESS OWNERSHIP (нет matching-процессов на
    (порт, токен)), CDP (/json/version недоступен), PROFILE (не совпадает с
    main и попарно различен; каталог создаётся). Любое нарушение → fatal:
    воркеры НЕ запускаются (без fallback).
    """
    errs: list[str] = []
    all_tokens = [P.PROFILE_TOKEN] + [s["profile_token"] for s in specs]
    if len(set(all_tokens)) != len(all_tokens):
        errs.append("профили воркеров не попарно различны или совпадают с main")
    all_paths = [s["profile_path"] for s in specs]
    if len(set(all_paths)) != len(all_paths):
        errs.append("profile_path воркеров не попарно различны")

    for s in specs:
        w = s["worker_id"]
        if s.get("kind") == "firefox":
            # Firefox-воркер: НЕТ CDP-контракта (порт/cdp_url пусты, см.
            # worker_spec). Живость оцениваем по firefox-процессам на нашем
            # профиле (geckodriver/Selenium) + heartbeat. Осиротевший
            # firefox от мёртвого родителя убираем по профилю — иначе
            # следующий прогон навсегда упирается в preflight. Ownership-контракт
            # для firefox = профиль: всё, что на нашем profile_token, наше
            # (тот же контракт, что в cdp.Session._connect_firefox).
            orphans = P.firefox_browser_processes(s["profile_token"])
            if orphans:
                try:
                    P.kill_records([P.ProcessRecord(f"worker{w}-orphan", ident)
                                    for ident in orphans], wait_s=3.0)
                    log.warning(f"[parallel] worker{w}: убран осиротевший "
                                f"Firefox ({len(orphans)} процесс(ов), "
                                f"родитель мёртв)")
                except Exception as e:
                    errs.append(f"worker{w}: не удалось убрать осиротевший "
                                f"Firefox: {type(e).__name__}: {e}")
            pp = s["profile_path"]
            if not os.path.isdir(pp):
                try:
                    os.makedirs(pp, exist_ok=True)
                except OSError as e:
                    errs.append(f"worker{w}: не удалось создать профиль "
                                f"{pp}: {e}")
            continue

    if any(s.get("kind") == "firefox" for s in specs):
        # Осиротевшие geckodriver: родитель нашего Firefox, держит окно даже
        # после гибели воркера (замер 2026-09-25 — 14 лишних окон). Убираем
        # только те, у которых нет живого firefox-ребёнка на нашем профиле.
        stale_drivers = P.orphaned_geckodriver_processes()
        if stale_drivers:
            try:
                P.kill_records([P.ProcessRecord("gecko-orphan", ident)
                                for ident in stale_drivers], wait_s=3.0)
                log.warning(f"[parallel] убрано осиротевших geckodriver: "
                            f"{len(stale_drivers)}")
            except Exception as e:
                errs.append(f"не удалось убрать осиротевшие geckodriver: "
                            f"{type(e).__name__}: {e}")

    # Автоочистка осиротевших браузеров воркеров: после аварийной гибели
    # родителя (kill/Ctrl+C/таймаут) воркер не выполняет finally и его
    # Chromium остаётся висеть. Идентифицируем по мёртвому родителю (PPID
    # воркера) и гасим — иначе следующий прогон навсегда упирается в
    # preflight. Живой воркер (реально идущий прогон) не трогаем.
    for s in specs:
        w = s["worker_id"]
        if s.get("kind") == "firefox":
            continue
        orphans = P.orphaned_worker_browser_processes(
            s["cdp_url"], s["profile_token"])
        if not orphans:
            continue
        try:
            manager = BrowserProcessManager(cdp_url=s["cdp_url"],
                                            profile_token=s["profile_token"])
            records = [P.ProcessRecord(f"worker{w}-orphan", ident)
                       for ident in orphans]
            manager.force_stop(records)
            log.warning(f"[parallel] worker{w}: убран осиротевший Chromium "
                        f"({len(records)} процесс(ов), родитель мёртв)")
        except Exception as e:
            errs.append(f"worker{w}: не удалось убрать осиротевший "
                        f"Chromium: {type(e).__name__}: {e}")
        # процесс убит, но CDP-порт/профиль освобождаются не мгновенно —
        # ждём, пока исчезнут все matching-процессы и порт перестанет отвечать
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not P.browser_processes(s["cdp_url"],
                                       profile_token=s["profile_token"]) \
                    and not cdp.browser_alive(s["cdp_url"]):
                break
            time.sleep(0.3)

    for s in specs:
        w = s["worker_id"]
        if s.get("kind") == "firefox":
            continue
        if not _port_free(s["port"]):
            errs.append(f"worker{w}: порт {s['port']} уже занят")
        if P.browser_processes(s["cdp_url"],
                               profile_token=s["profile_token"]):
            errs.append(f"worker{w}: уже есть Chromium на профиле "
                        f"{s['profile_token']} ({s['cdp_url']})")
        if cdp.browser_alive(s["cdp_url"]):
            errs.append(f"worker{w}: CDP жив на {s['cdp_url']}")
        pp = s["profile_path"]
        if not os.path.isdir(pp):
            try:
                os.makedirs(pp, exist_ok=True)
            except OSError as e:
                errs.append(f"worker{w}: не удалось создать профиль {pp}: {e}")
    _gc_profile_caches(specs)
    return errs


#: Порог «профиль грязный», МБ. Живой замер 07.10: сразу после уборки и с
#: работающим потолком `--disk-cache-size` на 9 профилях осталось 140-199 МБ
#: воспроизводимого мусора (Code Cache + BrowserMetrics + component_crx_cache).
#: При 300 МБ потолок не срабатывал бы никогда; при 200 МБ уборка идёт раз в
#: несколько прогонов, а состояние сессии (6-13 МБ) этот порог не задевает.
GC_MIN_MB = 200.0


def _gc_profile_caches(specs: list[dict]) -> dict:
    """Убрать кэш профилей воркеров перед стартом (живой замер 2026-10-07).

    Точка вызова выбрана по инварианту, а не по удобству: здесь осиротевшие
    браузеры воркеров УЖЕ убиты (шаг выше), а новые ещё не подняты, поэтому
    профили гарантированно не заняты. Без этого уборка в прогоне сдвинула бы
    момент на «после сбора», где браузеры ещё живы и удалять нельзя.

    Кэш воспроизводим: Cookies/Local Storage/Sessions/IndexedDB/Preferences/
    Network в белый список не входят (profile_gc.JUNK_DIRS). Ошибка уборки НЕ
    роняет preflight — сбор важнее чистоты диска.
    """
    tokens = [s["profile_token"] for s in specs
              if s.get("kind") != "firefox" and s.get("profile_token")]
    if not tokens:
        return {}
    freed_total = 0.0
    touched = {}
    for token in tokens:
        path = P.profile_path(token)
        try:
            junk = profile_gc.junk_size_mb(path)
        except OSError as e:
            log.debug(f"[gc] {token}: не удалось измерить кэш: {e}")
            continue
        if junk < GC_MIN_MB:
            continue
        try:
            res = profile_gc.clean_profile(path, apply=True)
        except Exception as e:
            log.warning(f"[gc] {token}: уборка не удалась ({type(e).__name__}: "
                        f"{e}) — продолжаю, на сбор это не влияет")
            continue
        if res["skipped"]:
            log.warning(f"[gc] {token}: живой Chromium на профиле, кэш "
                        f"({junk:.0f} МБ) оставлен")
            continue
        freed_total += res["freed_mb"]
        touched[token] = res["freed_mb"]
    if touched:
        log.info(f"[gc] убрал кэш профилей: " + ", ".join(
            f"{t} −{v:.0f} МБ" for t, v in sorted(touched.items()))
            + f" (всего −{freed_total:.0f} МБ, состояние сессии цело)")
    return touched


# ---- WorkerContext и reader ------------------------------------------------

class WorkerContext:
    """Состояние воркера со стороны parent: процесс, события, payload.

    conn — канал воркер→parent (все сообщения от воркера: start/heartbeat/
    progress/recovery/done_batch/done). p2w_conn — обратный канал parent→worker
    (только выдача пачек {t:"next", batch} / {t:"none"}).
    """

    def __init__(self, spec: dict, proc, conn, p2w_conn=None):
        self.spec = spec
        self.proc = proc
        self.conn = conn
        self.p2w_conn = p2w_conn
        self.pid: int | None = None
        self.started: bool = False
        self.recovery_events: list[dict] = []
        self.payload: dict | None = None
        self.done: bool = False
        self.reader: threading.Thread | None = None
        self.exitcode: int | None = None
        # Список индексов шарда этого воркера и ещё не обработанные (для
        # переноса недоделанной работы при перезапуске упавшего воркера).
        self.shard_indexes: list[int] = []
        self.pending_indexes: list[int] = []
        self.restarts: int = 0
        self.deadline_halted: bool = False
        # Снимок уже собранных товаров упавшего воркера, перенесённый в
        # воркера-замену при перезапуске (см. _await_workers). Без него
        # собранное до аварии терялось бы в merge_shard_results.
        self.carried_payload: dict | None = None
        # Последняя активность воркера (монотонные секунды): обновляется при
        # каждом сообщении от воркера (start/recovery/progress/done/hb).
        # Оркестратор по простою этого поля terminate'ит зависший воркер.
        self.last_activity: float = time.monotonic()
        # Динамическая пакетная раздача: флаг «воркер запросил пачку и ждёт
        # ответа» (выставляется reader-потоком при {t:"task_request"}/
        # {t:"done_batch"}). Модифицируется ТОЛЬКО reader-потоком; читается
        # (и сбрасывается) главным циклом _await_workers при раздаче.
        self.waiting_for_batch: bool = False
        # Индексы завершённой выдачи воркера (из done_batch processed+failed),
        # накопленные reader-потоком; главный цикл переводит их в
        # completed_indexes. Отдельная очередь — чтобы не было гонки reader
        # thread ↔ основной цикл по модификации общих completed_indexes.
        self.batch_completed: list[int] = []
        # The queue above still crosses two threads: the reader appends here
        # while the main loop drains it in _drain_completed. Without a shared
        # lock the reader can extend the list object right after the main loop
        # rebinds the attribute to a fresh [] and that index is lost
        # (audit 2026-10-18, B3). Guard both sides with this lock.
        self.batch_lock = threading.Lock()
        # ВСЕ products, пришедшие от воркера (progress + done_batch), без дублей
        # по индексу. Нужно потому, что done_batch несёт products ТОЛЬКО за
        # текущую выдачу, а wctx.payload перезаписывается каждым сообщением:
        # при terminate/respawn без накопления в merge уезжал бы последний
        # товар вместо всех собранных (аудит 2026-10-06, находка 4).
        self.acc_products: list[dict] = []
        # Ленивый запуск браузера (контракт «не поднимать окно раньше, чем
        # понадобятся данные со страницы»): счётчик warmup-событий воркера.
        # 0 = браузер не поднимался (все пачки из кэша), 1 = поднят один раз
        # и переиспользуется. Наполняется reader-потоком из {t:"warmup"}.
        self.browsers_started: int = 0
        self.warmups: list[dict] = []

    @property
    def worker_idx(self) -> int:
        return int(self.spec["worker_id"])


def _reader_loop(wctx: WorkerContext, on_event=None):
    """Читает протокол воркера (select по pipe на Windows не работает → поток)."""
    try:
        while True:
            try:
                msg = wctx.conn.recv()
            except (EOFError, OSError, ValueError):
                break
            t = msg.get("t")
            wctx.last_activity = time.monotonic()
            if t == "start":
                wctx.pid = msg.get("pid")
                wctx.started = True
            elif t == "recovery":
                wctx.recovery_events.append(msg)
            elif t == "progress":
                wctx.payload = msg.get("payload") or {}
                _absorb_products(wctx, wctx.payload)
                _update_pending(wctx)
            elif t == "task_request":
                wctx.waiting_for_batch = True
            elif t == "warmup":
                # Воркер реально поднял браузер (Session → launch_and_wait).
                wctx.browsers_started += 1
                wctx.warmups.append(msg)
            elif t == "done_batch":
                pl = msg.get("payload") or {}
                wctx.payload = pl
                _absorb_products(wctx, pl)
                _update_pending(wctx)
                with wctx.batch_lock:
                    wctx.batch_completed.extend(
                        list(pl.get("processed_indexes") or [])
                        + list(pl.get("failed_indexes") or []))
                # воркер сразу обязан отослать следующий task_request, но
                # страхуемся: после done_batch тоже готовы к выдаче пачки.
                wctx.waiting_for_batch = True
            elif t == "hb":
                pass  # heartbeat: last_activity уже обновлён выше
            elif t == "done":
                wctx.payload = msg.get("payload") or {}
                _absorb_products(wctx, wctx.payload)
                wctx.done = True
                _update_pending(wctx)
                if on_event is not None:
                    try:
                        on_event(wctx, msg)
                    except Exception:
                        pass
                break
            if on_event is not None:
                try:
                    on_event(wctx, msg)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            wctx.conn.close()
        except Exception:
            pass


def _update_pending(wctx: WorkerContext):
    """Обновить pending_indexes воркера из последнего progress/done payload.

    processed_indexes = уже доведённые до результата индексы. Недоделанными
    считаем те, что входят в шард воркера, но ещё не processed.
    """
    pl = wctx.payload or {}
    processed = set(pl.get("processed_indexes") or [])
    wctx.pending_indexes = [
        idx for idx in wctx.shard_indexes if idx not in processed]


def _absorb_products(wctx: WorkerContext, payload: dict) -> None:
    """Дописать products из сообщения в накопитель wctx.acc_products.

    Зачем накопитель: done_batch несёт products ТОЛЬКО за текущую выдачу (при
    batch_size=1 — за один товар), а wctx.payload перезаписывается каждым
    сообщением. При terminate/respawn payload брался бы последним товаром, и
    merge_shard_results пометил бы все остальные собранные товары как
    terminated/failed (аудит 2026-10-06, находка 4). Ключ уникальности — индекс
    товара (контракт merge_shard_results), поэтому повторы и кумулятивные
    payload'ы дублей не добавляют.
    """
    entries = payload.get("products") or []
    if not entries:
        return
    have = {int(e["index"]) for e in wctx.acc_products}
    for entry in entries:
        idx = int(entry["index"])
        if idx in have:
            continue
        have.add(idx)
        wctx.acc_products.append(entry)


def _with_acc_products(pl: dict, wctx: WorkerContext) -> dict:
    """payload воркера + накопленные products (для merge и carried_payload).

    Порядок — по порядку прихода выдач (накопитель), дополнительные записи из
    payload (их там быть не должно) дописываются в конец: merge_shard_results
    раскладывает всё по index, поэтому порядок на корректность не влияет.
    """
    if not wctx.acc_products:
        return pl
    products = list(wctx.acc_products)
    have = {int(e["index"]) for e in products}
    for entry in pl.get("products") or []:
        idx = int(entry["index"])
        if idx not in have:
            have.add(idx)
            products.append(entry)
    pl = dict(pl)
    pl["products"] = products
    return pl


def _spawn_ctx(spec: dict, task: dict, on_event=None) -> WorkerContext:
    """spawn воркера + reader-поток (протокол накапливается в WorkerContext).

    Два однонаправленных pipe (динамическая пакетная раздача):
      worker→parent: conn (все сообщения воркера);
      parent→worker : p2w_conn (только выдача пачек next/none).
    task["items"] — начальный список для merge-совместимости/журнала; при
    динамической раздаче фактическая выдача идёт через p2w_conn по заданию
    оркестратора, а НЕ через статический шард.
    """
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    # parent→worker канал: duplex=False → первый конец RECV, второй SEND.
    # Родителю нужен SEND (отправлять пачки), воркеру — RECV (принимать).
    # Переставляем: p2w_conn (родитель) = SEND-конец, p2w_child = RECV-конец.
    _p2w_recv, _p2w_send = multiprocessing.Pipe(duplex=False)
    p2w_child = _p2w_recv
    p2w_conn = _p2w_send
    proc = ctx.Process(target=worker_main,
                       args=(spec, task, child_conn, p2w_child),
                       name=f"ozon-worker-{spec['worker_id']}")
    proc.start()
    child_conn.close()
    p2w_child.close()
    wctx = WorkerContext(spec, proc, parent_conn, p2w_conn)
    wctx.shard_indexes = [int(idx) for idx, _p, _u in (task.get("items") or [])]
    wctx.pending_indexes = list(wctx.shard_indexes)
    wctx.waiting_for_batch = True
    wctx.reader = threading.Thread(target=_reader_loop, args=(wctx, on_event),
                                   daemon=True)
    wctx.reader.start()
    return wctx


# ---- Worker-процесс ---------------------------------------------------------

def _send(conn, msg: dict):
    try:
        conn.send(msg)
    except Exception:
        pass


def _empty_payload(k: int) -> dict:
    return {
        "worker_idx": int(k),
        "started": None,
        "finished": None,
        "exitcode": 0,
        "products": [],
        "processed_indexes": [],
        "failed_indexes": [],
        "recovery_metrics": {"recycles": 0, "events": []},
        "rss_bytes": None,
        "warmup": None,
        "fatal_error": None,
        "browser_stopped": False,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _make_session(spec: dict):
    """Session воркера (env OZON_WORKER_DOUBLES=1 → тест-дубль, см. tests)."""
    if os.environ.get("OZON_WORKER_DOUBLES") == "1":
        from tests._fake_worker import fake_session
        return fake_session(spec)
    _wm = spec.get("window_mode")
    return cdp.Session(spec["cdp_url"], profile_token=spec["profile_token"],
                       keep_window=None if _wm is None else (_wm == "visible"))


def _make_manager(spec: dict):
    """BrowserProcessManager воркера (тест-дубль при OZON_WORKER_DOUBLES=1)."""
    if os.environ.get("OZON_WORKER_DOUBLES") == "1":
        from tests._fake_worker import fake_manager
        return fake_manager(spec)
    return BrowserProcessManager(cdp_url=spec["cdp_url"],
                                 profile_token=spec["profile_token"])


def _item_needs_browser(item, gather_cfg: dict) -> bool:
    """Нужен ли браузер для ОДНОГО товара (обратная сторона cache_check).

    Повторяет логику gather.run_gather: max_age_s<=0 → кэш off (все miss),
    sorts-разнарядка и брак отзывов тоже требуют пересбора живьём. Любое
    исключение считаем за miss: дешевле поднять браузер, чем потерять товар.
    """
    _idx, pid, _url = item
    try:
        raw, reason = K.cache_check(
            pid, gather_cfg.get("max_age_s", 0),
            sorts=gather_cfg.get("sorts"),
            target_variant=C.parse_variant(gather_cfg.get("variant") or ""))
    except Exception:
        return True
    if raw is None or reason != "hit":
        return True
    return bool(C.variant_review_problems(
        raw, allow_filter_disabled=bool(
            gather_cfg.get("no_variant_mode"))))


def _first_browser_index(items, gather_cfg: dict) -> int:
    """Индекс первого cache-miss в пачке (len(items) → браузер не нужен)."""
    for i, item in enumerate(items):
        if _item_needs_browser(item, gather_cfg):
            return i
    return len(items)


def _batch_needs_browser(items, gather_cfg: dict) -> bool:
    """Нужен ли ЖИВОЙ браузер для пачки (есть хоть один cache-miss).

    Кэш-hit (валидный raw по cache_check, без variant_review_problems)
    обрабатывается без браузера. Проверка повторяет логику gather.run_gather:
    max_age_s<=0 → кэш off (все считаются miss), sorts-разнарядка и брак
    отзывов тоже требуют пересбора живьём.
    """
    return _first_browser_index(items, gather_cfg) < len(items)


def _collect_batch_without_browser(items, gather_cfg: dict,
                                   on_progress=None) -> list[dict]:
    """Собрать пачку целиком из кэша, без запуска браузера (run_gather, sess=None).

    Возвращает результат в формате search._collect_shard: res run_gather +
    recycle_attempts/new_tab_attempts (обнулены — recovery-цикл не нужен).
    on_progress — тот же колбэк, что в _run_shard (периодический progress
    оркестратору, защита от terminate-stall).
    """
    out = []
    for idx, pid, url in items:
        cfg = dict(gather_cfg)
        cfg["pid"] = pid
        cfg["url"] = url
        res = run_gather(cfg)
        res["recycle_attempts"] = 0
        res["new_tab_attempts"] = 0
        out.append(res)
        if on_progress is not None:
            try:
                on_progress(out, len(out), len(items))
            except Exception:
                pass
    return out


def _run_shard(spec: dict, sess, manager, items, gather_cfg: dict,
               max_recycles: int, recovery: dict, on_recovery,
               on_progress=None):
    """Шард-цикл воркера (тест-дубль при OZON_WORKER_DOUBLES=1)."""
    if os.environ.get("OZON_WORKER_DOUBLES") == "1":
        from tests._fake_worker import fake_run_shard
        return fake_run_shard(spec, sess, manager, items, gather_cfg,
                              max_recycles, recovery, on_recovery,
                              on_progress=on_progress)
    return S._collect_shard(sess, manager, items, gather_cfg,
                            max_recycles=max_recycles, recovery=recovery,
                            on_recovery=on_recovery, on_progress=on_progress,
                            label=f"worker{spec['worker_id']}")


WARMUP_URL = "https://www.ozon.ru/"
WARMUP_WAIT_MS = 4000

PROGRESS_EVERY = 5  # каждые N товаров воркер шлёт частичный payload (защита от потери при terminate)


def _warmup_session(sess, label: str = "warmup") -> dict:
    """Прогрев сессии: переход на главную ozon.ru ДО сбора товаров.

    Зачем: Ozon формирует антибот-куки/решает JS-челлендж при первом
    «человеческом» заходе на главную. Начало сбора сразу с целевого URL
    (карточка/выдача) без прогрева расценивается как подозрительное
    поведение и провоцирует access_challenge. Свежие куки после главной
    снимают блок (доказано экспериментами 2026-08-17).

    Безопасно: challenge на главной → логируем и продолжаем (не фейлим
    воркер — решает уже сам _collect_shard по товарам).
    """
    from .collect import detect_challenge
    t0 = time.monotonic()
    result = {"url": WARMUP_URL, "challenge": False, "elapsed_s": 0.0}
    try:
        sess.goto(WARMUP_URL, wait_ms=WARMUP_WAIT_MS)
        sig = detect_challenge(sess.page)
        if sig is not None:
            result["challenge"] = True
            result["sig"] = str(sig)
            log.warning(f"[{label}] challenge на главной: {sig}")
        else:
            log.info(f"[{label}] главная открыта, сессия прогрета")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        log.warning(f"[{label}] warmup warn: {result['error']}")
    result["elapsed_s"] = round(time.monotonic() - t0, 2)
    return result


def _stop_worker_browser(spec: dict) -> int:
    """Гашение Chromium воркера по его профилю/порту (best-effort).

    Используется в finally воркера и в родительском watchdog: чтобы браузер
    воркера не висел после смерти воркера/демона. Возвращает число
    остановленных процессов (0 — если браузера не было).
    """
    try:
        records = [P.ProcessRecord(f"worker{spec['worker_id']}-stop", ident)
                   for ident in P.browser_processes(
                       spec["cdp_url"],
                       profile_token=spec["profile_token"])]
        if not records:
            return 0
        manager = BrowserProcessManager(cdp_url=spec["cdp_url"],
                                        profile_token=spec["profile_token"])
        manager.force_stop(records)
        return len(records)
    except Exception as e:
        log.warning(f"worker{spec['worker_id']} stop browser: "
                    f"{type(e).__name__}: {e}")
        return 0


def worker_main(spec: dict, task: dict, conn, p2w_conn):
    """Точка входа воркер-процесса: свой браузер, свой шард, payload → pipe.

    Весь worker_main в try/except (правка 5): любое исключение → payload с
    fatal_error + частичные processed/failed → exit 0. В finally — остановка
    СВОЕГО браузера (force_stop по зарегистрированным identity) и close Session.
    """
    # Воркеры НЕ должны видеть консольный Ctrl+C (Windows CTRL_C_EVENT идёт всей
    # console-group: демон + spawn-воркеры). Иначе KeyboardInterrupt рвёт finally
    # воркера и _stop_worker_browser не доезжает → осиротевший Chromium (порты
    # 922x). Глушим Ctrl+C в воркере КАК МОЖНО РАНЬШЕ, до браузера: остановкой
    # управляет только оркестратор (stop_event → _terminate_worker → _cleanup_worker
    # в родителе). Паттерн из run_tests_guarded.py.
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
        except Exception:
            pass
    k = spec["worker_id"]
    payload = _empty_payload(k)
    sess = None
    manager = None
    _stop_event = threading.Event()
    _wd = None
    try:
        # Windows-консоль по умолчанию cp1251: юникодные символы (→ и др.)
        # в print падают с UnicodeEncodeError и роняют воркер. Переключаем
        # stdout/stderr на UTF-8 с безопасным fallback.
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass
        worker_log = task.get("worker_log") or spec["log_path"]
        # Append в общий лог демона (тот же path, что передал orchestrator);
        # ротацию выполняет только оркестратор-владелец файла.
        log.attach_file(worker_log, mode="a")
        log.set_context(f"worker={k}")
        os.environ["OZON_WORKER_ID"] = str(k)
        payload["started"] = _now()

        # Родительский watchdog (2026-09-02): если ОРКЕСТРАТОР (родитель)
        # принудительно убит (kill -9 / Stop-Process -Force), воркер обязан
        # завершиться сам, иначе остаётся сиротой: держит свой Chromium
        # (порт/профиль), пишет в wd_out.log и вообще живёт вечно. Следим за
        # живостью родителя; при его смерти — жёсткий os._exit(2) (браузер
        # потом уберёт preflight-очистка orphaned_worker_browser_processes).
        parent_pid = os.getppid()

        def _parent_watchdog():
            check_s = max(2.0, float(task.get("parent_watchdog_s") or 5.0))
            while not _stop_event.is_set():
                if not P.pid_alive(parent_pid):
                    # Демон умер: сначала гасим СВОЙ Chromium (иначе осиротевший
                    # браузер воркера будет висеть до preflight-очистки),
                    # затем завершаем воркер. finally здесь не выполнится.
                    log.error("parent (orchestrator) died — killing own "
                              "browser and exiting")
                    _stop_worker_browser(spec)
                    os._exit(2)
                time.sleep(check_s)

        _wd = threading.Thread(target=_parent_watchdog, daemon=True,
                               name=f"parent-wd-{k}")
        _wd.start()
        _send(conn, {"t": "start", "worker": k, "pid": os.getpid(),
                     "profile": spec["profile_token"], "port": spec["port"]})

        sess = None
        manager = None
        from .collect import set_scroll_jitter_seed

        # Heartbeat оркестратору: перехватываем collect._touch_watchdog и
        # шлём {"t":"hb"} в pipe (throttled). Родитель по простою heartbeat
        # terminate'ит воркера, зависшего на CDP-вызове. Таймаут heartbeat
        # не зависит от скорости сбора отзывов одного товара (см. WORKER_STALL_S).
        hb_state = {"last": 0.0}

        def _hb():
            now = time.monotonic()
            if now - hb_state["last"] < 15.0:
                return
            hb_state["last"] = now
            _send(conn, {"t": "hb", "worker": k})

        from .collect import set_heartbeat_hook
        set_heartbeat_hook(_hb)

        warmup = None

        gather_cfg = dict(task.get("gather_cfg") or {})
        max_recycles = int(task.get("max_recycles", 2))
        recovery = payload["recovery_metrics"]
        recovery["canary_url"] = S.resolve_canary_url(
            task.get("canary_url"))[0]
        tb = task.get("session_budget")
        if tb:
            recovery["session_budget"] = tb
            recovery.setdefault("session_started_ts", time.time())

        def on_recovery(ev: dict):
            # recovery-метрики накапливает сам _collect_shard (в payload);
            # сюда — только телеметрия по pipe родителю.
            _send(conn, {"t": "recovery", "worker": k, **ev})

        # Суммарный результат воркера (по всем пачкам): сохраняем отдельно,
        # т.к. payload наполняется по пачкам, а итоговый {"t":"done"} уходит
        # один раз в конце.
        agg_products: list[dict] = []
        agg_processed: list[int] = []
        agg_failed: list[int] = []
        num_batches = 0

        def _recv_batch(recv_conn):
            """Дождаться следующей пачки от оркестратора.

            Блокирующий recv между пачками (Windows: poll на pipe не работает).
            При обрыве канала (родитель умер) — EOFError/BrokenPipeError →
            трактуем как конец работы (graceful выход).
            """
            try:
                msg = recv_conn.recv()
            except (EOFError, OSError, ValueError):
                return None
            if not isinstance(msg, dict):
                return None
            if msg.get("t") != "next" or not msg.get("batch"):
                return None
            return list(msg["batch"])

        # Первая пачка: ждём назначения товара.
        batch = _recv_batch(p2w_conn)
        # Ленивый запуск: браузер НЕ открываем до реального cache-miss. Пачка,
        # целиком состоящая из кэш-hit товаров, собирается run_gather(sess=None)
        # без запуска Chromium: не тратим warmup и не гоняем окно впустую.
        # Воркеры без товаров выйдут без запуска Chrome.
        warmup = None

        def _ensure_session():
            """Поднять браузер воркера + прогреть сессию."""
            # Аренда владельца ДО handshake: браузер мог быть доставлен
            # живым (adoption) под старой мёртвой арендой — чужой preflight
            # в это окно убил бы принятый браузер (ревью 2026-11-09).
            # launch тоже пишет аренду, дубль безвреден (тот же pid).
            # Firefox аренду не использует (chromium-профиль не его) —
            # не оставляем ему чужую висящую аренду.
            if spec.get("kind") != "firefox":
                P.write_owner_lease(spec["profile_path"], pid=os.getpid(),
                                    port=spec["port"],
                                    profile_token=spec["profile_token"])
            s = _make_session(spec)
            m = _make_manager(spec)
            set_scroll_jitter_seed(task.get("scroll_jitter_seed"))
            wu = _warmup_session(s, label=f"worker{k}")
            _send(conn, {"t": "warmup", "worker": k, **wu})
            return s, m

        def _report(payload):
            _send(conn, {"t": "progress", "worker": k, "payload": payload})

        shard_cfg = {"max_recycles": max_recycles, "recovery": recovery,
                     "on_recovery": on_recovery}

        while batch is not None:
            num_batches += 1
            items = list(batch)

            # Ленивый запуск: cache-hit'ы отдаём с диска, браузер поднимаем
            # только дойдя до первого cache-miss — хоть он и в середине пачки.
            # Раньше браузер поднимался заранее (на всю пачку, если в ней есть
            # хоть один miss), прогревался и ждал первого живого товара.
            results, sess, manager = _process_batch(
                spec, items, gather_cfg, sess, manager,
                ensure_session=_ensure_session, report=_report,
                shard_cfg=shard_cfg)

            agg_products.extend(
                {"index": idx, "result": res}
                for (idx, _pid, _url), res in zip(items, results))
            agg_processed.extend(idx for idx, _p, _u in items)
            agg_failed.extend(
                idx for (idx, _p, _u), res in zip(items, results)
                if not res["ok"])
            _send(conn, {
                "t": "done_batch", "worker": k,
                "payload": {
                    "products": [
                        {"index": idx, "result": res}
                        for (idx, _pid, _url), res in zip(items, results)],
                    "processed_indexes": list(agg_processed),
                    "failed_indexes": list(agg_failed),
                }})
            batch = _recv_batch(p2w_conn)

        payload["products"] = agg_products
        payload["processed_indexes"] = agg_processed
        payload["failed_indexes"] = agg_failed
        payload["num_batches"] = num_batches
        payload["rss_bytes"] = P.rss_bytes(os.getpid())
    except Exception as e:
        payload["fatal_error"] = f"{type(e).__name__}: {e}"
        log.error(f"fatal: {payload['fatal_error']}")
    finally:
        try:
            if sess is not None:
                sess.close()
        except Exception:
            pass
        if manager is not None:
            stopped = _stop_worker_browser(spec)
            if stopped:
                payload["browser_stopped"] = True
        payload["finished"] = _now()
        try:
            _stop_event.set()
        except Exception:
            pass
        _send(conn, {"t": "done", "worker": k, "payload": payload})
        try:
            conn.close()
        except Exception:
            pass
        try:
            p2w_conn.close()
        except Exception:
            pass
    return 0


# ---- Parent: merge, cleanup, collect_parallel ------------------------------

def _rebuild_shards(owner: dict, pid_by_idx: dict, url_by_idx: dict,
                    items: list, parallel: int) -> list[list[tuple]]:
    """Собрать совместимый shards из актуального owner (полные кортежи).

    Формат прежний: shards[k-1] = [(idx,pid,url), ...]. Индексы, не выданные
    ни одному воркеру (динамическая раздача их не успела раздать — e.g.
    graceful stop), остаются в статически-назначенном шарде (owner fallback),
    чтобы merge корректно пометил их как "terminated/failed".
    """
    shards: list[list[tuple]] = [[] for _ in range(parallel)]
    seen: set[int] = set()
    for idx in sorted(owner):
        w_k = int(owner[idx])
        shards[w_k - 1].append((idx, pid_by_idx.get(idx), url_by_idx.get(idx)))
        seen.add(idx)
    # fallback-владелец для невыданных индексов (тот же round-robin, что в
    # shard_items) — только для подсчёта owner в merge.
    for idx, pid, url in items:
        if idx in seen:
            continue
        shards[idx % parallel].append((idx, pid, url))
    return shards


def merge_shard_results(payloads: list[dict], shards: list[list[tuple]]) -> list[dict]:
    """Слияние по index → products строго в исходном порядке (всего len(shards)*N).

    Товар без результата → ok=False, error="worker{k} terminated/failed".
    Результат run_gather вставляется как есть (тот же формат, что в collect_search).

    pid у потерянного товара НЕ теряется: он уже известен merge'у из шарда
    ((idx,pid,url), см. _rebuild_shards), поэтому подставляется настоящий pid,
    а утрата помечается lost=True. Раньше тут был pid=None, и товар выпадал
    из discovered_pids в key_daemon.extract_discovered_pids → не попадал ни в
    done, ни в pending (split_done_pending) → исчезал из системы навсегда.
    ok остаётся False, поэтому в ok_pids/valid такие записи не проходят.
    """
    by_index = {}
    for pl in payloads:
        for entry in pl.get("products", []) or []:
            by_index[int(entry["index"])] = entry["result"]
    owner = {}
    pid_by_idx = {}
    for k, shard in enumerate(shards, 1):
        for idx, pid, _u in shard:
            owner[int(idx)] = k
            pid_by_idx[int(idx)] = pid
    total = sum(len(s) for s in shards)
    out = []
    for i in range(total):
        if i in by_index:
            out.append(by_index[i])
        else:
            out.append({"pid": pid_by_idx.get(i), "ok": False, "lost": True,
                        "error": f"worker{owner.get(i, '?')} terminated/failed"})
    return out


def _cleanup_worker(wctx: WorkerContext):
    """Аварийная зачистка после hang/terminate: по ПОСЛЕДНЕМУ валидному snapshot.

    Не «любой Chromium на порту» — только matching (порт, профиль воркера).
    """
    spec = wctx.spec
    try:
        records = [P.ProcessRecord(f"worker{spec['worker_id']}-cleanup", ident)
                   for ident in P.browser_processes(
                       spec["cdp_url"],
                       profile_token=spec["profile_token"])]
        if records:
            manager = BrowserProcessManager(cdp_url=spec["cdp_url"],
                                            profile_token=spec["profile_token"])
            manager.force_stop(records)
            log.warning(f"worker{spec['worker_id']} cleanup: "
                        f"остановлено {len(records)} процесса(ов)")
    except Exception as e:
        log.warning(f"worker{spec['worker_id']} cleanup: "
                    f"{type(e).__name__}: {e}")
    for _c in (wctx.conn, wctx.p2w_conn):
        try:
            if _c is not None:
                _c.close()
        except Exception:
            pass


def _storm_summary(wctxs, payloads, parallel: int) -> dict | None:
    """Сводка challenge-storm'ов по payload воркеров (или None, если шторма нет).

    Воркер, упёршийся в персистентный access_challenge, останавливает свой
    шард и кладёт recovery_metrics.challenge_storm. Родитель собирает сводку:
    какие воркеры штормили и был ли шторм у ВСЕХ (all_workers — признак того,
    что Ozon душит аккаунт/IP целиком → полный cooldown).
    """
    storms = []
    storm_rms = []
    for w, pl in zip(wctxs, payloads):
        rm = pl.get("recovery_metrics") or {}
        if rm.get("challenge_storm"):
            storm_rms.append(rm)
            storms.append({"worker": w.worker_idx,
                           **rm["challenge_storm"]})
    if not storms:
        return None
    # soft/hard (ревизия 2.9): SESSION_EXHAUSTED = сессия устала от серии
    # проверок (soft, короткая пауза), SESSION_BLOCKED (в т.ч. отказ origin,
    # не снятый лестницей) = жёсткая блокировка (hard, полный cooldown). Если
    # воркеры разного типа — шторм считаем hard (жалко ошибаться в сторону
    # «мягко»).
    #
    # ВАЖНО (ревизия 2.9): block_kind/block_kind_reason берём ТОЛЬКО из
    # штормивших воркеров. recovery.block_kind ставится любому воркеру, у
    # которого прогон упёрся в терминальный session-блок, даже без
    # challenge_storm; раньше такой «чистый» воркер с block_kind=hard
    # переводил в hard soft-шторм соседа. Тихий воркер не влияет на эскалацию.
    kinds = {str(rm.get("block_kind") or "hard") for rm in storm_rms}
    block_kind = "soft" if kinds == {"soft"} else "hard"
    return {
        "reason": "challenge_storm",
        "block_kind": block_kind,
        "block_reasons": sorted({str(rm["block_kind_reason"])
                                 for rm in storm_rms
                                 if rm.get("block_kind_reason")}),
        "workers_stormed": [s["worker"] for s in storms],
        "all_workers": len(storms) == parallel,
        **storms[0],
    }


# ---- Оркестрация воркеров: перезапуск упавших ---------------------------------

def _finalize_worker(wctx: WorkerContext):
    """Зафиксировать exitcode завершённого/остановленного воркера.

    Ждём и reader-поток (не только процесс): он асинхронно наполняет
    wctx.payload/done, и без join payload мог бы прочитаться неполным
    (гонка: процесс уже умер, а финальный progress/done ещё не обработан).
    """
    try:
        wctx.proc.join(2)
        wctx.exitcode = wctx.proc.exitcode
    except Exception:
        pass
    if wctx.reader is not None:
        try:
            wctx.reader.join(3.0)
        except Exception:
            pass


def _needs_restart(wctx: WorkerContext, max_restarts: int,
                   dynamic: bool = False,
                   owner: dict | None = None,
                   completed_indexes: set | None = None,
                   shared_items=None) -> bool:
    """Нужен ли перезапуск воркера (аварийная гибель без доведения работы).

    Перезапускаем при реальном сбое (процесс умер без done, либо done
    с fatal_error/ненулевым exitcode) И когда осталась незавершённая работа.
    Дополнительно перезапускаем при challenge_storm (нерешённый пазл) и при
    browser_dead (браузер воркера мёртв): в обоих случаях воркер завершился
    формально "чисто", но не довёл своё — открываем новое окно и пробуем дальше
    (ADR §14.6: частичный шторм ≠ бан всей кампании; лимит max_restarts
    ограничивает зомби-цикл).
    Истечение времени (deadline_halted) и успешное/ожидаемое завершение не
    перезапускаем.

    dynamic=True: критерий невыполненной работы — есть ещё индексы, назначенные
    воркеру и не завершённые (owner/completed_indexes), либо очередь пачек
    не пуста. legacy: pending_indexes воркера (статический шард).
    """
    if max_restarts <= 0 or wctx.deadline_halted:
        return False
    if wctx.restarts >= max_restarts:
        return False
    if dynamic:
        unfinished = bool(shared_items)
        if owner:
            unfinished = unfinished or any(
                w_k == wctx.worker_idx and idx not in completed_indexes
                for idx, w_k in owner.items())
        if not unfinished:
            return False
    elif not wctx.pending_indexes:
        return False
    finished = wctx.done or not wctx.proc.is_alive()
    if not finished:
        return False
    pl = wctx.payload or {}
    if pl.get("fatal_error"):
        return True
    if wctx.exitcode not in (None, 0):
        return True
    # Пазл-шторм (recovery_metrics.challenge_storm): воркер завершился "чисто"
    # (exitcode 0, без fatal_error), НО сдвинулся из-за нерешённого пазла и
    # оставил незавершённую работу. Это НЕ штатное завершение: открываем новое
    # окно и пробуем дальше (свежий браузер = шанс снять блок). Лимит
    # max_restarts ограничивает число таких перезапусков (защита от зомби-цикла
    # при IP-бане Ozon).
    rm = pl.get("recovery_metrics") or {}
    if rm.get("challenge_storm"):
        return True
    # Браузер воркера МЁРТВ (kill/crash): search.py помечает товар
    # browser_dead и НЕ тратит recycle (пересоздавать нечего), поэтому
    # воркер выходит "чисто" (exitcode 0, без fatal_error) и без этой
    # проверки до конца прогона жёг бы бюджет на каждом товаре, сыпя
    # SESSION_EXHAUSTED. Свежий браузер на респауне — единственное лечение.
    if rm.get("browser_dead"):
        return True
    return False


def _worker_task(items, gather_cfg: dict, max_recycles: int,
                 session_budget: dict | None, worker_log: str | None,
                 canary_url: str | None = None) -> dict:
    """Task для spawn-воркера (spawn-picklable, без сокетов).

    Единая точка сборки: обычный спавн и респавн после падения должны передавать
    ОДИНАКОВЫЙ набор (иначе замена воркера незаметно теряет, например, canary_url
    и начинает проверять главную вместо сущности кампании).
    """
    return {
        "items": list(items or []),
        "gather_cfg": gather_cfg,
        "max_recycles": max_recycles,
        "scroll_jitter_seed": gather_cfg.get("scroll_jitter_seed"),
        "session_budget": dict(session_budget) if session_budget else None,
        "worker_log": worker_log,
        "canary_url": canary_url,
    }


def _respawn(wctx: WorkerContext, pending: list[int],
             items_by_index: dict, gather_cfg: dict,
             max_recycles: int, session_budget: dict | None,
             on_event=None, worker_log: str | None = None,
             dynamic: bool = False,
             canary_url: str | None = None) -> WorkerContext:
    """Поднять воркера-замену на том же worker_id с недоделанными индексами.

    Сначала чистим зависший Chromium воркера (fresh браузер), ждём
    освобождения порта, затем спавним заново с тем же spec и перенесённым
    подмножеством items.

    dynamic=True (динамическая пакетная раздача): items НЕ передаём статически
    (их уже вернули в shared_items через requeue) — новая выдача пачек
    управляется очередью оркестратора, поэтому worker стартует с items=[].
    """
    k = wctx.worker_idx
    spec = dict(wctx.spec)
    if not dynamic:
        log.warning(f"worker{k}: перезапуск {wctx.restarts + 1}, "
                    f"недоделано {len(pending)} из "
                    f"{len(wctx.shard_indexes)} товаров")
    _cleanup_worker(wctx)
    _wait_port_free(spec)
    if dynamic:
        items2 = []
    else:
        pend_set = set(pending)
        items2 = [(_idx, pid, url)
                  for _idx, (pid, url) in items_by_index.items()
                  if _idx in pend_set]
    task2 = _worker_task(items2, gather_cfg, max_recycles, session_budget,
                         worker_log, canary_url=canary_url)
    # Рестарт после общего сбоя не должен бить синхронно с остальными рестартами.
    _stagger = _start_stagger_s(int(spec.get("worker_id") or 1) + 1000)
    if _stagger > 0:
        log.info(f"[parallel] stagger рестарта worker-{spec.get('worker_id')}: "
                 f"{_stagger:.2f}с")
        time.sleep(_stagger)
    new = _spawn_ctx(spec, task2, on_event=on_event)
    new.restarts = wctx.restarts + 1
    new.shard_indexes = list(pending)
    new.pending_indexes = list(pending)
    return new


def _wait_port_free(spec: dict, timeout_s: float = 8.0):
    """Дождаться освобождения CDP-порта воркера после зачистки браузера."""
    if not spec.get("port"):
        return          # firefox-спек: порта/CDP нет вовсе (port=None)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _port_free(spec["port"]) and not cdp.browser_alive(
                spec["cdp_url"]):
            return
        time.sleep(0.2)


def _terminate_worker(wctx: WorkerContext, reason: str,
                      finalize: bool = True):
    """Принудительно завершить воркера: terminate + join + cleanup.

    Общий код для stall (нет heartbeat) и hang (истёк дедлайн). Если
    finalize=True — дополнительно фиксируем exitcode и ждём reader-поток
    (нужно перед включением воркера в finalized / перезапуском).
    """
    log.warning(f"worker{wctx.worker_idx} {reason}: terminate()")
    wctx.proc.terminate()
    wctx.proc.join(2)
    if finalize:
        _finalize_worker(wctx)
    _cleanup_worker(wctx)


def _unfinished_crashed(wctx: WorkerContext, completed_indexes,
                        owner) -> list[int]:
    """Индексы, назначенные упавшему воркеру и не завершённые (для respawn).

    owner[idx]==k и idx∉completed_indexes. Возвращает отсортированный список.
    Если динамическая раздача неактивна (owner пуст) — fallback на
    pending_indexes воркера (legacy-семантика шарда).
    """
    if owner:
        return sorted(idx for idx, w_k in owner.items()
                      if w_k == wctx.worker_idx
                      and idx not in completed_indexes)
    return list(wctx.pending_indexes or [])


def _attach_scheduler_metrics(wctxs, requeue_metrics: dict) -> None:
    """Прикрепить к воркерам агрегированную телеметрию пакетной раздачи.

    Хранится на самом WorkerContext (wctx.scheduler_metrics), чтобы не менять
    возврат _await_workers (сигнатура остаётся прежней для unit-тестов).
    _worker_summary затем включает их в combined.parallel.
    """
    for wctx in wctxs:
        wctx.scheduler_metrics = {
            "requeue_count": int(requeue_metrics.get("count") or 0),
            "p2w_broken": int(requeue_metrics.get("p2w_broken") or 0),
        }


def _process_batch(spec, items, gather_cfg, sess, manager, *,
                   ensure_session, report, shard_cfg):
    """Обработать пачку с ленивым стартом браузера. Возвращает (results, sess, manager).

    cache-hit префикс пачки отдаётся с диска (без Chromium), браузер поднимается
    на первом cache-miss и дальше пачка идёт через _run_shard. Если браузер уже
    поднят — вся пачка в _run_shard без разбора.

    ensure_session() -> (sess, manager) — поднимает браузер + прогрев.
    report(payload) — отправка progress в оркестратор (уже с глобальными
    индексами: индексы prefix не должны сдвинуться после split пачки).
    shard_cfg — {max_recycles, recovery, on_recovery} для _run_shard.

    results идут строго в порядке items, независимо от того, где разрезалась
    пачка: без этого merge_shard_results/QA получают неверные индексы.

    При batch_size=1 отчёт _progress не срабатывает ВСЕГДА (n=1 < PROGRESS_EVERY)
    — и это нормально, а не регресс наблюдаемости: отчёт нужен для частичного
    снимка ВНУТРИ выдачи, а при выдаче в один товар частичного состояния нет.
    Per-item payload идёт через done_batch после каждого товара (reader кладёт
    его в wctx.payload/acc_products), поэтому демон по-прежнему видит живой
    сбор, а потеря при terminate ограничена одним товаром.
    """
    head = (0 if sess is not None
            else _first_browser_index(items, gather_cfg))

    def _progress(products, offset, n):
        if n % PROGRESS_EVERY != 0:
            return
        sub = items[offset:offset + len(products)]
        report({
            "products": [
                {"index": idx, "result": res}
                for (idx, _pid, _url), res in zip(sub, products)],
            "processed_indexes": [idx for (idx, _p, _u) in sub],
            "failed_indexes": [
                idx for (idx, _p, _u), res in zip(sub, products)
                if not res["ok"]],
        })

    results = []
    if head:
        results.extend(_collect_batch_without_browser(
            items[:head], gather_cfg, on_progress=lambda p, n, t: _progress(p, 0, n)))
    if head < len(items):
        if sess is None:
            log.info(f"worker{spec['worker_id']}: стартую браузер (cache-miss "
                     f"на товаре {head + 1} из {len(items)})")
            sess, manager = ensure_session()
        results.extend(_run_shard(
            spec, sess, manager, items[head:], gather_cfg,
            max_recycles=shard_cfg["max_recycles"],
            recovery=shard_cfg["recovery"],
            on_recovery=shard_cfg["on_recovery"],
            on_progress=lambda p, n, t: _progress(p, head, n)))
    return results, sess, manager


def _resolve_batch_size(gather_cfg: dict) -> int:
    """Размер выдачи из gather_cfg["batch_size"], с защитой от вырожденных значений.

    Кламп вниз единицей — НЕ косметика: при size<=0 _take_batch не набирает ни
    одного товара даже при непустой очереди, оркестратор шлёт воркеру
    {"t": "none"}, воркер тихо выходит, и весь прогон уходит в «0 собрано»
    без единой ошибки. Мусор в значении (строка/None) — тоже дефолт, а не
    ValueError посреди прогона.
    """
    raw = (gather_cfg or {}).get("batch_size", DEFAULT_BATCH_SIZE)
    try:
        size = int(raw)
    except (TypeError, ValueError):
        log.warning(f"[parallel] batch_size={raw!r} не число → "
                    f"{DEFAULT_BATCH_SIZE}")
        return DEFAULT_BATCH_SIZE
    if size < 1:
        log.warning(f"[parallel] batch_size={size} < 1 → 1 (иначе воркеры "
                    f"получат 'none' при непустой очереди и прогон соберёт 0)")
        return 1
    return size


def _take_batch(queue, size: int, prefer_miss: bool, gather_cfg: dict,
                worker_idx: int, owner: dict, verdicts: dict) -> list:
    """Достать из очереди пачку, отдавая приоритет нужному типу товаров.

    Воркер с браузером (prefer_miss=True) получает первыми cache-miss, воркер
    без браузера — cache-hit'ы, чтобы отработать вообще без Chromium. Причина:
    FIFO раздавал каждому воркеру по одному miss'у, и при 8 воркерах
    поднимались 8 окон ради 8 товаров из кэша (в бою 26.09: 6 missing +
    2 incomplete на 40 товаров → 8 браузеров вместо двух). Если товаров
    приоритетного типа не осталось — отдаём что есть, воркер не должен
    простаивать вхолостую.

    Недобор НЕ добираем товарами другого типа: короткая выдача дешевле, чем
    гонять воркер с браузером по кэшу (лишний round-trip и потеря ленивости —
    hit'ы предназначены воркерам без Chromium).

    size=1 (дефолт) — та же логика без веток: в picked попадает ПЕРВЫЙ товар
    приоритетного типа, порядок rest сохраняется, fallback срабатывает только
    когда товаров приоритетного типа в очереди не осталось вовсе.

    verdicts — кэш «нужен ли браузер» по pid на прогон: cache_check читает
    JSON с диска, а раздач на прогон столько же, сколько выдач.
    """
    def _needs(item) -> bool:
        pid = item[1]
        if pid not in verdicts:
            verdicts[pid] = _item_needs_browser(item, gather_cfg)
        return verdicts[pid]

    picked: list = []
    rest: list = []
    for item in queue:
        if len(picked) >= size:
            rest.append(item)
        elif _needs(item) == prefer_miss:
            picked.append(item)
        else:
            rest.append(item)
    if not picked:
        picked = rest[:size]
        rest = rest[size:]
    for item in picked:
        owner[int(item[0])] = worker_idx
    queue.clear()
    queue.extend(rest)
    return picked


def _await_workers(wctxs: list, deadline: float,
                   max_worker_restarts: int, items_by_index: dict,
                   gather_cfg: dict, max_recycles: int,
                   session_budget: dict | None, on_event=None,
                   worker_log: str | None = None,
                   poll: float = 0.3,
                   stop_event=None, force_event=None,
                   shared_items=None, completed_indexes=None,
                   owner=None, pid_by_idx=None, url_by_idx=None,
                   batch_size: int = DEFAULT_BATCH_SIZE,
                   canary_url: str | None = None) -> list:
    """Дождаться завершения воркеров, перезапуская аварийно упавших.

    Динамическая пакетная раздача: когда переданы shared_items/completed_indexes/
    owner (из collect_parallel), воркеры получают пачки через parent→worker pipe
    по запросу task_request; завершённые индексы (done_batch) идут в
    completed_indexes (immutable guard, повторно не выдаются). Если НЕ переданы
    (legacy-режим, используется unit-тестами оркестрации с фейковыми воркерами
    без живых pipe) — поведение прежнее (просто join по дедлайну + респавн).

    max_worker_restarts=0 → исходное поведение (join по очереди, terminate
    зависших по дедлайну, без перезапуска). >0 → оркестрационный цикл с
    перезапуском воркеров, упавших без доведения пачки.

    stop_event (threading.Event, от демона) — кооперативный запрос остановки
    (Ctrl+C): прекращаем выдачу новых пачек/респавн и сворачиваем живых воркеров
    (_terminate_worker → _cleanup_worker), НЕ помечая остановку как CRASH.
    Текущая пачка/товар воркера теряется и на resume добирается заново (цена
    быстрого, детерминированного стопа — Вариант A).

    force_event (2-й Ctrl+C) — приоритет над stop_event: немедленно
    _terminate_worker для ВСЕХ активных воркеров, без респавна и без выдачи
    новых пачек. Сам os._exit() НЕ зовём — это ответственность демона.
    """
    dynamic = (
        shared_items is not None and completed_indexes is not None
        and owner is not None and pid_by_idx is not None
        and url_by_idx is not None)

    # Кэш вердиктов «нужен ли браузер» по pid на прогон (см. _take_batch).
    verdicts: dict = {}

    # Все воркеры, которых оркестратор видел за прогон, включая респавны:
    # _drain_completed обязан обходить их, а не только исходный список — иначе
    # индексы, завершённые воркером-ЗАМЕНОЙ, не попадают в completed_indexes и
    # товар выдаётся повторно (тот же товар собирается дважды).
    all_wctxs: list = list(wctxs)

    def _send_to_worker(wctx, msg: dict) -> bool:
        """Отправить пачку/команду воркеру через parent→worker pipe.

        Возвращает False при обрыве канала (воркер уже мёртв) — в этом случае
        выданная пачка должна быть возвращена в очередь.
        """
        if wctx.p2w_conn is None:
            return False
        try:
            wctx.p2w_conn.send(msg)
            return True
        except (BrokenPipeError, EOFError, OSError, ValueError):
            return False

    def _dispatch(wctx):
        """Выдать воркеру следующую пачку (или none), если он её ждёт."""
        if not dynamic or wctx.p2w_conn is None:
            return
        if not wctx.proc.is_alive() or wctx.done:
            return
        if not wctx.waiting_for_batch:
            return
        # stop/force → больше пачек не выдаём, воркеру none.
        if (force_event is not None and force_event.is_set()) or \
                (stop_event is not None and stop_event.is_set()):
            wctx.waiting_for_batch = False
            _send_to_worker(wctx, {"t": "none"})
            return
        batch = _take_batch(shared_items, batch_size,
                            prefer_miss=wctx.browsers_started > 0,
                            gather_cfg=gather_cfg, worker_idx=wctx.worker_idx,
                            owner=owner, verdicts=verdicts)
        if batch:
            wctx.waiting_for_batch = False
            if not _send_to_worker(wctx, {"t": "next", "batch": batch}):
                # Воркер умер до получения выдачи → вернуть её в очередь.
                # owner тоже чистим: индексы уже не в руках этого воркера, и
                # _requeue_unfinished вернул бы их ВТОРЫМ разом — товар оказался
                # бы в очереди дважды и собрался бы дважды.
                for item in batch:
                    owner.pop(int(item[0]), None)
                shared_items.extendleft(reversed(batch))
                requeue_metrics["p2w_broken"] = (
                    requeue_metrics.get("p2w_broken", 0) + 1)
        else:
            wctx.waiting_for_batch = False
            _send_to_worker(wctx, {"t": "none"})

    def _drain_completed():
        """Перелить подтверждённые done_batch индексы в completed_indexes."""
        if not dynamic:
            return
        for wctx in all_wctxs:
            with wctx.batch_lock:
                if not wctx.batch_completed:
                    continue
                completed_indexes.update(wctx.batch_completed)
                wctx.batch_completed = []

    def _requeue_unfinished(crashed):
        """Вернуть недоделанные выданные пачки упавшего воркера в очередь.

        Только индексы, назначенные этому воркеру из owner и НЕ завершённые
        (completed_indexes — immutable guard). Пачка возвращается целиком в начало
        очереди, исходный порядок внутри сохранён (FIFO-front приоритет).
        """
        if not dynamic:
            return 0
        pend = [idx for idx, w_k in owner.items()
                if w_k == crashed.worker_idx and idx not in completed_indexes]
        if not pend:
            return 0
        pend_sorted = sorted(pend)
        requeue = [(idx, pid_by_idx.get(idx), url_by_idx.get(idx))
                   for idx in pend_sorted]
        shared_items.extendleft(reversed(requeue))
        requeue_metrics["count"] += len(requeue)
        return len(requeue)

    requeue_metrics = {"count": 0, "p2w_broken": 0}

    def _force_stop():
        log.warning("stop: force (Ctrl+C ×2) — принудительно завершаю всех "
                    "воркеров (без респавна)")
        # all_wctxs, not wctxs: respawned replacements must die too, otherwise
        # a live replacement keeps running until the daemon's os._exit
        # (audit 2026-10-19).
        for wctx in list(all_wctxs):
            if wctx.proc.is_alive() and not wctx.done:
                _terminate_worker(wctx, "force: 2-й Ctrl+C, emergency stop")
            else:
                _finalize_worker(wctx)

    if max_worker_restarts <= 0:
        while True:
            if force_event is not None and force_event.is_set():
                _force_stop()
                break
            if stop_event is not None and stop_event.is_set():
                for wctx in wctxs:
                    if wctx.proc.is_alive() and not wctx.done:
                        _terminate_worker(wctx, "stop: user interrupt (Ctrl+C)")
                break
            _drain_completed()
            for wctx in wctxs:
                _dispatch(wctx)
            now = time.monotonic()
            stalled = [wctx for wctx in wctxs
                       if wctx.proc.is_alive() and not wctx.done
                       and not wctx.waiting_for_batch
                       and now - wctx.last_activity > WORKER_STALL_S]
            if stalled:
                log.warning(f"stall: {len(stalled)} воркер(ов) без heartbeat "
                            f"(> {WORKER_STALL_S}s) — terminate")
                for wctx in stalled:
                    _terminate_worker(
                        wctx,
                        f"stall: нет heartbeat {int(now - wctx.last_activity)}s "
                        f"(> {WORKER_STALL_S}s)")
                _drain_completed()
                break
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            alive = [wctx for wctx in wctxs
                     if wctx.proc.is_alive() and not wctx.done]
            if not alive:
                break
            # Очередь пуста и все живые воркеры либо ждут пачку (на неё уже
            # отправлено none), либо завершаются: единственный случай полного
            # ожидания — воркер обрабатывает пачку. Ждём, пока все не закончат.
            time.sleep(min(poll, remain))
        for wctx in wctxs:
            if wctx.proc.is_alive() and not wctx.done:
                _terminate_worker(wctx, "hang: нет завершения к дедлайну")
        _drain_completed()
        for wctx in wctxs:
            _finalize_worker(wctx)
        _attach_scheduler_metrics(wctxs, requeue_metrics)
        return wctxs

    active = list(wctxs)
    finalized: list = []
    while active:
        if force_event is not None and force_event.is_set():
            _force_stop()
            # Last wins per worker_idx: force must cover respawned
            # replacements, while dead originals must not duplicate their
            # payloads in the report (review 2026-10-19).
            finalized = list({w.worker_idx: w for w in all_wctxs}.values())
            break
        if stop_event is not None and stop_event.is_set():
            log.warning("stop: user interrupt (Ctrl+C) — сворачиваю воркеров, "
                        f"без респавна (active={len(active)})")
            for wctx in list(active):
                if wctx.proc.is_alive() and not wctx.done:
                    _terminate_worker(wctx, "stop: user interrupt (Ctrl+C)")
                else:
                    _finalize_worker(wctx)
                finalized.append(wctx)
            _drain_completed()
            break
        _drain_completed()
        for wctx in active:
            _dispatch(wctx)
        now = time.monotonic()
        for wctx in list(active):
            finished = wctx.done or not wctx.proc.is_alive()
            if finished:
                _finalize_worker(wctx)
                finalized.append(wctx)
                active.remove(wctx)
                continue
            if (not wctx.waiting_for_batch
                    and now - wctx.last_activity > WORKER_STALL_S):
                _terminate_worker(
                    wctx,
                    f"stall: нет heartbeat {int(now - wctx.last_activity)}s "
                    f"(> {WORKER_STALL_S}s)")
                finalized.append(wctx)
                active.remove(wctx)
                continue
            if now >= deadline:
                log.warning(f"worker{wctx.worker_idx} hang/таймаут: "
                            f"terminate()")
                wctx.deadline_halted = True
                wctx.proc.terminate()
                wctx.proc.join(2)
                _cleanup_worker(wctx)
                finalized.append(wctx)
                active.remove(wctx)
        for wctx in list(finalized):
            if not _needs_restart(
                    wctx, max_worker_restarts,
                    dynamic=dynamic, owner=owner,
                    completed_indexes=completed_indexes,
                    shared_items=shared_items):
                continue
            _requeue_unfinished(wctx)
            pending = _unfinished_crashed(wctx, completed_indexes, owner)
            if dynamic:
                new = _respawn(
                    wctx, pending, items_by_index, gather_cfg,
                    max_recycles, session_budget, on_event, worker_log,
                    dynamic=dynamic, canary_url=canary_url)
            else:
                new = _respawn(
                    wctx, pending, items_by_index, gather_cfg,
                    max_recycles, session_budget, on_event, worker_log,
                    canary_url=canary_url)
            # Перенести уже собранные товары упавшего воркера в замену: иначе
            # merge широко пометит их как "terminated/failed" (потеря данных).
            # products берём из накопителя acc_products: payload упавшего
            # воркера — это последний присланный снимок (при batch_size=1 —
            # один товар), а не всё, что он собрал.
            # _merge_carried: воркер сам мог быть респавном и уже нести
            # carried_payload предыдущего преда — иначе цепочка респавнов
            # теряет самые ранние товары (ревью 2026-10-15).
            old_pl = _merge_carried(
                _with_acc_products(wctx.payload or {}, wctx), wctx)
            old_products = old_pl.get("products") or []
            if old_products:
                new.carried_payload = {
                    "products": list(old_products),
                    "processed_indexes": list(
                        old_pl.get("processed_indexes") or []),
                    "failed_indexes": list(old_pl.get("failed_indexes") or []),
                }
            active.append(new)
            all_wctxs.append(new)
            finalized.remove(wctx)
        time.sleep(poll)
    _attach_scheduler_metrics(wctxs, requeue_metrics)
    return finalized


def _release_profile_children(profile_token: str = P.PROFILE_TOKEN) -> bool:
    """Kill renderer/gpu leftovers after the main Chromium is gone.

    The singleton profile lock is held by children too; without this wait the
    next worker bootstrap can fail with "profile captured by a live instance".
    Returns True when no process holds the profile anymore.
    """
    left = P.wait_profile_released(profile_token)
    if not left:
        return True
    try:
        mains, helpers = P.profile_process_roles(profile_token)
        if mains:
            # A manual main appeared during the wait: never kill it, and drop
            # our stale marker so the next cleanup cannot claim it.
            log.error(f"profile release {profile_token!r}: чужой main "
                      f"{[m.pid for m in mains]} — не трогаю")
            _clear_launch_marker(profile_token)
            return False
        killed = P.kill_identities(helpers)
        still = P.wait_profile_released(profile_token, timeout_s=5.0)
    except Exception as e:
        log.warning(f"profile release {profile_token}: "
                    f"{type(e).__name__}: {e}")
        return False
    if still:
        log.error(f"профиль {profile_token!r} НЕ освободился после main-stop: "
                  f"{still} держат профиль")
        try:
            mains, _helpers_left = P.profile_process_roles(profile_token)
        except Exception:
            mains = []
        if mains:
            _clear_launch_marker(profile_token)
        return False
    log.info(f"профиль {profile_token!r} освобождён "
             f"(добито потомков: {len(killed)})")
    return True


def _clear_launch_marker(profile_token: str = P.PROFILE_TOKEN) -> None:
    """Drop the ownership marker after our browser is fully stopped."""
    path = os.path.join(P.profile_path(profile_token),
                        ".ozon_collector_launched")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.debug(f"launch marker {path}: {type(e).__name__}: {e}")


def _stop_main_browser(owner: str | None = None):
    """Полный останов main-браузера (main-профиль) ДО воркеров.

    chrome: manager по CDP-порту 9222 — Session.close гасит только CDP-клиент,
    лишний main-процесс ест RAM при параллельном сборе.
    firefox: driver.quit() в Session.close асинхронен, поэтому дожимаем
    процессы по токену профиля. Иначе bootstrap воркеров увидит живой main и
    пропустит сев сессии → cold worker-профиль → блок «Похоже, нет соединения».

    owner == "external" — чужой браузер пользователя: не трогаем никогда
    (ADR §14.1), даже в finally.
    """
    if owner == "external":
        log.info("main stop: browser_owner=external — чужой браузер не трогаю")
        return
    if B.browser_kind() == "firefox":
        records = [P.ProcessRecord("main-stop-ff", ident)
                   for ident in P.firefox_browser_processes(P.PROFILE_TOKEN)]
        if not records:
            return
        try:
            killed = P.kill_records(records, wait_s=5.0)
            log.info(f"main Firefox stopped ({len(killed)} процесс(ов))")
        except Exception as e:
            log.warning(f"stop main Firefox: {type(e).__name__}: {e}")
        return

    # Ищем НАШ main по профилю на всех портах, а не по «свободной базе».
    # Живой замер 2026-10-03: база сдвинулась на 9444, наш Chromium занял её,
    # и cdp_port_base() отдал 9445 — browser_processes(9445) пуст, main не
    # остановился, профиль остался занятым, и bootstrap упал на копировании
    # кук: [WinError 32] «файл занят другим процессом».
    records = []
    _by_port = P.profile_debug_ports(P.PROFILE_TOKEN)
    _marker_pid = (cdp._launch_marker_pid(P.profile_path(P.PROFILE_TOKEN))
                   if owner == "collector" else None)
    if owner == "collector" and _marker_pid is None:
        _marker_file = os.path.join(P.profile_path(P.PROFILE_TOKEN),
                                    ".ozon_collector_launched")
        if not os.path.exists(_marker_file):
            # Ownership was asserted earlier but no marker exists now. Without
            # it we cannot distinguish our main from a manual debug-port
            # Chrome; leaking the profile is safer than killing a user window.
            log.warning("main stop: marker исчез — main не трогаю")
            return
        # Marker exists but its PID is dead: our main crashed and only
        # renderer/gpu helpers remain. Skip live debug mains (manual) and let
        # the orphan branch below reap the helpers.
        log.info("main stop: marker есть, pid мёртв — чищу только helpers")
        _by_port = {}
    if _marker_pid is not None:
        # A launch marker names exactly one main. Everything else on this
        # profile is a manual browser and must survive cleanup.
        _by_port = {port: [pid for pid in pids if pid == _marker_pid]
                    for port, pids in _by_port.items()}
        _by_port = {port: pids for port, pids in _by_port.items() if pids}
    _ports = sorted(_by_port)
    for _port in _ports:
        for _pid in _by_port[_port]:
            _ident = P.process_identity(_pid)
            if _ident is not None:
                records.append(P.ProcessRecord(f"main-stop-{_port}", _ident))
    if not records:
        # A main that crashed on its own leaves no debug port, but its
        # renderer/gpu children still hold the profile singleton. Kill those
        # helpers only: a live main without CDP is the user's browser.
        try:
            mains, helpers = P.profile_process_roles(P.PROFILE_TOKEN)
            if mains:
                log.info("main stop: main без CDP — не наш или ручной, "
                         "не трогаю")
                return
            if helpers:
                killed = P.kill_identities(helpers)
                left = P.wait_profile_released(P.PROFILE_TOKEN)
                if not left:
                    _clear_launch_marker()
                    log.warning(f"main stop: осиротевшие потомки сняты "
                                f"({len(killed)}), профиль освобождён")
                else:
                    mains_now, _helpers_now = P.profile_process_roles(
                        P.PROFILE_TOKEN)
                    if mains_now:
                        _clear_launch_marker()
                    log.warning(f"main stop: профиль всё ещё держат {left}")
        except Exception as e:
            log.warning(f"main stop: очистка осиротевших потомков: "
                        f"{type(e).__name__}: {e}")
        return
    try:
        killed = P.kill_records(records, wait_s=5.0)
        if len(killed) < len(records):
            # Our main survived (AccessDenied / D-state). Keep the marker so a
            # later cleanup can retry; releasing children now would mistake the
            # surviving owner for a foreign browser and drop ownership.
            log.error(f"main Chromium: убито {len(killed)} из {len(records)}, "
                      f"маркер оставлен для повтора")
            return
        log.info(f"main Chromium stopped ({len(killed)} процесс(ов), "
                 f"порты {_ports})")
        if _release_profile_children():
            _clear_launch_marker()
    except Exception as e:
        log.warning(f"stop main Chromium: {type(e).__name__}: {e}")


def _collect_worker_payloads(wctxs: list) -> list[dict]:
    """payload'ы воркеров для merge: накопители + carried + fatal по exitcode.

    Ключ fatal_error читается через .get: payload последнего done_batch его не
    содержит (ключ есть только у _empty_payload и done-снимка), а subscript
    падал KeyError ровно на terminate-пути (deadline/stall/stop) — том самом,
    ради которого гвард и написан (аудит 2026-10-15).
    """
    payloads = []
    for wctx in wctxs:
        # Накопитель products обязателен и для штатно завершившегося воркера:
        # без него merge видел бы только последний снимок payload.
        pl = _with_acc_products(
            wctx.payload or _empty_payload(wctx.worker_idx), wctx)
        pl = _merge_carried(pl, wctx)
        if wctx.exitcode is None:
            try:
                wctx.exitcode = wctx.proc.exitcode
            except Exception:
                wctx.exitcode = None
        if (wctx.exitcode not in (0, None)
                and pl.get("fatal_error") is None
                and not wctx.deadline_halted):
            # None (процесс не сообщил код) и deadline_halted (штатный
            # terminate по дедлайну) — не fatal: classify_status иначе
            # пометил бы кампанию CRASH и сжёг transient-ретраи.
            pl["fatal_error"] = f"exit {wctx.exitcode}"
        pl["exitcode"] = wctx.exitcode
        payloads.append(pl)
    return payloads


def collect_parallel(url: str, limit: int, gather_cfg: dict, parallel: int,
                     query: str | None = None, max_recycles: int = 2,
                     join_timeout_s: float = 1800.0,
                     pids: list | None = None,
                     skip_pids: list | None = None,
                     no_bootstrap: bool = False,
                     bootstrap_post_storm_at: float | None = None,
                     session_budget: dict | None = None,
                     on_event=None,
                     max_worker_restarts: int = 0,
                     worker_log: str | None = None,
                     stop_event=None,
                     force_event=None,
                     canary_url: str | None = None,
                     skip_no_reviews: bool = True) -> dict:
    """Parallel-search: links в main-сессии → шарды → воркеры → merge.

    pids — явный список артикулов вместо докрутки выдачи: main-сессия не
    запускается, товары берутся ТОЛЬКО из списка (found = len(pids)).

    bootstrap: сев Ozon-сессии в воркеры выполняется ЗДЕСЬ (после остановки
    main-браузера), а не в cli — иначе main жив и профили не обновляются.
    bootstrap_post_storm_at — epoch последнего ФУЛЛ-шторма: в окне после него
    прогретые воркер-профили не пересеиваются (ADR §14.7, иначе испорченная
    сессия main тиражируется на весь пул).

    Возвращает combined-объект схемы 2.0 (аддитивно parallel{workers[],shards[]}).
    Ошибка preflight → RuntimeError (воркеры не запускаются).

    stop_event/force_event (от демона, threading.Event): остановка Ctrl+C.
      stop_event  во время докрутки ссылок → CollectionInterrupted(forced=False):
                  возвращаем partial combined (discovered_pids из найденных
                  ссылок, collected=0, воркеры НЕ запускаем) — демон сохранит
                  discovery и завершится INTERRUPTED.
      force_event во время докрутки → пробрасываем CollectionInterrupted(forced=True)
                  наверх: воркеры ещё не спавнились, экстренный выход выполняет
                  демон. force_event во время worker-фазы обрабатывает
                  _await_workers (terminate всех воркеров).

    canary_url — цель canary-пробы в воркерах после access-recovery. По умолчанию
      URL этой кампании (та же сущность, на которой упало), см.
      search.resolve_canary_url; переопределяется OZON_CANARY_URL.
    """
    B.resolve_browser_for_parallel()  # дефолт движка для воркеров (firefox; переопределяется env/флагом)
    parallel = int(parallel)
    skipped_no_reviews = []
    if pids:
        links = S._links_from_pids(pids)
        recovery_main = {"recycles": 0, "events": []}
    else:
        _wm_main = gather_cfg.get("window_mode")
        sess = cdp.Session(
            keep_window=None if _wm_main is None else (_wm_main == "visible"))
        recovery_main = {"recycles": 0, "events": []}
        try:
            manager_main = BrowserProcessManager(cdp_url=sess.cdp_url)
            links, skipped_no_reviews = S._collect_links_with_recovery(
                sess, manager_main, url, limit, max_recycles, recovery_main,
                stop_event=stop_event, force_event=force_event,
                skip_no_reviews=skip_no_reviews)
        except S.CollectionInterrupted as exc:
            # finally ниже всё равно гасит main-браузер и сессию; здесь только
            # решаем: graceful (вернуть partial) или force (пробросить).
            if exc.forced:
                log.warning("collection: принудительная остановка докрутки ссылок "
                            "(Ctrl+C ×2) — пробрасываю в демон (emergency)")
                raise
            log.warning("collection: докрутка ссылок прервана Ctrl+C — возвращаю "
                        f"частично найденные ссылки ({len(exc.links)})")
            discovered = [it["pid"] for it in exc.links]
            partial = {
                "schema_version": SCHEMA_VERSION,
                "collector_version": VERSION,
                "collected_at_utc": _now(),
                "search": {"url": url, "query": query},
                "found": len(exc.links),
                "limit": limit,
                "collected": 0,
                "excluded": 0,
                "discovered_pids": discovered,
                "recovery": recovery_main,
                "products": [],
                "interrupted": True,
                "parallel": {"workers": [], "shards": []},
            }
            # Tile-skipped products collected before the interrupt: the
            # partial result must carry them exactly like the normal path
            # does below (audit 2026-10-18, B6).
            if exc.skipped_no_reviews:
                partial["skipped_no_reviews"] = len(exc.skipped_no_reviews)
                partial["skipped_no_reviews_pids"] = [
                    it["pid"] for it in exc.skipped_no_reviews]
            return partial
        finally:
            try:
                sess.close()
            except Exception:
                pass
            _stop_main_browser(getattr(sess, "browser_owner", None))

    found = len(links)
    links = S._filter_skip_pids(links, skip_pids)
    selected = links[:limit]
    items = [(i, it["pid"], it["url"]) for i, it in enumerate(selected)]
    total = len(items)
    log.info(f"[search] найдено {found} товаров в выдаче, "
             f"собираю {total} через {parallel} воркер(ов)")

    if not no_bootstrap:
        kind = B.browser_kind()
        fn = (P.bootstrap_firefox_worker_profiles if kind == "firefox"
              else P.bootstrap_worker_profiles)
        bs = fn(range(1, parallel + 1),
                post_storm_at=bootstrap_post_storm_at)
        for k, st in bs.items():
            log.info(f"[parallel] bootstrap worker-{k}: {st}")

    specs = [worker_spec(k) for k in range(1, parallel + 1)]
    errs = preflight_check(specs)
    if errs:
        raise RuntimeError("preflight FAILED:\n" + "\n".join(errs))

    # Canary-цель решается ОДИН раз в оркестраторе и уезжает в воркеры в task:
    # иначе каждый воркер решает сам и может взять разные цели (главная/категория).
    canary_target, canary_kind = S.resolve_canary_url(canary_url, campaign=url)
    log.info(f"[canary] цель: {canary_kind} ({canary_target})")

    wctxs = []
    _wm = gather_cfg.get("window_mode")
    for k, spec in enumerate(specs, 1):
        # window_mode: visible/hidden для воркер-браузеров (spec видит Session)
        spec["window_mode"] = _wm
        # Динамическая пакетная раздача: полный список НЕ раздаётся
        # статическим шардом; фактическая выдача пачек идёт через
        # parent→worker pipe в _await_workers. items здесь — только для
        # журнала/совместимости инициализации WorkerContext.
        task = _worker_task([], gather_cfg, max_recycles, session_budget,
                            worker_log, canary_url=canary_target)
        wctxs.append(_spawn_ctx(spec, task, on_event=on_event))
        if k < len(specs):
            _stagger = _start_stagger_s(k)
            log.info(f"[parallel] stagger старта worker-{k + 1}: "
                     f"{_stagger:.2f}с")
            time.sleep(_stagger)

    # Динамическая пакетная раздача: источники истины у оркестратора.
    shared_items = deque(items)
    completed_indexes: set[int] = set()
    owner: dict[int, int] = {}
    pid_by_idx = {idx: pid for idx, pid, _url in items}
    url_by_idx = {idx: url for idx, _pid, url in items}
    batch_size = _resolve_batch_size(gather_cfg)
    log.info(f"[parallel] выдача: по {batch_size} товар(а) за раз "
             f"({total} товаров, {parallel} воркер(ов))")

    deadline = time.monotonic() + join_timeout_s
    wctxs = _await_workers(
        wctxs, deadline=deadline,
        max_worker_restarts=max_worker_restarts,
        items_by_index={i: (pid, url) for i, pid, url in items},
        gather_cfg=gather_cfg, max_recycles=max_recycles,
        session_budget=session_budget, on_event=on_event,
        worker_log=worker_log, stop_event=stop_event,
        force_event=force_event,
        shared_items=shared_items, completed_indexes=completed_indexes,
        owner=owner, pid_by_idx=pid_by_idx, url_by_idx=url_by_idx,
        batch_size=batch_size, canary_url=canary_target)

    # Once the dynamic dispatch is over, shards are rebuilt from the current
    # owner (full (idx,pid,url) tuples) — the format merge_shard_results/QA/
    # digest expect. Records without an owner (never dispatched) keep their
    # statically assigned shard (fallback idx % parallel inside
    # _rebuild_shards).
    shards = _rebuild_shards(
        owner, pid_by_idx, url_by_idx, items, parallel)

    payloads = _collect_worker_payloads(wctxs)

    products = merge_shard_results(payloads, shards)
    sort_digest_products(products)
    collected = sum(1 for p in products
                    if p["ok"] and not p.get("excluded"))
    excluded = sum(1 for p in products if p.get("excluded"))

    combined = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": VERSION,
        "collected_at_utc": _now(),
        "search": {"url": url, "query": query},
        "found": found,
        "limit": limit,
        "collected": collected,
        "excluded": excluded,
        "recovery": recovery_main,
        "products": products,
        "parallel": {
            "workers": [_worker_summary(w, pl)
                        for w, pl in zip(wctxs, payloads)],
            "shards": [{"worker_idx": k, "count": len(s),
                        "indexes": [idx for idx, _p, _u in s]}
                       for k, s in enumerate(shards, 1)],
            "scheduler": _scheduler_summary(wctxs, batch_size),
        },
    }
    mr = gather_cfg.get("min_rating")
    if mr is not None:
        combined["min_rating"] = mr
    # Товары без отзывов, отброшенные по плитке выдачи (решение владельца
    # 2026-10-07): страницы не открывались, в combined идут ОТДЕЛЬНО от недобора.
    if skipped_no_reviews:
        combined["skipped_no_reviews"] = len(skipped_no_reviews)
        combined["skipped_no_reviews_pids"] = [it["pid"]
                                               for it in skipped_no_reviews]
    combined["kpi"] = _kpi_summary(payloads, len(products))
    storm = _storm_summary(wctxs, payloads, parallel)
    if storm:
        combined["aborted"] = storm
    return combined


def _merge_carried(pl: dict, wctx: WorkerContext) -> dict:
    """Включить в payload воркера-замены снимок уже собранного из упавшего.

    carried_payload заполняется в _await_workers при перезапуске (иначе merge
    пометил бы эти товары как terminated/failed и данные терялись бы).
    """
    carried = wctx.carried_payload
    if not carried or not carried.get("products"):
        return pl
    pl = dict(pl)
    pl["products"] = list(carried["products"]) + list(pl.get("products") or [])
    pl["processed_indexes"] = list(carried.get("processed_indexes") or []) \
        + list(pl.get("processed_indexes") or [])
    pl["failed_indexes"] = list(carried.get("failed_indexes") or []) \
        + list(pl.get("failed_indexes") or [])
    return pl


def _kpi_int(v) -> int:
    """Метрика → int (None/bool/строка/мусор не роняют агрегат)."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _kpi_summary(payloads: list[dict], attempted: int) -> dict:
    """Агрегат health-телеметрии пула в combined.kpi (ADR §14.7).

    Суммирует по воркерам recovery_metrics: challenges, recoveries (recycle +
    new_tab), session_blocked/exhausted, puzzle storms/attempts/solved,
    canary_fails и origin_denied (отказ origin 403/429/503 — сессионный, не
    бан адреса); считает производные puzzle_solve_rate и challenge_rate_per_100
    (challenge на 100 товаров).
    """
    ch = rec = blocked = exhausted = storms = p_att = p_fail = p_solved = 0
    canary_fails = origin_denied = 0
    for pl in payloads:
        rm = (pl or {}).get("recovery_metrics")
        if not isinstance(rm, dict):
            continue
        astats = rm.get("access_stats")
        if isinstance(astats, dict):
            ch += _kpi_int(astats.get("challenges"))
        rec += _kpi_int(rm.get("recycles")) + _kpi_int(rm.get("new_tab_attempts"))
        blocked += _kpi_int(rm.get("session_blocked"))
        exhausted += _kpi_int(rm.get("session_exhausted"))
        canary_fails += _kpi_int(rm.get("canary_fails"))
        origin_denied += _kpi_int(rm.get("origin_denied"))
        if rm.get("challenge_storm"):
            storms += 1
        pz = rm.get("puzzle")
        if isinstance(pz, dict):
            p_att += _kpi_int(pz.get("attempts"))
            p_fail += _kpi_int(pz.get("failures"))
            p_solved += _kpi_int(pz.get("solved"))
    # solved берётся ИЗМЕРЕННЫМ (счётчик note_solved), а не вычисляется как
    # attempts - failures: вычисление молча ломается, если счётчики разъедутся
    # (например, заход завершился поворотом из середины), и тогда отчёт
    # показывает неверную долю успеха без следа. Если поле отсутствует (старый
    # payload), откатываемся на прежнюю формулу — совместимость с воркерами,
    # запущенными до 2026-10-02.
    if not p_solved and p_att:
        p_solved = max(0, p_att - p_fail)
    return {
        "total_challenges": ch,
        "total_recoveries": rec,
        "session_blocked": blocked,
        "session_exhausted": exhausted,
        "canary_fails": canary_fails,
        "origin_denied": origin_denied,
        "puzzle_storms": storms,
        "puzzle_attempts": p_att,
        "puzzle_solved": p_solved,
        "puzzle_solve_rate": (round(p_solved / p_att, 3) if p_att else None),
        "challenge_rate_per_100": (round(ch / attempted * 100, 2)
                                   if attempted else None),
    }


def _worker_challenges(pl: dict) -> int:
    """Access-challenges конкретного воркера (для A/B параллелизма)."""
    rm = (pl or {}).get("recovery_metrics")
    astats = rm.get("access_stats") if isinstance(rm, dict) else None
    return _kpi_int(astats.get("challenges")) if isinstance(astats, dict) else 0


def _worker_summary(wctx: WorkerContext, pl: dict) -> dict:
    spec = wctx.spec
    processed = len(pl.get("processed_indexes", []))
    failed = len(pl.get("failed_indexes", []))
    challenges = _worker_challenges(pl)
    attempted = processed + failed
    return {
        "worker_idx": wctx.worker_idx,
        "pid": wctx.pid,
        "port": spec["port"],
        "profile_token": spec["profile_token"],
        "exitcode": pl.get("exitcode"),
        "started": pl.get("started"),
        "finished": pl.get("finished"),
        "processed": processed,
        "failed": failed,
        # Телеметрия для A/B параллелизма: сколько access-challenges досталось
        # этому воркеру и как часто (на 100 товаров). Раньше challenges были
        # только суммой по пулу — нельзя было понять, вредит ли параллелизм
        # именно этому IP/профилю.
        "challenges": challenges,
        "challenge_rate_per_100": (round(challenges / attempted * 100, 2)
                                   if attempted else None),
        "fatal_error": pl.get("fatal_error"),
        "rss_bytes": pl.get("rss_bytes"),
        "browser_stopped": pl.get("browser_stopped"),
        # Прокси воркера (маскированный, ревизия 2.9): нужно, чтобы A/B
        # «свой IP на воркер» читался прямо из combined.parallel.workers.
        "proxy": spec.get("proxy"),
        "recovery": pl.get("recovery_metrics"),
        "num_batches": pl.get("num_batches"),
        "scheduler": getattr(wctx, "scheduler_metrics", None),
    }


def _scheduler_summary(wctxs, batch_size: int | None) -> dict:
    """Агрегированная телеметрия пакетной раздачи (динамический режим).

    wall/active тайминги строятся из UTC-меток started/finished воркеров
    (единое время _now(), без смещения часовых поясов). tail_ratio и
    active_balance_ratio описывают «хвост»: насколько дольше/активнее работал
    самый медленный воркер относительно среднего.
    """
    walls = []
    for wctx in wctxs:
        pl = wctx.payload or {}
        s, f = pl.get("started"), pl.get("finished")
        if s and f:
            try:
                walls.append((datetime.fromisoformat(f)
                              - datetime.fromisoformat(s)).total_seconds())
            except (ValueError, TypeError):
                pass
    summary = {
        "batch_size": batch_size,
        "requeue_count": 0,
        "p2w_broken": 0,
        "tail_ratio": None,
        "active_balance_ratio": None,
    }
    for wctx in wctxs:
        m = getattr(wctx, "scheduler_metrics", None) or {}
        summary["requeue_count"] += int(m.get("requeue_count") or 0)
        summary["p2w_broken"] += int(m.get("p2w_broken") or 0)
    if walls:
        mean = sum(walls) / len(walls)
        mx = max(walls)
        if mean > 0:
            summary["tail_ratio"] = round(mx / mean, 3)
            summary["active_balance_ratio"] = round(mx / mean, 3)
    return summary
