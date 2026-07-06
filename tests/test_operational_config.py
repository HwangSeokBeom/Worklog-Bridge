import argparse
import json
import subprocess
from pathlib import Path

import pytest
import mac_collect_lorotopik_worklog as collector

from mac_collect_lorotopik_worklog import (
    CollectorConfig,
    PROJECT_ROOT,
    collect_configured_sources,
    load_operator_config,
    main,
    print_diagnostics,
    resolve_collector_config,
    resolve_period,
    validate_output_directory,
)


def _payload(tmp_path, *, repos=None, outbox=None, log_dir=None):
    return {
        "config_version": 1,
        "timezone": "Asia/Seoul",
        "repo_paths": repos or [],
        "notes": {"enabled": False, "path": ""},
        "plan": {"enabled": False, "path": ""},
        "outbox_path": str(outbox or (tmp_path / "outbox")),
        "log_dir": str(log_dir or (tmp_path / "logs")),
        "privacy_exclude_patterns": [".env", "**/secrets/**"],
        "company_keyword_hints": ["LoroTOPIK"],
        "personal_exclude_hints": ["TokenForge", "personal"],
    }


def _write_config(tmp_path, payload):
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _args(config_path):
    return argparse.Namespace(
        config=config_path,
        repo=[],
        notes_dir=None,
        plan_file=None,
        out_dir=None,
    )


def test_missing_config_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="config 파일이 없습니다"):
        load_operator_config(tmp_path / "missing.json")


def test_missing_repo_path_blocks_diagnostics(tmp_path, capsys):
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(tmp_path, repos=[str(tmp_path / "LoroTOPIK-missing")], outbox=outbox, log_dir=logs),
    )
    result = print_diagnostics(resolve_collector_config(_args(config_path)))
    assert result == 2
    assert "repo 경로가 없습니다" in capsys.readouterr().out


def test_missing_outbox_path_is_rejected(tmp_path):
    errors = validate_output_directory(tmp_path / "missing-outbox", [], require_exists=True)
    assert any("outbox 디렉터리가 없습니다" in error for error in errors)


def test_preflight_blocks_non_git_repo(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "LoroTOPIK"
    repo.mkdir()
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(tmp_path, repos=[str(repo)], outbox=outbox, log_dir=logs),
    )
    monkeypatch.setattr(collector, "_system_timezone_name", lambda: "Asia/Seoul")
    result = print_diagnostics(resolve_collector_config(_args(config_path)), preflight_only=True)
    assert result == 2
    assert "Git repo가 아닙니다" in capsys.readouterr().out


def test_dry_run_creates_no_files(tmp_path, capsys):
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    repo = tmp_path / "TokenForge"
    (repo / ".git").mkdir(parents=True)
    config_path = _write_config(
        tmp_path,
        _payload(tmp_path, repos=[str(repo)], outbox=outbox, log_dir=logs),
    )
    before = list(outbox.iterdir())
    result = main(
        [
            "--config", str(config_path), "--dry-run", "--mode", "daily",
            "--since", "2026-07-02", "--until", "2026-07-02",
        ]
    )
    assert result == 0
    assert list(outbox.iterdir()) == before
    assert "files_written: false" in capsys.readouterr().out


def test_output_path_guard_rejects_source_tree():
    errors = validate_output_directory(PROJECT_ROOT / "outbox", [], require_exists=False)
    assert any("source repo 내부" in error for error in errors)


def test_output_path_guard_rejects_home_root():
    errors = validate_output_directory(Path.home(), [], require_exists=False)
    assert any("HOME 자체" in error for error in errors)


def test_configured_outbox_cannot_be_overridden(tmp_path):
    repo = tmp_path / "LoroTOPIK"
    (repo / ".git").mkdir(parents=True)
    configured_outbox = tmp_path / "configured-outbox"
    configured_outbox.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(
            tmp_path,
            repos=[str(repo)],
            outbox=configured_outbox,
            log_dir=logs,
        ),
    )
    args = _args(config_path)
    args.out_dir = tmp_path / "different-outbox"
    config = resolve_collector_config(args)
    assert config.config_error is not None
    assert "outbox_dir와 같아야" in config.config_error


