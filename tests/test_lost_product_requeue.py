"""Регресс «товар без результата теряется навсегда» (merge → демон → очередь).

Цепочка дефекта (2026-10-07):
  merge_shard_results давал потерянному товару pid=None
    → extract_discovered_pids его выбрасывал (`pid is not None`)
      → pid не попадал в progress["discovered_pids"]
        → split_done_pending не видел его ни в done, ни в pending
          → товар исчезал из кампании навсегда: не собирался больше и не
            считался недобором.

Починка: pid потерянного товара известен merge'у из шарда, он и подставляется,
а утрата помечается lost=True (ok остаётся False). Проверяем всю цепочку и
обе границы: lost обязан вернуться в очередь и обязан НЕ попасть в valid.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.key_daemon as k  # noqa: E402
from ozon_collector import parallel as PL  # noqa: E402
from ozon_collector import report as R  # noqa: E402


def _items(n, start=0):
    return [(i, str(1000 + start + i),
             f"https://www.ozon.ru/product/{1000 + start + i}")
            for i in range(n)]


def _ok_payload(worker, indexes):
    return {
        "worker_idx": worker,
        "products": [{"index": i, "result": {"pid": str(1000 + i), "ok": True,
                                             "raw": {"product": {"title": "t"},
                                                     "reviews": []}}}
                     for i in indexes],
    }


def _merged_with_losses(n=3, collected=(1,), parallel=2):
    """combined прогона: n товаров, воркер вернул результаты только по collected."""
    shards = PL.shard_items(_items(n), parallel)
    payloads = [_ok_payload(2, list(collected))]
    products = PL.merge_shard_results(payloads, shards)
    return {"products": products, "found": n, "collected": len(collected)}


# ---- merge_shard_results ------------------------------------------------------

def test_merge_lost_product_keeps_real_pid_and_marks_lost():
    """Товар без результата сохраняет настоящий pid и помечается lost.

    pid=None выбрасывал запись из discovered_pids — товар пропадал навсегда.
    """
    products = _merged_with_losses()["products"]
    assert [p["pid"] for p in products] == ["1000", "1001", "1002"]
    lost = [p for p in products if p.get("lost")]
    assert [p["pid"] for p in lost] == ["1000", "1002"]
    for p in lost:
        assert p["ok"] is False, "утрата результата обязана оставаться неуспехом"
        assert "terminated/failed" in p["error"]
    assert products[1].get("lost") is None, "собранный товар не помечается lost"


def test_merge_all_lost_keeps_every_pid():
    """Все товары без результата (весь пул воркеров упал) — pid у всех есть."""
    shards = PL.shard_items(_items(4), 2)
    products = PL.merge_shard_results([], shards)
    assert [p["pid"] for p in products] == ["1000", "1001", "1002", "1003"]
    assert all(p["ok"] is False and p["lost"] for p in products)


def test_lost_product_named_by_pid_in_digest():
    """Товар без результата опознаваем в артефакте: в блоке его pid.

    Прежний pid=None печатался в дайджесте как «## Товар None»: оператор
    видел, что товар не собран, но не знал КАКОЙ — восстановить прогон
    и добрать его было нечем.
    """
    combined = _merged_with_losses()
    md = R.render_search_digest(combined)
    assert "## Товар 1000" in md and "## Товар 1002" in md
    assert "None" not in md, "в дайджесте не должно быть потерянного pid"


# ---- extract_discovered_pids / extract_ok_pids -------------------------------

def test_lost_pid_reaches_discovered_pids():
    combined = _merged_with_losses()
    assert k.extract_discovered_pids(combined) == ["1000", "1001", "1002"]


def test_lost_pid_never_reaches_ok_pids():
    combined = _merged_with_losses()
    assert k.extract_ok_pids(combined) == ["1001"], "ok=True только у собранного"


def test_lost_flag_keeps_record_out_of_ok_pids_even_if_ok_true():
    """Страховка: маркер утраты сильнее случайно проставленного ok.

    Иначе товар без собранных отзывов уехал бы в valid и тихо нарушил I1/I5.
    """
    combined = {"products": [{"pid": "777", "ok": True, "lost": True}]}
    assert k.extract_ok_pids(combined) == []
    assert k.extract_discovered_pids(combined) == ["777"], \
        "но в discovered он обязан остаться (иначе потеряется снова)"


# ---- split_done_pending: возврат в очередь ------------------------------------

def test_lost_pids_returned_to_queue_by_split_done_pending(tmp_path):
    """Возвращаются в pending → уйдут в следующий прогон, а не исчезнут."""
    combined = _merged_with_losses()
    (tmp_path / "1001_raw.json").write_text("{}", encoding="utf-8")
    prog = {"discovered_pids": k.extract_discovered_pids(combined),
            "valid_pids": [], "failed_pids": [], "permanently_failed_pids": [],
            "runs": []}
    done, retry = k.split_done_pending(prog, str(tmp_path))
    assert retry == ["1000", "1002"], "оба потерянных товара — снова в очереди"
    assert done == {"1001"}, "собранный товар пересобираться не должен"
    assert not ({"1000", "1002"} & done)


def test_lost_pid_not_counted_valid_after_daemon_step(tmp_path):
    """Полный шаг демона: discovery засчитан, а valid — нет.

    Проверяет обе стороны одной правки: пид возвращается в очередь И не
    попадает в valid_pids (там нужен canonical raw, которого у lost нет).
    """
    combined = _merged_with_losses()
    prog = {"run_cfg": {"data_dir": str(tmp_path)},
            "discovered_pids": [], "valid_pids": [], "failed_pids": [],
            "permanently_failed_pids": [], "verdicts": {}, "verdict_meta": {},
            "runs": []}
    prog["discovered_pids"] = k.normalize_pids(
        list(prog["discovered_pids"]) + k.extract_discovered_pids(combined))
    assert prog["discovered_pids"] == ["1000", "1001", "1002"]

    checked = k.classify_new_pids(prog, k.extract_ok_pids(combined),
                                  qa_fn=lambda batch: {"ok": len(batch), "fail": 0,
                                                       "per_pid": {}})
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["valid_pids"] == [], "lost-товар не может стать valid"
    assert "1000" in k.pending_pids(prog, str(tmp_path))
    assert "1002" in k.pending_pids(prog, str(tmp_path))
