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
import socket
import threading
import time
from datetime import datetime, timezone

from . import SCHEMA_VERSION, VERSION
from . import cdp
from . import log
from . import process as P
from . import search as S
from .browser import BrowserProcessManager

WORKER_LOG_DIR = "logs"


# ---- Спеки и сериализуемые структуры ---------------------------------------

def worker_spec(k: int) -> dict:
    """Спека воркера k: порт/профиль/лог. ЕДИНЫЙ источник имён (контракт).

    Порт = 9222 + k (k=1..N; база 9222 занята main-сессией). Профиль и путь —
    из process.worker_profile_* (тестируются отдельно).
    """
    k = int(k)
    return {
        "worker_id": k,
        "port": 9222 + k,
        "cdp_url": f"http://127.0.0.1:{9222 + k}",
        "profile_token": P.worker_profile_token(k),
        "profile_path": P.worker_profile_path(k),
        "log_path": _worker_log_path(k),
    }


def _worker_log_path(k: int) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, WORKER_LOG_DIR, f"ozon_worker_{int(k)}.log")


def shard_items(items, parallel: int) -> list[list[tuple]]:
    """Round-robin по индексу: items[(idx,pid,url)] → shard[idx % parallel].

    Индекс — ЧАСТЬ задания (появляется в merge). Детерминизм: тот же links →
    те же шарды. Каждый товар ровно в одном шарде.
    """
    parallel = int(parallel)
    shards: list[list[tuple]] = [[] for _ in range(parallel)]
    for idx, pid, url in items:
        shards[idx % parallel].append((idx, pid, url))
    return shards


# ---- Preflight: 4 независимых ресурса ---------------------------------------

