"""Privacy-first text filtering for Worklog Bridge.

The guard deliberately prefers dropping a line over trying to retain potentially
sensitive material.  It is used both while collecting and immediately before
rendering output (defence in depth).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlsplit


REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_PHONE = "[REDACTED_PHONE]"

_DROP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:sk|pk|rk|api)[_-][A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(
        r"\b(?:access_token|refresh_token|password|passwd|client_secret|private_key|api[_ -]?key)\b\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:export\s+)?[A-Z][A-Z0-9_]{1,}\s*=\s*\S+"),
    re.compile(r"^\s*(?:diff --git|index [0-9a-f]+\.\.[0-9a-f]+|@@\s+-\d|\+\+\+\s+[ab]/|---\s+[ab]/)"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"^\s*(?:def|class|import|from\s+\S+\s+import|function|const|let|var)\b", re.IGNORECASE),
)

_SENSITIVE_WORD = re.compile(
    r"\b(?:access_token|refresh_token|password|passwd|secret|client_secret|private_key|api[_ -]?key)\b",
    re.IGNORECASE,
)
_ENV_FILE = re.compile(r"(?:^|[/\\\s])\.env(?:\.[A-Za-z0-9_-]+)?(?:$|\s)", re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(
    r"(?<!\d)(?:\+?82[- .]?)?(?:0?1[016789]|0[2-6][1-5]?)[- .]?\d{3,4}[- .]?\d{4}(?!\d)"
)
_URL = re.compile(r"https?://[^\s<>\]\[)\"']+", re.IGNORECASE)


def is_sensitive_line(line: str) -> bool:
    """Return True when a complete line must be discarded."""

    stripped = line.strip()
    if not stripped:
        return False
    if _ENV_FILE.search(stripped) or _SENSITIVE_WORD.search(stripped):
        return True
    return any(pattern.search(stripped) for pattern in _DROP_PATTERNS)


def _domain_only(match: re.Match[str]) -> str:
    raw = match.group(0).rstrip(".,;:")
    punctuation = match.group(0)[len(raw) :]
    try:
        parsed = urlsplit(raw)
        if not parsed.hostname:
            return "[REDACTED_URL]" + punctuation
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}/[REDACTED_PATH]{punctuation}"
    except (TypeError, ValueError):
        return "[REDACTED_URL]" + punctuation


def sanitize_line(line: str, *, max_length: int = 500) -> Optional[str]:
    """Sanitize one line, returning None when it is unsafe to retain."""

    line = line.replace("\x00", "").strip()
    if not line or is_sensitive_line(line):
        return None
    safe = _EMAIL.sub(REDACTED_EMAIL, line)
    safe = _PHONE.sub(REDACTED_PHONE, safe)
    safe = _URL.sub(_domain_only, safe)
    safe = re.sub(r"\s+", " ", safe).strip()
    if len(safe) > max_length:
        safe = safe[: max_length - 1].rstrip() + "…"
    return safe or None


def sanitize_text(text: str, *, max_lines: int = 50, max_length: int = 500) -> str:
    """Remove unsafe lines and code/diff fences from a block of text."""

    output: list[str] = []
    in_code_fence = False
    in_diff = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if re.match(r"^(?:diff --git|index [0-9a-f]+\.\.[0-9a-f]+|@@\s+-\d)", stripped):
            in_diff = True
            continue
        if in_diff:
            # A pasted patch has no reliable line-level end marker. Discard the
            # remainder rather than risk retaining code/file content.
            continue
        safe = sanitize_line(raw_line, max_length=max_length)
        if safe is not None:
            output.append(safe)
        if len(output) >= max_lines:
            break
    return "\n".join(output)


def sanitize_lines(lines: Iterable[str], *, max_lines: int = 50) -> list[str]:
    """Return a bounded list containing only safe, non-empty lines."""

    result: list[str] = []
    for line in lines:
        safe = sanitize_line(line)
        if safe:
            result.append(safe)
        if len(result) >= max_lines:
            break
    return result


def contains_sensitive_info(text: str) -> bool:
    """Conservative predicate useful for validation and tests."""

    return any(is_sensitive_line(line) for line in text.splitlines())
