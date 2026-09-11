"""The disposition workbook: real Excel types, and the rows the screen asked for.

Two things used to make the download less useful than the table it copies.

Every cell was written as text, so Excel sorted it as text -- the same wrong
order test_disposition_sorting pins the screen against ("10" ahead of "9", dates
by the digits they start with), except that in a workbook there is no server to
re-sort it. A text date cannot be filtered by month or reformatted either: to
Excel the value is a sentence, not a day.

And the Rows selector stopped at the screen. Setting it to "Only new /
undispositioned" and clicking Download Excel handed back every eligible row,
leaving the reader to find the backlog again in a spreadsheet -- which is the one
job that setting exists to do.

The workbook is written by hand (no openpyxl on the deployment), so these read
the parts back out of the zip the same way.
"""

import importlib
import json
import re
import sqlite3
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.life_data_service import (
    COLUMN_TYPE_DATETIME,
    COLUMN_TYPE_NUMBER,
    DISPLAY_COLUMN_SOURCES,
    EXCEL_COLUMN_TYPES,
    EXCEL_DATE_EPOCH,
    EXCEL_PM_DISPOSITION_COLUMNS,
    EXCEL_STYLE_DATETIME,
    EXCEL_WO_DISPOSITION_COLUMNS,
    LifeDataService,
)

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"main": MAIN}


class Sheet:
    """One worksheet, read back out of the workbook as cells keyed by header."""

    def __init__(self, path: Path, sheet_part: str = "xl/worksheets/sheet1.xml"):
        with zipfile.ZipFile(path) as workbook:
            self.parts = set(workbook.namelist())
            self.xml = {name: workbook.read(name).decode("utf-8") for name in self.parts}
        self.root = ET.fromstring(self.xml[sheet_part])
        self.rows = self.root.findall(".//main:sheetData/main:row", NS)
        self.headers = [self._text(cell) for cell in self.rows[0]]

    @staticmethod
    def _text(cell: ET.Element) -> str:
        node = cell.find(".//main:t", NS)
        return node.text if node is not None and node.text else ""

    def column(self, header: str) -> list[ET.Element]:
        index = self.headers.index(header)
        return [list(row)[index] for row in self.rows[1:]]

    def values(self, header: str) -> list:
        """Each cell in ``header`` as the Python value Excel would read from it."""

        read = []
        for cell in self.column(header):
            value = cell.find("main:v", NS)
            if cell.attrib.get("t") == "inlineStr":
                read.append(self._text(cell))
            elif value is None:
                read.append(None)
            else:
                read.append(float(value.text))
        return read

    def styles(self, header: str) -> list[str | None]:
        return [cell.attrib.get("s") for cell in self.column(header)]


class DispositionExcelTestCase(unittest.TestCase):
    """A service over a handful of corrective work orders on one asset."""

    ASSET = "2288"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.db_path = self.workspace / "gremlin.db"
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

    def export(self, name="wo.xlsx", kind="wo", **kwargs) -> Sheet:
        path = self.workspace / name
        self.service.export_disposition_excel(self.ASSET, kind, path, **kwargs)
        return Sheet(path)


class ColumnTypeCoverageTests(unittest.TestCase):
    """Every exported column is a column the writer knows the type of."""

    def test_every_header_of_both_workbooks_has_a_type(self):
        for headers in (EXCEL_WO_DISPOSITION_COLUMNS, EXCEL_PM_DISPOSITION_COLUMNS):
            for header in headers:
                with self.subTest(header=header):
                    self.assertIn(header, EXCEL_COLUMN_TYPES)

    def test_the_record_columns_carry_the_type_the_screen_sorts_them_by(self):
        """A column has one type, not one per surface.

        The table's ORDER BY and the workbook read the same columns out of the
        same rows; typing them apart is how a workbook comes to disagree with the
        screen that produced it about which record is oldest.
        """

        for header, (_, column_type) in DISPLAY_COLUMN_SOURCES.items():
            with self.subTest(header=header):
                self.assertEqual(EXCEL_COLUMN_TYPES[header], column_type)


