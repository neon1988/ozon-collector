"""Чтение DOM пазла и признаков состояния челленджа.

Только page.evaluate(): есть ли пазл, есть ли капча-заглушка, решено ли,
и где на странице лежит слайдер (трек + хэндл). Ни CV, ни мыши.
_firefox_page — обход только для диагностических аргументов.

Перенесено из puzzle.py без изменения кода (механический сплит).
"""

import time
from typing import Optional


def is_puzzle(signature: Optional[str]) -> bool:
    """Сигнатура challenge указывает на слайдер-пазл (а не FAB-заглушку)."""
    return bool(signature and ("пазл" in signature or "ползунок" in signature))


def looks_like_puzzle(page) -> bool:
    """Есть ли на странице элементы слайдер-пазла (фон + кусочек) по DOM.

    Не полагается на текстовую сигнатуру title/body: если detect_challenge
    вернул общий анти-бот маркер (например 'antibot captcha'), но в DOM есть
    фон и кусочек — это пазл, который можно решить, а не глухой FAB-reload.
    """
    info = _capture_info(page)
    return bool(info and info.get("has"))


def is_captcha_shell(page) -> bool:
    """Капча v10 «скелетом»: контейнер/слайдер уже в DOM, но картинки ещё с
    пустым src и нулевым размером (captcha_v10.js проставляет их через
    0.3–2s), поэтому looks_like_puzzle() пока False. По скелету это РЕШАЕМЫЙ
    пазл — классификатор должен звать solver, который дождётся src поллингом.
    В has не влияет (не ломает _still_challenge после снятия пазла)."""
    info = _capture_info(page)
    return bool(info and info.get("shell"))
def _capture_info(page) -> Optional[dict]:
    """Извлечь элементы пазла (фон, кусочек, слайдер) из DOM страницы."""
    try:
        return page.evaluate(
            """() => {
              const out = {bg:null, puzzle:null, slider:null, has:false};
              document.querySelectorAll('img').forEach(el => {
                const r = el.getBoundingClientRect();
                const c = el.className || '';
                if (c.includes('image') && r.width > 200)
                  out.bg = {src: el.currentSrc||el.src,
                            r:[r.x,r.y,r.width,r.height]};
                if (c.includes('puzzle'))
                  out.puzzle = {src: el.currentSrc||el.src,
                                r:[r.x,r.y,r.width,r.height]};
              });
              document.querySelectorAll(
                '[class*=slider],[class*=slide],[class*=handler],[class*=track],[class*=drag],[class*=ckbox]'
              ).forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 30 && r.height > 15)
                  out.slider = {r:[r.x,r.y,r.width,r.height]};
              });
              out.shell = !!(document.querySelector(
                '#captcha-container, #captcha, #slider-container, img#image, img#puzzle'));
              out.has = !!(out.bg && out.puzzle);
              return out;
            }""")
    except Exception:
        return None


def _still_challenge(page) -> bool:
    """Остался ли на странице ЛЮБОЙ анти-бот челлендж (пазл, FAB, VPN-блок).

    Не считаем «решённым», если после драга страница оказалась другой
    заглушкой (например, 'выключите vpn'): пазл исчез, но выдача не открыта —
    это НЕ успех, а подмена челленджа.
    """
    if looks_like_puzzle(page):
        return True
    from .collect import detect_challenge
    return bool(detect_challenge(page))


def _captcha_done(page) -> bool:
    """Единственный достоверный признак успешного снятия пазла: URL получил
    параметр captchaDone=true.

    Исчезновение челленджа из DOM НЕ является успехом (подмена FAB/VPN-блоком,
    реролл, обрыв). Валидировано: url уже содержит captchaDone=true, а челлендж
    в DOM живёт ещё ~минуту — маркер именно в URL, не в DOM.
    """
    try:
        href = page.evaluate("() => location.href") or ""
    except Exception:
        return False
    return "captchaDone=true" in href


def _classify(url_done: bool, challenge_gone: bool, stub: bool) -> str:
    """Чистый вердикт снятия капчи из трёх признаков (без DOM).

    'done'      — URL несёт captchaDone=true (окончательный маркер старого
                  поколения капчи со слайдером).
    'unblocked' — челлендж исчез из DOM, страница не заглушка. Это приёмка
                  приёмка текущего поколения (v10, кусочек без трека): после
                  верного дропа капча снимает челлендж и перезапрашивает
                  выдачу НА МЕСТЕ, без навигации, поэтому captchaDone=true в
                  URL не появляется НИКОГДА.
    'no'        — челлендж жив либо страница подменена заглушкой.
    """
    if url_done:
        return "done"
    if not challenge_gone or stub:
        return "no"
    return "unblocked"


