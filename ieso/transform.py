"""Parse, validate and reshape the IESO zonal demand report."""
from __future__ import annotations

import io
import logging
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)

ZONE_COLUMNS = ["Northwest", "Northeast", "Ottawa", "East", "Toronto", "Essa", "Bruce", "Southwest", "Niagara", "West"]
ZONE_FIELDS = [f"{z.lower()}_mw" for z in ZONE_COLUMNS]
HEADER_PREFIX = "Date,Hour,Ontario Demand,"

WIDE_KEYS = ["delivery_date", "hour_ending"]
LONG_KEYS = ["delivery_date", "hour_ending", "zone"]

_RENAME = {
    "Date": "delivery_date",
    "Hour": "hour_ending",
    "Ontario Demand": "ontario_demand_mw",
    "Zone Total": "zone_total_mw",
    "Diff": "zone_total_minus_ontario_demand_mw",
    **{z: f"{z.lower()}_mw" for z in ZONE_COLUMNS},
}
_MEASURES = ["ontario_demand_mw", *ZONE_FIELDS, "zone_total_mw", "zone_total_minus_ontario_demand_mw"]


def _header_index(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if line.strip().startswith(HEADER_PREFIX):
            return idx
    raise ValueError("Could not find the CSV header row in the IESO zonal demand report.")


def parse_report(csv_text: str, year: int, source_url: str, ingested_at: pd.Timestamp) -> pd.DataFrame:
    """Parse the raw CSV into the wide schema, keeping only rows of `year`. May return an empty frame."""
    lines = [line for line in csv_text.splitlines() if line.strip()]
    start = _header_index(lines)
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), thousands=",")

    missing = [c for c in _RENAME if c not in df.columns]
    if missing:
        raise ValueError(f"Missing expected columns: {missing}. Found columns: {list(df.columns)}")
    df = df.rename(columns=_RENAME)[list(_RENAME.values())]

    # Rows for hours that have not been published yet can be blank; drop rather than fail the load.
    incomplete = df[["delivery_date", "hour_ending", *_MEASURES]].isna().any(axis=1)
    if incomplete.any():
        logger.warning("Dropping incomplete rows", extra={"rows": int(incomplete.sum())})
        df = df[~incomplete].copy()

    df["delivery_date"] = pd.to_datetime(df["delivery_date"], format="mixed", errors="raise")
    for col in ["hour_ending", *_MEASURES]:
        df[col] = pd.to_numeric(df[col], errors="raise")
    df["hour_ending"] = df["hour_ending"].astype("int16")
    df[_MEASURES] = df[_MEASURES].astype("int32")

    available_years = sorted(df["delivery_date"].dt.year.unique().tolist())
    df = df[df["delivery_date"].dt.year == year].copy()
    if df.empty:
        logger.warning("No rows for requested year", extra={"year": year, "available_years": available_years})
        return df

    df["year"] = df["delivery_date"].dt.year.astype("int16")
    df["month"] = df["delivery_date"].dt.month.astype("int8")
    df["day"] = df["delivery_date"].dt.day.astype("int8")
    df["source"] = "IESO"
    df["dataset"] = "DemandZonal"
    df["source_url"] = source_url
    df["timezone_note"] = "IESO trading hour / hour-ending convention; Ontario local market time"
    df["ingested_at_utc"] = ingested_at
    df["interval_start_local_naive"] = df["delivery_date"] + pd.to_timedelta(df["hour_ending"] - 1, unit="h")
    df["interval_end_local_naive"] = df["delivery_date"] + pd.to_timedelta(df["hour_ending"], unit="h")

    ordered = [
        "delivery_date", "hour_ending", "interval_start_local_naive", "interval_end_local_naive",
        "ontario_demand_mw", *ZONE_FIELDS, "zone_total_mw", "zone_total_minus_ontario_demand_mw",
        "year", "month", "day", "source", "dataset", "source_url", "timezone_note", "ingested_at_utc",
    ]
    return df[ordered].sort_values(WIDE_KEYS).reset_index(drop=True)


def validate(df: pd.DataFrame, today: date) -> list[str]:
    """Return human-readable data-quality issues (also logged). Never raises: the data is still worth keeping."""
    issues: list[str] = []
    if df.empty:
        return issues
    dup = int(df.duplicated(WIDE_KEYS).sum())
    if dup:
        issues.append(f"{dup} duplicate (delivery_date, hour_ending) rows")
    per_day = df.groupby(df["delivery_date"].dt.date)["hour_ending"].nunique()
    # 23 / 25 hours are legitimate on the DST change days.
    for day, hours in per_day.items():
        if day < today and hours not in (23, 24, 25):
            issues.append(f"{day}: {hours} hours published (expected 24)")
    if (df["ontario_demand_mw"] <= 0).any():
        issues.append("non-positive ontario_demand_mw values")
    for issue in issues:
        logger.warning("Data quality issue", extra={"issue": issue})
    return issues


def to_long_format(df_wide: pd.DataFrame) -> pd.DataFrame:
    """One row per (hour, zone). Ontario-wide demand is emitted as the extra zone `ontario`."""
    df = df_wide.copy()
    df["ontario_mw"] = df["ontario_demand_mw"]
    id_columns = [
        "delivery_date", "hour_ending", "interval_start_local_naive", "interval_end_local_naive",
        "zone_total_mw", "zone_total_minus_ontario_demand_mw", "year", "month", "day",
        "source", "dataset", "source_url", "timezone_note", "ingested_at_utc",
    ]
    long = df.melt(id_vars=id_columns, value_vars=[*ZONE_FIELDS, "ontario_mw"], var_name="zone", value_name="load_mw")
    long["zone"] = long["zone"].str.removesuffix("_mw")
    long["load_mw"] = long["load_mw"].astype("float64")
    return long.sort_values(LONG_KEYS).reset_index(drop=True)
