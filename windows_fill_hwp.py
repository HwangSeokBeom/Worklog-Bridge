#!/usr/bin/env python3
"""Fill an HWP template on Windows from a Worklog Bridge JSON draft.

pyhwpx is attempted first.  win32com is the compatibility fallback.  This file
does not edit HWP documents on macOS and never overwrites the source template.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

from privacy_guard import sanitize_text
from worklog_draft_generator import FIELD_NAMES


def parse_bool(value: str) -> bool:
    lowered = value.casefold()
    if lowered in {"true", "1", "yes", "y"}:
        return True
    if lowered in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("true 또는 false를 사용하세요.")


def load_fields(json_path: Path) -> dict[str, str]:
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"JSON 파일을 찾을 수 없습니다: {json_path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON 파일을 읽을 수 없습니다 ({json_path}): {exc}") from exc
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, Mapping):
        raise ValueError("입력 JSON에 fields 객체가 없습니다.")
    missing = [name for name in FIELD_NAMES if name not in raw_fields]
    if missing:
        raise ValueError(f"입력 JSON fields에 필수 필드가 없습니다: {', '.join(missing)}")
    fields: dict[str, str] = {}
    for name in FIELD_NAMES:
        raw = raw_fields.get(name, "")
        fields[name] = sanitize_text(str(raw), max_lines=200, max_length=1000)
    return fields


def _split_field_names(value: object) -> set[str]:
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        result: set[str] = set()
        for entry in value:
            result.update(_split_field_names(entry))
        return result
    normalized = str(value).replace("\x02", "\n")
    return {part.split("{{", 1)[0].strip() for part in normalized.splitlines() if part.strip()}


def _warn_missing(existing: set[str], fields: Mapping[str, str]) -> None:
    if not existing:
        print("경고: 템플릿 필드 목록을 조회하지 못해 입력을 계속합니다.", file=sys.stderr)
        return
    for name in fields:
        if name not in existing:
            print(f"경고: 템플릿에 필드가 없습니다: {name}", file=sys.stderr)


def ensure_distinct_template_output(template: Path, output: Path) -> None:
    template_key = os.path.normcase(str(template.expanduser().resolve()))
    output_key = os.path.normcase(str(output.expanduser().resolve()))
    if template_key == output_key:
        raise ValueError("--output은 원본 --template과 다른 경로여야 합니다.")


def validate_hwp_output_path(output: Path) -> None:
    resolved = output.expanduser().resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError("--output은 filesystem root일 수 없습니다.")
    unsafe_parts = {
        ".git", ".ssh", "secrets", "credentials", "windows", "system32", "program files",
    }
    if any(part.casefold() in unsafe_parts for part in resolved.parts):
        raise ValueError("--output은 system/credential/source 관련 unsafe path에 둘 수 없습니다.")
    if resolved.suffix.casefold() != ".hwp":
        raise ValueError("--output 확장자는 .hwp여야 합니다.")


def list_fields_with_pyhwpx(template: Path, *, visible: bool) -> set[str]:
    from pyhwpx import Hwp  # type: ignore[import-not-found]

    hwp = None
    try:
        hwp = Hwp(visible=visible)
        opened = hwp.open(str(template))
        if opened is False:
            raise RuntimeError("pyhwpx가 템플릿 열기에 실패했습니다.")
        try:
            return _split_field_names(hwp.get_field_list())
        except Exception as exc:
            raise RuntimeError(f"pyhwpx가 필드 목록을 조회하지 못했습니다: {exc}") from exc
    finally:
        if hwp is not None:
            try:
                hwp.quit()
            except Exception:
                pass


def list_fields_with_win32com(template: Path, *, visible: bool) -> set[str]:
    import win32com.client  # type: ignore[import-not-found]

    hwp = None
    try:
        hwp = win32com.client.gencache.EnsureDispatch("HWPFrame.HwpObject")
        try:
            hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModuleExample")
        except Exception as exc:
            print(f"경고: RegisterModule 사용 불가: {exc}", file=sys.stderr)
        hwp.XHwpWindows.Item(0).Visible = visible
        opened = hwp.Open(str(template), "HWP", "forceopen:true")
        if opened is False:
            raise RuntimeError("win32com이 템플릿 열기에 실패했습니다.")
        try:
            return _split_field_names(hwp.GetFieldList())
        except Exception as exc:
            raise RuntimeError(f"win32com이 필드 목록을 조회하지 못했습니다: {exc}") from exc
    finally:
        if hwp is not None:
            try:
                hwp.Quit()
            except Exception:
                pass


def validate_template_fields(template: Path, *, visible: bool) -> tuple[set[str], str]:
    errors: list[str] = []
    try:
        return list_fields_with_pyhwpx(template, visible=visible), "pyhwpx"
    except Exception as exc:
        errors.append(f"pyhwpx: {exc}")
    try:
        return list_fields_with_win32com(template, visible=visible), "win32com"
    except Exception as exc:
        errors.append(f"win32com: {exc}")
    raise RuntimeError("; ".join(errors))


def print_template_field_report(detected: set[str], backend: str) -> int:
    required = set(FIELD_NAMES)
    missing = required - detected
    report = {
        "backend": backend,
        "detected_fields": sorted(detected),
        "required_fields": list(FIELD_NAMES),
        "missing_required_fields": sorted(missing),
        "unexpected_fields": sorted(detected - required),
        "template_valid": not missing,
        "output_written": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not missing else 3


def fill_with_pyhwpx(
    template: Path, output: Path, fields: Mapping[str, str], *, visible: bool
) -> None:
    from pyhwpx import Hwp  # type: ignore[import-not-found]

    hwp = None
    try:
        hwp = Hwp(visible=visible)
        opened = hwp.open(str(template))
        if opened is False:
            raise RuntimeError("pyhwpx가 템플릿 열기에 실패했습니다.")
        try:
            existing = _split_field_names(hwp.get_field_list())
        except Exception:
            existing = set()
        _warn_missing(existing, fields)
        for name, value in fields.items():
            if existing and name not in existing:
                continue
            try:
                hwp.put_field_text(field=name, text=value)
            except TypeError:
                # Compatibility with releases exposing positional-only wrappers.
                hwp.put_field_text(name, value)
            except Exception as exc:
                print(f"경고: {name} 필드 입력 실패: {exc}", file=sys.stderr)
        saved = hwp.save_as(str(output))
        if saved is False or not output.exists():
            raise RuntimeError("pyhwpx가 output 파일 저장에 실패했습니다.")
    finally:
        if hwp is not None:
            try:
                hwp.quit()
            except Exception:
                pass


def fill_with_win32com(
    template: Path, output: Path, fields: Mapping[str, str], *, visible: bool
) -> None:
    import win32com.client  # type: ignore[import-not-found]

    hwp = None
    try:
        hwp = win32com.client.gencache.EnsureDispatch("HWPFrame.HwpObject")
        try:
            registered = hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModuleExample")
            if registered is False:
                print(
                    "경고: RegisterModule이 거부되었습니다. 한글 보안 경고가 나타날 수 있습니다.",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"경고: RegisterModule 사용 불가: {exc}", file=sys.stderr)
        hwp.XHwpWindows.Item(0).Visible = visible
        opened = hwp.Open(str(template), "HWP", "forceopen:true")
        if opened is False:
            raise RuntimeError("win32com이 템플릿 열기에 실패했습니다.")
        try:
            existing = _split_field_names(hwp.GetFieldList())
        except Exception:
            existing = set()
        _warn_missing(existing, fields)
        for name, value in fields.items():
            if existing and name not in existing:
                continue
            try:
                hwp.PutFieldText(name, value)
            except Exception as exc:
                print(f"경고: {name} 필드 입력 실패: {exc}", file=sys.stderr)
        saved = hwp.SaveAs(str(output), "HWP", "")
        if saved is False or not output.exists():
            raise RuntimeError("win32com이 output 파일 저장에 실패했습니다.")
    finally:
        if hwp is not None:
            try:
                hwp.Quit()
            except Exception:
                pass


def fallback_text(fields: Mapping[str, str]) -> str:
    labels = {
        "DATE": "날짜", "WEEK_RANGE": "주간 범위", "SUMMARY": "업무 요약",
        "TASKS": "수행 업무", "BUSINESS_ANALYSIS": "사업 분석", "APP_DIRECTION": "앱 개발 방향",
        "DEV_WORK": "개발 업무", "LEARNINGS": "학습/파악 내용", "DIFFICULTIES": "어려웠던 점",
        "NEXT_PLAN": "다음 계획", "COMMENT": "코멘트",
    }
    lines = ["LoroTopik 근무일지 붙여넣기용 fallback", ""]
    for name in FIELD_NAMES:
        lines.extend((f"[{labels[name]} / {name}]", fields.get(name, "") or "해당 사항 없음", ""))
    return "\n".join(lines)


def write_fallback(output: Path, fields: Mapping[str, str]) -> Path:
    fallback = output.with_suffix(".txt")
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text(fallback_text(fields), encoding="utf-8")
    return fallback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Windows 한글 HWP 템플릿에 근무일지 fields를 입력합니다.")
    parser.add_argument("--json", type=Path, dest="json_path")
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--visible", type=parse_bool, default=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--validate-template-fields",
        action="store_true",
        help="템플릿 필드 목록만 조회·비교하고 output을 쓰지 않음",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    template = args.template.expanduser().resolve()
    if args.validate_template_fields:
        if args.dry_run:
            parser.error("--validate-template-fields는 실제 템플릿을 열어야 하므로 --dry-run과 함께 쓸 수 없습니다.")
        if os.name != "nt":
            parser.error("HWP 템플릿 필드 검증은 Windows + 한컴오피스 설치 환경에서만 실행할 수 있습니다.")
        if not template.is_file():
            parser.error(f"HWP 템플릿을 찾을 수 없습니다: {template}")
        try:
            detected, backend = validate_template_fields(template, visible=args.visible)
        except RuntimeError as exc:
            print(f"HWP 템플릿 필드 검증 실패: {exc}", file=sys.stderr)
            return 2
        return print_template_field_report(detected, backend)

    if args.json_path is None:
        parser.error("일반 입력 모드에는 --json이 필요합니다.")
    if args.output is None:
        parser.error("일반 입력 모드에는 --output이 필요합니다.")
    try:
        fields = load_fields(args.json_path.expanduser())
    except ValueError as exc:
        parser.error(str(exc))
    output = args.output.expanduser().resolve()
    try:
        ensure_distinct_template_output(template, output)
        validate_hwp_output_path(output)
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run:
        print("Windows HWP dry-run")
        print(f"json_valid: true ({len(fields)} required fields)")
        print(f"template_path: {template}")
        print(f"output_path: {output}")
        print("template_output_distinct: true")
        print("hancom_opened: false")
        print("files_written: false")
        return 0
    if os.name != "nt":
        parser.error("HWP 자동 입력은 Windows + 한컴오피스 설치 환경에서만 실행할 수 있습니다.")
    if not template.is_file():
        fallback = write_fallback(output, fields)
        print(f"HWP 템플릿을 찾을 수 없습니다: {template}", file=sys.stderr)
        print(f"붙여넣기용 텍스트를 생성했습니다: {fallback}")
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    try:
        fill_with_pyhwpx(template, output, fields, visible=args.visible)
        print(f"HWP 생성 완료 (pyhwpx): {output}")
        return 0
    except Exception as exc:
        errors.append(f"pyhwpx: {exc}")
        print(f"경고: pyhwpx 실패, win32com fallback을 시도합니다: {exc}", file=sys.stderr)
    try:
        fill_with_win32com(template, output, fields, visible=args.visible)
        print(f"HWP 생성 완료 (win32com): {output}")
        return 0
    except Exception as exc:
        errors.append(f"win32com: {exc}")

    fallback = write_fallback(output, fields)
    print("HWP 자동 입력에 실패했습니다.", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    print(f"붙여넣기용 텍스트를 생성했습니다: {fallback}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
