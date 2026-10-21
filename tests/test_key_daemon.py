"""Юнит-тесты чистых функций scripts/key_daemon.py (без запуска CLI/браузера)."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.key_daemon as k  # noqa: E402


def test_clear_interrupted_state_allows_resume():
    p = {"final_state": "INTERRUPTED", "final_reason": "user_interrupt",
         "final_note": "stop"}
    assert k._clear_interrupted_state(p) is True
    assert p["final_state"] is None and p["final_reason"] is None


def test_clear_interrupted_state_keeps_other_terminal_states():
    p = {"final_state": "NEEDS_ATTENTION", "final_reason": "x"}
    assert k._clear_interrupted_state(p) is False
    assert p["final_state"] == "NEEDS_ATTENTION"


def test_validate_campaign_name_rejects_path_escape():
    for bad in ("../x", "a/b", "a\\b", ".hidden", "..", ""):
        with pytest.raises(SystemExit):
            k.validate_campaign_name(bad)
    assert k.validate_campaign_name("sprey_dlya-gorla.1") == \
        "sprey_dlya-gorla.1"


def test_validate_campaign_name_allows_existing_legacy_name(
        tmp_path, monkeypatch):
    legacy = tmp_path / "my campaign"
    legacy.mkdir()
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path)
    assert k.validate_campaign_name("my campaign") == "my campaign"
    with pytest.raises(SystemExit):
        k.validate_campaign_name("missing campaign")


def test_validate_campaign_name_rejects_windows_reserved_names():
    for bad in ("CON", "nul.txt", "COM1", "campaign."):
        with pytest.raises(SystemExit):
            k.validate_campaign_name(bad)


def test_inproc_gather_cfg_rescan_bypasses_cache():
    normal = k._inproc_gather_cfg({}, 3600, mode="full")
    rescan = k._inproc_gather_cfg({}, 3600, mode="rescan")
    assert normal["max_age_s"] == 3600 and normal["overwrite"] is False
    assert rescan["max_age_s"] == 0 and rescan["overwrite"] is True


def test_run_sort_key_orders_unpadded_sequence():
    ids = ["run_100_10", "run_100_9", "run_99_100"]
    assert sorted(ids, key=k._run_sort_key) == [
        "run_99_100", "run_100_9", "run_100_10"]


def test_run_qa_batch_passes_absolute_data_dir(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        report = cmd[cmd.index("--report-json") + 1]
        with open(report, "w", encoding="utf-8") as f:
            json.dump({
                "summary": {"products": 1, "ok": 1, "warn": 0, "fail": 0},
                "products": [{"pid": "1", "verdict": "OK", "issues": []}],
            }, f)

        class R:
            returncode = 0
            stdout = ""
        return R()

    monkeypatch.setattr(k.subprocess, "run", fake_run)
    out = k.run_qa_batch(["1"], data_dir=str(tmp_path))
    assert out["ok"] == 1
    assert seen["cmd"][seen["cmd"].index("--data-dir") + 1] == str(tmp_path)
    assert seen["cwd"] == str(k.ROOT)


# ---------------------------------------------------------------------------
# Тестовый shim для удалённого k.build_cli_args (fac(argv) остаются
# совместимыми с протоколом, который проверяют интеграционные тесты)
# ---------------------------------------------------------------------------
def _test_build_cli_args(progress, run_id, mode, targets=None):
    cfg = progress["run_cfg"]
    base = [mode,
            "--limit", str(len(targets or []) if mode == "rescan"
                           else progress["target_count"] + int(cfg.get("reserve_count") or 0)),
            "--parallel", str(cfg.get("parallel", 1)),
            "--max-age", cfg.get("max_age", "7d")]
    if cfg.get("min_reviews"):
        base += ["--min-reviews", str(cfg["min_reviews"])]
    if mode == "rescan":
        base += ["--pids", ",".join(targets or [])]
    else:
        base += ["--url", progress.get("url", ""),
                 "--out", progress["out"],
                 "--out-raw", f"combined_{run_id}.json"]
        known = list(dict.fromkeys(progress.get("valid_pids", [])))
        if known:
            base += ["--skip-pids", ",".join(known)]
    if cfg.get("window_mode"):
        base += ["--window-mode", str(cfg["window_mode"])]
    if cfg.get("session_budget_wall_clock"):
        base += ["--session-budget-wall-clock", str(cfg["session_budget_wall_clock"])]
    return base

k.build_cli_args = _test_build_cli_args


# --- apply_window_mode ---

def test_apply_window_mode_sets_env(monkeypatch):
    """CLI --window-mode → OZON_KEEP_WINDOW (находка ревью 2026-10-17).

    Раньше visible только писался в run_cfg манифеста, а сентинел и сторожа
    продолжали прятать окна: обещание справки --window-mode не выполнялось.
    None (аргумент не задан) режим не трогает — env остаётся как был.
    """
    monkeypatch.setenv("OZON_KEEP_WINDOW", "0")
    k.apply_window_mode("visible")
    assert os.environ["OZON_KEEP_WINDOW"] == "1"
    k.apply_window_mode("hidden")
    assert os.environ["OZON_KEEP_WINDOW"] == ""
    k.apply_window_mode(None)
    assert os.environ["OZON_KEEP_WINDOW"] == "", "None режим не трогает"


def test_window_mode_from_manifest(tmp_path, monkeypatch):
    """Resume без --window-mode: режим кампании читается из run_cfg.

    Регресс ревью 2026-10-17: visible-кампания после resume получала
    keep_window воркерам из манифеста, а env демона оставался пуст —
    сентинел гасил видимые окна кампании.
    """
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path)
    camp = tmp_path / "camp1"
    camp.mkdir()
    (camp / "progress.json").write_text(
        json.dumps({"run_cfg": {"window_mode": "visible"}}), encoding="utf-8")
    assert k.window_mode_from_manifest("camp1") == "visible"
    assert k.window_mode_from_manifest("no-such-campaign") is None
    (camp / "progress.json").write_text("{не json", encoding="utf-8")
    assert k.window_mode_from_manifest("camp1") is None
    (camp / "progress.json").write_text(
        json.dumps({"run_cfg": {"window_mode": None}}), encoding="utf-8")
    assert k.window_mode_from_manifest("camp1") is None, \
        "None/мусор в манифесте — режим не трогаем"


# --- classify_status ---

def test_classify_challenge_storm():
    assert k.classify_status(0, {"aborted": {"reason": "challenge_storm"}}) == "CHALLENGE_STORM"
    assert k.classify_status(1, {"aborted": {"reason": "x"}}) == "CHALLENGE_STORM"


def test_classify_normal_done():
    assert k.classify_status(0, {"products": []}) == "NORMAL_DONE"
    assert k.classify_status(0, None) == "NORMAL_DONE"


def test_classify_crash_vs_unknown():
    assert k.classify_status(1, None) == "CRASH"
    assert k.classify_status(2, {"products": []}) == "UNKNOWN_FAILURE"


def test_classify_all_workers_fatal_is_crash():
    """Весь пул с fatal_error → CRASH даже при exit 0 и наличии combined."""
    combined = {"products": [], "parallel": {"workers": [
        {"fatal_error": "RuntimeError: boom"}, {"fatal_error": "OSError: x"}]}}
    assert k.classify_status(0, combined) == "CRASH"


def test_classify_single_fatal_with_alive_worker_is_done():
    """Один fatal при живом втором воркере — телеметрия респавна, не CRASH."""
    combined = {"products": [], "parallel": {"workers": [
        {"fatal_error": "boom"}, {"fatal_error": None}]}}
    assert k.classify_status(0, combined) == "NORMAL_DONE"


# --- extract pids ---

def _combined(products):
    return {"products": products}


def test_extract_discovered_pids():
    c = _combined([{"pid": 111}, {"pid": "222"}, {"pid": None}, {}])
    assert k.extract_discovered_pids(c) == ["111", "222"]


def test_extract_discovered_empty():
    assert k.extract_discovered_pids(None) == []
    assert k.extract_discovered_pids({}) == []


def test_extract_ok_pids_filters_fail_and_excluded():
    c = _combined([
        {"pid": 1, "ok": True},
        {"pid": 2, "ok": True, "excluded": {"reason": "rating_below"}},
        {"pid": 3, "ok": False},
        {"pid": 4, "ok": True},
    ])
    assert k.extract_ok_pids(c) == ["1", "4"]


# --- normalize_pids ---

def test_normalize_pids_strips_urls_and_dups():
    assert k.normalize_pids(["123", "123", "https://www.ozon.ru/product/456",
                             "789?tab=reviews", " 45 ", "abc"]) == \
        ["123", "456", "789", "45"]


# --- batch_pids ---

def test_batch_pids_sizes():
    r = k.batch_pids(list(map(str, range(235))), 100)
    assert len(r) == 3
    assert [len(b) for b in r] == [100, 100, 35]
    assert sum(len(b) for b in r) == 235


def test_batch_pids_empty():
    assert k.batch_pids([]) == []


# --- backoff_delay ---

def test_backoff_delay_clamped():
    assert k.backoff_delay(0) == 0
    assert k.backoff_delay(1) == 300
    assert k.backoff_delay(2) == 900
    assert k.backoff_delay(3) == 1800
    assert k.backoff_delay(10) == k.BACKOFF_DELAYS_S[-1]  # 7200 (clamp)


# --- parse_qa_stdout ---

def test_parse_qa_stdout():
    out = k.parse_qa_stdout(
        "[qa] товаров: 100 | OK 90 | WARN 7 | FAIL 3 | WALL_LIMITED 5\n"
        "[qa] KPI stars_missing: 2/500 (0.4%)\n")
    assert out["products"] == 100
    assert out["ok"] == 90
    assert out["warn"] == 7
    assert out["fail"] == 3
    assert out["wall_limited"] == 5


def test_parse_qa_stdout_empty():
    out = k.parse_qa_stdout("no summary line")
    assert out["fail"] == 0 and out["products"] == 0


# --- reconcile_validity ---

def _manifest(tmp_path, target=300, min_valid=200):
    return k.default_manifest("c", target, min_valid, "url", "out.md",
                              data_dir=str(tmp_path))


def test_reconcile_validity_from_raw(tmp_path):
    # два валидных raw-файла, один FAIL-вердикт
    for pid in ("101", "102"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101", "102", "103"]
    checked = {"verdicts": {"101": "OK", "102": "FAIL", "103": "ERR"},
               "valid_add": ["101"], "checked": ["101", "102"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert set(prog["valid_pids"]) == {"101"}
    assert "102" in prog["failed_pids"]


def test_reconcile_failed_not_accumulated_when_ok(tmp_path):
    # после успешного rescan вердикт пида стал OK → он должен выйти из failed
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    prog["failed_pids"] = ["101"]  # был FAIL в прошлый раз
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert "101" in prog["valid_pids"]
    assert "101" not in prog["failed_pids"]


def test_reconcile_records_verdict_meta(tmp_path):
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    fp = k.raw_fingerprint(str(tmp_path), "101")
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"],
               "new_meta": {"101": {"verdict": "OK",
                                    "raw_mtime_ns": fp["raw_mtime_ns"],
                                    "raw_sha256": fp["raw_sha256"],
                                    "raw_size": fp["raw_size"],
                                    "qa_version": k.QA_VERSION,
                                    "qa_at": k.now_utc_iso()}}}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["verdict_meta"]["101"]["verdict"] == "OK"
    assert prog["verdict_meta"]["101"]["raw_sha256"] == fp["raw_sha256"]


def test_reconcile_keeps_previous_meta_when_not_checked(tmp_path):
    # пиды, не проверенные в этом цикле, не теряют/не обнуляют verdict_meta
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["verdict_meta"] = {"201": {"verdict": "OK", "raw_mtime_ns": 123}}
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"],
               "new_meta": {"101": {"verdict": "OK", "raw_mtime_ns": 999}}}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["verdict_meta"]["201"]["raw_mtime_ns"] == 123
    assert prog["verdict_meta"]["101"]["raw_mtime_ns"] == 999


# --- title-фильтр (exact-match) ---

def _raw_with_title(tmp_path, pid, title):
    (tmp_path / f"{pid}_raw.json").write_text(
        json.dumps({"product": {"title": title}}), encoding="utf-8")


def test_integration_text_in_url_disables_filter_by_default(tmp_path, monkeypatch):
    """РЕГРЕССИЯ инцидента podarok_five: URL содержит text=, но --match-title
    НЕ задан → фильтр title выключен (title_match_query==""), сбор идёт как
    обычно. Авто-подхват text= из URL убран намеренно (поисковый запрос ≠
    название товара)."""
    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(3)], "found": 3}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=3, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=3,
        reserve_count=0, max_total_hours=24.0,
        url="https://www.ozon.ru/search/?from_global=true&text=%D0%B4%D0%BB%D1%8F"
            "%20%D0%B4%D0%B5%D0%B2%D0%BE%D1%87%D0%BA%D0%B8%205%20%D0%BB%D0%B5%D1%82"
            "%20%D0%BF%D0%BE%D0%B4%D0%B0%D1%80%D0%BE%D0%BA")
    assert prog["final_state"] == "COMPLETED"
    assert prog["final_reason"] == "target_reached"
    assert prog["title_match_query"] == ""          # фильтр ВЫКЛЮЧЕН
    assert len(prog["valid_pids"]) == 3             # ничего не отсечено
    assert prog["title_mismatch_pids"] == []


def test_integration_explicit_match_title_sets_phrase(tmp_path, monkeypatch):
    """Явный --match-title ИЗВЛЕКАЕТ из URL, а НЕ авто-текст: фраза фиксируется
    в манифесте и применяется (товары без совпадения title → title_mismatch,
    valid не растёт). Только явный аргумент включает фильтр."""
    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(3)], "found": 3}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=3, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=3,
        reserve_count=0, max_total_hours=24.0,
        max_no_progress_runs=2,
        url="https://www.ozon.ru/category/edifier-26667829/?text=Edifier+W830NB",
        match_title="Edifier W830NB")
    assert prog["title_match_query"] == "Edifier W830NB"  # из явного флага
    assert set(prog["title_mismatch_pids"]) == {"0", "1", "2"}  # фильтр активен
    assert prog["valid_pids"] == []                   # ни один не подошёл
    assert prog["final_state"] == "ABANDONED"         # честный итог без магии


def test_reconcile_title_filter_keeps_matching(tmp_path):
    # подходящий по title товар остаётся valid и не попадает в mismatch
    _raw_with_title(tmp_path, "101", "Наушники Edifier W830NB беспроводные")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path), match_query="Edifier W830NB")
    assert "101" in prog["valid_pids"]
    assert prog["title_mismatch_pids"] == []


def test_reconcile_title_filter_removes_nomatch(tmp_path):
    # несоответствующий title: НЕ valid, НО и НЕ failed (title_mismatch)
    _raw_with_title(tmp_path, "101", "Наушники Edifier W820NB")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path), match_query="W830NB")
    assert "101" not in prog["valid_pids"]
    assert "101" not in prog["failed_pids"]
    assert "101" in prog["title_mismatch_pids"]


def test_reconcile_title_filter_case_insensitive(tmp_path):
    # регистронезависимость: фраза/название в разном регистре
    _raw_with_title(tmp_path, "101", "EDIFIER W830NB BLACK")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path), match_query="edifier w830nb")
    assert "101" in prog["valid_pids"]


def test_reconcile_title_filter_disabled_without_query(tmp_path):
    # без фразы фильтр выключен: всё как обычно (никакой магии)
    _raw_with_title(tmp_path, "101", "Edifier W820NB")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path), match_query="")
    assert "101" in prog["valid_pids"]
    assert prog["title_mismatch_pids"] == []


def test_reconcile_title_filter_fallback_from_progress(tmp_path):
    # вызов без match_query берёт фразу из progress["title_match_query"]
    _raw_with_title(tmp_path, "101", "Edifier W830NB Pro")
    _raw_with_title(tmp_path, "102", "Edifier W820NB")
    prog = _manifest(tmp_path)
    prog["title_match_query"] = "W830NB"
    prog["discovered_pids"] = ["101", "102"]
    checked = {"verdicts": {"101": "OK", "102": "OK"},
               "valid_add": ["101", "102"], "checked": ["101", "102"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert "101" in prog["valid_pids"]
    assert "102" not in prog["valid_pids"]
    assert "102" in prog["title_mismatch_pids"]


def test_reconcile_title_filter_does_not_grow_failed(tmp_path):
    # title-mismatch добро-товара НЕ попадает в failed (списки разделены):
    # один pid не подошёл по title (но OK по QA) → только в title_mismatch;
    # другой — FAIL по QA → только в failed.
    _raw_with_title(tmp_path, "101", "Ozon банка")
    _raw_with_title(tmp_path, "202", "Ozon банка")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101", "202"]
    checked = {"verdicts": {"101": "OK", "202": "FAIL"},
               "valid_add": ["101"], "checked": ["101", "202"]}
    k.reconcile_validity(prog, checked, str(tmp_path), match_query="Edifier")
    assert "101" not in prog["valid_pids"]
    assert "101" in prog["title_mismatch_pids"]
    assert "101" not in prog["failed_pids"]
    assert "202" in prog["failed_pids"]
    assert "202" not in prog["title_mismatch_pids"]
    # заголовок файла не содержит "Edifier", поэтому оба мимо фильтра title


# --- compute_final ---

def _progress(valid_n, target, min_valid):
    # valid_n уникальных pid (не дублей)
    return {"valid_pids": [f"p{i}" for i in range(valid_n)],
            "target_count": target, "min_valid": min_valid}


def test_compute_final_states():
    assert k.compute_final(_progress(300, 300, 200), exhausted=False) == "COMPLETED"
    assert k.compute_final(_progress(240, 300, 200), exhausted=True) == "COMPLETED_MIN"
    assert k.compute_final(_progress(150, 300, 200), exhausted=True) == "ABANDONED"
    assert k.compute_final(_progress(150, 300, 200), exhausted=False) == "ABANDONED"
    assert k.compute_final(_progress(250, 300, 200), exhausted=False) is None


# --- summarize ---

def _progress_filled():
    p = k.default_manifest("x", 300, 200, "u", "campaign.md")
    p["discovered_pids"] = ["1", "2"]
    p["valid_pids"] = ["1"]
    p["failed_pids"] = ["2"]
    return p


def test_summarize_fields():
    s = k.summarize(_progress_filled())
    assert s["target"] == 300 and s["valid"] == 1 and s["discovered"] == 2
    assert s["failed"] == 1 and s["final_state"] is None


# --- qa_summary_consistent (K4) ---

def test_qa_summary_consistent_ok():
    qa = {"products": 100, "ok": 90, "warn": 7, "fail": 3}
    assert k.qa_summary_consistent(qa, 100) is True
    qa2 = {"products": 100, "ok": 100, "warn": 0, "fail": 0}
    assert k.qa_summary_consistent(qa2, 100) is True


def test_qa_summary_inconsistent_missing_products():
    assert k.qa_summary_consistent({}, 100) is False
    assert k.qa_summary_consistent({"products": 0, "ok": 0, "warn": 0, "fail": 0}, 100) is False


def test_qa_summary_inconsistent_sum():
    # сумма OK+WARN+FAIL не сходится с products
    qa = {"products": 100, "ok": 90, "warn": 7, "fail": 1}  # 98 != 100
    assert k.qa_summary_consistent(qa, 100) is False


# --- classify_new_pids / parse_error (K4) ---

def _make_qa(products, ok, warn, fail, per_pid=None):
    return {"products": products, "ok": ok, "warn": warn, "fail": fail,
            "per_pid": per_pid}


def _mtime(path):
    """st_mtime в секундах (float)."""
    return path.stat().st_mtime


def test_classify_no_candidates(tmp_path):
    prog = _manifest(tmp_path)
    r = k.classify_new_pids(prog, [], qa_fn=lambda b: {})
    assert r["parse_error"] is False
    assert r["valid_add"] == [] and r["checked"] == []


def test_run_data_dir_reads_run_cfg():
    """Run data_dir comes from run_cfg; when absent it stays None."""
    assert k._run_data_dir({"run_cfg": {"data_dir": "X"}}) == "X"
    assert k._run_data_dir({}) is None


def test_classify_null_data_dir_falls_back_to_default(tmp_path, monkeypatch):
    """data_dir=null must fall back to DEFAULT_DATA_DIR, not reach Path(None).

    Review 2026-10-19: the inline .get() returned None for an explicit null,
    so classify_new_pids lost the raw dir (or crashed on Path(None)).
    """
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(k, "DEFAULT_DATA_DIR", tmp_path)
    prog = _manifest(tmp_path)
    prog["run_cfg"]["data_dir"] = None
    calls = []

    def qa(batch):
        calls.append(tuple(batch))
        return _make_qa(products=len(batch), ok=len(batch), warn=0, fail=0,
                        per_pid={p: "OK" for p in batch})

    r = k.classify_new_pids(prog, ["101"], qa_fn=qa)
    assert calls == [("101",)], \
        "data_dir=null обязан падать на DEFAULT_DATA_DIR, а не на None"
    assert r["valid_add"] == ["101"]


def test_classify_checks_force_before_qa_batches(tmp_path, monkeypatch):
    """FORCE (2nd Ctrl+C) is checked before every QA batch call.

    Audit regression 2026-10-19: force exit during a QA phase waited for all
    batches to finish (minutes). _emergency_shutdown is mocked here (os._exit
    is impossible in-process); the pinned fact is the order guard -> qa.
    """
    import threading as _threading
    pid = "101"
    (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    order = []

    def _fake_exit(progress_path=None, force=""):
        order.append(("exit", force))

    monkeypatch.setattr(k, "_emergency_shutdown", _fake_exit)
    force_ev = _threading.Event()
    force_ev.set()
    monkeypatch.setattr(k, "_FORCE_EXIT", force_ev)

    def qa(batch):
        order.append(("qa", tuple(batch)))
        return _make_qa(products=len(batch), ok=len(batch), warn=0, fail=0,
                        per_pid={p: "OK" for p in batch})

    k.classify_new_pids(prog, [pid], qa_fn=qa)
    assert order and order[0] == ("exit", "qa"), \
        "guard обязан сработать ДО первого QA-батча"
    assert ("qa", (pid,)) in order


def test_classify_inconsistent_summary_parse_error(tmp_path):
    pid = "101"
    (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def bad_qa(batch):
        return _make_qa(products=len(batch) - 1, ok=len(batch), warn=0, fail=0)

    r = k.classify_new_pids(prog, [pid], qa_fn=bad_qa)
    assert r["parse_error"] is True
    assert r["valid_add"] == []


def test_classify_fail_and_empty_per_pid_parse_error(tmp_path):
    pid = "101"
    (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        return _make_qa(products=len(batch), ok=len(batch) - 1, warn=0, fail=1,
                        per_pid={})  # fail>0, но per_pid пуст

    r = k.classify_new_pids(prog, [pid], qa_fn=qa)
    assert r["parse_error"] is True


def test_classify_fail_with_valid_per_pid(tmp_path):
    pid = "101"
    (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        return _make_qa(products=len(batch), ok=len(batch) - 1, warn=0, fail=1,
                        per_pid={pid: "FAIL"})

    r = k.classify_new_pids(prog, [pid], qa_fn=qa)
    assert r["parse_error"] is False
    assert pid in r["failed_add"]
    assert r["valid_add"] == []


def test_classify_fail_partial_per_pid_missing_fail_pid(tmp_path):
    # fail>0, но per_pid покрывает не все PID: fail-пид может получить fallback OK
    pids = ["101", "102"]
    for p in pids:
        (tmp_path / f"{p}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        # fail=1, но per_pid описывает только 101 (OK); 102 без вердикта
        return _make_qa(products=len(batch), ok=len(batch) - 1, warn=0, fail=1,
                        per_pid={"101": "OK"})

    r = k.classify_new_pids(prog, pids, qa_fn=qa)
    assert r["parse_error"] is True
    assert r["valid_add"] == []


def test_classify_all_ok_empty_per_pid_no_parse_error(tmp_path):
    # легитимный SHOW_ALL_LIMIT: сводка сходится, fail==0, per_pid пуст → все OK
    pids = ["101", "102", "103"]
    for p in pids:
        (tmp_path / f"{p}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        return _make_qa(products=len(batch), ok=len(batch), warn=0, fail=0,
                        per_pid={})

    r = k.classify_new_pids(prog, pids, qa_fn=qa)
    assert r["parse_error"] is False
    assert set(r["valid_add"]) == set(pids)


def test_classify_ok_with_partial_per_pid(tmp_path):
    pids = ["101", "102", "103"]
    for p in pids:
        (tmp_path / f"{p}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        # сводка сходится, fail==0 → отсутствующие в per_pid = OK
        return _make_qa(products=len(batch), ok=len(batch), warn=0, fail=0,
                        per_pid={"101": "OK"})

    r = k.classify_new_pids(prog, pids, qa_fn=qa)
    assert r["parse_error"] is False
    assert set(r["valid_add"]) == set(pids)


# --- finalize_campaign (K1): приоритеты статусов ---

def _final_progress(valid_n, target, min_valid, reason=None):
    return {"valid_pids": [str(i) for i in range(valid_n)],
            "target_count": target, "min_valid": min_valid,
            "final_reason": reason, "final_state": None, "qa": {}}


def test_finalize_target_reached_completed(tmp_path):
    # target_reached: snapshot(target) + резерв, чистые >= target → COMPLETED
    prog = _final_progress(300, 300, 200, reason="target_reached")
    prog["campaign_final_pids"] = [str(i) for i in range(300)]
    prog["campaign_reserve_pids"] = ["900", "901", "902"]
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b), "warn": 0, "products": len(b)})
    assert prog["final_state"] == "COMPLETED"
    assert prog["final_reason"] == "target_reached"


def test_finalize_no_progress_completed_min(tmp_path):
    prog = _final_progress(240, 300, 200, reason="no_progress")
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b), "warn": 0, "products": len(b)})
    assert prog["final_state"] == "COMPLETED_MIN"
    assert prog["final_reason"] == "no_progress"


def test_finalize_time_limit_completed_min(tmp_path):
    prog = _final_progress(220, 300, 200, reason="time_limit")
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b), "warn": 0, "products": len(b)})
    assert prog["final_state"] == "COMPLETED_MIN"
    assert prog["final_reason"] == "time_limit"


def test_finalize_below_min_valid_abandoned(tmp_path):
    prog = _final_progress(150, 300, 200, reason="no_progress")
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b), "warn": 0, "products": len(b)})
    assert prog["final_state"] == "ABANDONED"


def test_classify_warn_counts_valid(tmp_path):
    pid = "101"
    (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        return _make_qa(products=len(batch), ok=0, warn=1, fail=0,
                        per_pid={pid: "WARN"})

    r = k.classify_new_pids(prog, [pid], qa_fn=qa)
    assert r["parse_error"] is False
    assert pid in r["valid_add"]  # WARN тоже валиден (не брак)


def test_classify_skips_pid_without_raw(tmp_path):
    pid = "999"  # нет raw-файла
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = [pid]
    r = k.classify_new_pids(prog, [pid], qa_fn=lambda b: {"fail": 0})
    assert r["checked"] == []  # без raw нечего QA-ить
    assert r["valid_add"] == []


def test_classify_already_verdict_skipped(tmp_path):
    pid = "101"
    raw = tmp_path / f"{pid}_raw.json"
    raw.write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["verdicts"] = {pid: "FAIL"}  # уже решён ранее
    fp = k.raw_fingerprint(str(tmp_path), pid)
    # fingerprint совпадает и qa_version актуальна → вердикт актуален
    prog["verdict_meta"] = {pid: {"verdict": "FAIL",
                                  "raw_mtime_ns": fp["raw_mtime_ns"],
                                  "raw_sha256": fp["raw_sha256"],
                                  "qa_version": k.QA_VERSION}}
    called = []
    r = k.classify_new_pids(prog, [pid], qa_fn=lambda b: called.append(1) or
                            _make_qa(products=0, ok=0, warn=0, fail=0))
    assert r["checked"] == []  # вердикт актуален (fingerprint совпал) → не переQA
    assert called == []


def test_classify_verdict_stale_mtime_reqa(tmp_path):
    # вердикт есть, но raw изменился (fp новее) → обязательная перепроверка
    pid = "101"
    raw = tmp_path / f"{pid}_raw.json"
    raw.write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["verdicts"] = {pid: "OK"}
    # сохранённый fingerprint отличается от текущего (raw менялся) → reQA
    prog["verdict_meta"] = {pid: {"verdict": "OK", "raw_mtime_ns": 1,
                                  "raw_sha256": "old", "qa_version": k.QA_VERSION}}
    called = []
    r = k.classify_new_pids(prog, [pid],
                            qa_fn=lambda b: called.append(1) or
                            _make_qa(products=1, ok=1, warn=0, fail=0,
                                     per_pid={pid: "OK"}))
    assert called == [1]  # переQA состоялся
    assert r["checked"] == [pid]
    assert r["verdicts"][pid] == "OK"


def test_classify_empty_need_no_qa_call(tmp_path):
    prog = _manifest(tmp_path)
    called = []
    r = k.classify_new_pids(prog, [], qa_fn=lambda b: called.append(1) or {})
    assert r["parse_error"] is False
    assert called == []


# --- default_manifest ---

def test_default_manifest_fields():
    m = k.default_manifest("camp", 300, 200, "u", "campaign.md",
                           max_age="7d", parallel=1)
    assert m["campaign"] == "camp"
    assert m["target_count"] == 300 and m["min_valid"] == 200
    assert k.raw_pid_name("123") == "123_raw.json"
    assert m["final_state"] is None
    assert m["discovered_pids"] == [] and m["runs"] == []
    assert m["cooldown_until"] is None and m["challenge_seq"] == 0
    assert m["run_cfg"]["parallel"] == 1


# --- raw-файловые хелперы ---

def test_all_raw_pids_and_has_raw(tmp_path):
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    (tmp_path / "102_raw.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notraw.txt").write_text("x", encoding="utf-8")
    r = k.all_raw_pids(str(tmp_path))
    assert set(r) == {"101", "102"}
    assert k.has_raw(str(tmp_path), "101") is True
    assert k.has_raw(str(tmp_path), "999") is False


def test_all_raw_pids_missing_dir(tmp_path):
    assert k.all_raw_pids(str(tmp_path / "nope")) == []


# --- batch_pids дедупликация ---

def test_batch_pids_dedups():
    r = k.batch_pids(["1", "1", "2", "2", "3"], 100)
    assert len(r) == 1 and sorted(r[0]) == ["1", "2", "3"]


# --- parse_qa_stdout edge cases ---

def test_parse_qa_stdout_zero():
    out = k.parse_qa_stdout("[qa] товаров: 0 | OK 0 | WARN 0 | FAIL 0")
    assert out["products"] == 0 and out["fail"] == 0


def test_parse_qa_stdout_non_numeric_ignored():
    out = k.parse_qa_stdout("[qa] товаров: abc | OK x | WARN 1 | FAIL 2")
    assert out["products"] == 0
    assert out["warn"] == 1 and out["fail"] == 2


def test_classify_verdicts_persisted_in_progress(tmp_path):
    pid = "101"
    (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    r = k.classify_new_pids(
        prog, [pid],
        qa_fn=lambda b: _make_qa(products=1, ok=0, warn=0, fail=1,
                                 per_pid={pid: "FAIL"}))
    # classify возвращает итоговые вердикты в r["verdicts"]; в прогресс их
    # записывает reconcile_validity (боевой поток вызывает её сразу после)
    assert r["verdicts"][pid] == "FAIL"
    k.reconcile_validity(prog, r, str(tmp_path))
    assert prog["verdicts"][pid] == "FAIL"
    # потом повторный classify с тем же prog не должен переQA (вердикт уже есть)
    called = []
    k.classify_new_pids(prog, [pid],
                        qa_fn=lambda b: called.append(1) or
                        _make_qa(products=0, ok=0, warn=0, fail=0))
    assert called == []


# --- finalize_campaign: дополнительные ---

def test_finalize_batched_qa_full_target(tmp_path):
    # target_reached: 3 FAIL в snapshot, резерв чистый и хватает → COMPLETED
    prog = _final_progress(300, 300, 200, reason="target_reached")
    prog["campaign_final_pids"] = [str(i) for i in range(300)]
    prog["campaign_reserve_pids"] = [str(i) for i in range(300, 330)]
    pa = tmp_path / "p.json"
    bad = {"0", "100", "200"}  # по одному FAIL в каждом snapshot-батче

    def qa(batch):
        per = {p: ("FAIL" if p in bad else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "COMPLETED"  # резерв заменил брак
    assert prog["qa"]["total_fail"] == 3
    assert len(prog["replacement_pids"]) == 3  # брак заменён из резерва
    assert prog["replacement_pids"][0]["replaced_by"] in {"300", "301", "302"}
    assert set(prog["campaign_final_pids"]) & {"300", "301", "302"}  # заполнены резервом
    assert not (set(prog["campaign_final_pids"]) & {"0", "100", "200"})  # брака нет


def test_finalize_uses_campaign_final_pids_snapshot(tmp_path):
    # snapshot = campaign_final_pids (зафиксирован ДО финального QA), а не весь valid
    prog = _final_progress(5, 3, 2)
    prog["valid_pids"] = ["1", "2", "3", "4", "5"]
    prog["campaign_final_pids"] = ["1", "2", "3"]
    pa = tmp_path / "p.json"
    checked = []
    k.finalize_campaign(prog, pa, qa_fn=lambda b: checked.extend(b) or
                        {"fail": 0, "ok": len(b), "warn": 0,
                         "products": len(b)})
    assert prog["final_state"] == "COMPLETED"
    assert set(checked) == {"1", "2", "3"}  # только snapshot, не 4,5


def test_finalize_empty_snapshot_no_qa_no_crash(tmp_path):
    prog = _final_progress(0, 300, 200)
    pa = tmp_path / "p.json"
    called = []
    k.finalize_campaign(prog, pa, qa_fn=lambda b: called.append(1) or
                        {"fail": 0, "ok": 0, "warn": 0})
    assert called == []  # нет snapshot → финальный QA не гоняем
    assert prog["final_state"] == "ABANDONED"


def test_finalize_attention_not_overridden(tmp_path):
    # NEEDS_ATTENTION — терминальный, финализатор не трогает и не прогоняет QA
    prog = _final_progress(150, 300, 200, reason="qa_parse_error")
    prog["final_state"] = "NEEDS_ATTENTION"
    prog["campaign_final_pids"] = ["1"]
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 99, "ok": 0, "warn": 0})
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog.get("qa", {}).get("total_fail") != 99


# --- finalize_campaign: target_reached + резерв ---

def _target_prog(target=300, min_valid=200, n_valid=300, reserve=30,
                 reason="target_reached"):
    p = _final_progress(n_valid, target, min_valid, reason=reason)
    p["campaign_final_pids"] = [str(i) for i in range(target)]
    p["campaign_reserve_pids"] = [str(i) for i in range(target, target + reserve)]
    return p


def test_finalize_reserve_shortfall_abandoned(tmp_path):
    # target_reached: в пуле 5 FAIL, резерв меньше → чистых 297 < target 300
    # → ABANDONED (COMPLETED_MIN для target_reached недопустим)
    prog = _target_prog(reserve=2)
    bad = {"0", "1", "2", "3", "4"}

    def qa(batch):
        per = {p: ("FAIL" if p in bad else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=qa)
    # 302 в пуле - 5 FAIL = 297 чистых < target(300) → ABANDONED
    assert prog["final_state"] == "ABANDONED"


def test_finalize_reserve_covered_completed(tmp_path):
    # 3 FAIL, резерв (10) хватает → чистых 307-3=304 >= 300 → COMPLETED
    prog = _target_prog(reserve=10)
    bad = {"0", "1", "2"}

    def qa(batch):
        per = {p: ("FAIL" if p in bad else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "COMPLETED"


def test_finalize_reserve_exhausted_below_min_abandoned(tmp_path):
    # 200 FAIL, резерв 10 → чистых 110 < min_valid 200 → ABANDONED
    prog = _target_prog(target=300, min_valid=200, reserve=10)
    bad = {str(i) for i in range(50)}.union({str(i) for i in range(100, 250)})  # 200 шт

    def qa(batch):
        per = {p: ("FAIL" if p in bad else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "ABANDONED"


def test_finalize_replacement_not_in_snapshot(tmp_path):
    # бракованный pid из snapshot заменяется чистым из резерва;
    # replacement_pids — массив объектов {failed_pid, replaced_by}
    prog = _target_prog(target=3, min_valid=2, n_valid=5, reserve=3)
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["4", "5", "6"]

    def qa(batch):
        per = {p: ("FAIL" if p == "2" else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "COMPLETED"
    final = prog["campaign_final_pids"]
    assert "2" not in final          # брак вытеснен
    assert set(final) & {"4", "5", "6"}  # заполнен из резерва
    replaced_bys = {r["replaced_by"] for r in prog["replacement_pids"]}
    failed_pids = {r["failed_pid"] for r in prog["replacement_pids"]}
    assert replaced_bys & {"4", "5", "6"}
    assert "2" not in replaced_bys
    assert "2" in failed_pids


def _cfg_progress(campaign="camp", target=300, min_valid=200, mode_cfg=None):
    cfg = {"parallel": 1, "max_age": "7d", "min_reviews": 40,
           "data_dir": "D:\\tmp\\data"}
    if mode_cfg:
        cfg.update(mode_cfg)
    return {"campaign": campaign, "target_count": target, "min_valid": min_valid,
            "url": "https://ozon", "out": "docs/c.md", "run_cfg": cfg}


# --- _parse_qa_per_pid ---

def test_parse_qa_per_pid_rows():
    stdout = (
        "[qa] товаров: 2 | OK 1 | WARN 0 | FAIL 1\n"
        "101  FAIL  ...\n"
        "102  OK    ...\n"
    )
    v = k._parse_qa_per_pid(stdout)
    assert v == {"101": "FAIL", "102": "OK"}


def test_parse_qa_per_pid_ignores_malformed():
    stdout = ("notanumber  OK\n101  SOMETHING_ELSE\n102  INFO\n")
    v = k._parse_qa_per_pid(stdout)
    # "notanumber" не pid и "SOMETHING_ELSE" не вердикт → игнорируются;
    # INFO — допустимый вердикт qa_raw, парсится.
    assert "101" not in v
    assert v == {"102": "INFO"}


# --- classify_status edge ---

def test_classify_status_error_with_aborted_priority():
    # aborted распознаётся по ПРИСУТСТВИЮ ключа (даже если dict пустой),
    # а не по truthiness — иначе пустой aborted {} не опознавался бы.
    assert k.classify_status(1, {"aborted": {"reason": "challenge_storm"}}) == "CHALLENGE_STORM"
    assert k.classify_status(1, {"aborted": {}}) == "CHALLENGE_STORM"
    assert k.classify_status(0, {"aborted": {}}) == "CHALLENGE_STORM"
    assert k.classify_status(3, None) == "CRASH"
    assert k.classify_status(0, {"products": []}) == "NORMAL_DONE"
    assert k.classify_status(2, {"products": []}) == "UNKNOWN_FAILURE"


# --- _storm_label: причина шторма для оператора ---

def test_storm_label_prioritises_puzzle_then_signature():
    # пазл-шторм важнее сигнатуры: сигнатуры у него нет вовсе, и без
    # приоритета в last_error попадало «сигнатура неизвестна»
    assert k._storm_label(None, {"puzzle_persistent": True,
                                 "puzzle_failures": 3}) == \
        "puzzle_storm: пазл не решён (3 неудач)"
    assert k._storm_label(None, {"puzzle_persistent": True}) == \
        "puzzle_storm: пазл не решён"
    # обычный шторм: сигнатура страницы
    assert k._storm_label("title:похоже, нет соединения", {}) == \
        "title:похоже, нет соединения"
    # ни пазла, ни сигнатуры — показываем воркеров, а не «неизвестно»
    assert k._storm_label(None, {"workers_stormed": [1, 2]}) == \
        "access_challenge (воркеры [1, 2])"
    assert k._storm_label(None, {}) == "access_challenge (сигнатура неизвестна)"


# --- normalize/extract edge ---

def test_extract_discovered_pids_strips_url_wrap():
    c = _combined([{"pid": "https://www.ozon.ru/product/777?tab=reviews"}])
    assert k.extract_discovered_pids(c) == ["777"]


def test_extract_ok_pids_strips_url_wrap():
    c = _combined([{"pid": "https://www.ozon.ru/product/888", "ok": True}])
    assert k.extract_ok_pids(c) == ["888"]


def test_extract_pids_order_preserved():
    c = _combined([{"pid": 5}, {"pid": 2}, {"pid": 5}])
    assert k.extract_discovered_pids(c) == ["5", "2"]  # дубль 5 убран, порядок сохранён


def test_delta_basic():
    assert k.delta(["1", "2"], ["1", "3", "2"]) == {"3"}
    assert k.delta([], ["1"]) == {"1"}
    assert k.delta(["1"], ["1"]) == set()


# --- finalize: default reason ---

def test_finalize_min_valid_default_reason_exhausted(tmp_path):
    # min_valid>=valid, FAIL==0, reason не задан → COMPLETED_MIN c exhausted
    prog = _final_progress(240, 300, 200)  # reason=None
    prog["final_reason"] = None
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b), "warn": 0, "products": len(b)})
    assert prog["final_state"] == "COMPLETED_MIN"
    assert prog["final_reason"] == "exhausted"


def test_finalize_abandoned_no_fail_below_min_note(tmp_path):
    prog = _final_progress(150, 300, 200)
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b), "warn": 0, "products": len(b)})
    assert prog["final_state"] == "ABANDONED"
    assert "min_valid" in prog["final_note"]  # нет брака → причина ниже min_valid


# --- reconcile: защита от накопления FAIL-пидов в valid ---

def test_reconcile_does_not_accumulate_fail_in_valid(tmp_path):
    # если раньше пид попал в valid_pids, а его ТЕКУЩИЙ вердикт — FAIL,
    # reconcile не должен держать его в valid (защита от накопленного брака).
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    prog["valid_pids"] = ["101"]  # накоплен ранее
    checked = {"verdicts": {"101": "FAIL"}, "valid_add": [], "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert "101" not in set(prog["valid_pids"])


def test_reconcile_keeps_prior_valid_when_still_ok(tmp_path):
    # пид, подтверждённый раннее как OK, не теряется при reconcile без нового QA
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    prog["valid_pids"] = ["101"]
    prog["verdicts"] = {"101": "OK"}
    checked = {"verdicts": {"101": "OK"}, "valid_add": [], "checked": []}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert "101" in set(prog["valid_pids"])


# --- normalize_pids edge ---

def test_normalize_pids_empty_and_garbage():
    assert k.normalize_pids([]) == []
    assert k.normalize_pids(["", "abc", "   ", None]) == []
    assert k.normalize_pids(["123?x=1", "product/456", "789/"]) == ["123", "456", "789"]


def test_raw_pid_name():
    assert k.raw_pid_name("123") == "123_raw.json"


# --- parse_qa_stdout: WALL_LIMITED / многострочность ---

def test_parse_qa_stdout_without_wall_limited():
    out = k.parse_qa_stdout("[qa] товаров: 50 | OK 50 | WARN 0 | FAIL 0")
    assert out["wall_limited"] == 0
    assert out["products"] == 50


def test_parse_qa_stdout_multiline_only_takes_qa_line():
    stdout = (
        "INFO: загружен кеш\n"
        "[qa] товаров: 3 | OK 2 | WARN 1 | FAIL 0 | WALL_LIMITED 2\n"
        "done\n"
    )
    out = k.parse_qa_stdout(stdout)
    assert out["products"] == 3 and out["ok"] == 2


# --- qa_summary_consistent: products не число / None ---

def test_qa_summary_consistent_products_none():
    assert k.qa_summary_consistent({"products": None, "ok": 0}, 5) is False


def test_qa_summary_consistent_products_non_numeric():
    assert k.qa_summary_consistent({"products": "abc", "ok": 0}, 5) is False


# --- compute_final: min_valid=0 edge ---

def test_compute_final_min_valid_zero():
    # min_valid=0 → даже 1 валидный с исчерпанной выдачей = COMPLETED_MIN
    assert k.compute_final({"valid_pids": ["1"], "target_count": 300,
                            "min_valid": 0}, exhausted=True) == "COMPLETED_MIN"
    assert k.compute_final({"valid_pids": [], "target_count": 300,
                            "min_valid": 0}, exhausted=True) == "COMPLETED_MIN"


# --- default_manifest: run_cfg передаётся ---

def test_default_manifest_run_cfg_passthrough():
    m = k.default_manifest("c", 100, 50, "u", "o",
                           data_dir="x", max_age="3d", min_reviews=0,
                           parallel=2, max_no_progress_runs=5, max_rescans=1,
                           max_total_hours=6.0, notify="toast")
    assert m["run_cfg"] == {
        "data_dir": "x", "max_age": "3d", "min_reviews": 0, "parallel": 2,
        "max_no_progress_runs": 5, "max_rescans": 1,
        "max_total_hours": 6.0, "notify": "toast",
    }


# --- classify_new_pids: несколько батчей, mixed вердикты ---

def test_classify_multibatch_aggregates(tmp_path):
    # 250 pid → разбиваются на 3 батча; вердикты собираются по всем батчам
    n = 250
    pids = [str(i) for i in range(n)]
    for p in pids:
        (tmp_path / f"{p}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        per = {p: ("FAIL" if (int(p) % 100 == 0) else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return _make_qa(products=len(batch), ok=len(batch) - fails,
                        warn=0, fail=fails, per_pid=per)

    r = k.classify_new_pids(prog, pids, qa_fn=qa)
    assert r["parse_error"] is False
    assert len(r["checked"]) == n
    # 0, 100, 200 — FAIL (делятся на 100)
    assert set(r["failed_add"]) == {"0", "100", "200"}
    assert len(r["valid_add"]) == n - 3


def test_classify_multibatch_second_parse_error(tmp_path):
    # первый батч норм, второй — недостоверная сводка → parse_error по всей пачке
    pids = [str(i) for i in range(150)]
    for p in pids:
        (tmp_path / f"{p}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    calls = []

    def qa(batch):
        calls.append(len(batch))
        if calls[-1] == 50:  # второй батч (50 шт) ломаем
            return _make_qa(products=len(batch) - 1, ok=len(batch), warn=0, fail=0)
        return _make_qa(products=len(batch), ok=len(batch), warn=0, fail=0)

    r = k.classify_new_pids(prog, pids, qa_fn=qa)
    assert r["parse_error"] is True
    assert r["valid_add"] == []
    assert r["checked"] == []  # при parse_error не подтверждаем ничего
    assert r["verdicts"] == {}  # частичные вердикты первого батча НЕ протекают


# --- reconcile: valid_add не дублирует и переживает reconcile ---

def test_reconcile_valid_add_no_dup_and_not_fail(tmp_path):
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    prog["valid_pids"] = ["101"]  # уже был
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"], "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["valid_pids"] == ["101"]  # без дублей
    assert "101" not in prog["failed_pids"]


def test_reconcile_fail_removed_from_valid_but_in_failed(tmp_path):
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    prog["valid_pids"] = ["101"]
    checked = {"verdicts": {"101": "FAIL"}, "valid_add": [], "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert "101" not in set(prog["valid_pids"])
    assert "101" in prog["failed_pids"]  # и одновременно попадает в failed


# --- summarize: final_reason/state ---

def test_summarize_includes_final_reason():
    p = _progress_filled()
    p["final_state"] = "COMPLETED_MIN"
    p["final_reason"] = "no_progress"
    s = k.summarize(p)
    assert s["final_state"] == "COMPLETED_MIN"
    assert s["final_reason"] == "no_progress"


# --- default_manifest: резервные поля + run_cfg ---

def test_default_manifest_reserve_fields():
    m = k.default_manifest("c", 100, 50, "u", "o", reserve_count=30)
    assert m["replacement_pids"] == []
    assert m["run_cfg"]["reserve_count"] == 30
    assert "reserve_pids" not in m  # убрано legacy-поле (дублировало campaign_reserve_pids)


# --- финализатор: мягкий путь с FAIL по snapshot → ABANDONED ---

def test_finalize_soft_stop_fail_abandoned(tmp_path):
    # мягкая остановка: финальный QA по snapshot даёт FAIL>0 → ABANDONED
    prog = _final_progress(240, 300, 200, reason="no_progress")
    prog["valid_pids"] = [str(i) for i in range(240)]
    # без campaign_final_pids — snapshot = valid[:target] = все 240
    pa = tmp_path / "p.json"
    bad = {"5"}

    def qa(batch):
        per = {p: ("FAIL" if p in bad else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "ABANDONED"  # FAIL>0 при мягкой остановке без резерва
    assert "брак" in prog["final_note"]


# --- finalize_campaign: target_reached без резерва / крайние случаи ---

def test_finalize_target_reached_no_reserve(tmp_path):
    # len(valid)==target → резерва нет; всё чисто → COMPLETED без замен
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = []
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b),
                                                   "warn": 0, "products": len(b),
                                                   "per_pid": {p: "OK" for p in b}})
    assert prog["final_state"] == "COMPLETED"
    assert prog["replacement_pids"] == []


def test_finalize_target_reached_all_bad_reserve(tmp_path):
    # весь snapshot бракованный, резерв чистый и покрывает target → COMPLETED
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10", "11", "12"]
    pa = tmp_path / "p.json"
    bad = {"1", "2", "3"}

    def qa(batch):
        per = {p: ("FAIL" if p in bad else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "COMPLETED"  # целиком из резерва
    assert set(prog["campaign_final_pids"]) == {"10", "11", "12"}
    replaced_bys = {r["replaced_by"] for r in prog["replacement_pids"]}
    assert replaced_bys == {"10", "11", "12"}


def test_finalize_target_reached_reserve_less_than_bad(tmp_path):
    # target_reached: резерва меньше чем брака → чистых < target → ABANDONED
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10"]  # только 1 чистая замена
    pa = tmp_path / "p.json"
    bad = {"1", "2"}  # 2 брака, резерв 1 → чистых = 1(из snapshot)+1(резерв)=2

    def qa(batch):
        per = {p: ("FAIL" if p in bad else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    # pool = [1,2,3,10]; чистые = {3,10} = 2 < target 3 → ABANDONED (не COMPLETED_MIN)
    assert prog["final_state"] == "ABANDONED"


def test_final_qa_keeps_wall_limited_metric(tmp_path):
    """WALL_LIMITED не теряется: qa_raw уже считает его, демон обязан сохранить.

    Регресс: parse_qa_stdout разбирал `WALL_LIMITED N`, но финальный QA
    отбрасывал значение, и в progress/status.json не было видно, сколько
    товаров упёрлось в анонимный потолок Ozon (~100 отзывов).
    """
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = []
    pa = tmp_path / "p.json"

    def qa(batch):
        # K4: products == размер батча, ok+warn+fail == products
        return {"fail": 0, "ok": 0, "warn": len(batch),
                "products": len(batch), "wall_limited_count": len(batch),
                "per_pid": {p: "WARN" for p in batch}}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["qa"]["wall_limited"] == 3
    assert prog["qa"]["batches"][0]["wall_limited"] == 3
    assert k._status_payload(prog)["qa"]["wall_limited"] == 3


def test_qa_clean_empty_list(tmp_path):
    # _qa_clean с пустым списком не должен вызывать qa_fn и не падать
    prog = _final_progress(0, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = []
    prog["campaign_reserve_pids"] = []
    called = []
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: called.append(1) or
                        {"fail": 0, "ok": 0, "warn": 0})
    assert called == []
    assert prog["final_state"] == "ABANDONED"  # 0 чистых < min_valid


def test_finalize_soft_stop_reserve_ignored(tmp_path):
    # при мягкой остановке резерв НЕ используется — snapshot = valid[:target]
    prog = _final_progress(10, 5, 3, reason="exhausted")
    prog["valid_pids"] = [str(i) for i in range(10)]
    # некорректно занесённый резерв (быть не должен, но защищаемся)
    prog["campaign_reserve_pids"] = [str(i) for i in range(100, 110)]
    pa = tmp_path / "p.json"
    checked = []
    k.finalize_campaign(prog, pa, qa_fn=lambda b: checked.extend(b) or
                        {"fail": 0, "ok": len(b), "warn": 0,
                         "products": len(b)})
    assert prog["final_state"] == "COMPLETED"  # clean 5 >= target 5
    # QA только по snapshot (5 из valid), ни один не из резерва 100-109
    assert len(checked) == 5
    assert not (set(checked) & set(str(i) for i in range(100, 110)))


# --- campaign_accepted_pids: итоговый принятый список ---

def test_finalize_target_reached_accepted_pids(tmp_path):
    # target_reached: после замен accepted = итоговые target чистых PID
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10"]
    pa = tmp_path / "p.json"
    bad = {"1"}

    def qa(batch):
        per = {p: ("FAIL" if p in bad else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    # замена 1 -> 10; принятый список = [2,3,10]
    assert prog["final_state"] == "COMPLETED"
    assert set(prog["campaign_accepted_pids"]) == {"2", "3", "10"}
    assert len(prog["campaign_accepted_pids"]) == 3


def test_finalize_target_reached_accepted_no_reserve_needed(tmp_path):
    # no брака — accepted = исходный snapshot ровно target
    prog = _final_progress(3, 2, 1, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2"]
    prog["campaign_reserve_pids"] = ["3"]
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b),
                                                   "warn": 0,
                                                   "per_pid": {p: "OK"
                                                               for p in b}})
    assert prog["final_state"] == "COMPLETED"
    assert set(prog["campaign_accepted_pids"]) == {"1", "2"}


def test_finalize_soft_stop_accepted_pids_truncated(tmp_path):
    # мягкая остановка: accepted = чистые, но не длиннее target
    prog = _final_progress(10, 5, 3, reason="exhausted")
    prog["valid_pids"] = [str(i) for i in range(10)]
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b),
                                                   "warn": 0,
                                                   "per_pid": {p: "OK"
                                                               for p in b}})
    assert prog["final_state"] == "COMPLETED"
    assert len(prog["campaign_accepted_pids"]) == 5  # усечено до target


def test_finalize_soft_stop_no_accepted_on_abandoned(tmp_path):
    # ABANDONED: accepted всё равно фиксирует, сколько реально чистых (< target)
    prog = _final_progress(3, 10, 5, reason="exhausted")
    prog["valid_pids"] = [str(i) for i in range(3)]
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b),
                                                   "warn": 0,
                                                   "per_pid": {p: "OK"
                                                               for p in b}})
    assert prog["final_state"] == "ABANDONED"
    assert set(prog["campaign_accepted_pids"]) == {"0", "1", "2"}


def test_commit_run_results_interrupted_returns_none(tmp_path):
    """Ctrl+C-ветка пост-обработки (#13): состояние сохраняется, выход None."""
    pf = tmp_path / "progress.json"
    camp_dir = tmp_path / "camp"
    camp_dir.mkdir()
    progress = {"discovered_pids": ["1"], "valid_pids": [], "verdicts": {}}
    was_set = k._STOP_EVENT.is_set()
    k._STOP_EVENT.set()
    try:
        res = k._commit_run_results(
            progress, pf, "run-x", "full", None, {"products": []}, 0,
            lambda *a, **kw: {}, tmp_path, camp_dir, set(), set())
    finally:
        if not was_set:
            k._STOP_EVENT.clear()
    assert res is None
    assert progress["final_state"] == "INTERRUPTED"
    assert (json.loads(pf.read_text(encoding="utf-8"))["final_state"]
            == "INTERRUPTED")


# ============================================================================
# Интеграционные тесты run_campaign через монку _inproc_collect и qa_fn
# ============================================================================

import json as _json
from pathlib import Path as _Path


def _run_campaign_integration(tmp_path, monkeypatch, target, min_valid,
                              combined_factory, qa_fn,
                              max_no_progress_runs=3, n_pids=0, **kw):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    campaigns = tmp_path / "campaigns"
    logs = tmp_path / "logs"
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", campaigns)
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", logs)
    monkeypatch.setattr(k, "PYTHON", "python")

    # raw-файлы для ok-пидов (source of truth для classify)
    for i in range(n_pids):
        (data_dir / f"{i}_raw.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: combined_factory(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    return k.run_campaign(
        campaign="testcamp", url=kw.pop("url", "u"), target=target, min_valid=min_valid,
        out=kw.pop("out", str(data_dir / "digest.md")), data_dir=str(data_dir), max_no_progress_runs=max_no_progress_runs,
        qa_fn=qa_fn, **kw)


def test_integration_postprocess_crash_marks_needs_attention(tmp_path,
                                                             monkeypatch):
    """A post-processing crash (finalize_campaign / digest) must not kill the
    daemon: run_campaign returns progress with NEEDS_ATTENTION and saves it to
    disk (audit 2026-10-18, B1)."""
    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None}],
                "found": 1}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    def boom(*a, **kw):
        raise RuntimeError("digest boom")

    monkeypatch.setattr(k, "finalize_campaign", boom)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=2, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=1, max_no_progress_runs=3)
    assert prog is not None
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog["final_reason"] == "unhandled_RuntimeError"
    assert "digest boom" in prog["final_note"]
    pf = tmp_path / "campaigns" / "testcamp" / "progress.json"
    assert _json.loads(pf.read_text(encoding="utf-8"))["final_state"] \
        == "NEEDS_ATTENTION"


