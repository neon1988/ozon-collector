# Unit-тесты scripts/digest_from_cache.py: офлайн-рендер дайджеста из кеша.
# Не требуют сети, браузера и реальных данных data/.
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.digest_from_cache as DFC  # noqa: E402


# ---- accepted_pids: приоритет полей манифеста --------------------------------

def test_accepted_pids_priority():
    """campaign_accepted_pids > campaign_final_pids > valid_pids."""
    assert DFC.accepted_pids({"campaign_accepted_pids": ["1"],
                              "campaign_final_pids": ["2"],
                              "valid_pids": ["3"]}) == ["1"]
    assert DFC.accepted_pids({"campaign_final_pids": ["2"],
                              "valid_pids": ["3"]}) == ["2"]
    assert DFC.accepted_pids({"valid_pids": [3, 4]}) == ["3", "4"]


def test_accepted_pids_empty():
    """Нет ни одного поля — пустой список (дайджест писать нечего)."""
    assert DFC.accepted_pids({}) == []
    assert DFC.accepted_pids({"valid_pids": []}) == []


# ---- main(): отказы без записи файлов ----------------------------------------

def test_main_missing_manifest_returns_2(tmp_path, monkeypatch, capsys):
    """Нет манифеста кампании → rc=2, ничего не создано."""
    monkeypatch.setattr(DFC, "ROOT", tmp_path)
    assert DFC.main(["net-takoy-takoy"]) == 2
    assert "нет манифеста" in capsys.readouterr().err


def test_main_no_pids_returns_1(tmp_path, monkeypatch, capsys):
    """Манифест есть, принятых PID нет → rc=1, файл дайджеста не создан."""
    camp = tmp_path / "data" / "campaigns" / "pustaya"
    camp.mkdir(parents=True)
    (camp / "progress.json").write_text(
        json.dumps({"campaign": "pustaya", "valid_pids": [],
                    "out": "docs/digests/pustaya.md"}), encoding="utf-8")
    monkeypatch.setattr(DFC, "ROOT", tmp_path)
    assert DFC.main(["pustaya"]) == 1
    assert not (tmp_path / "docs" / "digests" / "pustaya.md").exists()


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    """--dry-run только печатает план (accepted PID + путь), файл не трогает."""
    camp = tmp_path / "data" / "campaigns" / "test_campaign"
    camp.mkdir(parents=True)
    (camp / "progress.json").write_text(
        json.dumps({"campaign": "test_campaign", "valid_pids": ["111", "222"],
                    "out": "docs/digests/test_campaign.md"}), encoding="utf-8")
    monkeypatch.setattr(DFC, "ROOT", tmp_path)
    assert DFC.main(["test_campaign", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "принято PID: 2" in out
    assert "dry-run" in out
    assert not (tmp_path / "docs" / "digests" / "test_campaign.md").exists()
