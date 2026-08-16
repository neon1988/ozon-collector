"""Тесты CLI: парсинг списка артикулов/URL в PID (cli.parse_pids)."""
from __future__ import annotations

from cli import parse_pids


def test_parse_pids_bare_and_urls():
    got = parse_pids([
        "3627230434",
        "https://www.ozon.ru/product/3627230434",
        "https://www.ozon.ru/product/xiaomi-redmi-buds-8-3627230434/",
        "/product/3627230434/?tab=reviews",
    ])
    assert got == ["3627230434"]


def test_parse_pids_dedup_keeps_order():
    got = parse_pids(["11111111", "22222222", "11111111"])
    assert got == ["11111111", "22222222"]


def test_parse_pids_skips_invalid():
    got = parse_pids(["abc", "", "https://ozon.ru/not-a-product", "   "])
    assert got == []


def test_parse_pids_slug_with_leading_digits():
    assert parse_pids(["https://www.ozon.ru/product/2000-1234567/"]) == ["1234567"]


def test_parse_pids_file_lines():
    assert parse_pids(["11111111", "https://www.ozon.ru/product/22222222",
                       "33333333"]) == ["11111111", "22222222", "33333333"]