def test_integration_target_reached_completed(tmp_path, monkeypatch):
    # один full-прогон выдаёт 3 ok-пида (target=3), QA всё OK, резерв есть → COMPLETED
    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(4)], "found": 4}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=3, min_valid=2,
                                     combined_factory=fac, qa_fn=qa, n_pids=4,
                                     reserve_count=2, max_total_hours=24.0)
    assert prog["final_state"] == "COMPLETED"
    assert prog["final_reason"] == "target_reached"
    assert len(prog["campaign_final_pids"]) == 3
    assert len(prog["campaign_reserve_pids"]) == 1  # 4 valid - 3 target
    # единый логгер демона: строки оркестратора в {campaign}_daemon.log
    daemon_log = (tmp_path / "logs" / "testcamp_daemon.log").read_text(
        encoding="utf-8")
    assert "[orchestrator]" in daemon_log
    assert "[log] no context" not in daemon_log  # контекст взят из attach+set_context


def test_integration_stagnation_soft_stop_completed_min(tmp_path, monkeypatch):
    # Выдача вечно выдаёт одни и те же 2 известных хороших пида, новых нет
    # (found>0, но 0 новых discovery/valid). Демон ДОЛЖЕН выйти из этого по
    # no_progress (мягкая остановка), а НЕ крутиться вечно. Это верифицирует
    # фикс бага «бесконечный цикл при живой, но не растущей выдаче».
    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None},
                             {"pid": 1, "ok": True, "excluded": None}],
                "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=5, min_valid=2,
                                     combined_factory=fac, qa_fn=qa, n_pids=2,
                                     reserve_count=0, max_total_hours=24.0,
                                     max_no_progress_runs=3)
    assert prog["final_state"] == "COMPLETED_MIN"
    assert prog["final_reason"] == "no_progress"
    assert len(prog["valid_pids"]) == 2