class DateColumnTests(DispositionExcelTestCase):
    """Dates reach the sheet as dates."""

    def setUp(self):
        super().setUp()
        # The same three the screen is pinned against: chronological order is the
        # reverse of text order, in the three shapes the mapper receives.
        self.add_wo("1", completedDate_Final="2026-01-05T00:00:00+00:00")
        self.add_wo("2", completedDate_Final="12/31/2025 08:00")
        self.add_wo("3", completedDate_Final="2025-02-20 17:30:00")

    def serials(self):
        return self.export().values("completedDate_Final")

    @staticmethod
    def read_back(serial: float) -> datetime:
        """A serial as the datetime a reader takes from it.

        Rounded to the nearest second, which is what Excel, LibreOffice and
        openpyxl all do: a clock time is a fraction of a day and most of them have
        no exact binary form, so every serial sits a fraction of a microsecond off
        the second it means.
        """

        return EXCEL_DATE_EPOCH + timedelta(days=serial // 1) + timedelta(
            seconds=round((serial % 1) * 86400)
        )

    def test_a_date_is_written_as_the_number_excel_stores_a_date_as(self):
        stored = {self.read_back(serial) for serial in self.serials()}
        self.assertEqual(
            stored,
            {
                datetime(2026, 1, 5, tzinfo=timezone.utc),
                datetime(2025, 12, 31, 8, 0, tzinfo=timezone.utc),
                datetime(2025, 2, 20, 17, 30, tzinfo=timezone.utc),
            },
        )

    def test_sorting_that_column_in_excel_is_chronological(self):
        """The whole point: as text these three sort 12/31, 2025-02, 2026-01."""

        by_cell_value = [self.read_back(serial).strftime("%Y-%m-%d %H:%M") for serial in sorted(self.serials())]
        self.assertEqual(
            by_cell_value,
            ["2025-02-20 17:30", "2025-12-31 08:00", "2026-01-05 00:00"],
        )
        # Not the order those same three cells took when they were text.
        self.assertNotEqual(by_cell_value, sorted(["12/31/2025 08:00", "2025-02-20 17:30:00", "2026-01-05T00:00:00+00:00"]))

    def test_the_cell_carries_a_date_format_so_it_does_not_read_as_a_number(self):
        """Without the style the column is honestly sortable and unreadable: 46027."""

        self.assertEqual(self.export().styles("completedDate_Final"), [str(EXCEL_STYLE_DATETIME)] * 3)
        styles = Sheet(self.workspace / "wo.xlsx").xml["xl/styles.xml"]
        self.assertIn('numFmtId="164"', styles)
        self.assertIn("yyyy", styles)

    def test_a_value_that_is_not_a_date_stays_the_text_it_is(self):
        """Dropping it would be tidier and would lose what the record says."""

        self.add_wo("4", completedDate_Final="unknown - see notes")
        cells = self.export().values("completedDate_Final")
        self.assertIn("unknown - see notes", cells)

    def test_a_record_with_no_date_leaves_an_empty_cell(self):
        self.add_wo("5")
        self.assertIn(None, self.export().values("completedDate_Final"))


