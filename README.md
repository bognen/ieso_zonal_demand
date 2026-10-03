# scrape-ieso-zonal-demand

AWS Lambda that downloads the IESO Hourly Zonal Demand report and keeps monthly Parquet partitions up to date in S3.

- Source: `https://reports-public.ieso.ca/public/DemandZonal/` (`PUB_DemandZonal.csv` for the current year, `PUB_DemandZonal_<year>.csv` for closed years)
- Schedule: daily at 10:15 UTC (after Ontario midnight). Each run refreshes the last `lookback_days` completed **Ontario-time** days (default 2, to pick up IESO revisions).
- Partial refreshes are **merged** into the existing month file (upsert on date/hour/zone), so they never truncate the month. Full-year loads (`year`) overwrite the year's partitions.
- Output: `s3://<bucket>/<prefix>/format=long|wide/year=YYYY/month=MM/ieso_hourly_zonal_demand_YYYY_MM_<format>.parquet`
  - `long` (default): one row per hour and zone (`zone`, `load_mw`; Ontario-wide demand is zone `ontario`)
  - `wide`: one column per zone

## Layout

| Path | Purpose |
|------|---------|
| `app.py` | Lambda entry point (`app.lambda_handler`) |
| `ieso/` | `config` (DynamoDB), `source` (download + retries), `transform` (parse/validate/reshape), `storage` (Parquet/S3), `pipeline` |
| `utils/logging_setup.py` | JSON structured logging for CloudWatch |
| `config/dynamodb_config_item.json` | Runtime configuration item |
| `events/` | Sample Lambda test events |
| `scripts/` | `run_local.py` (workstation run), `backfill.ps1` |
| `template.yaml` | AWS SAM template (Zip deployment) |

## Configuration (DynamoDB)

All runtime config lives in DynamoDB table `scrape-ieso-zonal-demand-config`, item `config_id = scrape-ieso-zonal-demand`. The table name and item id reach the Lambda as environment variables from `template.yaml`. The source is public, so no secrets are needed.

| Attribute | Default | Meaning |
|-----------|---------|---------|
| `bucket` | – | Target S3 bucket (must match the `TargetBucket` stack parameter, which scopes IAM) |
| `prefix` | `ieso/load/zonal_hourly` | Key prefix (must match `TargetPrefix`) |
| `dataset_name` | `ieso_hourly_zonal_demand` | File name stem |
| `timeout_seconds` | `60` | HTTP timeout |
| `s3_server_side_encryption` | none | `AES256` or `aws:kms` |
| `normalize_long_format` | `true` | Long vs wide output |
| `lookback_days` | `2` | Completed Ontario days refreshed per run |
| `latest_only` | `false` | Keep only the newest date found |
| `log_level` | `INFO` | |

The test event can override `year`, `lookback_days`, `latest_only`, `normalize_long_format` and `dry_run`.

## Prerequisites

- Python 3.12, [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html), AWS credentials for the target account
- Deployment bucket `[BUCKET-NAME-HERE]` (for SAM artifacts) and the existing data bucket (`com.dsa.ieso-project` by default)
- For tests: `pip install -r requirements-dev.txt`

## Deploy

```bash
sam build
sam deploy --stack-name scrape-ieso-zonal-demand \
  --s3-bucket [BUCKET-NAME-HERE] --s3-prefix scrape-ieso-zonal-demand \
  --capabilities CAPABILITY_NAMED_IAM --region us-east-1 \
  --parameter-overrides TargetBucket=com.dsa.ieso-project TargetPrefix=ieso/load/zonal_hourly

# first deploy only (and after any config change): load the config item
aws dynamodb put-item --table-name scrape-ieso-zonal-demand-config \
  --item file://config/dynamodb_config_item.json --region us-east-1
```

Explicit resource names: Lambda `scrape-ieso-zonal-demand`, role `scrape-ieso-zonal-demand-role`, log group `/aws/lambda/scrape-ieso-zonal-demand` (90-day retention), alarm `scrape-ieso-zonal-demand-errors`, schedule `scrape-ieso-zonal-demand-daily`.

## Test events

Files in `events/`:

```json
{}                                                  // scheduled run (events/scheduled.json)
{"lookback_days": 5, "dry_run": true}               // fetch + transform, write nothing
{"year": 2025, "normalize_long_format": true}       // full-year backfill
{"latest_only": true, "dry_run": true}
```

```bash
sam local invoke ScrapeFunction --event events/dry_run_lookback.json
aws lambda invoke --function-name scrape-ieso-zonal-demand --payload fileb://events/dry_run_lookback.json out.json
```

## Local run and tests

```bash
python -m scripts.run_local --year 2025 --local-output ./out      # full year to disk
python -m scripts.run_local --lookback-days 3 --dry-run           # no writes
.\scripts\backfill.ps1 -Bucket com.dsa.ieso-project -From 2010    # many years to S3
python -m pytest
```

## Observability

Logs are one JSON object per line (`timestamp`, `level`, `message`, `request_id` plus context fields). Example Logs Insights query:

```
fields @timestamp, message, rows, issue | filter level in ["WARNING","ERROR"] | sort @timestamp desc
```

Runs return `status: ok | no_data` plus per-year row counts and data-quality `issues` (missing hours, duplicates). A run that fails raises, which trips the `Errors` alarm.