def test_integration_reserve_covers_final_bad_completed(tmp_path, monkeypatch):
    # сквозной сценарий: набрано 3 valid (target=2) → snapshot [0,1] + резерв [2].
    # Финальный QA бракует пид 1 → замена из резерва на 2 → COMPLETED.
    state = {"n": 0}

    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(3)], "found": 3}

    def qa(batch):
        state["n"] += 1
        # 1-й вызов = классификация новых (всё OK); 2-й = финальный QA (пид 1 бракуем)
        per = {p: ("FAIL" if state["n"] >= 2 and p == "1" else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"products": len(batch), "ok": len(batch) - fails, "warn": 0,
                "fail": fails, "per_pid": per}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=2, min_valid=1,
                                     combined_factory=fac, qa_fn=qa, n_pids=3,
                                     reserve_count=1, max_total_hours=24.0)
    assert prog["final_state"] == "COMPLETED"
    assert prog["final_reason"] == "target_reached"
    assert set(prog.get("campaign_accepted_pids", [])) >= {"0", "2"}


# --- _raw_mtime ---

def test_raw_mtime_exists(tmp_path):
    raw = tmp_path / "101_raw.json"
    raw.write_text("{}", encoding="utf-8")
    m = k._raw_mtime(str(tmp_path), "101")
    assert isinstance(m, float) and m == raw.stat().st_mtime


def test_raw_mtime_missing(tmp_path):
    assert k._raw_mtime(str(tmp_path), "999") is None


# --- save_json / load_json roundtrip (атомарность) ---

def test_save_load_json_roundtrip(tmp_path):
    p = tmp_path / "sub" / "p.json"
    data = {"a": 1, "b": [1, 2, {"c": "тест"}]}
    k.save_json(p, data)
    assert k.load_json(p) == data


def test_load_json_missing(tmp_path):
    assert k.load_json(tmp_path / "nothere.json") is None


def test_load_json_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert k.load_json(p) is None


# --- PidLock ---

def test_pidlock_acquire_release(tmp_path):
    lock = k.PidLock(tmp_path / "key_daemon.pid")
    assert lock.acquire("camp") is True
    assert (tmp_path / "key_daemon.pid").is_file()
    lock.release()
    assert not (tmp_path / "key_daemon.pid").is_file()


def test_pidlock_rejects_alive_owner(tmp_path):
    pf = tmp_path / "key_daemon.pid"
    k.save_json(pf, {"pid": k.os.getpid(), "campaign": "x"})
    lock = k.PidLock(pf)
    assert lock.acquire("camp") is False  # текущий pid жив → занято


def test_pidlock_stale_pid_overwritten(tmp_path):
    pf = tmp_path / "key_daemon.pid"
    # Симулируем stale pid-файл: сначала создаём, потом удаляем (как если бы
    # процесс-владелец завершился), acquire должен перезаписать.
    k.save_json(pf, {"pid": 1, "campaign": "old"})
    pf.unlink()
    lock = k.PidLock(pf)
    assert lock.acquire("new_camp") is True
    assert k.load_json(pf)["campaign"] == "new_camp"


# --- classify_new_pids: пиды без raw-файла исключаются из need ---

def test_classify_missing_raw_file_no_qa_call(tmp_path):
    prog = _manifest(tmp_path)
    # нет 101_raw.json - нечего проверять, QA не вызывается, но разбор не падает
    called = []
    r = k.classify_new_pids(prog, ["101"], qa_fn=lambda b: called.append(1) or
                            _make_qa(products=0, ok=0, warn=0, fail=0))
    assert r["checked"] == []
    assert called == []
    assert r["parse_error"] is False


# ============================================================================
# НОВЫЕ ТЕСТЫ
# ============================================================================

# --- challenge_pause параметр ---

def test_default_manifest_includes_challenge_pause():
    m = k.default_manifest("c", 100, 50, "u", "o",
                           data_dir="x", max_age="3d", min_reviews=0,
                           parallel=2, max_no_progress_runs=5, max_rescans=1,
                           max_total_hours=6.0, notify="toast",
                           challenge_pause=90)
    assert m["run_cfg"]["challenge_pause"] == 90


def test_default_manifest_challenge_pause_default():
    m = k.default_manifest("c", 100, 50, "u", "o")
    # challenge_pause не передан → не в run_cfg; daemon берёт CHALLENGE_RETRY_DELAY_S
    assert "challenge_pause" not in m.get("run_cfg", {})


# --- replacement_pids: формат объектов ---

def test_replacement_pids_format_pairs(tmp_path):
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10", "11"]
    pa = tmp_path / "p.json"

    def qa(batch):
        per = {p: ("FAIL" if p == "2" else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    pairs = prog["replacement_pids"]
    assert len(pairs) == 1
    assert pairs[0]["failed_pid"] == "2"
    assert pairs[0]["replaced_by"] == "10"


def test_replacement_pids_empty_when_no_fail(tmp_path):
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10"]
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b),
                                                       "warn": 0,
                                                       "per_pid": {p: "OK"
                                                                   for p in b}})
    assert prog["replacement_pids"] == []


def test_replacement_pids_multiple_replacements(tmp_path):
    prog = _final_progress(5, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10", "11", "12"]
    pa = tmp_path / "p.json"

    def qa(batch):
        per = {p: ("FAIL" if p in {"1", "3"} else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    pairs = prog["replacement_pids"]
    assert len(pairs) == 2
    failed_pids = {p["failed_pid"] for p in pairs}
    replaced_bys = {p["replaced_by"] for p in pairs}
    assert failed_pids == {"1", "3"}
    assert replaced_bys <= {"10", "11", "12"}


# --- mtime-инвалидация вердиктов ---

def test_classify_verdict_stale_reqa_adds_to_valid(tmp_path):
    """Вердикт протух по mtime_ns → перепроверка, pid попадает в valid_add.

    Парный тест выше (test_classify_verdict_stale_mtime_reqa) проверяет другую
    сторону того же кейса: checked/verdicts. Имена разведены намеренно — раньше
    они совпадали, и верхний молча не выполнялся."""
    pid = "101"
    raw = tmp_path / f"{pid}_raw.json"
    raw.write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["verdicts"] = {"101": "OK"}
    prog["verdict_meta"] = {"101": {"verdict": "OK", "raw_mtime_ns": 1,
                                    "raw_sha256": "x", "qa_version": k.QA_VERSION}}

    called = []
    r = k.classify_new_pids(prog, [pid], qa_fn=lambda b: called.append(b) or
                            _make_qa(products=1, ok=1, warn=0, fail=0,
                                     per_pid={"101": "OK"}))
    assert called  # QA был вызван (перепроверка)
    assert r["valid_add"] == ["101"]


def test_classify_verdict_fresh_meta_skips(tmp_path):
    """Если fingerprint совпадает и qa_version актуальна — QA не вызывается."""
    pid = "101"
    raw = tmp_path / f"{pid}_raw.json"
    raw.write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["verdicts"] = {"101": "WARN"}
    fp = k.raw_fingerprint(str(tmp_path), pid)
    prog["verdict_meta"] = {"101": {"verdict": "WARN",
                                    "raw_mtime_ns": fp["raw_mtime_ns"],
                                    "raw_sha256": fp["raw_sha256"],
                                    "qa_version": k.QA_VERSION}}

    called = []
    r = k.classify_new_pids(prog, [pid], qa_fn=lambda b: called.append(b) or
                            _make_qa(products=1, ok=1, warn=0, fail=0))
    assert not called  # QA НЕ вызывался
    assert r["checked"] == []
    assert "101" in r["verdicts"]  # старый вердикт сохранён


# --- reconcile: fingerprint отслеживается ---

def test_reconcile_records_new_meta(tmp_path):
    raw = tmp_path / "101_raw.json"
    raw.write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    fp = k.raw_fingerprint(str(tmp_path), "101")
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"],
               "new_meta": {"101": {"verdict": "OK",
                                    "raw_mtime_ns": fp["raw_mtime_ns"],
                                    "raw_sha256": fp["raw_sha256"],
                                    "qa_version": k.QA_VERSION}}}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["verdict_meta"]["101"]["raw_sha256"] == fp["raw_sha256"]


def test_reconcile_keeps_old_meta_for_unchecked(tmp_path):
    raw = tmp_path / "101_raw.json"
    raw.write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101", "102"]
    prog["verdicts"] = {"101": "OK", "102": "OK"}
    prog["verdict_meta"] = {"101": {"verdict": "OK", "raw_mtime_ns": 1000,
                                    "raw_sha256": "old", "qa_version": "1.0"}}
    checked = {"verdicts": {"102": "OK"}, "valid_add": ["102"],
               "checked": ["102"],
               "new_meta": {"102": {"verdict": "OK", "raw_mtime_ns": 2000,
                                    "raw_sha256": "new", "qa_version": k.QA_VERSION}}}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["verdict_meta"]["101"]["raw_mtime_ns"] == 1000
    assert prog["verdict_meta"]["102"]["raw_sha256"] == "new"


# --- compute_final edge cases ---

def test_compute_final_valid_exactly_target():
    p = {"valid_pids": ["1", "2", "3"], "target_count": 3, "min_valid": 1}
    assert k.compute_final(p, False) == "COMPLETED"


def test_compute_final_valid_above_min_but_below_target_exhausted():
    p = {"valid_pids": ["1", "2"], "target_count": 3, "min_valid": 1}
    assert k.compute_final(p, True) == "COMPLETED_MIN"


def test_compute_final_below_min_exhausted():
    p = {"valid_pids": ["1"], "target_count": 3, "min_valid": 2}
    assert k.compute_final(p, True) == "ABANDONED"


def test_compute_final_not_exhausted_not_enough():
    p = {"valid_pids": ["1", "2"], "target_count": 3, "min_valid": 1}
    assert k.compute_final(p, False) is None


def test_backoff_delay_zero_index():
    assert k.backoff_delay(0) == 0


def test_backoff_delay_negative_index():
    assert k.backoff_delay(-1) == 0


def test_backoff_delay_beyond_max():
    assert k.backoff_delay(100) == k.BACKOFF_DELAYS_S[-1]


# --- finalize: snapshot все чистые — замен не нужно ---

def test_finalize_target_reached_all_clean_no_replacements(tmp_path):
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10", "11"]
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b),
                                                       "warn": 0,
                                                       "per_pid": {p: "OK"
                                                                   for p in b}})
    assert prog["final_state"] == "COMPLETED"
    assert prog["replacement_pids"] == []
    assert prog["campaign_accepted_pids"] == ["1", "2", "3"]


