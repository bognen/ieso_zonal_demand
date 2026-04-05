#!/usr/bin/env python3
"""
Fetch historical IESO hourly zonal demand data and store it as Parquet locally and/or in S3.

Supports both:
- local CLI execution
- AWS Lambda invocation via lambda_handler(event, context)

Example local usage:
    python ieso_zonal_to_s3.py \
        --year 2025 \
        --bucket my-data-bucket \
        --prefix ieso/load/zonal_hourly \
        --local-output ./out

Example Lambda event:
    {
      "year": 2025,
      "bucket": "my-data-bucket",
      "prefix": "ieso/load/zonal_hourly",
      "write_local_copy": false
    }
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
import pandas as pd
import requests

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:
    raise RuntimeError(
        "pyarrow is required. Install it locally or package it in your Lambda container image."
    ) from exc

# Logging configuration for AWS Lambda
# In AWS Lambda, the root logger is often already initialized, so basicConfig may not work.
# We ensure the level is set for the module logger and use a custom setup if needed.
LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").upper() or "INFO"
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

# Set the level for the current logger
logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Ensure output to console (CloudWatch Logs) if not already configured
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Fallback: Also set the level of the root logger to ensure logs from other modules are caught
logging.getLogger().setLevel(LOG_LEVEL)

# Use print as a robust fallback for the very start of the execution in Lambda
print(f"DEBUG_LOG: Loading Lambda module. LOG_LEVEL set to {LOG_LEVEL_STR}.")
logger.info("Logger initialized with level: %s", LOG_LEVEL_STR)

BASE_URL = "https://reports-public.ieso.ca/public/DemandZonal"
BUCKET_NAME = "com.dsa.ieso-project"
DEFAULT_PREFIX = "ieso/load/zonal_hourly"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_DATASET_NAME = "ieso_hourly_zonal_demand"
CURRENT_YEAR = datetime.now(timezone.utc).year
ZONE_COLUMNS = [
    "Northwest",
    "Northeast",
    "Ottawa",
    "East",
    "Toronto",
    "Essa",
    "Bruce",
    "Southwest",
    "Niagara",
    "West",
]


@dataclass
class Config:
    year: int
    bucket: str = BUCKET_NAME
    prefix: str = DEFAULT_PREFIX
    local_output: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    region_name: str | None = None
    dataset_name: str = DEFAULT_DATASET_NAME
    write_local_copy: bool = True
    s3_server_side_encryption: str | None = None
    normalize_long_format: bool = True
    latest_only: bool = False
    yesterday_only: bool = False


def build_report_url(year: int) -> str:
    """Use the annual file for closed years and the rolling file for the current year."""
    if year >= CURRENT_YEAR:
        url = f"{BASE_URL}/PUB_DemandZonal.csv"
    else:
        url = f"{BASE_URL}/PUB_DemandZonal_{year}.csv"
    logger.info("Built report URL for year %d: %s", year, url)
    return url


def fetch_report_csv_text(url: str, timeout_seconds: int) -> str:
    logger.info("Fetching IESO zonal demand report from %s with timeout %d seconds", url, timeout_seconds)
    try:
        response = requests.get(url, timeout=timeout_seconds)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error("Failed to fetch CSV from IESO: %s", str(e))
        raise
    
    text = response.text
    logger.info("Successfully fetched CSV. Length: %d characters, approx %d lines.", len(text), text.count("\n"))
    return text


def _find_header_row(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if line.strip().startswith("Date,Hour,Ontario Demand,"):
            return idx
    raise ValueError("Could not find the CSV header row in the IESO zonal demand report.")


def parse_ieso_zonal_csv(csv_text: str, requested_year: int, source_url: str) -> pd.DataFrame:
    lines = [line for line in csv_text.splitlines() if line.strip()]
    header_idx = _find_header_row(lines)
    logger.info("Found CSV header at row %d. Total non-empty lines: %d.", header_idx, len(lines))
    normalized_csv = "\n".join(lines[header_idx:])
    df = pd.read_csv(io.StringIO(normalized_csv), thousands=",")
    logger.info("Raw DataFrame loaded. Initial rows: %d, columns: %d.", len(df), len(df.columns))

    required_cols = ["Date", "Hour", "Ontario Demand", *ZONE_COLUMNS, "Zone Total", "Diff"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"Missing expected columns: {missing}. Found columns: {list(df.columns)}")

    rename_map = {
        "Date": "delivery_date",
        "Hour": "hour_ending",
        "Ontario Demand": "ontario_demand_mw",
        "Zone Total": "zone_total_mw",
        "Diff": "zone_total_minus_ontario_demand_mw",
    }
    zone_rename_map = {zone: f"{zone.lower()}_mw" for zone in ZONE_COLUMNS}
    rename_map.update(zone_rename_map)
    df = df.rename(columns=rename_map)

    numeric_columns = [
        "hour_ending",
        "ontario_demand_mw",
        *zone_rename_map.values(),
        "zone_total_mw",
        "zone_total_minus_ontario_demand_mw",
    ]

    df["delivery_date"] = pd.to_datetime(df["delivery_date"], format="mixed", errors="raise")
    logger.info("Converted 'delivery_date' to datetime. Sample: %s", df["delivery_date"].iloc[0] if not df.empty else "N/A")

    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors="raise")
    logger.info("Converted numeric columns: %s", numeric_columns)

    df["hour_ending"] = df["hour_ending"].astype("int16")
    for col in numeric_columns:
        if col != "hour_ending":
            df[col] = df[col].astype("int32")

    df = df[df["delivery_date"].dt.year == requested_year].copy()
    if df.empty:
        logger.warning("No rows found for requested year %d in the dataset. Available years: %s", 
                       requested_year, df["delivery_date"].dt.year.unique() if "delivery_date" in df.columns else "unknown")
        raise ValueError(f"No rows found for requested year {requested_year}.")

    min_date = df["delivery_date"].min()
    max_date = df["delivery_date"].max()
    logger.info("Filtered for year %d. Rows: %d. Date range: %s to %s.", requested_year, len(df), min_date.date(), max_date.date())

    df["year"] = df["delivery_date"].dt.year.astype("int16")
    df["month"] = df["delivery_date"].dt.month.astype("int8")
    df["day"] = df["delivery_date"].dt.day.astype("int8")
    df["source"] = "IESO"
    df["dataset"] = "DemandZonal"
    df["source_url"] = source_url
    df["ingested_at_utc"] = pd.Timestamp.now(tz="UTC")
    df["timezone_note"] = "IESO trading hour / hour-ending convention; Ontario local market time"
    df["interval_start_local_naive"] = df["delivery_date"] + pd.to_timedelta(df["hour_ending"] - 1, unit="h")
    df["interval_end_local_naive"] = df["delivery_date"] + pd.to_timedelta(df["hour_ending"], unit="h")

    ordered_cols = [
        "delivery_date",
        "hour_ending",
        "interval_start_local_naive",
        "interval_end_local_naive",
        "ontario_demand_mw",
        *(f"{zone.lower()}_mw" for zone in ZONE_COLUMNS),
        "zone_total_mw",
        "zone_total_minus_ontario_demand_mw",
        "year",
        "month",
        "day",
        "source",
        "dataset",
        "source_url",
        "timezone_note",
        "ingested_at_utc",
    ]
    return df[ordered_cols].sort_values(["delivery_date", "hour_ending"]).reset_index(drop=True)


def to_long_format(df_wide: pd.DataFrame) -> pd.DataFrame:
    # 1. Create a temporary column for Ontario so it can be melted as a zone
    df_wide = df_wide.copy()
    df_wide["ontario_mw"] = df_wide["ontario_demand_mw"]

    # 2. Define which columns are attributes (ID vars) and which are measurements (Value vars)
    # Note: We exclude 'ontario_demand_mw' from id_columns if we want it to be part of the 'zone' rows
    zone_value_columns = [f"{zone.lower()}_mw" for zone in ZONE_COLUMNS] + ["ontario_mw"]

    id_columns = [
        "delivery_date",
        "hour_ending",
        "interval_start_local_naive",
        "interval_end_local_naive",
        "zone_total_mw",
        "zone_total_minus_ontario_demand_mw",
        "year",
        "month",
        "day",
        "source",
        "dataset",
        "source_url",
        "timezone_note",
        "ingested_at_utc",
    ]

    # 3. Melt the table
    df_long = df_wide.melt(
        id_vars=id_columns,
        value_vars=zone_value_columns,
        var_name="zone",
        value_name="load_mw",  # Renamed from zonal_demand_mw
    )

    # 4. Clean up zone names (remove the '_mw' suffix)
    df_long["zone"] = df_long["zone"].str.removesuffix("_mw")

    # 5. Ensure numeric types
    df_long["load_mw"] = df_long["load_mw"].astype("float64")

    return df_long.sort_values(["delivery_date", "hour_ending", "zone"]).reset_index(drop=True)


def dataframe_to_parquet_bytes(df: pd.DataFrame) -> bytes:
    table = pa.Table.from_pandas(df, preserve_index=False)
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="snappy")
    return sink.getvalue()


def local_parquet_path(local_output: str, year: int, month: int, normalize_long_format: bool) -> str:
    dataset_suffix = "long" if normalize_long_format else "wide"
    return os.path.join(
        local_output,
        f"year={year}",
        f"month={month:02d}",
        f"ieso_hourly_zonal_demand_{year}_{month:02d}_{dataset_suffix}.parquet",
    )


def s3_parquet_key(prefix: str, year: int, month: int, normalize_long_format: bool) -> str:
    prefix = prefix.strip("/")
    dataset_suffix = "long" if normalize_long_format else "wide"
    return (
        f"{prefix}/format={dataset_suffix}/year={year}/month={month:02d}/"
        f"ieso_hourly_zonal_demand_{year}_{month:02d}_{dataset_suffix}.parquet"
    )


def write_local_file(parquet_bytes: bytes, target_path: str) -> None:
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "wb") as fh:
        fh.write(parquet_bytes)
    logger.info("Wrote local parquet file: %s", target_path)


def upload_to_s3(
    parquet_bytes: bytes,
    bucket: str,
    key: str,
    region_name: str | None = None,
    server_side_encryption: str | None = None,
) -> None:
    s3 = boto3.client("s3", region_name=region_name)
    content_length = len(parquet_bytes)
    logger.info("Preparing to upload %d bytes to s3://%s/%s (SSE: %s)", content_length, bucket, key, server_side_encryption)
    extra_args: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": parquet_bytes,
        "ContentType": "application/octet-stream",
    }
    if server_side_encryption:
        extra_args["ServerSideEncryption"] = server_side_encryption

    try:
        s3.put_object(**extra_args)
        logger.info("Successfully uploaded parquet file to s3://%s/%s", bucket, key)
    except Exception as e:
        logger.error("Failed to upload to S3: %s", str(e), exc_info=True)
        raise


def run(config: Config) -> dict[str, Any]:
    source_url = build_report_url(config.year)
    csv_text = fetch_report_csv_text(source_url, config.timeout_seconds)
    df_wide = parse_ieso_zonal_csv(csv_text, config.year, source_url)
    df_final = to_long_format(df_wide) if config.normalize_long_format else df_wide

    if config.latest_only or config.yesterday_only:
        if config.yesterday_only:
            logger.info("yesterday_only is enabled. Filtering for the day before...")
            # Use ingested_at_utc (now) to determine yesterday's date
            # We use UTC because the Lambda trigger is UTC and IESO data is market time
            # Market time is usually EST/EDT.
            # However, for simplicity and consistency with the previous "latest_only" logic
            # let's assume "yesterday" relative to the execution time.
            now = pd.Timestamp.now(tz="UTC")
            yesterday = (now - pd.Timedelta(days=1)).normalize().tz_localize(None)
            df_final = df_final[df_final["delivery_date"] == yesterday].copy()
            logger.info("Filtering for yesterday's data. Target date: %s. Rows remaining after filter: %d.", yesterday.date(), len(df_final))
        else:
            logger.info("latest_only is enabled. Filtering for most recent date...")
            # Sort by delivery_date and hour_ending to find the most recent record(s)
            # They should already be sorted from parse_ieso_zonal_csv, but let's be safe.
            df_final = df_final.sort_values(["delivery_date", "hour_ending"], ascending=False)
            if not df_final.empty:
                latest_date = df_final.iloc[0]["delivery_date"]
                # Keep only the rows matching the most recent date
                # This handles both long/wide formats and multiple hours for the same date.
                # Using latest date (instead of just hour) is safer for hourly triggers
                # as it handles cases where multiple hours are updated at once.
                df_final = df_final[df_final["delivery_date"] == latest_date].copy()
                logger.info("Filtering for latest data only. Found latest date: %s. Rows remaining after filter: %d.", latest_date.date(), len(df_final))
                if df_final.empty:
                    logger.warning("Dataframe became empty after filtering for latest date %s", latest_date)
            else:
                logger.warning("latest_only is enabled but dataset is empty before filtering.")

    result: dict[str, Any] = {
        "dataset": config.dataset_name,
        "year": config.year,
        "row_count": int(len(df_final)),
        "format": "long" if config.normalize_long_format else "wide",
        "source_url": source_url,
        "local_paths": [],
        "s3_uris": [],
    }

    # Partition by month
    logger.info("Partitioning data by month. Total rows to process: %d", len(df_final))
    for month, df_month in df_final.groupby("month"):
        month = int(month)
        logger.info("Processing month %d with %d rows", month, len(df_month))
        parquet_bytes = dataframe_to_parquet_bytes(df_month)

        if config.write_local_copy and config.local_output:
            target_path = local_parquet_path(
                config.local_output, config.year, month, config.normalize_long_format
            )
            write_local_file(parquet_bytes, target_path)
            result["local_paths"].append(target_path)

        if config.bucket:
            key = s3_parquet_key(config.prefix, config.year, month, config.normalize_long_format)
            logger.info("Uploading partition to S3: s3://%s/%s", config.bucket, key)
            upload_to_s3(
                parquet_bytes=parquet_bytes,
                bucket=config.bucket,
                key=key,
                region_name=config.region_name,
                server_side_encryption=config.s3_server_side_encryption,
            )
            result["s3_uris"].append(f"s3://{config.bucket}/{key}")
        else:
            logger.info("No bucket specified, skipping S3 upload for month %d", month)

    logger.info(
        "Finished processing year=%s rows=%s format=%s. Generated %s monthly partitions.",
        config.year,
        len(df_final),
        result["format"],
        len(result["s3_uris"]) or len(result["local_paths"]),
    )
    return result


def _to_int(value: Any, default: int) -> int:
    """Safely convert value to int, falling back to default if value is None or an empty string."""
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    print(f"DEBUG_LOG: lambda_handler invoked with event: {json.dumps(event, default=str)}")
    logger.info("Lambda handler invoked with event: %s", json.dumps(event, default=str))
    
    # Log all relevant environment variables for debugging
    env_vars = {
        "TARGET_BUCKET": os.getenv("TARGET_BUCKET"),
        "TARGET_PREFIX": os.getenv("TARGET_PREFIX"),
        "TIMEOUT_SECONDS": os.getenv("TIMEOUT_SECONDS"),
        "LOG_LEVEL": os.getenv("LOG_LEVEL"),
        "LATEST_ONLY": os.getenv("LATEST_ONLY"),
        "YESTERDAY_ONLY": os.getenv("YESTERDAY_ONLY"),
        "YEAR": os.getenv("YEAR"),
    }
    print(f"DEBUG_LOG: Environment variables: {json.dumps(env_vars, default=str)}")
    logger.info("Environment variables: %s", json.dumps(env_vars, default=str))

    year = _to_int(event.get("year") or os.getenv("YEAR"), CURRENT_YEAR)
    bucket = event.get("bucket") or os.getenv("TARGET_BUCKET") or BUCKET_NAME
    prefix = event.get("prefix") or os.getenv("TARGET_PREFIX", DEFAULT_PREFIX)
    local_output = event.get("local_output") or os.getenv("LOCAL_OUTPUT", "/tmp")
    timeout_seconds = _to_int(
        event.get("timeout_seconds") or os.getenv("TIMEOUT_SECONDS"), DEFAULT_TIMEOUT_SECONDS
    )
    region_name = (
        event.get("region_name") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    )
    write_local_copy = bool(event.get("write_local_copy", False))
    sse = event.get("s3_server_side_encryption") or os.getenv("S3_SERVER_SIDE_ENCRYPTION")
    normalize_long_format = bool(event.get("normalize_long_format", True))
    latest_only = bool(event.get("latest_only", os.getenv("LATEST_ONLY", "false").lower() == "true"))
    yesterday_only = bool(event.get("yesterday_only", os.getenv("YESTERDAY_ONLY", "false").lower() == "true"))

    config = Config(
        year=year,
        bucket=bucket,
        prefix=prefix,
        local_output=local_output,
        timeout_seconds=timeout_seconds,
        region_name=region_name,
        write_local_copy=write_local_copy,
        s3_server_side_encryption=sse,
        normalize_long_format=normalize_long_format,
        latest_only=latest_only,
        yesterday_only=yesterday_only,
    )
    print(f"DEBUG_LOG: Final configuration: {config}")
    logger.info("Configuration: %s", config)
    
    try:
        result = run(config)
        print(f"DEBUG_LOG: Execution finished successfully. S3 URIs: {result.get('s3_uris')}")
        return result
    except Exception as e:
        print(f"DEBUG_LOG: ERROR in lambda_handler: {str(e)}")
        logger.exception("Error during execution of run(): %s", str(e))
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch IESO hourly zonal demand and store it as Parquet")
    parser.add_argument("--year", type=int, required=True, help="Target calendar year, e.g. 2025")
    parser.add_argument("--bucket", type=str, default=None, help="Target S3 bucket")
    parser.add_argument("--prefix", type=str, default=DEFAULT_PREFIX, help="Target S3 key prefix")
    parser.add_argument("--local-output", type=str, default="./data", help="Local output directory")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--region-name", type=str, default=None)
    parser.add_argument("--wide-format", action="store_true", help="Write wide zonal columns instead of one row per zone per hour")
    parser.add_argument("--latest-only", action="store_true", help="Only process the most recent records found in the source")
    parser.add_argument("--yesterday-only", action="store_true", help="Only process the records for the day before execution")
    parser.add_argument("--skip-local-copy", action="store_true", help="Do not write a local parquet file")
    parser.add_argument(
        "--s3-server-side-encryption",
        type=str,
        default=None,
        help="Example: AES256 or aws:kms",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = Config(
        year=args.year,
        bucket=args.bucket or BUCKET_NAME,
        prefix=args.prefix,
        local_output=args.local_output,
        timeout_seconds=args.timeout_seconds,
        region_name=args.region_name,
        write_local_copy=not args.skip_local_copy,
        s3_server_side_encryption=args.s3_server_side_encryption,
        normalize_long_format=not args.wide_format,
        latest_only=args.latest_only,
        yesterday_only=args.yesterday_only,
    )
    output = run(cfg)
    print(json.dumps(output, indent=2, default=str))
