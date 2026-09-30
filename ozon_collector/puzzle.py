"""Авто-решение слайдер-пазла Ozon (анти-бот «Сопоставьте пазл»).

Точка входа — solve_puzzle(page). Интегрируется в check_challenge
(collect.py): при слайдер-пазле НЕ перезагружаем вслепую (FAB-reload), а
решаем пазл — снимаем живой кадр фона (лунка нарисована оверлеем и есть
только в нём), детектим её детекторами и перетаскиваем кусочек за
калибровкой k. Template-match по CDN-PNG удалён как мёртвый для текущего
формата (см. puzzle_detect).

Модуль-фасад: сам держит только оркестрацию (FSM solve_puzzle, диагностику
провалов _solve_diag, CDN-кадр) и реэкспортирует остальное, чтобы
`puzzle.X` продолжал работать:

  puzzle_dom.py     — DOM пазла и признаки состояния челленджа
  puzzle_capture.py — съёмка живого кадра фона + геометрия полей
  puzzle_detect.py  — чистый CV: силуэт, детекторы лунки, отбор кандидатов
  puzzle_drag.py    — механика драга (калибровка k, доведение, свайп)
  puzzle_input.py   — человекоподобное движение мыши
  puzzle_state.py   — счётчики попыток и журнал попыток (JSONL)

Процесс-локальный счётчик неудач (puzzle_state._PUZZLE_FAILURES): если
решатель не смог снять пазл за PUZZLE_MAX_ATTEMPTS, шард об этом узнаёт
(recovery.challenge_storm) — оркестратор/демон понимает, что это не «голый»
challenge, а нерешаемый пазл (перезапуск/cooldown-логика отстраивается по
дополнительному признаку).
"""
from __future__ import annotations

import json
import os
import tempfile
import time

import numpy as np

from .puzzle_capture import (  # noqa: F401  (реэкспорт: puzzle.X работает)
    _fetch,
    _shot_bg_clean,
    _shot_piece_margin,
    _shot_piece_margin_rect,
    _shot_bg_reason,
)
from .puzzle_detect import (  # noqa: F401
    _detect_slot_diff,
    _detect_slot_edges,
    _detect_slot_topk,
    _diff_score_profile,
    _grip_point,
    _resample_lanczos,
    _sil_left,
    _silhouette,
    _solve_cands,
)
from .puzzle_dom import (  # noqa: F401
    _await_done,
    _await_solved,
    _captcha_done,
    _capture_info,
    _classify,
    _confirm_solved,
    _find_slider,
    _grace_resolve,
    _still_challenge,
    _stub_page,
    _page_has_content,
    is_captcha_shell,
    is_puzzle,
    looks_like_puzzle,
)
from .puzzle_input import (_human_click, _human_drag, _human_move,  # noqa: F401
                           _human_path)

from .puzzle_drag import (  # noqa: F401  (реэкспорт: puzzle.X работает)
    _drag_to,
    _piece_grip_offset,
    _read_sl_piece,
    _wait_piece,
)
from .puzzle_state import (  # noqa: F401
    PUZZLE_MAX_ATTEMPTS,
    _ATTEMPT_LOG_DIR,
    _PUZZLE_ATTEMPTS,
    _PUZZLE_FAILURES,
    _attempt,
    _note_attempt,
    attempts,
    failures,
    reset,
)

from ozon_collector import log

# Статистика кадра фона в последнем решении (mean/std/max/min) — для диагностики.
_PBG_STATS: list = [None]


_DIAG_DIR = os.environ.get(
    "OZON_PUZZLE_DIAG_DIR", os.path.join(tempfile.gettempdir(), "puzzle_diag"))
# Диаг-картинки/np.save на горячем пути — только по явному env (OZON_PUZZLE_DIAG=1).
# Fail-стадии пишутся всегда (это и есть диагностика проблем).
_DIAG_ENABLED = os.environ.get("OZON_PUZZLE_DIAG", "0") == "1"
_FAIL_DIAG_STAGES = {"no_slot", "all_cands_failed", "exception", "no_bg_shot"}

