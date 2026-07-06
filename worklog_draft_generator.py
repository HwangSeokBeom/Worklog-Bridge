"""Generate privacy-safe LoroLog JSON fields and Markdown drafts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Iterable, Mapping, Optional

from privacy_guard import sanitize_line, sanitize_text


FIELD_NAMES: tuple[str, ...] = (
    "DATE", "WEEK_RANGE", "SUMMARY", "TASKS", "BUSINESS_ANALYSIS",
    "APP_DIRECTION", "DEV_WORK", "LEARNINGS", "DIFFICULTIES", "NEXT_PLAN", "COMMENT",
)
PRIVACY_NOTE = (
    "No source code, full diffs, secrets, credentials, or sensitive company data collected."
)


def _as_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _week_metadata(day: date) -> tuple[str, str]:
    iso = day.isocalendar()
    monday = day - timedelta(days=day.weekday())
    friday = monday + timedelta(days=4)
    return f"{iso.year}-{iso.week:02d}", f"{monday:%Y.%m.%d} ~ {friday:%Y.%m.%d}"


def _item_text(item: Mapping[str, object]) -> str:
    raw = str(item.get("summary") or item.get("text") or item.get("title") or "")
    safe_lines = [line for line in sanitize_text(raw, max_lines=3, max_length=300).splitlines() if line]
    return " ".join(safe_lines)


def _unique_text(items: Iterable[Mapping[str, object]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _field_text(item)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _bullets(values: Iterable[str], empty: str = "해당 사항 없음") -> str:
    cleaned = [value.strip(" -•\t") for value in values if value.strip(" -•\t")]
    return "\n".join(f"- {value}" for value in cleaned) if cleaned else empty


def _matching(items: Iterable[Mapping[str, object]], keywords: Iterable[str]) -> list[str]:
    needles = tuple(value.casefold() for value in keywords)
    result: list[str] = []
    for item in items:
        raw = _item_text(item)
        if raw and any(needle in raw.casefold() for needle in needles):
            result.append(_field_text(item))
    return result


def _field_text(item: Mapping[str, object]) -> str:
    """Turn terse source metadata into a restrained intern-worklog sentence."""

    text = _item_text(item)
    if not text:
        return ""
    text = re.sub(
        r"^(?:feat|fix|docs|refactor|test|chore|build|ci|perf)(?:\([^)]*\))?!?:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    finished = ("했다.", "하였다.", "파악했다", "정리했다", "검토했다", "학습했다", "작성했다", "도출했다", "이해했다")
    if text.endswith(finished):
        return text if text.endswith(".") else text + "."
    if str(item.get("source_type", "")).casefold() == "git":
        return f"{text} 관련 구현 내용을 확인하고 정리했다."
    if any(word in text.casefold() for word in ("계획", "todo", "예정", "다음")):
        return f"{text} 관련 계획을 정리했다."
    if any(word in text.casefold() for word in ("분석", "경쟁", "사업")):
        return f"{text} 관련 내용을 분석하고 정리했다."
    return f"{text} 관련 내용을 검토하고 정리했다."


def _safe_item(item: Mapping[str, object]) -> dict[str, object]:
    """Keep bounded metadata only; never copy arbitrary collector payloads."""

    allowed = (
        "source_type", "date", "title", "summary", "repo_name", "repo_path",
        "commit_sha", "changed_files", "shortstat", "source_file", "tags",
        "classification", "classification_reasons", "frontmatter",
    )
    safe: dict[str, object] = {}
    for key in allowed:
        value = item.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, Mapping):
            clean_mapping: dict[str, str] = {}
            for child_key, child_value in list(value.items())[:20]:
                clean_key = sanitize_line(str(child_key), max_length=50)
                clean_value = sanitize_line(str(child_value), max_length=200)
                if clean_key and clean_value:
                    clean_mapping[clean_key] = clean_value
            if clean_mapping:
                safe[key] = clean_mapping
        elif isinstance(value, list):
            bounded: list[str] = []
            for entry in value[:50]:
                clean = sanitize_line(str(entry), max_length=300)
                if clean:
                    bounded.append(clean)
            if bounded:
                safe[key] = bounded
        else:
            clean = sanitize_line(str(value), max_length=500)
            if clean:
                safe[key] = clean
    short_draft = _field_text(item)
    if short_draft:
        safe["short_worklog_draft"] = short_draft
    return safe


def _personal_stub(item: Mapping[str, object]) -> dict[str, object]:
    project = item.get("repo_name") or item.get("project_name")
    if not project:
        text = _item_text(item).casefold()
        known_projects = ("TokenForge", "HWANGTODO", "Lovey Moment")
        project = next((name for name in known_projects if name.casefold() in text), "personal_project")
    clean = sanitize_line(str(project), max_length=100) or "personal_project"
    return {
        "source_type": str(item.get("source_type") or "unknown"),
        "project_name": clean,
        "classification": "personal_work",
        "classification_reason": "personal project signal; details omitted",
    }


def generate_worklog(
    included_items: Iterable[Mapping[str, object]],
    excluded_personal_items: Iterable[Mapping[str, object]] = (),
    uncertain_items: Iterable[Mapping[str, object]] = (),
    *,
    mode: str = "daily",
    target_date: str | date | datetime | None = None,
    include_uncertain: bool = False,
    generated_at: Optional[str] = None,
    privacy_exclusions_count: int = 0,
    date_range: Optional[tuple[str, str]] = None,
    collected_item_count: Optional[int] = None,
) -> dict[str, object]:
    """Build the documented JSON structure from already classified items."""

    if mode not in {"daily", "weekly"}:
        raise ValueError("mode must be 'daily' or 'weekly'")
    day = _as_date(target_date or date.today())
    week_id, week_range = _week_metadata(day)
    if date_range is None:
        if mode == "weekly":
            monday = day - timedelta(days=day.weekday())
            friday = monday + timedelta(days=4)
            date_range = (monday.isoformat(), friday.isoformat())
        else:
            date_range = (day.isoformat(), day.isoformat())
    raw_included = list(included_items)
    raw_personal = list(excluded_personal_items)
    raw_uncertain = list(uncertain_items)
    all_classified_items = raw_included + raw_personal + raw_uncertain
    git_activity_count = sum(
        1
        for item in all_classified_items
        if str(item.get("source_type", "")).casefold() == "git"
    )
    total_collected = (
        len(all_classified_items)
        if collected_item_count is None
        else max(0, int(collected_item_count))
    )
    selected_range = f"{date_range[0]} ~ {date_range[1]}"
    if git_activity_count:
        git_activity_status = "ACTIVITY_FOUND"
        activity_message = (
            f"선택한 수집 기간({selected_range})에 Git 활동 {git_activity_count}건이 발견되었습니다."
        )
    else:
        git_activity_status = "EXPECTED_ZERO_ACTIVITY"
        activity_message = (
            f"선택한 수집 기간({selected_range})에 Git 활동이 발견되지 않았습니다."
        )

    included = [_safe_item(item) for item in raw_included]
    included = [item for item in included if item]
    personal = [_personal_stub(item) for item in raw_personal]
    uncertain = [_safe_item(item) for item in raw_uncertain]
    uncertain = [item for item in uncertain if item]

    # Explicit opt-in is visible in the policy.  The original uncertain section
    # remains intact so a reviewer can spot what was promoted.
    field_items = included + (uncertain if include_uncertain else [])
    all_tasks = _unique_text(field_items)
    comments = [
        text for item in field_items
        if str(item.get("source_type", "")).casefold() == "comment"
        and (text := _field_text(item))
    ]
    business = _matching(field_items, ("사업", "경쟁", "duolingo", "말해보카", "스픽", "분석"))
    direction = _matching(field_items, ("방향", "모바일 앱", "학습 앱", "학습앱", "사용자", "기획"))
    development = _matching(
        field_items,
        ("sso", "oidc", "oauth", "idp", "flutter", "redirect", "state", "nonce", "구현", "개발", "수정"),
    )
    learnings = _matching(field_items, ("학습", "파악", "이해", "검토", "조사", "분석"))
    difficulties = _matching(field_items, ("어려움", "이슈", "문제", "오류", "장애", "제약"))
    next_plan = _matching(field_items, ("계획", "todo", "예정", "다음", "향후", "개선"))

    period_word = "이번 주" if mode == "weekly" else "오늘"
    if all_tasks:
        summary = f"{period_word} LoroTopik 관련 업무 {len(all_tasks)}건을 파악하고 진행 내용을 정리했다."
    elif git_activity_count:
        summary = (
            f"{activity_message} 회사 업무로 포함된 항목은 0건이므로 실제 업무를 수집한 것으로 "
            "기록하지 않았다."
        )
    else:
        summary = f"{activity_message} 포함된 회사 업무는 0건이다."

    fields = {
        "DATE": day.isoformat(),
        "WEEK_RANGE": week_range,
        "SUMMARY": summary,
        "TASKS": _bullets(all_tasks),
        "BUSINESS_ANALYSIS": _bullets(business),
        "APP_DIRECTION": _bullets(direction),
        "DEV_WORK": _bullets(development),
        "LEARNINGS": _bullets(learnings),
        "DIFFICULTIES": _bullets(difficulties),
        "NEXT_PLAN": _bullets(next_plan),
        "COMMENT": _bullets(comments),
    }

    return {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "date": day.isoformat(),
        "week_id": week_id,
        "week_range": week_range,
        "date_range": {"since": date_range[0], "until": date_range[1]},
        "included_policy": (
            "company_work_plus_explicit_uncertain" if include_uncertain else "company_work_only"
        ),
        "collection_summary": {
            "collected_item_count": total_collected,
            "git_activity_count": git_activity_count,
            "git_activity_status": git_activity_status,
            "included_company_work_count": len(included),
            "message": activity_message,
        },
        "fields": fields,
        "included_items": included,
        "excluded_personal_items": personal,
        "uncertain_items": uncertain,
        "privacy_exclusions_summary": {
            "excluded_count": max(0, int(privacy_exclusions_count)),
            "policy": PRIVACY_NOTE,
        },
        "privacy_note": PRIVACY_NOTE,
    }


def render_markdown(worklog: Mapping[str, object]) -> str:
    """Render an email- and HWP-fallback-friendly Markdown document."""

    fields = worklog.get("fields", {})
    if not isinstance(fields, Mapping):
        raise ValueError("worklog fields must be a mapping")
    labels = {
        "SUMMARY": "업무 요약", "TASKS": "수행 업무", "BUSINESS_ANALYSIS": "사업 분석",
        "APP_DIRECTION": "앱 개발 방향", "DEV_WORK": "개발 업무", "LEARNINGS": "학습/파악 내용",
        "DIFFICULTIES": "어려웠던 점", "NEXT_PLAN": "다음 계획", "COMMENT": "코멘트",
    }
    mode = str(worklog.get("mode", "daily"))
    title = "주간" if mode == "weekly" else "일일"
    raw_date_range = worklog.get("date_range", {})
    if isinstance(raw_date_range, Mapping):
        date_range_text = f"{raw_date_range.get('since', '')} ~ {raw_date_range.get('until', '')}"
    else:
        date_range_text = str(raw_date_range)
    lines = [
        f"# LoroTopik {title} 근무일지 초안",
        "",
        f"- 날짜: {fields.get('DATE', '')}",
        f"- 주간 범위: {fields.get('WEEK_RANGE', '')}",
        f"- 수집 범위: {date_range_text}",
        f"- 포함 정책: {worklog.get('included_policy', 'company_work_only')}",
    ]
    collection_summary = worklog.get("collection_summary", {})
    if isinstance(collection_summary, Mapping):
        activity_message = str(collection_summary.get("message", "")).strip()
        if activity_message:
            lines.append(f"- 수집 상태: {activity_message}")
    for key, label in labels.items():
        lines.extend(("", f"## {label}", "", str(fields.get(key, "해당 사항 없음"))))

    included = worklog.get("included_items", [])
    uncertain = worklog.get("uncertain_items", [])
    personal = worklog.get("excluded_personal_items", [])
    lines.extend(("", "## 분류 요약", "", f"- 포함된 회사 업무: {len(included) if isinstance(included, list) else 0}건"))
    if isinstance(collection_summary, Mapping):
        lines.append(
            f"- Git 활동 상태: {collection_summary.get('git_activity_status', 'UNKNOWN')} "
            f"({collection_summary.get('git_activity_count', 0)}건)"
        )
    lines.append(f"- 검토가 필요한 uncertain: {len(uncertain) if isinstance(uncertain, list) else 0}건")
    if isinstance(uncertain, list) and uncertain:
        lines.append("- uncertain 항목은 기본적으로 위 HWP 필드에 반영하지 않음")
        lines.extend(("", "### uncertain 검토 목록", ""))
        for item in uncertain[:20]:
            if isinstance(item, Mapping):
                summary = _item_text(item)
                if summary:
                    lines.append(f"- {summary}")
    project_names: list[str] = []
    if isinstance(personal, list):
        for item in personal:
            if isinstance(item, Mapping):
                name = str(item.get("project_name", "personal_project"))
                if name not in project_names:
                    project_names.append(name)
    project_suffix = f" ({', '.join(project_names)})" if project_names else ""
    lines.append(f"- 제외된 개인 작업: {len(personal) if isinstance(personal, list) else 0}건{project_suffix}")
    privacy_summary = worklog.get("privacy_exclusions_summary", {})
    if isinstance(privacy_summary, Mapping):
        lines.append(f"- privacy 제외: {privacy_summary.get('excluded_count', 0)}건")
    lines.extend(("", f"> Privacy: {worklog.get('privacy_note', PRIVACY_NOTE)}", ""))
    return "\n".join(lines)


def write_worklog_files(
    worklog: Mapping[str, object], json_path: Path, markdown_path: Optional[Path] = None
) -> tuple[Path, Path]:
    """Convenience writer used by the collector and tests."""

    import json

    markdown_path = markdown_path or json_path.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(worklog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(worklog), encoding="utf-8")
    return json_path, markdown_path


# Backwards-friendly aliases for small scripts/tests.
generate_draft = generate_worklog
generate_markdown = render_markdown
