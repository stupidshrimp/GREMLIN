"""The example the Perform an Analysis walk-through picks for somebody who hasn't.

With no asset chosen, the tour asks the server for one to show round and picks it
the way choosing it from the list would; with no mechanism shown, it opens one of
the fits the Highest-beta list names. Neither may write: the example is a read,
and the fit is read back through saved-analysis rather than run again.
"""

import importlib
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


def _seed(service: LifeDataService, asset_number: str, mechanism: str, completed_dates, *, category="INCLUDED_FAILURE"):
    """One failure mode/mechanism on an asset, with a corrective WO for each date."""

    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT OR IGNORE INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        mode_id = conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES (?)", (f"{mechanism} mode",)).lastrowid
        mechanism_id = conn.execute(
            "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES (?, ?)",
            (mechanism, mode_id),
        ).lastrowid
        for index, completed_date in enumerate(completed_dates, start=1):
            raw_id = conn.execute("INSERT INTO raw_cmms_record (import_batch_id, raw_json) VALUES (1, '{}')").lastrowid
            mapped_id = conn.execute(
                """
                INSERT INTO mapped_cmms_record (
                    raw_record_id, import_batch_id, asset_number, task_id, task_name,
                    completed_date_final, downtime_hours, record_class_auto, is_corrective_wo_candidate
                ) VALUES (?, 1, ?, ?, ?, ?, 1.0, 'CORRECTIVE_WO', 1)
                """,
                (raw_id, asset_number, f"{asset_number}-WO-{index}", f"Failure {index}", completed_date),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO event_disposition (
                    mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                    include_in_weibull_candidate, failure_mode_id, failure_mechanism_id
                ) VALUES (?, 'CORRECTIVE_WO', ?, 1, 1, ?, ?)
                """,
                (mapped_id, category, mode_id, mechanism_id),
            )
    return int(mode_id), int(mechanism_id)


THREE_DATES = ["2024-01-15", "2024-02-15", "2024-03-15"]
FIVE_DATES = ["2024-01-10", "2024-02-10", "2024-03-10", "2024-04-10", "2024-05-10"]


class TourExampleAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)

    def _fit(self, asset_number, ids):
        mode_id, mechanism_id = ids
        return self.service.perform_weibull_analysis(
            asset_number,
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=mode_id,
            failure_mechanism_id=mechanism_id,
        )

    def _counts(self):
        with self.service.connect() as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("modeled_population", "analysis_dataset", "weibull_analysis_run", "weibull_result")
            }

    def test_there_is_no_example_without_an_included_failure(self):
        self.assertIsNone(self.service.tour_example_asset())
        _seed(self.service, "A-1", "Excluded", THREE_DATES, category="EXCLUDED_NON_FAILURE")
        self.assertIsNone(self.service.tour_example_asset())

    def test_without_saved_fits_it_is_the_asset_with_the_most_failures(self):
        _seed(self.service, "A-1", "Few", THREE_DATES)
        _seed(self.service, "B-2", "Many", FIVE_DATES)

        self.assertEqual(self.service.tour_example_asset(), "B-2")

    def test_an_asset_with_a_saved_fit_comes_first(self):
        # The tour can only open a fit that is already saved, so an asset with one
        # has more to show than a busier asset without.
        fitted = _seed(self.service, "A-1", "Few", THREE_DATES)
        _seed(self.service, "B-2", "Many", FIVE_DATES)
        self._fit("A-1", fitted)

        self.assertEqual(self.service.tour_example_asset(), "A-1")

    def test_picking_the_example_writes_nothing(self):
        fitted = _seed(self.service, "A-1", "Few", THREE_DATES)
        self._fit("A-1", fitted)
        before = self._counts()

        self.service.tour_example_asset()

        self.assertEqual(self._counts(), before)

    def test_a_ranked_fit_carries_what_opens_it_again(self):
        """The tour opens the Weibull example from the Highest-beta list.

        So a ranking row has to name the failure mode and mechanism that
        saved-analysis reads the fit back by, not only their display names.
        """
        mode_id, mechanism_id = _seed(self.service, "A-1", "Few", THREE_DATES)
        performed = self._fit("A-1", (mode_id, mechanism_id))

        [ranked] = self.service.latest_failure_mechanism_beta_rankings("A-1")

        self.assertEqual((ranked["failure_mode_id"], ranked["failure_mechanism_id"]), (mode_id, mechanism_id))
        saved = self.service.load_saved_weibull_analysis(
            "A-1",
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=ranked["failure_mode_id"],
            failure_mechanism_id=ranked["failure_mechanism_id"],
        )
        self.assertEqual(saved.result_id, performed.result_id)


def test_the_example_is_readable_without_logging_in(monkeypatch, tmp_path):
    """The tour runs for viewers and signed-out visitors too, so it can't be role-gated."""
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    module = importlib.reload(app)
    response = module.app.test_client().get("/life-data-analysis/api/tour-example")
    assert response.status_code == 200
    assert response.get_json() == {"asset_number": None}


if __name__ == "__main__":
    unittest.main()