def _port_free(port: int) -> bool:
    """TCP connect к 127.0.0.1:port НЕ открывается → порт свободен."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return False
    except OSError:
        return True


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
    return errs


# ---- WorkerContext и reader ------------------------------------------------

class WorkerContext:
    """Состояние воркера со стороны parent: процесс, события, payload."""

    def __init__(self, spec: dict, proc, conn):
        self.spec = spec
        self.proc = proc
        self.conn = conn
        self.pid: int | None = None
        self.started: bool = False
        self.recovery_events: list[dict] = []
        self.payload: dict | None = None
        self.reader: threading.Thread | None = None
        self.exitcode: int | None = None

    @property
    def worker_idx(self) -> int:
        return int(self.spec["worker_id"])


def _reader_loop(wctx: WorkerContext):
    """Читает протокол воркера (select по pipe на Windows не работает → поток)."""
    try:
        while True:
            try:
                msg = wctx.conn.recv()
            except (EOFError, OSError, ValueError):
                break
            t = msg.get("t")
            if t == "start":
                wctx.pid = msg.get("pid")
                wctx.started = True
            elif t == "recovery":
                wctx.recovery_events.append(msg)
            elif t == "done":
                wctx.payload = msg.get("payload") or {}
                break
    except Exception:
        pass
    finally:
        try:
            wctx.conn.close()
        except Exception:
            pass


def _spawn_ctx(spec: dict, task: dict) -> WorkerContext:
    """spawn воркера + reader-поток (протокол накапливается в WorkerContext)."""
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    proc = ctx.Process(target=worker_main, args=(spec, task, child_conn),
                       name=f"ozon-worker-{spec['worker_id']}")
    proc.start()
    child_conn.close()
    wctx = WorkerContext(spec, proc, parent_conn)
    wctx.reader = threading.Thread(target=_reader_loop, args=(wctx,),
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
    return cdp.Session(spec["cdp_url"], profile_token=spec["profile_token"])


def _make_manager(spec: dict):
    """BrowserProcessManager воркера (тест-дубль при OZON_WORKER_DOUBLES=1)."""
    if os.environ.get("OZON_WORKER_DOUBLES") == "1":
        from tests._fake_worker import fake_manager
        return fake_manager(spec)
    return BrowserProcessManager(cdp_url=spec["cdp_url"],
                                 profile_token=spec["profile_token"])


def _run_shard(spec: dict, sess, manager, items, gather_cfg: dict,
               max_recycles: int, recovery: dict, on_recovery):
    """Шард-цикл воркера (тест-дубль при OZON_WORKER_DOUBLES=1)."""
    if os.environ.get("OZON_WORKER_DOUBLES") == "1":
        from tests._fake_worker import fake_run_shard
        return fake_run_shard(spec, sess, manager, items, gather_cfg,
                              max_recycles, recovery, on_recovery)
    return S._collect_shard(sess, manager, items, gather_cfg,
                            max_recycles=max_recycles, recovery=recovery,
                            on_recovery=on_recovery,
                            label=f"worker{spec['worker_id']}")


def worker_main(spec: dict, task: dict, conn):
    """Точка входа воркер-процесса: свой браузер, свой шард, payload → pipe.

    Весь worker_main в try/except (правка 5): любое исключение → payload с
    fatal_error + частичные processed/failed → exit 0. В finally — остановка
    СВОЕГО браузера (force_stop по зарегистрированным identity) и close Session.
    """
    k = spec["worker_id"]
    prefix = f"[worker={k} pid={os.getpid()}] "
    payload = _empty_payload(k)
    sess = None
    manager = None
    try:
        log.attach_file(spec["log_path"], mode="a", prefix=prefix)
        os.environ["OZON_WORKER_ID"] = str(k)
        payload["started"] = _now()
        _send(conn, {"t": "start", "worker": k, "pid": os.getpid(),
                     "profile": spec["profile_token"], "port": spec["port"]})

        sess = _make_session(spec)
        manager = _make_manager(spec)
        from .collect import set_scroll_jitter_seed
        set_scroll_jitter_seed(task.get("scroll_jitter_seed"))

        items = list(task["items"])
        gather_cfg = dict(task.get("gather_cfg") or {})
        max_recycles = int(task.get("max_recycles", 2))
        recovery = payload["recovery_metrics"]

        def on_recovery(ev: dict):
            # recovery-метрики накапливает сам _collect_shard (в payload);
            # сюда — только телеметрия по pipe родителю.
            _send(conn, {"t": "recovery", "worker": k, **ev})

        results = _run_shard(
            spec, sess, manager, items, gather_cfg,
            max_recycles=max_recycles, recovery=recovery,
            on_recovery=on_recovery)

        payload["products"] = [
            {"index": idx, "result": res}
            for (idx, _pid, _url), res in zip(items, results)]
        payload["processed_indexes"] = [idx for idx, _p, _u in items]
        payload["failed_indexes"] = [
            idx for (idx, _p, _u), res in zip(items, results)
            if not res["ok"]]
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
            try:
                records = [P.ProcessRecord(f"worker{k}-stop", ident)
                           for ident in P.browser_processes(
                               spec["cdp_url"],
                               profile_token=spec["profile_token"])]
                if records:
                    manager.force_stop(records)
                    payload["browser_stopped"] = True
            except Exception as e:
                log.warning(f"stop browser: {type(e).__name__}: {e}")
        payload["finished"] = _now()
        _send(conn, {"t": "done", "worker": k, "payload": payload})
        try:
            conn.close()
        except Exception:
            pass
    return 0


# ---- Parent: merge, cleanup, collect_parallel ------------------------------

def merge_shard_results(payloads: list[dict], shards: list[list[tuple]]) -> list[dict]:
    """Слияние по index → products строго в исходном порядке (всего len(shards)*N).

    Товар без результата → ok=False, error="worker{k} terminated/failed".
    Результат run_gather вставляется как есть (тот же формат, что в collect_search).
    """
    by_index = {}
    for pl in payloads:
        for entry in pl.get("products", []) or []:
            by_index[int(entry["index"])] = entry["result"]
    owner = {}
    for k, shard in enumerate(shards, 1):
        for idx, _p, _u in shard:
            owner[int(idx)] = k
    total = sum(len(s) for s in shards)
    out = []
    for i in range(total):
        if i in by_index:
            out.append(by_index[i])
        else:
            out.append({"pid": None, "ok": False,
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


def _storm_summary(wctxs, payloads, parallel: int) -> dict | None:
    """Сводка challenge-storm'ов по payload воркеров (или None, если шторма нет).

    Воркер, упёршийся в персистентный access_challenge, останавливает свой
    шард и кладёт recovery_metrics.challenge_storm. Родитель собирает сводку:
    какие воркеры штормили и был ли шторм у ВСЕХ (all_workers — признак того,
    что Ozon душит аккаунт/IP целиком → полный cooldown).
    """
    storms = []
    for w, pl in zip(wctxs, payloads):
        rm = pl.get("recovery_metrics") or {}
        if rm.get("challenge_storm"):
            storms.append({"worker": w.worker_idx,
                           **rm["challenge_storm"]})
    if not storms:
        return None
    return {
        "reason": "challenge_storm",
        "workers_stormed": [s["worker"] for s in storms],
        "all_workers": len(storms) == parallel,
        **storms[0],
    }


def _stop_main_browser():
    """Полный останов main-Chromium (порт 9222, ozon-profile) ДО воркеров.

    Session.close гасит только CDP-клиент; лишний main-процесс ест RAM при
    параллельном сборе — останавливаем через manager по главному токену.
    """
    cdp_url = "http://127.0.0.1:9222"
    records = [P.ProcessRecord("main-stop", ident)
               for ident in P.browser_processes(cdp_url)]
    if not records:
        return
    try:
        manager = BrowserProcessManager(cdp_url=cdp_url)
        manager.force_stop(records)
        log.info(f"main Chromium stopped ({len(records)} процесс(ов))")
    except Exception as e:
        log.warning(f"stop main Chromium: {type(e).__name__}: {e}")


def collect_parallel(url: str, limit: int, gather_cfg: dict, parallel: int,
                     query: str | None = None, max_recycles: int = 2,
                     join_timeout_s: float = 1800.0) -> dict:
    """Parallel-search: links в main-сессии → шарды → воркеры → merge.

    Возвращает combined-объект схемы 2.0 (аддитивно parallel{workers[],shards[]}).
    Ошибка preflight → RuntimeError (воркеры не запускаются).
    """
    parallel = int(parallel)
    sess = cdp.Session()
    recovery_main = {"recycles": 0, "events": []}
    try:
        manager_main = BrowserProcessManager(cdp_url=sess.cdp_url)
        links = S._collect_links_with_recovery(
            sess, manager_main, url, limit, max_recycles, recovery_main)
    finally:
        try:
            sess.close()
        except Exception:
            pass
        _stop_main_browser()

    found = len(links)
    selected = links[:limit]
    items = [(i, it["pid"], it["url"]) for i, it in enumerate(selected)]
    total = len(items)
    print(f"[search] найдено {found} товаров в выдаче, "
          f"собираю {total} через {parallel} воркер(ов)")
    log.info(f"найдено {found} товаров в выдаче, собираю {total} "
             f"через {parallel} воркер(ов)")

    shards = shard_items(items, parallel)
    specs = [worker_spec(k) for k in range(1, parallel + 1)]
    errs = preflight_check(specs)
    if errs:
        raise RuntimeError("preflight FAILED:\n" + "\n".join(errs))

    wctxs = []
    for k, spec in enumerate(specs, 1):
        task = {
            "items": shards[k - 1],
            "gather_cfg": gather_cfg,
            "max_recycles": max_recycles,
            "scroll_jitter_seed": gather_cfg.get("scroll_jitter_seed"),
        }
        wctxs.append(_spawn_ctx(spec, task))

    deadline = time.monotonic() + join_timeout_s
    for wctx in wctxs:
        remain = deadline - time.monotonic()
        if remain > 0:
            wctx.proc.join(remain)

    for wctx in wctxs:
        if wctx.proc.is_alive() and wctx.payload is None:
            log.warning(f"worker{wctx.worker_idx} hang: terminate()")
            wctx.proc.terminate()
            wctx.proc.join(2)
            _cleanup_worker(wctx)

    for wctx in wctxs:
        wctx.proc.join(2)
        wctx.exitcode = wctx.proc.exitcode

    payloads = []
    for wctx in wctxs:
        pl = wctx.payload or _empty_payload(wctx.worker_idx)
        if wctx.proc.exitcode != 0 and pl["fatal_error"] is None:
            pl["fatal_error"] = f"exit {wctx.proc.exitcode}"
        pl["exitcode"] = wctx.proc.exitcode
        payloads.append(pl)

    products = merge_shard_results(payloads, shards)
    collected = sum(1 for p in products if p["ok"])

    combined = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": VERSION,
        "collected_at_utc": _now(),
        "search": {"url": url, "query": query},
        "found": found,
        "limit": limit,
        "collected": collected,
        "recovery": recovery_main,
        "products": products,
        "parallel": {
            "workers": [_worker_summary(w, pl)
                        for w, pl in zip(wctxs, payloads)],
            "shards": [{"worker_idx": k, "count": len(s),
                        "indexes": [idx for idx, _p, _u in s]}
                       for k, s in enumerate(shards, 1)],
        },
    }
    storm = _storm_summary(wctxs, payloads, parallel)
    if storm:
        combined["aborted"] = storm
    return combined


def _worker_summary(wctx: WorkerContext, pl: dict) -> dict:
    spec = wctx.spec
    return {
        "worker_idx": wctx.worker_idx,
        "pid": wctx.pid,
        "port": spec["port"],
        "profile_token": spec["profile_token"],
        "exitcode": pl.get("exitcode"),
        "started": pl.get("started"),
        "finished": pl.get("finished"),
        "processed": len(pl.get("processed_indexes", [])),
        "failed": len(pl.get("failed_indexes", [])),
        "fatal_error": pl.get("fatal_error"),
        "rss_bytes": pl.get("rss_bytes"),
        "browser_stopped": pl.get("browser_stopped"),
        "recovery": pl.get("recovery_metrics"),
    }
