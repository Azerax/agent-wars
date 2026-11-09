from datetime import date, datetime

import pytest

from stockagent import clock as mcal
from stockagent.config import Config, FeeConfig, load_config
from stockagent.fees import compute_fees


def test_buy_has_no_regulatory_fees():
    f = compute_fees(FeeConfig(), "buy", 50, 2.0)
    assert f.total == 0.0


def test_small_sell_pays_a_cent_of_each_regulatory_fee():
    f = compute_fees(FeeConfig(), "sell", 50, 2.0)  # $100 sale
    assert f.sec_fee == 0.01  # $0.00206 rounded up
    assert f.taf_fee == 0.01  # $0.00975 rounded up
    assert f.total == 0.02


def test_large_sell_hits_taf_cap():
    f = compute_fees(FeeConfig(), "sell", 100_000, 1.0)
    assert f.sec_fee == 2.06
    assert f.taf_fee == 9.79


def test_ibkr_preset_minimum_and_cap(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[fees]\npreset = "ibkr_pro_fixed"\n')
    cfg = load_config(p)
    assert compute_fees(cfg.fees, "buy", 100, 2.0).commission == 1.0  # $0.50 -> $1 minimum
    assert compute_fees(cfg.fees, "buy", 1000, 0.30).commission == 3.0  # $5 capped at 1% of $300


def test_unknown_config_key_is_an_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[risk]\nrisk_per_trad = 0.1\n")
    with pytest.raises(ValueError):
        load_config(p)


@pytest.mark.parametrize("d", [
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 3, 26), date(2027, 6, 18), date(2027, 7, 5), date(2027, 12, 24), date(2025, 1, 9),
])
def test_nyse_holidays(d):
    assert not mcal.is_trading_day(d)


def test_early_closes():
    assert date(2026, 11, 27) in mcal.early_closes(2026)
    assert date(2026, 12, 24) in mcal.early_closes(2026)
    assert date(2025, 7, 3) in mcal.early_closes(2025)
    _, close = mcal.session_times(date(2026, 11, 27))
    assert close.hour == 13


def test_t_plus_one_skips_bank_holidays():
    assert mcal.settlement_date(date(2026, 10, 9)) == date(2026, 10, 13)  # Columbus Day
    assert mcal.settlement_date(date(2026, 11, 10)) == date(2026, 11, 12)  # Veterans Day
    assert mcal.settlement_date(date(2026, 11, 25)) == date(2026, 11, 27)  # Thanksgiving
    assert mcal.settlement_date(date(2026, 11, 6)) == date(2026, 11, 9)  # weekend


def test_trading_day_counting():
    assert mcal.trading_days_between(date(2026, 11, 2), date(2026, 11, 6)) == 4
    assert mcal.trading_days_between(date(2026, 11, 24), date(2026, 11, 30)) == 3
    assert mcal.next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)


def test_market_phase():
    d = date(2026, 11, 2)
    assert mcal.market_phase(datetime(2026, 11, 2, 8, 0, tzinfo=mcal.ET)) == "premarket"
    assert mcal.market_phase(datetime(2026, 11, 2, 10, 0, tzinfo=mcal.ET)) == "open"
    assert mcal.market_phase(datetime(2026, 11, 2, 17, 0, tzinfo=mcal.ET)) == "afterhours"
    assert mcal.market_phase(datetime(2026, 11, 1, 10, 0, tzinfo=mcal.ET)) == "closed"
    assert mcal.minutes_to_close(datetime.combine(d, datetime.min.time(), mcal.ET).replace(hour=15, minute=50)) == 10


def test_default_config_is_valid():
    cfg = Config()
    assert cfg.risk.starting_cash == 100.0
    assert cfg.target.multiple == 2.0 and cfg.target.period_trading_days == 4
