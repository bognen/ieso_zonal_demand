import json
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

import app
from ieso import pipeline
from ieso.config import build_config, fetch_dynamodb_config

TABLE = "scrape-ieso-zonal-demand-config"
ITEM = {
    "config_id": "scrape-ieso-zonal-demand", "bucket": "test-bucket", "prefix": "p", "timeout_seconds": 30,
    "normalize_long_format": True, "lookback_days": 2, "latest_only": False, "log_level": "DEBUG",
    "bogus_key": "ignored",
}


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIG_TABLE_NAME", TABLE)
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        table = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
        )
        boto3.client("s3").create_bucket(Bucket="test-bucket")
        yield SimpleNamespace(ddb=ddb, table=table)


def test_event_overrides_dynamodb(aws):
    aws.table.put_item(Item=ITEM)
    stored = fetch_dynamodb_config(TABLE, "scrape-ieso-zonal-demand", aws.ddb)
    assert stored["timeout_seconds"] == 30 and isinstance(stored["timeout_seconds"], int)
    cfg = build_config(stored, {"lookback_days": "5", "dry_run": "true"})
    assert (cfg.bucket, cfg.lookback_days, cfg.dry_run, cfg.log_level) == ("test-bucket", 5, True, "DEBUG")


def test_missing_config_item_fails_loudly(aws):
    with pytest.raises(RuntimeError, match="No config item"):
        fetch_dynamodb_config(TABLE, "nope", aws.ddb)


def test_lambda_handler_end_to_end(aws, monkeypatch, sample_text):
    aws.table.put_item(Item=ITEM)
    monkeypatch.setattr(pipeline, "fetch_first_available", lambda urls, timeout: (sample_text, urls[0]))
    result = app.lambda_handler({"year": 2026}, SimpleNamespace(aws_request_id="req-1"))
    assert result["status"] == "ok" and len(result["windows"][0]["destinations"]) == 4
    json.dumps(result)  # response must be JSON-serialisable for Lambda


def test_json_log_lines():
    import logging
    from utils.logging_setup import JsonFormatter, set_request_id
    set_request_id("abc")
    record = logging.getLogger("t").makeRecord("t", logging.INFO, __file__, 1, "hello %s", ("x",), None,
                                                extra={"rows": 3})
    out = json.loads(JsonFormatter().format(record))
    assert (out["message"], out["rows"], out["request_id"], out["level"]) == ("hello x", 3, "abc", "INFO")
