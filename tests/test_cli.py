"""Тесты CLI: парсинг списка артикулов/URL в PID (cli.parse_pids) и дефолты."""
from __future__ import annotations

import cli
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


def test_gather_default_max_age_7d():
    args = cli.build_parser().parse_args(["gather", "--pid", "123"])
    assert args.max_age == "7d"


def test_default_sorts_new_plus_negative():
    """Дефолт sorts: сначала «новые и полезные», потом «сначала низкие оценки»."""
    from cli import DEFAULT_SORTS
    assert DEFAULT_SORTS == "published_at_desc,score_asc"
    for cmd in ("collect", "gather"):
        args = cli.build_parser().parse_args([cmd, "--pid", "123"])
        assert args.sorts == DEFAULT_SORTS
        assert cli._parse_sorts(args.sorts) == ["published_at_desc", "score_asc"]


def test_gather_has_no_min_rating():
    args = cli.build_parser().parse_args(["gather", "--pid", "123"])
    assert not hasattr(args, "min_rating")
