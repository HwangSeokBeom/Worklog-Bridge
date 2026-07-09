"""Generate privacy-safe LoroLog JSON fields and Markdown drafts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Iterable, Mapping, Optional

from privacy_guard import sanitize_line, sanitize_text


FIELD_NAMES: tuple[str, ...] = (
    "DATE", "WEEK_RANGE", "WORK_CONTENT", "SUMMARY", "TASKS", "BUSINESS_ANALYSIS",
    "APP_DIRECTION", "DEV_WORK", "LEARNINGS", "DIFFICULTIES", "NEXT_PLAN", "COMMENT",
)
PARAGRAPH_FIELD_NAMES: tuple[str, ...] = (
    "DATE", "WEEK_RANGE", "WORK_CONTENT", "NEXT_PLAN", "COMMENT",
)
LEGACY_FIELD_NAMES: tuple[str, ...] = tuple(
    name for name in FIELD_NAMES if name != "WORK_CONTENT"
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


def _bullets(values: Iterable[str], empty: str = "") -> str:
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


_INTERNAL_STATUS_PHRASES = (
    "Git 활동이 발견되지 않았습니다",
    "포함된 회사 업무는 0건",
    "해당 사항 없음",
    "EXPECTED_ZERO_ACTIVITY",
)
_KNOWN_PERSONAL_PROJECTS = ("TokenForge", "HWANGTODO", "Lovey Moment")
_ACTION_VERBS: tuple[tuple[str, str], ...] = (
    ("파악", "파악하였다"),
    ("검토", "검토하였다"),
    ("학습", "학습하였다"),
    ("정리", "정리하였다"),
    ("이해", "이해하였다"),
    ("분석", "분석하였다"),
    ("조사", "조사하였다"),
    ("작성", "작성하였다"),
    ("설계", "설계하였다"),
    ("구현", "구현하였다"),
    ("개발", "개발하였다"),
    ("수정", "수정하였다"),
    ("개선", "개선하였다"),
    ("확인", "확인하였다"),
    ("검증", "검증하였다"),
    ("진행", "진행하였다"),
    ("테스트", "테스트하였다"),
    ("논의", "논의하였다"),
)


def _clean_activity_text(item: Mapping[str, object]) -> str:
    """Remove source-format and internal-status wording from a worklog activity."""

    text = _item_text(item).strip()
    if not text or any(phrase.casefold() in text.casefold() for phrase in _INTERNAL_STATUS_PHRASES):
        return ""
    text = re.sub(r"^[#>*+\-•\s]+", "", text)
    text = re.sub(r"\s+#{1,6}\s*", " ", text)
    text = re.sub(r"\s+[-*+•]\s+", ", ", text)
    text = re.sub(
        r"^(?:업무\s*요약|수행\s*업무|사업\s*분석|앱\s*개발\s*방향|개발\s*업무|학습\s*내용)\s*[:：-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:(?:ChatGPT|GPT|Claude|Gemini)(?:\s*[-/]?\s*[^\s]+)?|Microsoft\s+Teams|Teams)"
        r"(?:을|를)?\s*(?:사용(?:하여|해|해서)|통해)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:ChatGPT|GPT|Claude|Gemini|Microsoft\s+Teams|Teams)(?:에서)?\s*[:：-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip(" \t-•")


def _is_personal_item(item: Mapping[str, object]) -> bool:
    classification = str(item.get("classification", "")).casefold()
    if "personal" in classification:
        return True
    project = str(item.get("repo_name") or item.get("project_name") or "").casefold()
    return any(name.casefold() in project for name in _KNOWN_PERSONAL_PROJECTS)


def _has_batchim(character: str) -> bool:
    code = ord(character)
    return 0xAC00 <= code <= 0xD7A3 and (code - 0xAC00) % 28 != 0


def _with_object_particle(text: str) -> str:
    text = text.strip()
    if not text or text.endswith(("을", "를", "대해", "관해")):
        return text
    final = next((character for character in reversed(text) if character.isalnum()), "")
    particle = "을" if final and _has_batchim(final) else "를"
    return text + particle


def _naturalize_activity_phrase(text: str, action: str) -> str:
    text = re.sub(
        r"\b(LoroTOPIK)\s+서비스\s+전체\s+구조\b",
        r"\1 서비스의 전체 구조",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("모바일 앱 로그인 버튼 이후", "모바일 앱에서 로그인 버튼을 누른 뒤")
    text = text.replace(
        "모의고사 서비스 로그인 화면 이동 흐름",
        "모의고사 서비스 로그인 화면으로 이동하는 흐름",
    )
    if text.endswith(" 역할") and not text.endswith("의 역할"):
        text = text[:-3].rstrip() + "의 역할"
    if action == "학습" and "개념" not in text and sum(
        keyword.casefold() in text.casefold()
        for keyword in ("SSO", "IdP", "OIDC", "redirect URI", "state", "nonce")
    ) >= 2:
        text += " 등 인증 관련 개념"
    return text


def _activity_sentence(item: Mapping[str, object]) -> str:
    """Convert a terse activity record into one restrained Korean sentence."""

    text = _clean_activity_text(item)
    if not text:
        return ""
    text = re.sub(
        r"^(?:feat|fix|docs|refactor|test|chore|build|ci|perf)(?:\([^)]*\))?!?:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if re.search(r"(?:다|요)[.!?]$", text):
        return text
    if text.endswith(("하였다", "했다", "되었다", "이다")):
        return text + "."
    for action, conjugated in _ACTION_VERBS:
        match = re.match(rf"^(.*?)\s+{re.escape(action)}$", text)
        if match:
            subject = _naturalize_activity_phrase(match.group(1).strip(), action)
            return f"{_with_object_particle(subject)} {conjugated}."
    if str(item.get("source_type", "")).casefold() == "git":
        return f"{_with_object_particle(text)} 확인하고 정리하였다."
    if any(word in text.casefold() for word in ("계획", "todo", "예정", "다음", "향후")):
        return f"{text} 관련 계획을 정리하였다."
    return f"{text} 관련 내용을 검토하고 정리하였다."


def _to_connective(sentence: str) -> str:
    sentence = sentence.strip()
    endings = (
        ("하였다.", "하였고,"),
        ("했다.", "했고,"),
        ("되었다.", "되었고,"),
        ("이다.", "이며,"),
    )
    for ending, connective in endings:
        if sentence.endswith(ending):
            return sentence[: -len(ending)] + connective
    return sentence.rstrip(".!?") + "고,"


def _sentence_group(sentences: list[str]) -> str:
    if not sentences:
        return ""
    if len(sentences) == 1:
        return sentences[0]
    return " ".join([*(_to_connective(value) for value in sentences[:-1]), sentences[-1]])


def _paragraph(sentences: Iterable[str], *, max_items: int = 6, max_sentences: int = 3) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        clean = sentence.strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            unique.append(clean)
        if len(unique) >= max_items:
            break
    if not unique:
        return ""
    sentence_count = min(max_sentences, len(unique))
    base, remainder = divmod(len(unique), sentence_count)
    groups: list[list[str]] = []
    cursor = 0
    for index in range(sentence_count):
        size = base + (1 if index < remainder else 0)
        groups.append(unique[cursor : cursor + size])
        cursor += size
    return " ".join(_sentence_group(group) for group in groups if group)


def _item_sentences(items: Iterable[Mapping[str, object]]) -> list[str]:
    return [sentence for item in items if (sentence := _activity_sentence(item))]


def _source_type(item: Mapping[str, object]) -> str:
    return str(item.get("source_type", "")).casefold()


def _hybrid_work_items(
    items: Iterable[Mapping[str, object]], *, max_items: int = 6
) -> list[Mapping[str, object]]:
    """Keep manual/note context first while preserving Git as one hybrid signal.

    Manual activity is a first-class source, not a fallback.  When both manual
    or note-like evidence and Git evidence exist, reserve room for at least one
    Git item so the final paragraph reflects the combined work trail without
    turning into a raw commit list.
    """

    ordered = list(items)
    non_git = [item for item in ordered if _source_type(item) != "git"]
    git_items = [item for item in ordered if _source_type(item) == "git"]
    if not non_git or not git_items or len(ordered) <= max_items:
        return ordered[:max_items]

    selected: list[Mapping[str, object]] = []
    selected_ids: set[int] = set()

    for item in non_git[: max_items - 1]:
        selected.append(item)
        selected_ids.add(id(item))

    first_git = git_items[0]
    selected.append(first_git)
    selected_ids.add(id(first_git))

    for item in ordered:
        if len(selected) >= max_items:
            break
        if id(item) in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(id(item))

    return selected


def _field_text(item: Mapping[str, object]) -> str:
    """Turn terse source metadata into a restrained intern-worklog sentence."""

    return _activity_sentence(item)


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
        project = next(
            (name for name in _KNOWN_PERSONAL_PROJECTS if name.casefold() in text),
            "personal_project",
        )
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
    draft_style: str = "paragraph",
) -> dict[str, object]:
    """Build the documented JSON structure from already classified items."""

    if mode not in {"daily", "weekly"}:
        raise ValueError("mode must be 'daily' or 'weekly'")
    if draft_style not in {"paragraph", "sectioned"}:
        raise ValueError("draft_style must be 'paragraph' or 'sectioned'")
    day = _as_date(target_date or date.today())
    week_id, week_range = _week_metadata(day)
    if date_range is None:
        if mode == "weekly":
            monday = day - timedelta(days=day.weekday())
            friday = monday + timedelta(days=4)
            date_range = (monday.isoformat(), friday.isoformat())
        else:
            date_range = (day.isoformat(), day.isoformat())
    supplied_included = list(included_items)
    raw_personal = list(excluded_personal_items)
    raw_included: list[Mapping[str, object]] = []
    for item in supplied_included:
        if _is_personal_item(item):
            raw_personal.append(item)
        else:
            raw_included.append(item)
    raw_uncertain: list[Mapping[str, object]] = []
    for item in uncertain_items:
        if _is_personal_item(item):
            raw_personal.append(item)
        else:
            raw_uncertain.append(item)
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

    # Explicit opt-in is visible in the policy. The original uncertain section
    # remains intact so a reviewer can spot what was promoted.
    field_items = included + (uncertain if include_uncertain else [])
    planning_keywords = ("계획", "todo", "예정", "다음", "향후")
    work_items = [
        item for item in field_items
        if not any(keyword in _item_text(item).casefold() for keyword in planning_keywords)
    ]
    work_items = _hybrid_work_items(work_items)
    work_sentences = _item_sentences(work_items)
    work_content = _paragraph(work_sentences)
    no_collected_activity = total_collected == 0 and not field_items
    if not work_content and no_collected_activity:
        work_content = "선택한 기간에 수집된 업무 활동이 없어 근무일지 초안을 생성하지 않았다."

    comments = [
        sentence for item in field_items
        if str(item.get("source_type", "")).casefold() in {"comment", "manual_comment"}
        and (sentence := _activity_sentence(item))
    ]
    business = _matching(field_items, ("사업", "경쟁", "duolingo", "말해보카", "스픽", "분석"))
    direction = _matching(field_items, ("방향", "모바일 앱", "학습 앱", "학습앱", "사용자", "기획"))
    development = _matching(
        field_items,
        ("sso", "oidc", "oauth", "idp", "flutter", "redirect", "state", "nonce", "로그인", "구현", "개발", "수정"),
    )
    learnings = _matching(field_items, ("학습", "파악", "이해", "검토", "조사", "분석"))
    difficulties = _matching(field_items, ("어려움", "이슈", "문제", "오류", "장애", "제약"))
    next_plan = _matching(field_items, planning_keywords)

    paragraph_fields = {
        "WORK_CONTENT": work_content,
        "SUMMARY": work_content,
        "TASKS": work_content,
        "BUSINESS_ANALYSIS": _paragraph(business, max_items=2, max_sentences=1),
        "APP_DIRECTION": _paragraph(direction, max_items=2, max_sentences=1),
        "DEV_WORK": _paragraph(development, max_items=3, max_sentences=2),
        "LEARNINGS": _paragraph(learnings, max_items=2, max_sentences=1),
        "DIFFICULTIES": _paragraph(difficulties, max_items=2, max_sentences=1),
        "NEXT_PLAN": _paragraph(next_plan, max_items=2, max_sentences=1),
        "COMMENT": _paragraph(comments, max_items=2, max_sentences=1),
    }

    if draft_style == "sectioned":
        sections: list[str] = []
        section_values = (
            ("업무 요약", work_content),
            ("수행 업무", _bullets(work_sentences)),
            ("사업 분석", _bullets(business)),
            ("앱 개발 방향", _bullets(direction)),
            ("개발 업무", _bullets(development)),
            ("학습 내용", _bullets(learnings)),
        )
        for label, value in section_values:
            if value:
                sections.append(f"## {label}\n{value}")
        paragraph_fields.update(
            {
                "WORK_CONTENT": "\n\n".join(sections),
                "TASKS": _bullets(work_sentences),
                "BUSINESS_ANALYSIS": _bullets(business),
                "APP_DIRECTION": _bullets(direction),
                "DEV_WORK": _bullets(development),
                "LEARNINGS": _bullets(learnings),
                "DIFFICULTIES": _bullets(difficulties),
            }
        )

    fields = {
        "DATE": day.isoformat(),
        "WEEK_RANGE": week_range,
        **paragraph_fields,
    }

    return {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "draft_style": draft_style,
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
    """Render a human-review document separately from the HWP field style."""

    fields = worklog.get("fields", {})
    if not isinstance(fields, Mapping):
        raise ValueError("worklog fields must be a mapping")
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
        f"- HWP 초안 스타일: {worklog.get('draft_style', 'paragraph')}",
    ]
    collection_summary = worklog.get("collection_summary", {})
    if isinstance(collection_summary, Mapping):
        activity_message = str(collection_summary.get("message", "")).strip()
        if activity_message:
            lines.append(f"- 수집 상태: {activity_message}")
    work_content = str(fields.get("WORK_CONTENT") or fields.get("SUMMARY") or "").strip()
    if work_content:
        lines.extend(("", "## HWP 근무 내용 미리보기", "", work_content))
    next_plan = str(fields.get("NEXT_PLAN", "")).strip()
    if next_plan:
        lines.extend(("", "## 다음 계획", "", next_plan))
    comment = str(fields.get("COMMENT", "")).strip()
    if comment:
        lines.extend(("", "## 수동 코멘트", "", comment))

    included = worklog.get("included_items", [])
    uncertain = worklog.get("uncertain_items", [])
    personal = worklog.get("excluded_personal_items", [])
    lines.extend(("", "## 수집·분류 참고", "", f"- 포함된 회사 업무: {len(included) if isinstance(included, list) else 0}건"))
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
