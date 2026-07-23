import unittest

from services.ingestion_service import IngestionService
from services.life_data_service import LifeDataService


class _DummyClient:
    pass


class _DummyRepo:
    db_path = ":memory:"


class DowntimeHoursTests(unittest.TestCase):
    def test_ingestion_preserves_raw_downtime_and_adds_hour_derivatives(self):
        service = IngestionService(_DummyClient(), _DummyRepo(), fetch_assets=False, refresh_mapping=False, log=lambda _: None)

        record = service.transform({"taskID": 123, "assetID": 456, "downtime": 2.5}, asset_index=type("Empty", (), {"enrich": lambda *_: None})())

        self.assertEqual(record["downtime"], 2.5)
        self.assertEqual(record["downtime_source_value"], 2.5)
        self.assertEqual(record["downtime_source_unit"], "hours")
        self.assertEqual(record["downtime_hours"], 2.5)
        self.assertEqual(record["downtime_minutes"], 150.0)

    def test_mapping_treats_numeric_raw_downtime_as_hours(self):
        service = object.__new__(LifeDataService)

        mapped = service._map_raw_record({"taskID": 123, "downtime": 2.5})

        self.assertEqual(mapped["downtime_raw"], 2.5)
        self.assertEqual(mapped["downtime_hours"], 2.5)
        self.assertEqual(mapped["downtime_minutes"], 150.0)

    def test_explicit_minute_strings_still_convert_to_hours(self):
        service = object.__new__(LifeDataService)

        self.assertEqual(service._parse_downtime_hours("90 minutes"), 1.5)
        self.assertEqual(service._parse_downtime_hours("2 hr"), 2.0)


if __name__ == "__main__":
    unittest.main()