# Сколько ждём снятия капты после отпускания куска, прежде чем считать дроп
# отвергнутым. Приёмка приходит быстро (перезапрос выдачи на месте), поэтому
# 5s хватает с запасом; прежний код ждал до 24s (_confirm_solved 6s +
# _grace_resolve 18s) ради URL-маркера, которого текущее поколение капчи не
# ставит, — время сжигалось впустую на каждом отвергнутом кандидате.
_SOLVED_GRACE_S = 5.0


"""Живой метод решения (валидирован на живых капчах, новое поколение пазлов):

Кусочек НЕ является вырезом фона (template-match мёртв), а выемка-лунка
нарисована ДОМ/CSS-оверлеем ПОВЕРХ фона — на CDN-PNG её нет, она видна только
в реальном кадре страницы. Поэтому:
  1) прячем кусочек (visibility hidden) и снимаем именно элемент фона
     через page.screenshot(clip=rect bg) — кадр содержит лунку;
  2) силуэт кусочка = альфа его CDN-картинки, ресайз к css-боксу;
  3) детектор ВариаНТ A по строке кусочка (дорога горизонтальная,
     top кусочка == top лунки): argmax(mean(gm по силуэту) - mean(gm по
     внутреннему прямоугольнику фона вне силуэта));
  4) цель: viewport_x кусочка = bg.x + det_x;
  5) k-калибровка драгом +40px (кусок отстаёт от мыши: k 0.4..3), затем
     доводка до |err|<=2px и отпускание.

Пороговые константы взяты из живой прогонки 2026-09-08 (12 авто-решений).
"""


