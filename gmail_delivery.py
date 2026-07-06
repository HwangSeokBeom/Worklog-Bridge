"""Optional, privacy-preserving delivery of generated daily worklogs by Gmail.

Credentials use explicit environment variables first and macOS Keychain as a
reboot-safe fallback. Delivery uses Gmail SMTP over SSL and never modifies the
generated source files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Callable, Mapping, Optional

from gmail_credentials import (
    CredentialResolutionError,
    resolve_gmail_credentials,
)
from privacy_guard import is_sensitive_line, sanitize_line


DEFAULT_SMTP_SERVER = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465
DEFAULT_SENDER_ENV = "WORKLOGBRIDGE_GMAIL_ADDRESS"
DEFAULT_APP_PASSWORD_ENV = "WORKLOGBRIDGE_GMAIL_APP_PASSWORD"
DEFAULT_RECIPIENT_ENV = "WORKLOGBRIDGE_GMAIL_RECIPIENT"
DAILY_MARKDOWN_NAME = re.compile(r"^daily_worklog_(\d{4}-\d{2}-\d{2})\.md$")
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUCCESS = "SUCCESS"
FAILED = "FAILED"
SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
DRY_RUN = "DRY_RUN"


class DeliveryError(ValueError):
    """A safe-to-display delivery validation error."""


@dataclass(frozen=True)
class GmailDeliveryConfig:
    enabled: bool = False
    smtp_server: str = DEFAULT_SMTP_SERVER
    smtp_port: int = DEFAULT_SMTP_PORT
    sender_email_env: str = DEFAULT_SENDER_ENV
    app_password_env: str = DEFAULT_APP_PASSWORD_ENV
    recipient_email_env: str = DEFAULT_RECIPIENT_ENV
    attach_markdown: bool = True
    attach_json: bool = True
    include_markdown_body: bool = True
    max_body_chars: int = 12000
    fail_collection_on_delivery_error: bool = False
    dedupe: bool = True


@dataclass(frozen=True)
class PreparedEmail:
    date: str
    filename: str
    source_hash: str
    subject: str
    body: str
    markdown_attachment: str
    json_attachment: Optional[str]
    body_chars: int
    truncated: bool


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    date: str
    filename: str
    source_hash: str
    metadata_path: Optional[Path] = None
    smtp_response: Optional[str] = None
    sanitized_error: Optional[str] = None
    dedupe_hit: bool = False
    body_chars: int = 0
    truncated: bool = False
    smtp_connection_attempted: bool = False
    credential_source: Optional[str] = None


def parse_gmail_delivery_config(payload: Mapping[str, object]) -> GmailDeliveryConfig:
    """Validate and resolve the optional gmail_delivery config section."""

    raw = payload.get("gmail_delivery")
    if raw is None:
        return GmailDeliveryConfig()
    if not isinstance(raw, Mapping):
        raise DeliveryError("config의 gmail_delivery는 JSON object여야 합니다.")
    allowed_keys = {
        "enabled",
        "smtp_server",
        "smtp_port",
        "sender_email_env",
        "app_password_env",
        "recipient_email_env",
        "attach_markdown",
        "attach_json",
        "include_markdown_body",
        "max_body_chars",
        "fail_collection_on_delivery_error",
        "dedupe",
    }
    unexpected = sorted(str(key) for key in raw if key not in allowed_keys)
    if unexpected:
        raise DeliveryError(
            "gmail_delivery에는 환경 변수 이름과 지원되는 설정만 저장할 수 있습니다: "
            + ", ".join(unexpected)
        )

    def boolean(name: str, default: bool) -> bool:
        value = raw.get(name, default)
        if not isinstance(value, bool):
            raise DeliveryError(f"gmail_delivery.{name}은 boolean이어야 합니다.")
        return value

    smtp_server = raw.get("smtp_server", DEFAULT_SMTP_SERVER)
    if smtp_server != DEFAULT_SMTP_SERVER:
        raise DeliveryError("gmail_delivery.smtp_server는 smtp.gmail.com이어야 합니다.")
    smtp_port = raw.get("smtp_port", DEFAULT_SMTP_PORT)
    if (
        isinstance(smtp_port, bool)
        or not isinstance(smtp_port, int)
        or smtp_port != DEFAULT_SMTP_PORT
    ):
        raise DeliveryError("gmail_delivery.smtp_port는 Gmail SSL port 465여야 합니다.")

    environment_names: dict[str, str] = {}
    defaults = {
        "sender_email_env": DEFAULT_SENDER_ENV,
        "app_password_env": DEFAULT_APP_PASSWORD_ENV,
        "recipient_email_env": DEFAULT_RECIPIENT_ENV,
    }
    for name, default in defaults.items():
        value = raw.get(name, default)
        if not isinstance(value, str) or not ENVIRONMENT_NAME.fullmatch(value):
            raise DeliveryError(f"gmail_delivery.{name}는 유효한 환경 변수 이름이어야 합니다.")
        environment_names[name] = value
    if len(set(environment_names.values())) != len(environment_names):
        raise DeliveryError("Gmail address, App Password, recipient 환경 변수 이름은 서로 달라야 합니다.")

    max_body_chars = raw.get("max_body_chars", 12000)
    if (
        isinstance(max_body_chars, bool)
        or not isinstance(max_body_chars, int)
        or max_body_chars <= 0
    ):
        raise DeliveryError("gmail_delivery.max_body_chars는 양의 정수여야 합니다.")

    return GmailDeliveryConfig(
        enabled=boolean("enabled", False),
        smtp_server=str(smtp_server),
        smtp_port=int(smtp_port),
        sender_email_env=environment_names["sender_email_env"],
        app_password_env=environment_names["app_password_env"],
        recipient_email_env=environment_names["recipient_email_env"],
        attach_markdown=boolean("attach_markdown", True),
        attach_json=boolean("attach_json", True),
        include_markdown_body=boolean("include_markdown_body", True),
        max_body_chars=max_body_chars,
        fail_collection_on_delivery_error=boolean(
            "fail_collection_on_delivery_error", False
        ),
        dedupe=boolean("dedupe", True),
    )


def _configured_path(value: str, config_path: Path) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute():
        expanded = config_path.parent / expanded
    return expanded.resolve()


def load_delivery_runtime_config(
    config_path: Path,
) -> tuple[GmailDeliveryConfig, Path, Path]:
    """Load only the settings required by the standalone delivery CLI."""

    resolved = config_path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeliveryError(f"config 파일이 없습니다: {resolved}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"config 파일을 읽을 수 없습니다: {resolved}") from exc
    if not isinstance(payload, Mapping):
        raise DeliveryError("config의 최상위 값은 JSON object여야 합니다.")
    outbox_value = payload.get("outbox_dir", payload.get("outbox_path"))
    log_value = payload.get("log_dir")
    if not isinstance(outbox_value, str) or not outbox_value.strip():
        raise DeliveryError("config의 outbox_dir 문자열이 필요합니다.")
    if not isinstance(log_value, str) or not log_value.strip():
        raise DeliveryError("config의 log_dir 문자열이 필요합니다.")
    return (
        parse_gmail_delivery_config(payload),
        _configured_path(outbox_value, resolved),
        _configured_path(log_value, resolved),
    )


def _date_from_filename(path: Path) -> str:
    match = DAILY_MARKDOWN_NAME.fullmatch(path.name)
    if not match:
        raise DeliveryError(
            "daily Markdown 파일명은 daily_worklog_YYYY-MM-DD.md 형식이어야 합니다."
        )
    value = match.group(1)
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise DeliveryError(f"daily Markdown 파일명의 날짜가 올바르지 않습니다: {value}") from exc
    return value


def _ensure_inside_outbox(path: Path, outbox_dir: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(outbox_dir.expanduser().resolve())
    except ValueError as exc:
        raise DeliveryError("daily worklog 파일은 configured outbox 내부에 있어야 합니다.") from exc
    return resolved


def resolve_daily_markdown(
    outbox_dir: Path, *, date_value: Optional[str] = None, latest: bool = False
) -> Path:
    """Resolve an exact-date or newest generated daily Markdown file."""

    outbox = outbox_dir.expanduser().resolve()
    if not outbox.is_dir():
        raise DeliveryError(f"outbox 디렉터리가 없습니다: {outbox}")
    if (date_value is None) == (not latest):
        raise DeliveryError("--date 또는 --latest 중 하나만 선택해야 합니다.")
    if date_value is not None:
        try:
            datetime.strptime(date_value, "%Y-%m-%d")
        except ValueError as exc:
            raise DeliveryError("--date는 YYYY-MM-DD 형식이어야 합니다.") from exc
        candidate = outbox / f"daily_worklog_{date_value}.md"
        if not candidate.is_file():
            raise DeliveryError(f"daily Markdown 파일이 없습니다: {candidate.name}")
        resolved = _ensure_inside_outbox(candidate, outbox)
        _date_from_filename(resolved)
        return resolved

    candidates: list[Path] = []
    for candidate in outbox.glob("daily_worklog_*.md"):
        if not candidate.is_file() or not DAILY_MARKDOWN_NAME.fullmatch(candidate.name):
            continue
        try:
            _date_from_filename(candidate)
            candidates.append(_ensure_inside_outbox(candidate, outbox))
        except (DeliveryError, OSError):
            continue
    if not candidates:
        raise DeliveryError("outbox에 daily_worklog_*.md 파일이 없습니다.")
    try:
        return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    except OSError as exc:
        raise DeliveryError("최신 daily Markdown 파일을 확인할 수 없습니다.") from exc


def _remove_review_only_sections(markdown: str) -> str:
    """Remove uncertain details and personal project names from delivery content."""

    output: list[str] = []
    in_uncertain_review = False
    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        if stripped == "### uncertain 검토 목록":
            in_uncertain_review = True
            continue
        if stripped.startswith("- 제외된 개인 작업:"):
            in_uncertain_review = False
            output.append("- 제외된 개인 작업: 상세 내용은 이메일에서 제외됨")
            continue
        if in_uncertain_review:
            if stripped.startswith("## "):
                in_uncertain_review = False
            else:
                continue
        output.append(raw_line)
    return "\n".join(output)


def _safe_multiline(value: str, *, label: str, max_length: int = 12000) -> str:
    output: list[str] = []
    for raw_line in value.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith(("```", "~~~")) or is_sensitive_line(raw_line):
            raise DeliveryError(f"privacy guard가 {label}에서 전송 금지 내용을 감지했습니다.")
        if not stripped:
            output.append("")
            continue
        safe_line = sanitize_line(raw_line, max_length=max_length)
        if safe_line is None:
            raise DeliveryError(f"privacy guard가 {label}의 안전성을 확인하지 못했습니다.")
        output.append(safe_line)
    return "\n".join(output)


def _truncate_markdown(markdown: str, max_body_chars: int) -> tuple[str, bool]:
    if len(markdown) <= max_body_chars:
        return markdown, False
    notes = (
        "\n\n[TRUNCATED by Worklog Bridge: markdown exceeded max_body_chars]",
        "\n\n[TRUNCATED by Worklog Bridge]",
        "[TRUNCATED]",
        "…",
    )
    note = next((value for value in notes if len(value) <= max_body_chars), "")
    return markdown[: max_body_chars - len(note)].rstrip() + note, True


def _read_companion_json(markdown_path: Path, *, required: bool) -> dict[str, object]:
    json_path = markdown_path.with_suffix(".json")
    if not json_path.is_file():
        if required:
            raise DeliveryError(f"daily JSON 파일이 없습니다: {json_path.name}")
        return {}
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"daily JSON 파일을 읽거나 검증할 수 없습니다: {json_path.name}") from exc
    if not isinstance(payload, Mapping):
        raise DeliveryError("daily JSON의 최상위 값은 object여야 합니다.")
    return dict(payload)


def _sanitize_json_value(value: object, *, label: str) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_json_value(child, label=f"{label}.{key}")
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_json_value(child, label=f"{label}[{index}]")
            for index, child in enumerate(value)
        ]
    if isinstance(value, str):
        return _safe_multiline(value, label=label, max_length=2000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise DeliveryError(f"daily JSON의 {label}에 지원하지 않는 값이 있습니다.")


def _safe_json_attachment(payload: Mapping[str, object], date_value: str) -> str:
    if payload.get("mode") != "daily":
        raise DeliveryError("daily JSON의 mode가 daily가 아닙니다.")
    if payload.get("date") != date_value:
        raise DeliveryError("daily JSON 날짜가 Markdown 파일명 날짜와 다릅니다.")
    fields = payload.get("fields")
    if not isinstance(fields, Mapping):
        raise DeliveryError("daily JSON에 fields object가 없습니다.")

    # Keep the Windows/HWP fields and high-level provenance, while deliberately
    # excluding included-item paths, uncertain review details, and all personal
    # stubs from the email attachment.
    allowed = (
        "generated_at",
        "mode",
        "date",
        "week_id",
        "week_range",
        "date_range",
        "included_policy",
        "collection_summary",
        "fields",
        "privacy_exclusions_summary",
        "privacy_note",
    )
    safe = {
        key: _sanitize_json_value(payload[key], label=key)
        for key in allowed
        if key in payload
    }
    return json.dumps(safe, ensure_ascii=False, indent=2) + "\n"


def _summary_values(payload: Mapping[str, object]) -> tuple[str, str]:
    activity = "해당 사항 없음"
    summary = "해당 사항 없음"
    collection = payload.get("collection_summary")
    if isinstance(collection, Mapping):
        raw_activity = collection.get("message") or collection.get("git_activity_status")
        if isinstance(raw_activity, str) and raw_activity.strip():
            activity = _safe_multiline(raw_activity, label="Activity", max_length=1000)
    fields = payload.get("fields")
    if isinstance(fields, Mapping):
        raw_summary = fields.get("SUMMARY")
        if isinstance(raw_summary, str) and raw_summary.strip():
            summary = _safe_multiline(raw_summary, label="Summary", max_length=2000)
    return activity, summary


def _content_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def build_email_payload(
    markdown_path: Path,
    settings: GmailDeliveryConfig,
) -> PreparedEmail:
    """Validate files and create body plus delivery-safe attachment content."""

    source = markdown_path.expanduser().resolve()
    date_value = _date_from_filename(source)
    try:
        original_markdown = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DeliveryError(f"daily Markdown 파일이 없습니다: {source.name}") from exc
    except (OSError, UnicodeError) as exc:
        raise DeliveryError(f"daily Markdown 파일을 읽을 수 없습니다: {source.name}") from exc

    delivery_markdown = _safe_multiline(
        _remove_review_only_sections(original_markdown),
        label="daily Markdown",
        max_length=max(2000, settings.max_body_chars),
    )
    companion = _read_companion_json(source, required=settings.attach_json)
    json_attachment = (
        _safe_json_attachment(companion, date_value) if settings.attach_json else None
    )
    activity, summary = _summary_values(companion)
    bounded_markdown, truncated = _truncate_markdown(
        delivery_markdown, settings.max_body_chars
    )
    if not settings.include_markdown_body:
        bounded_markdown = ""
        truncated = False
    body = (
        "Worklog Bridge Daily Draft\n\n"
        f"Date:\n{date_value}\n\n"
        f"Activity:\n{activity}\n\n"
        f"Summary:\n{summary}"
    )
    if bounded_markdown:
        body += "\n\n" + bounded_markdown
    return PreparedEmail(
        date=date_value,
        filename=source.name,
        source_hash=_content_hash(original_markdown),
        subject=f"[Worklog Bridge] Daily Draft {date_value}",
        body=body,
        markdown_attachment=delivery_markdown,
        json_attachment=json_attachment,
        body_chars=len(bounded_markdown),
        truncated=truncated,
    )


def _valid_email_address(value: str) -> bool:
    if not value or value != value.strip() or "\r" in value or "\n" in value:
        return False
    display_name, parsed = parseaddr(value)
    return not display_name and parsed == value and value.count("@") == 1


def _build_message(
    prepared: PreparedEmail,
    settings: GmailDeliveryConfig,
    sender: str,
    recipient: str,
) -> EmailMessage:
    if not _valid_email_address(sender):
        raise DeliveryError(
            f"Gmail credential {settings.sender_email_env}에 유효한 email address가 필요합니다."
        )
    if not _valid_email_address(recipient):
        raise DeliveryError(
            f"Gmail credential {settings.recipient_email_env}에 유효한 email address가 필요합니다."
        )
    message = EmailMessage()
    message["Subject"] = prepared.subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(prepared.body)
    if settings.attach_markdown:
        message.add_attachment(
            prepared.markdown_attachment,
            subtype="markdown",
            filename=prepared.filename,
        )
    if settings.attach_json and prepared.json_attachment is not None:
        message.add_attachment(
            prepared.json_attachment,
            subtype="json",
            filename=Path(prepared.filename).with_suffix(".json").name,
        )
    return message


def _metadata_path(log_dir: Path, date_value: str) -> Path:
    return log_dir.expanduser().resolve() / f"gmail_delivery_{date_value}.json"


def _load_metadata(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _write_metadata(path: Path, payload: Mapping[str, object]) -> Path:
    if not path.parent.is_dir():
        raise OSError(f"log directory is missing: {path.parent}")
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def _is_duplicate(metadata: Mapping[str, object], source_hash: str) -> bool:
    if metadata.get("last_successful_hash") == source_hash:
        return True
    return metadata.get("hash") == source_hash and metadata.get("status") in {
        SUCCESS,
        SKIPPED_DUPLICATE,
    }


def _base_metadata(prepared: PreparedEmail) -> dict[str, object]:
    return {
        "date": prepared.date,
        "filename": prepared.filename,
        "hash": prepared.source_hash,
        "status": None,
        "smtp_response": None,
        "sanitized_error": None,
        "sent_at": None,
        "dedupe_hit": False,
    }


def _preserve_last_success(
    metadata: dict[str, object], previous: Mapping[str, object]
) -> None:
    if previous.get("last_successful_hash"):
        metadata["last_successful_hash"] = previous["last_successful_hash"]
        metadata["last_successful_sent_at"] = previous.get("last_successful_sent_at")


def _safe_smtp_error(error: BaseException) -> str:
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "Gmail SMTP authentication failed. Verify the sender and Google App Password."
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "Gmail SMTP rejected the recipient address."
    if isinstance(error, smtplib.SMTPSenderRefused):
        return "Gmail SMTP rejected the sender address."
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "Gmail SMTP connection timed out."
    if isinstance(error, (smtplib.SMTPException, OSError, ssl.SSLError)):
        return "Gmail SMTP delivery failed."
    return "Gmail delivery failed."


def _failure_result(
    prepared: PreparedEmail,
    metadata_path: Path,
    previous: Mapping[str, object],
    error: str,
    *,
    smtp_attempted: bool,
    credential_source: Optional[str] = None,
) -> DeliveryResult:
    metadata = _base_metadata(prepared)
    metadata.update(
        {
            "status": FAILED,
            "sanitized_error": error,
            "attempted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "credential_source": credential_source,
        }
    )
    _preserve_last_success(metadata, previous)
    try:
        written_path: Optional[Path] = _write_metadata(metadata_path, metadata)
    except OSError:
        written_path = None
    return DeliveryResult(
        status=FAILED,
        date=prepared.date,
        filename=prepared.filename,
        source_hash=prepared.source_hash,
        metadata_path=written_path,
        sanitized_error=error,
        body_chars=prepared.body_chars,
        truncated=prepared.truncated,
        smtp_connection_attempted=smtp_attempted,
        credential_source=credential_source,
    )


def deliver_daily_worklog(
    markdown_path: Path,
    settings: GmailDeliveryConfig,
    log_dir: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    smtp_factory: Optional[Callable[..., object]] = None,
) -> DeliveryResult:
    """Validate, dedupe, and optionally send one generated daily worklog."""

    prepared = build_email_payload(markdown_path, settings)
    metadata_path = _metadata_path(log_dir, prepared.date)
    previous = _load_metadata(metadata_path)
    duplicate = settings.dedupe and not force and _is_duplicate(
        previous, prepared.source_hash
    )

    if dry_run:
        return DeliveryResult(
            status=DRY_RUN,
            date=prepared.date,
            filename=prepared.filename,
            source_hash=prepared.source_hash,
            dedupe_hit=duplicate,
            body_chars=prepared.body_chars,
            truncated=prepared.truncated,
        )

    if duplicate:
        sent_at = previous.get("last_successful_sent_at", previous.get("sent_at"))
        metadata = _base_metadata(prepared)
        metadata.update(
            {
                "status": SKIPPED_DUPLICATE,
                "smtp_response": previous.get("smtp_response"),
                "sent_at": sent_at,
                "dedupe_hit": True,
                "last_successful_hash": prepared.source_hash,
                "last_successful_sent_at": sent_at,
            }
        )
        try:
            written_path: Optional[Path] = _write_metadata(metadata_path, metadata)
        except OSError:
            written_path = None
        return DeliveryResult(
            status=SKIPPED_DUPLICATE,
            date=prepared.date,
            filename=prepared.filename,
            source_hash=prepared.source_hash,
            metadata_path=written_path,
            smtp_response=(
                str(previous["smtp_response"])
                if previous.get("smtp_response") is not None
                else None
            ),
            dedupe_hit=True,
            body_chars=prepared.body_chars,
            truncated=prepared.truncated,
        )
    try:
        credentials = resolve_gmail_credentials(
            settings.sender_email_env,
            settings.app_password_env,
            settings.recipient_email_env,
        )
    except CredentialResolutionError as exc:
        return _failure_result(
            prepared, metadata_path, previous, str(exc), smtp_attempted=False
        )
    sender = credentials.sender
    app_password = credentials.app_password
    recipient = credentials.recipient
    if "\r" in app_password or "\n" in app_password:
        return _failure_result(
            prepared,
            metadata_path,
            previous,
            f"Gmail credential {settings.app_password_env}에 유효한 Google App Password가 필요합니다.",
            smtp_attempted=False,
            credential_source=credentials.source,
        )
    try:
        message = _build_message(prepared, settings, sender, recipient)
    except DeliveryError as exc:
        return _failure_result(
            prepared,
            metadata_path,
            previous,
            str(exc),
            smtp_attempted=False,
            credential_source=credentials.source,
        )

    factory = smtp_factory or smtplib.SMTP_SSL
    try:
        with factory(
            settings.smtp_server,
            settings.smtp_port,
            timeout=20,
            context=ssl.create_default_context(),
        ) as smtp:
            login = getattr(smtp, "login")
            send_message = getattr(smtp, "send_message")
            login(sender, app_password)
            refused = send_message(message)
            if isinstance(refused, Mapping) and refused:
                raise smtplib.SMTPRecipientsRefused(dict(refused))
    except Exception as exc:
        return _failure_result(
            prepared,
            metadata_path,
            previous,
            _safe_smtp_error(exc),
            smtp_attempted=True,
            credential_source=credentials.source,
        )
    finally:
        app_password = ""

    sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
    smtp_response = "accepted"
    metadata = _base_metadata(prepared)
    metadata.update(
        {
            "status": SUCCESS,
            "smtp_response": smtp_response,
            "sent_at": sent_at,
            "last_successful_hash": prepared.source_hash,
            "last_successful_sent_at": sent_at,
            "credential_source": credentials.source,
        }
    )
    try:
        written_path = _write_metadata(metadata_path, metadata)
    except OSError:
        return DeliveryResult(
            status=FAILED,
            date=prepared.date,
            filename=prepared.filename,
            source_hash=prepared.source_hash,
            smtp_response=smtp_response,
            sanitized_error="Email was accepted, but delivery metadata could not be written.",
            body_chars=prepared.body_chars,
            truncated=prepared.truncated,
            smtp_connection_attempted=True,
            credential_source=credentials.source,
        )
    return DeliveryResult(
        status=SUCCESS,
        date=prepared.date,
        filename=prepared.filename,
        source_hash=prepared.source_hash,
        metadata_path=written_path,
        smtp_response=smtp_response,
        body_chars=prepared.body_chars,
        truncated=prepared.truncated,
        smtp_connection_attempted=True,
        credential_source=credentials.source,
    )
