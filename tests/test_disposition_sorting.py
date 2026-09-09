"""Sorting the disposition table by a column that is not text.

Every column on that table arrives as TEXT (or as a bare REAL): SQLite has no
date type, the CMMS hands over task ids as strings, and the page renders
whatever it is given. Comparing those as text is what the screen used to do, and
it reads wrong in the two places it matters most -- "10" sorts before "9", and a
column of dates sorts by the digits each value happens to start with.

The other half is where the sort happens. The table pages 50 rows at a time, so
sorting the rows already on screen answers "which of these 50 is oldest" when the
question is "which of this asset's records is oldest". These tests pin the
ordering to the whole selection by asking for a one-row page and checking which
row it is.
"""

import importlib
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


class DispositionSortTestCase(unittest.TestCase):
    """A service over a handful of corrective work orders on one asset."""

    ASSET = "2288"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "gremlin.db"
        self.service = LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS import_batch "
                "(import_batch_id INTEGER PRIMARY KEY, status TEXT)"
            )
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (0, 'COMPLETED')")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 0, "
                "source_record_id TEXT, raw_json TEXT NOT NULL, raw_content_hash TEXT)"
            )
            conn.commit()
        self._next_raw_id = 0

    def add_wo(self, task_id, **fields):
        """Seed one corrective work order through the real mapping path."""

        self._next_raw_id += 1
        task = {
            "taskID": task_id,
            "assetID": self.ASSET,
            "Asset Number": self.ASSET,
            "type": "6",  # corrective work order
            "name": f"Task {task_id}",
        }
        task.update(fields)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 0, ?)",
                (self._next_raw_id, json.dumps(task)),
            )
            conn.commit()
        self.service.refresh_mapped_cmms_records()

    def task_ids(self, **kwargs):
        rows = self.service.disposition_rows(self.ASSET, "wo", **kwargs)
        return [str(row["taskID"]) for row in rows]


class DateSortTests(DispositionSortTestCase):
    def setUp(self):
        super().setUp()
        # Three dates whose chronological order is the reverse of their text
        # order, written in the three shapes the mapper is known to receive.
        self.add_wo("1", completedDate_Final="2026-01-05T00:00:00+00:00")
        self.add_wo("2", completedDate_Final="12/31/2025 08:00")
        self.add_wo("3", completedDate_Final="2025-02-20 17:30:00")

    def test_dates_sort_chronologically_not_alphabetically(self):
        self.assertEqual(self.task_ids(sort="completedDate_Final", sort_dir="asc"), ["3", "2", "1"])

    def test_descending_reverses_the_chronology(self):
        self.assertEqual(self.task_ids(sort="completedDate_Final", sort_dir="desc"), ["1", "2", "3"])

    def test_a_blank_date_sorts_last_in_both_directions(self):
        self.add_wo("4")  # no completed date at all
        self.assertEqual(self.task_ids(sort="completedDate_Final", sort_dir="asc")[-1], "4")
        self.assertEqual(self.task_ids(sort="completedDate_Final", sort_dir="desc")[-1], "4")

    def test_the_default_order_reads_the_dates_as_dates_too(self):
        """No column chosen still means oldest first, not lowest text first."""

        self.assertEqual(self.task_ids(), ["3", "2", "1"])

    def test_the_search_box_matches_the_normalised_date(self):
        """A date copied out of a cell finds its row.

        The table renders "2026-01-05 00:00" for a value the database holds as
        "2026-01-05T00:00:00+00:00", and pasting what is on screen into the
        search box has to work.
        """

        self.assertEqual(self.task_ids(search="2026-01-05 00:00"), ["1"])
        self.assertEqual(self.task_ids(search="2025-12-31 08:00"), ["2"])


