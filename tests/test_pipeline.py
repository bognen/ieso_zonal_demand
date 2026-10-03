import io
from datetime import date, datetime, timezone

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from ieso import pipeline
from ieso.config import Config
from ieso.pipeline import plan_windows, run
from ieso.source import candidate_urls

BUCKET = "test-bucket"
NOW = datetime(2026, 4, 5, 15, 0, tzinfo=timezone.utc)  # 11:00 in Toronto -> "today" is 2026-04-05


@pytest.fixture()
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture()
def serve(monkeypatch, sample_text):
    """Make the pipeline 'download' the given CSV text instead of calling IESO."""
    def _serve(text=sample_text):
        monkeypatch.setattr(pipeline, "fetch_first_available", lambda urls, timeout: (text, urls[0]))
    _serve()
    return _serve


def read(s3, key):
    return pq.read_table(io.BytesIO(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())).to_pandas()


def keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def test_plan_windows_lookback():
    cfg = Config(bucket="b", lookback_days=2)
    assert plan_windows(cfg, date(2026, 4, 5)) == [pipeline.Window(2026, date(2026, 4, 3), date(2026, 4, 4))]


def test_plan_windows_straddles_new_year():
    cfg = Config(bucket="b", lookback_days=3)
    assert plan_windows(cfg, date(2026, 1, 1)) == [
        pipeline.Window(2025, date(2025, 12, 29), date(2025, 12, 31)),
    ]
    assert plan_windows(cfg, date(2026, 1, 2)) == [
        pipeline.Window(2025, date(2025, 12, 30), date(2025, 12, 31)),
        pipeline.Window(2026, date(2026, 1, 1), date(2026, 1, 1)),
    ]


def test_candidate_urls():
    assert candidate_urls(2026, 2026) == ["https://reports-public.ieso.ca/public/DemandZonal/PUB_DemandZonal.csv"]
    assert candidate_urls(2024, 2026)[0].endswith("PUB_DemandZonal_2024.csv")


def test_config_requires_lookback_or_year():
    with pytest.raises(ValueError):
        Config(bucket="b", lookback_days=0)


def test_full_year_writes_monthly_partitions(s3, serve):
    result = run(Config(bucket=BUCKET, year=2026), now_utc=NOW)
    assert result["status"] == "ok"
    assert keys(s3) == [
        f"ieso/load/zonal_hourly/format=long/year=2026/month={m:02d}/ieso_hourly_zonal_demand_2026_{m:02d}_long.parquet"
        for m in (1, 2, 3, 4)
    ]


def test_lookback_merges_instead_of_truncating_month(s3, serve, sample_text):
    run(Config(bucket=BUCKET, year=2026), now_utc=NOW)
    key = [k for k in keys(s3) if "month=04" in k][0]
    before = read(s3, key)

    revised = sample_text.replace("2026-04-04,1,", "2026-04-04,1,9", 1)  # 'Ontario Demand' for 04-04 h1 -> 9xxxx
    assert revised != sample_text
    serve(revised)
    run(Config(bucket=BUCKET, lookback_days=2), now_utc=NOW)

    after = read(s3, key)
    assert len(after) == len(before)  # rest of April preserved, no duplicates
    row = lambda df: df[(df["delivery_date"] == "2026-04-04") & (df["hour_ending"] == 1) & (df["zone"] == "ontario")]
    assert row(after)["load_mw"].iloc[0] != row(before)["load_mw"].iloc[0]


def test_lookback_into_empty_bucket_writes_only_window(s3, serve):
    run(Config(bucket=BUCKET, lookback_days=2), now_utc=NOW)
    (key,) = keys(s3)
    df = read(s3, key)
    assert sorted(df["delivery_date"].dt.date.unique()) == [date(2026, 4, 3), date(2026, 4, 4)]


def test_dry_run_writes_nothing(s3, serve):
    result = run(Config(bucket=BUCKET, lookback_days=2, dry_run=True), now_utc=NOW)
    assert result["row_count"] > 0 and keys(s3) == []


def test_empty_window_is_no_data_not_error(s3, serve):
    result = run(Config(bucket=BUCKET, lookback_days=2), now_utc=datetime(2026, 8, 1, 15, tzinfo=timezone.utc))
    assert result["status"] == "no_data" and keys(s3) == []


def test_full_year_with_no_rows_raises(s3, serve):
    with pytest.raises(ValueError, match="No rows"):
        run(Config(bucket=BUCKET, year=2020), now_utc=NOW)


def test_wide_format_and_local_output(serve, tmp_path):
    run(Config(local_output=str(tmp_path), year=2026, normalize_long_format=False), now_utc=NOW)
    files = sorted(p.name for p in tmp_path.rglob("*.parquet"))
    assert files[0] == "ieso_hourly_zonal_demand_2026_01_wide.parquet" and len(files) == 4
