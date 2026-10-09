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
from typing import Optional

import numpy as np

from .puzzle_capture import (  # noqa: F401  (реэкспорт: puzzle.X работает)
    _fetch,
    _page_shot,
    _shot_bg_clean,
    _shot_piece_margin,
    _shot_piece_margin_rect,
    _shot_bg_reason,
    ensure_render_window,
)
from .puzzle_detect import (  # noqa: F401
    _detect_slot_diff,
    _detect_slot_edges,
    _detect_slot_shape,
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
    STUB_MARKERS,
    _page_has_content,
    _slot_from_js,
    _slot_spy_install,
    is_captcha_shell,
    is_puzzle,
    looks_like_puzzle,
)
from .puzzle_drag import (  # noqa: F401  (реэкспорт: puzzle.X работает)
    ACC,
    _drag_to,
    _dsl,
    _gain_update,
    _k_from_probe,
    _k_usable,
    _last_move,
    _left,
    _mouse_up,
    _piece_grip_offset,
    _read_settled,
    _read_sl_piece,
    _settle,
    _step_mouse,
    _wait_piece,
    last_drag_diag,
)
from .puzzle_input import (_fit_points, _human_click, _human_drag,  # noqa: F401
                           _human_move, _human_path, move_cost)
from .puzzle_state import (  # noqa: F401
    PUZZLE_BUDGET_S,
    PUZZLE_MAX_ATTEMPTS,
    _ATTEMPT_LOG_DIR,
    _PUZZLE_ATTEMPTS,
    _PUZZLE_FAILURES,
    _attempt,
    _note_attempt,
    attempts,
    failures,
    last_was_timeout,
    note_solved,
    note_storm,
    note_timeout,
    reset,
    solved,
    summary as puzzle_summary,
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
# отвергнутым. Приёмка приходит быстро, но страница рендерится МЕДЛЕННО: живой
# замер 2026-10-01 показал дропы, где челлендж исчез, а текста на странице
# было всего 189 символов и h1 пустой — контент ещё не успел прийти, и вердикт
# срезал реальный успех. Тот же замер: заглушка «нет соединения» вылезает
# через ~30s. Поэтому 12s вместо прежних 5s.
# Плата нулевая: _await_solved возвращается сразу, как только вердикт решился,
# поэтому grace тратится ТОЛЬКО когда страница действительно зависла (а в этом
# случае мы всё равно уходим на новый цикл).
_SOLVED_GRACE_S = 12.0

# Максимум ожидания, когда дроп ОТВЕРГНУТ (челлендж жив и не исчезает).
# Короткий намеренно: ждать тут нечего, а каждый выстрел идёт в общий бюджет
# solve. Полный _SOLVED_GRACE_S платится только когда челлендж исчез.
_REJECT_FAST_S = 1.5

# Максимум выстрелов (дропов) на один цикл. НАМЕРЕННО МАЛО: каждый дроп с
# неверной целью вызывает перезагрузку пазла, а после нескольких промахов
# сессия сгорает в заглушку «нет соединения» (наблюдение пользователя с экрана
# 2026-10-01). Раньше стояло 7 — это выжигало сессию впустую, т.к. после
# первого дропа кандидаты всё равно протухали. Стратегия: точнее попадать, а
# не больше стрелять.
SHOTS_MAX = 3

# Бюджет НЕУДАЧНЫХ дропов на одну сессию (страницу). Исчерпали — стоп и
# отдаём страницу вызывающему на перезагрузку. Смысл: не жчь сессию до
# заглушки, а честно вернуть управление, пока ещё можно перезагрузить.
DROP_BUDGET = 4

# Аварийный выключатель guard'а «сессия сожжена» (OZON_PUZZLE_NO_BLOCK_GUARD=1).
# Нужен, когда guard подозрителен: пазл на экране, а кусок не двигается —
# проще снять проверку и смотреть, чем гадать по логу. Симптом, который он
# снимает, случился 2026-10-01 из-за слишком широких маркеров _stub_page.
_NO_BLOCK_GUARD = os.environ.get("OZON_PUZZLE_NO_BLOCK_GUARD", "0") == "1"

# Ставился ли перехватчик координат лунки на текущей странице (список как
# изменяемый синглтон, чтобы reset между шардами не ломал spy на живой
# странице).
_SPY_OK: list = [False]


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


def _shot_order(cands) -> list:
    """Порядок выстрелов по списку кандидатов (чистая функция).

    Только топ-кандидат со своими микросдвигами ±3: дискретизация детектора
    даёт ошибку ~±1-4px, и валидация 2026-09-22 дала cands[0] в 36/59
    эпизодах, ±3 — ещё в 21/59.

    Дальше кандидаты НЕ перебираются намеренно: каждый дроп с неверной целью
    вызывает перезагрузку пазла, а после нескольких промахов сессия сгорает
    в заглушку «нет соединения» (наблюдение пользователя с экрана 2026-10-01).
    Лишние выстрелы не повышают шанс — они уничтожают сессию целиком.
    """
    if not cands:
        return []
    top, top_sc = cands[0]
    out = []
    seen = set()
    for off in (0, -3, 3):
        x = int(round(top + off))
        if x < 1 or x in seen:
            continue
        seen.add(x)
        out.append((x, top_sc))
    return out[:SHOTS_MAX]


def _js_slot_target(page, b_r) -> Optional[dict]:
    """Цель из перехваченных вызовов капты, если координата уже известна.

    Приоритетнее детекторов: координата берётся из кода капчи, а не
    угадывается по фотографическому фону (CV здесь принципиально ненадёжен:
    79 живых кадров дали 27 всегда успех / 40 всегда провал, переупорядочивание
    кандидатов не помогло). Требует, чтобы координата была МАСШТАБИРОВАНА в
    css-пиксели фона: проверяем, что результат попадает в ширину бокса
    фона, иначе отбрасываем (иначе получим координату в натуральном
    разрешении CDN-картинки).
    """
    d = _slot_from_js(page)
    if not d:
        return None
    x = d.get("x")
    if x is None or b_r is None or len(b_r) != 4:
        return None
    try:
        x = float(x)
    except Exception:
        return None
    if not (1.0 <= x <= float(b_r[2]) - 2.0):
        return None
    return {"bx": int(round(x)), "src": d.get("src"), "hits": d.get("hits")}


def _verdict(page) -> str:
    """Вердикт снятия капты: 'done' | 'unblocked' | 'no'.

    Признаки резолвятся через ГЛОБАЛЫ ЭТОГО МОДУЛЯ (не через puzzle_dom),
    чтобы юнит-тесты подменяли puzzle._captcha_done / puzzle._still_challenge
    / puzzle._stub_page / puzzle._page_has_content и влияли на боевое
    поведение solve_puzzle.
    """
    return _classify(_captcha_done(page), not _still_challenge(page),
                     _stub_page(page), _page_has_content(page))


def _await_verdict(page, seconds: float = _SOLVED_GRACE_S,
                   reject_after: float = _REJECT_FAST_S, left: float = 0.0) -> str:
    """Поллить вердикт снятия, различая «отвергнуто» и «ждём рендер».

    Полный grace нужен только когда челлендж ИСЧЕЗ: дроп мог быть верным, и мы
    ждём, пока страница отдаст контент (наблюдение: заглушка вылезает через
    ~30s, контент тоже медленно). Если челлендж ЖИВ — дроп уже отвергнут, и
    ждать бессмысленно: выходим после reject_after.

    Без этого разделения каждый отвергнутый выстрел стоил полные 12s, семь
    выстрелов = 84s, и solve упирался в таймаут хантера (90s): live7 дал два
    провала по 96-97s с reason=timeout вместо нормального отказа по кандидатам.

    left — остаток бюджета решения: подрезаем оба ожидания, иначе вердикт
    дописывает к израсходованному бюджету ещё 12 с.
    """
    if left > 0.0:
        seconds = max(0.0, min(seconds, left))
        reject_after = max(0.0, min(reject_after, left))
    t0 = time.monotonic()
    while True:
        st = _verdict(page)
        if st != "no":
            return st
        alive = _still_challenge(page)
        if alive:
            if time.monotonic() - t0 >= reject_after:
                return "no"
        elif time.monotonic() - t0 >= seconds:
            return "no"
        time.sleep(0.4)


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


def _cand_attempt_fields(diag: dict) -> dict:
    """Поля паспорта для JSONЛ-журнала попыток (машинночитаемый разбор).

    Паспорт обязан попадать не только в текстовый лог: статистику заходов (сколько
    времени заняло чтение позиции против ввода, чем закончил�� заход) иначе можно
    получить только парсингом лога, а это ручной труд на каждый разбор.
    Отдельная функция, чтобы набор полей было видно и покрывалось тестом: при
    случайном удалении поля тест упадёт.
    """
    return {"t_read_s": diag.get("t_read_s"),
            "t_move_s": diag.get("t_move_s"),
            "t_drag_s": diag.get("t_total_s"),
            "iters": diag.get("iters"),
            "stop": diag.get("stop"),
            "final_err": diag.get("final_err"),
            "k": diag.get("k")}


def _slot_ok(x: int, bg_w: int, piece_w: int) -> bool:
    """Может ли лунка начинаться в x? Чистая функция — ради юнит-тестов.

    Границы:
      * x >= 1 — лунка не начинается на первом пикселе (там край кадра, и дроп
        бессмысленен: пиксель-в-пиксель попасть нельзя);
      * x + piece_w <= bg_w — кусочек не встанет правее фона, значит лунка,
        начинающаяся за этой границей, физически невозможна.

    Зачем: кандидат вне границ = мусорный дроп, который тратит попытку И вызывает
    реролл капчи, из-за чего теряются остальные кандидаты того же захода.
    Детекторы по маске такое не возвращают, но кандидат по форме/краям может.
    """
    x_max = max(1, bg_w - piece_w) if piece_w > 0 else bg_w
    return 1 <= x <= x_max


def _verdict_reason(okc: bool, still: bool, verdict: str) -> str:
    """Причина вердикта на человеческом языке — одна строка для паспорта.

    Главная ценность: РАЗДЕЛИТЬ «промахнулись» и «попали, но не засчиталось».
    Это разные болезни с разными лекарствами: первое — детектор/калибровка,
    второе — антибот или подмена заглушкой. Раньше оба случая давали одинаковое
    `verdict=no`, и в логе не было видно, что кусок вставал в лунку с промахом
    0.07 px (живой замер 2026-10-02).
    """
    err = _drag_diag().get("final_err")
    moved = _drag_diag().get("moved_px")
    if verdict == "done":
        return "капча снята, доступ открыт"
    if verdict == "unblocked":
        return "капча снята, содержимое на месте"
    if verdict != "no":
        return f"вердикт={verdict}"
    parts = []
    if moved is not None and abs(moved) <= 1.0:
        parts.append("кусок НЕ ДВИНУЛСЯ (мышь не зацепила цель)")
    if err is not None and abs(err) > 2.0:
        parts.append(f"промах по позиции {err:+.1f}px")
    elif err is not None:
        parts.append(f"позиция верна (err={err:+.2f}px, в допуске), "
                     f"но Ozon не засчитал")
    if still:
        parts.append("капча осталась в DOM")
    else:
        parts.append("капча исчезла из DOM, но captchaDone/контента нет — "
                     "похоже на подмену заглушкой или перерисовку капчи; "
                     "это НЕ бан по IP")
    if okc:
        parts.append("кусок пропал из DOM после дропа")
    return "; ".join(parts)


def _drag_diag() -> dict:
    """Паспорт последнего захода драга (пусто, если драга не было)."""
    return last_drag_diag()


def _note_seen() -> str:
    """«2/3» — номер попытки этого вызова и сколько их всего разрешено."""
    return f"{attempts()}/{PUZZLE_MAX_ATTEMPTS}"


def solve_puzzle(page, attempts: int = PUZZLE_MAX_ATTEMPTS,
                 timeout_s: float = PUZZLE_BUDGET_S, on_event=None) -> bool:
    """Решить слайдер-пазл на странице. True — пазл снят.

    Живой метод: прятки кусочка + силуэтный детектор Вариант A по кадру фона.
    Фон сначала берётся из CDN src (надёжно, независимо от скролла/свёрнутого
    окна), при неудаче — фолбэк на скриншот окна. При любой ошибке (нет DOM,
    нет картинок, не нашлась выемка, обрыв CDP) — False. Счётчик неудач растёт
    за каждый неудачный заход.

    timeout_s — жёсткий лимит на всё решение: кандидаты прекращаются,
    неснятый пазл возвращается False. По умолчанию PUZZLE_BUDGET_S (60 с), а
    НЕ 0: бюджет нужен не «охотнику», а боевому сбору, где зависшая вкладка
    растягивала попытку на 178 с (замер 2026-10-02), а новая вкладка решала
    ту же капчу за 10 с. Передать 0 осознанно можно только точечному свипу.

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

    def _left() -> float:
        """Остаток бюджета решения (0.0 — бюджет выключен/исчерпан)."""
        return (timeout_s - (time.monotonic() - _t0)) if timeout_s > 0 else 0.0

    def _fail(reason: str, **kw) -> bool:
        """ЕДИНСТВЕННАЯ точка «попытка не удалась»: паспорт + счётчик + False.

        Инвариант KPI attempts() == failures() + solved() держится только если
        каждый выход из solve_puzzle после _note_attempt() помечен РОВНО
        одним исходом. Выходов было девять, и четыре из них возвращали False
        без failures += 1: таймаут внутри цикла ожидания src, on_event=empty,
        «timeout» от _drag_bx и исчерпание бюджета дропов. Счётчик попыток
        рос, доля успеха считалась от несогласованных чисел — в живом логе это
        читалось как «пазл не решён за 0 попыток».
        """
        _attempt("solve", ok=False, reason=reason, **kw)
        _PUZZLE_FAILURES[0] += 1
        return False

    def _ok(verdict: str, **kw) -> bool:
        """ЕДИНСТВЕННАЯ точка «попытка удалась»: паспорт + счётчик + True."""
        _attempt("solve", ok=True, via=verdict, **kw)
        note_solved()
        return True

    # Неудачные ДРОПЫ за ОДИН solve_puzzle. Прежде бюджет дропов сравнивался с
    # глобальным _PUZZLE_FAILURES, который (а) не инкрементится на возвратах
    # "reject" из _drag_bx — то есть на провале дропа, и (б) копится за всю
    # сессию, поэтому после 4 провалов подряд любой следующий пазл отказывался
    # двигать кусок вообще, даже имея полный бюджет времени. Теперь счётчик
    # локальный, а в паспорт попытки идёт `drops`.
    drops = 0

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
            return _fail("geom_mismatch", **g_metrics)

    for _ in range(max(1, int(attempts))):
        _note_attempt()
        if _expired():
            note_timeout()
            log.warning(f"solve: бюджет времени ({timeout_s:.0f}с) исчерпан "
                        f"до захвата кадра — вкладка, вероятно, зависла; "
                        f"отдаём в лестницу восстановления "
                        f"(FAB-reload → new_tab → recycle)")
            return _fail("timeout")
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
                note_timeout()
                return _fail("timeout_wait_src")
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
                    return _fail(_reason)
        if not info or not info.get("bg") or not info.get("puzzle"):
            # пазл уже снят (переход на url captchaDone=true) — успех; иначе
            # просто нет пазла на странице — неудачная попытка (счётчик
            # failures растёт: инвариант attempts == failures + solved).
            if on_event is not None:
                on_event({"kind": "empty",
                          "seconds_s": round(time.monotonic() - _t0, 1)})
            done = _captcha_done(page)
            return _ok("url_marker") if done else _fail("no_puzzle")
        try:
            psrc = (info["puzzle"] or {}).get("src")
            if not psrc:
                return _fail("no_puzzle_src")
            # Сессия уже сожжена: заглушка «Похоже, нет соединения» стоит в DOM
            # ОДНОВРЕМЕННО с пазлом (живой замер 2026-10-01: 6 из 8 дропов
            # дали st_h1='Похоже, нет соединения', st_blen=95 при живом пазле).
            # Драгами это не лечится — Ozon отвергнет любой кусок, а мы ещё и
            # сожжём попытки. Честно отказываем сразу, чтобы вызывающий ушёл в
            # reload/переработку сессии, а не в длинный бесполезный свип.
            if _stub_page(page) and not _NO_BLOCK_GUARD:
                log.warning("solve: сессия уже заблокирована (заглушка в DOM "
                            "при живом пазле) → без дропов")
                if on_event is not None:
                    on_event({"kind": "blocked", "ctx": "stub_with_puzzle",
                              "seconds_s": round(time.monotonic() - _t0, 1)})
                _solve_diag(page, info, "session_blocked",
                            detail="stub present while puzzle alive")
                return _fail("session_blocked")
            # Живой композитор — условие любой съёмки кадра. На свёрнутом окне
            # Chromium под Windows не отдаёт кадры вообще: скриншот виснет до
            # таймаута (замер 2026-10-08 — три раза подряд по 6с), а
            # captureScreenshot(fromSurface=false) отдал 210с и плоскую заливку.
            # Session по умолчанию окно сворачивает, поэтому возвращаем его
            # ЗДЕСЬ, до детекторов: композитору нужно время проснуться, и весь
            # цикл съёмок (8 кадров) пойдёт уже по живому окну. Иначе 40-45с
            # таймаутов съёмки съедали PUZZLE_BUDGET_S=60 до первого драга —
            # это и есть боевой «исчерпан бюджет времени (60с)» при drops=0.
            if ensure_render_window(page):
                _attempt("win", state="restored")
            cdn = _bg_from_cdn(info)
            shot = None
            grip = None
            # Перехват координаты из кода капчи. Ставим сразу, до дропов: после
            # каждого неверного дропа капча перерисовывается и инициализация
            # вызывается заново, так что запас обновляется сам. Дальше каждый
            # цикл сначала спрашивает _js_slot_target и, если координата есть,
            # стреляет по ней, а не по догадкам детекторов.
            if not _SPY_OK[0]:
                _SPY_OK[0] = _slot_spy_install(page)
                if _SPY_OK[0]:
                    log.debug("solve: slot-spy установлен")
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
                va_cands = _detect_slot_topk(bg_l, mask, yrow, k=3)
                # ОСНОВНОЙ ДЕТЕКТОР: локализация лунки по форме силуэта куска
                # (2026-10-01, замер 87% top-1 против 20-27% у колоночных).
                # yrow ОБЯЗАТЕЛЕН: функция берёт полосу em[y0:y0+mh], поэтому
                # передача 0 означала бы верх картинки. Замер: с yrow из
                # кадра 13/15 = 87%, с yrow=0 — 4/15 = 27%.
                shape_cands = _detect_slot_shape(bg_l, mask, yrow, k=3)
                # ЖИВОЙ КАДР ФОНА — ТОЛЬКО ЕСЛИ ОН МОЖЕТ ИЗМЕНИТЬ РЕШЕНИЕ.
                #
                # Порядок детекторов в _solve_cands: shape идут первыми со
                # сдвигом +_SHAPE_SCORE_BIAS (1e6), а шкалы колоночных
                # детекторов — суммы энергий/доли пика, то есть ≤ ~510 при
                # байтовых значениях diff. Следовательно, когда shape_cands
                # непуст, cands[0] = shape_cands[0] НЕЗАВИСИМО от результатов
                # diff/edges, а _shot_order стреляет только в cands[0] и его
                # ±3. Живой кадр и silR в этом случае не могут изменить ни
                # одного выстрела.
                #
                # А стоят они 8 скриншотов (5 с таймаут каждый) плюс ~40
                # evaluate — на подвиревшей вкладке это треть-половина
                # PUZZLE_BUDGET_S до того, как начался хоть один drag. В живом
                # логе 2026-10-07 отведённый на ожидания остаток бюджета и
                # кончился на stop=deadline_micro при iters=2. Поэтому кадр
                # снимается лениво: если shape нашёл лунку, дифф-детекторы и
                # рендерная маржа не платят за съёмку; если не нашёл — платят,
                # и решение остаётся прежним.
                shot = None
                diff_cands = []
                edge_cands = []
                diff_mode = "skipped_shape"
                silL = _sil_left(mask)
                silR = None
                sil_use = silL
                if not shape_cands:
                    shot = _shot_bg_clean(page, info)
                    diff_mode = "none"
                    if shot is not None and shot["bg_l"].shape == bg_l.shape:
                        diff_cands, diff_mode = _detect_slot_diff(
                            shot["bg_l"], bg_l, mask, yrow, k=3)
                        edge_cands = _detect_slot_edges(shot["bg_l"], bg_l,
                                                        mask, yrow, k=3)
                    # Диагноз 2026-09-20: edges возвращает левую ГРАНЬ лунки в
                    # фоне, а цель драга — левый край img куска. У куска слева
                    # прозрачная маржа silL (6-21px), поэтому img.left должен
                    # быть на silL меньше грани лунки. Валидация на 18
                    # diag-кадрах: edges-silL попадает в нужную лунку 14/17
                    # (±1px) — приоритет скорректированным edges.
                    try:
                        silR = _shot_piece_margin(page, info)
                    except Exception as e:
                        log.warning(f"solve silR failed, fallback silL: {e}")
                    sil_use = silR if silR is not None else silL
                cands = _solve_cands(mask, edge_cands, diff_cands,
                                     va_cands, sil_use,
                                     shape_cands=shape_cands)
                _attempt("det", shape=[x for x, _ in shape_cands],
                         diff_mode=diff_mode)
                log.debug(f"solve cdn bg={bg_l.shape} piece={mask.shape} p_r={p_r} "
                          f"yrow={yrow} edges={[x for x, _ in edge_cands]} "
                          f"diff={[x for x, _ in diff_cands]} "
                          f"va={[x for x, _ in va_cands]} cands={cands} "
                          f"diff_mode={diff_mode} "
                          f"shot_reason={_shot_bg_reason() or 'not_shot'} "
                          f"silL={silL} silR={silR}")
                _attempt("det", diff=[x for x, _ in diff_cands],
                         va=[x for x, _ in va_cands],
                         edges=[x for x, _ in edge_cands],
                         cands=[x for x, _ in cands],
                         silL=silL, silR=silR, sil_use=sil_use,
                         diff_mode=diff_mode,
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
                    return _fail("no_bg_shot")
                bg_l = shot["bg_l"]
                b_r = shot["b_r"]
                p_r = shot["pz_r"]
                if len(p_r) != 4 or len(b_r) != 4:
                    return _fail("bad_rects")
                mask = _silhouette(_fetch(psrc).read(), p_r[2], p_r[3])
                yrow = int(round(p_r[1] - b_r[1]))
                cands = _detect_slot_topk(bg_l, mask, yrow, k=3)
                _PBG_STATS[0] = (float(bg_l.mean()), float(bg_l.std()),
                                 float(bg_l.max()), float(bg_l.min()))
            if not cands:
                fullshot = None
                try:
                    fullshot = _page_shot(page, animations="disabled")
                except Exception:
                    pass
                log.warning("solve: детекторы пусты → no_slot")
                _solve_diag(page, info, "no_slot",
                            detail=(f"bg={bg_l.shape} piece={mask.shape} yrow={yrow} "
                                    f"reason={_shot_bg_reason()}"),
                            bg_stats=_PBG_STATS[0], bg=bg_l,
                            shot=shot.get("orig") if shot else None,
                            fullshot=fullshot)
                return _fail("no_slot")
            tried = set()

            def _drag_bx(x: int, score0: float):
                """Один драг к bx=x. Возвращает статус."""

                # Границы кандидата: лунка не бывает на первом пикселе и не может
                # начинаться правее (фон − кусок). Кандидат вне границ — это мусорный
                # дроп: он тратит попытку и вызывает реролл капчи, из-за чего
                # теряются остальные кандидаты захода.
                bg_w = bg_l.shape[1]
                piece_w = mask.shape[1] if mask is not None else 0
                if x in tried or not _slot_ok(x, bg_w, piece_w):
                    _attempt("solve", note="cand_out_of_range", bx=x,
                             score=score0, bg_w=bg_w, piece_w=piece_w)
                    log.debug(f"solve: кандидат bx={x} вне фона "
                              f"(допустимо 1..{max(1, bg_w - piece_w)}, "
                              f"фон {bg_w}, кусок {piece_w}) — пропускаю "
                              f"без дропа")
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
                    return "timeout"
                # Двигался ли кусок вообще — отдельный вопрос от «попала ли
                # лунка». no_move означает поломку захвата (мышь не зацепила),
                # а не промах детектора: различать обязательно, иначе обе
                # причины выглядят одинаково и в логе, и на экране.
                _moved = _last_move()
                if _expired():
                    return "timeout"
                # Вердикт снятия — по ЛЮБОМУ из двух признаков. Прежний ждал
                # только captchaDone=true в URL, чего текущее поколение капчи
                # (v10, кусочек без трека) не делает НИКОГДА: после верного
                # дропа челлендж снимается из DOM, выдача перезапрашивается
                # на месте. Живой лог 2026-09-29: 64 дропа легли в лунку
                # (|err|≤1px), успехом засчитаны 3 — 61 верное попадание
                # выброшено, плюс 24s мёртвого ожидания URL на кандидата.
                # Оба ожидания подрезаются остатком бюджета решения: ещё 12с
                # после израсходованных 55с — это ровно тот разрыв между
                # «бюджет 60с» в настройке и 178с в логе.
                verdict = _await_verdict(page, _SOLVED_GRACE_S,
                                         left=_left())
                _d = _drag_diag()
                # Паспорт захода идёт и в машиночитаемый JSONL (_attempt), иначе
                # разбор требует парсинга текстового лога.
                _attempt("cand", bx=x, score=score0, okc=bool(okc),
                         still=still, verdict=verdict, moved_px=_moved,
                         target_vx=round(target_vx, 1),
                         **_cand_attempt_fields(_d),
                         **_page_state(page))
                log.debug(f"solve drag okc={okc} still_challenge={still} "
                          f"verdict={verdict}")
                # ПАСПОРТ ПОПЫТКИ (2026-10-02). Одна строка, по которой видно
                # ВСЁ про заход, без чтения десятков debug-строк. Раньше провал
                # выглядел как голое `verdict=no`, и разбор стоил часа: 11 с на
                # шаг читалось как проблема детектора, хотя промах был 0.07 px.
                _why = _verdict_reason(okc, still, verdict)
                _tail = (f"bx={x} score={score0:.3g} target={target_vx:.0f} "
                         f"err={_d.get('final_err')} k={_d.get('k')} "
                         f"k_rem={_d.get('k_rem')} "
                         f"moved={_d.get('moved_px')}/{_moved} "
                         f"шагов={_d.get('iters')} stop={_d.get('stop')} "
                         f"settled={_d.get('settled')} "
                         f"t_drag={_d.get('t_total_s')}с "
                         f"t_read={_d.get('t_read_s')}с "
                         f"t_move={_d.get('t_move_s')}с "
                         f"чтений={_d.get('probe_reads')} "
                         f"попытка={_note_seen()}")
                if verdict in ("done", "unblocked"):
                    log.info(f"solve: РЕШЁН ({verdict}) — {_why}; {_tail}")
                else:
                    log.warning(f"solve: НЕ РЕШЁН — {_why}; {_tail}")
                if verdict in ("done", "unblocked"):
                    return "solved"
                # Не снят. Челлендж исчез, но контента нет: это либо подмена
                # заглушкой, либо капча успела перерисоваться. Различаем
                # ТОЛЬКО фактом смены капчи: раньше любой such случай рвал
                # свип как reroll, не проверив src, и мы выбрасывали оставшиеся
                # кандидаты. Живой замер 2026-10-01 (live8): 3 дропа из 11 были
                # именно такими (still=False, verdict=no) — свип рвался, хотя
                # капча могла остаться той же и следующие выстрелы были годны.
                if not still:
                    if _captcha_src_changed(page, info):
                        if on_event is not None:
                            on_event({"kind": "reroll", "ctx": "confirm_failed"})
                        return "reroll"
                    return "reject"
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

            # ПОРЯДОК ВЫСТРЕЛОВ.
            #
            # ВАЖНО (наблюдение пользователя с экрана 2026-10-01): «если
            # начинаешь двигать ползунок, но потом его бросаешь, происходит
            # перезагрузка пазла». То есть КАЖДЫЙ дроп с неверной целью
            # вызывает реролл (новый фон/кусок), и оставшиеся кандидаты
            # протухают. Плюс «после нескольких неудачных попыток вылезает
            # заглушка» - у сессии есть бюджет промахов.
            #
            # Отсюда стратегия: МЕНЬШЕ дропов, но ТОЧНЕЕ. Свип по всем
            # кандидатам (SHOTS_MAX>1) не просто бесполезен, он ВРЕДЕН -
            # выжигает сессию. Поэтому пробуем cands[0] с микросдвигами
            # ±3 (дискретизация детектора ~±1-4px) и, если капча НЕ сменилась
            # (редко), следующий кластер. Бюджет промахов на сессию держит
            # DROP_BUDGET: исчерпали - стоп и сигнал перезагрузки, а не жжение.
            shots = _shot_order(cands)
            r = "skip"
            js_hit = _js_slot_target(page, b_r)
            if js_hit:
                # Точная цель из кода капты — она важнее любой догадки.
                log.debug(f"solve: цель из JS {js_hit['src']} bx={js_hit['bx']} "
                          f"(hits={js_hit.get('hits')})")
                _attempt("det", js_bx=js_hit["bx"], js_src=js_hit["src"],
                         cands=[c[0] for c in cands])
                shots = [(js_hit["bx"], float("inf"))]
            for bx, sc in shots:
                r = _drag_bx(bx, sc)
                if r == "solved":
                    # Через _ok, а не голый True: иначе note_solved() не
                    # вызывается, и метрика solve-rate в отчёте и в
                    # recovery_telemetry демона всегда 0 (падал инвариант
                    # attempts == failures + solved).
                    #
                    # Порядок ИМЕННО такой: сначала _ok (он инкрементит
                    # solved), потом лог. Наоборот сводка печатала счётчик ДО
                    # инкремента, и на каждом успехе строка лгала «решено=0»
                    # при фактическом снятом пазле (живой прогон 2026-10-08,
                    # 5 из 5 пазлов: «пазл снят за 1/3 попытки; попыток=1
                    # решено=0»). Метрика в отчёте при этом была верной —
                    # врала только строка лога, но именно её читают при разборе.
                    _ok("solved", bx=bx, drops=drops)
                    log.info(f"solve: ИТОГ — пазл снят за {_note_seen()} "
                             f"попытки; {puzzle_summary()}")
                    return True
                if r == "timeout":
                    # Бюджет времени исчерпан: значит вкладка зависла (медленный
                    # CDP), а не капча сложная. Логируем явно — иначе «не решён»
                    # в логе неотличимо от «солвер не попал». Замер 2026-10-02:
                    # без этого бюджета одна попытка жгла 178 с, а новая
                    # вкладка решала ту же капчу за 10 с.
                    note_timeout()
                    log.warning(f"solve: ИТОГ — исчерпан бюджет времени "
                                f"({timeout_s:.0f}с), вкладка, вероятно, "
                                f"зависла; отдаём в лестницу восстановления "
                                f"(FAB-reload → new_tab → recycle); "
                                f"{puzzle_summary()}")
                    return _fail("timeout", drops=drops)
                # «reject» и «reroll» — оба означают, что дроп состоялся и
                # не засчитан. Считаем их в бюджет дропов: прежний код смотрел
                # в глобальный _PUZZLE_FAILURES, который на них НЕ растёт, то
                # есть бюджет дропов на провале не срабатывал никогда, а на
                # СЛЕДУЮЩЕМ пазле блокировал движение куска при полном бюджете
                # времени (живой лог 2026-10-07: «пазл не решён за 0 попыток»).
                if r in ("reject", "reroll"):
                    drops += 1
                    if drops >= DROP_BUDGET:
                        log.warning(f"solve: исчерпан бюджет дропов "
                                    f"({DROP_BUDGET}) на этот пазл — стоп, "
                                    f"сессия сгорит")
                        if on_event is not None:
                            on_event({"kind": "budget", "drops": drops})
                        return _fail("drop_budget", drops=drops)
                if r == "reroll":
                    break
            _solve_diag(page, info, "all_cands_failed",
                        detail=f"cands={cands} shots={shots}")
            _attempt("solve", ok=False, cands=[c[0] for c in cands],
                     shots=[b for b, _ in shots], drops=drops)
            # Исход ЭТОГО круга пишется здесь, на continue: попытка с
            # исчерпанными кандидатами завершена и измерима, а следующий круг
            # начинает новую с новым захватом кадра. Без этой пары «попытка +
            # провал» итерация уходила без исхода, а следующая записывала
            # провал уже на себя — и attempts != failures + solved на каждом
            # цикле с рероллом капты.
            _PUZZLE_FAILURES[0] += 1
            continue
        except Exception as exc:
            _solve_diag(page, info, "exception",
                        detail=f"{type(exc).__name__}: {exc}")
            return _fail("exception", drops=drops,
                         error=f"{type(exc).__name__}: {exc}")

    # ХВОСТ ВНЕ ЦИКЛА ПОПЫТОК — это была настоящая ошибка отступов: хвост стоял
    # на уровне тела цикла, а последний оператор в нём (`continue`) его
    # перепрыгивал. То есть на пути «все круги ушли на continue» функция
    # просто падала с конца и возвращала None — falsy, но не False, без
    # failures += 1. В логе это и читалось как «пазл не решён за 0 попыток».
    #
    # Дальше: без этого хвоста solve_puzzle тоже возвращал None (falsy без
    # ошибки) — счётчик failures не рос и инвариант KPI attempts ==
    # failures + solved не сходился. Прежний `_grace_resolve(page, 18.0)` ждал
    # URL-маркер, которого текущее поколение капчи не ставит: 18s пустоты на
    # каждый проваленный инцидент (наблюдение с экрана 2026-09-30: «пазл
    # стоит, 20-30с ничего, потом перезагрузка»). Вердикт снятия теперь
    # полностью живёт в _drag_bx (_await_verdict).
    #
    # Счётчики здесь НЕ трогаем: каждый круг цикла уже записал свою пару
    # «попытка + провал» на continue выше, и повторная запись разъехала бы
    # инвариант. Единственное исключение — капта снялась ПОСЛЕ последнего
    # круга: тогда её провал честно отменяется, иначе в KPI будет провал
    # при фактически снятой капче.
    if _captcha_done(page):
        _PUZZLE_FAILURES[0] -= 1
        # _ok до лога — тот же порядок, что в ветке solved: сводка читает
        # счётчик solved, который инкрементит именно _ok.
        _ok("late_url", drops=drops)
        log.info(f"solve: ИТОГ — капча снята поздно (URL-маркер); "
                 f"{puzzle_summary()}")
        return True
    log.warning(f"solve: ИТОГ — НЕ решён за {_note_seen()} попытки "
                f"(капча ушла, но доступа нет), дропов={drops}; "
                f"{puzzle_summary()}")
    return False