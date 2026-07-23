from services.life_data_service import LifeDataService


def test_availability_percent_uses_scheduled_and_downtime_hours():
    assert LifeDataService._availability_percent_for_row(scheduled_hours=100, downtime_hours=12.5) == 87.5


def test_availability_percent_clamps_when_downtime_exceeds_schedule():
    assert LifeDataService._availability_percent_for_row(scheduled_hours=100, downtime_hours=150) == 0


def test_availability_percent_returns_none_when_schedule_is_missing():
    assert LifeDataService._availability_percent_for_row(scheduled_hours=0, downtime_hours=10) is None


def test_availability_scheduled_hours_uses_asset_schedule_rules():
    assert LifeDataService._availability_scheduled_hours("3102", "2026-01") == 528
    assert LifeDataService._availability_scheduled_hours("9999", "2026-01") == 440
