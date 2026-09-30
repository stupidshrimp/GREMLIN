"""The example the Perform an Analysis walk-through picks for somebody who hasn't.

With no asset chosen, the tour asks the server for one to show round and picks it
the way choosing it from the list would; with no mechanism shown, it opens one of
the fits the Highest-beta list names. Neither may write: the example is a read,
and the fit is read back through saved-analysis rather than run again.
"""

import importlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

from services.life_data_service import LifeDataService

ANALYSIS_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "life_data_analysis.js"


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


# pickTourExampleAsset lifted out of the page script, the way
# test_disposition_sorting.py lifts the date parser, and run against stand-ins
# for what it calls: a request for the example that answers when told to, the
# asset list, and whether the tour is still open. Each scenario prints what the
# page ended up with.
_PICK_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (name) => {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error("unbalanced " + name);
};

const API = "/life-data-analysis/api";
let answer;
const getJson = () => new Promise((resolve) => { answer = resolve; });
const state = {
  selectedAsset: null,
  assetByNumber: new Map([["P-100", { asset_number: "P-100" }], ["P-200", { asset_number: "P-200" }]]),
};
const chosen = [];
const chooseAsset = async (asset) => { chosen.push(asset.asset_number); state.selectedAsset = asset.asset_number; };
let tourOpen = true;
const window = { gremlinTour: { isOpen: () => tourOpen } };
let tourRun = 1;
let tourExampleAsset = null;
let tourNoExample = false;
eval("async " + grab("pickTourExampleAsset"));

const scenarios = {
  // Nothing happens while the request is out: the example is picked.
  waited: async () => {},
  // Skip, then the user's own pick from the list.
  skipped_then_picked: async () => { tourOpen = false; state.selectedAsset = "P-200"; },
  // Skip and nothing else: the page is theirs again, example or no.
  skipped: async () => { tourOpen = false; },
  // Skip and straight back in: a new tour, which asks for its own example.
  skipped_and_reopened: async () => { tourRun += 1; },
};

(async () => {
  const picking = pickTourExampleAsset();
  await scenarios[process.argv[3]]();
  answer({ asset_number: "P-100" });
  await picking;
  console.log(JSON.stringify({ chosen, selected: state.selectedAsset, example: tourExampleAsset }));
})();
"""


def _pick(tmp_path, scenario):
    runner = tmp_path / "pick.js"
    runner.write_text(_PICK_HARNESS)
    result = subprocess.run(
        ["node", str(runner), str(ANALYSIS_JS), scenario],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's example picker")
def test_the_example_is_picked_when_the_tour_waits_for_it(tmp_path):
    assert _pick(tmp_path, "waited") == {"chosen": ["P-100"], "selected": "P-100", "example": "P-100"}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's example picker")
@pytest.mark.parametrize(
    "scenario, selected",
    [("skipped_then_picked", "P-200"), ("skipped", None), ("skipped_and_reopened", None)],
)
def test_an_example_arriving_after_skip_is_not_applied(tmp_path, scenario, selected):
    """Skip stays live while the example is requested, and there is no loading veil.

    So somebody can close the tour and pick their own asset before the answer
    comes back. Applying it then would swap their choice for the example.
    """
    assert _pick(tmp_path, scenario) == {"chosen": [], "selected": selected, "example": None}


if __name__ == "__main__":
    unittest.main()
