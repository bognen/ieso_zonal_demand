"""Orchestration: fetch -> parse -> validate -> reshape -> write."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from ieso.config import Config
from ieso.source import candidate_urls, fetch_first_available
from ieso.storage import ParquetStore
from ieso.transform import LONG_KEYS, WIDE_KEYS, parse_report, to_long_format, validate

logger = logging.getLogger(__name__)

ONTARIO_TZ = ZoneInfo("America/Toronto")  # IESO publishes in Ontario market time, not UTC


@dataclass(frozen=True)
class Window:
    """A year to load, optionally restricted to [start, end] (inclusive)."""
    year: int
    start: date | None = None
    end: date | None = None


def ontario_today(now_utc: datetime) -> date:
    return now_utc.astimezone(ONTARIO_TZ).date()


def plan_windows(cfg: Config, today: date) -> list[Window]:
    if cfg.year is not None:
        return [Window(cfg.year)]
    end = today - timedelta(days=1)
    start = today - timedelta(days=cfg.lookback_days)
    # A window that straddles New Year needs two source files.
    return [
        Window(y, max(start, date(y, 1, 1)), min(end, date(y, 12, 31)))
        for y in sorted({start.year, end.year})
    ]


def _process_window(cfg: Config, window: Window, store: ParquetStore, now_utc: datetime, today: date) -> dict[str, Any]:
    fmt = "long" if cfg.normalize_long_format else "wide"
    text, url = fetch_first_available(candidate_urls(window.year, today.year), cfg.timeout_seconds)
    df = parse_report(text, window.year, url, pd.Timestamp(now_utc))

    if window.start is not None:
        dates = df["delivery_date"].dt.date
        df = df[(dates >= window.start) & (dates <= window.end)]
    if cfg.latest_only and not df.empty:
        df = df[df["delivery_date"] == df["delivery_date"].max()]

    summary: dict[str, Any] = {"year": window.year, "source_url": url, "rows": 0, "destinations": [], "issues": []}
    if df.empty:
        if window.start is None:
            raise ValueError(f"No rows found for requested year {window.year}.")
        logger.warning("No published rows in window yet",
                       extra={"year": window.year, "start": str(window.start), "end": str(window.end)})
        return summary

    summary["issues"] = validate(df, today)
    out = to_long_format(df) if cfg.normalize_long_format else df
    summary["rows"] = len(out)
    logger.info("Prepared data", extra={"year": window.year, "rows": len(out), "format": fmt,
                                        "first_date": str(df["delivery_date"].min().date()),
                                        "last_date": str(df["delivery_date"].max().date())})

    # Partial refreshes must merge into the existing month file instead of replacing it.
    partial = window.start is not None or cfg.latest_only
    keys = LONG_KEYS if cfg.normalize_long_format else WIDE_KEYS
    for month, month_df in out.groupby("month"):
        summary["destinations"] += store.write_partition(
            month_df, window.year, int(month), fmt, keys, merge=partial, dry_run=cfg.dry_run
        )
    return summary


def run(cfg: Config, now_utc: datetime | None = None, store: ParquetStore | None = None) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    today = ontario_today(now_utc)
    store = store or ParquetStore(cfg.bucket, cfg.prefix, cfg.dataset_name, local_output=cfg.local_output,
                                  sse=cfg.s3_server_side_encryption, region_name=cfg.region_name)
    windows = plan_windows(cfg, today)
    logger.info("Run started", extra={"today_ontario": str(today), "windows": [str(w) for w in windows],
                                      "dry_run": cfg.dry_run, "latest_only": cfg.latest_only})

    results = [_process_window(cfg, w, store, now_utc, today) for w in windows]
    rows = sum(r["rows"] for r in results)
    result = {
        "dataset": cfg.dataset_name,
        "status": "ok" if rows else "no_data",
        "dry_run": cfg.dry_run,
        "format": "long" if cfg.normalize_long_format else "wide",
        "row_count": rows,
        "windows": results,
    }
    logger.info("Run finished", extra={"status": result["status"], "row_count": rows})
    return result
