import os
import json
import plistlib
import subprocess
import sys
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install_macos_launchd.sh"
COLLECTOR = Path(__file__).resolve().parents[1] / "mac_collect_lorotopik_worklog.py"
LABEL = "com.worklogbridge.lorolog.daily.plist"


def _seoul_environment(tmp_path, home):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_readlink = bin_dir / "readlink"
    fake_readlink.write_text(
        "#!/bin/sh\nprintf '%s\\n' '/var/db/timezone/zoneinfo/Asia/Seoul'\n",
        encoding="utf-8",
    )
    fake_readlink.chmod(0o755)
    return {
        **os.environ,
        "HOME": str(home),
        "PYTHON_BIN": sys.executable,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    }


def _install_launchctl_stub(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_launchctl.chmod(0o755)


def _operational_fixture(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "LoroTOPIK"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("fixture", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "문구 수정"], check=True)
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    config = tmp_path / "config.local.json"
    config.write_text(
        json.dumps(
            {
                "config_version": 1,
                "timezone": "Asia/Seoul",
                "repo_paths": [str(repo)],
                "notes": {"enabled": False, "path": ""},
                "plan": {"enabled": False, "path": ""},
                "outbox_dir": str(outbox),
                "log_dir": str(logs),
                "privacy_exclude_patterns": [".env"],
                "company_keyword_hints": ["LoroTOPIK"],
                "personal_exclude_hints": ["TokenForge"],
            }
        ),
        encoding="utf-8",
    )
    return home, repo, outbox, logs, config


def test_launchd_installer_refuses_missing_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    missing = tmp_path / "missing.json"
    environment = {**os.environ, "HOME": str(home), "PYTHON_BIN": sys.executable}
    completed = subprocess.run(
        ["zsh", str(INSTALLER), "--config", str(missing)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 2
    assert "config 파일이 없습니다" in completed.stderr
    assert not (home / "Library" / "LaunchAgents" / LABEL).exists()


def test_launchd_installer_refuses_python_below_310_when_simulated(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_python = tmp_path / "python3"
    fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_python.chmod(0o755)
    environment = {**os.environ, "HOME": str(home), "PYTHON_BIN": str(fake_python)}
    completed = subprocess.run(
        ["zsh", str(INSTALLER), "--config", str(tmp_path / "config.json")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 2
    assert "Python 3.10+" in completed.stderr
    assert not (home / "Library" / "LaunchAgents" / LABEL).exists()


def test_launchd_dry_run_refuses_missing_config_without_installing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home), "PYTHON_BIN": sys.executable}
    completed = subprocess.run(
        [
            "zsh", str(INSTALLER), "--config", str(tmp_path / "missing.json"),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 2
    assert "설치하거나 job을 load하지 않습니다" in completed.stdout
    assert "config 파일이 없습니다" in completed.stderr
    assert not (home / "Library" / "LaunchAgents" / LABEL).exists()


def test_launchd_installer_requires_successful_one_shot_evidence(tmp_path):
    home, _, _, _, config = _operational_fixture(tmp_path)
    environment = _seoul_environment(tmp_path, home)
    completed = subprocess.run(
        ["zsh", str(INSTALLER), "--config", str(config), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 2
    assert "one-shot" in completed.stderr
    assert not (home / "Library" / "LaunchAgents" / LABEL).exists()


def test_launchd_installer_captures_and_reports_preflight_failure(tmp_path):
    home, repo, _, _, config = _operational_fixture(tmp_path)
    repo.rename(tmp_path / "repo-moved-out-of-config")
    environment = _seoul_environment(tmp_path, home)
    completed = subprocess.run(
        ["zsh", str(INSTALLER), "--config", str(config), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 2
    assert "preflight exit: 2" in completed.stdout
    assert "preflight가 exit 2로 실패" in completed.stderr
    assert "parameter not set" not in completed.stderr
    assert not (home / "Library" / "LaunchAgents" / LABEL).exists()


def test_launchd_dry_run_checks_all_gates_without_installing(tmp_path):
    home, _, _, _, config = _operational_fixture(tmp_path)
    subprocess.run(
        [sys.executable, str(COLLECTOR), "--config", str(config), "--mode", "daily"],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = _seoul_environment(tmp_path, home)
    completed = subprocess.run(
        ["zsh", str(INSTALLER), "--config", str(config), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0
    assert "preflight exit: 0" in completed.stdout
    assert "daily dry-run exit: 0" in completed.stdout
    assert "one-shot evidence: PASS" in completed.stdout
    assert "DRY RUN READY" in completed.stdout
    assert "NOT INSTALLED" in completed.stdout
    assert not (home / "Library" / "LaunchAgents" / LABEL).exists()


def test_launchd_dry_run_requires_nonempty_source_repo_evidence(tmp_path):
    home, _, _, logs, config = _operational_fixture(tmp_path)
    subprocess.run(
        [sys.executable, str(COLLECTOR), "--config", str(config), "--mode", "daily"],
        check=True,
        capture_output=True,
        text=True,
    )
    summary_path = logs / "last_run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["source_repos_considered"] = []
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    environment = _seoul_environment(tmp_path, home)
    completed = subprocess.run(
        ["zsh", str(INSTALLER), "--config", str(config), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 2
    assert "source_repos_considered가 비어 있습니다" in completed.stderr
    assert not (home / "Library" / "LaunchAgents" / LABEL).exists()


def test_installer_has_no_runner_dependency():
    installer_source = INSTALLER.read_text(encoding="utf-8")
    assert "run_macos_daily.sh" not in installer_source
    assert "DAILY_LAUNCHER" not in installer_source


def test_installer_renders_direct_python_plist_with_spaced_paths_and_no_credentials(tmp_path):
    spaced_root = tmp_path / "Worklog Bridge fixture"
    spaced_root.mkdir()
    home, _, _, _, config = _operational_fixture(spaced_root)

    python_dir = spaced_root / "Python Runtime"
    python_dir.mkdir()
    python_bin = python_dir / "python 3.10"
    python_bin.symlink_to(sys.executable)

    subprocess.run(
        [str(python_bin), str(COLLECTOR), "--config", str(config), "--mode", "daily"],
        check=True,
        capture_output=True,
        text=True,
    )
    _install_launchctl_stub(spaced_root)
    environment = _seoul_environment(spaced_root, home)
    credential_sentinels = {
        "WORKLOGBRIDGE_GMAIL_ADDRESS": "sender-secret@example.invalid",
        "WORKLOGBRIDGE_GMAIL_APP_PASSWORD": "never-store-this-app-password",
        "WORKLOGBRIDGE_GMAIL_RECIPIENT": "recipient-secret@example.invalid",
    }
    environment.update(credential_sentinels)
    environment["PYTHON_BIN"] = str(python_bin)

    completed = subprocess.run(
        ["zsh", str(INSTALLER), "--config", str(config)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr

    installed_plist = home / "Library" / "LaunchAgents" / LABEL
    with installed_plist.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["ProgramArguments"] == [
        str(python_bin),
        str(COLLECTOR),
        "--config",
        str(config),
        "--mode",
        "daily",
    ]
    installed_text = installed_plist.read_text(encoding="utf-8")
    assert "run_macos_daily" not in installed_text
    assert "Worklog Bridge fixture" in installed_text
    for secret_value in credential_sentinels.values():
        assert secret_value not in installed_text
    environment_variables = payload.get("EnvironmentVariables", {})
    assert not any(key.startswith("WORKLOGBRIDGE_GMAIL_") for key in environment_variables)
