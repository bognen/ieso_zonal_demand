"""Download the IESO Hourly Zonal Demand report."""
from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE_URL = "https://reports-public.ieso.ca/public/DemandZonal"


def candidate_urls(year: int, current_year: int) -> list[str]:
    """URLs to try, in order. The current year lives in the rolling file; closed years have an annual file.

    Just after New Year the annual file for the year that just closed may not exist yet, so the
    rolling file is kept as a fallback.
    """
    rolling = f"{BASE_URL}/PUB_DemandZonal.csv"
    if year >= current_year:
        return [rolling]
    return [f"{BASE_URL}/PUB_DemandZonal_{year}.csv", rolling]


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "scrape-ieso-zonal-demand/2.0"
    return session


def fetch_first_available(urls: list[str], timeout_seconds: int) -> tuple[str, str]:
    """Return (csv_text, url) for the first URL that exists. Non-404 errors are raised immediately."""
    session = _session()
    for url in urls:
        logger.info("Fetching IESO report", extra={"url": url, "timeout_seconds": timeout_seconds})
        response = session.get(url, timeout=timeout_seconds)
        if response.status_code == 404:
            logger.warning("Report not found, trying next candidate", extra={"url": url})
            continue
        response.raise_for_status()
        text = response.text
        logger.info("Fetched IESO report", extra={"url": url, "characters": len(text), "lines": text.count("\n")})
        return text, url
    raise FileNotFoundError(f"None of the candidate IESO report URLs exist: {urls}")
