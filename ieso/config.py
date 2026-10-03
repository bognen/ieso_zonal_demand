"""Runtime configuration.

Precedence (highest first): Lambda event override > DynamoDB config item > built-in default.
The DynamoDB table name and item id come from environment variables set in template.yaml.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any

import boto3

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "ieso/load/zonal_hourly"
DEFAULT_DATASET_NAME = "ieso_hourly_zonal_demand"
DEFAULT_CONFIG_ID = "scrape-ieso-zonal-demand"

# Keys a caller may override from the Lambda test event.
EVENT_OVERRIDES = ("year", "lookback_days", "latest_only", "normalize_long_format", "dry_run")


@dataclass(frozen=True)
class Config:
    bucket: str | None = None
    prefix: str = DEFAULT_PREFIX
    dataset_name: str = DEFAULT_DATASET_NAME
    timeout_seconds: int = 60
    region_name: str | None = None
    s3_server_side_encryption: str | None = None
    normalize_long_format: bool = True
    # Full-year load when set. Otherwise the last `lookback_days` completed Ontario days are refreshed.
    year: int | None = None
    lookback_days: int = 2
    latest_only: bool = False
    dry_run: bool = False
    local_output: str | None = None
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.year is None and self.lookback_days < 1:
            raise ValueError("lookback_days must be >= 1 when no year is given")
        if not self.bucket and not self.local_output:
            raise ValueError("Configuration needs a bucket and/or a local_output directory")


def as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _as_int(value: Any) -> int:
    return int(value)  # handles Decimal from DynamoDB as well as str


_COERCE = {
    "year": _as_int,
    "lookback_days": _as_int,
    "timeout_seconds": _as_int,
    "latest_only": as_bool,
    "normalize_long_format": as_bool,
    "dry_run": as_bool,
}


def _plain(value: Any) -> Any:
    return int(value) if isinstance(value, Decimal) and value == value.to_integral() else value


def fetch_dynamodb_config(table_name: str, config_id: str, dynamodb: Any = None) -> dict[str, Any]:
    """Read the config item. Raises if it is missing, so a mis-deployed stack fails loudly."""
    table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)
    item = table.get_item(Key={"config_id": config_id}).get("Item")
    if not item:
        raise RuntimeError(f"No config item with config_id={config_id!r} in DynamoDB table {table_name!r}")
    item.pop("config_id")
    logger.info("Loaded configuration from DynamoDB", extra={"table": table_name, "config_id": config_id})
    return {k: _plain(v) for k, v in item.items()}


def build_config(stored: dict[str, Any], event: dict[str, Any] | None = None, **base: Any) -> Config:
    """Merge defaults < stored (DynamoDB) < event overrides into a validated Config."""
    known = {f.name for f in fields(Config)}
    unknown = set(stored) - known
    if unknown:
        logger.warning("Ignoring unknown config keys", extra={"keys": sorted(unknown)})
    values: dict[str, Any] = {**base, **{k: v for k, v in stored.items() if k in known}}
    event = event or {}
    for key in EVENT_OVERRIDES:
        if event.get(key) is not None:
            values[key] = event[key]
    return Config(**{k: _COERCE.get(k, lambda v: v)(v) for k, v in values.items()})


def load_runtime_config(event: dict[str, Any] | None, dynamodb: Any = None) -> Config:
    table_name = os.environ["CONFIG_TABLE_NAME"]
    config_id = os.getenv("CONFIG_ID", DEFAULT_CONFIG_ID)
    stored = fetch_dynamodb_config(table_name, config_id, dynamodb)
    return build_config(stored, event, region_name=os.getenv("AWS_REGION"))