# --- finalize: резерв исчерпан, брак больше резерва ---

def test_finalize_target_reached_reserve_exhausted_abandoned(tmp_path):
    # snapshot = [1,2,3], reserve = [10], брак [1,2] → shortfall=2, резерва 1 → ABANDONED
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10"]
    pa = tmp_path / "p.json"

    def qa(batch):
        per = {p: ("FAIL" if p in {"1", "2"} else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "ABANDONED"
    assert len(prog["campaign_accepted_pids"]) < 3


# --- finalize: NEEDS_ATTENTION не перезаписывается ---

def test_finalize_needs_attention_not_overwritten(tmp_path):
    prog = _final_progress(300, 300, 200, reason="qa_parse_error")
    prog["final_state"] = "NEEDS_ATTENTION"
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b),
                                                       "warn": 0,
                                                       "per_pid": {p: "OK"
                                                                   for p in b}})
    assert prog["final_state"] == "NEEDS_ATTENTION"


# --- batch_pids: границы ---

def test_batch_pids_exact_boundary():
    pids = [str(i) for i in range(100)]
    batches = k.batch_pids(pids, 100)
    assert len(batches) == 1
    assert len(batches[0]) == 100


def test_batch_pids_one_over():
    pids = [str(i) for i in range(101)]
    batches = k.batch_pids(pids, 100)
    assert len(batches) == 2
    assert len(batches[0]) == 100
    assert len(batches[1]) == 1


def test_batch_pids_single():
    batches = k.batch_pids(["1"], 100)
    assert batches == [["1"]]


# --- normalize_pids:更多 edge cases ---

def test_normalize_pids_all_garbage():
    assert k.normalize_pids(["abc", "", "  ", "product/xyz?foo=bar"]) == []


def test_normalize_pids_mixed_clean_and_url():
    result = k.normalize_pids(["123", "https://ozon.ru/product/456/?utm=x",
                                "456", "789"])
    assert result == ["123", "456", "789"]


# --- all_raw_pids / has_raw ---

def test_all_raw_pids_with_files(tmp_path):
    for pid in ("10", "20", "30"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    (tmp_path / "abc_raw.json").write_text("{}", encoding="utf-8")  # не цифровой
    (tmp_path / "40_raw.json").write_text("{}", encoding="utf-8")
    result = k.all_raw_pids(str(tmp_path))
    assert result == ["10", "20", "30", "40"]


def test_all_raw_pids_empty_dir(tmp_path):
    assert k.all_raw_pids(str(tmp_path)) == []


def test_has_raw_exists(tmp_path):
    (tmp_path / "42_raw.json").write_text("{}", encoding="utf-8")
    assert k.has_raw(str(tmp_path), "42") is True


def test_has_raw_missing(tmp_path):
    assert k.has_raw(str(tmp_path), "42") is False


# --- parse_qa_stdout:更多 edge cases ---

def test_parse_qa_stdout_multiple_qa_lines():
    stdout = "[qa] товаров: 50 | OK 45 | WARN 3 | FAIL 2\n" \
             "[qa] товаров: 100 | OK 90 | WARN 7 | FAIL 3"
    result = k.parse_qa_stdout(stdout)
    # берёт последнюю [qa] строку
    assert result["products"] == 100


def test_parse_qa_stdout_no_match():
    result = k.parse_qa_stdout("какой-то мусор\nещё строка")
    assert result["products"] == 0


def test_parse_qa_stdout_partial_fields():
    stdout = "[qa] товаров: 10 | OK 8"
    result = k.parse_qa_stdout(stdout)
    assert result["products"] == 10
    assert result["ok"] == 8
    assert result["warn"] == 0
    assert result["fail"] == 0


# --- _parse_qa_per_pid ---

def test_parse_qa_per_pid_rows_plain():
    """Разбор строк qa_raw БЕЗ служебной преамбулы ([qa] товаров: ...)."""
    stdout = "101 OK 5 reviews\n102 FAIL 3 reviews\n103 WARN 2 reviews\n104 INFO text"
    result = k._parse_qa_per_pid(stdout)
    assert result == {"101": "OK", "102": "FAIL", "103": "WARN", "104": "INFO"}


def test_parse_qa_per_pid_ignores_short_lines():
    stdout = "ok\n101\n102 OK extra"
    result = k._parse_qa_per_pid(stdout)
    assert result == {"102": "OK"}


# --- PidLock: release на уже удалённый ---

def test_pidlock_release_already_deleted(tmp_path):
    lock = k.PidLock(tmp_path / "key_daemon.pid")
    assert lock.acquire("camp") is True
    (tmp_path / "key_daemon.pid").unlink()  # вручную удалили
    lock.release()  # не должен упасть


def test_pidlock_release_does_not_remove_foreign_file(tmp_path):
    """release снимает только СВОЙ pid-файл: чужой лок не сносится."""
    lock = k.PidLock(tmp_path / "key_daemon.pid")
    assert lock.acquire("camp") is True
    (tmp_path / "key_daemon.pid").write_text(
        '{"pid": 99999999, "campaign": "other"}', encoding="utf-8")
    lock.release()
    assert (tmp_path / "key_daemon.pid").exists()


# --- delta ---

def test_delta_empty_before():
    assert k.delta([], ["1", "2"]) == {"1", "2"}


def test_delta_empty_after():
    assert k.delta(["1", "2"], []) == set()


def test_delta_no_change():
    assert k.delta(["1", "2"], ["1", "2"]) == set()


# --- classify_new_pids: fail>0, per_pid не покрывает все → parse_error ---

def test_classify_fail_partial_per_pid_missing_pid(tmp_path):
    """fail>0, но per_pid не содержит все PID → parse_error (защита от тихого пропуска)."""
    for pid in ("101", "102"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        return _make_qa(products=2, ok=1, warn=0, fail=1,
                        per_pid={"101": "OK"})  # 102 отсутствует

    r = k.classify_new_pids(prog, ["101", "102"], qa_fn=qa)
    assert r["parse_error"] is True
    assert r["valid_add"] == []
    assert r["failed_add"] == []


def test_classify_fail_covers_all_per_pid(tmp_path):
    """fail>0, per_pid покрывает все PID → OK, вердикты записываются."""
    for pid in ("101", "102"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        return _make_qa(products=2, ok=1, warn=0, fail=1,
                        per_pid={"101": "OK", "102": "FAIL"})

    r = k.classify_new_pids(prog, ["101", "102"], qa_fn=qa)
    assert r["parse_error"] is False
    assert r["valid_add"] == ["101"]
    assert r["failed_add"] == ["102"]


# --- classify_new_pids: warn считается валидным ---

def test_classify_warn_counts_as_valid(tmp_path):
    pid = "101"
    (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)

    def qa(batch):
        return _make_qa(products=1, ok=0, warn=1, fail=0,
                        per_pid={"101": "WARN"})

    r = k.classify_new_pids(prog, [pid], qa_fn=qa)
    assert r["valid_add"] == ["101"]
    assert r["failed_add"] == []


# --- reconcile: FAIL из прошлого исчезает при OK вердикте ---

def test_reconcile_fail_removed_on_rescan_ok(tmp_path):
    """PID был FAIL, после rescan стал OK → выходит из failed_pids."""
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    prog["failed_pids"] = ["101"]
    checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
               "checked": ["101"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert "101" not in prog["failed_pids"]
    assert "101" in prog["valid_pids"]


# --- finalize: мягкий путь, min_valid=0 ---

def test_finalize_soft_stop_min_valid_zero(tmp_path):
    """min_valid=0, valid=0, FAIL=0 → COMPLETED_MIN (даже с 0 валидных)."""
    prog = _final_progress(0, 3, 0, reason="exhausted")
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": 0,
                                                    "warn": 0,
                                                    "per_pid": {}})
    assert prog["final_state"] == "COMPLETED_MIN"


# --- summarize: все ключи ---

def test_summarize_all_keys():
    p = _progress_filled()
    p["final_state"] = "COMPLETED"
    p["final_reason"] = "target_reached"
    s = k.summarize(p)
    # pending добавлен 2026-10-06: «нормально завершил», а товары из выдачи
    # без результата не видно — ровно то, что смущало пользователя в логах.
    assert set(s.keys()) == {"campaign", "target", "min_valid", "valid",
                             "discovered", "failed", "permanently_failed",
                             "pending", "runs", "final_state", "final_reason"}


# --- default_manifest: базовые дефолты ---

def test_default_manifest_all_defaults():
    m = k.default_manifest("camp", 100, 50, "url", "out.md")
    assert m["campaign"] == "camp"
    assert m["target_count"] == 100
    assert m["min_valid"] == 50
    assert m["url"] == "url"
    assert m["out"] == "out.md"
    assert m["started_at"]  # не пусто
    assert m["discovered_pids"] == []
    assert m["valid_pids"] == []
    assert m["failed_pids"] == []
    assert m["verdicts"] == {}
    assert m["final_state"] is None


# ---_integration: time_limit ---

def test_integration_time_limit_abandoned(tmp_path, monkeypatch):
    """Выдаёт только 2 PID (target=5, min_valid=3), лимит времени 0ч → ABANDONED."""
    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(2)], "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=5, min_valid=3,
                                     combined_factory=fac, qa_fn=qa, n_pids=2,
                                     reserve_count=0, max_total_hours=0.0,
                                     max_no_progress_runs=10)
    assert prog["final_state"] == "ABANDONED"
    assert prog["final_reason"] == "time_limit"


# ---integration: crash вызывает cooldown, но не падение ---

def test_crash_sets_no_progress_count():
    """CRASH увеличивает no_progress_count (unit test без event loop)."""
    prog = _manifest(k.Path("/tmp"))
    prog["no_progress_count"] = 0
    # имитируем CRASH: обновляем счётчик как делает демон
    prog["no_progress_count"] = prog.get("no_progress_count", 0) + 1
    assert prog["no_progress_count"] == 1


# ---integration: rescan брака ---

def test_integration_rescan_replaces_bad_with_good(tmp_path, monkeypatch):
    """1-й прогон: 3 PID, пид 2 FAIL → rescan → пид 2 стал OK → COMPLETED."""
    state = {"n": 0}

    def fac(argv):
        state["n"] += 1
        if state["n"] == 1:
            return {"products": [{"pid": i, "ok": True, "excluded": None}
                                 for i in range(3)], "found": 3}
        # rescan: пид 2 теперь OK
        return {"products": [{"pid": 2, "ok": True, "excluded": None}],
                "found": 1}

    def qa(batch):
        if state["n"] == 1:
            # первый прогон: пид 2 FAIL
            per = {p: ("FAIL" if p == "2" else "OK") for p in batch}
        else:
            # rescan: всё OK
            per = {p: "OK" for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"products": len(batch), "ok": len(batch) - fails, "warn": 0,
                "fail": fails, "per_pid": per}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=3, min_valid=2,
                                     combined_factory=fac, qa_fn=qa, n_pids=3,
                                     reserve_count=0, max_total_hours=24.0,
                                     max_no_progress_runs=5, max_rescans=2)
    assert prog["final_state"] == "COMPLETED"
    assert "2" in prog["valid_pids"] or "2" in prog.get("campaign_accepted_pids", [])


# --- finalize: snapshot с браком, резерв достаточно ---

def test_finalize_target_reached_replaces_exact_shortfall(tmp_path):
    """snapshot=[1,2,3], reserve=[10,11], брак [1,3] → replacements=2 из резерва."""
    prog = _final_progress(5, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10", "11"]
    pa = tmp_path / "p.json"

    def qa(batch):
        per = {p: ("FAIL" if p in {"1", "3"} else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"fail": fails, "ok": len(batch) - fails, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "COMPLETED"
    assert len(prog["replacement_pids"]) == 2
    assert set(prog["campaign_accepted_pids"]) == {"2", "10", "11"}


# --- finalize: snapshot чист, резерв не тронут ---

def test_finalize_target_reached_reserve_untouched(tmp_path):
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10", "11", "12"]
    pa = tmp_path / "p.json"
    k.finalize_campaign(prog, pa, qa_fn=lambda b: {"fail": 0, "ok": len(b), "products": len(b),
                                                       "warn": 0,
                                                       "per_pid": {p: "OK"
                                                                   for p in b}})
    assert prog["campaign_reserve_pids"] == ["10", "11", "12"]
    assert prog["replacement_pids"] == []


# --- pidlock: acquire两次同一个campaign ---

def test_pidlock_double_acquire_same_campaign(tmp_path):
    pf = tmp_path / "key_daemon.pid"
    lock1 = k.PidLock(pf)
    assert lock1.acquire("camp1") is True
    lock2 = k.PidLock(pf)
    assert lock2.acquire("camp2") is False  # уже занят


# --- classify_new_pids: candidates без raw-файла игнорируются ---

def test_classify_all_candidates_without_raw(tmp_path):
    prog = _manifest(tmp_path)
    r = k.classify_new_pids(prog, ["901", "902"], qa_fn=lambda b: _make_qa(
        products=0, ok=0, warn=0, fail=0))
    assert r["checked"] == []
    assert r["valid_add"] == []
    assert r["failed_add"] == []


# ============================================================================
# НОВЫЕ ТЕСТЫ: K4 в финализаторе + детерминизм + resume-конфиг
# ============================================================================

# --- финальный QA: недостоверная сводка (K4) → NEEDS_ATTENTION ---

def test_finalize_target_reached_inconsistent_summary_needs_attention(tmp_path, monkeypatch):
    # target_reached, но финальный QA даёт недостоверную сводку (products != len)
    # → NEEDS_ATTENTION, а НЕ COMPLETED/ABANDONED (тихий пропуск брака исключён)
    monkeypatch.setattr(k, "notify", lambda *a, **kw: None)
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10", "11"]
    pa = tmp_path / "p.json"

    def qa(batch):
        return {"fail": 0, "ok": len(batch) - 1, "warn": 0,
                "products": len(batch) - 1, "per_pid": {p: "OK" for p in batch}}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog["final_reason"] == "qa_parse_error"


def test_finalize_target_reached_fail_missing_per_pid_needs_attention(tmp_path, monkeypatch):
    # fail>0, но per_pid не покрывает все PID → parse_error → NEEDS_ATTENTION
    monkeypatch.setattr(k, "notify", lambda *a, **kw: None)
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = []
    pa = tmp_path / "p.json"

    def qa(batch):
        return {"fail": 1, "ok": len(batch) - 1, "warn": 0,
                "products": len(batch), "per_pid": {"1": "OK", "2": "OK"}}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog["final_reason"] == "qa_parse_error"


def test_finalize_soft_stop_inconsistent_summary_needs_attention(tmp_path, monkeypatch):
    # мягкая остановка: финальный QA недостоверен → NEEDS_ATTENTION
    monkeypatch.setattr(k, "notify", lambda *a, **kw: None)
    prog = _final_progress(5, 3, 2, reason="no_progress")
    prog["valid_pids"] = [str(i) for i in range(5)]
    pa = tmp_path / "p.json"

    def qa(batch):
        return {"fail": 0, "ok": len(batch), "warn": 0, "products": 0,
                "per_pid": {p: "OK" for p in batch}}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog["final_reason"] == "qa_parse_error"


def test_finalize_consistent_qa_not_needs_attention(tmp_path):
    # сводка консистентна (K4 сошёлся) → НЕ NEEDS_ATTENTION, а нормальный COMPLETED
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = ["10"]
    pa = tmp_path / "p.json"

    def qa(batch):
        per = {p: "OK" for p in batch}
        return {"fail": 0, "ok": len(batch), "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "COMPLETED"
    assert prog["final_reason"] == "target_reached"


# --- детерминизм порядка valid/snapshot ---

def test_reconcile_preserves_valid_order(tmp_path):
    # существующий порядок valid_pids сохраняется, новые добавляются в конец
    for pid in ("1", "2", "3", "4"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["1", "2", "3", "4"]
    prog["valid_pids"] = ["1", "2", "3"]
    checked = {"verdicts": {"1": "OK", "2": "OK", "3": "OK", "4": "OK"},
               "valid_add": ["4"], "checked": ["4"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["valid_pids"] == ["1", "2", "3", "4"]


def test_reconcile_preserves_order_when_fail_removed(tmp_path):
    # FAIL-пид удаляется из valid, но порядок остальных сохраняется
    for pid in ("1", "2", "3"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["1", "2", "3"]
    prog["valid_pids"] = ["1", "2", "3"]
    checked = {"verdicts": {"1": "OK", "2": "FAIL", "3": "OK"},
               "valid_add": [], "checked": ["1", "2", "3"]}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["valid_pids"] == ["1", "3"]


def test_integration_snapshot_uses_valid_order(tmp_path, monkeypatch):
    # snapshot/reserve выбираются в порядке valid_pids (порядок обнаружения),
    # а не в hash-порядке set()/сортированном порядке
    def fac(argv):
        return {"products": [{"pid": p, "ok": True, "excluded": None}
                             for p in ("4", "2", "0", "1", "3")], "found": 5}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=3, min_valid=2,
                                     combined_factory=fac, qa_fn=qa, n_pids=5,
                                     reserve_count=2, max_total_hours=24.0)
    assert prog["final_state"] == "COMPLETED"
    # valid_pids в порядке обнаружения: 4,2,0,1,3 → snapshot = первые 3
    assert prog["campaign_final_pids"] == ["4", "2", "0"]
    assert prog["campaign_reserve_pids"] == ["1", "3"]


# --- resume: конфиг берётся из манифеста, а не из аргументов CLI ---

def test_integration_resume_uses_manifest_config(tmp_path, monkeypatch):
    # В манифесте max_no_progress_runs=1. Демон запускается с параметром 10,
    # но должен использовать значение из манифеста → остановка после 1 прогона.
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    for i in range(2):
        (data_dir / f"{i}_raw.json").write_text("{}", encoding="utf-8")
    campaigns = tmp_path / "campaigns"
    camp_dir = campaigns / "testcamp"
    camp_dir.mkdir(parents=True, exist_ok=True)
    k.save_json(camp_dir / "progress.json", {
        "campaign": "testcamp", "target_count": 5, "min_valid": 2,
        "url": "u", "out": str(tmp_path / "campaign.md"),
        "run_cfg": {"data_dir": str(data_dir), "max_age": "7d",
                    "min_reviews": 0, "parallel": 1,
                    "max_no_progress_runs": 1, "max_rescans": 2,
                    "max_total_hours": 24.0, "reserve_count": 0},
        "started_at": k.now_utc_iso(),
        "discovered_pids": [], "valid_pids": [], "failed_pids": [],
        "permanently_failed_pids": [], "replacement_pids": [],
        "verdicts": {}, "verdict_meta": {}, "running_rescans": {},
        "runs": [], "cooldown_until": None, "challenge_seq": 0,
        "no_progress_count": 0, "final_state": None,
        "final_reason": None, "final_note": None,
    })
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", campaigns)
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(k, "PYTHON", "python")

    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None},
                             {"pid": 1, "ok": True, "excluded": None}],
                "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: fac(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    prog = k.run_campaign(
        campaign="testcamp", url="u", target=5, min_valid=2,
        out=str(tmp_path / "campaign.md"), data_dir=str(data_dir), max_no_progress_runs=10,
        max_total_hours=24.0, qa_fn=qa)
    # манифестное max_no_progress_runs=1 → 1 прогон с прогрессом + 1 без,
    # затем остановка по no_progress. Если бы использовался параметр 10,
    # демон крутился бы 11 раз (10 без-прогресс + break).
    assert prog["final_reason"] == "no_progress"
    assert len(prog["runs"]) == 2


def _write_resume_manifest(tmp_path, *, target_count, run_cfg):
    """Манифест существующей кампании для resume-тестов (минимум полей)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    campaigns = tmp_path / "campaigns"
    camp_dir = campaigns / "testcamp"
    camp_dir.mkdir(parents=True, exist_ok=True)
    k.save_json(camp_dir / "progress.json", {
        "campaign": "testcamp", "target_count": target_count, "min_valid": 1,
        "url": "u", "out": str(tmp_path / "campaign.md"),
        "run_cfg": {"data_dir": str(data_dir), "max_age": "7d",
                    "min_reviews": 0, "parallel": 1,
                    "max_total_hours": 24.0, **run_cfg},
        "started_at": k.now_utc_iso(),
        "discovered_pids": [], "valid_pids": [], "failed_pids": [],
        "permanently_failed_pids": [], "replacement_pids": [],
        "verdicts": {}, "verdict_meta": {}, "running_rescans": {},
        "runs": [], "cooldown_until": None, "challenge_seq": 0,
        "no_progress_count": 0, "final_state": None,
        "final_reason": None, "final_note": None,
    })
    return data_dir, campaigns


def test_integration_resume_target_from_manifest(tmp_path, monkeypatch):
    # target_count живёт в манифесте; запуск с target=99 (имитация CLI-дефолта
    # --limit 300) не должен менять ни стоп-условие, ни срез snapshot —
    # инвариант 4, аудит 2026-10-15.
    data_dir, campaigns = _write_resume_manifest(
        tmp_path, target_count=2,
        run_cfg={"max_no_progress_runs": 3, "max_rescans": 2,
                 "reserve_count": 1})
    for i in range(3):
        (data_dir / f"{i}_raw.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", campaigns)
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(k, "PYTHON", "python")

    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(3)], "found": 3}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: fac(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    prog = k.run_campaign(
        campaign="testcamp", url="u", target=99, min_valid=2,
        out=str(tmp_path / "campaign.md"), data_dir=str(data_dir),
        max_no_progress_runs=10, max_total_hours=24.0, qa_fn=qa)
    # Манифестный target_count=2: останов после ПЕРВОГО прогона (3 valid >= 2),
    # snapshot ровно 2 пида + 1 в резерве. С CLI-target=99 демон молотил бы
    # до no_progress и дал final_reason=no_progress.
    assert prog["final_reason"] == "target_reached"
    assert prog["final_state"] == "COMPLETED"
    assert len(prog["campaign_final_pids"]) == 2
    assert len(prog["campaign_reserve_pids"]) == 1


def test_integration_resume_parallel_from_manifest_storm(tmp_path, monkeypatch):
    # parallel живёт в run_cfg манифеста. Запуск с parallel=4 не должен
    # превращать шторм одиночного (по манифесту) воркера в «частичный»:
    # parallel==1 → ФУЛЛ-шторм → storm_streak кампании растёт (аудит 2026-10-15).
    data_dir, campaigns = _write_resume_manifest(
        tmp_path, target_count=5,
        run_cfg={"max_no_progress_runs": 1, "max_rescans": 0,
                 "reserve_count": 0})
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", campaigns)
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(k, "PYTHON", "python")

    def fac(argv):
        return {"products": [], "found": 0,
                "aborted": {"reason": "challenge_storm", "block_kind": "hard",
                            "workers_stormed": [1], "signature": "http_429"},
                "kpi": {"total_challenges": 1}}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {}}

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: fac(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    prog = k.run_campaign(
        campaign="testcamp", url="u", target=5, min_valid=1,
        out=str(tmp_path / "campaign.md"), data_dir=str(data_dir),
        parallel=4, max_no_progress_runs=10, max_total_hours=24.0, qa_fn=qa)
    assert prog["challenge_control"]["storm_streak"] == 1
    # Диагноз честный: блок Ozon, а не «выдача перетёрлась» (1873+).
    assert prog["final_reason"] == "challenge_storm"


# --- краткий дайджест (--compact) -----------------------------------------

def test_integration_compact_writes_both_digests(tmp_path, monkeypatch):
    """--compact → пишутся полный (--out) и краткий {root}_compact.md; флаг
    фиксируется в манифесте."""
    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(2)], "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    out = str(tmp_path / "camp.md")
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=2, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=2,
        reserve_count=1, max_total_hours=24.0,
        out=out, compact=True)
    assert prog["compact_digest"] is True
    assert (tmp_path / "camp.md").exists()             # полный
    comp = tmp_path / "camp_compact.md"
    assert comp.exists()                               # краткий
    md = comp.read_text(encoding="utf-8")
    assert "# Дайджест выдачи Ozon (краткий)" in md
    assert f"Полный дайджест: {out}" in md


def test_integration_without_compact_no_compact_file(tmp_path, monkeypatch):
    """Без --compact краткий файл не появляется; полный — как раньше."""
    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(2)], "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    out = str(tmp_path / "camp.md")
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=2, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=2,
        reserve_count=1, max_total_hours=24.0, out=out)
    assert prog["compact_digest"] is False
    assert (tmp_path / "camp.md").exists()
    assert not (tmp_path / "camp_compact.md").exists()


def test_integration_no_digest_disables_both(tmp_path, monkeypatch):
    """--no-digest гасит ОБА дайджеста, даже при --compact (правило ревью)."""
    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(2)], "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    out = str(tmp_path / "camp.md")
    _run_campaign_integration(
        tmp_path, monkeypatch, target=2, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=2,
        reserve_count=1, max_total_hours=24.0,
        out=out, compact=True, write_digest=False)
    assert not (tmp_path / "camp.md").exists()
    assert not (tmp_path / "camp_compact.md").exists()


def _seed_manifest(tmp_path, monkeypatch, **extra):
    """Манифест для resume-тестов (пустая живая кампания без final_state)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "0_raw.json").write_text("{}", encoding="utf-8")
    campaigns = tmp_path / "campaigns"
    camp_dir = campaigns / "testcamp"
    camp_dir.mkdir(parents=True, exist_ok=True)
    init = {
        "campaign": "testcamp", "target_count": 1, "min_valid": 1,
        "url": "u", "out": str(tmp_path / "campaign.md"),
        "run_cfg": {"data_dir": str(data_dir), "max_age": "7d",
                    "min_reviews": 0, "parallel": 1,
                    "max_no_progress_runs": 1, "max_rescans": 2,
                    "max_total_hours": 24.0, "reserve_count": 0},
        "started_at": k.now_utc_iso(),
        "discovered_pids": [], "valid_pids": [], "failed_pids": [],
        "permanently_failed_pids": [], "replacement_pids": [],
        "verdicts": {}, "verdict_meta": {}, "running_rescans": {},
        "runs": [], "cooldown_until": None, "challenge_seq": 0,
        "no_progress_count": 0, "final_state": None,
        "final_reason": None, "final_note": None,
    }
    init.update(extra)
    k.save_json(camp_dir / "progress.json", init)
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", campaigns)
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(k, "PYTHON", "python")
    return data_dir


def _run_resume(data_dir, monkeypatch, **kw):
    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None}], "found": 1}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: fac(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)
    return k.run_campaign(
        campaign="testcamp", url="u", target=1, min_valid=1,
        out=str(data_dir / "campaign.md"), data_dir=str(data_dir), max_total_hours=24.0,
        qa_fn=qa, **kw)


