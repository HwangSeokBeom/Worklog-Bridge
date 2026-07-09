import json

from worklog_draft_generator import generate_worklog, render_markdown, write_worklog_files


def _items():
    included = [
        {
            "source_type": "note",
            "summary": "LoroTopik OIDC 인증 구조를 검토했다.",
            "classification": "company_work",
        },
        {
            "source_type": "comment",
            "summary": "TOPIK 모의고사 개선 방향을 정리했다.",
            "classification": "company_work",
        },
    ]
    personal = [
        {
            "source_type": "git",
            "repo_name": "TokenForge",
            "summary": "TokenForge 개인 기능 개발",
            "classification": "personal_work",
        }
    ]
    uncertain = [
        {
            "source_type": "note",
            "summary": "출처가 불명확한 화면 작업",
            "classification": "uncertain",
        }
    ]
    return included, personal, uncertain


def test_only_included_items_feed_fields():
    draft = generate_worklog(*_items(), target_date="2026-07-02")
    rendered_fields = json.dumps(draft["fields"], ensure_ascii=False)
    assert "LoroTopik OIDC" in rendered_fields
    assert "TokenForge" not in rendered_fields
    assert "출처가 불명확" not in rendered_fields
    assert draft["date_range"] == {"since": "2026-07-02", "until": "2026-07-02"}
    assert "short_worklog_draft" in draft["included_items"][0]
    assert draft["privacy_exclusions_summary"]["excluded_count"] == 0


def test_uncertain_is_excluded_from_fields_by_default():
    draft = generate_worklog(*_items(), target_date="2026-07-02")
    assert "출처가 불명확" not in json.dumps(draft["fields"], ensure_ascii=False)


def test_personal_items_are_reduced_to_project_stub():
    draft = generate_worklog(*_items(), target_date="2026-07-02")
    personal = draft["excluded_personal_items"][0]
    assert personal["project_name"] == "TokenForge"
    assert "summary" not in personal


def test_comment_is_reflected_in_comment_field():
    draft = generate_worklog(*_items(), target_date="2026-07-02")
    assert "TOPIK 모의고사 개선 방향" in draft["fields"]["COMMENT"]


def test_markdown_fallback_has_separate_counts():
    draft = generate_worklog(*_items(), target_date="2026-07-02")
    markdown = render_markdown(draft)
    assert "LoroTopik 일일 근무일지" in markdown
    assert "검토가 필요한 uncertain: 1건" in markdown
    assert "제외된 개인 작업: 1건 (TokenForge)" in markdown
    assert "TokenForge 개인 기능 개발" not in markdown


def test_markdown_and_json_files_are_written(tmp_path):
    draft = generate_worklog(*_items(), target_date="2026-07-02")
    json_path, markdown_path = write_worklog_files(draft, tmp_path / "daily.json")
    assert json_path.exists()
    assert markdown_path.exists()
    assert "LoroTopik" in markdown_path.read_text(encoding="utf-8")


def test_uncertain_can_only_enter_fields_when_explicitly_enabled():
    draft = generate_worklog(*_items(), target_date="2026-07-02", include_uncertain=True)
    assert "출처가 불명확" in json.dumps(draft["fields"], ensure_ascii=False)
    assert draft["included_policy"] == "company_work_plus_explicit_uncertain"


def test_zero_activity_diagnostics_stay_out_of_hwp_fields():
    draft = generate_worklog(
        [],
        target_date="2026-07-03",
        date_range=("2026-07-03", "2026-07-03"),
        collected_item_count=0,
    )
    collection = draft["collection_summary"]
    assert collection["collected_item_count"] == 0
    assert collection["included_company_work_count"] == 0
    assert collection["git_activity_status"] == "EXPECTED_ZERO_ACTIVITY"
    rendered_fields = json.dumps(draft["fields"], ensure_ascii=False)
    assert "Git 활동이 발견되지 않았습니다" not in rendered_fields
    assert "포함된 회사 업무는 0건" not in rendered_fields
    assert "EXPECTED_ZERO_ACTIVITY" not in rendered_fields
    assert "해당 사항 없음" not in rendered_fields
    markdown = render_markdown(draft)
    assert "Git 활동 상태: EXPECTED_ZERO_ACTIVITY (0건)" in markdown
    assert "포함된 회사 업무: 0건" in markdown
