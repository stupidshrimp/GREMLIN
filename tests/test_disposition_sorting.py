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
import itertools
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

from services.life_data_service import MODELED_POPULATION_PLACEHOLDER, LifeDataService


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

    def test_a_task_id_that_is_not_a_number_is_not_treated_as_zero(self):
        """SQLite reads a CAST of "A-14" to REAL as 0.0, which is not what it is.

        Ordering on that cast put a task id with no number in it at the very top
        of the ascending page, in among the ones that are numbers, as the smallest
        of them.
        """

        self.add_wo("A-14")
        self.assertEqual(self.task_ids(sort="taskID", sort_dir="asc"), ["9", "10", "100", "A-14"])

    def test_it_is_not_treated_as_an_empty_cell_either(self):
        """Which is the same mistake with the other sign.

        "A-14" is neither a number nor a blank, and pinning it to the end in both
        directions -- where this column does keep its blanks -- reads it as the
        second. It is text, so it moves as a block of its own: after the numbers
        ascending, ahead of them descending, which is where a spreadsheet puts it
        and therefore where the workbook built from these rows puts it.
        """

        self.add_wo("A-14")
        self.assertEqual(self.task_ids(sort="taskID", sort_dir="desc"), ["A-14", "100", "10", "9"])

    def test_a_blank_still_sorts_last_in_both_directions(self):
        """The blanks keep the rule the text just stopped sharing.

        Sorting descending must not open on a page of rows with nothing in the
        column being sorted -- that is the whole reason blanks are pinned -- so the
        third block stays where it was.
        """

        self.add_wo("A-14")
        self.add_wo("")  # nothing in the column at all
        for direction in ("asc", "desc"):
            with self.subTest(direction=direction):
                rows = self.service.disposition_rows(self.ASSET, "wo", sort="taskID", sort_dir=direction)
                self.assertFalse(rows[-1]["taskID"], rows[-1]["taskID"])
                # And the text is in front of it rather than sharing its place.
                self.assertEqual(str(rows[-2]["taskID"] if direction == "asc" else rows[0]["taskID"]), "A-14")

    def test_whitespace_around_an_id_is_part_of_it_when_ordering_too(self):
        """Comparing " 7 " as "7" sorts it somewhere other than where it reads.

        The parse stopped trimming so the workbook could not rewrite the id; the
        ordering has to stop trimming for the same reason, or the two put the same
        record in different places. Trimmed, " 7 " would sort after "1e3" and
        "0009"; untrimmed it leads the text block, which is where the cell reads
        and where a spreadsheet puts it.
        """

        for task_id in (" 7 ", "0009", "1e3"):
            self.add_wo(task_id)
        self.assertEqual(self.task_ids(sort="taskID", sort_dir="asc")[3:], [" 7 ", "0009", "1e3"])

    def test_several_non_numbers_still_have_an_order_of_their_own(self):
        """A block of its own is not an arbitrary heap."""

        for task_id in ("B-2", "A-14"):
            self.add_wo(task_id)
        self.assertEqual(self.task_ids(sort="taskID", sort_dir="asc")[-2:], ["A-14", "B-2"])
        self.assertEqual(self.task_ids(sort="taskID", sort_dir="desc")[:2], ["B-2", "A-14"])

    def test_an_id_spelled_a_way_no_number_spells_it_sorts_as_text(self):
        """"0009", "+9" and "9e0" are each a record of their own.

        Each has 9 underneath it, and reading them as 9 makes four records into
        one -- on the screen and in the workbook alike. Only text that is the
        number's own canonical form is a number, so these sort in the text block
        by their own characters.
        """

        for task_id in ("0009", "+9", "9e0"):
            with self.subTest(task_id=task_id):
                self.add_wo(task_id)
                order = self.task_ids(sort="taskID", sort_dir="asc")
                self.assertEqual(order[:3], ["9", "10", "100"])
                self.assertIn(task_id, order[3:])

    def test_two_ids_that_differ_past_double_precision_still_order(self):
        """Rounding them to a double for the sort key merges them into one.

        -9007199254740993 and -9007199254740992 became the same key, tied, and
        were then separated by the text key, which orders negative numbers
        backwards -- so they came out in the opposite of their numeric order in
        both directions. The key is the integer itself, which SQLite compares
        exactly.
        """

        for task_id in ("-9007199254740993", "-9007199254740992"):
            self.add_wo(task_id)
        self.assertEqual(
            self.task_ids(sort="taskID", sort_dir="asc")[:2],
            ["-9007199254740993", "-9007199254740992"],
        )
        self.assertEqual(
            self.task_ids(sort="taskID", sort_dir="desc")[-2:],
            ["-9007199254740992", "-9007199254740993"],
        )

    def test_an_id_too_long_for_a_double_still_sorts_as_the_number_it_is(self):
        """The ordering runs in Python, where the integer is exact.

        The workbook keeps such an id as text because a spreadsheet cannot hold it
        without rounding; the screen has no such limit, so it stays a number here
        rather than being pushed down with the values that are not numbers at all.
        """

        self.add_wo("9007199254740993")
        self.assertEqual(
            self.task_ids(sort="taskID", sort_dir="asc"),
            ["9", "10", "100", "9007199254740993"],
        )


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


