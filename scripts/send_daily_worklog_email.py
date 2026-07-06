#!/usr/bin/env python3
"""Send one generated daily worklog through Gmail SMTP SSL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gmail_delivery import (  # noqa: E402
    FAILED,
    DeliveryError,
    deliver_daily_worklog,
    load_delivery_runtime_config,
    resolve_daily_markdown,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="생성된 daily worklog를 Gmail SMTP SSL로 전송합니다."
    )
    parser.add_argument("--config", required=True, type=Path, help="config.local.json 경로")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--date", help="daily worklog 날짜 (YYYY-MM-DD)")
    selection.add_argument("--latest", action="store_true", help="outbox의 최신 daily worklog 사용")
    parser.add_argument("--dry-run", action="store_true", help="파일과 email payload만 검증하고 전송하지 않음")
    parser.add_argument("--force", action="store_true", help="동일 Markdown SHA-256 dedupe를 우회")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings, outbox_dir, log_dir = load_delivery_runtime_config(args.config)
        markdown_path = resolve_daily_markdown(
            outbox_dir, date_value=args.date, latest=args.latest
        )
        result = deliver_daily_worklog(
            markdown_path,
            settings,
            log_dir,
            dry_run=args.dry_run,
            force=args.force,
        )
    except DeliveryError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    print(f"Gmail delivery status: {result.status}")
    print(f"date: {result.date}")
    print(f"filename: {result.filename}")
    print(f"body_chars: {result.body_chars}")
    print(f"truncated: {str(result.truncated).lower()}")
    print(f"dedupe_hit: {str(result.dedupe_hit).lower()}")
    print(
        "smtp_connection_attempted: "
        + str(result.smtp_connection_attempted).lower()
    )
    if result.smtp_response is not None:
        print(f"smtp_response: {result.smtp_response}")
    if result.credential_source is not None:
        print(f"credentials_loaded_from: {result.credential_source}")
    if result.metadata_path is not None:
        print(f"delivery_metadata: {result.metadata_path}")
    if result.sanitized_error:
        print(f"오류: {result.sanitized_error}", file=sys.stderr)
    return 2 if result.status == FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
