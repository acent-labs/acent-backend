"""연속 배치 업로드 실행기.

예)
    python scripts/run_sequential_upload.py \\
        --store-id fileSearchStores/freshworkskb20251121-kty2cqb5xugr \\
        --display-name "freshworks-kb-20251121" \\
        --batches 6 \\
        --limit 1000 \\
        --concurrency 5 \\
        --wipe-first \\
        --create-if-missing

첫 배치 이후에는 자동으로 resume 정보를 전달하여 다음 배치를 이어서 실행합니다.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from scripts.reupload_with_metadata import parse_columns_arg, reupload  # noqa: E402


async def run_batches(args: argparse.Namespace) -> None:
    resume_updated_at = args.resume_updated_at
    resume_id = args.resume_id
    for idx in range(args.batches):
        human_idx = idx + 1
        attempt = 0
        while True:
            attempt += 1
            try:
                print(f"=== Batch {human_idx}/{args.batches} 시작 (시도 {attempt}/{args.batch_retries}) ===")
                cursor = await reupload(
                    store_id=args.store_id,
                    display_name=args.display_name,
                    limit=args.limit,
                    product=args.product,
                    dry_run=args.dry_run,
                    create_if_missing=args.create_if_missing if idx == 0 else False,
                    wipe_first=(args.wipe_first and idx == 0 and not resume_updated_at and attempt == 1),
                    resume_updated_at=resume_updated_at,
                    resume_id=resume_id,
                    concurrency=args.concurrency,
                    upload_timeout=args.upload_timeout,
                    upload_attempts=args.upload_attempts,
                    columns=args.columns,
                    skip_count=args.skip_count,
                )
                if not cursor:
                    print("업로드할 문서가 더 이상 없습니다. 작업을 종료합니다.")
                    return
                resume_updated_at = cursor.updated_at
                resume_id = cursor.id
                print(
                    f"Batch {human_idx} 완료. 다음 시작점: "
                    f"--resume-updated-at {resume_updated_at} --resume-id {resume_id}"
                )
                break
            except Exception as exc:  # pylint: disable=broad-except
                if attempt >= args.batch_retries:
                    print(f"Batch {human_idx} 실패: {exc}")
                    raise
                wait_seconds = args.batch_retry_wait * attempt
                print(f"Batch {human_idx} 실패, {wait_seconds}s 후 재시도: {exc}")
                await asyncio.sleep(wait_seconds)
    print("지정된 배치를 모두 실행했습니다.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini 배치 업로드 연속 실행기")
    parser.add_argument("--store-id", required=True, help="File Search Store 이름")
    parser.add_argument("--display-name", help="스토어 표시 이름")
    parser.add_argument("--limit", type=int, default=1000, help="배치당 문서 수")
    parser.add_argument("--concurrency", type=int, default=5, help="동시 업로드 수")
    parser.add_argument("--batches", type=int, default=6, help="실행할 배치 수")
    parser.add_argument(
        "--upload-timeout",
        type=float,
        default=120.0,
        help="단일 업로드 HTTP 타임아웃(초)",
    )
    parser.add_argument(
        "--upload-attempts",
        type=int,
        default=5,
        help="업로드 재시도 횟수",
    )
    parser.add_argument(
        "--batch-retries",
        type=int,
        default=3,
        help="배치 단위 재시도 횟수",
    )
    parser.add_argument(
        "--batch-retry-wait",
        type=float,
        default=10.0,
        help="배치 재시도 기본 대기(초). 시도마다 배로 늘어남",
    )
    parser.add_argument("--product", help="특정 product 만 업로드")
    parser.add_argument("--dry-run", action="store_true", help="실제 업로드 없이 확인만")
    parser.add_argument(
        "--create-if-missing",
        action="store_true",
        help="스토어가 없으면 자동 생성",
    )
    parser.add_argument(
        "--wipe-first",
        action="store_true",
        help="첫 배치 전에 스토어 문서를 모두 삭제",
    )
    parser.add_argument(
        "--resume-updated-at",
        help="기존 배치 이후 이어서 시작할 updated_at 값",
    )
    parser.add_argument("--resume-id", help="기존 배치 이후 이어서 시작할 문서 ID")
    parser.add_argument(
        "--columns",
        help="Supabase에서 읽어올 컬럼을 콤마로 지정 (기본은 전체)",
    )
    parser.add_argument(
        "--skip-count",
        type=int,
        default=0,
        help="선행 문서를 건너뛰고 업로드 (병렬 실행 시 사용)",
    )
    args = parser.parse_args()
    args.columns = parse_columns_arg(args.columns)
    return args


def main() -> None:
    args = parse_args()
    asyncio.run(run_batches(args))


if __name__ == "__main__":
    main()
