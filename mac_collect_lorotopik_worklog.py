#!/usr/bin/env python3
"""Collect a privacy-minimized LoroTopik worklog draft on macOS.

Git collection is metadata-only: commit subject, changed paths, and shortstat.
No diff or source-file content command is used.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import plistlib
import shlex
import subprocess
import sys
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, Sequence

from privacy_guard import is_sensitive_line, sanitize_line
from worklog_classifier import (
    COMPANY_WORK,
    PERSONAL_WORK,
    classify_with_reasons,
)
from worklog_draft_generator import generate_worklog, render_markdown, write_worklog_files
from gmail_delivery import (
    FAILED as GMAIL_DELIVERY_FAILED,
    GmailDeliveryConfig,
    deliver_daily_worklog,
    parse_gmail_delivery_config,
)
from gmail_credentials import (
    CredentialResolutionError,
    resolve_gmail_credentials,
    setup_macos_keychain,
)


DEFAULT_OUT_DIR = Path("~/Documents/WorklogBridge/outbox").expanduser()
DEFAULT_LOG_DIR = Path("~/Documents/WorklogBridge/logs").expanduser()
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.local.json"
LAUNCHD_LABEL = "com.worklogbridge.lorolog.daily"
INSTALLED_PLIST = Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()
ALLOWED_NOTE_SUFFIXES = {".md", ".txt"}
ALLOWED_PLAN_SUFFIXES = {".md", ".txt", ".json"}
COMPLETION_MESSAGE = "오늘의 LoroTopik 근무일지 초안 생성 완료. 검토 후 Windows PC로 전달하세요."
DEFAULT_PRIVACY_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".env", ".env.*", "**/.env", "**/.env.*", "**/secrets/**",
    "**/private/**", "**/personal/**", "*.pem", "*.key", "*.p12", "*.pfx",
)
FORBIDDEN_CONFIG_KEYS = {
    "token", "api_key", "access_key", "access_token", "refresh_token", "password", "passwd",
    "secret", "client_secret", "private_key", "webhook_url",
}


@dataclass
class CollectionStats:
    privacy_exclusions: int = 0

    def excluded(self, count: int = 1) -> None:
        self.privacy_exclusions += count


@dataclass
class CollectorConfig:
    config_path: Path = DEFAULT_CONFIG_PATH
    config_exists: bool = False
    config_error: Optional[str] = None
    repos: list[Path] = field(default_factory=list)
    notes_dir: Optional[Path] = None
    plan_file: Optional[Path] = None
    notes_enabled: bool = False
    plan_enabled: bool = False
    out_dir: Path = DEFAULT_OUT_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    privacy_exclude_patterns: tuple[str, ...] = DEFAULT_PRIVACY_EXCLUDE_PATTERNS
    company_keyword_hints: tuple[str, ...] = ()
    personal_exclude_hints: tuple[str, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)
    launchd_plist: Path = INSTALLED_PLIST
    gmail_delivery: GmailDeliveryConfig = field(default_factory=GmailDeliveryConfig)


def _parse_temporal(value: str, *, end_of_day: bool = False) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"날짜 형식이 올바르지 않습니다: {value!r} (YYYY-MM-DD 또는 ISO datetime 사용)"
        ) from exc
    if "T" not in value and " " not in value:
        parsed = datetime.combine(parsed.date(), time.max if end_of_day else time.min)
    return parsed


def resolve_period(
    mode: str, since: Optional[str], until: Optional[str], *, today: Optional[date] = None
) -> tuple[datetime, datetime]:
    reference = today or date.today()
    if mode == "weekly":
        default_start = datetime.combine(reference - timedelta(days=reference.weekday()), time.min)
    else:
        default_start = datetime.combine(reference, time.min)
    default_end = datetime.combine(reference, time.max)
    start = _parse_temporal(since) if since else default_start
    end = _parse_temporal(until, end_of_day=True) if until else default_end
    if start > end:
        raise ValueError("--since는 --until보다 늦을 수 없습니다.")
    return start, end


def _run_git(repo: Path, arguments: Sequence[str]) -> str:
    command = ["git", "-C", str(repo), *arguments]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()
        detail = message[-1] if message else f"exit code {completed.returncode}"
        raise RuntimeError(f"Git 메타데이터 수집 실패 ({repo}): {detail}")
    return completed.stdout


def _matches_privacy_exclude(value: str | Path, patterns: Iterable[str]) -> bool:
    normalized = str(value).replace("\\", "/")
    # macOS temp/runtime paths resolve under /private/tmp or /private/var; those
    # system prefixes are not user folders named "private".
    is_macos_system_private = normalized.startswith(("/private/tmp/", "/private/var/"))
    full_path = "" if is_macos_system_private else normalized
    candidates = (full_path, normalized.lstrip("/"), Path(normalized).name)
    return any(fnmatch.fnmatch(candidate.casefold(), pattern.casefold()) for pattern in patterns for candidate in candidates)


def _safe_path(
    value: str, privacy_exclude_patterns: Iterable[str] = DEFAULT_PRIVACY_EXCLUDE_PATTERNS
) -> Optional[str]:
    normalized = value.strip().replace("\\", "/")
    if not normalized or is_sensitive_line(normalized):
        return None
    if any(part.casefold().startswith(".env") for part in normalized.split("/")):
        return None
    if _matches_privacy_exclude(normalized, privacy_exclude_patterns):
        return None
    return sanitize_line(normalized, max_length=300)


def collect_git_repo(
    repo: Path,
    since: datetime,
    until: datetime,
    *,
    stats: Optional[CollectionStats] = None,
    privacy_exclude_patterns: Iterable[str] = DEFAULT_PRIVACY_EXCLUDE_PATTERNS,
) -> list[dict[str, object]]:
    """Collect Git metadata without invoking any patch/diff-content command."""

    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"Git 저장소 경로를 찾을 수 없습니다: {repo}")
    if not (repo / ".git").exists():
        # Worktrees may use a .git file, which exists() still accepts.
        raise ValueError(f"Git 저장소가 아닙니다: {repo}")
    raw = _run_git(
        repo,
        [
            "log",
            f"--since={since.isoformat(timespec='seconds')}",
            f"--until={until.isoformat(timespec='seconds')}",
            "--date=iso-strict",
            "--format=%x1e%H%x1f%h%x1f%cI%x1f%s",
        ],
    )
    items: list[dict[str, object]] = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x1f", maxsplit=3)
        if len(parts) != 4:
            continue
        full_sha, short_sha, committed_at, subject = parts
        safe_subject = sanitize_line(subject, max_length=300)
        if not safe_subject:
            if stats and subject.strip():
                stats.excluded()
            continue
        changed_raw = _run_git(
            repo, ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", full_sha]
        )
        changed_files: list[str] = []
        for value in changed_raw.splitlines():
            safe = _safe_path(value, privacy_exclude_patterns)
            if safe is not None:
                changed_files.append(safe)
            elif stats and value.strip():
                stats.excluded()
            if len(changed_files) >= 100:
                break
        shortstat_lines = _run_git(repo, ["show", "--shortstat", "--format=", full_sha]).splitlines()
        shortstat = next(
            (safe for value in reversed(shortstat_lines) if (safe := sanitize_line(value, max_length=200))),
            "",
        )
        item: dict[str, object] = {
            "source_type": "git",
            "date": committed_at,
            "title": safe_subject,
            "summary": safe_subject,
            "commit_sha": short_sha,
            "changed_files": changed_files,
            "shortstat": shortstat,
            "repo_path": str(repo),
            "repo_name": repo.name,
        }
        items.append(item)
    return items


def _within_mtime(path: Path, since: datetime, until: datetime) -> bool:
    modified = path.stat().st_mtime
    return since.timestamp() <= modified <= until.timestamp()


def _frontmatter(
    lines: list[str], stats: Optional[CollectionStats] = None
) -> tuple[dict[str, str], int]:
    if not lines or lines[0].strip() != "---":
        return {}, 0
    metadata: dict[str, str] = {}
    for index, raw in enumerate(lines[1:31], start=1):
        if raw.strip() == "---":
            return metadata, index + 1
        if ":" not in raw:
            continue
        key, value = raw.split(":", maxsplit=1)
        key = key.strip().casefold()
        safe = sanitize_line(value, max_length=200)
        if key in {"title", "tags", "tag", "date", "project", "category"} and safe:
            metadata[key] = safe
        elif key in {"title", "tags", "tag", "date", "project", "category"} and value.strip() and stats:
            stats.excluded()
    return metadata, 0


def _candidate_lines(
    path: Path, *, source_type: str, stats: Optional[CollectionStats] = None
) -> Iterator[tuple[str, list[str], dict[str, str]]]:
    """Yield bounded heading/bullet records rather than storing a whole file."""

    if path.suffix.casefold() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"계획 JSON을 읽을 수 없습니다 ({path}): {exc}") from exc

        def walk(value: object, key: str = "") -> Iterator[str]:
            if isinstance(value, Mapping):
                for child_key, child in list(value.items())[:100]:
                    yield from walk(child, str(child_key))
            elif isinstance(value, list):
                for child in value[:100]:
                    yield from walk(child, key)
            elif isinstance(value, (str, int, float, bool)):
                yield f"{key}: {value}" if key else str(value)

        for raw in walk(payload):
            safe = sanitize_line(raw, max_length=400)
            if safe:
                yield safe, [], {}
            elif stats and raw.strip():
                stats.excluded()
        return

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ValueError(f"메모 파일을 읽을 수 없습니다 ({path}): {exc}") from exc
    metadata, body_start = _frontmatter(lines, stats)
    tags = [
        value.strip(" []\"'")
        for value in (metadata.get("tags") or metadata.get("tag") or "").split(",")
        if value.strip(" []\"'")
    ][:20]
    yielded = 0
    in_code_fence = False
    in_diff = False
    for raw in lines[body_start:]:
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            in_code_fence = not in_code_fence
            continue
        if not stripped:
            continue
        if in_code_fence:
            if stats:
                stats.excluded()
            continue
        if stripped.startswith("diff --git") or stripped.startswith("@@ -"):
            in_diff = True
            if stats:
                stats.excluded()
            continue
        if in_diff:
            if stats:
                stats.excluded()
            continue
        is_structured = stripped.startswith(("#", "-", "*", "+", "•"))
        if not is_structured:
            # A short plain plan sentence is useful; code-like/file-body lines are not.
            if len(stripped) > 300 or any(token in stripped for token in ("{", "};", "=>", "import ", "def ")):
                if stats:
                    stats.excluded()
                continue
        safe = sanitize_line(stripped.lstrip("#-*+• "), max_length=400)
        if not safe:
            if stats and stripped.lstrip("#-*+• ").strip():
                stats.excluded()
            continue
        # Do not use a company-looking filename/frontmatter to promote an
        # otherwise ambiguous sentence. Metadata is retained for review, while
        # classification stays line-local and conservative.
        yield safe, tags, metadata
        yielded += 1
        if yielded >= 100:
            break


def collect_text_file(
    path: Path,
    since: datetime,
    until: datetime,
    *,
    source_type: str,
    stats: Optional[CollectionStats] = None,
    privacy_exclude_patterns: Iterable[str] = DEFAULT_PRIVACY_EXCLUDE_PATTERNS,
) -> list[dict[str, object]]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")
    if _matches_privacy_exclude(path, privacy_exclude_patterns):
        if stats:
            stats.excluded()
        return []
    if not _within_mtime(path, since, until):
        return []
    modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    items: list[dict[str, object]] = []
    for text, tags, metadata in _candidate_lines(path, source_type=source_type, stats=stats):
        items.append(
            {
                "source_type": source_type,
                "date": modified,
                "title": metadata.get("title") or path.stem,
                "summary": text,
                "source_file": path.name,
                "source_path": str(path),
                "tags": tags,
                "frontmatter": metadata,
            }
        )
    return items


def collect_notes(
    directory: Path,
    since: datetime,
    until: datetime,
    *,
    stats: Optional[CollectionStats] = None,
    privacy_exclude_patterns: Iterable[str] = DEFAULT_PRIVACY_EXCLUDE_PATTERNS,
) -> list[dict[str, object]]:
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"메모 디렉터리를 찾을 수 없습니다: {directory}")
    items: list[dict[str, object]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.casefold() in ALLOWED_NOTE_SUFFIXES:
            items.extend(
                collect_text_file(
                    path,
                    since,
                    until,
                    source_type="note",
                    stats=stats,
                    privacy_exclude_patterns=privacy_exclude_patterns,
                )
            )
    return items


def collect_comment(
    value: str, target_date: date, *, stats: Optional[CollectionStats] = None
) -> Optional[dict[str, object]]:
    safe = sanitize_line(value, max_length=500)
    if not safe:
        if stats and value.strip():
            stats.excluded()
        return None
    return {
        "source_type": "comment",
        "date": target_date.isoformat(),
        "title": "수동 코멘트",
        "summary": safe,
    }


def classify_collected(
    items: Iterable[dict[str, object]],
    *,
    company_keyword_hints: Iterable[str] = (),
    personal_keyword_hints: Iterable[str] = (),
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    included: list[dict[str, object]] = []
    personal: list[dict[str, object]] = []
    uncertain: list[dict[str, object]] = []
    for item in items:
        if item.get("source_type") == "git":
            text = " ".join(
                str(item.get(key, ""))
                for key in ("title", "summary", "repo_name", "changed_files")
            )
            path = str(item.get("repo_path") or "")
        else:
            text = str(item.get("summary", ""))
            path = ""
        result = classify_with_reasons(
            text,
            path or None,
            company_keyword_hints=company_keyword_hints,
            personal_keyword_hints=personal_keyword_hints,
        )
        item["classification"] = result.category
        item["classification_reasons"] = result.reasons
        if result.category == COMPANY_WORK:
            included.append(item)
        elif result.category == PERSONAL_WORK:
            personal.append(item)
        else:
            uncertain.append(item)
    return included, personal, uncertain


def collect_configured_sources(
    config: CollectorConfig,
    since: datetime,
    until: datetime,
    *,
    comments: Iterable[str] = (),
    stats: Optional[CollectionStats] = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """Collect configured sources without reading personal-repository commits."""

    stats = stats or CollectionStats()
    items: list[dict[str, object]] = []
    warnings: list[str] = []
    for repo in config.repos:
        repo_classification = classify_with_reasons(
            repo.name,
            str(repo),
            company_keyword_hints=config.company_keyword_hints,
            personal_keyword_hints=config.personal_exclude_hints,
        )
        if repo_classification.category == PERSONAL_WORK:
            items.append(
                {
                    "source_type": "repository",
                    "repo_name": repo.name,
                    "repo_path": str(repo),
                    "summary": repo.name,
                    "classification": PERSONAL_WORK,
                    "classification_reasons": repo_classification.reasons,
                }
            )
            warnings.append(f"개인 repo는 commit metadata도 수집하지 않았습니다: {repo}")
            continue
        try:
            items.extend(
                collect_git_repo(
                    repo,
                    since,
                    until,
                    stats=stats,
                    privacy_exclude_patterns=config.privacy_exclude_patterns,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            warnings.append(str(exc))
    if config.notes_enabled and config.notes_dir:
        try:
            items.extend(
                collect_notes(
                    config.notes_dir,
                    since,
                    until,
                    stats=stats,
                    privacy_exclude_patterns=config.privacy_exclude_patterns,
                )
            )
        except (OSError, ValueError) as exc:
            warnings.append(str(exc))
    if config.plan_enabled and config.plan_file:
        if config.plan_file.suffix.casefold() not in ALLOWED_PLAN_SUFFIXES:
            warnings.append(f"지원하지 않는 계획 파일 형식입니다: {config.plan_file}")
        else:
            try:
                items.extend(
                    collect_text_file(
                        config.plan_file,
                        since,
                        until,
                        source_type="plan",
                        stats=stats,
                        privacy_exclude_patterns=config.privacy_exclude_patterns,
                    )
                )
            except (OSError, ValueError) as exc:
                warnings.append(str(exc))
    for raw_comment in comments:
        item = collect_comment(raw_comment, until.date(), stats=stats)
        if item:
            items.append(item)
        else:
            warnings.append("민감정보가 감지된 --comment 한 건을 제외했습니다.")
    return items, warnings


def _paths_from_value(value: str) -> list[Path]:
    return [Path(entry).expanduser() for entry in value.split(os.pathsep) if entry]


def _path_from_config(value: str, config_path: Path) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute():
        expanded = config_path.parent / expanded
    return expanded.resolve()


def _string_list(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError(f"config의 {key}는 문자열 배열이어야 합니다.")
    return tuple(entry.strip() for entry in value if entry.strip())


def load_operator_config(config_path: Path) -> dict[str, object]:
    """Load a local config while rejecting credential-like keys."""

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"로컬 config 파일이 없습니다: {config_path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"로컬 config를 읽을 수 없습니다 ({config_path}): {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("로컬 config의 최상위 값은 JSON object여야 합니다.")

    def reject_forbidden_keys(value: object, location: str = "config") -> None:
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                key = str(raw_key).casefold()
                if key in FORBIDDEN_CONFIG_KEYS:
                    raise ValueError(f"로컬 config에 비밀정보 키를 둘 수 없습니다: {location}.{raw_key}")
                reject_forbidden_keys(child, f"{location}.{raw_key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                reject_forbidden_keys(child, f"{location}[{index}]")

    reject_forbidden_keys(payload)
    if payload.get("config_version") != 1:
        raise ValueError("config_version은 1이어야 합니다.")
    if payload.get("timezone") != "Asia/Seoul":
        raise ValueError("config의 timezone은 Asia/Seoul이어야 합니다.")
    repo_paths = payload.get("repo_paths")
    if not isinstance(repo_paths, list) or not all(isinstance(entry, str) for entry in repo_paths):
        raise ValueError("config의 repo_paths는 문자열 배열이어야 합니다.")
    for section_name in ("notes", "plan"):
        section = payload.get(section_name)
        if not isinstance(section, Mapping) or not isinstance(section.get("enabled"), bool):
            raise ValueError(f"config의 {section_name}는 enabled boolean을 포함한 object여야 합니다.")
        if section.get("enabled") and not isinstance(section.get("path"), str):
            raise ValueError(f"config의 {section_name}.path가 필요합니다.")
    configured_outbox = payload.get("outbox_dir", payload.get("outbox_path"))
    if not isinstance(configured_outbox, str) or not configured_outbox.strip():
        raise ValueError("config의 outbox_dir 문자열이 필요합니다.")
    if not isinstance(payload.get("log_dir"), str) or not str(payload["log_dir"]).strip():
        raise ValueError("config의 log_dir 문자열이 필요합니다.")
    _string_list(payload, "privacy_exclude_patterns")
    company_hints = _string_list(payload, "company_keyword_hints")
    personal_hints = _string_list(payload, "personal_exclude_hints")
    for hint in company_hints + personal_hints:
        if sanitize_line(hint, max_length=100) is None:
            raise ValueError("config keyword hint에 민감정보 패턴을 사용할 수 없습니다.")
    parse_gmail_delivery_config(payload)
    return payload


def _read_launchd_plist(path: Path = INSTALLED_PLIST) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return {}
    return payload if isinstance(payload, dict) else {}


def _plist_out_dir(payload: Mapping[str, object]) -> Optional[Path]:
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list):
        return None
    values = [str(value) for value in arguments]
    try:
        index = values.index("--out-dir")
        return Path(values[index + 1]).expanduser()
    except (ValueError, IndexError):
        return None


def resolve_collector_config(args: argparse.Namespace) -> CollectorConfig:
    """Resolve CLI plus safe local config, retaining legacy environment support."""

    plist_payload = _read_launchd_plist()
    raw_plist_env = plist_payload.get("EnvironmentVariables", {})
    plist_env = raw_plist_env if isinstance(raw_plist_env, Mapping) else {}
    sources: dict[str, str] = {}
    configured_path = (
        getattr(args, "config", None)
        or (Path(os.environ["WORKLOGBRIDGE_CONFIG"]) if os.environ.get("WORKLOGBRIDGE_CONFIG") else None)
        or (Path(str(plist_env["WORKLOGBRIDGE_CONFIG"])) if plist_env.get("WORKLOGBRIDGE_CONFIG") else None)
        or DEFAULT_CONFIG_PATH
    ).expanduser().resolve()
    config_payload: dict[str, object] = {}
    config_error: Optional[str] = None
    if configured_path.is_file():
        try:
            config_payload = load_operator_config(configured_path)
        except ValueError as exc:
            config_error = str(exc)
    else:
        config_error = f"로컬 config 파일이 없습니다: {configured_path}"
    sources["config"] = "loaded" if config_payload else "missing_or_invalid"

    config_repos = [
        _path_from_config(value, configured_path)
        for value in config_payload.get("repo_paths", [])
        if isinstance(value, str) and value.strip()
    ]

    if getattr(args, "repo", None):
        repos = list(args.repo)
        sources["repos"] = "cli"
    elif config_repos:
        repos = config_repos
        sources["repos"] = "config"
    elif os.environ.get("LOROLOG_REPOS"):
        repos = _paths_from_value(os.environ["LOROLOG_REPOS"])
        sources["repos"] = "environment"
    else:
        repos = _paths_from_value(str(plist_env.get("LOROLOG_REPOS", "")))
        sources["repos"] = "launchd_plist" if repos else "not_configured"

    def legacy_single_path(argument: Optional[Path], environment_name: str) -> tuple[Optional[Path], str]:
        if argument is not None:
            return argument, "cli"
        if os.environ.get(environment_name):
            return Path(os.environ[environment_name]).expanduser(), "environment"
        plist_value = str(plist_env.get(environment_name, ""))
        return (Path(plist_value).expanduser(), "launchd_plist") if plist_value else (None, "not_configured")

    raw_notes = config_payload.get("notes", {})
    notes_config = raw_notes if isinstance(raw_notes, Mapping) else {}
    notes_enabled = bool(notes_config.get("enabled", False))
    if getattr(args, "notes_dir", None) is not None:
        notes_dir, sources["notes_dir"] = args.notes_dir, "cli"
        notes_enabled = True
    elif notes_enabled and isinstance(notes_config.get("path"), str):
        notes_dir = _path_from_config(str(notes_config["path"]), configured_path)
        sources["notes_dir"] = "config"
    elif config_payload:
        notes_dir, sources["notes_dir"] = None, "config_disabled"
    else:
        notes_dir, sources["notes_dir"] = legacy_single_path(None, "LOROLOG_NOTES_DIR")
        notes_enabled = notes_dir is not None

    raw_plan = config_payload.get("plan", {})
    plan_config = raw_plan if isinstance(raw_plan, Mapping) else {}
    plan_enabled = bool(plan_config.get("enabled", False))
    if getattr(args, "plan_file", None) is not None:
        plan_file, sources["plan_file"] = args.plan_file, "cli"
        plan_enabled = True
    elif plan_enabled and isinstance(plan_config.get("path"), str):
        plan_file = _path_from_config(str(plan_config["path"]), configured_path)
        sources["plan_file"] = "config"
    elif config_payload:
        plan_file, sources["plan_file"] = None, "config_disabled"
    else:
        plan_file, sources["plan_file"] = legacy_single_path(None, "LOROLOG_PLAN_FILE")
        plan_enabled = plan_file is not None

    configured_out_dir = config_payload.get("outbox_dir", config_payload.get("outbox_path"))
    if getattr(args, "out_dir", None) is not None:
        out_dir = args.out_dir
        sources["out_dir"] = "cli"
        if isinstance(configured_out_dir, str):
            config_out_dir = _path_from_config(configured_out_dir, configured_path)
            if out_dir.expanduser().resolve() != config_out_dir:
                config_error = (
                    "--out-dir는 로컬 config의 outbox_dir와 같아야 합니다. "
                    "실제 출력 경로는 config에서 변경하세요."
                )
    elif isinstance(configured_out_dir, str):
        out_dir = _path_from_config(configured_out_dir, configured_path)
        sources["out_dir"] = "config"
    elif (plist_out_dir := _plist_out_dir(plist_payload)) is not None:
        out_dir = plist_out_dir
        sources["out_dir"] = "launchd_plist"
    else:
        out_dir = DEFAULT_OUT_DIR
        sources["out_dir"] = "default"
    configured_log_dir = config_payload.get("log_dir")
    log_dir = (
        _path_from_config(configured_log_dir, configured_path)
        if isinstance(configured_log_dir, str)
        else DEFAULT_LOG_DIR
    )
    privacy_patterns = tuple(dict.fromkeys(
        DEFAULT_PRIVACY_EXCLUDE_PATTERNS + _string_list(config_payload, "privacy_exclude_patterns")
    )) if config_payload else DEFAULT_PRIVACY_EXCLUDE_PATTERNS
    company_hints = _string_list(config_payload, "company_keyword_hints") if config_payload else ()
    personal_hints = _string_list(config_payload, "personal_exclude_hints") if config_payload else ()
    gmail_delivery = (
        parse_gmail_delivery_config(config_payload)
        if config_payload
        else GmailDeliveryConfig()
    )
    return CollectorConfig(
        config_path=configured_path,
        config_exists=configured_path.is_file(),
        config_error=config_error,
        repos=[path.expanduser().resolve() for path in repos],
        notes_dir=notes_dir.expanduser().resolve() if notes_dir else None,
        plan_file=plan_file.expanduser().resolve() if plan_file else None,
        notes_enabled=notes_enabled,
        plan_enabled=plan_enabled,
        out_dir=out_dir.expanduser().resolve(),
        log_dir=log_dir.expanduser().resolve(),
        privacy_exclude_patterns=privacy_patterns,
        company_keyword_hints=company_hints,
        personal_exclude_hints=personal_hints,
        sources=sources,
        gmail_delivery=gmail_delivery,
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_output_directory(
    out_dir: Path,
    repo_paths: Iterable[Path],
    *,
    require_exists: bool,
) -> list[str]:
    """Reject source trees and credential-oriented directories as outputs."""

    resolved = out_dir.expanduser().resolve()
    errors: list[str] = []
    forbidden_roots = [PROJECT_ROOT, *(path.expanduser().resolve() for path in repo_paths)]
    if resolved in {Path("/"), Path.home().resolve()}:
        errors.append("outbox는 filesystem root나 HOME 자체일 수 없습니다.")
    for root in forbidden_roots:
        if _is_within(resolved, root):
            errors.append(f"outbox는 source repo 내부일 수 없습니다: {root}")
            break
    sensitive_parts = {".git", ".ssh", ".aws", ".gnupg", "secrets", "credentials"}
    if any(part.casefold() in sensitive_parts for part in resolved.parts):
        errors.append("outbox는 credential/secrets 관련 경로에 둘 수 없습니다.")
    if require_exists:
        if not resolved.is_dir():
            errors.append(f"outbox 디렉터리가 없습니다: {resolved}")
        elif not os.access(resolved, os.W_OK):
            errors.append(f"outbox 디렉터리에 쓸 수 없습니다: {resolved}")
    return errors


def validate_log_directory(
    log_dir: Path,
    repo_paths: Iterable[Path] = (),
    *,
    require_exists: bool,
) -> list[str]:
    resolved = log_dir.expanduser().resolve()
    errors: list[str] = []
    if resolved in {Path("/"), Path.home().resolve()}:
        errors.append("log directory는 filesystem root나 HOME 자체일 수 없습니다.")
    if any(
        _is_within(resolved, root)
        for root in (PROJECT_ROOT, *(path.expanduser().resolve() for path in repo_paths))
    ):
        errors.append("log directory는 source repo 내부일 수 없습니다.")
    sensitive_parts = {".git", ".ssh", ".aws", ".gnupg", "secrets", "credentials"}
    if any(part.casefold() in sensitive_parts for part in resolved.parts):
        errors.append("log directory는 credential/secrets 관련 경로에 둘 수 없습니다.")
    if require_exists:
        if not resolved.is_dir():
            errors.append(f"log 디렉터리가 없습니다: {resolved}")
        elif not os.access(resolved, os.W_OK):
            errors.append(f"log 디렉터리에 쓸 수 없습니다: {resolved}")
    return errors


def _schedule_is_weekdays_at_17(payload: Mapping[str, object]) -> bool:
    intervals = payload.get("StartCalendarInterval")
    if not isinstance(intervals, list):
        return False
    valid_weekdays = {
        int(entry.get("Weekday"))
        for entry in intervals
        if isinstance(entry, Mapping)
        and entry.get("Hour") == 17
        and entry.get("Minute") == 0
        and entry.get("Weekday") is not None
    }
    return valid_weekdays == {1, 2, 3, 4, 5}


def _launchd_is_registered() -> bool:
    if sys.platform != "darwin":
        return False
    completed = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _system_timezone_name() -> str:
    try:
        target = Path("/etc/localtime").resolve()
        marker = "/zoneinfo/"
        if marker in str(target):
            return str(target).split(marker, 1)[1]
    except OSError:
        pass
    return "unknown"


def _installed_plist_matches_config(
    payload: Mapping[str, object], config: CollectorConfig
) -> bool:
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list):
        return False
    values = [str(value) for value in arguments]
    try:
        config_argument = Path(values[values.index("--config") + 1]).expanduser().resolve()
    except (ValueError, IndexError):
        return False
    stdout_path = Path(str(payload.get("StandardOutPath", ""))).expanduser()
    stderr_path = Path(str(payload.get("StandardErrorPath", ""))).expanduser()
    return (
        config_argument == config.config_path
        and stdout_path.parent.resolve() == config.log_dir
        and stderr_path.parent.resolve() == config.log_dir
    )


def print_diagnostics(config: CollectorConfig, *, preflight_only: bool = False) -> int:
    """Run read-only operational checks with 0/1/2 readiness exit codes."""

    checks: list[tuple[str, str]] = []
    blocked = False
    warning = False

    def add(level: str, message: str) -> None:
        nonlocal blocked, warning
        checks.append((level, message))
        blocked = blocked or level == "BLOCKED"
        warning = warning or level == "WARN"

    if sys.version_info >= (3, 10):
        add("OK", f"Python: {sys.version.split()[0]}")
    else:
        add("BLOCKED", f"Python 3.10+가 필요합니다: {sys.version.split()[0]}")

    if config.config_exists and not config.config_error:
        add("OK", f"config: {config.config_path}")
    else:
        add("BLOCKED", config.config_error or f"config가 없습니다: {config.config_path}")

    if not config.repos:
        add("BLOCKED", "repo_paths가 비어 있습니다.")
    for repo in config.repos:
        if not repo.is_dir():
            add("BLOCKED", f"repo 경로가 없습니다: {repo}")
            continue
        if not (repo / ".git").exists():
            add("BLOCKED", f"Git repo가 아닙니다: {repo}")
            continue
        if _matches_privacy_exclude(repo, config.privacy_exclude_patterns):
            add("BLOCKED", f"privacy exclude pattern에 해당하는 repo입니다: {repo}")
            continue
        repo_classification = classify_with_reasons(
            repo.name,
            str(repo),
            company_keyword_hints=config.company_keyword_hints,
            personal_keyword_hints=config.personal_exclude_hints,
        )
        if repo_classification.category != COMPANY_WORK:
            add(
                "BLOCKED",
                f"회사 repo로 확인되지 않습니다({repo_classification.category}): {repo}. "
                "company_keyword_hints를 확인하세요.",
            )
        else:
            add("OK", f"company repo: {repo}")

    if config.notes_enabled:
        if config.notes_dir and _matches_privacy_exclude(config.notes_dir, config.privacy_exclude_patterns):
            add("BLOCKED", f"privacy exclude pattern에 해당하는 notes 경로입니다: {config.notes_dir}")
        elif config.notes_dir and config.notes_dir.is_dir():
            add("OK", f"notes: {config.notes_dir}")
        else:
            add("BLOCKED", f"활성화된 notes 경로가 없습니다: {config.notes_dir}")
    else:
        add("OK", "notes: disabled")
    if config.plan_enabled:
        if config.plan_file and _matches_privacy_exclude(config.plan_file, config.privacy_exclude_patterns):
            add("BLOCKED", f"privacy exclude pattern에 해당하는 plan 경로입니다: {config.plan_file}")
        elif config.plan_file and config.plan_file.is_file():
            add("OK", f"plan: {config.plan_file}")
        else:
            add("BLOCKED", f"활성화된 plan 경로가 없습니다: {config.plan_file}")
    else:
        add("OK", "plan: disabled")

    output_errors = validate_output_directory(config.out_dir, config.repos, require_exists=True)
    if output_errors:
        for error in output_errors:
            add("BLOCKED", error)
    else:
        add("OK", f"outbox writable: {config.out_dir}")
    log_errors = validate_log_directory(config.log_dir, config.repos, require_exists=True)
    if log_errors:
        for error in log_errors:
            add("BLOCKED", error)
    else:
        add("OK", f"log directory writable: {config.log_dir}")

    timezone_name = _system_timezone_name()
    if timezone_name == "Asia/Seoul":
        add("OK", "timezone: Asia/Seoul")
    else:
        add("BLOCKED", f"Mac timezone이 Asia/Seoul이 아닙니다: {timezone_name}")

    privacy_active = (
        _safe_path(".env", config.privacy_exclude_patterns) is None
        and sanitize_line("Bearer abcdefghijklmnop") is None
        and len(config.privacy_exclude_patterns) >= len(DEFAULT_PRIVACY_EXCLUDE_PATTERNS)
    )
    add("OK" if privacy_active else "BLOCKED", "privacy filters active" if privacy_active else "privacy filters inactive")
    personal_pre_git_skip = classify_with_reasons(
        "TokenForge", "/safe/TokenForge"
    ).category == PERSONAL_WORK
    add(
        "OK" if personal_pre_git_skip else "BLOCKED",
        "personal repos skipped before Git collection"
        if personal_pre_git_skip
        else "personal repo pre-Git skip is inactive",
    )

    if config.config_exists and not config.config_error:
        if not config.gmail_delivery.enabled:
            add("OK", "Gmail delivery: disabled")
        else:
            try:
                credentials = resolve_gmail_credentials(
                    config.gmail_delivery.sender_email_env,
                    config.gmail_delivery.app_password_env,
                    config.gmail_delivery.recipient_email_env,
                )
            except CredentialResolutionError:
                setup_command = shlex.join(
                    [
                        sys.executable,
                        str(PROJECT_ROOT / "mac_collect_lorotopik_worklog.py"),
                        "--config",
                        str(config.config_path),
                        "--setup-gmail-keychain",
                    ]
                )
                add(
                    "BLOCKED",
                    "Gmail delivery: enabled but credentials missing. "
                    f"Run: {setup_command}",
                )
            else:
                add(
                    "OK",
                    "Gmail delivery: enabled; credentials available from "
                    + credentials.source,
                )

    if not blocked:
        before = sorted(path.name for path in config.out_dir.iterdir())
        stats = CollectionStats()
        since, until = resolve_period("daily", None, None)
        items, dry_warnings = collect_configured_sources(config, since, until, stats=stats)
        included, personal, uncertain = classify_collected(
            items,
            company_keyword_hints=config.company_keyword_hints,
            personal_keyword_hints=config.personal_exclude_hints,
        )
        generate_worklog(
            included,
            personal,
            uncertain,
            mode="daily",
            target_date=until.date(),
            privacy_exclusions_count=stats.privacy_exclusions,
            date_range=(since.isoformat(), until.isoformat()),
        )
        after = sorted(path.name for path in config.out_dir.iterdir())
        if dry_warnings:
            add("BLOCKED", f"read-only dry-run 실패: {'; '.join(dry_warnings)}")
        elif before != after:
            add("BLOCKED", "진단 dry-run이 outbox 내용을 변경했습니다.")
        else:
            add(
                "OK",
                f"read-only dry-run: collected={len(items)}, company={len(included)}, "
                f"personal={len(personal)}, uncertain={len(uncertain)}, privacy={stats.privacy_exclusions}",
            )

    if preflight_only:
        print("Worklog Bridge preflight")
        for level, message in checks:
            print(f"[{level}] {message}")
        if blocked:
            print("Result: BLOCKED (exit 2)")
            return 2
        if warning:
            print("Result: WARNINGS (exit 1)")
            return 1
        print("Result: READY (exit 0)")
        return 0

    plist_payload = _read_launchd_plist(config.launchd_plist)
    if not config.launchd_plist.is_file():
        add("WARN", f"launchd plist not installed: {config.launchd_plist}")
    else:
        if not _schedule_is_weekdays_at_17(plist_payload):
            add("BLOCKED", "설치된 launchd 스케줄이 월~금 17:00이 아닙니다.")
        elif not _installed_plist_matches_config(plist_payload, config):
            add("BLOCKED", "설치된 launchd plist가 현재 config/log 경로와 다릅니다.")
        elif not _launchd_is_registered():
            add("BLOCKED", "launchd plist는 있으나 job을 load/list할 수 없습니다.")
        else:
            add("OK", "launchd loaded: weekdays 17:00 Asia/Seoul")

    daily_jsons = sorted(config.out_dir.glob("daily_worklog_*.json")) if config.out_dir.is_dir() else []
    daily_markdowns = sorted(config.out_dir.glob("daily_worklog_*.md")) if config.out_dir.is_dir() else []
    if daily_jsons and daily_markdowns:
        add("OK", f"latest daily artifacts: {daily_jsons[-1].name}, {daily_markdowns[-1].name}")
    else:
        add("WARN", "실제 daily JSON/Markdown 운영 산출물이 아직 없습니다.")

    print("Worklog Bridge diagnostics")
    for level, message in checks:
        print(f"[{level}] {message}")
    if blocked:
        print("Result: BLOCKED (exit 2)")
        return 2
    if warning:
        print("Result: PARTIALLY_CONFIGURED (exit 1)")
        return 1
    print("Result: READY (exit 0)")
    return 0


def print_activity_diagnostics(
    config: CollectorConfig, since: datetime, until: datetime
) -> int:
    """Show commit presence and timestamps without reading source, diffs, or subjects."""

    print("Worklog Bridge Git activity diagnostics")
    print(f"selected_date_range: {since.isoformat(timespec='seconds')} ~ {until.isoformat(timespec='seconds')}")
    if not config.repos:
        print("[BLOCKED] configured_repos: 0")
        print("activity_status: NOT_VERIFIED")
        return 2

    print(f"configured_repos: {len(config.repos)}")
    blocked = False
    commits_in_range = 0
    for repo in config.repos:
        print(f"repo: {repo}")
        if not repo.is_dir() or not (repo / ".git").exists():
            print("  latest_commit: NOT_AVAILABLE (missing or not a Git repo)")
            print("  commits_in_selected_range: NOT_VERIFIED")
            blocked = True
            continue
        if _matches_privacy_exclude(repo, config.privacy_exclude_patterns):
            print("  latest_commit: NOT_CHECKED (privacy excluded)")
            print("  commits_in_selected_range: NOT_VERIFIED")
            blocked = True
            continue
        repo_classification = classify_with_reasons(
            repo.name,
            str(repo),
            company_keyword_hints=config.company_keyword_hints,
            personal_keyword_hints=config.personal_exclude_hints,
        )
        if repo_classification.category != COMPANY_WORK:
            print(f"  latest_commit: NOT_CHECKED ({repo_classification.category})")
            print("  commits_in_selected_range: NOT_VERIFIED")
            blocked = True
            continue
        try:
            latest = _run_git(repo, ["log", "-1", "--date=iso-strict", "--format=%h%x1f%cI"]).strip()
            if latest:
                short_sha, committed_at = (latest.split("\x1f", maxsplit=1) + [""])[:2]
                print(f"  latest_commit: {short_sha} at {committed_at}")
                count_text = _run_git(
                    repo,
                    [
                        "rev-list",
                        "--count",
                        f"--since={since.isoformat(timespec='seconds')}",
                        f"--until={until.isoformat(timespec='seconds')}",
                        "HEAD",
                    ],
                ).strip()
                count = int(count_text or "0")
            else:
                print("  latest_commit: NONE")
                count = 0
            commits_in_range += count
            presence = "YES" if count else "NO"
            print(f"  commits_in_selected_range: {presence} ({count})")
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"  latest_commit: NOT_AVAILABLE ({exc})")
            print("  commits_in_selected_range: NOT_VERIFIED")
            blocked = True

    if blocked:
        print("activity_status: NOT_VERIFIED")
        return 2
    status = "ACTIVITY_FOUND" if commits_in_range else "EXPECTED_ZERO_ACTIVITY"
    print(f"total_commits_in_selected_range: {commits_in_range}")
    print(f"activity_status: {status}")
    return 0


def expected_output_paths(
    worklog: Mapping[str, object], out_dir: Path
) -> tuple[Path, Path]:
    if worklog.get("mode") == "weekly":
        stem = f"weekly_worklog_{worklog['week_id']}"
    else:
        stem = f"daily_worklog_{worklog['date']}"
    resolved_out_dir = out_dir.expanduser().resolve()
    return resolved_out_dir / f"{stem}.json", resolved_out_dir / f"{stem}.md"


def print_dry_run_summary(
    *,
    collected_count: int,
    included_count: int,
    personal_count: int,
    uncertain_count: int,
    privacy_exclusions_count: int,
    git_activity_count: int,
    since: datetime,
    until: datetime,
    json_path: Path,
    markdown_path: Path,
) -> None:
    print("--- Dry-run summary ---")
    print(f"selected_date_range: {since.isoformat(timespec='seconds')} ~ {until.isoformat(timespec='seconds')}")
    print(f"collected_item_count: {collected_count}")
    print(f"included_company_work_count: {included_count}")
    print(f"git_activity_count: {git_activity_count}")
    print(
        "git_activity_status: "
        + ("ACTIVITY_FOUND" if git_activity_count else "EXPECTED_ZERO_ACTIVITY")
    )
    if not git_activity_count:
        print("activity_note: 선택한 수집 기간에 Git 활동이 발견되지 않았습니다.")
    print(f"excluded_personal_work_count: {personal_count}")
    print(f"uncertain_count: {uncertain_count}")
    print(f"privacy_exclusions_count: {privacy_exclusions_count}")
    print(f"output_json_path: {json_path}")
    print(f"output_markdown_path: {markdown_path}")
    print("files_written: false")


def write_run_summary(
    config: CollectorConfig,
    *,
    run_id: str,
    mode: str,
    since: datetime,
    until: datetime,
    warnings: Iterable[str],
    output_files: Iterable[Path],
    privacy_exclusions_count: int,
    collected_item_count: int,
    included_company_work_count: int,
    git_activity_count: int,
    final_status: str,
    elapsed_seconds: float,
) -> Path:
    """Write metadata-only observability without task/source content."""

    skipped = [
        warning.split(": ", 1)[1]
        for warning in warnings
        if warning.startswith("개인 repo는 commit metadata도 수집하지 않았습니다:")
        and ": " in warning
    ]
    payload = {
        "run_id": run_id,
        "mode": mode,
        "date_range": {"since": since.isoformat(), "until": until.isoformat()},
        "config_path": str(config.config_path),
        "source_repos_considered": [str(path) for path in config.repos],
        "repos_skipped": [
            {"repo_path": path, "reason": "personal/private repository; Git metadata not collected"}
            for path in skipped
        ],
        "output_files_created": [str(path) for path in output_files],
        "privacy_exclusions_count": privacy_exclusions_count,
        "collected_item_count": max(0, int(collected_item_count)),
        "included_company_work_count": max(0, int(included_company_work_count)),
        "git_activity_count": max(0, int(git_activity_count)),
        "git_activity_status": (
            "ACTIVITY_FOUND" if git_activity_count else "EXPECTED_ZERO_ACTIVITY"
        ),
        "final_status": final_status,
        "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
    }
    summary_path = config.log_dir / f"run_summary_{run_id}.json"
    latest_path = config.log_dir / "last_run_summary.json"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = config.log_dir / f".{summary_path.name}.tmp"
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(summary_path)
    latest_temporary = config.log_dir / ".last_run_summary.json.tmp"
    latest_temporary.write_text(serialized, encoding="utf-8")
    latest_temporary.replace(latest_path)
    return summary_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LoroTopik 회사 업무만 보수적으로 분류해 근무일지 초안을 생성합니다."
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="로컬 config JSON (기본: config.local.json, launchd는 절대 경로 사용)",
    )
    parser.add_argument("--repo", action="append", default=[], type=Path, help="Git 저장소(여러 번 지정 가능)")
    parser.add_argument("--notes-dir", type=Path, help="업무 메모 .md/.txt 디렉터리")
    parser.add_argument("--plan-file", type=Path, help="업무 계획 .md/.txt/.json 파일")
    parser.add_argument("--comment", action="append", default=[], help="수동 업무 코멘트(여러 번 지정 가능)")
    parser.add_argument("--mode", choices=("daily", "weekly"), default="daily")
    parser.add_argument("--since", help="수집 시작일/시각 (포함)")
    parser.add_argument("--until", help="수집 종료일/시각 (포함)")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과를 표준 출력")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="현재 launchd/source/outbox 설정과 경로 존재 여부를 출력하고 종료",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="launchd 상태를 제외한 config/Python/path/privacy/read-only 수집 준비 상태 검사",
    )
    parser.add_argument(
        "--activity-diagnose",
        action="store_true",
        help="repo별 최신 commit 시각과 선택 기간의 commit 존재 여부만 안전하게 출력",
    )
    parser.add_argument(
        "--setup-gmail-keychain",
        action="store_true",
        help="Gmail credentials를 macOS Keychain에 secure prompt로 저장하고 종료",
    )
    parser.add_argument(
        "--include-uncertain",
        action="store_true",
        help="검토 후 명시적으로 uncertain을 fields에도 포함(기본 false)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if sum(
        (
            args.diagnose,
            args.preflight,
            args.activity_diagnose,
            args.setup_gmail_keychain,
        )
    ) > 1:
        parser.error(
            "--diagnose, --preflight, --activity-diagnose, "
            "--setup-gmail-keychain은 함께 사용할 수 없습니다."
        )
    config = resolve_collector_config(args)
    if args.setup_gmail_keychain:
        if config.config_error:
            print(f"오류: {config.config_error}", file=sys.stderr)
            return 2
        try:
            setup_macos_keychain(dry_run=args.dry_run)
        except CredentialResolutionError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 2
        print("Keychain setup complete. Verify with --preflight or --diagnose.")
        return 0
    if args.diagnose:
        return print_diagnostics(config)
    if args.preflight:
        return print_diagnostics(config, preflight_only=True)
    if args.activity_diagnose:
        if config.config_error:
            print(f"오류: {config.config_error}", file=sys.stderr)
            return 2
        try:
            since, until = resolve_period(args.mode, args.since, args.until)
        except (ValueError, argparse.ArgumentTypeError) as exc:
            parser.error(str(exc))
        return print_activity_diagnostics(config, since, until)
    if config.config_error and (args.config is not None or config.config_exists):
        print(f"오류: {config.config_error}", file=sys.stderr)
        return 2
    try:
        since, until = resolve_period(args.mode, args.since, args.until)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(str(exc))
    target_date = until.date()
    run_id = f"{datetime.now().astimezone():%Y%m%dT%H%M%S%z}-{uuid.uuid4().hex[:8]}"
    started_at = time_module.monotonic()
    stats = CollectionStats()
    output_errors = validate_output_directory(
        config.out_dir, config.repos, require_exists=not args.dry_run
    )
    if output_errors:
        for error in output_errors:
            print(f"오류: {error}", file=sys.stderr)
        return 2
    if not args.dry_run:
        log_errors = validate_log_directory(config.log_dir, config.repos, require_exists=True)
        if log_errors:
            for error in log_errors:
                print(f"오류: {error}", file=sys.stderr)
            return 2

    all_items, warnings = collect_configured_sources(
        config, since, until, comments=args.comment, stats=stats
    )
    git_activity_count = sum(
        1 for item in all_items if str(item.get("source_type", "")).casefold() == "git"
    )
    blocking_warnings = [
        warning for warning in warnings
        if not warning.startswith("개인 repo는 commit metadata도 수집하지 않았습니다:")
        and not warning.startswith("민감정보가 감지된 --comment")
    ]
    if blocking_warnings and not args.dry_run:
        for warning in warnings:
            print(f"오류: {warning}", file=sys.stderr)
        try:
            write_run_summary(
                config,
                run_id=run_id,
                mode=args.mode,
                since=since,
                until=until,
                warnings=warnings,
                output_files=(),
                privacy_exclusions_count=stats.privacy_exclusions,
                collected_item_count=len(all_items),
                included_company_work_count=0,
                git_activity_count=git_activity_count,
                final_status="BLOCKED",
                elapsed_seconds=time_module.monotonic() - started_at,
            )
        except OSError as exc:
            print(f"경고: 실패 run summary를 쓰지 못했습니다: {exc}", file=sys.stderr)
        return 2

    included, personal, uncertain = classify_collected(
        all_items,
        company_keyword_hints=config.company_keyword_hints,
        personal_keyword_hints=config.personal_exclude_hints,
    )
    worklog = generate_worklog(
        included,
        personal,
        uncertain,
        mode=args.mode,
        target_date=target_date,
        include_uncertain=args.include_uncertain,
        privacy_exclusions_count=stats.privacy_exclusions,
        date_range=(since.isoformat(), until.isoformat()),
        collected_item_count=len(all_items),
    )
    json_path, markdown_path = expected_output_paths(worklog, config.out_dir)
    if args.dry_run:
        print_dry_run_summary(
            collected_count=len(all_items),
            included_count=len(included),
            personal_count=len(personal),
            uncertain_count=len(uncertain),
            privacy_exclusions_count=stats.privacy_exclusions,
            git_activity_count=git_activity_count,
            since=since,
            until=until,
            json_path=json_path,
            markdown_path=markdown_path,
        )
    else:
        delivery_failed = False
        try:
            json_path, markdown_path = write_worklog_files(worklog, json_path, markdown_path)
        except OSError as exc:
            print(f"오류: output 파일 생성 실패: {exc}", file=sys.stderr)
            try:
                write_run_summary(
                    config,
                    run_id=run_id,
                    mode=args.mode,
                    since=since,
                    until=until,
                    warnings=warnings,
                    output_files=(),
                    privacy_exclusions_count=stats.privacy_exclusions,
                    collected_item_count=len(all_items),
                    included_company_work_count=len(included),
                    git_activity_count=git_activity_count,
                    final_status="FAILED",
                    elapsed_seconds=time_module.monotonic() - started_at,
                )
            except OSError:
                pass
            return 2
        print(f"JSON: {json_path}")
        print(f"Markdown: {markdown_path}")
        if args.mode == "daily" and config.gmail_delivery.enabled:
            try:
                delivery_result = deliver_daily_worklog(
                    markdown_path,
                    config.gmail_delivery,
                    config.log_dir,
                )
                print(f"Gmail delivery: {delivery_result.status}")
                if delivery_result.credential_source:
                    print(
                        "Gmail credentials loaded from: "
                        + delivery_result.credential_source
                    )
                if delivery_result.status == GMAIL_DELIVERY_FAILED:
                    delivery_failed = True
                    warnings.append(
                        "Gmail delivery 실패: "
                        + (
                            delivery_result.sanitized_error
                            or "안전한 delivery metadata와 launchd stderr log를 확인하세요."
                        )
                    )
            except Exception:
                # The generated files are already durable. Keep delivery
                # failures isolated and never expose credentials or addresses.
                delivery_failed = True
                warnings.append(
                    "Gmail delivery 실패: delivery 처리 중 안전하게 보고할 수 없는 오류가 발생했습니다."
                )
        try:
            summary_path = write_run_summary(
                config,
                run_id=run_id,
                mode=args.mode,
                since=since,
                until=until,
                warnings=warnings,
                output_files=(json_path, markdown_path),
                privacy_exclusions_count=stats.privacy_exclusions,
                collected_item_count=len(all_items),
                included_company_work_count=len(included),
                git_activity_count=git_activity_count,
                final_status=(
                    "DELIVERY_FAILED"
                    if delivery_failed
                    and config.gmail_delivery.fail_collection_on_delivery_error
                    else "SUCCESS"
                ),
                elapsed_seconds=time_module.monotonic() - started_at,
            )
        except OSError as exc:
            print(f"오류: run summary 생성 실패: {exc}", file=sys.stderr)
            return 2
        print(f"Run summary: {summary_path}")
    for warning in warnings:
        print(f"경고: {warning}", file=sys.stderr)
    if (
        not args.dry_run
        and args.mode == "daily"
        and config.gmail_delivery.enabled
        and delivery_failed
        and config.gmail_delivery.fail_collection_on_delivery_error
    ):
        print(
            "오류: daily worklog는 생성됐지만 Gmail delivery 설정에 따라 nonzero로 종료합니다.",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        print("Dry-run 완료: 어떤 output 파일도 생성하지 않았습니다.")
    elif args.mode == "daily" and config.gmail_delivery.enabled:
        if delivery_failed:
            print("Daily worklog 생성 완료. Gmail delivery는 실패했으며 output 파일은 보존되었습니다.")
        else:
            print("Daily worklog 생성 및 Gmail delivery 단계 완료.")
    else:
        print(COMPLETION_MESSAGE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
