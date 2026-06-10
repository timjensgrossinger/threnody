#!/usr/bin/env python3
"""Tests for shared/doctor.py — provider health diagnostics."""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("THRENODY_TEST_MODE", "1")

from shared.db import Database
from shared.health import _FAILURE_THRESHOLD, record_provider_failure
import shared.doctor as doctor_mod


def _make_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Database(Path(f.name))


def test_diagnose_all_healthy_exits_zero(capsys):
    db = _make_db()
    try:
        # No failures recorded → all healthy
        with patch.object(doctor_mod, "_load_providers", return_value=[
            {"name": "github-copilot", "display_name": "GitHub Copilot", "available": True},
        ]):
            exit_code = doctor_mod.diagnose(db)
        assert exit_code == 0
    finally:
        db.close()


def test_diagnose_quarantined_exits_one(capsys):
    db = _make_db()
    try:
        for _ in range(_FAILURE_THRESHOLD):
            record_provider_failure(db, "github-copilot", "auth_expired")
        with patch.object(doctor_mod, "_load_providers", return_value=[
            {"name": "github-copilot", "display_name": "GitHub Copilot", "available": True},
        ]):
            exit_code = doctor_mod.diagnose(db)
        assert exit_code == 1
    finally:
        db.close()


def test_diagnose_output_contains_provider(capsys):
    db = _make_db()
    try:
        with patch.object(doctor_mod, "_load_providers", return_value=[
            {"name": "claude-code", "display_name": "Claude Code", "available": True},
        ]):
            doctor_mod.diagnose(db)
        captured = capsys.readouterr()
        assert "Claude Code" in captured.out
    finally:
        db.close()


def test_suggest_fix_auth_expired_copilot():
    fix = doctor_mod._suggest_fix("github-copilot", "auth_expired")
    assert "gh auth login" in fix


def test_suggest_fix_auth_expired_claude():
    fix = doctor_mod._suggest_fix("claude-code", "auth_expired")
    assert "claude login" in fix


def test_suggest_fix_binary_missing():
    fix = doctor_mod._suggest_fix("github-copilot", "binary_missing")
    assert "install" in fix.lower()


def test_suggest_fix_unknown_provider():
    fix = doctor_mod._suggest_fix("unknown-provider", "auth_expired")
    assert fix  # Returns something, not empty


def test_repair_creates_backup(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    try:
        # Patch last_backup_ts to None (overdue) and backup_db to a mock
        with patch.object(type(db), "last_backup_ts", new_callable=lambda: property(lambda self: None)):
            with patch.object(db, "backup_db", return_value=str(tmp_path / "backup.db")) as mock_backup:
                doctor_mod.run_self_repair(db, dry_run=False)
                mock_backup.assert_called_once()
    finally:
        db.close()


def test_repair_dry_run_no_backup(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    try:
        with patch.object(type(db), "last_backup_ts", new_callable=lambda: property(lambda self: None)):
            with patch.object(db, "backup_db") as mock_backup:
                doctor_mod.run_self_repair(db, dry_run=True)
                mock_backup.assert_not_called()
    finally:
        db.close()
