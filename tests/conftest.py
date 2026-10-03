import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE_CSV = ROOT / "tests" / "data" / "sample_2026.csv"
INGESTED_AT = pd.Timestamp("2026-04-06T10:15:00", tz="UTC")


@pytest.fixture(scope="session")
def sample_text() -> str:
    return SAMPLE_CSV.read_text()


@pytest.fixture()
def wide(sample_text):
    from ieso.transform import parse_report
    return parse_report(sample_text, 2026, "http://example/PUB_DemandZonal.csv", INGESTED_AT)