class NumericSortTests(DispositionSortTestCase):
    def setUp(self):
        super().setUp()
        # Limble reports downtime in seconds; these are 2, 10 and 100 hours,
        # which sort 10, 100, 2 as text.
        self.add_wo("9", downtime=100 * 3600)
        self.add_wo("10", downtime=2 * 3600)
        self.add_wo("100", downtime=10 * 3600)

    def test_downtime_sorts_as_a_number(self):
        self.assertEqual(self.task_ids(sort="downtime", sort_dir="asc"), ["10", "100", "9"])
        self.assertEqual(self.task_ids(sort="downtime", sort_dir="desc"), ["9", "100", "10"])

    def test_task_id_sorts_as_a_number(self):
        self.assertEqual(self.task_ids(sort="taskID", sort_dir="asc"), ["9", "10", "100"])
        self.assertEqual(self.task_ids(sort="taskID", sort_dir="desc"), ["100", "10", "9"])

    def test_a_blank_downtime_sorts_last_in_both_directions(self):
        self.add_wo("7")  # no downtime reported
        self.assertEqual(self.task_ids(sort="downtime", sort_dir="asc")[-1], "7")
        self.assertEqual(self.task_ids(sort="downtime", sort_dir="desc")[-1], "7")


class SortSpansEveryPageTests(DispositionSortTestCase):
    """The sort covers the selection, not the page."""

    def setUp(self):
        super().setUp()
        self.add_wo("1", downtime=50 * 3600, completedDate_Final="2026-03-01T00:00:00+00:00")
        self.add_wo("2", downtime=5 * 3600, completedDate_Final="2026-02-01T00:00:00+00:00")
        self.add_wo("3", downtime=500 * 3600, completedDate_Final="2026-01-01T00:00:00+00:00")

    def test_the_first_page_holds_the_highest_value_in_the_whole_set(self):
        # Row 3 is last in the default order, so a page-local sort could never
        # bring it to the front of page one.
        self.assertEqual(self.task_ids(sort="downtime", sort_dir="desc", limit=1, offset=0), ["3"])
        self.assertEqual(self.task_ids(sort="downtime", sort_dir="desc", limit=1, offset=1), ["1"])
        self.assertEqual(self.task_ids(sort="downtime", sort_dir="desc", limit=1, offset=2), ["2"])

    def test_paging_a_sorted_column_never_repeats_or_drops_a_row(self):
        """Rows that tie still need a total order, or paging double-counts them."""

        for task_id in ("4", "5", "6"):
            self.add_wo(task_id)  # every one of these ties on every column
        seen = []
        for offset in range(0, 6, 2):
            seen.extend(self.task_ids(sort="downtime", sort_dir="asc", limit=2, offset=offset))
        self.assertEqual(sorted(seen), sorted(["1", "2", "3", "4", "5", "6"]))


class NarrativeSortTests(DispositionSortTestCase):
    """The Failure Narrative column orders what the cell actually reads.

    The cell shows only the boxes that were filled in, each captioned. Ordering
    the four raw values run together orders something else entirely: a row whose
    only box is Area Affected "Z" would sort before one whose only box is
    Condition "A", while the two cells read the other way round.
    """

    def rendered(self, row):
        labels = (("area_affected", "Area Affected"), ("condition_found", "Condition"),
                  ("cause", "Cause"), ("action_taken", "Action"))
        return " · ".join(f"{label}: {row[key].strip()}" for key, label in labels if row.get(key))

    def setUp(self):
        super().setUp()
        self.add_wo("1", **{"Area Affected": "Z drive end"})
        self.add_wo("2", **{"Condition": "A bearing hot"})
        self.add_wo("3", **{"Area Affected": "A infeed", "Cause": "Seal wear"})

    def narrative_order(self, sort_dir):
        rows = self.service.disposition_rows(self.ASSET, "wo", sort="failure_narrative", sort_dir=sort_dir)
        return [self.rendered(row) for row in rows]

    def test_the_order_is_the_order_of_the_rendered_cells(self):
        shown = self.narrative_order("asc")
        self.assertEqual(shown, sorted(shown, key=str.casefold))
        # The row whose caption sorts first, not the row whose raw value does.
        self.assertEqual(self.task_ids(sort="failure_narrative", sort_dir="asc"), ["3", "1", "2"])

    def test_descending_reverses_it(self):
        self.assertEqual(self.narrative_order("desc"), sorted(self.narrative_order("asc"), key=str.casefold, reverse=True))

    def test_a_record_with_no_narrative_sorts_last_in_both_directions(self):
        self.add_wo("4")
        self.assertEqual(self.task_ids(sort="failure_narrative", sort_dir="asc")[-1], "4")
        self.assertEqual(self.task_ids(sort="failure_narrative", sort_dir="desc")[-1], "4")


