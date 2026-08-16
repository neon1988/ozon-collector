"""Офлайн-дайджест из raw (scripts/digest_from_raw.py): фильтр --pids."""
from __future__ import annotations

import json
import os

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _make_raw(pid: str, title: str) -> dict:
    with open(os.path.join(TESTS_DIR, "raw_fixtures", "shape_minimal.json"),
              encoding="utf-8") as f:
        raw = json.load(f)
    raw["collector_version"] = "1.14.0"
    raw["pid"] = pid
    raw["product_url"] = f"https://www.ozon.ru/product/{pid}"
    raw["reviews_url"] = f"https://www.ozon.ru/product/{pid}?tab=reviews"
    raw["product"] = dict(raw["product"], title=title)
    raw["product"]["characteristics"] = dict(
        raw["product"]["characteristics"], Артикул=pid)
    return raw


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    for pid, title in [
        ("3627230434", "Xiaomi REDMI Buds 8 Active, черные"),
        ("9999999999", "Товар без фильтра"),
    ]:
        with open(d / f"{pid}_raw.json", "w", encoding="utf-8") as f:
            json.dump(_make_raw(pid, title), f, ensure_ascii=False)
    return d


def test_digest_no_filter_all_pids(data_dir, capsys):
    from scripts.digest_from_raw import main
    assert main(["--data-dir", str(data_dir)]) == 0
    out = capsys.readouterr().out
    assert "pid'ов: 2" in out
    assert "3627230434" in out
    assert "9999999999" in out


def test_digest_pids_filter(data_dir, capsys):
    from scripts.digest_from_raw import main
    assert main(["--data-dir", str(data_dir), "--pids", "3627230434"]) == 0
    out = capsys.readouterr().out
    assert "pid'ов: 1" in out
    assert "3627230434" in out
    assert "9999999999" not in out


def test_digest_pids_file(data_dir, tmp_path, capsys):
    from scripts.digest_from_raw import main
    pf = tmp_path / "pids.txt"
    pf.write_text("https://www.ozon.ru/product/9999999999/\n", encoding="utf-8")
    assert main(["--data-dir", str(data_dir), "--pids-file", str(pf)]) == 0
    out = capsys.readouterr().out
    assert "pid'ов: 1" in out
    assert "9999999999" in out
    assert "3627230434" not in out
