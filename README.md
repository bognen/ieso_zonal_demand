# IESO Zonal Demand -> S3 Parquet

This script downloads the IESO Hourly Zonal Demand report, converts it to Parquet, and optionally uploads it to Amazon S3.

## Files
- `ieso_zonal_demand_to_s3.py` (Main script)
- `requirements.txt` (Python dependencies)
- `serverless.yml` (Serverless Framework configuration)
- `.env` (Environment variables for deployment, not committed)

## Source
Base folder:
- `https://reports-public.ieso.ca/public/DemandZonal/`

The script uses:
- `PUB_DemandZonal_<year>.csv` for closed years
- `PUB_DemandZonal.csv` for the current year

## Local run
```bash
python ieso_zonal_demand_to_s3.py \
  --year 2025 \
  --bucket your-bucket \
  --prefix ieso/load/zonal_hourly \
  --local-output ./out
```

## Local run in wide format
```bash
python ieso_zonal_demand_to_s3.py \
  --year 2025 \
  --bucket your-bucket \
  --prefix ieso/load/zonal_hourly \
  --local-output ./out \
  --wide-format
```

## AWS Lambda Deployment (Serverless Framework)

This project is configured to be deployed as an AWS Lambda function using **Serverless Framework v3.39** and **Docker container image**.

### Prerequisites
- [Node.js](https://nodejs.org/) installed.
- [Serverless Framework](https://www.serverless.com/) v3.x installed: `npm install -g serverless@3`.
- [Docker](https://www.docker.com/) installed (required to build the Lambda container image).

### 1. Setup Environment Variables
Copy or create a `.env` file in the project root and fill in the required values. Note that the `serverless.yml` template does not provide default values; all variables must be defined in `.env`.

```env
TARGET_BUCKET=your-target-s3-bucket
TARGET_PREFIX=ieso/load/zonal_hourly
TIMEOUT_SECONDS=60
S3_SERVER_SIDE_ENCRYPTION=AES256
LOG_LEVEL=INFO
NORMALIZE_LONG_FORMAT=true
WRITE_LOCAL_COPY=false
```

### 2. Deployment Details
The service is deployed using a Docker container image. Serverless Framework will automatically build the image from the `Dockerfile` in the root directory and push it to AWS ECR.

### 3. Deploy
Deploy the service to AWS:
```bash
serverless deploy --region us-east-1
```

### EventBridge Trigger
The deployment includes an EventBridge schedule that triggers the Lambda function automatically **daily at 3:15 AM UTC** (`cron(15 3 * * ? *)`) to pull the prior day's data.

## Lambda event
```json
{
  "year": 2025,
  "bucket": "your-bucket",
  "prefix": "ieso/load/zonal_hourly",
  "write_local_copy": false,
  "normalize_long_format": true
}
```

## Suggested S3 layout
Long format (default):
```text
s3://your-bucket/ieso/load/zonal_hourly/format=long/year=2025/ieso_hourly_zonal_demand_2025_long.parquet
```

Wide format:
```text
s3://your-bucket/ieso/load/zonal_hourly/format=wide/year=2025/ieso_hourly_zonal_demand_2025_wide.parquet
```

## Notes
- Long format is now the default as it is nicer for agents/tools to query by zone as an entity dimension.
- Wide format is usually best for direct analytics and simple Athena/Glue tables.
- The Lambda function is deployed as a Docker container image to simplify dependency management for `pandas` and `pyarrow`.