def _solve_diag(page, info, stage: str, detail: str = "", bg=None, piece=None,
                bg_stats=None, shot=None, fullshot=None) -> None:
    """Сохранить диагностику НЕУДАЧИ решателя в _DIAG_DIR (обычно $TMP).

    Одноразовый отладочный хук (проблема «пазл не решается» 2026-09-07):
    сохраняет текущий DOM-срез (все img + элементы slider/puzzle/track),
    найденные bg/piece-картинки (np.save), статистику кадра фона (bg_stats),
    сырой PNG кадра (shot), полный скриншот вьюпорта (fullshot) и стадию
    отказа. Никак не влияет на поведение (внутренний try/except, только
    запись файлов).
    """
    try:
        if not _DIAG_ENABLED and stage not in _FAIL_DIAG_STAGES:
            return
        d = _DIAG_DIR
        os.makedirs(d, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        rec = {"stage": stage, "detail": detail, "ts": ts}
        if bg_stats is not None:
            rec["bg_stats"] = {
                "mean": float(bg_stats[0]), "std": float(bg_stats[1]),
                "max": float(bg_stats[2]), "min": float(bg_stats[3]),
            }
        if shot:
            try:
                with open(os.path.join(d, f"{ts}_shot.png"), "wb") as f:
                    f.write(shot)
                rec["shot_png"] = f"{ts}_shot.png"
            except Exception as e:
                rec["shot_err"] = str(e)
        if fullshot:
            try:
                with open(os.path.join(d, f"{ts}_full.png"), "wb") as f:
                    f.write(fullshot)
                rec["full_png"] = f"{ts}_full.png"
            except Exception as e:
                rec["full_err"] = str(e)
        try:
            rec["info"] = info
        except Exception:
            pass
        try:
            dom = page.evaluate(
                """() => {
                  const out=[];
                  document.querySelectorAll('img,div,button').forEach(el=>{
                    const r=el.getBoundingClientRect();
                    if (r.width<=0 && r.height<=0) return;
                    const tag=el.tagName;
                    const c=String(el.className||'');
                    if (tag!=='IMG' &&
                        !/image|puzzle|slider|slide|handler|track|drag|ckbox/.test(c)) return;
                    out.push({
                      tag, cls:c.slice(0,80),
                      r:[Math.round(r.x),Math.round(r.y),
                         Math.round(r.width),Math.round(r.height)],
                      src:(el.currentSrc||el.src||'').slice(0,120)
                    });
                  });
                  return out;
                }""")
            rec["dom"] = dom
        except Exception as e:
            rec["dom_err"] = str(e)
        for name, arr in (("bg", bg), ("piece", piece)):
            if arr is not None:
                try:
                    np.save(os.path.join(d, f"{ts}_{name}.npy"), arr)
                    rec[f"{name}_shape"] = list(arr.shape)
                except Exception as e:
                    rec[f"{name}_err"] = str(e)
        with open(os.path.join(d, f"{ts}.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=2, default=str))
    except Exception:
        pass


def _bg_from_cdn(info):
    """Фоновый кадр из CDN-картинки (без скриншота окна).

    Скриншот-путь страдает гонкой скролла/свёрнутым окном: кадр снимается не
    с пазла, а с соседнего контента (diff с CDN 75+ по всему кадру). Детекция
    по CDN src напрямую не зависит от отрисовки страницы.

    Возвращает dict {bg_l, bg_r, pz_r} или None. bg_l ресайзится под
    CSS-размер bg_r[2]x[3], поэтому координаты лунки сразу в экранных px.
    """
    try:
        bg = info.get("bg") or {}
        pz = info.get("puzzle") or {}
        if not bg.get("src") or not pz.get("src"):
            return None
        b_r, p_r = bg["r"], pz["r"]
        if len(b_r) != 4 or len(p_r) != 4 or b_r[2] <= 0 or b_r[3] <= 0:
            return None
        from PIL import Image
        bw, bh = int(round(b_r[2])), int(round(b_r[3]))
        with Image.open(_fetch(bg["src"])) as im:
            bg_l = np.asarray(im.resize((bw, bh),
                                        _resample_lanczos()).convert("L")).astype(float)
        return {"bg_l": bg_l, "bg_r": b_r, "pz_r": p_r}
    except Exception:
        return None


def _page_state(page) -> dict:
    """Снимок состояния страницы после дропа — для последующего разбора.

    Нужен, чтобы отличить «пазл не решён» от «подменён заглушкой» и от
    «страница ещё грузится» уже ПОСЛЕ факта, а не по памяти. Один
    page.evaluate, тихий при любой ошибке. Пишется в журнал попыток
    (puzzle._attempt, kind=cand): даёт размеченный датасет «куда поставили →
    чем кончилось» для последующей правки детектора.
    """
    try:
        d = page.evaluate("""() => ({
            href: (location.href || '').slice(0, 160),
            title: (document.title || '').slice(0, 80),
            h1: (() => { const e = document.querySelector('h1');
                         return e ? (e.innerText || '').trim().slice(0, 60) : ''; })(),
            blen: (document.body ? (document.body.innerText || '').length : 0),
        })""")
        if isinstance(d, dict):
            return {("st_" + k): v for k, v in d.items()}
    except Exception:
        pass
    return {}


def _verdict(page) -> str:
    """Вердикт снятия капты: 'done' | 'unblocked' | 'no'.

    Признаки резолвятся через ГЛОБАЛЫ ЭТОГО МОДУЛЯ (не через puzzle_dom),
    чтобы юнит-тесты подменяли puzzle._captcha_done / puzzle._still_challenge
    / puzzle._stub_page / puzzle._page_has_content и влияли на боевое
    поведение solve_puzzle.
    """
    return _classify(_captcha_done(page), not _still_challenge(page),
                     _stub_page(page), _page_has_content(page))


def _await_verdict(page, seconds: float = 5.0) -> str:
    """Поллить вердикт снятия (см. _verdict)."""
    return _await_solved(page, seconds, probe=lambda: _verdict(page))


def _captcha_src_changed(page, info) -> bool:
    """Капча сменилась (новые bg/puzzle src после дропа) — True.

    После неудачного дропа Ozon часто РЕРОЛЛИТ капчу с новым фоном/куском:
    старые кандидаты бесполезны, и свип по ним — трата попыток (диагноз
    2026-09-22 по live-логу: каждый reject оставляет десятки мёртвых дропов).
    Сравниваем src bg и puzzle текущего захвата info со свежим захватом."""
    try:
        nfo = _capture_info(page)
        if not nfo:
            return True
        def _src(d):
            return ((d or {}).get("src") or "").strip()
        old = (_src((info or {}).get("bg")), _src((info or {}).get("puzzle")))
        new = (_src((nfo or {}).get("bg")), _src((nfo or {}).get("puzzle")))
        return old != new
    except Exception:
        return True


def solve_puzzle(page, attempts: int = PUZZLE_MAX_ATTEMPTS,
                 timeout_s: float = 0.0, on_event=None) -> bool:
    """Решить слайдер-пазл на странице. True — пазл снят.

    Живой метод: прятки кусочка + силуэтный детектор Вариант A по кадру фона.
    Фон сначала берётся из CDN src (надёжно, независимо от скролла/свёрнутого
    окна), при неудаче — фолбэк на скриншот окна. При любой ошибке (нет DOM,
    нет картинок, не нашлась выемка, обрыв CDP) — False. Счётчик неудач растёт
    за каждый неудачный заход.

    timeout_s > 0 — жёсткий лимит на всё решение: кандидаты прекращаются,
    неснятый пазл возвращается False (защита охотника от долгих свипов).

    on_event: опциональный колбек `on_event(dict)` для журнала решателя
    (puzzle_hunter пишет из него в свой журнал про «что внутри окна»):
      - {"kind": "reroll"}        — пазл неправильно решён, Ozon перезалил новый
                                    (кусок упал / челлендж жив после дропа);
      - {"kind": "empty"}         — капча есть, но пазл так и не пришёл
                                    (заглушка «Попробуйте, пожалуйста, ещё раз»).
    """
    _t0 = time.monotonic()

    def _expired() -> bool:
        return bool(timeout_s > 0 and time.monotonic() - _t0 > timeout_s)

    # Геометрический инвариант: физическое OS-окно должно совпадать с
    # CDP-моделью, иначе композитор рисует контент смещённо относительно
    # координат мыши и решать пазл в сломанной геометрии бессмысленно
    # (промах гарантирован). Guard читает HWND+CDP; при расхождении один раз
    # нормализует и перепроверяет; после провала решаем — только если ок
    # (страховка от внешних случаев: ручной ресайз, смена DPI/монитора).
    geom = getattr(page, "_geom_guard", None)
    if geom is not None:
        try:
            g_ok, g_metrics = geom(page)
        except Exception as e:
            log.debug(f"geometry_guard err {type(e).__name__}: {e} "
                      f"(guard отключён)")
            g_ok, g_metrics = True, {}
        _attempt("geom", ok=bool(g_ok), **g_metrics)
        if not g_ok:
            log.warning(f"solve: геометрия HWND↔CDP осталась рассинхронизированной "
                        f"({g_metrics.get('hwnd_rect')} vs "
                        f"{g_metrics.get('cdp_bounds')}) — не решаю в сломанной "
                        f"геометрии")
            _note_attempt()
            _PUZZLE_FAILURES[0] += 1
            return False

    for _ in range(max(1, int(attempts))):
        _note_attempt()
        if _expired():
            _PUZZLE_FAILURES[0] += 1
            _attempt("solve", ok=False, reason="timeout")
            return False
        try:
            info = _capture_info(page)
        except Exception:
            info = None
        # Живой пазл поднимается с loader'ом: captcha_v10.js заполняет src
        # картинок через 0.3-2s после появления оболочки #captcha (в HTML
        # img#image/img#puzzle приходят с пустым src, см. actual DOM).
        # Пока src пуст — пазл не готов: поллим до загрузки (или дедлайна).
        for _w in range(16):
            if (info and info.get("bg") and info.get("puzzle")
                    and (info["bg"].get("src") or "").strip()
                    and (info["puzzle"].get("src") or "").strip()):
                break
            if _expired():
                return False
            time.sleep(0.5)
            try:
                info = _capture_info(page)
            except Exception:
                info = None
            # Капча жива, а пазл так и НЕ пришёл (src пустой) дольше 3s —
            # это заглушка «нет соединения» / пустой челлендж: сообщаем сразу,
            # не ждём полные 8s буфера. Инжект пазла идёт 0.3-2s, так что
            # ложных срабатываний нет. Второй случай — челлендж ИСЧЕЗ, но на
            # странице «нет соединения»/«попробуйте ещё раз»: тоже заглушка.
            if (on_event is not None and _w >= 5
                    and time.monotonic() - _t0 > 3.0
                    and not _captcha_done(page)):
                if _still_challenge(page):
                    _reason = "src_empty"
                elif _stub_page(page):
                    _reason = "challenge_gone"
                else:
                    _reason = None
                if _reason:
                    on_event({"kind": "empty", "reason": _reason,
                              "seconds_s": round(time.monotonic() - _t0, 1)})
                    return False
        if not info or not info.get("bg") or not info.get("puzzle"):
            # пазл уже снят (переход на url captchaDone=true) — успех; иначе
            # просто нет пазла на странице — неудачная попытка (счётчик
            # failures растёт: инвариант attempts == failures + solved).
            if on_event is not None:
                on_event({"kind": "empty",
                          "seconds_s": round(time.monotonic() - _t0, 1)})
            done = _captcha_done(page)
            if not done:
                _PUZZLE_FAILURES[0] += 1
            return done
        try:
            psrc = (info["puzzle"] or {}).get("src")
            if not psrc:
                _PUZZLE_FAILURES[0] += 1
                return False
            cdn = _bg_from_cdn(info)
            shot = None
            grip = None
            if cdn is not None:
                # Кандидаты: diff(живой кадр, CDN) ПЕРВИЧНЫ — лунка рисуется
                # CSS-оверлеем только в живом кадре (новый формат), на плоских
                # фонах градиентный Variant A слеп. Variant A по CDN — фолбэк.
                bg_l, b_r, p_r = cdn["bg_l"], cdn["bg_r"], cdn["pz_r"]
                mask = _silhouette(_fetch(psrc).read(), p_r[2], p_r[3])
                grip = _grip_point(mask)
                yrow = int(round(p_r[1] - b_r[1]))
                _PBG_STATS[0] = (float(bg_l.mean()), float(bg_l.std()),
                                 float(bg_l.max()), float(bg_l.min()))
                shot = _shot_bg_clean(page, info)
                diff_cands = []
                if shot is not None and shot["bg_l"].shape == bg_l.shape:
                    diff_cands = _detect_slot_diff(shot["bg_l"], bg_l,
                                                   mask, yrow, k=3)
                edge_cands = []
                if shot is not None and shot["bg_l"].shape == bg_l.shape:
                    edge_cands = _detect_slot_edges(shot["bg_l"], bg_l,
                                                    mask, yrow, k=3)
                va_cands = _detect_slot_topk(bg_l, mask, yrow, k=3)
                # Диагноз 2026-09-20: edges возвращает левую ГРАНЬ лунки в фоне,
                # а цель драга — левый край img куска. У куска слева прозрачная
                # маржа silL (6-21px), поэтому img.left должен быть на silL
                # меньше грани лунки. Валидация на 18 diag-кадрах: edges-silL
                # попадает в нужную лунку 14/17 (±1px), diff из-за присутствия
                # самого куска в живом кадре ошибается чаще (4/18) — приоритет
                # скорректированным edges.
                silL = _sil_left(mask)
                silR = None
                try:
                    silR = _shot_piece_margin(page, info)
                except Exception as e:
                    log.warning(f"solve silR failed, fallback silL: {e}")
                sil_use = silR if silR is not None else silL
                cands = _solve_cands(mask, edge_cands, diff_cands,
                                     va_cands, sil_use)
                log.debug(f"solve cdn bg={bg_l.shape} piece={mask.shape} p_r={p_r} "
                          f"yrow={yrow} edges={[x for x, _ in edge_cands]} "
                          f"diff={[x for x, _ in diff_cands]} "
                          f"va={[x for x, _ in va_cands]} cands={cands} "
                          f"shot_reason={_shot_bg_reason()} "
                          f"silL={silL} silR={silR}")
                _attempt("det", diff=[x for x, _ in diff_cands],
                         va=[x for x, _ in va_cands],
                         edges=[x for x, _ in edge_cands],
                         cands=[x for x, _ in cands],
                         shot_reason=_shot_bg_reason())
                if edge_cands or diff_cands:
                    _solve_diag(page, info, "slot_diff",
                                detail=(f"bg={bg_l.shape} piece={mask.shape} "
                                        f"yrow={yrow} "
                                        f"edges={[x for x, _ in edge_cands]} "
                                        f"diff={[x for x, _ in diff_cands]} "
                                        f"va={[x for x, _ in va_cands]}"),
                                bg_stats=_PBG_STATS[0], bg=bg_l,
                                shot=shot.get("orig") if shot else None)
                elif cands:
                    _solve_diag(page, info, "slot_cdn",
                                detail=f"bg={bg_l.shape} piece={mask.shape} "
                                       f"yrow={yrow} cands={cands[:2]}",
                                bg_stats=_PBG_STATS[0], bg=bg_l)
            else:
                cands = None
            if cands is None:
                shot = _shot_bg_clean(page, info)
                if shot is None:
                    # Живого кадра фона нет → решать нечем: лунка рисуется
                    # оверлеем только в живом кадре (на CDN-PNG её нет), а
                    # template-match по CDN для текущего формата мёртв.
                    # Раньше здесь звался classic-fallback: он делал слепой
                    # драг, возвращал True, и код падал в UnboundLocalError
                    # на f-string ниже (bg_l/mask/yrow не присвоены).
                    _solve_diag(page, info, "no_bg_shot",
                                detail=f"reason={_shot_bg_reason()}")
                    _PUZZLE_FAILURES[0] += 1
                    return False
                bg_l = shot["bg_l"]
                b_r = shot["b_r"]
                p_r = shot["pz_r"]
                if len(p_r) != 4 or len(b_r) != 4:
                    _PUZZLE_FAILURES[0] += 1
                    return False
                mask = _silhouette(_fetch(psrc).read(), p_r[2], p_r[3])
                yrow = int(round(p_r[1] - b_r[1]))
                cands = _detect_slot_topk(bg_l, mask, yrow, k=3)
                _PBG_STATS[0] = (float(bg_l.mean()), float(bg_l.std()),
                                 float(bg_l.max()), float(bg_l.min()))
            if not cands:
                fullshot = None
                try:
                    fullshot = page.screenshot(timeout=10000, animations="disabled")
                except Exception:
                    pass
                log.warning("solve: детекторы пусты → no_slot")
                _solve_diag(page, info, "no_slot",
                            detail=(f"bg={bg_l.shape} piece={mask.shape} yrow={yrow} "
                                    f"reason={_shot_bg_reason()}"),
                            bg_stats=_PBG_STATS[0], bg=bg_l,
                            shot=shot.get("orig") if shot else None,
                            fullshot=fullshot)
                _PUZZLE_FAILURES[0] += 1
                return False
            tried = set()

            def _drag_bx(x: int, score0: float):
                """Один драг к bx=x. Возвращает статус."""

                if x < 1 or x in tried:
                    return "skip"
                tried.add(x)
                if _expired():
                    return "timeout"
                target_vx = b_r[0] + x
                log.debug(f"solve cand bx={x} score={score0:.3f} "
                          f"target_vx={target_vx:.1f}")
                okc = _drag_to(page, target_vx, info, x, score0, grip=grip,
                           deadline=(_t0 + timeout_s) if timeout_s > 0 else 0.0)
                still = _still_challenge(page)
                if _expired():
                    _attempt("solve", ok=False, reason="timeout")
                    return "timeout"
                # Вердикт снятия — по ЛЮБОМУ из двух признаков. Прежний ждал
                # только captchaDone=true в URL, чего текущее поколение капчи
                # (v10, кусочек без трека) не делает НИКОГДА: после верного
                # дропа челлендж снимается из DOM, выдача перезапрашивается
                # на месте. Живой лог 2026-09-29: 64 дропа легли в лунку
                # (|err|≤1px), успехом засчитаны 3 — 61 верное попадание
                # выброшено, плюс 24s мёртвого ожидания URL на кандидата.
                verdict = _await_verdict(page, _SOLVED_GRACE_S)
                _attempt("cand", bx=x, score=score0, okc=bool(okc),
                         still=still, verdict=verdict,
                         target_vx=round(target_vx, 1),
                         **_page_state(page))
                log.debug(f"solve drag okc={okc} still_challenge={still} "
                          f"verdict={verdict}")
                if verdict in ("done", "unblocked"):
                    _attempt("solve", ok=True, via=verdict, bx=x)
                    return "solved"
                # Не снят. Подмена заглушкой (челлендж исчез, но страница —
                # «нет соединения»/FAB) или кусок пропал с живым челленджем:
                # кандидаты устарели, свип прекращаем.
                if not still:
                    if on_event is not None:
                        on_event({"kind": "reroll", "ctx": "confirm_failed"})
                    return "reroll"
                if okc:
                    # кусок пропал из DOM, но челлендж жив (реролл после
                    # дропа) — кандидаты устарели, прекращаем весь свип.
                    if on_event is not None:
                        on_event({"kind": "reroll", "ctx": "piece_drop"})
                    return "reroll"
                # точный/грубый дроп не снял капчу. Если капча за это время
                # сменилась (реролл новым фоном/куском) — кандидаты устарели,
                # прекращаем весь свип; иначе кандидат отвергнут.
                if _captcha_src_changed(page, info):
                    if on_event is not None:
                        on_event({"kind": "reroll", "ctx": "src_changed"})
                    return "reroll"
                return "reject"

            # ЛИВ-вапидация 2026-09-22 (59 успешных эпизодов): решающий bx
            # == cands[0] в 36 эп., cands[0]±3px в 21 эп., кандидаты r1/r2 в
            # 5 эп., ноль — дальше. Свип по ВСЕМ кандидатам давал 4+bx → 0%
            # успехов: капча рероллится после каждого неудачного дропа,
            # устаревшие кандидаты бесполезны. Итого: только cands[0] и
            # микросдвиги ±3 (компенсация дискретизации детектора ~±1-4px),
            # затем рестарт цикла (свежий захват новой капчи).
            bx0, score0 = cands[0]
            for off in (0, -3, 3):
                r = _drag_bx(bx0 + off, score0)
                if r == "solved":
                    return True
                if r == "timeout":
                    return False
                if r == "reroll":
                    break
            if r == "reject" and len(cands) > 1:
                # точная цель + микросдвиги кандидата-1 не сняли капчу,
                # капча не сменилась: один шанс ближайшему следующему кластеру
                # (по данным r1/r2 решают в 5/59 эпизодов); потом рестарт.
                r2 = _drag_bx(cands[1][0], cands[1][1])
                if r2 == "solved":
                    return True
                if r2 == "timeout":
                    return False
            _solve_diag(page, info, "all_cands_failed",
                        detail=f"cands={cands}")
            _attempt("solve", ok=False, cands=[c[0] for c in cands])
            # Пазл часто МЕНЯЕТСЯ после неудачного дропа (новый фон/слайдер):
            # устаревшие кандидаты бесполезны — выходим на повторный захват.
            continue
        except Exception as exc:
            _solve_diag(page, info, "exception",
                        detail=f"{type(exc).__name__}: {exc}")
            _PUZZLE_FAILURES[0] += 1
            return False
        # Сюда падаем только если ПОСЛЕДНИЙ цикл закончился на `continue`:
        # без этого хвоста solve_puzzle возвращал None (falsy без ошибки), и
        # счётчик failures не рос — ломался инвариант KPI attempts ==
        # failures + solved. Прежний `_grace_resolve(page, 18.0)` здесь ждал
        # URL-маркер, которого текущее поколение капчи не ставит: 18s
        # пустоты на каждый проваленный инцидент (наблюдение с экрана
        # 2026-09-30: «пазл стоит, 20-30s ничего, потом перезагрузка»).
        # Вердикт снятия теперь полностью живёт в _drag_bx (_await_verdict).
        if _captcha_done(page):
            _attempt("solve", ok=True, note="late_url")
            return True
        _PUZZLE_FAILURES[0] += 1
        return False