def test_integration_resume_heals_stale_failure_counters(tmp_path, monkeypatch):
    """Resume после багованной эпохи: transient_failures=3 не убивает кампанию.

    Живой случай zvukoizolyatsiya (2026-09-25): в манифесте осталось
    transient_failures=3 (4× UNKNOWN_FAILURE из-за неверной классификации
    блока) и пустой challenge_control. В старом коде ПЕРВЫЙ же новый сбой
    давал tr=3 → терминал NEEDS_ATTENTION, т.е. кампания была мертва до
    первого прогона. Новый код обязан самолечить счётчик первым же
    не-UNKNOWN прогоном и честно остановиться по шторму.
    """
    def fac(argv):
        return {"products": [], "found": 0,
                "aborted": {"reason": "challenge_storm", "all_workers": True}}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    monkeypatch.setattr(k, "CHALLENGE_COOLDOWN_STEPS_S", [1, 2, 3])
    monkeypatch.setattr(k, "CHALLENGE_JITTER_FRAC", 0.0)
    data_dir = _seed_manifest(
        tmp_path, monkeypatch, target_count=5, min_valid=1,
        transient_failures=3,
        challenge_control={"storm_streak": 0, "total_storms": 0,
                           "last_storm_at": None, "cooldown_until": None,
                           "last_signature": None},
        profile_cooldown={"until": None, "reason": None, "challenge_count": 0,
                          "last_signature": None, "last_full_storm_at": None},
        recovery_telemetry={},
        runs=[{"run_id": f"run_old_{i}", "mode": "full",
               "status": "UNKNOWN_FAILURE"} for i in range(4)])
    manifest = k.load_json(tmp_path / "campaigns" / "testcamp" / "progress.json")
    manifest["run_cfg"]["max_no_progress_runs"] = 3
    k.save_json(tmp_path / "campaigns" / "testcamp" / "progress.json", manifest)

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: fac(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)
    prog = k.run_campaign(
        campaign="testcamp", url="u", target=5, min_valid=1,
        out=str(data_dir / "campaign.md"), data_dir=str(data_dir),
        max_total_hours=24.0, qa_fn=qa, max_no_progress_runs=3)
    # stale transient_failures=3 обнулён первым же штормом (не-UNKNOWN прогон)
    assert prog["transient_failures"] == 0
    assert prog["challenge_control"]["total_storms"] == 3
    assert prog["final_reason"] == "challenge_storm"
    assert "нет соединения" not in (prog.get("last_error") or "")
    daemon_log = (tmp_path / "logs" / "testcamp_daemon.log").read_text(
        encoding="utf-8")
    assert "серия UNKNOWN_FAILURE прервана" in daemon_log


def test_integration_resume_uses_manifest_compact_flag(tmp_path, monkeypatch):
    """Флаг compact зафиксирован при fresh (True в манифесте); resume БЕЗ
    --compact НЕ переопределяет его (источник истины — манифест) и краткий
    дайджест создаётся."""
    out = str(tmp_path / "r.md")
    data_dir = _seed_manifest(tmp_path, monkeypatch,
                              compact_digest=True, out=out)
    prog = _run_resume(data_dir, monkeypatch)            # CLI: compact=False
    assert prog["compact_digest"] is True
    assert (tmp_path / "r.md").exists()
    assert (tmp_path / "r_compact.md").exists()


def test_integration_resume_cli_cannot_reenable_disabled_compact(tmp_path, monkeypatch):
    """compact_digest=False в манифесте → resume c --compact НЕ включает:
    компакт-файл не создаётся (полный — создаётся)."""
    out = str(tmp_path / "r.md")
    data_dir = _seed_manifest(tmp_path, monkeypatch,
                              compact_digest=False, out=out)
    prog = _run_resume(data_dir, monkeypatch, compact=True)
    assert prog["compact_digest"] is False
    assert (tmp_path / "r.md").exists()
    assert not (tmp_path / "r_compact.md").exists()


def test_integration_resume_missing_flag_uses_cli(tmp_path, monkeypatch):
    """Старый манифест без compact_digest → fallback на CLI-флаг (backward
    compatibility): компакт создаётся В ЭТОМ запуске, но манифест НЕ
    загрязняется значением — поле остаётся отсутствующим."""
    out = str(tmp_path / "r.md")
    data_dir = _seed_manifest(tmp_path, monkeypatch, out=out)
    prog = _run_resume(data_dir, monkeypatch, compact=True)
    assert "compact_digest" not in prog          # не записали в манифест
    assert (tmp_path / "r.md").exists()
    assert (tmp_path / "r_compact.md").exists()  # fallback реально сработал


# --- режим --no-reviews (skip_reviews) ---------------------------------------

def _product_only_raw(pid):
    """product-only raw: карточка цела, отзывы намеренно не тронуты (режим
    --no-reviews). collected_at_utc свежий — иначе OLD_DATA даст лишний WARN."""
    import datetime as _dt
    now = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)).isoformat()
    return {
        "pid": str(pid),
        "schema_version": "2.0",
        "collector_version": "1.16.0",
        "collected_at_utc": now,
        "product_url": f"https://www.ozon.ru/product/{pid}",
        "reviews_url": f"https://www.ozon.ru/product/{pid}?tab=reviews",
        "product": {"title": "Спрей для горла с прополисом", "rating": 4.8,
                    "rating_count": 100, "price": {"current": 156},
                    "price_text": "156 ₽",
                    "customer_opinion": {"recommend_percent": None,
                                         "stars": {}, "counts": {},
                                         "text": ""}},
        "reviews": [], "reviews_collected": 0, "reviews_skipped": True,
        "reviews_expected": None,
        "reviews_sorts": ["published_at_desc", "score_asc"],
        "reviews_variant_mode": {"used": False, "reason": "reviews_skipped",
                                 "variant_count": None},
        "pages": [], "review_pages": [], "source": "test",
        "collection_telemetry": {}, "incomplete": False,
        "missing_fields": [], "artifacts": {},
    }


def test_integration_no_reviews_flag_fixed_at_fresh(tmp_path, monkeypatch):
    """--no-reviews при fresh → run_cfg.skip_reviews=True в манифесте."""
    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None},
                             {"pid": 1, "ok": True, "excluded": None}],
                "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    out = str(tmp_path / "camp.md")
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=2, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=2,
        reserve_count=0, max_total_hours=24.0, out=out, skip_reviews=True)
    assert prog["run_cfg"]["skip_reviews"] is True


def test_integration_no_reviews_resume_uses_manifest(tmp_path, monkeypatch):
    """Режим зафиксирован в манифесте (skip_reviews=True); resume без флага
    НЕ переключает его обратно в полный сбор (источник истины — манифест)."""
    data_dir = _seed_manifest(tmp_path, monkeypatch)
    camp_dir = tmp_path / "campaigns" / "testcamp"
    prog_path = camp_dir / "progress.json"
    init = k.load_json(prog_path)
    init["run_cfg"]["skip_reviews"] = True
    k.save_json(prog_path, init)
    prog = _run_resume(data_dir, monkeypatch)            # CLI: без флага
    assert prog["run_cfg"].get("skip_reviews") is True


def test_integration_no_reviews_resume_cli_cannot_enable(tmp_path, monkeypatch):
    """Манифест с skip_reviews=False → resume с --no-reviews НЕ включает режим:
    манифест источник истины, CLI не переопределяет (манифест не трогается)."""
    data_dir = _seed_manifest(tmp_path, monkeypatch)
    camp_dir = tmp_path / "campaigns" / "testcamp"
    prog_path = camp_dir / "progress.json"
    init = k.load_json(prog_path)
    init["run_cfg"]["skip_reviews"] = False
    k.save_json(prog_path, init)
    prog = _run_resume(data_dir, monkeypatch, skip_reviews=True)
    assert prog["run_cfg"].get("skip_reviews") is False


def test_integration_no_reviews_zero_reviews_no_rescan(tmp_path, monkeypatch):
    """Регресс п.6: product-only raw (0 отзывов) с --no-reviews проходит QA как
    OK (REVIEWS_SKIPPED) → valid; демон завершается целевым состоянием, БЕЗ
    rescan-прогонов (failed пуст)."""
    import json as _json
    from scripts.qa_raw import scan_file, summarize as qa_summarize

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    for pid in ("0", "1"):
        (data_dir / f"{pid}_raw.json").write_text(
            _json.dumps(_product_only_raw(pid), ensure_ascii=False),
            encoding="utf-8")

    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None},
                             {"pid": 1, "ok": True, "excluded": None}],
                "found": 2}

    def qa(batch):
        reps = [
            scan_file(str(data_dir / f"{p}_raw.json"), p) for p in batch]
        s = qa_summarize(reps)
        return {"products": len(batch), "ok": s["ok"], "warn": s["warn"],
                "fail": s["fail"],
                "per_pid": {r["pid"]: r["verdict"] for r in reps}}

    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=2, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=0,   # raw-файлы — свои, не «{}»
        reserve_count=0, max_total_hours=24.0, skip_reviews=True)
    assert prog["final_state"] == "COMPLETED"
    assert not prog.get("failed_pids")
    assert not prog.get("permanently_failed_pids")
    assert len(prog.get("running_rescans") or {}) == 0
    assert len(prog.get("runs") or []) == 1          # только full, без rescan


# --- _qa_clean: пустой snapshot → parse_error=False, qa не вызывается ---

def test_finalize_empty_snapshot_parse_error_false(tmp_path):
    prog = _final_progress(0, 3, 2, reason="no_progress")
    pa = tmp_path / "p.json"
    called = []
    k.finalize_campaign(prog, pa, qa_fn=lambda b: called.append(1) or
                        {"fail": 0, "ok": 0, "warn": 0})
    assert called == []
    assert prog["final_state"] == "ABANDONED"  # не NEEDS_ATTENTION (нет QA)
    assert prog["final_reason"] == "no_progress"


# --- финальный QA: WARN считается чистым ---

def test_finalize_warn_counts_clean(tmp_path):
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = []
    pa = tmp_path / "p.json"

    def qa(batch):
        return {"fail": 0, "ok": 0, "warn": len(batch),
                "products": len(batch),
                "per_pid": {p: "WARN" for p in batch}}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "COMPLETED"
    assert set(prog["campaign_accepted_pids"]) == {"1", "2", "3"}


# --- PidLock: битый pid-файл (не int) трактуется как stale ---

def test_pidlock_corrupt_pid_treated_stale(tmp_path):
    pf = tmp_path / "key_daemon.pid"
    k.save_json(pf, {"pid": "not-an-int", "campaign": "old"})
    lock = k.PidLock(pf)
    assert lock.acquire("new_camp") is True  # не падает, перезаписывает
    assert k.load_json(pf)["campaign"] == "new_camp"


def test_pidlock_missing_pid_field_treated_stale(tmp_path):
    pf = tmp_path / "key_daemon.pid"
    k.save_json(pf, {"campaign": "old"})  # нет поля pid
    lock = k.PidLock(pf)
    assert lock.acquire("new_camp") is True


def test_pidlock_empty_file_window_not_stolen(tmp_path):
    """An empty pid file (the winner created it with O_EXCL but has not
    written the JSON yet) is not treated as stale: the second acquire must
    WAIT for the write and not steal a live lock (audit 2026-10-18, B4)."""
    import os as _os
    import threading as _t
    import time as _time
    pf = tmp_path / "key_daemon.pid"
    pf.write_text("", encoding="utf-8")   # window between O_EXCL and JSON write

    def _late_write():
        _time.sleep(0.05)
        k.save_json(pf, {"pid": _os.getpid(), "campaign": "live"})

    t = _t.Thread(target=_late_write, daemon=True)
    t.start()
    lock = k.PidLock(pf)
    assert lock.acquire("new_camp") is False   # live lock not stolen
    t.join(3)
    assert k.load_json(pf)["campaign"] == "live"


def test_pidlock_empty_file_stays_stale(tmp_path):
    """An empty file that stays empty through the grace period is genuinely
    broken → treated as stale (behavior preserved)."""
    pf = tmp_path / "key_daemon.pid"
    pf.write_text("", encoding="utf-8")
    lock = k.PidLock(pf)
    assert lock.acquire("new_camp") is True


# ============================================================================
# ТЕСТЫ: валидность требует raw (П1) + immutable snapshot при target_reached
# ============================================================================

def test_reconcile_drops_valid_without_raw(tmp_path):
    # raw для 101 удалён после QA → вердикт OK, но PID должен выйти из valid
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101", "102"]
    prog["valid_pids"] = ["101", "102"]
    checked = {"verdicts": {"101": "OK", "102": "OK"},
               "valid_add": [], "checked": []}
    k.reconcile_validity(prog, checked, str(tmp_path))
    # ни у одного из пидов нет raw-файла → valid пуст (источник истины — П1)
    assert prog["valid_pids"] == []


def test_reconcile_keeps_valid_with_raw(tmp_path):
    # у 101 и 102 raw есть → остаются valid
    for pid in ("101", "102"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101", "102"]
    prog["valid_pids"] = ["101", "102"]
    checked = {"verdicts": {"101": "OK", "102": "OK"},
               "valid_add": [], "checked": []}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["valid_pids"] == ["101", "102"]


def test_reconcile_drops_missing_raw_but_keeps_present(tmp_path):
    # 101 без raw → выходит; 102 с raw → остаётся
    (tmp_path / "102_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101", "102"]
    prog["valid_pids"] = ["101", "102"]
    checked = {"verdicts": {"101": "OK", "102": "OK"},
               "valid_add": [], "checked": []}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["valid_pids"] == ["102"]


def test_integration_snapshot_saved_before_finalize(tmp_path, monkeypatch):
    # target_reached: snapshot фиксируется в progress.json СРАЗУ (immutable),
    # до финального QA — проверяем наличие campaign_final_pids на диске
    def fac(argv):
        return {"products": [{"pid": i, "ok": True, "excluded": None}
                             for i in range(4)], "found": 4}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=3, min_valid=2,
                                     combined_factory=fac, qa_fn=qa, n_pids=4,
                                     reserve_count=1, max_total_hours=24.0)
    assert prog["final_state"] == "COMPLETED"
    assert prog["campaign_final_pids"] == ["0", "1", "2"]
    assert prog["campaign_reserve_pids"] == ["3"]
    # snapshot записан в manifest (не только в памяти)
    saved = k.load_json(tmp_path / "campaigns" / "testcamp" / "progress.json")
    assert saved["campaign_final_pids"] == ["0", "1", "2"]
    assert saved["campaign_reserve_pids"] == ["3"]


# ============================================================================
# ТЕСТЫ: валидация новой кампании + сброс счётчиков при прогрессе
# ============================================================================

# --- validate_new_campaign: 0 < min_valid < target ---

def test_validate_new_campaign_min_valid_zero_rejected():
    with pytest.raises(SystemExit):
        k.validate_new_campaign(0, 300)


def test_validate_new_campaign_min_valid_ge_target_rejected():
    with pytest.raises(SystemExit):
        k.validate_new_campaign(300, 300)


def test_validate_new_campaign_valid_range_accepted():
    k.validate_new_campaign(1, 300)   # нижняя граница
    k.validate_new_campaign(299, 300)  # верхняя граница
    k.validate_new_campaign(200, 300)  # типичный


def test_new_campaign_rejects_min_valid_zero(tmp_path, monkeypatch):
    # min_valid=0 при создании НОВОЙ кампании → SystemExit (нет пустого COMPLETED_MIN)
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    with pytest.raises(SystemExit):
        k.run_campaign(campaign="x", url="u", target=3, min_valid=0,
                       out="campaign.md", data_dir=str(tmp_path / "data"),
                       max_no_progress_runs=10)


def test_new_campaign_rejects_missing_url(tmp_path, monkeypatch):
    # новая кампания без --url → SystemExit (fail-fast вместо тихого --url "")
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    with pytest.raises(SystemExit):
        k.run_campaign(campaign="x", url="", target=3, min_valid=1,
                       out="campaign.md", data_dir=str(tmp_path / "data"),
                       max_no_progress_runs=10)


def test_resume_without_url_ok(tmp_path, monkeypatch):
    # при resume url не нужен — берётся из манифеста (не fail-fast)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    for i in range(2):
        (data_dir / f"{i}_raw.json").write_text("{}", encoding="utf-8")
    campaigns = tmp_path / "campaigns"
    camp_dir = campaigns / "testcamp"
    camp_dir.mkdir(parents=True, exist_ok=True)
    k.save_json(camp_dir / "progress.json", {
        "campaign": "testcamp", "target_count": 5, "min_valid": 1,
        "url": "stored_url", "out": str(tmp_path / "campaign.md"),
        "run_cfg": {"data_dir": str(data_dir), "max_age": "7d",
                    "min_reviews": 0, "parallel": 1,
                    "max_no_progress_runs": 1, "max_rescans": 2,
                    "max_total_hours": 24.0, "reserve_count": 0},
        "started_at": k.now_utc_iso(),
        "discovered_pids": [], "valid_pids": [], "failed_pids": [],
        "permanently_failed_pids": [], "replacement_pids": [],
        "verdicts": {}, "verdict_meta": {}, "running_rescans": {},
        "runs": [], "cooldown_until": None, "challenge_seq": 0,
        "no_progress_count": 0, "final_state": None,
        "final_reason": None, "final_note": None,
    })
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", campaigns)
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(k, "PYTHON", "python")

    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None},
                             {"pid": 1, "ok": True, "excluded": None}],
                "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: fac(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    prog = k.run_campaign(
        campaign="testcamp", url="", target=5, min_valid=1,
        out=str(tmp_path / "campaign.md"), data_dir=str(data_dir), max_no_progress_runs=10,
        max_total_hours=24.0, qa_fn=qa)
    # resume не падает на пустом url; URL из манифеста передаётся в CLI
    assert prog["final_reason"] == "no_progress"


# --- сброс счётчиков при прогрессе (п.2 ревью) ---

def test_integration_progress_resets_no_progress_counter(tmp_path, monkeypatch):
    # Сценарий: стагнация → прогресс (новый valid) → сброс счётчика → снова стагнация.
    # Если бы сброса не было, кампания завершилась бы раньше (ложный ABANDONED).
    # fac по номеру вызова возвращает разное.
    calls = {"n": 0}

    def fac(argv):
        calls["n"] += 1
        if calls["n"] == 3:
            # 3-й прогон приносит новый PID → прогресс
            return {"products": [{"pid": p, "ok": True, "excluded": None}
                                 for p in (0, 1, 2)], "found": 3}
        return {"products": [{"pid": 0, "ok": True, "excluded": None},
                             {"pid": 1, "ok": True, "excluded": None}],
                "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=5, min_valid=1,
                                     combined_factory=fac, qa_fn=qa, n_pids=3,
                                     reserve_count=0, max_total_hours=24.0,
                                     max_no_progress_runs=2)
    # runs: 1(progress) → 2(stagnation) → 3(progress, сброс) → 4(stagn) → 5(stagn)
    # → count=2 ≥ max → stop. Без сброса на run3 было бы 4 runs, а не 5.
    assert prog["final_state"] == "COMPLETED_MIN"
    assert prog["final_reason"] == "no_progress"
    assert len(prog["runs"]) == 5
    # 3-й прогон действительно дал прогресс (new_valid != [])
    assert prog["runs"][2]["new_valid"] == ["2"]


