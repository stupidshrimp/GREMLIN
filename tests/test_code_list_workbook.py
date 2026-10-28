"""The code lists as a workbook: downloaded from Developer > Code lists, filled in, uploaded.

An administrator keeps the lists in Excel as well as in the Code lists dialog.
The download is every code, retired ones too, with the columns an upload reads
highlighted and empty highlighted rows to add codes in. The upload adds and
changes codes through the dialog's own checks, all or none, and never deletes.
"""

import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from services import disposition_standard as std
from services.life_data_service import EXCEL_STYLE_EDITABLE, EXCEL_STYLE_EDITABLE_HEADER, EXCEL_STYLE_READ_ONLY_HEADER
from services.standard_disposition_service import (
    CODE_EXCEL_BLANK_ROWS,
    CODE_EXCEL_COLUMNS,
    CODE_EXCEL_EDITABLE_COLUMNS,
)
from tests.test_standard_disposition_service import PFS, StandardDispositionFixture, coded

NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
SEEDED = sum(len(entries) for entries in std.SEED_CODES.values())


class CodeListWorkbookFixture(StandardDispositionFixture):
    def setUp(self) -> None:
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "code_lists.xlsx"

    def download(self) -> list[list]:
        """The downloaded sheet's rows, each padded to the header's width."""

        self.std.export_code_lists_excel(self.path)
        rows = [list(row) for row in self.life._read_xlsx(self.path)]
        width = len(rows[0])
        return [row + [None] * (width - len(row)) for row in rows]

    def upload(self, rows: list[list], **kwargs):
        self.life._write_xlsx(self.path, rows, "Code lists")
        return self.std.import_code_lists_excel(self.path, **kwargs)

    @staticmethod
    def row(rows: list[list], code_list: str, code: str) -> list:
        index = rows[0].index
        return next(r for r in rows[1:] if r[index("code_list")] == code_list and r[index("code")] == code)

    @staticmethod
    def fill(rows: list[list], **values) -> list:
        """Type a new code into the first empty row."""

        target = next(r for r in rows[1:] if not any(r))
        for column, value in values.items():
            target[rows[0].index(column)] = value
        return target

    def code(self, code_list: str, code: str) -> dict | None:
        return next((entry for entry in self.std.code_lists()[code_list]["codes"] if entry["code"] == code), None)


