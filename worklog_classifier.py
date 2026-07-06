"""Conservative company/personal/uncertain classification for LoroLog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Mapping, Optional


COMPANY_WORK = "company_work"
PERSONAL_WORK = "personal_work"
UNCERTAIN = "uncertain"

COMPANY_KEYWORDS: tuple[str, ...] = (
    "lorotopik", "lorotopick", "loroexam", "topik", "한국어능력시험",
    "sso", "oidc", "oauth", "idp", "redirect uri", "state", "nonce",
    "flutter", "모바일 앱", "모의고사", "학습 앱", "학습앱", "유학생",
    "외국인 학생", "학생 권한", "선생 권한", "교수 권한", "관리자",
    "학생/선생/교수 권한", "학생/선생님/교수 권한",
    "결제", "다운로드", "알림", "e4net", "사업 분석", "사업분석",
    "앱 개발 방향", "경쟁 서비스 분석", "duolingo", "말해보카", "스픽",
)

PERSONAL_KEYWORDS: tuple[str, ...] = (
    "tokenforge", "hwangtodo", "lovey moment", "lovey", "개인 앱",
    "사이드 프로젝트", "side project", "toy project", "toy", "personal",
    "private", "playground", "portfolio", "indie", "macos companion",
    "캐릭터 성장", "todo 앱", "잠금화면 todo",
)

DEFAULT_COMPANY_ALLOWLIST: tuple[str, ...] = (
    "loro", "lorotopik", "lorotopick", "loroexam", "topik", "e4net",
)
DEFAULT_PERSONAL_DENYLIST: tuple[str, ...] = (
    "tokenforge", "hwangtodo", "lovey", "personal", "toy", "side", "playground",
)


@dataclass(frozen=True)
class Classification:
    category: str
    company_signals: tuple[str, ...] = ()
    personal_signals: tuple[str, ...] = ()

    @property
    def reasons(self) -> list[str]:
        reasons = [f"company:{value}" for value in self.company_signals]
        reasons.extend(f"personal:{value}" for value in self.personal_signals)
        return reasons or ["no_strong_signal"]


def _matched_keywords(text: str, keywords: Iterable[str]) -> tuple[str, ...]:
    folded = text.casefold()
    matches: list[str] = []
    for keyword in keywords:
        needle = keyword.casefold()
        if needle.replace(" ", "").isascii() and any(character.isalpha() for character in needle):
            pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
            found = re.search(pattern, folded) is not None
        else:
            found = needle in folded
        if found:
            matches.append(keyword)
    return tuple(matches)


def _path_signals(path: Optional[str], values: Iterable[str]) -> tuple[str, ...]:
    if not path:
        return ()
    folded_parts = {part.casefold() for part in Path(path).parts if part}
    folded_path = str(path).casefold().replace("_", "-")
    matches: list[str] = []
    for value in values:
        needle = value.casefold()
        if needle in folded_parts or needle in folded_path:
            matches.append(f"path:{value}")
    return tuple(matches)


def classify_with_reasons(
    text: str,
    repo_path: Optional[str] = None,
    *,
    company_allowlist: Optional[Iterable[str]] = None,
    personal_denylist: Optional[Iterable[str]] = None,
    company_keyword_hints: Optional[Iterable[str]] = None,
    personal_keyword_hints: Optional[Iterable[str]] = None,
) -> Classification:
    """Classify without ever promoting mixed signals to company work."""

    company_paths = tuple(company_allowlist or DEFAULT_COMPANY_ALLOWLIST)
    personal_paths = tuple(personal_denylist or DEFAULT_PERSONAL_DENYLIST)
    company_keywords = COMPANY_KEYWORDS + tuple(company_keyword_hints or ())
    personal_keywords = PERSONAL_KEYWORDS + tuple(personal_keyword_hints or ())
    company = _matched_keywords(text, company_keywords) + _path_signals(repo_path, company_paths)
    personal = _matched_keywords(text, personal_keywords) + _path_signals(repo_path, personal_paths)

    if company and personal:
        # Privacy wins over recall: a direct personal-project signal is enough to
        # exclude a mixed item.  It must never be promoted to company work.
        category = PERSONAL_WORK
    elif personal:
        category = PERSONAL_WORK
    elif company:
        category = COMPANY_WORK
    else:
        category = UNCERTAIN
    return Classification(category, company, personal)


def classify_item(
    item: str | Mapping[str, object],
    repo_path: Optional[str] = None,
    *,
    company_allowlist: Optional[Iterable[str]] = None,
    personal_denylist: Optional[Iterable[str]] = None,
    company_keyword_hints: Optional[Iterable[str]] = None,
    personal_keyword_hints: Optional[Iterable[str]] = None,
) -> str:
    """Return company_work, personal_work, or uncertain.

    A mapping may be supplied by collectors; only its short textual metadata is
    considered.  File contents are neither required nor inspected.
    """

    if isinstance(item, Mapping):
        selected = (
            item.get("title", ""), item.get("summary", ""), item.get("text", ""),
            item.get("repo_name", ""), " ".join(map(str, item.get("tags", []) or [])),
        )
        text = " ".join(str(value) for value in selected if value)
        repo_path = repo_path or str(item.get("repo_path", "") or item.get("source_path", ""))
    else:
        text = item
    return classify_with_reasons(
        text,
        repo_path,
        company_allowlist=company_allowlist,
        personal_denylist=personal_denylist,
        company_keyword_hints=company_keyword_hints,
        personal_keyword_hints=personal_keyword_hints,
    ).category
