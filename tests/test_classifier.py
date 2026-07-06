import pytest

from worklog_classifier import COMPANY_WORK, PERSONAL_WORK, UNCERTAIN, classify_item


@pytest.mark.parametrize(
    "text",
    [
        "LoroTopik 앱 구조를 검토했다",
        "SSO와 OIDC 인증 흐름 정리",
        "TOPIK 모의고사 기능 검토",
        "Flutter 학습 앱 개발 방향",
    ],
)
def test_company_keywords(text):
    assert classify_item(text) == COMPANY_WORK


@pytest.mark.parametrize(
    "text",
    ["TokenForge 기능 구상", "HWANGTODO 잠금화면 TODO", "Lovey Moment 앱 작업"],
)
def test_personal_keywords(text):
    assert classify_item(text) == PERSONAL_WORK


def test_mixed_company_and_personal_is_never_company():
    assert classify_item("LoroTopik 아이디어를 TokenForge 개인 앱에 적용") == PERSONAL_WORK


def test_personal_repo_path_overrides_company_term():
    assert classify_item("TOPIK 관련 메모", "/Users/me/src/TokenForge") == PERSONAL_WORK


def test_uncertain_without_strong_signal():
    assert classify_item("회의 내용을 정리하고 화면을 검토했다") == UNCERTAIN


def test_company_allowlisted_repo_can_classify_generic_commit():
    assert classify_item("문구 수정", "/Users/me/src/LoroTopik") == COMPANY_WORK