class ModeledPopulationSortTests(DispositionSortTestCase):
    """The Modeled Population placeholder is a value on that column, not a blank.

    A row with no population yet still reads something -- the population is
    created on save -- so ordering it as NULL pinned a visibly non-blank cell to
    the bottom in both directions, with the column sorting by something the
    screen does not say.
    """

    def name_population(self, task_id, population_id, population_name):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            mapped = conn.execute(
                "SELECT mapped_record_id FROM mapped_cmms_record WHERE task_id = ?", (str(task_id),)
            ).fetchone()["mapped_record_id"]
            conn.execute(
                "INSERT INTO modeled_population (modeled_population_id, asset_number, population_name)"
                " VALUES (?, ?, ?)",
                (population_id, self.ASSET, population_name),
            )
            conn.execute(
                "INSERT INTO event_disposition (mapped_record_id, disposition_category, is_current,"
                " modeled_population_id) VALUES (?, 'INCLUDED_FAILURE', 1, ?)",
                (mapped, population_id),
            )
            conn.commit()

    def setUp(self):
        super().setUp()
        self.add_wo("1")
        self.add_wo("2")  # no population, so its cell reads the placeholder
        self.add_wo("3")
        self.name_population("1", 99, "Zulu")
        self.name_population("3", 98, "Alpha")

    def shown(self, sort_dir):
        rows = self.service.disposition_rows(self.ASSET, "wo", sort="modeled_population_name", sort_dir=sort_dir)
        return [row["modeled_population_name"] or MODELED_POPULATION_PLACEHOLDER for row in rows]

    def test_the_placeholder_sorts_where_its_text_belongs(self):
        self.assertEqual(self.shown("asc"), ["Alpha", MODELED_POPULATION_PLACEHOLDER, "Zulu"])

    def test_descending_reverses_it_rather_than_pinning_it_last(self):
        self.assertEqual(self.shown("desc"), ["Zulu", MODELED_POPULATION_PLACEHOLDER, "Alpha"])


class FractionalSecondTests(DispositionSortTestCase):
    """Timestamps carrying milliseconds are dates like any other.

    The ingestion path converts a millisecond Unix timestamp by dividing it, so
    every date it writes from one carries a ".123000" fraction. Both parsers have
    to take those or the column silently stops being a date for most of its rows.
    """

    def test_the_sort_key_reads_a_fractional_second(self):
        for value in ("2023-11-14T22:13:19.123000+00:00", "2023-11-14T22:13:19.5Z", "2023-11-14 22:13:19.123"):
            with self.subTest(value=value):
                self.assertEqual(self.service._datetime_sort_key(value), "2023-11-14 22:13:19")

    def test_rows_with_fractional_timestamps_still_sort_chronologically(self):
        self.add_wo("1", completedDate_Final="2026-03-01T09:15:00.500000+00:00")
        self.add_wo("2", completedDate_Final="2025-12-31T08:00:00.250000+00:00")
        self.add_wo("3", completedDate_Final="2026-01-15T00:00:00.001000+00:00")
        self.assertEqual(self.task_ids(sort="completedDate_Final", sort_dir="asc"), ["2", "3", "1"])