class DownloadTests(CodeListWorkbookFixture):
    def test_every_code_is_a_row_retired_ones_too_in_the_lists_order(self):
        self.std.save_code({"code_list": "COMPONENT", "code": "Belt", "name": "Belt", "is_active": False})
        rows = self.download()
        self.assertEqual(rows[0], list(CODE_EXCEL_COLUMNS))
        codes = [r for r in rows[1:] if any(r)]
        self.assertEqual(len(codes), SEEDED)
        lists = [r[0] for r in codes]
        # The part list starts empty, so it has no rows yet.
        self.assertEqual(list(dict.fromkeys(lists)), [code_list for code_list in std.CODE_LISTS if code_list != std.PART])
        belt = self.row(rows, "COMPONENT", "Belt")
        self.assertEqual(belt[rows[0].index("is_active")], "FALSE")
        wear = self.row(rows, "FAILURE_MECHANISM", "WEAR")
        self.assertEqual(wear[rows[0].index("default_cause_code")], "EXP-WEAR")

    def test_it_says_how_many_work_orders_use_each_code(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        self.add_record(2, PFS, "Two", "2024-01-02", "2024-01-02")
        self.std.save_dispositions([
            {"mapped_record_id": 1, **coded()},
            {"mapped_record_id": 2, **coded("PM_RESET", component="Belt", mode="DEG", renewed_items=["Belt", "Filter"])},
        ])
        rows = self.download()
        using = rows[0].index("work_orders_using")
        # Filter is one's component and renewed alongside the other's Belt.
        self.assertEqual(self.row(rows, "COMPONENT", "Filter")[using], 2)
        self.assertEqual(self.row(rows, "COMPONENT", "Belt")[using], 1)
        self.assertEqual(self.row(rows, "COMPONENT", "Motor")[using], 0)

    def test_the_columns_an_upload_reads_are_highlighted_and_so_are_rows_to_add_codes_in(self):
        rows = self.download()
        self.assertEqual(sum(1 for r in rows[1:] if not any(r)), CODE_EXCEL_BLANK_ROWS)
        with zipfile.ZipFile(self.path) as workbook:
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        xml_rows = sheet.findall(".//main:sheetData/main:row", NS)
        header_styles = [cell.get("s") for cell in xml_rows[0]]
        for column, style in zip(CODE_EXCEL_COLUMNS, header_styles):
            expected = EXCEL_STYLE_EDITABLE_HEADER if column in CODE_EXCEL_EDITABLE_COLUMNS else EXCEL_STYLE_READ_ONLY_HEADER
            self.assertEqual(style, str(expected), column)
        last = list(xml_rows[-1])
        self.assertEqual(last[0].get("s"), str(EXCEL_STYLE_EDITABLE))

    def test_the_list_meaning_yes_no_and_cause_columns_have_dropdowns(self):
        self.download()
        with zipfile.ZipFile(self.path) as workbook:
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            lookup = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        letters = {column: chr(ord("A") + index) for index, column in enumerate(CODE_EXCEL_COLUMNS)}
        validations = {
            node.get("sqref").split(":")[0][:-1]: node for node in sheet.findall(".//main:dataValidation", NS)
        }
        for column in ("code_list", "semantic", "default_cause_code", "renews_item", "is_active"):
            self.assertEqual(validations[letters[column]].get("type"), "list", column)
        # A mechanism may name a cause added further down the same sheet, so that
        # dropdown suggests rather than insists.
        self.assertEqual(validations[letters["default_cause_code"]].get("showErrorMessage"), "0")
        self.assertEqual(validations[letters["code_list"]].get("showErrorMessage"), "1")
        self.assertIn("EXP-WEAR", lookup)
        self.assertIn("NO_FAULT_FOUND", lookup)
        # One semantic dropdown serves every row, so each meaning says which lists take it.
        self.assertIn("Found on inspection, still running (detection: inspection) — failure modes only", lookup)
        self.assertIn("not known&apos; — failure mechanisms and failure causes only",
                      lookup.replace("'", "&apos;"))


class UploadTests(CodeListWorkbookFixture):
    def test_an_untouched_download_changes_nothing(self):
        outcome = self.upload(self.download(), updated_by="ana")
        self.assertEqual((outcome["added"], outcome["updated"]), ([], []))
        self.assertEqual(outcome["unchanged"], SEEDED)
        self.assertEqual(outcome["not_in_workbook"], 0)
        with self.life.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM disposition_code WHERE updated_by = 'ana'").fetchone()[0], 0)

    def test_new_codes_are_added_and_changed_ones_saved(self):
        rows = self.download()
        header = rows[0].index
        self.row(rows, "COMPONENT", "Filter")[header("name")] = "Filter element"
        self.row(rows, "COMPONENT", "Hose")[header("is_active")] = "FALSE"
        self.fill(rows, code_list="COMPONENT", code="Boiler", name="Boiler / water heater")
        # A list's label is as good as its key, and a new cause can be a new mechanism's default.
        self.fill(rows, code_list="Failure cause", code="EXT", name="External")
        self.fill(rows, code_list="failure_mechanism", code="CORR", name="Corrosion", iso_ref="2.2",
                  default_cause_code="EXT", sort_order=75)
        self.fill(rows, code_list="ACTION", code="MODIFY", name="Modify / install", renews_item=True)
        outcome = self.upload(rows, updated_by="ana")

        self.assertEqual(
            [(item["code_list"], item["code"]) for item in outcome["added"]],
            [("COMPONENT", "Boiler"), ("FAILURE_CAUSE", "EXT"), ("FAILURE_MECHANISM", "CORR"), ("ACTION", "MODIFY")],
        )
        self.assertEqual([item["code"] for item in outcome["updated"]], ["Filter", "Hose"])
        self.assertEqual(outcome["unchanged"], SEEDED - 2)
        self.assertEqual(self.code("COMPONENT", "Filter")["name"], "Filter element")
        self.assertFalse(self.code("COMPONENT", "Hose")["is_active"])
        corr = self.code("FAILURE_MECHANISM", "CORR")
        self.assertEqual((corr["default_cause_code"], corr["iso_ref"], corr["sort_order"]), ("EXT", "2.2", 75))
        self.assertTrue(self.code("ACTION", "MODIFY")["renews_item"])
        # A new code given no sort order goes to the end of its list, active;
        # components have no order and list by name.
        actions = self.std.code_lists()["ACTION"]["codes"]
        self.assertEqual(actions[-1]["code"], "MODIFY")
        components = self.std.code_lists()["COMPONENT"]["codes"]
        names = [component["name"].lower() for component in components]
        self.assertEqual(names, sorted(names))
        self.assertTrue(self.code("COMPONENT", "Boiler")["is_active"])
        self.assertEqual(self.std.codebook().default_cause("CORR"), "EXT")
        with self.life.connect() as conn:
            stamped = {row[0] for row in conn.execute("SELECT code FROM disposition_code WHERE updated_by = 'ana'")}
        self.assertEqual(stamped, {"Filter", "Hose", "Boiler", "EXT", "CORR", "MODIFY"})

    def test_a_trimmed_workbook_leaves_the_codes_it_omits_alone(self):
        rows = self.download()
        kept = [rows[0], self.row(rows, "FAILURE_MODE", "DEG")]
        kept[1][rows[0].index("name")] = "Degraded"
        outcome = self.upload(kept)
        self.assertEqual([item["code"] for item in outcome["updated"]], ["DEG"])
        self.assertEqual(outcome["not_in_workbook"], SEEDED - 1)
        self.assertEqual(sum(len(lst["codes"]) for lst in self.std.code_lists().values()), SEEDED)

    def test_an_empty_cell_keeps_a_codes_yes_no_and_order_but_clears_its_text(self):
        rows = self.download()
        header = rows[0].index
        replace = self.row(rows, "ACTION", "REPLACE")
        self.assertEqual(replace[header("renews_item")], "TRUE")
        order = self.code("ACTION", "REPLACE")["sort_order"]
        replace[header("renews_item")] = None
        replace[header("sort_order")] = None
        filt = self.row(rows, "COMPONENT", "Filter")
        before = self.code("COMPONENT", "Filter")
        self.std.save_code({**before, "code_list": "COMPONENT", "description": "Any filter"})
        filt[header("description")] = None
        self.upload(rows)
        self.assertTrue(self.code("ACTION", "REPLACE")["renews_item"])
        self.assertEqual(self.code("ACTION", "REPLACE")["sort_order"], order)
        self.assertIsNone(self.code("COMPONENT", "Filter")["description"])

    def test_one_bad_row_refuses_the_whole_upload(self):
        rows = self.download()
        self.fill(rows, code_list="COMPONENT", code="Boiler", name="Boiler")
        self.fill(rows, code_list="COMPONENT", code="Light")
        self.row(rows, "COMPONENT", "Filter")[rows[0].index("name")] = "Filter element"
        with self.assertRaisesRegex(ValueError, r"^Row \d+ \(Component Light\): Give the component a name\. Nothing was saved\.$"):
            self.upload(rows)
        self.assertIsNone(self.code("COMPONENT", "Boiler"))
        self.assertEqual(self.code("COMPONENT", "Filter")["name"], "Filter / strainer / sieve")

    def test_every_refused_row_is_named_at_once(self):
        rows = self.download()
        self.fill(rows, code_list="COMPONENT", code="Boiler", name="Boiler")
        refused_rows = [
            self.fill(rows, code_list="FAILURE_MECHANISM", code="OTH", name="Other", semantic="REVIEW"),
            self.fill(rows, code_list="FAILURE_MECHANISM", code="CORR", name="Corrosion", default_cause_code="RUST"),
            self.fill(rows, code_list="FAILURE_MODE", code="UNK", name="Unknown", semantic="UNKNOWN"),
        ]
        sheet_row = {id(row): number for number, row in enumerate(rows, start=1)}
        with self.assertRaises(ValueError) as refused:
            self.upload(rows)
        lines = str(refused.exception).split("\n")
        self.assertEqual(lines[0], "3 rows were refused, so nothing was saved:")
        # In the sheet's order, the default cause (checked last) included.
        self.assertEqual([line.split(":")[0] for line in lines[1:]], [
            f"Row {sheet_row[id(refused_rows[0])]} (Failure mechanism OTH)",
            f"Row {sheet_row[id(refused_rows[1])]} (Failure mechanism CORR)",
            f"Row {sheet_row[id(refused_rows[2])]} (Failure mode UNK)",
        ])
        self.assertIsNone(self.code("COMPONENT", "Boiler"))

    def test_a_cause_refused_for_its_capitals_is_no_default_cause_either(self):
        # It can never be saved spelled so, so the mechanism naming it so is refused
        # in the same upload, not only once the cause row is fixed.
        rows = [list(CODE_EXCEL_COLUMNS), [None] * len(CODE_EXCEL_COLUMNS), [None] * len(CODE_EXCEL_COLUMNS)]
        self.fill(rows, code_list="FAILURE_CAUSE", code="exp-wear", name="Wear")
        self.fill(rows, code_list="FAILURE_MECHANISM", code="CORR", name="Corrosion", default_cause_code="exp-wear")
        with self.assertRaises(ValueError) as refused:
            self.upload(rows)
        lines = str(refused.exception).split("\n")
        self.assertEqual(lines[0], "2 rows were refused, so nothing was saved:")
        self.assertIn("(Failure cause exp-wear): the failure cause list already has EXP-WEAR", lines[1])
        self.assertIn("(Failure mechanism CORR): default cause 'exp-wear' is not in the failure cause list", lines[2])

    def test_a_long_list_of_refusals_is_cut_short(self):
        rows = [list(CODE_EXCEL_COLUMNS)]
        for number in range(30):
            rows.append([None] * len(CODE_EXCEL_COLUMNS))
            self.fill(rows, code_list="ACTION", code=f"X{number}", name="X", semantic="UNKNOWN")
        with self.assertRaises(ValueError) as refused:
            self.upload(rows)
        lines = str(refused.exception).split("\n")
        self.assertEqual(lines[0], "30 rows were refused, so nothing was saved:")
        self.assertEqual(len(lines), 1 + 25 + 1)
        self.assertEqual(lines[-1], "…and 5 more.")

    def test_what_each_row_is_refused_for(self):
        cases = [
            ({"code_list": "PARTS", "code": "X", "name": "X"}, r"'PARTS' is not a code list"),
            ({"code": "X", "name": "X"}, r"give the code's list and the code itself"),
            ({"code_list": "COMPONENT", "code": "bad code", "name": "X"}, r"A code is 1-32 letters"),
            ({"code_list": "COMPONENT", "code": "filter", "name": "X"},
             r"already has Filter, which differs only in capitals"),
            ({"code_list": "FAILURE_MECHANISM", "code": "CORR", "name": "Corrosion", "default_cause_code": "RUST"},
             r"default cause 'RUST' is not in the failure cause list"),
            ({"code_list": "ACTION", "code": "X", "name": "X", "semantic": "UNKNOWN"},
             r"Semantic UNKNOWN is for failure mechanisms and failure causes only\. "
             r"An action can mean nothing to the engine: leave semantic blank\."),
            ({"code_list": "FAILURE_MECHANISM", "code": "OTH", "name": "Other", "semantic": "REVIEW"},
             r"Semantic REVIEW is not one GREMLIN knows\. A failure mechanism can mean UNKNOWN, or leave semantic blank\."),
            ({"code_list": "FAILURE_MODE", "code": "UNK", "name": "Unknown", "semantic": "NOPE"},
             r"Semantic NOPE is not one GREMLIN knows\. A failure mode can mean INSPECTION_FINDING or NO_FAULT_FOUND"),
            ({"code_list": "ACTION", "code": "X", "name": "X", "is_active": "maybe"}, r"is_active 'maybe' is not TRUE or FALSE"),
            ({"code_list": "ACTION", "code": "X", "name": "X", "sort_order": "first"}, r"Sort order must be a whole number"),
        ]
        for values, message in cases:
            with self.subTest(values=values):
                # Just the one row, so the code it names is not also in the sheet.
                rows = [list(CODE_EXCEL_COLUMNS), [None] * len(CODE_EXCEL_COLUMNS)]
                self.fill(rows, **values)
                with self.assertRaisesRegex(ValueError, message):
                    self.upload(rows)

    def test_a_code_twice_in_the_workbook_is_refused(self):
        rows = self.download()
        self.fill(rows, code_list="COMPONENT", code="Boiler", name="Boiler")
        self.fill(rows, code_list="COMPONENT", code="BOILER", name="Boiler again")
        with self.assertRaisesRegex(ValueError, r"\(Component BOILER\): the code is in the workbook twice, also on row \d+"):
            self.upload(rows)

    def test_a_workbook_that_is_not_the_code_lists_is_refused(self):
        # The Disposition page's own workbook, uploaded here by mistake, is named as such.
        with self.assertRaisesRegex(ValueError, "This is a Disposition page workbook.*Upload Excel on the Disposition page"):
            self.upload([["mapped_record_id", "disposition_category"], [1, "FAILURE"]])
        with self.assertRaisesRegex(ValueError, "needs the name column"):
            self.upload([["code_list", "code"], ["COMPONENT", "X"]])
        with self.assertRaisesRegex(ValueError, "has no codes in it"):
            self.upload([list(CODE_EXCEL_COLUMNS)])


if __name__ == "__main__":
    unittest.main()