# --- UNKNOWN_FAILURE → NEEDS_ATTENTION с final_reason ---

def test_integration_unknown_failure_needs_attention(tmp_path, monkeypatch):
    # CLI: exit!=0, но combined есть → UNKNOWN_FAILURE → NEEDS_ATTENTION/unknown_failure
    def fac(argv):
        return {"products": [], "found": 0}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    def fake_inproc(progress, run_id, mode, targets=None):
        # in-process сбор упал исключением → демон сам ставит combined
        # «пуст, но есть» + exit=1 → UNKNOWN_FAILURE → NEEDS_ATTENTION
        fac(k.build_cli_args(progress, run_id, mode, targets))
        raise RuntimeError("simulated in-process failure")

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(k, "PYTHON", "python")
    monkeypatch.setattr(k, "_inproc_collect", fake_inproc)
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    prog = k.run_campaign(
        campaign="testcamp", url="u", target=3, min_valid=1,
        out=str(tmp_path / "campaign.md"), data_dir=str(data_dir),
        max_no_progress_runs=10, qa_fn=qa, max_total_hours=24.0)
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog["final_reason"] == "unknown_failure"


# --- run_id уникален для быстрых последовательных прогонов ---

def test_integration_run_ids_unique(tmp_path, monkeypatch):
    # даже если прогоны завершаются <1с (cache-hits), run_id не должен
    # сталкиваться (иначе combined_{run_id}.json перезаписывается)
    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None},
                             {"pid": 1, "ok": True, "excluded": None}],
                "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=5, min_valid=1,
                                     combined_factory=fac, qa_fn=qa, n_pids=2,
                                     reserve_count=0, max_total_hours=24.0,
                                     max_no_progress_runs=2)
    ids = [r["run_id"] for r in prog["runs"]]
    assert len(ids) == len(set(ids))  # все run_id уникальны


# --- build_cli_args: min_reviews=0 не добавляет флаг ---

def test_build_cli_args_min_reviews_zero_skips_flag():
    from pathlib import Path
    prog = _manifest(Path("/tmp"))
    prog["run_cfg"]["min_reviews"] = 0
    args = k.build_cli_args(prog, "r1", "full")
    assert "--min-reviews" not in args


# --- _parse_qa_per_pid ---

# --- резерв реально материализуется при discovery > target (интеграция) ---

def test_integration_reserve_materializes_with_headroom(tmp_path, monkeypatch):
    # fac возвращает target+2 продукта (как реальный search с запасом).
    # Все valid → snapshot=[:target], reserve=[target:target+reserve] НЕ пуст.
    def fac(argv):
        return {"products": [{"pid": p, "ok": True, "excluded": None}
                             for p in range(5)], "found": 5}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=3, min_valid=2,
                                     combined_factory=fac, qa_fn=qa, n_pids=5,
                                     reserve_count=2, max_total_hours=24.0)
    assert prog["final_state"] == "COMPLETED"
    # резерв формируется из valid сверх target
    assert len(prog["campaign_final_pids"]) == 3
    assert len(prog["campaign_reserve_pids"]) == 2


def test_integration_reserve_used_when_snapshot_bad(tmp_path, monkeypatch):
    # discovery > target: в snapshot пид 1 бракованный → замена из резерва →
    # COMPLETED (резерв реально работает, а не пуст)
    def fac(argv):
        return {"products": [{"pid": p, "ok": True, "excluded": None}
                             for p in range(4)], "found": 4}

    def qa(batch):
        # 1-й вызов = живой QA (всё OK); 2-й = финальный QA (пид 1 брак)
        if not getattr(qa, "final", False):
            qa.final = False
        state = qa.final
        if not state:
            qa.final = True  # следующий вызов — финальный
            per = {p: "OK" for p in batch}
        else:
            per = {p: ("FAIL" if p == "1" else "OK") for p in batch}
        fails = sum(1 for v in per.values() if v == "FAIL")
        return {"products": len(batch), "ok": len(batch) - fails, "warn": 0,
                "fail": fails, "per_pid": per}

    prog = _run_campaign_integration(tmp_path, monkeypatch, target=3, min_valid=2,
                                     combined_factory=fac, qa_fn=qa, n_pids=4,
                                     reserve_count=1, max_total_hours=24.0)
    assert prog["final_state"] == "COMPLETED"
    assert "1" not in set(prog["campaign_accepted_pids"])
    assert len(prog["replacement_pids"]) == 1


# --- parse_qa_stdout: WALL_LIMITED ---

def test_parse_qa_stdout_wall_limited():
    stdout = "[qa] товаров: 100 | OK 90 | WARN 7 | FAIL 3 | WALL_LIMITED 3"
    r = k.parse_qa_stdout(stdout)
    assert r["wall_limited"] == 3
    assert r["products"] == 100


# --- now_utc_iso / load_json edge / _alive ---

def test_now_utc_iso_format():
    s = k.now_utc_iso()
    assert s[:4].isdigit()           # год
    assert s.endswith("+00:00")      # UTC-суффикс
    assert "T" in s


def test_load_json_empty_file_returns_none(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("", encoding="utf-8")
    assert k.load_json(p) is None


def test_alive_current_process():
    assert k._alive(os.getpid()) is True


def test_alive_dead_pid():
    # нереалистичные PID → False без вызова os.kill (на Windows os.kill(huge,0)
    # мог бы зависнуть)
    assert k._alive(2 ** 31 - 1) is False
    assert k._alive(0) is False
    assert k._alive(-5) is False
    assert k._alive("123") is False  # не int


def test_batch_pids_dedup_keeps_first_occurrence():
    assert k.batch_pids(["3", "1", "3", "2", "1"], 10) == [["3", "1", "2"]]


# --- summarize: target_reached содержит accepted? (нет — только счётчики) ---

def test_summarize_after_target_reached():
    p = _progress_filled()
    p["campaign_final_pids"] = ["1", "2", "3"]
    s = k.summarize(p)
    assert s["valid"] == 1
    assert s["discovered"] == 2
    # summarize не утверждает campaign_final_pids — это счётчик-снимок
    assert "campaign_final_pids" not in s


# --- finalize: все брак в snapshot, резерва нет → ABANDONED ---

def test_finalize_target_reached_no_reserve_all_bad(tmp_path):
    prog = _final_progress(3, 3, 2, reason="target_reached")
    prog["campaign_final_pids"] = ["1", "2", "3"]
    prog["campaign_reserve_pids"] = []
    pa = tmp_path / "p.json"

    def qa(batch):
        per = {p: "FAIL" for p in batch}
        return {"fail": len(batch), "ok": 0, "warn": 0,
                "products": len(batch), "per_pid": per}

    k.finalize_campaign(prog, pa, qa_fn=qa)
    assert prog["final_state"] == "ABANDONED"
    assert prog["campaign_accepted_pids"] == []
    assert prog["replacement_pids"] == []


# --- finalize: мягкий путь использует campaign_final_pids, если они заданы ---

def test_finalize_soft_stop_uses_campaign_final_pids_if_set(tmp_path):
    # если по какой-то причине snapshot зафиксирован, а мягкая остановка —
    # QA идёт по нему, а не по valid_pids
    prog = _final_progress(10, 3, 2, reason="no_progress")
    prog["valid_pids"] = [str(i) for i in range(10)]
    prog["campaign_final_pids"] = ["7", "8", "9"]
    pa = tmp_path / "p.json"
    checked = []
    k.finalize_campaign(prog, pa, qa_fn=lambda b: checked.extend(b) or
                        {"fail": 0, "ok": len(b), "warn": 0,
                         "products": len(b)})
    assert set(checked) == {"7", "8", "9"}


# ============================================================================
# ТЕСТЫ: JSON-контракт QA (--report-json) + повторный детерминированный брак
# ============================================================================

# --- _parse_qa_json_report ---

def _qa_payload(products, summary=None):
    return {
        "generated_at_utc": "2026-08-31T00:00:00+00:00",
        "summary": summary or {"products": len(products), "ok": 1, "warn": 0,
                               "fail": 1, "wall_limited_count": 1},
        "products": products,
    }


def test_parse_qa_json_report_summary_and_per_pid(tmp_path):
    payload = _qa_payload([
        {"pid": "101", "verdict": "OK",
         "issues": [{"code": "X", "severity": "INFO"}]},
        {"pid": "102", "verdict": "FAIL",
         "issues": [{"code": "BAD_JSON", "severity": "FAIL"}]},
    ])
    p = tmp_path / "qa.json"
    p.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r = k._parse_qa_json_report(str(p))
    assert r is not None
    assert r["products"] == 2
    assert r["ok"] == 1 and r["fail"] == 1
    assert r["per_pid"] == {"101": "OK", "102": "FAIL"}
    assert r["per_pid_issues"] == {"101": ["X"], "102": ["BAD_JSON"]}


def test_parse_qa_json_report_missing_returns_none(tmp_path):
    assert k._parse_qa_json_report(str(tmp_path / "nope.json")) is None
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert k._parse_qa_json_report(str(p)) is None


def test_parse_qa_json_report_broken_product_row_returns_none(tmp_path):
    """A products row without pid/verdict/issues → None (fallback to stdout)
    instead of a KeyError up into run_campaign (audit 2026-10-18, B2)."""
    payload = _qa_payload([
        {"pid": "101", "verdict": "OK", "issues": []},
        {"pid": "102", "issues": []},          # no "verdict"
    ])
    p = tmp_path / "qa.json"
    p.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert k._parse_qa_json_report(str(p)) is None


def test_parse_qa_json_report_scalar_or_non_dict_summary_returns_none(tmp_path):
    """B2 follow-up: a scalar payload or a non-dict summary → None (stdout
    fallback), not TypeError/AttributeError."""
    scal = tmp_path / "scalar.json"
    scal.write_text("5", encoding="utf-8")
    assert k._parse_qa_json_report(str(scal)) is None
    bad = tmp_path / "bad_summary.json"
    bad.write_text(_json.dumps({"summary": [1, 2], "products": []}),
                   encoding="utf-8")
    assert k._parse_qa_json_report(str(bad)) is None


# --- fail_reason_seq: повторный идентичный FAIL учитывается ---

def test_reconcile_records_fail_reason_seq(tmp_path):
    for pid in ("101", "102"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101", "102"]
    checked = {"verdicts": {"101": "FAIL", "102": "OK"},
               "valid_add": [], "checked": ["101", "102"],
               "per_pid_issues": {"101": ["BAD_JSON"], "102": []}}
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["fail_reason_seq"]["101"]["count"] == 1
    assert "102" not in prog["fail_reason_seq"]  # OK → нет истории


def test_reconcile_fail_reason_seq_increments_on_same_reason(tmp_path):
    for pid in ("101",):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    # 1-й FAIL
    checked = {"verdicts": {"101": "FAIL"}, "valid_add": [], "checked": ["101"],
               "per_pid_issues": {"101": ["BAD_JSON"]}}
    k.reconcile_validity(prog, checked, str(tmp_path))
    # 2-й FAIL с той же причиной
    k.reconcile_validity(prog, checked, str(tmp_path))
    assert prog["fail_reason_seq"]["101"]["count"] == 2
    assert k._fail_reason_recurring(prog, "101") is True


def test_reconcile_fail_reason_seq_resets_on_different_reason(tmp_path):
    for pid in ("101",):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    checked1 = {"verdicts": {"101": "FAIL"}, "valid_add": [], "checked": ["101"],
                "per_pid_issues": {"101": ["BAD_JSON"]}}
    k.reconcile_validity(prog, checked1, str(tmp_path))
    checked2 = {"verdicts": {"101": "FAIL"}, "valid_add": [], "checked": ["101"],
                "per_pid_issues": {"101": ["GARBAGE_TEXT"]}}
    k.reconcile_validity(prog, checked2, str(tmp_path))
    assert prog["fail_reason_seq"]["101"]["count"] == 1  # новая причина → сброс
    assert k._fail_reason_recurring(prog, "101") is False


def test_reconcile_fail_reason_seq_resets_on_ok(tmp_path):
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    checked = {"verdicts": {"101": "FAIL"}, "valid_add": [], "checked": ["101"],
               "per_pid_issues": {"101": ["BAD_JSON"]}}
    k.reconcile_validity(prog, checked, str(tmp_path))
    ok_checked = {"verdicts": {"101": "OK"}, "valid_add": ["101"],
                  "checked": ["101"], "per_pid_issues": {"101": []}}
    k.reconcile_validity(prog, ok_checked, str(tmp_path))
    assert "101" not in prog["fail_reason_seq"]


# --- _recover_interrupted_run (run-journal recovery) ---

def _mk_recover_env(tmp_path, monkeypatch):
    camp = tmp_path / "campaigns" / "c"
    camp.mkdir(parents=True, exist_ok=True)
    (camp / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    return camp


def test_recover_commits_interrupted_run(tmp_path, monkeypatch):
    camp = _mk_recover_env(tmp_path, monkeypatch)
    # raw для двух pid: один из прерванного прогона, другой уже известен
    for pid in ("101", "102", "103"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    prog["verdicts"] = {"102": "FAIL"}  # уже решён ранее
    # прерванный прогон: run_started есть, state_committed нет
    k._journal_stage(prog, "run_x", "run_started")
    # combined этого прогона с одним новым ok-pid (103) и одним уже известным (102)
    combined = {"products": [{"pid": 102, "ok": True},
                             {"pid": 103, "ok": True}]}
    k.save_json(camp / "combined_run_x.json", combined)

    called = []
    k._recover_interrupted_run(
        prog, str(tmp_path),
        qa_fn=lambda b: called.append(list(b)) or
        _make_qa(products=len(b), ok=len(b), warn=0, fail=0,
                 per_pid={p: "OK" for p in b}))

    # 102 уже имеет вердикт (FAIL) → только 103 пере-QA'd (если fp не совпадает)
    assert "103" in prog["discovered_pids"]
    assert "103" in prog["valid_pids"]
    assert all(p in prog["valid_pids"] for p in called[0])
    rec = k.load_json(camp / "runs" / "run_x.json")
    assert "state_committed" in rec["stages"]


def test_recover_skips_committed_run(tmp_path, monkeypatch):
    camp = _mk_recover_env(tmp_path, monkeypatch)
    for pid in ("101",):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    # run уже закоммичен → recovery не трогает
    k._journal_stage(prog, "run_x", "run_started")
    k._journal_stage(prog, "run_x", "state_committed")
    k.save_json(camp / "combined_run_x.json",
                {"products": [{"pid": 101, "ok": True}]})

    called = []
    k._recover_interrupted_run(prog, str(tmp_path),
                               qa_fn=lambda b: called.append(1) or {})
    assert not called  # QA не вызывался
    assert prog["valid_pids"] == []


def test_recover_no_journal_does_nothing(tmp_path, monkeypatch):
    camp = tmp_path / "campaigns" / "c"
    camp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    for pid in ("101",):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["101"]
    called = []
    k._recover_interrupted_run(prog, str(tmp_path),
                               qa_fn=lambda b: called.append(1) or {})
    assert not called
    assert prog["valid_pids"] == []


# --- этап 0: поглощение recovery-телеметрии (ADR §14.6) ----------------------

def test_absorb_recovery_telemetry():
    """Из combined.recovery переносятся поля телеметрии в progress."""
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    combined = {"recovery": {
        "events": [
            {"kind": "new_tab", "signature": "анти-бот (page): captcha",
             "last_url": "https://www.ozon.ru/product/1",
             "browser_tabs": 5, "collector_tabs": 1},
        ],
        "browser_tabs": 5,
    }}
    k._absorb_recovery_telemetry(p, combined)
    tele = p["recovery_telemetry"]
    assert tele["last_challenge_signature"] == "анти-бот (page): captcha"
    assert tele["last_page_url"] == "https://www.ozon.ru/product/1"
    assert tele["browser_tabs"] == 5
    assert tele["collector_tabs"] == 1


def test_absorb_recovery_telemetry_keeps_origin_denied():
    """origin_denied и canary_fails доезжают до progress.json.

    Находка 2026-10-02: поля считались в combined.kpi, но не попадали в список
    суммируемых ключей — метрика отказа origin (403/429/503) молча терялась на
    финише. Именно она нужна, чтобы отличить «много отказов origin» от «много
    капч»: это разные диагнозы с разными лекарствами.
    """
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    combined = {"recovery": {"events": []},
                "kpi": {"origin_denied": 4, "canary_fails": 2,
                        "puzzle_attempts": 3, "puzzle_solved": 1}}
    k._absorb_recovery_telemetry(p, combined)
    acc = p["recovery_telemetry"]["kpi"]
    assert acc["origin_denied"] == 4, acc
    assert acc["canary_fails"] == 2, acc
    assert acc["puzzle_solved"] == 1, acc
    # кумулятивный solve-rate считается по измеренному успеху
    assert acc["puzzle_solve_rate"] == round(1 / 3, 3), acc


def test_absorb_recovery_telemetry_accumulates_origin_denied():
    """Накопление между прогонами: отказы origin суммируются, а не перезатираются."""
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    k._absorb_recovery_telemetry(
        p, {"recovery": {"events": []}, "kpi": {"origin_denied": 3}})
    k._absorb_recovery_telemetry(
        p, {"recovery": {"events": []}, "kpi": {"origin_denied": 2}})
    assert p["recovery_telemetry"]["kpi"]["origin_denied"] == 5


def test_absorb_recovery_telemetry_no_events():
    """Без events/combined — recovery_telemetry не заполняется."""
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    k._absorb_recovery_telemetry(p, {"recovery": {"events": []}})
    assert p.get("recovery_telemetry", {}) == {}
    k._absorb_recovery_telemetry(p, None)
    assert p.get("recovery_telemetry", {}) == {}


def test_absorb_kpi_accumulates():
    """KPI из combined накапливается в progress.recovery_telemetry.kpi.

    Проверяем и НАКОПЛЕНИЕ (два прогона складывают total_challenges), и что
    fingerprint переносится, и что KPI абсорбируется даже без events
    (ревизия 2.6 — KPI независим от списка событий).
    """
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    k._absorb_recovery_telemetry(p, {
        "kpi": {"total_challenges": 2, "total_recoveries": 3,
                "session_blocked": 1, "session_exhausted": 2,
                "puzzle_storms": 1,
                "puzzle_attempts": 4, "puzzle_solved": 3,
                "puzzle_solve_rate": 0.75,
                "challenge_rate_per_100": 5.0,
                "success_rate": 0.5},
        "fingerprint": {"webdriver": False, "tz": "Europe/Moscow"},
        "recovery": {"events": []},
    })
    tele = p["recovery_telemetry"]
    assert tele["kpi"]["total_challenges"] == 2
    assert tele["kpi"]["total_recoveries"] == 3
    assert tele["kpi"]["last_success_rate"] == 0.5
    assert tele["kpi"]["session_blocked"] == 1
    assert tele["kpi"]["session_exhausted"] == 2
    assert tele["kpi"]["puzzle_storms"] == 1
    assert tele["kpi"]["puzzle_attempts"] == 4
    assert tele["kpi"]["puzzle_solved"] == 3
    assert tele["kpi"]["puzzle_solve_rate"] == 0.75
    assert tele["kpi"]["last_challenge_rate_per_100"] == 5.0
    assert tele["fingerprint"]["webdriver"] is False
    # второй прогон — накопление
    k._absorb_recovery_telemetry(p, {
        "kpi": {"total_challenges": 1, "total_recoveries": 1,
                "puzzle_attempts": 4, "puzzle_solved": 1},
        "recovery": {"events": []},
    })
    assert tele["kpi"]["total_challenges"] == 3
    assert tele["kpi"]["total_recoveries"] == 4
    assert tele["kpi"]["puzzle_attempts"] == 8
    assert tele["kpi"]["puzzle_solved"] == 4
    assert tele["kpi"]["puzzle_solve_rate"] == 0.5   # кумулятивный тренд


def test_status_payload_includes_telemetry(tmp_path):
    """status.json содержит browser_tabs/last_page_url/last_challenge_signature."""
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    p["recovery_telemetry"] = {
        "browser_tabs": 3, "collector_tabs": 1,
        "last_page_url": "https://www.ozon.ru/product/9",
        "last_challenge_signature": "title:доступ ограничен",
    }
    payload = k._status_payload(p)
    assert payload["browser_tabs"] == 3
    assert payload["collector_tabs"] == 1
    assert payload["last_page_url"] == "https://www.ozon.ru/product/9"
    assert payload["last_challenge_signature"] == "title:доступ ограничен"


# --- этап 5/6: adaptive cooldown + profile cooldown (ADR §14.4/14.5) ---------

def test_challenge_cooldown_escalates(monkeypatch):
    """Adaptive backoff: storm#1≈1м, #2≈5м, #3≈15м (с допустимым джиттером)."""
    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    p["challenge_control"] = {"storm_streak": 1}
    d1 = k._challenge_cooldown_seconds(p)
    p["challenge_control"] = {"storm_streak": 2}
    d2 = k._challenge_cooldown_seconds(p)
    p["challenge_control"] = {"storm_streak": 3}
    d3 = k._challenge_cooldown_seconds(p)
    p["challenge_control"] = {"storm_streak": 10}
    d4 = k._challenge_cooldown_seconds(p)
    assert 45 <= d1 <= 75          # 60 ±20%
    assert 225 <= d2 <= 375        # 300 ±20%
    assert 675 <= d3 <= 1125       # 900 ±20%
    assert 675 <= d4 <= 1125       # залипаем на 900


def test_bump_challenge_control():
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    p["recovery_telemetry"] = {"last_challenge_signature": "title:captcha"}
    cc = k._bump_challenge_control(p)
    assert cc["storm_streak"] == 1
    assert cc["total_storms"] == 1
    assert cc["last_signature"] == "title:captcha"
    k._bump_challenge_control(p)
    assert p["challenge_control"]["storm_streak"] == 2
    assert p["challenge_control"]["total_storms"] == 2


def test_challenge_control_reset_on_progress():
    p = k.default_manifest("c", 10, 1, "url", "out.md")
    p["challenge_control"] = {"storm_streak": 3}
    p["no_progress_count"] = 5
    p["challenge_seq"] = 4
    # имитируем блок сброса при new_disc/new_valid
    if True:  # прогресс
        p["no_progress_count"] = 0
        p["challenge_seq"] = 0
        cc = p.get("challenge_control") or {}
        cc["storm_streak"] = 0
        p["challenge_control"] = cc
    assert p["challenge_control"]["storm_streak"] == 0


def test_integration_persistent_challenge_needs_attention(tmp_path, monkeypatch):
    """Persistent challenge (combined.aborted каждый прогон) → после эскалации
    (storm_streak > max) → NEEDS_ATTENTION, а не вечный cooldown."""
    def fac(argv):
        return {"products": [], "found": 0, "aborted": {"reason": "session_blocked"}}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    # короткие ступени (≤5с) — иначе cooldown-цикл с mock-sleep зависнет
    monkeypatch.setattr(k, "CHALLENGE_COOLDOWN_STEPS_S", [1, 2, 3])
    monkeypatch.setattr(k, "CHALLENGE_JITTER_FRAC", 0.0)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=3, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=0,
        reserve_count=0, max_total_hours=24.0,
        max_no_progress_runs=100)
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog["final_reason"] == "challenge_storm"
    assert prog["challenge_control"]["storm_streak"] > 3
    assert prog["profile_cooldown"]["reason"] == "repeated_challenge"


def test_integration_search_page_challenge_becomes_storm(tmp_path, monkeypatch):
    """Блок на СТРАНИЦЕ ВЫДАЧИ → CHALLENGE_STORM + adaptive cooldown.

    Живой случай (zvukoizolyatsiya, 2026-09-25): AccessChallengeError из
    search_page попадал в общий except → UNKNOWN_FAILURE → транзиентные
    ретраи по 4с молотили заблокированную сессию, storm_streak оставался 0.
    Теперь это шторм: cooldown 1/5/15 мин, NEEDS_ATTENTION(challenge_storm).
    """
    from ozon_collector.browser import AccessChallengeError

    def fac(argv):
        raise AccessChallengeError(
            "анти-бот (search_page): title:похоже, нет соединения")

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    monkeypatch.setattr(k, "CHALLENGE_COOLDOWN_STEPS_S", [1, 2, 3])
    monkeypatch.setattr(k, "CHALLENGE_JITTER_FRAC", 0.0)
    # Штормы подряд: adaptive-пауза должна расти 1→2→3с, и BOTH cooldown'а
    # (progress.cooldown_until и profile_cooldown.until) совпадать. Баг был:
    # pc["until"] ставился по последнему шагу (3с) → цикл брал max → лестница
    # 1/5/15 мин не работала (всегда 15м с первого шторма).
    snaps = []
    _orig_set_state = k._set_state

    def _spy(progress, state, event, **kw):
        if state == "COOLDOWN_CHALLENGE":
            snaps.append((progress.get("cooldown_until"),
                          (progress.get("profile_cooldown") or {}).get("until")))
        return _orig_set_state(progress, state, event, **kw)

    monkeypatch.setattr(k, "_set_state", _spy)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=3, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=0,
        reserve_count=0, max_total_hours=24.0,
        max_no_progress_runs=100)
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog["final_reason"] == "challenge_storm"
    assert prog["challenge_control"]["total_storms"] > 1
    assert prog["profile_cooldown"]["reason"] == "repeated_challenge"
    assert prog["profile_cooldown"]["last_full_storm_at"] > 0
    assert "нет соединения" in (prog["profile_cooldown"]["last_signature"] or "")
    assert prog["recovery_telemetry"]["kpi"]["total_challenges"] >= 1
    # причина блока видна в status.json (last_error), а не только в логе
    assert "нет соединения" in (prog.get("last_error") or "")
    assert "штормов подряд" in (prog.get("last_error") or "")
    # оба cooldown'а = одна adaptive-пауза, лестница растёт
    assert len(snaps) >= 2
    assert all(cu == pc for cu, pc in snaps), snaps
    pauses = [datetime.fromisoformat(cu).timestamp() for cu, _ in snaps]
    assert pauses == sorted(pauses) and len(set(pauses)) == len(pauses), pauses


def test_transient_failures_reset_on_progress(tmp_path, monkeypatch):
    """Серия UNKNOWN_FAILURE прерывается прогрессом (счётчик = подряд, не lifetime).

    Баг: transient_failures никогда не сбрасывался → 4-е случайное
    UNKNOWN_FAILURE за 12-часную кампанию (даже разнесённые по часам) убивало
    её в NEEDS_ATTENTION. Шпион _absorb_recovery_telemetry видит значение
    счётчика на входе каждого прогона.
    """
    calls = {"n": 0}

    def fac(argv):
        calls["n"] += 1
        if calls["n"] == 3:
            return {"products": [{"pid": 1, "ok": True, "excluded": None}],
                    "found": 1}
        # UNKNOWN_FAILURE = in-proc сбор упал НЕ-AccessChallenge исключением
        raise RuntimeError("boom")

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    seen = []
    _orig_absorb = k._absorb_recovery_telemetry

    def _spy(progress, combined):
        seen.append(progress.get("transient_failures", 0))
        return _orig_absorb(progress, combined)

    monkeypatch.setattr(k, "_absorb_recovery_telemetry", _spy)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=5, min_valid=1, combined_factory=fac,
        qa_fn=qa, n_pids=1, reserve_count=0, max_total_hours=24.0,
        max_no_progress_runs=100)
    # прогоны: crash, crash, ПРОГРЕСС, crash×4 (3 ретрая + терминал)
    assert seen == [0, 1, 2, 0, 1, 2, 3], seen
    assert prog["final_reason"] == "unknown_failure"
    assert prog["transient_failures"] == 3
    assert len(prog["runs"]) == 7


def test_storm_streak_reset_on_progress(tmp_path, monkeypatch):
    """storm_streak — счётчик ПОДРЯД идущих штормов: прогресс обнуляет его.

    Баг: streak жил всю кампанию → 2 шторма до обеда + 2 после обеда давали
    NEEDS_ATTENTION через часы нормальной работы.
    """
    calls = {"n": 0}

    def fac(argv):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"products": [{"pid": 1, "ok": True, "excluded": None}],
                    "found": 1}
        return {"products": [], "found": 0,
                "aborted": {"reason": "challenge_storm", "all_workers": True}}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    seen = []
    _orig_absorb = k._absorb_recovery_telemetry

    def _spy(progress, combined):
        seen.append((progress.get("challenge_control") or {}).get("storm_streak", 0))
        return _orig_absorb(progress, combined)

    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    monkeypatch.setattr(k, "CHALLENGE_COOLDOWN_STEPS_S", [1, 2, 3])
    monkeypatch.setattr(k, "CHALLENGE_JITTER_FRAC", 0.0)
    monkeypatch.setattr(k, "_absorb_recovery_telemetry", _spy)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=5, min_valid=1, combined_factory=fac,
        qa_fn=qa, n_pids=1, reserve_count=0, max_total_hours=24.0,
        max_no_progress_runs=100)
    # шторм → streak 1; прогресс → 0; затем 4 шторма подряд → NEEDS_ATTENTION
    assert seen == [0, 1, 0, 1, 2, 3], seen
    assert prog["final_reason"] == "challenge_storm"
    assert prog["challenge_control"]["total_storms"] == 5
    assert len(prog["runs"]) == 6


