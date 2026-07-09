import json
import os
import subprocess
from pathlib import Path

from mac_collect_lorotopik_worklog import (
    CollectorConfig,
    classify_collected,
    collect_configured_sources,
    resolve_period,
)
from worklog_draft_generator import generate_worklog, write_worklog_files
from windows_fill_hwp import load_fields
from gmail_delivery import GmailDeliveryConfig, build_email_payload


def _manual_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "manual"
    directory.mkdir()
    return directory


def _write_manual(directory: Path, date_value: str, body: str) -> Path:
    path = directory / f"{date_value}.md"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def _draft_from_config(config: CollectorConfig, date_value: str):
    since, until = resolve_period("daily", date_value, date_value)
    all_items, warnings = collect_configured_sources(config, since, until)
    assert warnings == []
    included, personal, uncertain = classify_collected(
        all_items,
        company_keyword_hints=config.company_keyword_hints,
        personal_keyword_hints=config.personal_exclude_hints,
    )
    return generate_worklog(
        included,
        personal,
        uncertain,
        target_date=date_value,
        date_range=(since.isoformat(), until.isoformat()),
        collected_item_count=len(all_items),
    )


def _make_git_repo(path: Path, *, message: str, date_value: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("fixture", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = f"{date_value}T10:00:00+09:00"
    env["GIT_COMMITTER_DATE"] = f"{date_value}T10:00:00+09:00"
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", message], env=env, check=True)
    return path


def test_git_zero_with_manual_activity_generates_meaningful_paragraph(tmp_path):
    manual = _manual_dir(tmp_path)
    _write_manual(
        manual,
        "2026-07-08",
        """
Date: 2026-07-08

오늘은 LoroTOPIK 서비스의 운영 인수인계 관점에서 HWP 근무일지 자동화 흐름을 점검하였다.
Windows 환경에서 Gmail 첨부 JSON을 받아 HWP 주차별 양식에 자동 입력되는지 확인하고, clean 템플릿 서식과 작업 스케줄러 실행 흐름을 검증하였다.
또한 Git 활동이 없는 날에도 수동 업무 기록을 기반으로 일지가 생성될 수 있도록 Mac 수집기 개선 방향을 정리하였다.
""",
    )
    config = CollectorConfig(manual_activity_dirs=[manual])

    draft = _draft_from_config(config, "2026-07-08")
    content = draft["fields"]["WORK_CONTENT"]

    assert draft["collection_summary"]["git_activity_count"] == 0
    assert "해당 사항 없음" not in json.dumps(draft["fields"], ensure_ascii=False)
    assert "Git 활동이 발견되지 않았습니다" not in content
    assert "HWP 근무일지 자동화 흐름을 점검하였다" in content
    assert "작업 스케줄러 실행 흐름을 검증하였다" in content
    assert "Mac 수집기 개선 방향을 정리하였다" in content
    assert 2 <= content.count(".") <= 3


def test_git_zero_without_any_source_allows_explicit_no_activity_draft():
    draft = generate_worklog(
        [],
        target_date="2026-07-08",
        date_range=("2026-07-08", "2026-07-08"),
        collected_item_count=0,
    )

    assert draft["collection_summary"]["git_activity_count"] == 0
    assert draft["fields"]["WORK_CONTENT"] == "선택한 기간에 수집된 업무 활동이 없어 근무일지 초안을 생성하지 않았다."
    assert "해당 사항 없음" not in json.dumps(draft["fields"], ensure_ascii=False)


def test_git_and_manual_activity_are_both_considered_with_paragraph_output(tmp_path):
    manual = _manual_dir(tmp_path)
    _write_manual(
        manual,
        "2026-07-08",
        """
LoroTOPIK 운영 인수인계 관점에서 HWP 자동화 흐름을 점검하였다.
Windows Gmail JSON 첨부 수신 흐름을 확인하였다.
clean 템플릿 서식 적용 결과를 검토하였다.
작업 스케줄러 실행 조건을 정리하였다.
Mac 수집기 수동 활동 경로를 확인하였다.
일일 업무 기록 작성 흐름을 정리하였다.
""",
    )
    repo = _make_git_repo(
        tmp_path / "LoroTOPIK",
        message="LoroTOPIK Windows HWP 입력 검증",
        date_value="2026-07-08",
    )
    config = CollectorConfig(repos=[repo], manual_activity_dirs=[manual])

    draft = _draft_from_config(config, "2026-07-08")
    content = draft["fields"]["WORK_CONTENT"]

    assert draft["collection_summary"]["git_activity_count"] == 1
    assert "HWP 자동화 흐름을 점검하였" in content
    assert "Windows HWP 입력을 검증하였다" in content
    assert "## 업무 요약" not in content
    assert "\n-" not in content


def test_git_activity_without_manual_still_generates_readable_paragraph(tmp_path):
    repo = _make_git_repo(
        tmp_path / "LoroTOPIK",
        message="LoroTOPIK Windows HWP 입력 검증",
        date_value="2026-07-08",
    )
    config = CollectorConfig(repos=[repo])

    draft = _draft_from_config(config, "2026-07-08")
    content = draft["fields"]["WORK_CONTENT"]

    assert draft["collection_summary"]["git_activity_count"] == 1
    assert "LoroTOPIK Windows HWP 입력" in content
    assert "검증하였다" in content
    assert "Git 활동" not in content
    assert "해당 사항 없음" not in content


def test_personal_project_manual_note_is_excluded(tmp_path):
    manual = _manual_dir(tmp_path)
    _write_manual(
        manual,
        "2026-07-08",
        "TokenForge 개인 프로젝트 기능 개발",
    )
    config = CollectorConfig(
        manual_activity_dirs=[manual],
        personal_exclude_hints=("TokenForge",),
    )

    draft = _draft_from_config(config, "2026-07-08")
    rendered_fields = json.dumps(draft["fields"], ensure_ascii=False)

    assert "TokenForge" not in rendered_fields
    assert draft["excluded_personal_items"][0]["project_name"] == "TokenForge"


def test_git_diagnostic_message_stays_out_of_manual_hwp_content(tmp_path):
    manual = _manual_dir(tmp_path)
    _write_manual(manual, "2026-07-08", "LoroTOPIK 수동 업무 기록 기반 일지 생성을 검토하였다.")
    config = CollectorConfig(manual_activity_dirs=[manual])

    draft = _draft_from_config(config, "2026-07-08")

    assert draft["collection_summary"]["git_activity_status"] == "EXPECTED_ZERO_ACTIVITY"
    assert "Git 활동이 발견되지 않았습니다" not in draft["fields"]["WORK_CONTENT"]
    assert "EXPECTED_ZERO_ACTIVITY" not in draft["fields"]["WORK_CONTENT"]


def test_manual_activity_json_remains_windows_and_gmail_compatible(tmp_path):
    manual = _manual_dir(tmp_path)
    _write_manual(manual, "2026-07-08", "LoroTOPIK HWP 필드 입력 JSON 구조를 검증하였다.")
    config = CollectorConfig(manual_activity_dirs=[manual])
    draft = _draft_from_config(config, "2026-07-08")

    json_path, markdown_path = write_worklog_files(draft, tmp_path / "daily_worklog_2026-07-08.json")
    fields = load_fields(json_path)
    prepared = build_email_payload(markdown_path, GmailDeliveryConfig())

    assert fields["WORK_CONTENT"] == draft["fields"]["WORK_CONTENT"]
    assert fields["SUMMARY"] == fields["WORK_CONTENT"]
    assert "Summary:\n해당 사항 없음" not in prepared.body
    assert "LoroTOPIK HWP 필드 입력 JSON 구조를 검증하였다." in prepared.body


def test_gmail_summary_prefers_work_content_over_legacy_summary(tmp_path):
    markdown = tmp_path / "daily_worklog_2026-07-08.md"
    markdown.write_text("# Daily\n", encoding="utf-8")
    markdown.with_suffix(".json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-08T17:00:00+09:00",
                "mode": "daily",
                "date": "2026-07-08",
                "collection_summary": {
                    "git_activity_status": "EXPECTED_ZERO_ACTIVITY",
                    "message": "선택한 수집 기간에 Git 활동이 발견되지 않았습니다.",
                },
                "fields": {
                    "DATE": "2026-07-08",
                    "WEEK_RANGE": "2026.07.06 ~ 2026.07.10",
                    "WORK_CONTENT": "LoroTOPIK 수동 업무 기록을 기반으로 근무 내용을 정리하였다.",
                    "SUMMARY": "오래된 요약",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    prepared = build_email_payload(markdown, GmailDeliveryConfig())

    assert "Summary:\nLoroTOPIK 수동 업무 기록을 기반으로 근무 내용을 정리하였다." in prepared.body
    assert "Summary:\n오래된 요약" not in prepared.body