def test_personal_repo_is_excluded_without_reading_commits(tmp_path):
    repo = tmp_path / "TokenForge"
    (repo / ".git").mkdir(parents=True)
    config = CollectorConfig(
        repos=[repo],
        out_dir=tmp_path / "outbox",
        personal_exclude_hints=("TokenForge",),
    )
    since, until = resolve_period("daily", "2026-07-02", "2026-07-02")
    items, warnings = collect_configured_sources(config, since, until)
    assert len(items) == 1
    assert items[0]["classification"] == "personal_work"
    assert any("commit metadata도 수집하지 않았습니다" in warning for warning in warnings)


@pytest.mark.parametrize(
    "forbidden_key",
    ["token", "password", "secret", "api_key", "access_key", "refresh_token"],
)
def test_config_rejects_secret_fields(tmp_path, forbidden_key):
    payload = _payload(tmp_path)
    payload[forbidden_key] = "never-store-this"
    config_path = _write_config(tmp_path, payload)
    with pytest.raises(ValueError, match="비밀정보 키"):
        load_operator_config(config_path)


def test_ready_config_is_warning_until_launchd_is_installed(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "LoroTOPIK"
    (repo / ".git").mkdir(parents=True)
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(tmp_path, repos=[str(repo)], outbox=outbox, log_dir=logs),
    )
    config = resolve_collector_config(_args(config_path))
    config.launchd_plist = tmp_path / "not-installed.plist"
    monkeypatch.setattr(collector, "_system_timezone_name", lambda: "Asia/Seoul")
    monkeypatch.setattr(
        collector,
        "collect_configured_sources",
        lambda *args, **kwargs: ([], []),
    )
    result = print_diagnostics(config)
    output = capsys.readouterr().out
    assert result == 1
    assert "read-only dry-run" in output
    assert "PARTIALLY_CONFIGURED" in output


def test_preflight_succeeds_with_valid_temp_environment(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "LoroTOPIK"
    (repo / ".git").mkdir(parents=True)
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(tmp_path, repos=[str(repo)], outbox=outbox, log_dir=logs),
    )
    config = resolve_collector_config(_args(config_path))
    monkeypatch.setattr(collector, "_system_timezone_name", lambda: "Asia/Seoul")
    monkeypatch.setattr(collector, "collect_configured_sources", lambda *args, **kwargs: ([], []))
    result = print_diagnostics(config, preflight_only=True)
    assert result == 0
    assert "Result: READY" in capsys.readouterr().out


def test_real_run_creates_worklogs_and_safe_run_summary(tmp_path, capsys):
    repo = tmp_path / "LoroTOPIK"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("temporary fixture", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "문구 수정"], check=True)
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(tmp_path, repos=[str(repo)], outbox=outbox, log_dir=logs),
    )
    result = main(
        [
            "--config", str(config_path), "--mode", "daily",
            "--since", "2026-01-01", "--until", "2026-12-31",
        ]
    )
    assert result == 0
    assert len(list(outbox.glob("daily_worklog_*.json"))) == 1
    assert len(list(outbox.glob("daily_worklog_*.md"))) == 1
    summary = json.loads((logs / "last_run_summary.json").read_text(encoding="utf-8"))
    assert summary["final_status"] == "SUCCESS"
    assert summary["mode"] == "daily"
    assert len(summary["output_files_created"]) == 2
    assert summary["collected_item_count"] == 1
    assert summary["included_company_work_count"] == 1
    assert summary["git_activity_count"] == 1
    assert summary["git_activity_status"] == "ACTIVITY_FOUND"
    assert "문구 수정" not in json.dumps(summary, ensure_ascii=False)
    assert "Run summary:" in capsys.readouterr().out


def test_activity_diagnostic_shows_repo_dates_and_presence_without_subject(tmp_path, capsys):
    repo = tmp_path / "LoroTOPIK"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("fixture", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    secret_subject = "subject-must-not-appear"
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", secret_subject], check=True)
    config_path = _write_config(tmp_path, _payload(tmp_path, repos=[str(repo)]))

    result = main(
        [
            "--config", str(config_path), "--activity-diagnose",
            "--since", "2000-01-01", "--until", "2000-01-01",
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "repo:" in output
    assert repo.name in output
    assert "latest_commit:" in output
    assert "commits_in_selected_range: NO (0)" in output
    assert "activity_status: EXPECTED_ZERO_ACTIVITY" in output
    assert secret_subject not in output