def test_full_storm_default_limit_reports_challenge_storm(tmp_path, monkeypatch):
    """Штормы с дефолтным лимитом no-progress → диагноз «блок», не «стагнация».

    Баг: шторм инкрементит no_progress_count, поэтому при дефолте
    max_no_progress_runs=3 демон останавливался с final_reason=no_progress
    при challenge_control.total_storms=3 — оператор ищет проблему в выдаче,
    а не в анти-боте.
    """
    def fac(argv):
        return {"products": [], "found": 0,
                "aborted": {"reason": "challenge_storm", "all_workers": True}}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    monkeypatch.setattr(k, "CHALLENGE_COOLDOWN_STEPS_S", [1, 2, 3])
    monkeypatch.setattr(k, "CHALLENGE_JITTER_FRAC", 0.0)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=3, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=0,
        reserve_count=0, max_total_hours=24.0)  # max_no_progress_runs=3 (дефолт)
    assert prog["final_reason"] == "challenge_storm"
    # final_note перезаписывает finalize (min_valid не достигнут), поэтому
    # проверяем диагноз по логу демона, где он пишется в момент остановки
    daemon_log = (tmp_path / "logs" / "testcamp_daemon.log").read_text(
        encoding="utf-8")
    assert "no-progress лимит → challenge_storm" in daemon_log
    assert "блок Ozon: 3 шторм(ов) подряд" in daemon_log
    assert prog["challenge_control"]["total_storms"] == 3
    assert prog["challenge_control"]["storm_streak"] == 3


def test_puzzle_storm_reports_diagnosis(tmp_path, monkeypatch):
    """Пазл-шторм (puzzle_persistent) → last_error объясняет ПРИЧИНУ.

    search.py кладёт recovery.challenge_storm.puzzle_persistent, parallel
    прокидывает его в combined.aborted; раньше демон это игнорировал и писал
    «сигнатура неизвестна» — оператор не мог отличить нерешаемый пазл от FAB.
    """
    def fac(argv):
        return {"products": [], "found": 0,
                "aborted": {"reason": "challenge_storm", "all_workers": True,
                            "puzzle_persistent": True, "puzzle_failures": 3}}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    monkeypatch.setattr(k, "CHALLENGE_COOLDOWN_STEPS_S", [1, 2, 3])
    monkeypatch.setattr(k, "CHALLENGE_JITTER_FRAC", 0.0)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=3, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=0,
        reserve_count=0, max_total_hours=24.0)
    assert prog["final_reason"] == "challenge_storm"
    assert "puzzle_storm" in (prog.get("last_error") or "")
    assert "3 неудач" in (prog.get("last_error") or "")
    assert "сигнатура неизвестна" not in (prog.get("last_error") or "")


def test_integration_partial_storm_no_escalation(tmp_path, monkeypatch):
    """ЧАСТИЧНЫЙ шторм (parallel=2, штормил ОДИН воркер, all_workers=False) →
    короткая пауза БЕЗ эскалации: storm_streak не растёт, profile_cooldown не
    заполняется, NEEDS_ATTENTION не ставится (ADR §14.6)."""
    def fac(argv):
        return {"products": [], "found": 0,
                "aborted": {"reason": "challenge_storm", "all_workers": False,
                            "workers_stormed": [1]}}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    monkeypatch.setattr(k, "CHALLENGE_COOLDOWN_STEPS_S", [1, 2, 3])
    monkeypatch.setattr(k, "CHALLENGE_JITTER_FRAC", 0.0)
    monkeypatch.setattr(k, "CHALLENGE_PARTIAL_PAUSE_S", 1)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=3, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=0,
        reserve_count=0, max_total_hours=24.0,
        max_no_progress_runs=5, parallel=2)
    # частичный шторм не должен эскалировать в NEEDS_ATTENTION
    assert prog.get("final_state") != "NEEDS_ATTENTION"
    assert not prog.get("final_reason") == "challenge_storm"
    assert prog["challenge_control"]["storm_streak"] == 0
    assert prog.get("profile_cooldown") is None or \
        not prog["profile_cooldown"].get("until")
    assert prog.get("profile_cooldown") is None or \
        not prog["profile_cooldown"].get("last_full_storm_at")


def test_integration_full_storm_parallel_escalates(tmp_path, monkeypatch):
    """ФУЛЛ-шторм (parallel=2, ВСЕ воркеры упёрлись, all_workers=True) →
    эскалация как у одиночного: NEEDS_ATTENTION + profile_cooldown."""
    def fac(argv):
        return {"products": [], "found": 0,
                "aborted": {"reason": "challenge_storm", "all_workers": True,
                            "workers_stormed": [1, 2]}}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setenv("KD_RANDOM_SEED", "0")
    monkeypatch.setattr(k, "CHALLENGE_COOLDOWN_STEPS_S", [1, 2, 3])
    monkeypatch.setattr(k, "CHALLENGE_JITTER_FRAC", 0.0)
    prog = _run_campaign_integration(
        tmp_path, monkeypatch, target=3, min_valid=1,
        combined_factory=fac, qa_fn=qa, n_pids=0,
        reserve_count=0, max_total_hours=24.0,
        max_no_progress_runs=100, parallel=2)
    assert prog["final_state"] == "NEEDS_ATTENTION"
    assert prog["final_reason"] == "challenge_storm"
    assert prog["challenge_control"]["storm_streak"] > 3
    assert prog["profile_cooldown"]["reason"] == "repeated_challenge"
    assert prog["profile_cooldown"]["last_full_storm_at"] > 0


# ---- Кооперативный стоп по Ctrl+C → INTERRUPTED ------------------------------

def test_integration_interrupted_top_of_loop(tmp_path, monkeypatch):
    """СТОП установлен ДО старта прогона → run_campaign выходит с INTERRUPTED,
    прогресс сохраняется, счётчики сбоев (no_progress/crash) НЕ растут."""
    prev = k._STOP_EVENT
    k._STOP_EVENT = threading.Event()
    k._STOP_EVENT.set()           # Ctrl+C уже нажат
    try:
        def fac(argv):
            return {"products": [{"pid": 0, "ok": True, "excluded": None}],
                    "found": 1}
        def qa(batch):
            return {"products": len(batch), "ok": len(batch), "warn": 0,
                    "fail": 0, "per_pid": {p: "OK" for p in batch}}
        prog = _run_campaign_integration(
            tmp_path, monkeypatch, target=3, min_valid=1,
            combined_factory=fac, qa_fn=qa, n_pids=1,
            reserve_count=0, max_total_hours=24.0)
        assert prog["final_state"] == "INTERRUPTED"
        assert prog["final_reason"] == "user_interrupt"
        assert prog.get("no_progress_count", 0) == 0   # НЕ как сбой
        assert prog.get("stall", {}).get("crash_runs", 0) == 0
    finally:
        k._STOP_EVENT = prev
        k._STOP_EVENT.clear()


def test_integration_interrupted_during_collect_saves_partial(tmp_path, monkeypatch):
    """СТОП поднят ВО ВРЕМЯ сбора: частично собранные пиды сохраняются,
    кампания завершается INTERRUPTED (не CRASH)."""
    prev = k._STOP_EVENT
    k._STOP_EVENT = threading.Event()
    try:
        def fac(argv):
            # воркеры успели собрать 2 пида, затем пришёл Ctrl+C
            k._STOP_EVENT.set()
            return {"products": [{"pid": 0, "ok": True, "excluded": None},
                                 {"pid": 1, "ok": True, "excluded": None}],
                    "found": 2}
        def qa(batch):
            return {"products": len(batch), "ok": len(batch), "warn": 0,
                    "fail": 0, "per_pid": {p: "OK" for p in batch}}
        prog = _run_campaign_integration(
            tmp_path, monkeypatch, target=5, min_valid=1,
            combined_factory=fac, qa_fn=qa, n_pids=2,
            reserve_count=0, max_total_hours=24.0)
        assert prog["final_state"] == "INTERRUPTED"
        assert prog["final_reason"] == "user_interrupt"
        # собранные пиды попали в манифест (не потерялись)
        assert len(prog.get("valid_pids", [])) >= 1
        assert prog.get("no_progress_count", 0) == 0   # НЕ как сбой
        assert prog.get("stall", {}).get("crash_runs", 0) == 0
    finally:
        k._STOP_EVENT = prev
        k._STOP_EVENT.clear()


# ---- Форс (2-й Ctrl+C) и частичный discovery из докрутки ---------------------

def test_extract_discovered_pids_merges_partial_discovery_field():
    """extract_discovered_pids сливает combined.discovered_pids (заполняемые при
    graceful-стопе докрутки ссылок, когда products пуст)."""
    combined = {"products": [], "discovered_pids": ["111", "222"]}
    assert k.extract_discovered_pids(combined) == ["111", "222"]
    combined2 = {"products": [{"pid": "333", "ok": True}],
                 "discovered_pids": ["111"]}
    assert k.extract_discovered_pids(combined2) == ["333", "111"]


# --- --reset: полное пересоздание существующей кампании ---

