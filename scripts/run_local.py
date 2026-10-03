#!/usr/bin/env python3
"""Run the scraper from a workstation, without DynamoDB.

    python -m scripts.run_local --year 2025 --local-output ./out
    python -m scripts.run_local --lookback-days 3 --bucket my-bucket --dry-run
"""
from __future__ import annotations

import argparse
import json

from ieso.config import DEFAULT_PREFIX, Config
from ieso.pipeline import run
from utils.logging_setup import setup_logging


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch IESO hourly zonal demand and store it as Parquet")
    p.add_argument("--year", type=int, help="Full-year load. Omit to refresh the last --lookback-days days")
    p.add_argument("--lookback-days", type=int, default=2)
    p.add_argument("--latest-only", action="store_true")
    p.add_argument("--bucket")
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--local-output", help="Directory for local Parquet copies (default ./data when no bucket)")
    p.add_argument("--wide-format", action="store_true", help="One column per zone instead of one row per zone")
    p.add_argument("--timeout-seconds", type=int, default=60)
    p.add_argument("--region-name")
    p.add_argument("--s3-server-side-encryption", help="AES256 or aws:kms")
    p.add_argument("--dry-run", action="store_true", help="Fetch and transform, but write nothing")
    p.add_argument("--log-level", default="INFO")
    a = p.parse_args()

    setup_logging(a.log_level)
    cfg = Config(
        bucket=a.bucket, prefix=a.prefix, local_output=a.local_output or (None if a.bucket else "./data"),
        timeout_seconds=a.timeout_seconds, region_name=a.region_name,
        s3_server_side_encryption=a.s3_server_side_encryption, normalize_long_format=not a.wide_format,
        year=a.year, lookback_days=a.lookback_days, latest_only=a.latest_only, dry_run=a.dry_run,
        log_level=a.log_level,
    )
    print(json.dumps(run(cfg), indent=2, default=str))


if __name__ == "__main__":
    main()
