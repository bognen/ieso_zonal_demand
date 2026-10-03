"""AWS Lambda entry point for scrape-ieso-zonal-demand."""
from __future__ import annotations

import logging
import os
from typing import Any

from ieso.config import load_runtime_config
from ieso.pipeline import run
from utils.logging_setup import set_request_id, setup_logging

setup_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    set_request_id(getattr(context, "aws_request_id", None))
    event = event or {}
    logger.info("Invoked", extra={"event": event})
    try:
        cfg = load_runtime_config(event)
        setup_logging(cfg.log_level)
        return run(cfg)
    except Exception:
        logger.exception("Run failed")
        raise