class ImpossibleDateTests(DispositionSortTestCase):
    """A day that does not exist is not a date.

    The browser builds its dates with Date.UTC/Date.parse, which roll an
    impossible one forward -- 2025-02-31 becomes March 3 -- so both halves have
    to refuse them, or the screen shows a day the record does not have and the
    search box cannot find it.
    """

    def test_the_sort_key_refuses_a_date_that_does_not_exist(self):
        for value in ("2025-02-31", "2025-02-31T00:00:00Z", "2/31/2025", "13/45/2025",
                      "2026-01-15 25:00", "2025-02-29", "not a date", "", None):
            with self.subTest(value=value):
                self.assertIsNone(self.service._datetime_sort_key(value))

    def test_a_real_leap_day_is_still_a_date(self):
        self.assertEqual(self.service._datetime_sort_key("2024-02-29"), "2024-02-29 00:00:00")

    def test_such_a_row_sorts_last_rather_than_under_an_invented_date(self):
        self.add_wo("1", completedDate_Final="2026-01-05T00:00:00+00:00")
        self.add_wo("2", completedDate_Final="2025-02-31")
        self.assertEqual(self.task_ids(sort="completedDate_Final", sort_dir="asc"), ["1", "2"])
        self.assertEqual(self.task_ids(sort="completedDate_Final", sort_dir="desc"), ["1", "2"])


class SortColumnContractTests(DispositionSortTestCase):
    def test_every_advertised_column_can_actually_be_ordered_by(self):
        """The names handed to the browser are the names the SQL accepts."""

        self.add_wo("1", completedDate_Final="2026-01-05T00:00:00+00:00", downtime=3600)
        for kind in ("wo", "pm"):
            columns = self.service.disposition_sort_columns(kind)
            self.assertIn("downtime", columns)
            for column, column_type in columns.items():
                self.assertIn(column_type, ("text", "number", "datetime", "boolean"), column)
                for direction in ("asc", "desc"):
                    with self.subTest(kind=kind, column=column, direction=direction):
                        self.service.disposition_rows(self.ASSET, kind, sort=column, sort_dir=direction)

    def test_the_two_record_types_offer_their_own_columns(self):
        wo = self.service.disposition_sort_columns("wo")
        pm = self.service.disposition_sort_columns("pm")
        self.assertIn("failure_mode", wo)
        self.assertNotIn("failure_mode", pm)
        self.assertIn("reset_target_failure_mode", pm)
        self.assertNotIn("reset_target_failure_mode", wo)

    def test_an_unknown_column_name_is_ignored_rather_than_run(self):
        """The column name picks an ORDER BY expression, so it is never pasted in."""

        self.add_wo("1", completedDate_Final="2026-01-05T00:00:00+00:00")
        self.add_wo("2", completedDate_Final="2025-02-20T00:00:00+00:00")
        injected = "m.task_id; DROP TABLE mapped_cmms_record"
        self.assertEqual(self.task_ids(sort=injected), self.task_ids())
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM mapped_cmms_record").fetchone()[0], 2
            )


# ---- the endpoint --------------------------------------------------------
# The browser types its columns from what the endpoint advertises and shows the
# sort the endpoint says it applied, so these pin the contract between them.