class NumberColumnTests(DispositionExcelTestCase):
    """Task ids and downtime reach the sheet as numbers."""

    def setUp(self):
        super().setUp()
        # 2, 10 and 100 hours on task ids that sort 10, 100, 9 as text.
        self.add_wo("9", downtime=100 * 3600)
        self.add_wo("10", downtime=2 * 3600)
        self.add_wo("100", downtime=10 * 3600)

    def test_a_task_id_the_cmms_stored_as_text_is_written_as_a_number(self):
        sheet = self.export()
        self.assertEqual(sorted(sheet.values("taskID")), [9.0, 10.0, 100.0])
        # A number, not a numeral: no cell on that column is an inline string.
        self.assertEqual([cell.attrib.get("t") for cell in sheet.column("taskID")], [None] * 3)

    def test_downtime_is_written_as_a_number(self):
        self.assertEqual(sorted(self.export().values("downtime")), [2.0, 10.0, 100.0])

    def test_a_task_id_that_is_not_a_number_stays_the_text_it_is(self):
        self.add_wo("A-14")
        self.assertIn("A-14", self.export().values("taskID"))

    def test_an_id_too_long_for_a_spreadsheet_keeps_every_digit(self):
        """Rounding a quantity is a rounding; rounding an id is another record.

        A spreadsheet holds every number as a double, so 9007199254740993 would be
        written back as ...992 -- a number that names a different work order than
        the one the row was built from. Past that limit the exact digits only
        survive as text, so that is what the cell gets.
        """

        self.add_wo("9007199254740993")
        self.assertIn("9007199254740993", self.export("huge.xlsx").values("taskID"))

    def test_the_largest_id_a_cell_can_hold_exactly_is_still_a_number(self):
        """The limit is where a double stops being exact, not a round number of digits."""

        self.assertEqual(self.service._excel_number_value("9007199254740992"), 9007199254740992)
        self.assertIsNone(self.service._excel_number_value("9007199254740993"))

    @staticmethod
    def as_excel_would_sort(cells: list, descending: bool) -> list[str]:
        """``cells`` in the order a spreadsheet puts them, as text.

        Excel's rule for a mixed column: the numbers in one block, the text in
        another, and the two swap ends with the direction -- text after the numbers
        ascending, ahead of them descending. Blanks stay last either way.
        """

        numbers = sorted((v for v in cells if isinstance(v, (int, float))), reverse=descending)
        text = sorted((v for v in cells if isinstance(v, str)), reverse=descending)
        ordered = [*text, *numbers] if descending else [*numbers, *text]
        return [v if isinstance(v, str) else str(int(v)) for v in ordered]

    def test_the_screen_and_the_workbook_agree_on_what_is_a_number(self):
        """The disagreement this shared rule exists to prevent.

        Ordering used to read a value that is not a number as 0.0, so "A-14" came
        first on the screen's ascending page while the workbook put it after every
        number, the way a spreadsheet does. Both halves read _parse_number now.
        """

        self.add_wo("A-14")
        cells = self.export("mixed.xlsx").values("taskID")
        for descending in (False, True):
            with self.subTest(descending=descending):
                on_screen = [
                    str(row["taskID"])
                    for row in self.service.disposition_rows(
                        self.ASSET, "wo", sort="taskID", sort_dir="desc" if descending else "asc"
                    )
                ]
                self.assertEqual(self.as_excel_would_sort(cells, descending), on_screen)

    def test_they_agree_descending_too_where_the_text_block_leads(self):
        """The direction the first fix did not cover.

        Pinning the text with the blanks made it last whichever way the column
        pointed, so a descending sort read one way on the screen and the other way
        in the file built from the same rows.
        """

        for task_id in ("A-14", "B-2"):
            self.add_wo(task_id)
        cells = self.export("desc.xlsx").values("taskID")
        on_screen = [
            str(row["taskID"])
            for row in self.service.disposition_rows(self.ASSET, "wo", sort="taskID", sort_dir="desc")
        ]
        self.assertEqual(on_screen[:2], ["B-2", "A-14"])
        self.assertEqual(self.as_excel_would_sort(cells, descending=True), on_screen)

    def test_a_zero_padded_id_keeps_every_digit_in_the_cell(self):
        """"001234" written as 1234 is a different record from the one in the CMMS.

        Where task ids are padded to a fixed width, "001234" and "1234" are two
        records; the number under them is the same, so the padded one is not
        written as a number at all.
        """

        self.add_wo("0009")
        self.assertIn("0009", self.export("padded.xlsx").values("taskID"))

    def test_the_id_columns_are_numbers_too(self):
        """mapped_record_id is how a row finds its record, and it is matched as an int."""

        self.assertEqual(sorted(self.export().values("mapped_record_id")), [1.0, 2.0, 3.0])


class InclusionFlagTests(DispositionExcelTestCase):
    """The Weibull flag is written as the words its own dropdown offers.

    A boolean cell and the dropdown's "TRUE" are different values to Excel: the
    column would sort in two blocks, and every row nobody had touched would be
    flagged against its own validation list.
    """

    def setUp(self):
        super().setUp()
        self.add_wo("1")

    def test_the_flag_matches_the_dropdown_it_is_validated_against(self):
        sheet = self.export()
        self.assertEqual(sheet.values("include_in_weibull_candidate"), ["FALSE"])
        lookup = Sheet(self.workspace / "wo.xlsx", "xl/worksheets/sheet2.xml")
        offered = {Sheet._text(cell) for row in lookup.rows for cell in row}
        self.assertIn("FALSE", offered)
        self.assertIn("TRUE", offered)

    def test_a_dispositioned_row_reads_true(self):
        row = self.service.disposition_rows(self.ASSET, "wo")[0]
        self.service.save_dispositions([{
            "mapped_record_id": row["mapped_record_id"],
            "kind": "wo",
            "disposition_category": "INCLUDED_FAILURE",
            "record_class_final": "CORRECTIVE_WO",
            "failure_mode_text": "Bearing failure",
            "failure_mechanism_text": "Fatigue",
            "include_in_weibull_candidate": True,
        }])
        self.assertEqual(self.export("after.xlsx").values("include_in_weibull_candidate"), ["TRUE"])


