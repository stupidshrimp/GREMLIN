import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


class DowntimeConversionTests(unittest.TestCase):
    """Limble reports task downtime in seconds; the pipeline must land on hours.

    Regression guard for the bug where a bare numeric downtime was treated as
    minutes, inflating every displayed downtime figure by 60x (e.g. a 3.5 h
    repair, 12600 s, showed as 210 h).
    """

    def _service(self) -> LifeDataService:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "gremlin.db"
        return LifeDataService(db_path, refresh_on_startup=False)

    def test_bare_seconds_convert_to_minutes(self):
        service = self._service()
        # 12600 s == 210 min == 3.5 h
        self.assertAlmostEqual(service._parse_downtime_minutes(12600), 210.0)
        self.assertAlmostEqual(service._parse_downtime_minutes(0), 0.0)
        self.assertIsNone(service._parse_downtime_minutes(None))
        self.assertIsNone(service._parse_downtime_minutes(""))

    def test_mapped_record_downtime_hours(self):
        service = self._service()
        mapped = service._map_raw_record({"taskID": 1, "downtime": 12600})
        self.assertAlmostEqual(mapped["downtime_minutes"], 210.0)
        self.assertAlmostEqual(mapped["downtime_hours"], 3.5)

    def test_legacy_prescaled_rows_are_not_rescaled(self):
        service = self._service()
        # Rows imported by the retired --downtime-unit=seconds ingestion path
        # stored `downtime` already in minutes and kept the original seconds in
        # provenance fields. The remap must not divide those by 60 again.
        mapped = service._map_raw_record(
            {
                "taskID": 2,
                "downtime": 210.0,  # already normalised to minutes
                "downtime_source_value": 12600,  # original seconds
                "downtime_source_unit": "seconds",
            }
        )
        self.assertAlmostEqual(mapped["downtime_minutes"], 210.0)
        self.assertAlmostEqual(mapped["downtime_hours"], 3.5)

    def test_explicit_text_units_are_honoured(self):
        service = self._service()
        # Legacy / hand-entered values that name their unit are not treated as seconds.
        self.assertAlmostEqual(service._parse_downtime_minutes("3.5 hours"), 210.0)
        self.assertAlmostEqual(service._parse_downtime_minutes("2 hr"), 120.0)
        # Plural abbreviation must not fall through to the seconds branch.
        self.assertAlmostEqual(service._parse_downtime_minutes("2 hrs"), 120.0)
        self.assertAlmostEqual(service._parse_downtime_minutes("45 min"), 45.0)
        # An explicit seconds label matches the default bare-number behaviour.
        self.assertAlmostEqual(service._parse_downtime_minutes("7200 seconds"), 120.0)


if __name__ == "__main__":
    unittest.main()
