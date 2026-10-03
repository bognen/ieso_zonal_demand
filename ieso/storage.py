"""Parquet output to S3 and/or local disk, with idempotent upserts for partial refreshes."""
from __future__ import annotations

import io
import logging
import os
from typing import Any

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def to_parquet_bytes(df: pd.DataFrame) -> bytes:
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), sink, compression="snappy")
    return sink.getvalue()


def _file_name(dataset: str, year: int, month: int, fmt: str) -> str:
    return f"{dataset}_{year}_{month:02d}_{fmt}.parquet"


def s3_key(prefix: str, dataset: str, year: int, month: int, fmt: str) -> str:
    return f"{prefix.strip('/')}/format={fmt}/year={year}/month={month:02d}/{_file_name(dataset, year, month, fmt)}"


def local_path(root: str, dataset: str, year: int, month: int, fmt: str) -> str:
    return os.path.join(root, f"year={year}", f"month={month:02d}", _file_name(dataset, year, month, fmt))


def upsert(existing: pd.DataFrame | None, new: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Merge `new` over `existing`; on key collisions the new (re-published) row wins."""
    if existing is None or existing.empty:
        return new
    merged = pd.concat([existing, new], ignore_index=True).drop_duplicates(keys, keep="last")
    return merged.sort_values(keys).reset_index(drop=True)


class ParquetStore:
    def __init__(self, bucket: str | None, prefix: str, dataset: str, *, local_output: str | None = None,
                 sse: str | None = None, region_name: str | None = None, s3_client: Any = None) -> None:
        self.bucket, self.prefix, self.dataset = bucket, prefix, dataset
        self.local_output, self.sse = local_output, sse
        self._s3 = s3_client or (boto3.client("s3", region_name=region_name) if bucket else None)

    def _read_s3(self, key: str) -> pd.DataFrame | None:
        try:
            body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return pq.read_table(io.BytesIO(body)).to_pandas()

    def _read_local(self, path: str) -> pd.DataFrame | None:
        return pq.read_table(path).to_pandas() if os.path.exists(path) else None

    def write_partition(self, df: pd.DataFrame, year: int, month: int, fmt: str, keys: list[str],
                        *, merge: bool, dry_run: bool = False) -> list[str]:
        """Write one month. With merge=True the existing file is read and upserted, so refreshing a
        few days never truncates the rest of the month. Returns the destinations written."""
        destinations: list[str] = []
        if self.bucket:
            key = s3_key(self.prefix, self.dataset, year, month, fmt)
            body_df = upsert(self._read_s3(key), df, keys) if merge else df
            destinations.append(self._put_s3(body_df, key, dry_run))
        if self.local_output:
            path = local_path(self.local_output, self.dataset, year, month, fmt)
            body_df = upsert(self._read_local(path), df, keys) if merge else df
            destinations.append(self._put_local(body_df, path, dry_run))
        return destinations

    def _put_s3(self, df: pd.DataFrame, key: str, dry_run: bool) -> str:
        uri = f"s3://{self.bucket}/{key}"
        if dry_run:
            logger.info("dry_run: would upload partition", extra={"uri": uri, "rows": len(df)})
            return uri
        payload = to_parquet_bytes(df)
        args: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": payload,
                                "ContentType": "application/vnd.apache.parquet"}
        if self.sse:
            args["ServerSideEncryption"] = self.sse
        self._s3.put_object(**args)
        logger.info("Uploaded partition", extra={"uri": uri, "rows": len(df), "bytes": len(payload)})
        return uri

    def _put_local(self, df: pd.DataFrame, path: str, dry_run: bool) -> str:
        if dry_run:
            logger.info("dry_run: would write file", extra={"path": path, "rows": len(df)})
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(to_parquet_bytes(df))
        logger.info("Wrote local file", extra={"path": path, "rows": len(df)})
        return path
