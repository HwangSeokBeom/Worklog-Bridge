import json

from mac_collect_lorotopik_worklog import build_parser
from worklog_draft_generator import generate_worklog


def _company_activity(summary, source_type="activity_file"):
    return {
        "source_type": source_type,
        "summary": summary,
        "classification": "company_work",
    }


def test_lorotopik_activities_become_one_natural_hwp_paragraph():
    items = [
        _company_activity("LoroTOPIK 서비스 전체 구조 파악", "manual_task"),
        _company_activity(
            "모바일 앱 로그인 버튼 이후 모의고사 서비스 로그인 화면 이동 흐름 검토"
        ),
        _company_activity("SSO, IdP, OIDC, redirect URI, state, nonce 학습", "note"),
        _company_activity("앱, 웹 로그인 페이지, 인증 서버, 외부 IdP 역할 정리", "comment"),
    ]

    draft = generate_worklog(items, target_date="2026-07-06")
    content = draft["fields"]["WORK_CONTENT"]

    assert "\n" not in content
    assert "## 업무 요약" not in content
    assert "-" not in content
    assert "Git 활동이 발견되지 않았습니다" not in content
    assert "해당 사항 없음" not in content
    for keyword in (
        "LoroTOPIK", "로그인", "SSO", "IdP", "OIDC", "redirect URI", "state", "nonce"
    ):
        assert keyword in content
    assert "IdP의의" not in content
    assert "외부 IdP의 역할" in content
    assert 2 <= content.count(".") <= 3
    assert draft["fields"]["SUMMARY"] == content
    assert draft["draft_style"] == "paragraph"


def test_manual_and_activity_file_work_generate_content_without_git_activity():
    items = [
        _company_activity("LoroTOPIK 서비스 구조 파악", "manual_task"),
        _company_activity("모바일 로그인 흐름 검토", "activity_file"),
    ]

    draft = generate_worklog(items, target_date="2026-07-06")

    assert draft["collection_summary"]["git_activity_count"] == 0
    assert "LoroTOPIK" in draft["fields"]["WORK_CONTENT"]
    assert "로그인 흐름을 검토하였다" in draft["fields"]["WORK_CONTENT"]
    assert "Git 활동" not in json.dumps(draft["fields"], ensure_ascii=False)


def test_personal_project_input_never_enters_work_content():
    mixed_items = [
        _company_activity("LoroTOPIK 인증 구조 검토", "manual_task"),
        {
            "source_type": "activity_file",
            "repo_name": "TokenForge",
            "summary": "TokenForge 개인 프로젝트 기능 개발",
        },
    ]

    draft = generate_worklog(mixed_items, target_date="2026-07-06")
    rendered_fields = json.dumps(draft["fields"], ensure_ascii=False)

    assert "LoroTOPIK" in rendered_fields
    assert "TokenForge" not in rendered_fields
    assert draft["excluded_personal_items"][0]["project_name"] == "TokenForge"


def test_cli_draft_style_defaults_to_paragraph_and_allows_sectioned():
    parser = build_parser()
    assert parser.parse_args([]).draft_style == "paragraph"
    assert parser.parse_args(["--draft-style", "sectioned"]).draft_style == "sectioned"

    sectioned = generate_worklog(
        [_company_activity("LoroTOPIK 인증 구조 검토")],
        target_date="2026-07-06",
        draft_style="sectioned",
    )
    assert "## 업무 요약" in sectioned["fields"]["WORK_CONTENT"]
