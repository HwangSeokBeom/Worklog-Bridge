import plistlib
from pathlib import Path

from mac_collect_lorotopik_worklog import _schedule_is_weekdays_at_17


def test_launchd_template_runs_daily_mode_weekdays_at_17():
    plist_path = (
        Path(__file__).resolve().parents[1]
        / "launchd"
        / "com.worklogbridge.lorolog.daily.plist"
    )
    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert _schedule_is_weekdays_at_17(payload)
    arguments = payload["ProgramArguments"]
    assert arguments == [
        "__PYTHON__",
        "__PROJECT_DIR__/mac_collect_lorotopik_worklog.py",
        "--config",
        "__CONFIG_PATH__",
        "--mode",
        "daily",
    ]
    assert payload["EnvironmentVariables"]["WORKLOGBRIDGE_CONFIG"] == "__CONFIG_PATH__"
    assert payload["EnvironmentVariables"]["TZ"] == "Asia/Seoul"
    environment = payload["EnvironmentVariables"]
    assert "WORKLOGBRIDGE_GMAIL_ADDRESS" not in environment
    assert "WORKLOGBRIDGE_GMAIL_APP_PASSWORD" not in environment
    assert "WORKLOGBRIDGE_GMAIL_RECIPIENT" not in environment
    assert not any("TEAMS" in key or "WEBHOOK" in key for key in environment)


def test_launchd_template_has_no_runner_or_embedded_delivery_credentials():
    template = (
        Path(__file__).resolve().parents[1]
        / "launchd"
        / "com.worklogbridge.lorolog.daily.plist"
    ).read_text(encoding="utf-8")
    folded = template.casefold()
    assert "run_macos_daily" not in template
    assert "keychain" not in folded
    assert "webhook" not in folded
    assert "gmail_address" not in folded
    assert "gmail_app_password" not in folded
    assert "gmail_recipient" not in folded


def test_launchd_plist_generation_replaces_local_placeholders():
    plist_path = (
        Path(__file__).resolve().parents[1]
        / "launchd"
        / "com.worklogbridge.lorolog.daily.plist"
    )
    rendered = plist_path.read_text(encoding="utf-8")
    replacements = {
        "__PYTHON__": "/opt/python3",
        "__PROJECT_DIR__": "/opt/Worklog Bridge",
        "__CONFIG_PATH__": "/opt/worklog/config.local.json",
        "__LOG_DIR__": "/opt/worklog/logs",
        "__HOME__": "/Users/operator",
    }
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    assert "__" not in rendered
    payload = plistlib.loads(rendered.encode("utf-8"))
    arguments = payload["ProgramArguments"]
    assert arguments == [
        "/opt/python3",
        "/opt/Worklog Bridge/mac_collect_lorotopik_worklog.py",
        "--config",
        "/opt/worklog/config.local.json",
        "--mode",
        "daily",
    ]
    assert arguments[arguments.index("--config") + 1] == "/opt/worklog/config.local.json"
    assert payload["StandardOutPath"].startswith("/opt/worklog/logs/")
