"""The example the Disposition walk-through picks for somebody who hasn't.

With no asset chosen, the tour asks the server for the asset whose table has the
most rows for the Record Type and Rows showing, and picks it the way choosing it
from the list would, so the editor steps have a real table to point at. The
editor it draws is the tour's doing, so it mustn't offer the editor tour -- not
even after Skip has closed the tour that asked for it.
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


def _records(service: LifeDataService, asset_number: str, count: int, *, kind="wo", reviewed=0):
    """`count` records of one kind on an asset, the first `reviewed` of them already dispositioned."""

    pm = kind == "pm"
    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT OR IGNORE INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        for index in range(count):
            raw_id = conn.execute("INSERT INTO raw_cmms_record (import_batch_id, raw_json) VALUES (1, '{}')").lastrowid
            mapped_id = conn.execute(
                """
                INSERT INTO mapped_cmms_record (
                    raw_record_id, import_batch_id, asset_number, task_id, task_name, completed_date_final,
                    record_class_auto, is_corrective_wo_candidate, is_pm_candidate
                ) VALUES (?, 1, ?, ?, ?, '2024-01-15', ?, ?, ?)
                """,
                (
                    raw_id,
                    asset_number,
                    f"{asset_number}-{kind}-{index}",
                    f"Record {index}",
                    "PM" if pm else "CORRECTIVE_WO",
                    int(not pm),
                    int(pm),
                ),
            ).lastrowid
            if index < reviewed:
                # A reviewed exclusion: dispositioned, and so out of the backlog.
                conn.execute(
                    "INSERT INTO event_disposition (mapped_record_id, record_class_final, disposition_category) "
                    "VALUES (?, ?, ?)",
                    (mapped_id, "PM" if pm else "CORRECTIVE_WO", "PM_CONTEXT_ONLY" if pm else "EXCLUDED_NON_FAILURE"),
                )


class DispositionTourExampleAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)

    def _example(self, kind="wo", *, new=False, search=None):
        return self.service.disposition_tour_example_asset(kind, only_needing_disposition=new, search=search)

    def test_there_is_no_example_without_a_record_of_that_kind(self):
        self.assertIsNone(self._example("wo"))
        self.assertIsNone(self._example("pm"))
        _records(self.service, "A-1", 3, kind="pm")
        self.assertIsNone(self._example("wo"))

    def test_it_is_the_asset_with_the_most_rows_of_the_record_type_showing(self):
        _records(self.service, "A-1", 2, kind="wo")
        _records(self.service, "A-1", 9, kind="pm")
        _records(self.service, "B-2", 5, kind="wo")
        _records(self.service, "B-2", 1, kind="pm")

        self.assertEqual(self._example("wo"), "B-2")
        self.assertEqual(self._example("pm"), "A-1")

    def test_only_new_rows_counts_the_backlog(self):
        # A-1 has more work orders, but every one is dispositioned already, so its
        # table would be empty with Rows on "Only new / undispositioned".
        _records(self.service, "A-1", 6, kind="wo", reviewed=6)
        _records(self.service, "B-2", 3, kind="wo", reviewed=1)

        self.assertEqual(self._example("wo"), "A-1")
        self.assertEqual(self._example("wo", new=True), "B-2")
        self.assertEqual(
            self.service.disposition_row_count("B-2", "wo", only_needing_disposition=True), 2
        )

    def test_a_search_counts_only_the_rows_it_matches(self):
        # Somebody who typed a search before opening the tour would otherwise
        # be shown an example whose table says no rows match.
        _records(self.service, "A-1", 6, kind="wo")
        _records(self.service, "B-2", 2, kind="wo")

        self.assertEqual(self._example("wo"), "A-1")
        self.assertEqual(self._example("wo", search="B-2-wo-1"), "B-2")
        self.assertIsNone(self._example("wo", search="nothing like this"))

    def test_it_is_a_number_the_asset_list_offers(self):
        # The list offers " C-3 " as "C-3", and the table asked for "C-3" matches
        # none of those rows, so C-3 has nothing to show as an example.
        _records(self.service, " C-3 ", 9, kind="wo")
        _records(self.service, "A-1", 2, kind="wo")

        example = self._example("wo")
        self.assertEqual(example, "A-1")
        self.assertIn(example, [option["asset_number"] for option in self.service.asset_number_options()])

    def test_the_example_has_rows_for_the_table_to_show(self):
        _records(self.service, "A-1", 4, kind="pm", reviewed=1)
        for kind, new in (("pm", False), ("pm", True)):
            example = self._example(kind, new=new)
            self.assertGreater(
                self.service.disposition_row_count(example, kind, only_needing_disposition=new), 0
            )


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    return importlib.reload(app)


def test_the_example_is_asked_for_by_record_type(monkeypatch, tmp_path):
    client = _app(monkeypatch, tmp_path).app.test_client()
    response = client.get("/life-data-analysis/api/disposition-tour-example?kind=pm&scope=new&search=seal")
    assert response.status_code == 200
    assert response.get_json() == {"asset_number": None}
    # Without one the example could be an asset with none of the records showing.
    assert client.get("/life-data-analysis/api/disposition-tour-example").status_code == 400


# pickTourExampleAsset and offerDispositionTour lifted out of the page script,
# the way test_analysis_tour_example.py lifts the analysis page's, and run
# against stand-ins for what they call: a request for the example that answers
# when told to, the asset list, whether the tour is still open, and a
# chooseAsset that stands for drawing the editor, which is where the page
# offers the editor tour. startOwedDispositionTour notes each offer that stuck.
_OFFER_HARNESS = r"""
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

