import json
import os
from pathlib import Path

import pytest

import gmail_delivery
import mac_collect_lorotopik_worklog as collector
from gmail_credentials import CredentialResolutionError
from gmail_delivery import (
    DRY_RUN,
    FAILED,
    SKIPPED_DUPLICATE,
    SUCCESS,
    DeliveryError,
    GmailDeliveryConfig,
    build_email_payload,
    deliver_daily_worklog,
    parse_gmail_delivery_config,
    resolve_daily_markdown,
)


ENV_VALUES = {
    "WORKLOGBRIDGE_GMAIL_ADDRESS": "sender@example.com",
    "WORKLOGBRIDGE_GMAIL_APP_PASSWORD": "fake-app-password",
    "WORKLOGBRIDGE_GMAIL_RECIPIENT": "recipient@example.com",
}


class FakeSMTP:
    def __init__(self, *args, error=None, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.error = error
        self.login_args = None
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def login(self, address, app_password):
        self.login_args = (address, app_password)
        if self.error:
            raise self.error

    def send_message(self, message):
        self.message = message
        return {}


def _environment(values=None):
    desired = ENV_VALUES if values is None else values
    previous = {name: os.environ.get(name) for name in ENV_VALUES}
    for name in ENV_VALUES:
        os.environ.pop(name, None)
    for name, value in desired.items():
        os.environ[name] = value

    def restore():
        for name in ENV_VALUES:
            os.environ.pop(name, None)
            if previous[name] is not None:
                os.environ[name] = previous[name]

    return restore


def _worklog(outbox: Path, date_value: str = "2026-07-03", body=None):
    markdown = outbox / f"daily_worklog_{date_value}.md"
    markdown.write_text(
        body
        or "\n".join(
            (
                "# LoroTopik 일일 근무일지 초안",
                "",
                f"- 날짜: {date_value}",
                "",
                "## 업무 요약",
                "",
                "안전한 회사 업무를 정리했다.",
                "",
                "## 분류 요약",
                "",
                "- 포함된 회사 업무: 1건",
                "- 검토가 필요한 uncertain: 1건",
                "",
                "### uncertain 검토 목록",
                "",
                "- 이메일로 보내지 않을 검토 상세",
                "- 제외된 개인 작업: 1건 (PrivateProject)",
                "- privacy 제외: 1건",
            )
        ),
        encoding="utf-8",
    )
    companion = markdown.with_suffix(".json")
    companion.write_text(
        json.dumps(
            {
                "generated_at": f"{date_value}T17:00:00+09:00",
                "mode": "daily",
                "date": date_value,
                "collection_summary": {
                    "git_activity_status": "ACTIVITY_FOUND",
                    "message": "회사 Git 활동 1건이 발견되었습니다.",
                },
                "fields": {
                    "DATE": date_value,
                    "SUMMARY": "안전한 회사 업무를 정리했다.",
                    "TASKS": "- 안전한 업무",
                },
                "included_items": [{"repo_path": "/private/company", "summary": "safe"}],
                "excluded_personal_items": [{"project_name": "PrivateProject"}],
                "uncertain_items": [{"summary": "이메일로 보내지 않을 검토 상세"}],
                "privacy_exclusions_summary": {"excluded_count": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return markdown


def _collector_config(tmp_path, *, enabled, fail_collection=False):
    repo = tmp_path / "TokenForge"
    (repo / ".git").mkdir(parents=True)
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    config_path = tmp_path / "config.local.json"
    config_path.write_text(
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
                "gmail_delivery": {
                    "enabled": enabled,
                    "smtp_server": "smtp.gmail.com",
                    "smtp_port": 465,
                    "sender_email_env": "WORKLOGBRIDGE_GMAIL_ADDRESS",
                    "app_password_env": "WORKLOGBRIDGE_GMAIL_APP_PASSWORD",
                    "recipient_email_env": "WORKLOGBRIDGE_GMAIL_RECIPIENT",
                    "attach_markdown": True,
                    "attach_json": True,
                    "include_markdown_body": True,
                    "max_body_chars": 12000,
                    "fail_collection_on_delivery_error": fail_collection,
                    "dedupe": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path, outbox, logs


def test_missing_markdown_file_fails_clearly(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    with pytest.raises(DeliveryError, match="daily Markdown 파일이 없습니다"):
        resolve_daily_markdown(outbox, date_value="2026-07-03")


def test_missing_json_attachment_fails_clearly(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    source = outbox / "daily_worklog_2026-07-03.md"
    source.write_text("# safe daily", encoding="utf-8")
    with pytest.raises(DeliveryError, match="daily JSON 파일이 없습니다"):
        build_email_payload(source, GmailDeliveryConfig())


def test_latest_resolves_newest_daily_markdown_by_mtime(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    old = _worklog(outbox, "2026-07-01")
    newest = _worklog(outbox, "2026-06-30")
    os.utime(old, (1, 1))
    os.utime(newest, (2, 2))
    assert resolve_daily_markdown(outbox, latest=True) == newest.resolve()


def test_dry_run_validates_without_smtp_or_metadata(tmp_path):
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    source = _worklog(outbox)
    restore = _environment({})
    try:
        result = deliver_daily_worklog(
            source,
            GmailDeliveryConfig(),
            logs,
            dry_run=True,
            smtp_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("SMTP must not be called")
            ),
        )
    finally:
        restore()
    assert result.status == DRY_RUN
    assert result.smtp_connection_attempted is False
    assert not list(logs.iterdir())


def test_missing_environment_variables_fails_without_smtp(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    source = _worklog(outbox)
    monkeypatch.setattr(
        gmail_delivery,
        "resolve_gmail_credentials",
        lambda *_args: (_ for _ in ()).throw(
            CredentialResolutionError(
                "Gmail credentials could not be resolved (missing: "
                "WORKLOGBRIDGE_GMAIL_APP_PASSWORD). Run: python3.10 "
                "mac_collect_lorotopik_worklog.py --config config.local.json "
                "--setup-gmail-keychain"
            )
        ),
    )
    restore = _environment({})
    try:
        result = deliver_daily_worklog(
            source,
            GmailDeliveryConfig(),
            logs,
            smtp_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("SMTP must not be called")
            ),
        )
    finally:
        restore()
    assert result.status == FAILED
    assert result.smtp_connection_attempted is False
    assert "WORKLOGBRIDGE_GMAIL_APP_PASSWORD" in result.sanitized_error
    assert json.loads(result.metadata_path.read_text(encoding="utf-8"))["status"] == FAILED


def test_body_format_and_truncation(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    source = _worklog(outbox, body="# Daily\n\n" + ("x" * 500))
    prepared = build_email_payload(source, GmailDeliveryConfig(max_body_chars=80))
    assert prepared.subject == "[Worklog Bridge] Daily Draft 2026-07-03"
    assert "Worklog Bridge Daily Draft" in prepared.body
    assert "Date:\n2026-07-03" in prepared.body
    assert "Activity:\n회사 Git 활동 1건이 발견되었습니다." in prepared.body
    assert "Summary:\n안전한 회사 업무를 정리했다." in prepared.body
    assert prepared.truncated is True
    assert prepared.body_chars <= 80
    assert "TRUNCATED" in prepared.body


def test_successful_mocked_send_has_safe_correct_attachments_and_metadata(tmp_path):
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    source = _worklog(outbox)
    fake = FakeSMTP()
    smtp_call = {}

    def factory(*args, **kwargs):
        smtp_call["args"] = args
        smtp_call["kwargs"] = kwargs
        return fake

    restore = _environment()
    try:
        result = deliver_daily_worklog(
            source,
            GmailDeliveryConfig(),
            logs,
            smtp_factory=factory,
        )
    finally:
        restore()
    assert result.status == SUCCESS
    assert smtp_call["args"] == ("smtp.gmail.com", 465)
    assert smtp_call["kwargs"]["timeout"] == 20
    assert smtp_call["kwargs"]["context"] is not None
    assert fake.message["Subject"] == "[Worklog Bridge] Daily Draft 2026-07-03"
    attachments = {part.get_filename(): part.get_content() for part in fake.message.iter_attachments()}
    assert set(attachments) == {
        "daily_worklog_2026-07-03.md",
        "daily_worklog_2026-07-03.json",
    }
    assert "PrivateProject" not in attachments["daily_worklog_2026-07-03.md"]
    assert "이메일로 보내지 않을 검토 상세" not in attachments["daily_worklog_2026-07-03.md"]
    safe_json = json.loads(attachments["daily_worklog_2026-07-03.json"])
    assert safe_json["fields"]["SUMMARY"] == "안전한 회사 업무를 정리했다."
    assert "excluded_personal_items" not in safe_json
    assert "uncertain_items" not in safe_json
    assert "included_items" not in safe_json
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["filename"] == source.name
    assert metadata["hash"] == result.source_hash
    assert metadata["status"] == SUCCESS
    assert metadata["smtp_response"] == "accepted"
    assert "message" not in metadata
    metadata_text = result.metadata_path.read_text(encoding="utf-8")
    assert not any(value in metadata_text for value in ENV_VALUES.values())


def test_failed_mocked_send_is_sanitized_and_preserves_files(tmp_path):
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    source = _worklog(outbox)
    original_markdown = source.read_text(encoding="utf-8")
    secret = ENV_VALUES["WORKLOGBRIDGE_GMAIL_APP_PASSWORD"]
    fake = FakeSMTP(error=RuntimeError(secret))
    restore = _environment()
    try:
        result = deliver_daily_worklog(
            source,
            GmailDeliveryConfig(),
            logs,
            smtp_factory=lambda *args, **kwargs: fake,
        )
    finally:
        restore()
    metadata_text = result.metadata_path.read_text(encoding="utf-8")
    assert result.status == FAILED
    assert secret not in metadata_text
    assert "sender@example.com" not in metadata_text
    assert "recipient@example.com" not in metadata_text
    assert source.read_text(encoding="utf-8") == original_markdown
    assert source.with_suffix(".json").is_file()


def test_dedupe_and_force(tmp_path):
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    outbox.mkdir()
    logs.mkdir()
    source = _worklog(outbox)
    calls = []

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeSMTP()

    restore = _environment()
    try:
        first = deliver_daily_worklog(source, GmailDeliveryConfig(), logs, smtp_factory=factory)
        second = deliver_daily_worklog(source, GmailDeliveryConfig(), logs, smtp_factory=factory)
        forced = deliver_daily_worklog(
            source, GmailDeliveryConfig(), logs, force=True, smtp_factory=factory
        )
    finally:
        restore()
    assert first.status == SUCCESS
    assert second.status == SKIPPED_DUPLICATE
    assert second.dedupe_hit is True
    assert forced.status == SUCCESS
    assert len(calls) == 2


def test_plaintext_addresses_or_password_are_rejected_in_config():
    with pytest.raises(DeliveryError, match="지원되는 설정"):
        parse_gmail_delivery_config(
            {"gmail_delivery": {"sender_email": "sender@example.com"}}
        )
    with pytest.raises(DeliveryError, match="지원되는 설정"):
        parse_gmail_delivery_config(
            {"gmail_delivery": {"app_password": "must-not-be-stored"}}
        )


def test_privacy_guard_blocks_code_or_secret_content(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    source = _worklog(outbox, body="```python\nprint('no')\n```")
    with pytest.raises(DeliveryError, match="privacy guard"):
        build_email_payload(source, GmailDeliveryConfig())


def test_disabled_config_keeps_daily_flow_without_delivery(tmp_path, monkeypatch):
    config_path, outbox, _ = _collector_config(tmp_path, enabled=False)

    def unexpected_delivery(*_args, **_kwargs):
        raise AssertionError("disabled delivery must not be called")

    monkeypatch.setattr(collector, "deliver_daily_worklog", unexpected_delivery)
    result = collector.main(
        [
            "--config",
            str(config_path),
            "--mode",
            "daily",
            "--since",
            "2026-07-03",
            "--until",
            "2026-07-03",
        ]
    )
    assert result == 0
    assert len(list(outbox.glob("daily_worklog_*.md"))) == 1


def test_enabled_collector_daily_dry_run_never_enters_delivery(tmp_path, monkeypatch):
    config_path, outbox, logs = _collector_config(tmp_path, enabled=True)

    def unexpected_delivery(*_args, **_kwargs):
        raise AssertionError("collector dry-run must not enter Gmail delivery")

    monkeypatch.setattr(collector, "deliver_daily_worklog", unexpected_delivery)
    result = collector.main(
        [
            "--config",
            str(config_path),
            "--mode",
            "daily",
            "--since",
            "2026-07-03",
            "--until",
            "2026-07-03",
            "--dry-run",
        ]
    )
    assert result == 0
    assert not list(outbox.iterdir())
    assert not list(logs.iterdir())


def test_delivery_failure_does_not_fail_collection_by_default(tmp_path, monkeypatch):
    config_path, outbox, logs = _collector_config(tmp_path, enabled=True)
    monkeypatch.setattr(
        gmail_delivery,
        "resolve_gmail_credentials",
        lambda *_args: (_ for _ in ()).throw(
            CredentialResolutionError("Gmail credentials could not be resolved")
        ),
    )
    restore = _environment({})
    try:
        result = collector.main(
            [
                "--config",
                str(config_path),
                "--mode",
                "daily",
                "--since",
                "2026-07-03",
                "--until",
                "2026-07-03",
            ]
        )
    finally:
        restore()
    assert result == 0
    assert len(list(outbox.glob("daily_worklog_*.md"))) == 1
    assert len(list(outbox.glob("daily_worklog_*.json"))) == 1
    summary = json.loads((logs / "last_run_summary.json").read_text(encoding="utf-8"))
    assert summary["final_status"] == SUCCESS


def test_strict_delivery_failure_returns_nonzero_after_files_are_written(
    tmp_path, monkeypatch
):
    config_path, outbox, logs = _collector_config(
        tmp_path, enabled=True, fail_collection=True
    )
    monkeypatch.setattr(
        gmail_delivery,
        "resolve_gmail_credentials",
        lambda *_args: (_ for _ in ()).throw(
            CredentialResolutionError("Gmail credentials could not be resolved")
        ),
    )
    restore = _environment({})
    try:
        result = collector.main(
            [
                "--config",
                str(config_path),
                "--mode",
                "daily",
                "--since",
                "2026-07-03",
                "--until",
                "2026-07-03",
            ]
        )
    finally:
        restore()
    assert result == 2
    assert len(list(outbox.glob("daily_worklog_*.md"))) == 1
    assert len(list(outbox.glob("daily_worklog_*.json"))) == 1
    summary = json.loads((logs / "last_run_summary.json").read_text(encoding="utf-8"))
    assert summary["final_status"] == "DELIVERY_FAILED"
