from repositories.pm_calendar_repo import PmCalendarRepository

repo = PmCalendarRepository("pm_calendar_test.db")
repo.ensure_schema()
repo.upsert_tasks([{
    "task_id": "123",
    "asset_id": "7",
    "asset_number": "7",
    "asset_name": "Pump",
    "task_name": "Monthly lube PM",
    "status_raw": "open",
    "due_date": "2026-09-15",
    "completed_date": None,
    "is_completed": 0,
}])
print(repo.fetch_tasks())
print(repo.asset_options())