// Answerable before it is asked: the picker waits for the asset list first.
let answer;
const answered = new Promise((resolve) => { answer = resolve; });
const asked = [];
const getJson = (url) => { asked.push(url); return answered; };
const LISTED = () => new Map([["P-100", { asset_number: "P-100" }]]);
const state = {
  pageMode: "disposition",
  selectedAsset: null,
  assetByNumber: LISTED(),
};
let assetsLoaded = Promise.resolve();
let assetsArrive = () => {};
let whileLoading = () => {};
const chooseAsset = async (asset) => {
  state.selectedAsset = asset.asset_number;
  whileLoading();
};
let tourOpen = true;
// The page tour has been seen -- it is the one that just closed -- and the
// editor part hasn't.
const window = {
  gremlinTour: { isOpen: () => tourOpen, seen: (key) => key === "page" },
};
const DISPOSITION_TOUR_SEEN_KEY = "page";
const DISPOSITION_EDITOR_TOUR_SEEN_KEY = "editor";
let dispositionTourOwed = false;
const offered = [];
const startOwedDispositionTour = () => { if (dispositionTourOwed) offered.push("editor"); };
let tourRun = 1;
let tourExampleAsset = null;
let tourNoExample = false;
let tourExampleLoading = false;
eval("async " + grab("pickTourExampleAsset") + ";" + grab("offerDispositionTour"));

const scenarios = {
  // The tour waits: its example's editor is drawn under it.
  waited: { answered: () => { whileLoading = () => { offerDispositionTour(); }; } },
  // Skip once the example has answered but while its editor is still loading.
  // The editor lands after the tour has closed, and the page would take it for
  // one drawn on its own and offer the editor tour straight after the Skip.
  // An editor somebody draws for themselves later still may.
  skipped_while_loading: {
    answered: () => { whileLoading = () => { tourOpen = false; offerDispositionTour(); }; },
    after: () => { offerDispositionTour(); },
  },
  // Show me around works before the asset list is in. The example isn't asked
  // for until it is: it is looked up in that list, and on a first visit after
  // an import, loading the list is what maps the records it is chosen from.
  assets_late: {
    before: () => {
      state.assetByNumber = new Map();
      assetsLoaded = new Promise((resolve) => {
        assetsArrive = () => { state.assetByNumber = LISTED(); resolve(); };
      });
    },
    late: () => { assetsArrive(); },
  },
};

(async () => {
  const scenario = scenarios[process.argv[3]];
  if (scenario.before) scenario.before();
  const picking = pickTourExampleAsset("/life-data-analysis/api/disposition-tour-example?kind=wo&scope=all");
  if (scenario.answered) scenario.answered();
  answer({ asset_number: "P-100" });
  let askedBeforeList = null;
  if (scenario.late) {
    // Give the picker every chance to ask, and to take the answer in, while
    // the list is still out.
    await new Promise((resolve) => setTimeout(resolve, 0));
    askedBeforeList = asked.length;
    scenario.late();
  }
  await picking;
  const offeredWhileLoading = offered.length;
  const owedWhileLoading = dispositionTourOwed;
  if (scenario.after) scenario.after();
  console.log(JSON.stringify({
    asked, askedBeforeList, selected: state.selectedAsset, example: tourExampleAsset, noExample: tourNoExample,
    offeredWhileLoading, owedWhileLoading, offeredAfter: offered.length - offeredWhileLoading,
  }));
})();
"""


def _offer(tmp_path, scenario):
    runner = tmp_path / "offer.js"
    runner.write_text(_OFFER_HARNESS)
    result = subprocess.run(
        ["node", str(runner), str(ANALYSIS_JS), scenario],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's example picker")
def test_the_example_is_picked_and_its_editor_drawn_under_the_tour(tmp_path):
    seen = _offer(tmp_path, "waited")
    assert seen["asked"] == ["/life-data-analysis/api/disposition-tour-example?kind=wo&scope=all"]
    assert (seen["selected"], seen["example"]) == ("P-100", "P-100")
    # The tour is already showing that editor; it isn't a reason to offer another.
    assert (seen["offeredWhileLoading"], seen["owedWhileLoading"]) == (0, False)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's example picker")
def test_the_example_is_not_asked_for_until_the_asset_list_is_in(tmp_path):
    """Show me around can be pressed as soon as the page is drawn, before the
    asset list is in. An example looked up in the empty list would be taken for
    missing; and on a first visit after an import, the list's request is what
    maps the records, so an example asked for alongside it can find none. Either
    way the tour, which offers its action once, couldn't try again."""
    seen = _offer(tmp_path, "assets_late")
    assert seen["askedBeforeList"] == 0
    assert (seen["selected"], seen["example"], seen["noExample"]) == ("P-100", "P-100", False)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's example picker")
def test_skip_while_the_example_loads_does_not_open_the_editor_tour(tmp_path):
    """Skipped means no more tour: the example's own editor landing isn't a reason.

    The editor tour offers itself the first time an editor is drawn, for
    somebody who skipped the page tour before it got there. The example's editor
    is the tour's doing, so it doesn't count; one drawn after it does.
    """
    seen = _offer(tmp_path, "skipped_while_loading")
    assert seen["selected"] == "P-100"
    assert (seen["offeredWhileLoading"], seen["owedWhileLoading"]) == (0, False)
    assert seen["offeredAfter"] == 1


if __name__ == "__main__":
    unittest.main()
