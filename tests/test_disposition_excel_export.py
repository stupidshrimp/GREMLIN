"""The disposition workbook: real Excel types, and the reader's columns marked.

Every cell used to be written as text, so Excel sorted it as text ("10" ahead of
"9", dates by the digits they start with), and a text date cannot be filtered by
month or reformatted either: to Excel the value is a sentence, not a day. The
Disposition page's workbook (StandardDispositionService.export_excel) writes ids
and downtime as numbers and dates as dates, keeps what will not convert as the
text it is, and highlights the columns the reader fills in.

The workbook is written by hand (no openpyxl on the deployment), so these read
the parts back out of the zip the same way.
"""

import json
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
    EXCEL_DATE_EPOCH,
    EXCEL_STYLE_DATETIME,
    LifeDataService,
)
from services.standard_disposition_service import (
    EXCEL_COLUMNS,
    EXCEL_DISPOSITION_COLUMNS,
    EXCEL_RECORD_COLUMNS,
    EXCEL_VERSION_COLUMN,
    StandardDispositionService,
)

# The columns the reader fills in.
EDITABLE = frozenset(EXCEL_DISPOSITION_COLUMNS)

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
# The Disposition workbook's headers, by the CMMS field each is read from.
HEADERS = {
    "taskID": "task_id",
    "name": "task_name",
    "createdDate_Final": "created_date",
    "completedDate_Final": "completed_date",
    "downtime": "downtime_hours",
    "completionNotes": "completion_notes",
    "requestTitle": "request_title",
    "requestorDescription": "requestor_description",
    "disposition_notes": "disposition_note",
}
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
        index = self.headers.index(HEADERS.get(header, header))
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

    def export(self, name="wo.xlsx", **kwargs) -> Sheet:
        path = self.workspace / name
        StandardDispositionService(self.service).export_excel(self.ASSET, path, **kwargs)
        return Sheet(path)


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

    def test_the_serials_match_excels_own_across_its_phantom_day(self):
        """Excel has a 1900-02-29 that never happened, and counts from it.

        From 1900-03-01 the phantom day is already in the count, so 1899-12-30 is
        the epoch that lands on Excel's numbers. Before it, that epoch puts every
        date a day late -- 1900-01-01 as 2 where Excel says 1, and 1900-02-28 as
        60, which is the phantom day itself, so the cell would read a date the
        record does not have.

        The phantom day needs no handling of its own: _parse_datetime refuses it
        with every other day that does not exist, so nothing reaches serial 60.
        """

        for text, serial in (("1900-01-01", 1), ("1900-01-31", 31), ("1900-02-28", 59),
                             ("1900-03-01", 61), ("1900-03-02", 62), ("2026-01-05", 46027)):
            with self.subTest(date=text):
                self.assertEqual(self.service._excel_serial_datetime(text), serial)
        self.assertIsNone(self.service._parse_datetime("1900-02-29"))

    def test_a_date_earlier_than_excel_can_count_stays_text(self):
        """The count would go negative, and a date cell cannot show that.

        So it takes the same answer as every other value that will not convert,
        rather than a cell of hashes or a date read off a negative number.
        """

        self.assertIsNone(self.service._excel_serial_datetime("1899-06-15"))
        self.add_wo("4", completedDate_Final="1899-06-15")
        self.assertIn("1899-06-15", self.export("pre-1900.xlsx").values("completedDate_Final"))

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

        A spreadsheet keeps fifteen significant digits, so 1234567890123456 would
        be shown, and saved back, as 1234567890123460 -- a number that names a
        different work order than the one the row was built from. Past that width
        the exact digits only survive as text, so that is what the cell gets.
        """

        self.add_wo("1234567890123456")
        self.assertIn("1234567890123456", self.export("huge.xlsx").values("taskID"))

    def test_the_limit_is_excels_decimal_precision_not_the_double_underneath(self):
        """Sixteen digits is too many even below 2**53.

        A double holds 1234567890123456 exactly and 2**53 is itself sixteen
        digits, so a binary limit lets that id through into a numeric cell -- where
        Excel keeps fifteen significant digits and hands back ...460. The limit has
        to be the one the reader actually sees.
        """

        self.assertEqual(self.service._excel_number_value("999999999999999"), 999999999999999)
        for too_wide in ("1000000000000000", "1234567890123456", "9007199254740992"):
            with self.subTest(task_id=too_wide):
                self.assertLess(int(too_wide), 2**53 + 1)  # a double would hold it
                self.assertIsNone(self.service._excel_number_value(too_wide))

    def test_an_id_too_long_for_python_to_convert_still_reaches_the_cell(self):
        """Whole, not truncated: the export refuses to make a number of it, not to carry it."""

        self.add_wo("9" * 5000)
        self.assertIn("9" * 5000, self.export("unconvertible.xlsx").values("taskID"))

    def test_the_screen_still_orders_those_ids_as_numbers(self):
        """The cell's limit is not the ordering's limit.

        Keeping a sixteen-digit id as text is about what a spreadsheet can show.
        The ordering runs in Python and SQLite, both exact well past that, so it
        still compares the id as the number it is.
        """

        self.assertEqual(self.service._number_sort_key("1234567890123456"), 1234567890123456)

    def test_an_id_keeps_its_own_spelling_in_the_cell(self):
        """What an identifier is written as is part of which record it names.

        "001234", "+1234", "1e3", "1,042" and " 7 " all have a number underneath
        them, and writing that number puts a different string in the cell than the
        one the CMMS holds -- each collapsing onto a record that already exists
        under its plain spelling. Only text that is the number's own canonical
        form becomes a number; the rest keep their characters, whitespace
        included: the CMMS field is stored whole, so the padding around " 7 " is
        as much a part of it as the zeros in "0009".
        """

        for task_id in ("0009", "+7", "1e3", "1,042", " 7 "):
            with self.subTest(task_id=task_id):
                self.add_wo(task_id)
                self.assertIn(task_id, self.export(f"spelled-{task_id}.xlsx").values("taskID"))

    def test_the_plain_spelling_is_still_a_number(self):
        """The rule has to keep the ordinary case ordering as a number."""

        self.add_wo("1042")
        self.assertIn(1042.0, self.export("plain.xlsx").values("taskID"))

    def test_a_cell_of_spaces_is_a_value_rather_than_a_blank(self):
        """Blanking it would be one more quiet rewrite of what the record holds.

        The reader is checking these cells against the CMMS, so an empty cell has
        to mean the record is empty. It also keeps the table and the workbook in
        step: a spreadsheet can only compare what is actually in the cell, so a
        value the table calls blank and the workbook calls text sorts in two
        different places.
        """

        self.add_wo("   ")
        self.assertIn("   ", self.export("spaces.xlsx").values("taskID"))

    def test_whitespace_in_a_cell_is_marked_significant(self):
        """A bare <t> lets a reader fold the spaces away when it opens the file.

        Keeping " 7 " out of the number column is only half of it: without
        xml:space the file format itself hands back the record next to it, undoing
        the parse fix one layer further out. Checked on the cell and then through a
        real reader, including a resave, which is where a consumer would normalise
        it.
        """

        self.add_wo(" 7 ")
        sheet = self.export("preserve.xlsx")
        cell = next(c for c in sheet.column("taskID") if c.find(".//main:t", NS) is not None
                    and c.find(".//main:t", NS).text == " 7 ")
        marker = "{http://www.w3.org/XML/1998/namespace}space"
        self.assertEqual(cell.find(".//main:t", NS).attrib.get(marker), "preserve")

    def test_it_is_not_written_on_a_cell_that_does_not_need_it(self):
        """An ordinary sheet should not carry the marker on every cell."""

        self.add_wo("A-14")
        sheet = self.export("plain-text.xlsx")
        cell = next(c for c in sheet.column("taskID") if c.find(".//main:t", NS) is not None
                    and c.find(".//main:t", NS).text == "A-14")
        self.assertEqual(cell.find(".//main:t", NS).attrib, {})

    def test_a_quantity_column_is_untouched_by_that_rule(self):
        """It applies to text, and only task ids arrive as text.

        downtime_hours is REAL, so a downtime is already a float before
        _parse_number sees it and never reaches the canonical-form check -- which
        is what makes that check safe to apply to identifiers as strictly as it is.
        """

        self.add_wo("1042", downtime=int(2.5 * 3600))
        self.assertIn(2.5, self.export("quantity.xlsx").values("downtime"))

    def test_the_id_columns_are_numbers_too(self):
        """mapped_record_id is how a row finds its record, and it is matched as an int."""

        self.assertEqual(sorted(self.export().values("mapped_record_id")), [1.0, 2.0, 3.0])


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
        """One \\x07 pasted into a completion note used to be a file Excel refuses to open.

        The \\x0b leaves a space behind rather than vanishing: it is one of the six
        in the set that \\s counts as whitespace, and running the words either side
        of it together would lose the break the writer put there. The \\x07 is not a
        separator and simply goes.
        """

        self.add_wo("3", completionNotes="stopped\x07 the line \x0b& restarted <it>")
        sheet = self.export("control.xlsx")
        notes = [value for value in sheet.values("completionNotes") if value]
        self.assertEqual(notes, ["stopped the line  & restarted <it>"])


class EditableColumnHighlightTests(DispositionExcelTestCase):
    """The columns a reader fills in are highlighted, and the rest are marked read-only.

    An edit anywhere but the disposition columns is discarded on import without a
    word, so the sheet itself has to say which cells are the reader's. Yellow for
    those, from the header down; a grey header over everything else.
    """

    EDITABLE_HEADER = "FFFFD966"
    EDITABLE_CELL = "FFFFF2CC"
    READ_ONLY_HEADER = "FFD9D9D9"

    def setUp(self):
        super().setUp()
        self.add_wo("1", completedDate_Final="2026-01-05T00:00:00+00:00", downtime=int(2.5 * 3600))
        self.add_wo("2")

    @staticmethod
    def style_xf(sheet: Sheet, style: str | None) -> ET.Element:
        styles = ET.fromstring(sheet.xml["xl/styles.xml"])
        return styles.findall("main:cellXfs/main:xf", NS)[int(style or 0)]

    def fill(self, sheet: Sheet, style: str | None) -> str | None:
        """The colour a cell drawn in ``style`` is filled with, or None for no fill."""

        styles = ET.fromstring(sheet.xml["xl/styles.xml"])
        fill_id = int(self.style_xf(sheet, style).attrib.get("fillId", 0))
        pattern = styles.findall("main:fills/main:fill", NS)[fill_id].find("main:patternFill", NS)
        if pattern.attrib.get("patternType") != "solid":
            return None
        return pattern.find("main:fgColor", NS).attrib["rgb"]

    def header_fills(self, sheet: Sheet) -> dict[str, str | None]:
        return {sheet._text(cell): self.fill(sheet, cell.attrib.get("s")) for cell in sheet.rows[0]}

    def test_the_editable_columns_are_the_disposition_columns(self):
        """Everything after the record columns but the hidden version, and only that."""

        self.assertEqual(
            set(EXCEL_COLUMNS) & EDITABLE, set(EXCEL_COLUMNS) - set(EXCEL_RECORD_COLUMNS) - {EXCEL_VERSION_COLUMN}
        )
        # The key the import matches rows by is read back too, but it is not
        # the reader's to change.
        self.assertNotIn("mapped_record_id", EDITABLE)

    def test_each_header_says_whether_its_column_is_editable(self):
        fills = self.header_fills(self.export())
        for header, fill in fills.items():
            expected = self.EDITABLE_HEADER if header in EDITABLE else self.READ_ONLY_HEADER
            self.assertEqual(fill, expected, header)

    def test_every_cell_of_an_editable_column_is_highlighted_even_an_empty_one(self):
        """A blank disposition cell is exactly the one the reader has to find."""

        sheet = self.export()
        for header in sheet.headers:
            with self.subTest(header=header):
                fills = {self.fill(sheet, style) for style in sheet.styles(header)}
                self.assertEqual(fills, {self.EDITABLE_CELL} if header in EDITABLE else {None})
        # Neither row has been dispositioned, so its failure mode is still empty.
        self.assertIn(None, sheet.values("failure_mode_code"))

    def test_the_read_only_columns_keep_their_number_formats(self):
        sheet = self.export()
        for header, number_format in (("completedDate_Final", "164"), ("downtime", "165")):
            with self.subTest(header=header):
                formats = {self.style_xf(sheet, style).attrib["numFmtId"] for style in sheet.styles(header)}
                self.assertIn(number_format, formats)

    def test_a_highlighted_cell_that_needs_a_number_format_keeps_it(self):
        """A cell has one style, so the fill cannot cost a date or a decimal its format."""

        sheet = self.export()
        for value, column_type, number_format in (
            ("2026-01-05T00:00:00+00:00", COLUMN_TYPE_DATETIME, "164"),
            (2.5, COLUMN_TYPE_NUMBER, "165"),
        ):
            with self.subTest(column_type=column_type):
                cell = ET.fromstring(
                    f'<row xmlns="{MAIN}">{self.service._xlsx_cell_xml("A2", value, column_type, editable=True)}</row>'
                )[0]
                self.assertEqual(self.fill(sheet, cell.attrib.get("s")), self.EDITABLE_CELL)
                self.assertEqual(self.style_xf(sheet, cell.attrib.get("s")).attrib["numFmtId"], number_format)

    def test_the_lookup_lists_keep_their_plain_header(self):
        """A sheet told nothing about editable columns is not marked read-only."""

        self.export()
        lookup = Sheet(self.workspace / "wo.xlsx", "xl/worksheets/sheet2.xml")
        self.assertEqual(set(self.header_fills(lookup).values()), {None})


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