class OrderChangedBySavingTests(DispositionSortTestCase):
    """Saving into the column being sorted by moves rows between pages.

    A page is a slice of a global ordering. Saving a value that ordering is built
    from re-cuts every page, so a screen still showing the pre-save slice can page
    on into a different order -- showing a row it already showed and skipping one
    it never did. mapped_record_id breaks ties within one ordering, not between
    two of them, so the client reloads the current page after such a save.
    """

    def setUp(self):
        super().setUp()
        for task_id in ("1", "2", "3", "4"):
            self.add_wo(task_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.mapped = {
                row["task_id"]: row["mapped_record_id"]
                for row in conn.execute("SELECT mapped_record_id, task_id FROM mapped_cmms_record")
            }
        for task_id in ("3", "4"):
            self.disposition(task_id, "EXCLUDED_NON_FAILURE")

    def disposition(self, task_id, category):
        self.service.save_disposition(
            self.mapped[task_id], kind="wo", disposition_category=category,
            disposition_text="reviewed", record_class_final="CORRECTIVE_WO",
        )

    def page(self, index, size=2):
        return self.task_ids(sort="disposition_category", sort_dir="asc", limit=size, offset=index * size)

    def test_a_save_really_does_re_cut_the_pages(self):
        """The precondition for the reload: without it the fix would be noise."""

        self.assertEqual(self.page(0), ["3", "4"])
        self.disposition("3", "UNKNOWN")
        # Row 3 has moved to the end, so the same offsets now describe other rows.
        self.assertEqual(self.task_ids(sort="disposition_category", sort_dir="asc"), ["4", "1", "2", "3"])

    def test_paging_on_from_a_stale_page_repeats_one_row_and_skips_another(self):
        seen = self.page(0)
        self.disposition("3", "UNKNOWN")
        seen += self.page(1)
        self.assertEqual(seen, ["3", "4", "2", "3"])
        repeated = sorted({task for task in seen if seen.count(task) > 1})
        never_shown = sorted({"1", "2", "3", "4"} - set(seen))
        self.assertEqual(repeated, ["3"])
        self.assertEqual(never_shown, ["1"])


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


def test_the_payload_names_the_modeled_population_placeholder(monkeypatch, tmp_path):
    """The screen renders it and the ORDER BY sorts by it, so there is one copy."""

    payload = _disposition(_client(monkeypatch, tmp_path)).get_json()
    assert payload["modeled_population_placeholder"] == MODELED_POPULATION_PLACEHOLDER


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


def test_the_script_renders_the_population_placeholder_the_server_named():
    """A second copy in the client would drift from the one the SQL sorts by."""

    assert "row.modeled_population_name || data.modeled_population_placeholder" in SCRIPT
    assert MODELED_POPULATION_PLACEHOLDER not in SCRIPT


def test_the_script_reloads_the_page_after_saving_into_the_sorted_column():
    """OrderChangedBySavingTests shows what the reload is for."""

    assert "const editableKeys = new Set(extraColumns.map((column) => column.key));" in SCRIPT
    assert "if (!editableKeys.has(state.dispositionSort.key)) return;" in SCRIPT
    assert "loadDispositionPage(data.kind, data.scope, data.page_index);" in SCRIPT


def test_the_script_accepts_a_fractional_second():
    """Python's parser takes them, so the browser's has to as well."""

    parser = re.search(r"function parseRecordDate\(value\)(.*?)\n  }\n", SCRIPT, re.S)
    assert parser, "the date parser is no longer where the test can read it"
    assert r"(?::(\d{2})(?:\.(\d+))?)?" in parser.group(1)


# The two parsers, run against the same values.
#
# Four separate review findings on this change were the same fault: the browser
# calling something a date that the server refuses. Each showed up the same way
# -- a value rendered as a normalised date on screen while the ORDER BY sorted it
# with the blanks, unfindable by searching for the text in its own cell.
#
# The two directions are not equally bad, which is what this asserts:
#
#   browser accepts, server refuses  -- the bug above. Never allowed.
#   server accepts, browser refuses  -- the cell shows the stored text as-is and
#                                       the server still orders the row
#                                       correctly, so it is cosmetic. Allowed,
#                                       but listed, so a new one is visible.
#
# The asymmetry is deliberate rather than laziness. The server's accepted set is
# strptime's, and strptime has quirks a regex cannot mirror without becoming a
# bug factory itself: it takes "2026-1-15" and "2026-1-15 15:00:30" but not
# "2026-1-15T15:00". Chasing that exactly is how three of the four findings
# happened. Being stricter than the server is safe; being looser is not.
#
# The corpus is generated rather than written out, because the fourth finding was
# a shape nobody thought to list ("1/15/2026T15:00" -- the separator, not the
# time). A cross-product does not depend on anyone's imagination.


def _date_corpus():
    """Every shape, crossed, rather than the ones anyone thought to write down.

    The separator and the run of spaces before the offset are their own axes
    because both produced findings: a "T" where the server wants a space, and
    "\\s*" where the server allows exactly one.
    """

    dates = [
        "2026-01-15", "2026-1-15", "1/15/2026", "01/15/2026", "1/15/26",
        "2025-02-31", "2/31/2025", "2024-02-29", "2025-02-29", "13/45/2025",
    ]
    separators = ["", " ", "  ", "T"]
    times = ["", "15:00", "15:00:30", "15:00:00.123000", "15:00:00.5", "25:00", "9:05"]
    zone_gaps = ["", " ", "  "]
    zones = ["", "Z", "+00:00", "-05:00", "+0000"]
    values = set()
    for date, separator, time, gap, zone in itertools.product(dates, separators, times, zone_gaps, zones):
        if bool(separator) != bool(time):
            continue  # a separator needs a time, and a time needs a separator
        if not time and zone:
            continue  # a zone with no clock time is not a shape either side sees
        if gap and not zone:
            continue  # a gap is only a gap when something follows it
        values.add(f"{date}{separator}{time}{gap}{zone}")
    values.update(["", "   ", "not a date", "TBD", "2026", "15:00", "1699999999"])
    return sorted(values)


DATE_CORPUS = _date_corpus()

# Values the server reads and the browser does not. Safe, because the cell then
# shows what is stored and the server still orders the row; see the note above.
# Both are unpadded ISO, which strptime takes through "%Y-%m-%d" and
# "%Y-%m-%d %H:%M:%S" -- and only through those, which is why the browser does
# not try to guess at them.
# The three ways the server reads a shape the browser will not. Listing the
# values themselves does not survive the corpus growing, and the point is not
# which strings they are -- it is that each one is explained by a known leniency
# rather than being a new kind of divergence.
_SERVER_LENIENCIES = (
    ("repeated whitespace", lambda value: "  " in value),
    ("a gap before the offset", lambda value: re.search(r"\d\s+(?:Z|[+-]\d{2}:?\d{2})$", value) is not None),
    (
        "an unpadded month or day",
        lambda value: re.match(r"^\d{4}-\d{1,2}-\d{1,2}(?:\D|$)", value) is not None
        and re.match(r"^\d{4}-\d{2}-\d{2}(?:\D|$)", value) is None,
    ),
)


def _server_leniency(value):
    for name, matches in _SERVER_LENIENCIES:
        if matches(value):
            return name
    return None


_READ_CLIENT_PARSER = """
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
// The script is one big IIFE that touches `document` as it loads, so the three
// functions under test are lifted out by brace matching rather than required.
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
eval(grab("utcInstant") + ";" + grab("fractionMillis") + ";" + grab("parseRecordDate"));
const corpus = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify(corpus.map((value) => Boolean(parseRecordDate(value)))));
"""


def _parser_verdicts(tmp_path):
    """What each side says about every value in the corpus."""

    service = LifeDataService.__new__(LifeDataService)
    runner = tmp_path / "parse.js"
    runner.write_text(_READ_CLIENT_PARSER)
    corpus_json = tmp_path / "corpus.json"
    corpus_json.write_text(json.dumps(DATE_CORPUS))
    script = Path(__file__).resolve().parent.parent / "static" / "js" / "life_data_analysis.js"
    result = subprocess.run(
        ["node", str(runner), str(script), str(corpus_json)],
        capture_output=True, text=True, check=True,
    )
    browser = json.loads(result.stdout)
    server = [service._datetime_sort_key(value) is not None for value in DATE_CORPUS]
    return server, browser


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the client parser")
def test_the_browser_never_calls_a_date_what_the_server_refuses(tmp_path):
    server, browser = _parser_verdicts(tmp_path)
    looser = [
        value for value, on_server, in_browser in zip(DATE_CORPUS, server, browser)
        if in_browser and not on_server
    ]
    assert not looser, (
        "the browser reads these as dates and the server does not, so each would render "
        "normalised on screen, sort with the blanks, and be unfindable by searching for "
        f"the text in its own cell: {looser}"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the client parser")
def test_every_value_only_the_server_reads_is_a_known_leniency(tmp_path):
    """Cosmetic rather than broken, but a new *kind* should still be noticed.

    Being stricter costs the tidier rendering on that cell and nothing else, so
    what matters is not how many of these there are but that each one is the
    server being loose about whitespace or zero-padding, not the two sides
    disagreeing about something that matters.
    """

    server, browser = _parser_verdicts(tmp_path)
    unexplained = [
        value for value, on_server, in_browser in zip(DATE_CORPUS, server, browser)
        if on_server and not in_browser and _server_leniency(value) is None
    ]
    assert not unexplained, (
        "the server reads these and the browser does not, and it is not one of the "
        f"known leniencies: {unexplained}"
    )


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
