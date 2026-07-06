import builtins
import json
from pathlib import Path

import pytest

from windows_fill_hwp import (
    ensure_distinct_template_output,
    load_fields,
    print_template_field_report,
    validate_hwp_output_path,
    validate_template_fields,
)
from worklog_draft_generator import FIELD_NAMES


def test_same_template_and_output_path_is_rejected(tmp_path):
    template = tmp_path / "template.hwp"
    with pytest.raises(ValueError, match="원본"):
        ensure_distinct_template_output(template, template)


def test_template_field_report_requires_every_field(capsys):
    result = print_template_field_report(set(FIELD_NAMES) - {"COMMENT"}, "test")
    captured = capsys.readouterr().out
    assert result == 3
    assert '"COMMENT"' in captured
    assert '"output_written": false' in captured


def test_template_field_report_accepts_complete_template(capsys):
    result = print_template_field_report(set(FIELD_NAMES), "test")
    captured = capsys.readouterr().out
    assert result == 0
    assert '"template_valid": true' in captured


def test_windows_com_unavailable_fails_clearly(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "pyhwpx" or name.startswith("win32com"):
            raise ImportError(f"unavailable: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(RuntimeError, match="pyhwpx.*win32com"):
        validate_template_fields(tmp_path / "template.hwp", visible=False)


def test_hwp_dry_run_input_requires_all_fields(tmp_path):
    json_path = tmp_path / "incomplete.json"
    json_path.write_text(json.dumps({"fields": {"DATE": "2026-07-02"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="필수 필드"):
        load_fields(json_path)


def test_hwp_output_rejects_credential_path(tmp_path):
    with pytest.raises(ValueError, match="unsafe path"):
        validate_hwp_output_path(tmp_path / "secrets" / "filled.hwp")
