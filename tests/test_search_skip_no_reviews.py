"""Пропуск товаров без отзывов по плитке выдачи (решение владельца 2026-10-07).

«Не надо заходить даже на страницы, если в выдаче показывает, что у товаров нет
отзывов вообще, нет оценки»: заход на такую карточку стоит минуты (открытие,
прогрев, пагинация пустого блока), а данных в ней всё равно нет.

Проверяем решение на трёх состояниях, которые различает SEARCH_JS:
  tile_no_reviews=True  → в сбор не идёт, попадает в skipped
  tile_no_reviews=False → идёт в сбор
  признака нет (None)   → идёт в сбор: «неизвестно» ≠ «отзывов нет», иначе
                          смена вёрстки Ozon молча вычеркнула бы весь каталог.
"""
from __future__ import annotations

import ozon_collector.search as S


class Sess:
    """Подмена сессии: отдаёт заранее заданные элементы выдачи."""

    def __init__(self, items):
        self.items = list(items)
        self._i = 0
        self.scrolled = 0

    def eval(self, _js):
        self._i += 1
        # первая итерация отдаёт всё, дальше — пусто (докрутка вхолостую)
        return self.items if self._i == 1 else []

    def scroll(self, _by):
        self.scrolled += 1

    def wait(self, _ms):
        pass


def _item(pid, no_reviews):
    return {"pid": pid, "url": f"https://www.ozon.ru/product/{pid}",
            "tile_no_reviews": no_reviews}


def test_products_without_reviews_are_not_opened(monkeypatch):
    """Товар без отзывов не попадает в сбор, но виден как пропущенный."""
    sess = Sess([_item("1111111111", True), _item("2222222222", False)])
    monkeypatch.setattr(S.log, "info", lambda *a, **kw: None)
    links, skipped = S._collect_links(
        sess, limit=10, scan_limit=10, max_scrolls=1)
    assert [it["pid"] for it in links] == ["2222222222"], "без отзывов в сбор нельзя"
    assert [it["pid"] for it in skipped] == ["1111111111"], "пропуск должен быть виден"


def test_collect_returns_skip_list_even_when_empty(monkeypatch):
    """Пустой пропуск — тоже кортеж, а не None: вызывающий ждёт пару."""
    sess = Sess([_item("3333333333", False)])
    monkeypatch.setattr(S.log, "info", lambda *a, **kw: None)
    out = S._collect_links(sess, limit=10, scan_limit=10, max_scrolls=1)
    assert isinstance(out, tuple) and len(out) == 2
    links, skipped = out
    assert len(links) == 1 and skipped == []


def test_unknown_signal_is_kept(monkeypatch):
    """Нет признака (item без ключа) → товар собирается: неизвестно ≠ нет."""
    sess = Sess([{"pid": "4444444444",
                  "url": "https://www.ozon.ru/product/4444444444"}])
    monkeypatch.setattr(S.log, "info", lambda *a, **kw: None)
    links, skipped = S._collect_links(
        sess, limit=10, scan_limit=10, max_scrolls=1)
    assert [it["pid"] for it in links] == ["4444444444"], "неизвестно не отбрасываем"
    assert skipped == []


def test_flag_off_collects_everything(monkeypatch):
    """--include-no-reviews: собираются и товары без отзывов."""
    sess = Sess([_item("5555555555", True), _item("6666666666", False)])
    monkeypatch.setattr(S.log, "info", lambda *a, **kw: None)
    links, skipped = S._collect_links(
        sess, limit=10, scan_limit=10, max_scrolls=1, skip_no_reviews=False)
    assert [it["pid"] for it in links] == ["5555555555", "6666666666"]
    assert skipped == []


def test_skip_does_not_affect_scan_limit(monkeypatch):
    """Пропущенные не занимают scan_limit: иначе докрутка оборвётся раньше."""
    items = [_item(f"{1000 + i}", True) for i in range(5)]
    items += [_item(f"{2000 + i}", False) for i in range(5)]
    sess = Sess(items)
    monkeypatch.setattr(S.log, "info", lambda *a, **kw: None)
    links, skipped = S._collect_links(
        sess, limit=10, scan_limit=6, max_scrolls=1)
    assert len(links) == 5, "все с отзывами собраны, лимит не съеден пропусками"
    assert len(skipped) == 5


def test_skip_logged_so_nedoborne_ne_vygladit_kak_prover(monkeypatch):
    """Число пропущенных пишется в лог: «найдено 300, собрано 40» должно
    читаться как «40 собрано, 200 отброшены осознанно», а не как потеря."""
    logged = []
    monkeypatch.setattr(S.log, "info",
                        lambda msg, *a, **kw: logged.append(str(msg)))
    sess = Sess([_item("7777777777", True), _item("8888888888", False)])
    S._collect_links(sess, limit=10, scan_limit=10, max_scrolls=1)
    assert any("без отзывов" in m for m in logged), "пропуск обязан попасть в лог"