def _solved_state(page) -> str:
    """Вердикт снятия одним вызовом (резолвит признаки в этом модуле)."""
    return _classify(_captcha_done(page), not _still_challenge(page),
                     _stub_page(page))


def _await_solved(page, seconds: float = 5.0, step: float = 0.4,
                  probe=None) -> str:
    """Поллить вердикт снятия до 'done'/'unblocked' или до дедлайна.

    probe — необязательный callable вместо _solved_state: вызывающий модуль
    подставляет свою резолюцию признаков (см. _verdict в puzzle.py и
    puzzle_drag.py), чтобы юнит-тесты патчили свои глобалы.
    """
    t0 = time.monotonic()
    while True:
        st = probe() if probe is not None else _solved_state(page)
        if st != "no" or time.monotonic() - t0 >= seconds:
            return st
        time.sleep(step)


def _stub_page(page) -> bool:
    """Страница — заглушка «нет соединения»/«попробуйте ещё раз» (FAB/VPN):
    маркеры в title/теле, а анти-бот оболочки в DOM уже НЕТ. Если челлендж
    исчез, а маркер есть — это подмена, а не разблокировка.
    """
    try:
        doc = (page.evaluate(
            "() => document.title + '||' + "
            "((document.body && document.body.innerText) || '').slice(0, 400)"
        ) or "").lower()
    except Exception:
        return False
    return any(m in doc for m in ("нет соединения", "попробуйте",
                                  "challenge", "antibot"))


def _await_done(page, seconds: float, step: float = 0.5) -> bool:
    """Поллить URL до появления captchaDone=true (или дедлайна)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if _captcha_done(page):
            return True
        time.sleep(step)
    return False


def _confirm_solved(page, grace_s: float = 18.0) -> bool:
    """Вердикт «solved» признаётся ТОЛЬКО при url captchaDone=true.

    Сначала короткий поллинг (редирект обычно приходит за секунды), затем
    _grace_resolve (DOM ещё может держать #captcha ~минуту после снятия).
    """
    if _await_done(page, 6.0):
        return True
    return _grace_resolve(page, grace_s)


def _grace_resolve(page, grace_s: float = 18.0) -> bool:
    """Пазл мог быть решён, но страница долго перестраивается (догрузка после
    снятия + бегунок проверки): DOM ещё секунды держит #captcha, и финальный
    вердикт получается ошибочно «failed» (валидировано на живом пазле
    1011675752: в url уже captchaDone=true, а челлендж в DOM дожил ~минуту).
    Поллим до grace_s: если url получил captchaDone=true — пазл снят (True),
    иначе честный fail (False)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < grace_s:
        time.sleep(2.0)
        if _captcha_done(page):
            return True
    return False


def _find_slider(page) -> Optional[dict]:
    """Найти ручку и трек слайдера по геометрии (у Ozon классы пустые).

    Возвращает {track:[x,y,w,h], handle:[x,y,w,h]} в экранных px или None.
    Трек — самый широкий тонкий DIV ниже фона; ручка — DIV ~40x32 у левого
    края трека (содержит svg-иконку).
    """
    try:
        return page.evaluate(
            """() => {
              let bg = null;
              [...document.querySelectorAll('img')].forEach(i => {
                const r = i.getBoundingClientRect();
                const c = String(i.className||'');
                if (c.includes('image') && r.width>200) bg = r;
              });
              if (!bg) return null;
              const all = [...document.querySelectorAll('div')];
              const belowY = bg.y + bg.height;
              let track=null, handle=null;
              all.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width<=0 || r.height<=0) return;
                if (r.y >= belowY-5 && r.y < belowY+200
                    && r.width>=300 && r.height<=6)
                  if (!track || r.width>track)
                    track=[r.x, r.y, r.width, r.height];
              });
              if (!track) return null;
              const ty = track[1] + track[3]/2;
              all.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width<=0 || r.height<=0) return;
                const cy = r.y + r.height/2;
                if (Math.abs(cy-ty) < 30
                    && Math.abs(r.width-40) <= 15
                    && Math.abs(r.height-32) <= 18)
                  if (!handle || r.x < handle[0])
                    handle=[r.x, r.y, r.width, r.height];
              });
              if (!handle) return null;
              return {track: track, handle: handle};
            }""")
    except Exception:
        return None
