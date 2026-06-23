from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from availability_dashboard.availability_calculator import AssetGroup, AvailabilityCalculator, LinkedDowntimeRule, RawWorkOrder, month_range_for_year, safe_availability, weekday_count


class FakeSource:
    def __init__(self, rows, manual_ot=None, rules=None, assets=None):
        self.rows = rows
        self.manual_ot = manual_ot or {}
        self.rules = rules or []
        self.assets = assets or ["3102", "3107"]

    def load_asset_groups(self, include_only=False):
        return [AssetGroup(None, "Salvagnini", self.assets, 24, 1, 2, 3, 18, True, "", 1)]

    def load_display_names(self):
        return {"3102": "PA", "3107": "ACN"}

    def load_linked_rules(self):
        return self.rules

    def load_manual_ot(self, selected_year):
        return self.manual_ot

    def load_raw_work_orders(self, selected_year, utc_offset_hours):
        return self.rows


def test_availability_repository_preserves_explicit_db_path_for_session_reuse():
    from availability_dashboard.availability_repository import AvailabilityRepository
    from services.life_data_service import DEFAULT_DB_PATH_CANDIDATES

    session_path = DEFAULT_DB_PATH_CANDIDATES[4]
    higher_priority_path = DEFAULT_DB_PATH_CANDIDATES[0]

    def session_path_is_file(path):
        return path == session_path

    def higher_priority_path_is_file(path):
        return path == higher_priority_path

    with patch.object(Path, "is_file", session_path_is_file), patch.object(AvailabilityRepository, "ensure_schema"):
        startup_repository = AvailabilityRepository()

    with patch.object(Path, "is_file", higher_priority_path_is_file), patch.object(AvailabilityRepository, "ensure_schema"):
        reused_repository = AvailabilityRepository(startup_repository.db_path)

    assert startup_repository.db_path == session_path
    assert reused_repository.db_path == session_path


def result_for(results, asset, month="2026-01-01"):
    return next(row for row in results if row.asset_number == asset and row.month_date == month)



def test_future_year_returns_no_months_or_result_rows():
    assert month_range_for_year(2027, today=date(2026, 6, 2)) == []
    results = AvailabilityCalculator(FakeSource([]), today=date(2026, 6, 2)).calculate_availability(2027)
    assert results == []


def test_weekday_scheduled_hours_for_salvagnini_january_2026():
    assert weekday_count(2026, 1) == 22
    results = AvailabilityCalculator(FakeSource([]), today=date(2026, 6, 2)).calculate_availability(2026)
    assert result_for(results, "3102").scheduled_hours == 396


def test_availability_clamps_at_zero_when_downtime_exceeds_schedule():
    assert safe_availability(500, 396) == 0


def test_type_4_rows_are_excluded_from_all_counts():
    rows = [RawWorkOrder("pm", "3102", None, datetime(2026, 1, 5), datetime(2026, 1, 5), 120, 4)]
    results = AvailabilityCalculator(FakeSource(rows), today=date(2026, 6, 2)).calculate_availability(2026)
    jan = result_for(results, "3102")
    assert jan.downtime_hours == 0
    assert jan.total_wo_count == 0
    assert jan.zero_downtime_wo_count == 0
    assert jan.overlap_count == 0



def test_mapped_type_1_pm_rows_are_excluded_from_all_counts():
    rows = [RawWorkOrder("pm-type-1", "3102", None, datetime(2026, 1, 5), datetime(2026, 1, 5), 120, "1")]
    results = AvailabilityCalculator(FakeSource(rows), today=date(2026, 6, 2)).calculate_availability(2026)
    jan = result_for(results, "3102")
    assert jan.downtime_hours == 0
    assert jan.total_wo_count == 0
    assert jan.zero_downtime_wo_count == 0
    assert jan.overlap_count == 0


