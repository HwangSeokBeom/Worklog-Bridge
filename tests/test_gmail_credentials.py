from pathlib import Path

import mac_collect_lorotopik_worklog as collector
from gmail_credentials import (
    CredentialResolutionError,
    GmailCredentials,
    KEYCHAIN_ACCOUNTS,
    KEYCHAIN_SERVICE,
    resolve_gmail_credentials,
    setup_macos_keychain,
)
from gmail_delivery import GmailDeliveryConfig
from mac_collect_lorotopik_worklog import CollectorConfig, print_diagnostics


VALUES = {
    "WORKLOGBRIDGE_GMAIL_ADDRESS": "sender-sensitive@example.invalid",
    "WORKLOGBRIDGE_GMAIL_RECIPIENT": "recipient-sensitive@example.invalid",
    "WORKLOGBRIDGE_GMAIL_APP_PASSWORD": "sensitive-app-password",
}


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_environment_credentials_win_without_keychain_access():
    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("Keychain must not be queried when env is complete")

    credentials = resolve_gmail_credentials(
        environment=VALUES,
        keychain_runner=unexpected_runner,
        platform_name="darwin",
    )
    assert credentials.source == "env"
    assert credentials.sender == VALUES["WORKLOGBRIDGE_GMAIL_ADDRESS"]
    assert credentials.recipient == VALUES["WORKLOGBRIDGE_GMAIL_RECIPIENT"]
    assert credentials.app_password == VALUES["WORKLOGBRIDGE_GMAIL_APP_PASSWORD"]


def test_keychain_is_used_when_environment_is_missing():
    by_account = {
        KEYCHAIN_ACCOUNTS["sender"]: VALUES["WORKLOGBRIDGE_GMAIL_ADDRESS"],
        KEYCHAIN_ACCOUNTS["recipient"]: VALUES["WORKLOGBRIDGE_GMAIL_RECIPIENT"],
        KEYCHAIN_ACCOUNTS["app_password"]: VALUES["WORKLOGBRIDGE_GMAIL_APP_PASSWORD"],
    }
    commands = []

    def security_runner(command, **_kwargs):
        commands.append(command)
        return Completed(stdout=by_account[command[command.index("-a") + 1]] + "\n")

    credentials = resolve_gmail_credentials(
        environment={},
        keychain_runner=security_runner,
        platform_name="darwin",
    )
    assert credentials.source == "keychain"
    assert len(commands) == 3
    assert all(command[0] == "/usr/bin/security" for command in commands)
    assert all(KEYCHAIN_SERVICE in command for command in commands)


def test_missing_credentials_error_is_sanitized():
    def missing_runner(*_args, **_kwargs):
        return Completed(returncode=44, stderr="unsafe: sensitive-app-password")

    try:
        resolve_gmail_credentials(
            environment={
                "WORKLOGBRIDGE_GMAIL_ADDRESS": VALUES[
                    "WORKLOGBRIDGE_GMAIL_ADDRESS"
                ]
            },
            keychain_runner=missing_runner,
            platform_name="darwin",
        )
    except CredentialResolutionError as exc:
        message = str(exc)
    else:
        raise AssertionError("missing credentials must fail")
    assert "--setup-gmail-keychain" in message
    assert VALUES["WORKLOGBRIDGE_GMAIL_ADDRESS"] not in message
    assert VALUES["WORKLOGBRIDGE_GMAIL_RECIPIENT"] not in message
    assert VALUES["WORKLOGBRIDGE_GMAIL_APP_PASSWORD"] not in message


def test_keychain_setup_uses_hidden_security_prompt_without_values_in_arguments():
    commands = []
    output = []

    def security_runner(command, **_kwargs):
        commands.append(command)
        return Completed()

    setup_macos_keychain(
        runner=security_runner,
        platform_name="darwin",
        output=output.append,
    )
    assert len(commands) == 3
    assert all(command[-1] == "-w" for command in commands)
    rendered = repr(commands) + repr(output)
    assert not any(value in rendered for value in VALUES.values())


def test_keychain_setup_dry_run_accepts_env_without_writing_or_printing_values():
    output = []

    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("dry-run must not alter Keychain")

    setup_macos_keychain(
        dry_run=True,
        environment=VALUES,
        runner=unexpected_runner,
        platform_name="darwin",
        output=output.append,
    )
    rendered = "\n".join(output)
    assert "DRY_RUN" in rendered
    assert not any(value in rendered for value in VALUES.values())


def _diagnostic_config(tmp_path: Path) -> CollectorConfig:
    repo = tmp_path / "LoroTOPIK"
    (repo / ".git").mkdir(parents=True)
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    config_path = tmp_path / "config.local.json"
    config_path.write_text("{}", encoding="utf-8")
    return CollectorConfig(
        config_path=config_path,
        config_exists=True,
        repos=[repo],
        out_dir=outbox,
        log_dir=logs,
        company_keyword_hints=("LoroTOPIK",),
        gmail_delivery=GmailDeliveryConfig(enabled=True),
    )


def test_preflight_reports_keychain_credentials_without_values(
    tmp_path, capsys, monkeypatch
):
    config = _diagnostic_config(tmp_path)
    monkeypatch.setattr(collector, "_system_timezone_name", lambda: "Asia/Seoul")
    monkeypatch.setattr(
        collector, "collect_configured_sources", lambda *args, **kwargs: ([], [])
    )
    monkeypatch.setattr(
        collector,
        "resolve_gmail_credentials",
        lambda *_args: GmailCredentials(
            sender=VALUES["WORKLOGBRIDGE_GMAIL_ADDRESS"],
            recipient=VALUES["WORKLOGBRIDGE_GMAIL_RECIPIENT"],
            app_password=VALUES["WORKLOGBRIDGE_GMAIL_APP_PASSWORD"],
            source="keychain",
        ),
    )
    assert print_diagnostics(config, preflight_only=True) == 0
    output = capsys.readouterr().out
    assert "credentials available from keychain" in output
    assert not any(value in output for value in VALUES.values())


def test_preflight_blocks_missing_gmail_credentials_with_exact_setup_command(
    tmp_path, capsys, monkeypatch
):
    config = _diagnostic_config(tmp_path)
    monkeypatch.setattr(collector, "_system_timezone_name", lambda: "Asia/Seoul")
    monkeypatch.setattr(
        collector, "collect_configured_sources", lambda *args, **kwargs: ([], [])
    )

    def missing(*_args):
        raise CredentialResolutionError("unsafe detail must not be displayed")

    monkeypatch.setattr(collector, "resolve_gmail_credentials", missing)
    assert print_diagnostics(config, preflight_only=True) == 2
    output = capsys.readouterr().out
    assert "enabled but credentials missing" in output
    assert "--setup-gmail-keychain" in output
    assert str(config.config_path) in output
    assert "unsafe detail" not in output