def _disposition(client, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return client.get(f"/life-data-analysis/api/dispositions?asset=2288&kind=wo&{query}")


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    import app

    return importlib.reload(app).app.test_client()


def test_the_payload_advertises_the_sortable_columns_and_their_types(monkeypatch, tmp_path):
    """The browser types its columns from this, so the two cannot drift apart."""

    payload = _disposition(_client(monkeypatch, tmp_path)).get_json()
    columns = payload["sortable_columns"]
    assert columns["completedDate_Final"] == "datetime"
    assert columns["createdDate_Final"] == "datetime"
    assert columns["downtime"] == "number"
    assert columns["taskID"] == "number"
    assert columns["include_in_weibull_candidate"] == "boolean"
    assert columns["name"] == "text"
    # Every column the table draws has to be one the server can order by, or its
    # header offers a sort that does nothing.
    for column in payload["display_columns"]:
        assert column in columns, column


def test_a_requested_sort_is_echoed_back(monkeypatch, tmp_path):
    payload = _disposition(_client(monkeypatch, tmp_path), sort="downtime", dir="desc").get_json()
    assert payload["sort"] == "downtime"
    assert payload["sort_dir"] == "desc"


def test_an_unknown_column_comes_back_cleared(monkeypatch, tmp_path):
    """Switching Record Type can leave a column selected that the new type lacks.

    The name also picks an ORDER BY expression, so anything unrecognised is
    dropped rather than passed along.
    """

    client = _client(monkeypatch, tmp_path)
    payload = _disposition(client, sort="failure_mode", dir="asc").get_json()
    assert payload["sort"] == "failure_mode"
    pm = client.get(
        "/life-data-analysis/api/dispositions?asset=2288&kind=pm&sort=failure_mode&dir=asc"
    ).get_json()
    assert pm["sort"] == ""
    assert pm["sort_dir"] == "asc"


def test_an_unknown_direction_falls_back_to_ascending(monkeypatch, tmp_path):
    payload = _disposition(_client(monkeypatch, tmp_path), sort="downtime", dir="sideways").get_json()
    assert payload["sort_dir"] == "asc"


# ---- the browser side ----------------------------------------------------
# The table is drawn by static/js/life_data_analysis.js, which names each column
# by the key the server orders it by. A renamed key on either side leaves a
# header whose sort menu quietly does nothing, which is exactly the kind of break
# that looks fine in a diff, so these read the script the way the Excel-help
# tests read the template.

SCRIPT = (Path(__file__).resolve().parent.parent / "static" / "js" / "life_data_analysis.js").read_text()


def test_the_editable_columns_the_script_draws_are_columns_the_server_can_sort():
    """The disposition columns are built in JS, so only the names tie them together."""

    service = LifeDataService.__new__(LifeDataService)
    block = re.search(r"const extraColumns = \(isPm(.*?)\)\.map\(typed\);", SCRIPT, re.S)
    assert block, "the disposition table no longer builds its editable columns from a list"
    keys = re.findall(r'\{ key: "([a-zA-Z_]+)", label: "[^"]+" \}', block.group(1))
    assert keys, "the disposition table no longer names its columns"
    sortable = set(service.disposition_sort_columns("wo")) | set(service.disposition_sort_columns("pm"))
    for key in keys:
        assert key in sortable, key


def test_the_script_asks_the_server_to_do_the_sorting():
    """Sorting the rows already rendered would only ever sort one page of 50."""

    assert "&sort=${encodeURIComponent(state.dispositionSort.key)}&dir=${state.dispositionSort.dir}" in SCRIPT
    assert "sortable_columns" in SCRIPT


def test_the_script_reads_the_date_columns_as_dates():
    """Both halves parse the stored text the same way, or they disagree on order."""

    assert "function parseRecordDate" in SCRIPT
    assert 'column.type === "datetime" ? formatRecordDate(value)' in SCRIPT


def test_a_column_filter_survives_the_reload_that_sorting_now_costs():
    """Sorting used to reorder the rows in place, which left filters alone.

    It reloads the table from the server now, so the filter state has to be
    handed back in or an active filter would silently disappear and the rows it
    was hiding would return.
    """

    assert "onFiltersChanged" in SCRIPT
    assert "filters: state.dispositionFilters" in SCRIPT
    # Keyed by column, because the two record types do not draw the same columns
    # in the same positions.
    assert "active[columns[col].key] = Array.from(set)" in SCRIPT
    # And dropped when the selection changes, since the rows change with it.
    assert "state.dispositionFilters = {};" in SCRIPT


def test_the_table_says_so_when_a_filter_hides_every_row_on_the_page():
    """A carried-over filter can match nothing on the page it lands on.

    A header sitting over an empty table reads as a page that failed to load,
    and on a table this wide the column doing the hiding is offscreen.
    """

    assert "Every row on this page is hidden by a column filter" in SCRIPT


def test_the_script_checks_a_date_it_builds_against_the_digits_it_came_from():
    """Date.UTC and Date.parse both roll 2025-02-31 forward to March 3.

    ImpossibleDateTests pins the server half; this pins that the browser still
    reads its instants back rather than displaying the rolled-over day.
    """

    assert "function utcInstant" in SCRIPT
    parser = re.search(r"function parseRecordDate\(value\)(.*?)\n  }\n", SCRIPT, re.S)
    assert parser, "the date parser is no longer where the test can read it"
    # Both shapes it parses -- ISO and the m/d/y one -- have to go through it.
    assert parser.group(1).count("utcInstant(") == 2


if __name__ == "__main__":
    unittest.main()