def test_mapped_classifier_pm_rows_are_excluded_even_when_type_is_corrective(tmp_path):
    from availability_dashboard.availability_calculator import AvailabilityCalculator
    from availability_dashboard.availability_repository import AvailabilityRepository

    repo = AvailabilityRepository(tmp_path / "GREMLIN.db")
    with repo.write_connection() as conn:
        conn.execute(
            """
            CREATE TABLE mapped_cmms_record (
                task_id TEXT, asset_number TEXT, asset_name TEXT, created_date_final TEXT, created_datetime_raw TEXT, created_date_raw TEXT,
                completed_date_final TEXT, completed_datetime_raw TEXT, completed_date_raw TEXT, downtime_minutes REAL, type_raw TEXT,
                status_raw TEXT, task_name TEXT, description_raw TEXT, completion_notes TEXT,
                record_class_auto TEXT, record_class_final TEXT, is_pm_candidate INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO mapped_cmms_record(
                task_id, asset_number, asset_name, created_date_final, downtime_minutes, type_raw, task_name,
                record_class_auto, record_class_final, is_pm_candidate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("classified-pm", "3102", "PA", "2026-01-05", 120, "6", "Monthly PM", "PM", None, 1),
        )

    rows = repo.load_raw_work_orders(2026, 0)
    assert len(rows) == 1
    assert rows[0].is_pm_candidate is True
    results = AvailabilityCalculator(repo, today=date(2026, 6, 2)).calculate_availability(2026, 0)
    jan = result_for(results, "3102")
    assert jan.downtime_hours == 0
    assert jan.total_wo_count == 0
    assert jan.overlap_count == 0

def test_manual_ot_adjusts_scheduled_hours_without_changing_base_schedule():
    manual = {("Salvagnini", "3102", "2026-01-01"): 10}
    results = AvailabilityCalculator(FakeSource([], manual_ot=manual), today=date(2026, 6, 2)).calculate_availability(2026)
    jan = result_for(results, "3102")
    assert jan.scheduled_hours == 396
    assert jan.manual_ot_hours == 10
    assert jan.adjusted_scheduled_hours == 406


def test_linked_downtime_is_applied_generically_by_rule():
    rows = [RawWorkOrder("wo", "3107", None, datetime(2026, 1, 7), datetime(2026, 1, 7), 1200, 6)]
    rules = [LinkedDowntimeRule("SALV", "3102", "3107", 0.5)]
    results = AvailabilityCalculator(FakeSource(rows, rules=rules), today=date(2026, 6, 2)).calculate_availability(2026)
    jan = result_for(results, "3102")
    assert jan.linked_downtime_hours == 10
    assert jan.downtime_logic == "Direct + linked downtime"



def test_linked_downtime_uses_raw_rows_for_linked_assets_not_included_in_groups():
    rows = [RawWorkOrder("support", "9999", None, datetime(2026, 1, 7), datetime(2026, 1, 7), 600, 6)]
    rules = [LinkedDowntimeRule("SUPPORT", "3102", "9999", 0.25)]
    results = AvailabilityCalculator(FakeSource(rows, rules=rules, assets=["3102"]), today=date(2026, 6, 2)).calculate_availability(2026)
    jan = result_for(results, "3102")
    assert jan.linked_downtime_hours == 2.5
    assert jan.adjusted_downtime_hours == 2.5

def test_no_work_orders_get_no_entry_flag_and_note():
    results = AvailabilityCalculator(FakeSource([]), today=date(2026, 6, 2)).calculate_availability(2026)
    jan = result_for(results, "3102")
    assert jan.total_wo_count == 0
    assert jan.no_wo_entries_flag == 1
    assert jan.zero_no_entry_note == "No WO entries this month"


def test_zero_downtime_work_order_counts_and_note():
    rows = [
        RawWorkOrder(str(i), "3102", None, datetime(2026, 1, 5 + i), datetime(2026, 1, 5 + i), 0 if i < 3 else 60, 6)
        for i in range(5)
    ]
    results = AvailabilityCalculator(FakeSource(rows), today=date(2026, 6, 2)).calculate_availability(2026)
    jan = result_for(results, "3102")
    assert jan.total_wo_count == 5
    assert jan.zero_downtime_wo_count == 3
    assert jan.zero_downtime_wo_percent == 0.6
    assert jan.zero_no_entry_note == "3 WO(s) with 0 downtime"


def test_repository_seeds_asset_numbers_as_text_and_includes_pangborn(tmp_path):
    from availability_dashboard.availability_repository import AvailabilityRepository

    repo = AvailabilityRepository(tmp_path / "GREMLIN.db")
    group = next(g for g in repo.load_asset_groups() if g.asset_group == "Building 1 Secondary Finishing")
    assert group.asset_numbers == ["505", "1682", "4028", "758", "3326", "2667", "987"]


def test_gremlin_life_data_page_offers_availability_analysis_type():
    source = open("gremlin_gui.py", encoding="utf-8").read()
    assert 'self.analysis_type_combo.addItems(("Weibull Analysis", "Availability Analysis"))' in source
    assert 'if analysis_type == "Availability Analysis":' in source
    assert 'self._perform_availability_analysis()' in source
    assert '"📉  Availability Dashboard"' not in source


def test_raw_json_fallback_parses_downtime_strings_with_units(tmp_path):
    import json
    from availability_dashboard.availability_repository import AvailabilityRepository

    repo = AvailabilityRepository(tmp_path / "GREMLIN.db")
    with repo.write_connection() as conn:
        conn.execute("CREATE TABLE raw_cmms_record (raw_json TEXT)")
        conn.execute(
            "INSERT INTO raw_cmms_record(raw_json) VALUES (?)",
            (json.dumps({"taskID": "raw-1", "Asset Number": "3102", "createdDate": "2026-01-05", "downtime": "2 hr", "type": 6}),),
        )

    rows = repo.load_raw_work_orders(2026, 5)
    assert len(rows) == 1
    assert rows[0].downtime_minutes == 120


def test_raw_json_fallback_applies_utc_offset_to_offset_bearing_space_timestamps(tmp_path):
    import json
    from availability_dashboard.availability_repository import AvailabilityRepository

    repo = AvailabilityRepository(tmp_path / "GREMLIN.db")
    with repo.write_connection() as conn:
        conn.execute("CREATE TABLE raw_cmms_record (raw_json TEXT)")
        conn.execute(
            "INSERT INTO raw_cmms_record(raw_json) VALUES (?)",
            (json.dumps({"taskID": "raw-2", "Asset Number": "3102", "createdDate": "2026-02-01 01:00:00+00:00", "downtime": 30, "type": 6}),),
        )

    rows = repo.load_raw_work_orders(2026, -5)
    assert len(rows) == 1
    assert rows[0].created_date.year == 2026
    assert rows[0].created_date.month == 1
    assert rows[0].created_date.day == 31


def test_gremlin_availability_analysis_preserves_zero_utc_offset_setting():
    source = open("gremlin_gui.py", encoding="utf-8").read()
    assert 'utc_offset_hours = float(settings.get("utc_offset_hours") or 5.0)' not in source
    assert 'utc_offset_hours = 5.0 if utc_offset_setting is None else float(utc_offset_setting)' in source


def test_replace_manual_ot_entries_for_year_deletes_removed_rows(tmp_path):
    from availability_dashboard.availability_repository import AvailabilityRepository

    repo = AvailabilityRepository(tmp_path / "GREMLIN.db")
    repo.save_manual_ot_entries([
        ("Salvagnini", "3102", "2026-01-01", 10),
        ("Salvagnini", "3102", "2026-02-01", 5),
        ("Salvagnini", "3102", "2025-01-01", 7),
    ])

    repo.replace_manual_ot_entries_for_year(2026, [("Salvagnini", "3102", "2026-02-01", 2)])

    assert repo.load_manual_ot(2026) == {("Salvagnini", "3102", "2026-02-01"): 2.0}
    assert repo.load_manual_ot(2025) == {("Salvagnini", "3102", "2025-01-01"): 7.0}


def test_replace_goal_entries_for_year_deletes_removed_rows(tmp_path):
    from availability_dashboard.availability_repository import AvailabilityRepository

    repo = AvailabilityRepository(tmp_path / "GREMLIN.db")
    repo.save_goal_entries([
        ("Salvagnini", "2026-01-01", 0.9),
        ("Salvagnini", "2026-02-01", 0.92),
        ("Salvagnini", "2025-01-01", 0.93),
    ])

    repo.replace_goal_entries_for_year(2026, [("Salvagnini", "2026-02-01", 0.95)])

    assert repo.load_goal_percent(2026) == {("Salvagnini", "2026-02-01"): 0.95}
    assert repo.load_goal_percent(2025) == {("Salvagnini", "2025-01-01"): 0.93}


def test_raw_json_fallback_runs_when_mapped_table_exists_but_has_no_rows(tmp_path):
    import json
    from availability_dashboard.availability_repository import AvailabilityRepository

    repo = AvailabilityRepository(tmp_path / "GREMLIN.db")
    with repo.write_connection() as conn:
        conn.execute(
            """
            CREATE TABLE mapped_cmms_record (
                task_id TEXT, asset_number TEXT, asset_name TEXT, created_date_final TEXT, created_datetime_raw TEXT, created_date_raw TEXT,
                completed_date_final TEXT, completed_datetime_raw TEXT, completed_date_raw TEXT, downtime_minutes REAL, type_raw TEXT,
                status_raw TEXT, task_name TEXT, description_raw TEXT, completion_notes TEXT
            )
            """
        )
        conn.execute("CREATE TABLE raw_cmms_record (raw_json TEXT)")
        conn.execute(
            "INSERT INTO raw_cmms_record(raw_json) VALUES (?)",
            (json.dumps({"taskID": "raw-fallback", "Asset Number": "3102", "createdDate": "2026-01-05", "downtime": "1 hour", "type": 6}),),
        )

    rows = repo.load_raw_work_orders(2026, 5)
    assert len(rows) == 1
    assert rows[0].task_id == "raw-fallback"
    assert rows[0].downtime_minutes == 60


def test_raw_json_fallback_uses_date_completed_final_alias_for_overlap_count(tmp_path):
    import json
    from availability_dashboard.availability_calculator import AvailabilityCalculator
    from availability_dashboard.availability_repository import AvailabilityRepository

    repo = AvailabilityRepository(tmp_path / "GREMLIN.db")
    with repo.write_connection() as conn:
        conn.execute("CREATE TABLE raw_cmms_record (raw_json TEXT)")
        conn.execute(
            "INSERT INTO raw_cmms_record(raw_json) VALUES (?)",
            (json.dumps({
                "taskID": "overlap-raw",
                "Asset Number": "3102",
                "createdDate": "2026-01-31 23:00:00",
                "dateCompleted_Final": "2026-02-01 01:00:00",
                "downtime": 120,
                "type": 6,
            }),),
        )

    rows = repo.load_raw_work_orders(2026, 0)
    assert len(rows) == 1
    assert rows[0].completed_date is not None
    results = AvailabilityCalculator(repo, today=date(2026, 6, 2)).calculate_availability(2026, 0)
    jan = next(row for row in results if row.asset_number == "3102" and row.month_date == "2026-01-01")
    assert jan.overlap_count == 1


def test_life_data_workspace_clears_when_analysis_type_changes_for_same_asset():
    source = open("gremlin_gui.py", encoding="utf-8").read()
    assert "self.selected_analysis_type: str | None = None" in source
    assert "previous_analysis_type = self.selected_analysis_type" in source
    assert "previous_analysis_type != self.selected_analysis_type" in source