class WorkbookShapeTests(DispositionExcelTestCase):
    """The parts a typed workbook needs, and the header row that sorts it."""

    def setUp(self):
        super().setUp()
        self.add_wo("1", completedDate_Final="2026-01-05T00:00:00+00:00")
        self.add_wo("2")

    def test_the_styles_part_is_declared_everywhere_it_has_to_be(self):
        """A part Excel cannot find is a workbook Excel offers to repair."""

        sheet = self.export()
        self.assertIn("xl/styles.xml", sheet.parts)
        self.assertIn("/xl/styles.xml", sheet.xml["[Content_Types].xml"])
        self.assertIn("styles.xml", sheet.xml["xl/_rels/workbook.xml.rels"])

    def test_every_part_of_the_workbook_is_well_formed_xml(self):
        sheet = self.export()
        for name, xml in sheet.xml.items():
            with self.subTest(part=name):
                ET.fromstring(xml)

    def test_the_header_row_carries_excels_own_sort_and_filter_menu(self):
        """Typed columns the reader still has to select a range to sort are half the job."""

        sheet = self.export()
        auto_filter = sheet.root.find("main:autoFilter", NS)
        self.assertIsNotNone(auto_filter)
        last_column = self.service._xlsx_column_name(len(sheet.headers))
        self.assertEqual(auto_filter.attrib["ref"], f"A1:{last_column}{len(sheet.rows)}")

    def test_the_dropdowns_survive_the_typed_cells(self):
        sheet = self.export()
        validations = sheet.root.findall("main:dataValidations/main:dataValidation", NS)
        self.assertTrue(validations)
        self.assertIn("Lookup Lists", "".join(sheet.xml["xl/worksheets/sheet1.xml"]))

    def test_a_control_character_in_a_note_does_not_corrupt_the_workbook(self):
        """One \\x07 pasted into a completion note used to be a file Excel refuses to open."""

        self.add_wo("3", completionNotes="stopped\x07 the line \x0b& restarted <it>")
        sheet = self.export("control.xlsx")
        notes = [value for value in sheet.values("completionNotes") if value]
        self.assertEqual(notes, ["stopped the line & restarted <it>"])


class DownloadScopeTests(DispositionExcelTestCase):
    """"Only new / undispositioned" narrows the workbook, not just the screen."""

    def setUp(self):
        super().setUp()
        for task_id in ("1", "2", "3"):
            self.add_wo(task_id, completedDate_Final=f"2026-01-0{task_id}T00:00:00+00:00")
        self.dispositioned = self.service.disposition_rows(self.ASSET, "wo")[0]["mapped_record_id"]
        self.service.save_dispositions([{
            "mapped_record_id": self.dispositioned,
            "kind": "wo",
            "disposition_category": "INCLUDED_FAILURE",
            "record_class_final": "CORRECTIVE_WO",
            "failure_mode_text": "Bearing failure",
            "failure_mechanism_text": "Fatigue",
            "include_in_weibull_candidate": True,
        }])

    def test_the_default_download_is_still_every_eligible_row(self):
        self.assertEqual(len(self.export("all.xlsx").rows) - 1, 3)

    def test_the_new_scope_leaves_out_what_has_already_been_dispositioned(self):
        sheet = self.export("new.xlsx", only_needing_disposition=True)
        exported = sheet.values("mapped_record_id")
        self.assertEqual(len(exported), 2)
        self.assertNotIn(float(self.dispositioned), exported)

    def test_the_workbook_holds_exactly_the_rows_the_table_shows(self):
        """One WHERE clause, so the screen and the file cannot disagree about "new"."""

        for only_new in (False, True):
            with self.subTest(only_new=only_new):
                on_screen = {
                    row["mapped_record_id"]
                    for row in self.service.disposition_rows(
                        self.ASSET, "wo", only_needing_disposition=only_new
                    )
                }
                sheet = self.export(f"scope-{only_new}.xlsx", only_needing_disposition=only_new)
                self.assertEqual({int(value) for value in sheet.values("mapped_record_id")}, on_screen)

    def test_the_count_it_reports_is_the_count_it_wrote(self):
        path = self.workspace / "counted.xlsx"
        written = self.service.export_disposition_excel(
            self.ASSET, "wo", path, only_needing_disposition=True
        )
        self.assertEqual(written, len(Sheet(path).rows) - 1)

    def test_a_narrowed_workbook_still_imports(self):
        """The import checks ids against every eligible row, not against the file."""

        path = self.workspace / "backlog.xlsx"
        self.service.export_disposition_excel(self.ASSET, "wo", path, only_needing_disposition=True)
        # Unchanged rows are no change at all, whichever scope produced them.
        self.assertEqual(self.service.import_disposition_excel(self.ASSET, "wo", path), 0)


