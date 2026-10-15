"""Тесты ozon_collector/constants.py — детект антибот-маркеров в title.

Регрессия ревью 2026-10-06: голый маркер «captcha» искался как подстрока, и
товар со словом captcha в названии уходил в бесконечный retry как челлендж.
"""
from ozon_collector.constants import title_challenge_marker


def test_captcha_requires_word_boundaries():
    assert title_challenge_marker("Игра CaptchaMaster Deluxe") is None
    assert title_challenge_marker("чехол суперcaptchaXXX") is None
    assert title_challenge_marker("Спрей captcha тест") == "captcha"
    assert title_challenge_marker("Captcha") == "captcha"


def test_substring_markers_unchanged():
    assert title_challenge_marker("Antibot captcha") == "antibot captcha"
    assert title_challenge_marker("Похоже, нет соединения") == \
        "похоже, нет соединения"
    assert title_challenge_marker("Подтвердите, что вы не робот!") == \
        "подтвердите, что вы не робот"


def test_regular_title_is_not_challenge():
    assert title_challenge_marker("Наушники Xiaomi Redmi Buds 8") is None
    assert title_challenge_marker("") is None
    assert title_challenge_marker(None) is None
