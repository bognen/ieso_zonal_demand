from datetime import date

import pandas as pd
import pytest

from ieso.transform import LONG_KEYS, WIDE_KEYS, parse_report, to_long_format, validate

from .conftest import INGESTED_AT


def test_parse_basic_shape(wide):
    assert not wide.empty
    assert set(wide["year"]) == {2026}
    assert wide["hour_ending"].between(1, 25).all()
    assert not wide.duplicated(WIDE_KEYS).any()
    first = wide.iloc[0]
    assert first["ontario_demand_mw"] == 16526 and first["toronto_mw"] == 5568
    assert first["interval_end_local_naive"] - first["interval_start_local_naive"] == pd.Timedelta(hours=1)


def test_parse_other_year_returns_empty(sample_text):
    assert parse_report(sample_text, 2020, "u", INGESTED_AT).empty


def test_parse_missing_header_raises():
    with pytest.raises(ValueError, match="header"):
        parse_report("nonsense\n1,2,3", 2026, "u", INGESTED_AT)


def test_parse_drops_blank_rows(sample_text):
    text = sample_text.rstrip("\n") + "\n2026-12-31,24,,,,,,,,,,,,,\n"
    assert len(parse_report(text, 2026, "u", INGESTED_AT)) == len(parse_report(sample_text, 2026, "u", INGESTED_AT))


def test_long_format(wide):
    long = to_long_format(wide)
    assert len(long) == len(wide) * 11  # 10 zones + ontario
    assert "ontario" in set(long["zone"])
    assert not long.duplicated(LONG_KEYS).any()
    row = long[(long["zone"] == "toronto") & (long["hour_ending"] == 1)].iloc[0]
    assert row["load_mw"] == 5568.0


def test_validate_flags_incomplete_past_day(wide):
    broken = wide[~((wide["delivery_date"] == "2026-01-02") & (wide["hour_ending"] > 12))]
    issues = validate(broken, today=date(2026, 4, 5))
    assert any("2026-01-02" in i and "12 hours" in i for i in issues)


def test_validate_ignores_today_partial(wide):
    last_day = wide["delivery_date"].max().date()
    partial = wide[~((wide["delivery_date"].dt.date == last_day) & (wide["hour_ending"] > 6))]
    assert validate(partial, today=last_day) == []
