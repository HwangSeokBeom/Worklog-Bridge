import pytest

from privacy_guard import REDACTED_EMAIL, contains_sensitive_info, sanitize_line, sanitize_text
from mac_collect_lorotopik_worklog import _safe_path


@pytest.mark.parametrize(
    "unsafe",
    [
        "api_key=sk-abcdefghijklmnopqrstuvwxyz123456",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYSJ9.abcdefghijklmno",
        ".env API_URL=https://internal.example.test/path",
        "password=do-not-store-this",
        "client_secret: do-not-store-this",
        "private_key=do-not-store-this",
        "diff --git a/app.py b/app.py",
        "@@ -1,2 +1,2 @@",
    ],
)
def test_sensitive_lines_are_excluded(unsafe):
    assert sanitize_line(unsafe) is None
    assert unsafe not in sanitize_text(unsafe)


def test_code_fence_body_is_excluded():
    text = "업무를 검토했다\n```python\nprint('source body')\n```\n정리했다"
    safe = sanitize_text(text)
    assert "source body" not in safe
    assert "업무를 검토했다" in safe


def test_email_and_phone_are_masked():
    safe = sanitize_line("담당자 user@example.com / 010-1234-5678")
    assert safe is not None
    assert REDACTED_EMAIL in safe
    assert "010-1234-5678" not in safe


def test_url_keeps_domain_only():
    safe = sanitize_line("확인 https://intranet.example.com/private/path?token=nope")
    assert safe is not None
    assert "intranet.example.com" in safe
    assert "private/path" not in safe


def test_env_paths_are_excluded_from_git_metadata():
    assert _safe_path(".env") is None
    assert _safe_path("config/.env.production") is None
    assert _safe_path("src/app.py") == "src/app.py"


def test_contains_sensitive_info():
    assert contains_sensitive_info("normal\nrefresh_token=abc")