class RelationshipTargetTests(unittest.TestCase):
    """An uploaded workbook may have been saved by something other than Excel.

    Excel writes a sheet's relationship Target relative ("worksheets/sheet1.xml");
    LibreOffice and Google Sheets write it package-absolute
    ("/xl/worksheets/sheet1.xml"). Reading the second as relative looked for
    "xl/xl/worksheets/sheet1.xml", and the upload died on a raw KeyError.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)

    def test_both_spellings_name_the_same_part(self):
        for target in ("worksheets/sheet1.xml", "/xl/worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"):
            with self.subTest(target=target):
                self.assertEqual(self.service._xlsx_part_path(target), "xl/worksheets/sheet1.xml")


class DownloadRouteTests(unittest.TestCase):
    """The Rows selector reaches the download the same way it reaches the table."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        import os

        self._previous = os.environ.get("GREMLIN_DB_PATH")
        os.environ["GREMLIN_DB_PATH"] = str(self.workspace / "gremlin.db")
        self.addCleanup(self._restore_env)
        import app

        self.module = importlib.reload(app)
        self.client = self.module.app.test_client()

    def _restore_env(self):
        import os

        if self._previous is None:
            os.environ.pop("GREMLIN_DB_PATH", None)
        else:
            os.environ["GREMLIN_DB_PATH"] = self._previous
        import app

        importlib.reload(app)

    def _asked_for(self, query):
        """The scope the route passed to the service for ``query``."""

        seen = {}

        def fake_export(asset_number, kind, path, *, only_needing_disposition=False):
            seen["only_needing_disposition"] = only_needing_disposition
            self.module.LifeDataService(
                self.workspace / "gremlin.db", refresh_on_startup=False
            )._write_xlsx(path, [["mapped_record_id"]], "WO Dispositions")
            return 0

        service = self.module._service_or_api_error()
        original = service.export_disposition_excel
        service.export_disposition_excel = fake_export
        self.addCleanup(setattr, service, "export_disposition_excel", original)
        response = self.client.get(f"/life-data-analysis/api/dispositions/excel?asset=2288&kind=wo{query}")
        self.assertEqual(response.status_code, 200)
        return seen["only_needing_disposition"], response.headers["Content-Disposition"]

    def test_the_new_scope_is_passed_through(self):
        only_new, disposition = self._asked_for("&scope=new")
        self.assertTrue(only_new)
        # Named apart, so the backlog file is not mistaken for the full record
        # set on somebody's desktop months later.
        self.assertIn("2288_wo_new_dispositions.xlsx", disposition)

    def test_no_scope_still_means_every_eligible_row(self):
        for query in ("", "&scope=all", "&scope=nonsense"):
            with self.subTest(query=query):
                only_new, disposition = self._asked_for(query)
                self.assertFalse(only_new)
                self.assertIn("2288_wo_dispositions.xlsx", disposition)


class ScreenAndFileAgreeTests(unittest.TestCase):
    """The browser half of the same promise.

    The route reads a scope; the button has to send the one the table is showing,
    or the setting is honoured by a URL nobody produces. Read off the files
    because the two halves live apart -- the button is built by the client script,
    the explainer is markup in the template.
    """

    ROOT = Path(__file__).resolve().parent.parent
    SCRIPT = (ROOT / "static" / "js" / "life_data_analysis.js").read_text()
    TEMPLATE = (ROOT / "templates" / "disposition.html").read_text()

    def test_the_download_button_sends_the_scope_the_table_is_showing(self):
        self.assertIn("downloadExcel(data.kind, data.scope)", self.SCRIPT)
        body = re.search(r"function downloadExcel\(.*?\n  \}", self.SCRIPT, re.S)
        self.assertTrue(body, "downloadExcel is no longer shaped the way this test reads it")
        self.assertIn("scope=", body.group(0))
        # Narrowed to the two the route knows, rather than pasted through: the
        # value reaches a SQL-shaped decision on the other side.
        self.assertIn('scope === "new" ? "new" : "all"', body.group(0))

    def test_the_search_box_and_the_page_still_do_not_travel(self):
        """Those narrow the view to look at something; they do not say what is outstanding."""

        body = re.search(r"function downloadExcel\(.*?\n  \}", self.SCRIPT, re.S)
        for parameter in ("search=", "page="):
            self.assertNotIn(parameter, body.group(0))

    def test_the_explainer_no_longer_says_the_rows_selector_is_ignored(self):
        collapsed = " ".join(self.TEMPLATE.split())
        self.assertNotIn("the Rows selector, the search box, and the page you are on do not narrow it", collapsed)
        self.assertIn("Only new / undispositioned", collapsed)

    def test_the_explainer_says_the_columns_arrive_typed(self):
        """A reader who expects text will not think to sort the date column at all."""

        collapsed = " ".join(self.TEMPLATE.split())
        self.assertIn("real dates and numbers", collapsed)
