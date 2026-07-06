import json
from pathlib import Path

from scripts.setup_macos_local import (
    create_runtime_directories,
    discover_loro_repos,
    scaffold_config,
)


def _example(project_root):
    payload = {
        "config_version": 1,
        "timezone": "Asia/Seoul",
        "repo_paths": ["/example/LoroTOPIK"],
        "notes": {"enabled": True, "path": "/example/notes"},
        "plan": {"enabled": True, "path": "/example/plan.md"},
        "outbox_dir": "/example/outbox",
        "log_dir": "/example/logs",
        "privacy_exclude_patterns": [".env"],
        "company_keyword_hints": ["LoroTOPIK"],
        "personal_exclude_hints": ["TokenForge"],
    }
    (project_root / "config.example.json").write_text(json.dumps(payload), encoding="utf-8")


def test_config_scaffold_does_not_overwrite_existing_file(tmp_path):
    _example(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text('{"preserve": true}\n', encoding="utf-8")
    returned, created = scaffold_config(tmp_path, tmp_path / "home", [])
    assert returned == config_path
    assert created is False
    assert config_path.read_text(encoding="utf-8") == '{"preserve": true}\n'


def test_config_scaffold_uses_one_discovered_repo(tmp_path):
    _example(tmp_path)
    repo = tmp_path / "LoroTOPIK"
    config_path, created = scaffold_config(tmp_path, tmp_path / "home", [repo])
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert created is True
    assert payload["repo_paths"] == [str(repo)]
    assert payload["notes"]["enabled"] is False
    assert payload["timezone"] == "Asia/Seoul"


def test_safe_outbox_and_log_directories_are_created(tmp_path):
    paths, errors = create_runtime_directories(tmp_path)
    assert errors == []
    assert all(path.is_dir() for path in paths)
    assert paths[0].name == "outbox"
    assert paths[1].name == "logs"


def test_discovery_only_returns_loro_git_repos(tmp_path):
    company = tmp_path / "Documents" / "GitHub" / "LoroTOPIK-App"
    personal = tmp_path / "Documents" / "GitHub" / "TokenForge"
    unrelated = tmp_path / "Desktop" / "OtherProject"
    for repo in (company, personal, unrelated):
        (repo / ".git").mkdir(parents=True)
    candidates = discover_loro_repos([tmp_path / "Documents", tmp_path / "Desktop"])
    assert candidates == [company.resolve()]