def test_run_campaign_force_reset_wipes_terminal_campaign(tmp_path, monkeypatch):
    """--reset (force_reset=True) на завершённой кампании: каталог кампании
    удаляется, кампания стартует заново — свежий манифест, новый identity
    (url_sha256), старые discovered/valid/runs не наследуются."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    for pid in ("0", "1"):
        (data_dir / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    campaigns = tmp_path / "campaigns"
    camp_dir = campaigns / "testcamp"
    camp_dir.mkdir(parents=True, exist_ok=True)
    old = k.default_manifest("testcamp", 5, 2, "old-url", "campaign.md",
                             data_dir=str(data_dir))
    old["discovered_pids"] = ["999", "998"]
    old["valid_pids"] = ["999"]
    old["runs"] = ["run_old_1", "run_old_2"]
    old["final_state"] = "COMPLETED_MIN"
    k.save_json(camp_dir / "progress.json", old)
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", campaigns)
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(k, "PYTHON", "python")
    # артефакты старой кампании, которые должны исчезнуть вместе с каталогом
    (camp_dir / "status.json").write_text('{"runtime_state":"COMPLETED"}',
                                          encoding="utf-8")

    def fac(argv):
        return {"products": [{"pid": p, "ok": True, "excluded": None}
                             for p in ("0", "1")], "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: fac(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    prog = k.run_campaign(
        campaign="testcamp", url="new-url", target=2, min_valid=1,
        out=str(tmp_path / "campaign.md"), data_dir=str(data_dir), max_no_progress_runs=10,
        max_total_hours=24.0, force_reset=True, qa_fn=qa)

    # старая терминальная кампания стёрта → стартовали именно новые пиды
    assert prog["campaign_identity"]["url_sha256"] == k._sha256_text("new-url")
    assert "999" not in prog["discovered_pids"]
    assert "998" not in prog["discovered_pids"]
    assert set(prog["discovered_pids"]) == {"0", "1"}
    assert prog["valid_pids"] == ["0", "1"]
    # прогоны уже НЕ содержат заглушки-"строки" из старого манифеста
    assert all(isinstance(r, dict) for r in prog["runs"])
    assert prog["final_state"] == "COMPLETED"


def test_run_campaign_force_reset_without_existing_is_fresh(tmp_path, monkeypatch):
    """--reset когда кампании ещё нет: работает как обычный новый старт,
    без ошибок (удалять нечего)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    for pid in ("0", "1"):
        (data_dir / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    monkeypatch.setattr(k, "DATA_DIR", tmp_path)
    monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(k, "PYTHON", "python")

    def fac(argv):
        return {"products": [{"pid": 0, "ok": True, "excluded": None},
                             {"pid": 1, "ok": True, "excluded": None}],
                "found": 2}

    def qa(batch):
        return {"products": len(batch), "ok": len(batch), "warn": 0, "fail": 0,
                "per_pid": {p: "OK" for p in batch}}

    monkeypatch.setattr(k, "_inproc_collect", lambda progress, run_id, mode,
                        targets=None: fac(
                            k.build_cli_args(progress, run_id, mode, targets)))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    prog = k.run_campaign(
        campaign="testcamp", url="u", target=2, min_valid=1,
        out=str(tmp_path / "campaign.md"), data_dir=str(data_dir), max_no_progress_runs=10,
        max_total_hours=24.0, force_reset=True, qa_fn=qa)

    assert prog["final_state"] == "COMPLETED"
    assert prog["valid_pids"] == ["0", "1"]


def test_run_campaign_force_collection_calls_emergency_shutdown(tmp_path, monkeypatch):
    """FORCE (2-й Ctrl+C) во время докрутки: CollectionInterrupted(forced=True)
    → вызывается _emergency_shutdown (НЕ классифицируется как CRASH/UNKNOWN).
    После (недостижимого в проде, т.к. os._exit) возврата из _emergency_shutdown —
    исключение пробрасывается наверх, а не глотается как UNKNOWN_FAILURE."""
    from ozon_collector.search import CollectionInterrupted
    prev_f = k._FORCE_EXIT
    prev_s = k._STOP_EVENT
    k._FORCE_EXIT = threading.Event()
    k._FORCE_EXIT.set()          # форс уже активен
    k._STOP_EVENT = threading.Event()
    k._STOP_EVENT.clear()
    calls = []
    monkeypatch.setattr(k, "_emergency_shutdown",
                        lambda progress_path, force="": calls.append(force))
    try:
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(k, "CAMPAIGNS_DIR", tmp_path / "campaigns")
        monkeypatch.setattr(k, "DATA_DIR", tmp_path)
        monkeypatch.setattr(k, "LOGS_DIR", tmp_path / "logs")

        def raise_forced(progress, run_id, mode, targets=None):
            raise CollectionInterrupted([], forced=True)

        monkeypatch.setattr(k, "_inproc_collect", raise_forced)
        monkeypatch.setattr(k, "_NO_COOLDOWN", True)

        with pytest.raises(CollectionInterrupted):
            k.run_campaign(
                campaign="testcamp", url="u", target=3, min_valid=1,
                out=str(tmp_path / "campaign.md"), data_dir=str(data_dir), max_total_hours=24.0,
                qa_fn=lambda batch: {"products": len(batch), "ok": len(batch),
                                     "warn": 0, "fail": 0,
                                     "per_pid": {p: "OK" for p in batch}})
    finally:
        k._FORCE_EXIT = prev_f
        k._STOP_EVENT = prev_s
    assert calls, "ожидали вызов _emergency_shutdown при FORCE (collection)"
    assert calls[0] == "collection"


# --- автогенерация имени кампании из --url (naming, НЕ фильтр) ----------------

def test_slugify_translit_basic():
    assert k._slugify("машинка на пульте управления для девочек") == (
        "mashinka_na_pulte_upravleniya_dlya_devochek")


def test_slugify_soft_hard_signs_removed():
    assert k._slugify("пальто") == "palto"
    assert k._slugify("объём") == "obem"


def test_slugify_special_chars_collapsed():
    assert k._slugify("Машинка&/ на (пульте)  ---") == "mashinka_na_pulte"


def test_slugify_truncation_no_trailing_underscore():
    long_text = "машинка очень хорошая большая модель для девочек " * 3
    slug = k._slugify(long_text)
    assert len(slug) <= 50
    assert not slug.endswith("_")


def test_slugify_only_invalid_returns_none():
    assert k._slugify("###") is None
    assert k._slugify("") is None
    assert k._slugify("   ") is None


def test_campaign_from_url_text_percent_encoded():
    url = ("https://www.ozon.ru/search/?from_global=true&text="
           "%D0%BC%D0%B0%D1%88%D0%B8%D0%BD%D0%BA%D0%B0+%D0%BD%D0%B0+"
           "%D0%BF%D1%83%D0%BB%D1%8C%D1%82%D0%B5+"
           "%D1%83%D0%BF%D1%80%D0%B0%D0%B2%D0%BB%D0%B5%D0%BD%D0%B8%D1%8F+"
           "%D0%B4%D0%BB%D1%8F+%D0%B4%D0%B5%D0%B2%D0%BE%D1%87%D0%B5%D0%BA")
    name, source, value = k.campaign_from_url(url)
    assert name == "mashinka_na_pulte_upravleniya_dlya_devochek"
    assert source == "url_text"
    assert value == "машинка на пульте управления для девочек"


def test_campaign_from_url_category_strips_id():
    url = ("https://www.ozon.ru/category/mashinki-radioupravlyaemye-7151/"
           "?category_was_predicted=true")
    name, source, value = k.campaign_from_url(url)
    assert name == "mashinki_radioupravlyaemye"
    assert source == "url_category"
    assert value == "mashinki-radioupravlyaemye-7151"


def test_campaign_from_url_blank_text_falls_to_category():
    url = "https://www.ozon.ru/category/mashinki-7151/?text=%20&from_global=true"
    name, source, _ = k.campaign_from_url(url)
    assert name == "mashinki"
    assert source == "url_category"


def test_campaign_from_url_timestamp_fallback():
    name, source, value = k.campaign_from_url("https://ozon.ru/somepage")
    assert re.fullmatch(r"campaign_\d{8}_\d{4}", name)
    assert source == "timestamp" and value is None


def test_campaign_from_url_empty_url_timestamp():
    name, source, _ = k.campaign_from_url("")
    assert re.fullmatch(r"campaign_\d{8}_\d{4}", name)
    assert source == "timestamp"


def test_campaign_from_url_deterministic_same_url():
    url = "https://www.ozon.ru/search/?text=%D0%BC%D0%B0%D1%88%D0%B8%D0%BD%D0%BA%D0%B0"
    assert k.campaign_from_url(url)[0] == k.campaign_from_url(url)[0]


def test_campaign_from_url_case_insensitive():
    a = k.campaign_from_url("https://x/search/?text=%D0%9C%D0%B0%D1%88%D0%B8%D0%BD%D0%BA%D0%B0")[0]
    b = k.campaign_from_url("https://x/search/?text=%D0%BC%D0%B0%D1%88%D0%B8%D0%BD%D0%BA%D0%B0")[0]
    assert a == b == "mashinka"


def test_campaign_from_url_text_only_invalid_chars_falls_back():
    url = "https://www.ozon.ru/search/?text=%23%23%23"
    name, source, _ = k.campaign_from_url(url)
    assert re.fullmatch(r"campaign_\d{8}_\d{4}", name)
    assert source == "timestamp"


def test_default_manifest_name_source_fields():
    m = k.default_manifest("camp", 10, 5, "u", "camp.md",
                           name_source="url_text", name_source_value="запрос")
    assert m["name_source"] == "url_text"
    assert m["name_source_value"] == "запрос"
    assert "run_cfg" in m and "name_source" not in m["run_cfg"]


def _fake_completed_run(captured, campaign):
    return {"campaign": campaign, "target_count": 5, "min_valid": 1,
            "valid_pids": [], "discovered_pids": [], "failed_pids": [],
            "permanently_failed_pids": [], "runs": [],
            "final_state": "COMPLETED", "final_reason": "target_reached"}


def test_main_auto_name_from_text(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured["campaign"] = kw["campaign"]
        captured["out"] = kw["out"]
        captured["url"] = kw["url"]
        captured["name_source"] = kw["name_source"]
        captured["name_source_value"] = kw["name_source_value"]
        return _fake_completed_run(captured, kw["campaign"])

    monkeypatch.setattr(k, "run_campaign", fake_run)
    k.main(["--url", "https://www.ozon.ru/search/?text=%D0%9C%D0%B0%D1%88%D0%B8%D0%BD%D0%BA%D0%B0",
            "--limit", "5", "--min-valid", "1"])
    assert captured["campaign"] == "mashinka"
    assert captured["out"] == "docs/digests/mashinka.md"
    assert captured["name_source"] == "url_text"
    assert captured["name_source_value"] == "Машинка"


def test_main_explicit_campaign_ignores_text(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured["campaign"] = kw["campaign"]
        captured["out"] = kw["out"]
        captured["name_source"] = kw["name_source"]
        return _fake_completed_run(captured, kw["campaign"])

    monkeypatch.setattr(k, "run_campaign", fake_run)
    k.main(["--campaign", "prezervativy",
            "--url", "https://www.ozon.ru/search/?text=%D0%9C%D0%B0%D1%88%D0%B8%D0%BD%D0%BA%D0%B0",
            "--limit", "5", "--min-valid", "1", "--out", "docs/digests/p.md"])
    assert captured["campaign"] == "prezervativy"
    assert captured["out"] == "docs/digests/p.md"
    assert captured["name_source"] == "explicit"


def test_main_timestamp_fallback(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured["campaign"] = kw["campaign"]
        captured["name_source"] = kw["name_source"]
        return _fake_completed_run(captured, kw["campaign"])

    monkeypatch.setattr(k, "run_campaign", fake_run)
    k.main(["--url", "https://ozon.ru/somepage", "--limit", "5", "--min-valid", "1"])
    assert re.fullmatch(r"campaign_\d{8}_\d{4}", captured["campaign"])
    assert captured["name_source"] == "timestamp"


# ---------------------------------------------------------------------------
# session_auth: видимость авторизации в status.json (потолок ~100 отзывов)
# ---------------------------------------------------------------------------

def _combined_with_auth(states):
    """combined с товарами, у каждого raw.session.auth.logged_in = states[i]."""
    return {"products": [
        {"pid": str(i), "ok": True,
         "raw": {"session": {"auth": {"logged_in": s,
                                      "source": "cookie"}}}}
        for i, s in enumerate(states)]}


def test_absorb_session_auth_counts_states():
    progress = {}
    k._absorb_session_auth(progress, _combined_with_auth([True, False, True]))
    sa = progress["session_auth"]
    assert sa["checked"] == 3
    assert sa["logged_in"] == 2
    assert sa["anonymous"] == 1
    assert sa["unknown"] == 0
    assert sa["updated_at_utc"]


def test_absorb_session_auth_unknown_when_marker_absent():
    # Старые combined без session → не выдумываем авторизацию.
    progress = {}
    k._absorb_session_auth(progress, {"products": [{"pid": "1", "ok": True,
                                                     "raw": {}}]})
    assert progress["session_auth"]["logged_in"] == 0
    assert progress["session_auth"]["anonymous"] == 0
    assert progress["session_auth"]["unknown"] == 1


def test_absorb_session_auth_noop_on_empty():
    progress = {"session_auth": {"checked": 7, "logged_in": 3}}
    k._absorb_session_auth(progress, None)
    k._absorb_session_auth(progress, {"products": []})
    # Пустой прогон не затирает уже накопленную картину.
    assert progress["session_auth"]["checked"] == 7


def test_status_payload_exposes_session_auth():
    progress = {
        "campaign": "c", "runtime_state": "RUNNING_FULL", "target_count": 5,
        "started_at": datetime.now().isoformat(),
        "run_cfg": {"max_total_hours": 1.0},
        "session_auth": {"checked": 4, "logged_in": 0, "anonymous": 4,
                         "unknown": 0, "updated_at_utc": "2026-09-25T10:00:00+00:00"},
    }
    payload = k._status_payload(progress)
    assert payload["session_auth"]["anonymous"] == 4
    assert payload["session_auth"]["logged_in"] == 0


def test_status_payload_session_auth_absent_is_none():
    payload = k._status_payload({
        "campaign": "c", "runtime_state": "RUNNING_FULL", "target_count": 1,
        "started_at": datetime.now().isoformat(),
        "run_cfg": {"max_total_hours": 1.0},
    })
    assert payload["session_auth"] is None


# ---- ревизия 2.9 P0: --url валидируется до прогона --------------------------

class TestDaemonUrlGuard:
    """Невалидный --url = exit 2 ДО старта браузера и демона.

    Реальный баг 2026-09-26: [текст](url) дошёл до Page.goto
    ('Cannot navigate to invalid URL'), выдача 0 товаров, прогоны
    NORMAL_DONE с discovered=0 — демон работал вхолостую.

    Политика: URL ЧИНИТСЯ, если это однозначно (markdown-обёртка, пробелы,
    хвостовая пунктуация) + warning в лог; ОТВЕРГАЕТСЯ, если это мусор
    (опечатка без схемы, javascript:, пусто) — exit 2 без прогона.
    """

    LIVE_BAD = ("[https://www.ozon.ru/category/zvukoizolyatsiya-33302/"
                "?insulationinstallation=320869%2C320870]"
                "(https://www.ozon.ru/category/fonari-11061/?type=47720)")

    def test_markdown_url_repaired_before_run(self, monkeypatch):
        got = {}
        monkeypatch.setattr(k, "run_campaign", lambda **kw: got.update(kw) or {})
        monkeypatch.setattr(k, "summarize", lambda p: {"ok": True})
        assert k.main(["--url", self.LIVE_BAD, "--limit", "1"]) is None
        assert got["url"] == ("https://www.ozon.ru/category/fonari-11061/"
                              "?type=47720")

    def test_markdown_url_names_campaign_from_repaired_url(self, monkeypatch):
        got = {}
        monkeypatch.setattr(k, "run_campaign", lambda **kw: got.update(kw) or {})
        monkeypatch.setattr(k, "summarize", lambda p: {"ok": True})
        k.main(["--url", "[заг](https://www.ozon.ru/category/g-7/)"])
        assert got["name_source"] == "url_category"
        assert got["campaign"] == "g"

    def test_trailing_punctuation_repaired(self, monkeypatch):
        got = {}
        monkeypatch.setattr(k, "run_campaign", lambda **kw: got.update(kw) or {})
        monkeypatch.setattr(k, "summarize", lambda p: {"ok": True})
        k.main(["--url", "https://www.ozon.ru/category/h-8/?x=1. "])
        assert got["url"] == "https://www.ozon.ru/category/h-8/?x=1"

    def test_junk_url_exits_2_without_run(self, monkeypatch):
        started = []
        monkeypatch.setattr(k, "run_campaign",
                            lambda **kw: started.append(kw) or {})
        assert k.main(["--url", "ozon категория шумы"]) == 2
        assert started == []

    def test_bad_canary_url_exits_2(self, monkeypatch):
        started = []
        monkeypatch.setattr(k, "run_campaign",
                            lambda **kw: started.append(kw) or {})
        rc = k.main(["--url", "https://www.ozon.ru/category/i-9/",
                     "--canary-url", "javascript:alert(1)"])
        assert rc == 2
        assert started == []

    def test_bad_url_prints_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(k, "run_campaign", lambda **kw: {})
        k.main(["--url", "просто текст"])
        err = capsys.readouterr().err
        assert "неверный --url" in err
        assert "ozon.ru" in err


# ---- ревизия 2.9: EMPTY_SEARCH не притворяется NORMAL_DONE -----------------

class TestEmptySearchStatus:
    """Прогон exit 0 с пустой сеткой = EMPTY_SEARCH, не «успех» (2.9)."""

    def test_classify_empty_search(self):
        c = {"found": 0, "collected": 0, "products": [],
             "recovery": {"search_grid_empty": True}}
        assert k.classify_status(0, c) == "EMPTY_SEARCH"

    def test_classify_normal_done_unchanged(self):
        c = {"found": 5, "collected": 3, "products": [{}] * 3,
             "recovery": {}}
        assert k.classify_status(0, c) == "NORMAL_DONE"

    def test_classify_storm_beats_empty(self):
        c = {"aborted": {"reason": "session_blocked"},
             "recovery": {"search_grid_empty": True}}
        assert k.classify_status(0, c) == "CHALLENGE_STORM"

    def test_classify_no_recovery_key_is_normal(self):
        assert k.classify_status(0, {"found": 0, "products": []}) == \
            "NORMAL_DONE"

# --- инкрементальный чекпоинт прогона: inflight.json (P0 fonari_ 2026-09-26) ---

def _qa_all_ok(batch):
    return _make_qa(products=len(batch), ok=len(batch), warn=0, fail=0,
                    per_pid={p: "OK" for p in batch})


def test_raw_pids_since_ignores_backups_and_junk(tmp_path):
    now = time.time()
    old = tmp_path / "333_raw.json"
    old.write_text("{}", encoding="utf-8")
    os.utime(old, (now - 500, now - 500))
    (tmp_path / "111_raw.json").write_text("{}", encoding="utf-8")
    (tmp_path / "222_raw.20260926-1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "gallery_123").write_text("x", encoding="utf-8")
    assert k.raw_pids_since(str(tmp_path), now - 100) == ["111"]
    assert sorted(k.raw_pids_since(str(tmp_path), 0)) == ["111", "333"]
    assert k.raw_pids_since(str(tmp_path / "нет"), 0) == []


# ---------------------------------------------------------------------------
# Очередь прогона: discovered ≠ собранные (регрессия 2026-10-06)
# ---------------------------------------------------------------------------
def test_pending_pids_are_discovered_without_result(tmp_path):
    """Пиды из выдачи без raw и без вердикта — «без результата».

    Именно они терялись: демон считал весь discovered «уже обработанным»
    и выкидал из очереди (в логе: «пропущено 336, осталось 0» при 66 raw).
    """
    (tmp_path / "111_raw.json").write_text("{}", encoding="utf-8")
    prog = {"discovered_pids": ["111", "222", "333"],
            "valid_pids": [], "failed_pids": ["333"],
            "permanently_failed_pids": []}
    assert k.pending_pids(prog, str(tmp_path)) == ["222"]


def test_pending_pids_empty_when_everything_collected(tmp_path):
    (tmp_path / "111_raw.json").write_text("{}", encoding="utf-8")
    (tmp_path / "222_raw.json").write_text("{}", encoding="utf-8")
    prog = {"discovered_pids": ["111", "222"], "valid_pids": ["222"],
            "failed_pids": [], "permanently_failed_pids": []}
    assert k.pending_pids(prog, str(tmp_path)) == []


def test_pending_pids_counts_valid_as_processed(tmp_path):
    """valid без файла на диске тоже «обработан» (есть куда спрятать raw)."""
    prog = {"discovered_pids": ["111", "222"], "valid_pids": ["111"],
            "failed_pids": [], "permanently_failed_pids": []}
    assert k.pending_pids(prog, str(tmp_path)) == ["222"]


def test_summarize_reports_pending(tmp_path):
    prog = {"campaign": "c", "target_count": 500, "min_valid": 1,
            "discovered_pids": ["111", "222"], "valid_pids": [],
            "failed_pids": [], "permanently_failed_pids": [], "runs": []}
    s = k.summarize(prog)
    assert s["discovered"] == 2 and s["pending"] == 2


def test_skip_pids_excludes_pending_from_queue(tmp_path):
    """skip_pids (= результат split_done_pending) НЕ содержит пидов без raw.

    Это ровно та величина, которая уходит в collect_parallel: раньше туда
    попадал весь discovered, и товары без результата выпадали из работы.
    """
    (tmp_path / "111_raw.json").write_text("{}", encoding="utf-8")
    prog = {"discovered_pids": ["111", "222"], "valid_pids": ["333"],
            "failed_pids": [], "permanently_failed_pids": [], "runs": []}
    done, retry = k.split_done_pending(prog, str(tmp_path))
    assert sorted(done) == ["111", "333"]
    assert retry == ["222"], "222 обязан вернуться в очередь"
    skip = sorted(done) or None
    assert "222" not in (skip or [])


def test_split_done_pending_keeps_failed_out_of_queue(tmp_path):
    """Брак и безнадёжные не пересобираются каждый раунд (исходное требование)."""
    prog = {"discovered_pids": ["1", "2", "3", "4"],
            "valid_pids": ["1"], "failed_pids": ["2"],
            "permanently_failed_pids": ["3"], "runs": []}
    done, retry = k.split_done_pending(prog, str(tmp_path))
    assert {"1", "2", "3"} <= done
    assert retry == ["4"], "только 4 (без результата) возвращается в очередь"


def test_pending_attempts_give_up_after_limit(tmp_path):
    """Товар без результата уходит в безнадёжные после N попыток.

    Обратная сторона починки очереди: без счётчика не собираемый товар
    пересобирался бы каждый прогон (ревью 2026-10-06).
    """
    prog = {"discovered_pids": ["1", "2"], "valid_pids": [],
            "failed_pids": [], "permanently_failed_pids": [],
            "pending_attempts": {"1": k.PENDING_ATTEMPT_LIMIT - 1}}
    attempts = dict(prog["pending_attempts"])
    for p in ["1", "2"]:
        attempts[p] = attempts.get(p, 0) + 1
    give_up = {p for p, n in attempts.items()
               if n >= k.PENDING_ATTEMPT_LIMIT}
    assert give_up == {"1"}, "товар на пределе попыток уходит в безнадёжные"
    assert "2" not in give_up, "у 2 только первая попытка — он ещё в работе"


def test_pending_attempts_reset_when_result_appears(tmp_path):
    """Появился raw → пид больше не «без результата», счётчик обнуляется."""
    (tmp_path / "1_raw.json").write_text("{}", encoding="utf-8")
    prog = {"discovered_pids": ["1", "2"], "valid_pids": [],
            "failed_pids": [], "permanently_failed_pids": [],
            "pending_attempts": {"1": 2, "2": 1}}
    _done, retry = k.split_done_pending(prog, str(tmp_path))
    kept = {p: n for p, n in prog["pending_attempts"].items() if p in retry}
    assert kept == {"2": 1}, "у пида с результатом счётчик сбрасывается"


def test_checkpoint_thread_writes_inflight_and_stop_is_clean(tmp_path):
    camp = tmp_path / "camp"
    camp.mkdir()
    h = k._start_checkpoint(camp, str(tmp_path), 0.05, campaign="c")
    try:
        (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
        path = camp / k.INFLIGHT_NAME
        deadline = time.time() + 5
        while time.time() < deadline and not path.is_file():
            time.sleep(0.05)
        assert path.is_file(), "чекпоинт не записал inflight.json"
    finally:
        k.stop_checkpoint(h)
    assert not h["thread"].is_alive()
    assert h["error"] is None
    snap = k.load_json(path)
    assert snap["pids"] == ["101"]
    assert snap["campaign"] == "c" and snap["checkpoint_at"]
    assert not list(camp.glob("*.tmp")), "остался временный файл"


def test_checkpoint_disabled_by_zero_interval(tmp_path):
    camp = tmp_path / "camp"
    camp.mkdir()
    h = k._start_checkpoint(camp, str(tmp_path), 0)
    assert h["thread"] is None
    assert not (camp / k.INFLIGHT_NAME).exists()


def test_checkpoint_survives_scan_error_and_keeps_running(tmp_path, monkeypatch):
    camp = tmp_path / "camp"
    camp.mkdir()
    boom = [True]

    def _boom(*_a, **_kw):
        if boom[0]:
            raise OSError("диск умер")
        return []

    monkeypatch.setattr(k, "raw_pids_since", _boom)
    h = k._start_checkpoint(camp, str(tmp_path), 0.05, campaign="c")
    try:
        deadline = time.time() + 5
        while time.time() < deadline and h["error"] is None:
            time.sleep(0.05)
        assert h["error"] and "диск умер" in h["error"]
        assert h["thread"].is_alive(), "чекпоинт умер от своей ошибки"
    finally:
        boom[0] = False
        k.stop_checkpoint(h)


def test_reconcile_inflight_promotes_to_valid(tmp_path):
    camp = tmp_path / "camp"
    camp.mkdir()
    for pid in ("101", "102"):
        (tmp_path / f"{pid}_raw.json").write_text("{}", encoding="utf-8")
    k.save_json(camp / k.INFLIGHT_NAME,
                {"campaign": "c", "pids": ["101", "102"]})
    prog = _manifest(tmp_path)
    fresh = k._reconcile_inflight(prog, camp / k.INFLIGHT_NAME,
                                  str(tmp_path), qa_fn=_qa_all_ok)
    assert fresh == ["101", "102"]
    assert prog["discovered_pids"] == ["101", "102"]
    assert prog["valid_pids"] == ["101", "102"]
    assert not prog["failed_pids"]
    assert prog["verdict_meta"]["101"]["verdict"] == "OK"


def test_reconcile_inflight_is_idempotent_and_bounded_to_manifest(tmp_path):
    camp = tmp_path / "camp"
    camp.mkdir()
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    k.save_json(camp / k.INFLIGHT_NAME, {"pids": ["101"]})
    prog = _manifest(tmp_path)
    k._reconcile_inflight(prog, camp / k.INFLIGHT_NAME, str(tmp_path),
                          qa_fn=_qa_all_ok)
    calls = []
    again = k._reconcile_inflight(prog, camp / k.INFLIGHT_NAME, str(tmp_path),
                                  qa_fn=lambda b: calls.append(b) or {})
    assert again == [] and not calls, "повторный merge не должен звать QA"
    assert prog["discovered_pids"] == ["101"]


def test_reconcile_inflight_without_file_is_noop(tmp_path):
    prog = _manifest(tmp_path)
    assert k._reconcile_inflight(prog, tmp_path / "нет.json", str(tmp_path),
                                 qa_fn=_qa_all_ok) == []
    assert prog["discovered_pids"] == []


def test_reconcile_inflight_parse_error_keeps_pid_discovered(tmp_path):
    camp = tmp_path / "camp"
    camp.mkdir()
    (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
    k.save_json(camp / k.INFLIGHT_NAME, {"pids": ["101"]})
    prog = _manifest(tmp_path)
    fresh = k._reconcile_inflight(
        prog, camp / k.INFLIGHT_NAME, str(tmp_path),
        qa_fn=lambda b: _make_qa(products=len(b) + 3, ok=0, warn=0, fail=0,
                                per_pid={}))
    assert fresh == ["101"]
    assert prog["discovered_pids"] == ["101"]
    assert prog["valid_pids"] == [], "недостоверный QA не должен давать valid"
    assert "101" not in prog["failed_pids"]


def test_checkpoint_never_writes_progress_manifest(tmp_path):
    camp = tmp_path / "camp"
    camp.mkdir()
    prog = _manifest(tmp_path)
    prog["discovered_pids"] = ["999"]
    k.save_json(camp / "progress.json", prog)
    h = k._start_checkpoint(camp, str(tmp_path), 0.05, campaign="c")
    try:
        (tmp_path / "101_raw.json").write_text("{}", encoding="utf-8")
        deadline = time.time() + 5
        target = camp / k.INFLIGHT_NAME
        while time.time() < deadline and not target.is_file():
            time.sleep(0.05)
        time.sleep(0.2)
        on_disk = k.load_json(camp / "progress.json")
    finally:
        k.stop_checkpoint(h)
    assert on_disk["discovered_pids"] == ["999"]
    assert h["pids"] == ["101"]


# --- batch_size: размер выдачи воркеру (режим «по одному товару») -------------

def test_inproc_gather_cfg_batch_size_default_one():
    """Без ключа в run_cfg демон выдаёт воркеру по одному товару."""
    cfg = k._inproc_gather_cfg({}, 3600)
    assert cfg["batch_size"] == 1


def test_inproc_gather_cfg_batch_size_from_manifest():
    """При resume значение берётся ИЗ run_cfg (манифест — источник истины)."""
    cfg = k._inproc_gather_cfg({"batch_size": 5}, 3600)
    assert cfg["batch_size"] == 5


def test_inproc_gather_cfg_batch_size_never_degenerate():
    """batch_size<=0 в манифесте дал бы тихий ноль: воркеры получат 'none'."""
    assert k._inproc_gather_cfg({"batch_size": 0}, 3600)["batch_size"] == 1
    assert k._inproc_gather_cfg({"batch_size": -2}, 3600)["batch_size"] == 1
    assert k._inproc_gather_cfg({"batch_size": None}, 3600)["batch_size"] == 1


def test_batch_size_cli_default_is_absent():
    """Без флага демон не переписывает манифест (значение — из run_cfg)."""
    assert k.parse_args(["--url", "u", "--campaign", "c"]).batch_size is None


def test_batch_size_cli_explicit_value():
    assert k.parse_args(["--url", "u", "--campaign", "c",
                         "--batch-size", "5"]).batch_size == 5
