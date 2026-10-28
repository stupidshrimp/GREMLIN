"""Components (maintainable items) and their parts.

A part is its own item: Compressor › Filter has its own lives, replacing the
filter renews nothing else, and replacing the whole compressor renews its parts
too. A population with no part picked reads each component as a whole; one with
parts picked reads those parts, in one component or pooled across several.
Components and parts are each just a name and a description.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

import openpyxl

from services import disposition_helper_text as helper_text
from services import disposition_standard as std
from services.disposition_standard import (
    CodeBook,
    CodeEntry,
    Population,
    RecordContext,
    StreamEvent,
    build_renewal_intervals,
    code_from_name,
    normalize_disposition,
    validity_warnings,
)
from tests.test_standard_disposition_service import PFS, S09, StandardDispositionFixture, coded

SEED = CodeBook.from_seed()
CODES = CodeBook([
    *(entry for entries in std.SEED_CODES.values() for entry in entries),
    CodeEntry(std.PART, "Filter", "Filter"),
    CodeEntry(std.PART, "ORing", "O-ring"),
    CodeEntry(std.PART, "OldSeal", "Old seal", is_active=False),
])
_ids = count(1)


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def event(component, day, *, part=None, category=std.FAILURE, action="REPLACE", repaired=None, renewed=(), location="L"):
    return StreamEvent(
        record_id=next(_ids),
        category=category,
        location=location,
        component=component,
        part=part,
        mechanism="WEAR",
        mode="FTS",
        cause="EXP-WEAR",
        action=action,
        renews_item=SEED.renews(action) if category == std.FAILURE else True,
        renewed_items=tuple(renewed),
        event_date=at(day),
        renewal_date=at(repaired or day),
    )


def lives(analysis):
    return [(interval.end_type, round(interval.raw_days)) for interval in analysis.intervals]


CUTOFF = at("2025-04-01")


class PartLivesTests(unittest.TestCase):
    def test_a_part_has_lives_of_its_own(self):
        events = [
            event("HVAC", "2025-01-01", part="Filter"),
            event("HVAC", "2025-01-20", action="RESET_ONLY"),  # the compressor reset, nothing renewed
            event("HVAC", "2025-02-01", part="Filter"),
            event("HVAC", "2025-03-01", part="Filter"),
        ]
        filters = build_renewal_intervals(events, Population(components=frozenset({"HVAC"}), parts=frozenset({"Filter"})), cutoff=CUTOFF)
        self.assertEqual(filters.streams, [("L", "HVAC", "Filter")])
        self.assertEqual(filters.failure_count, 2)
        # The compressor as a whole: only its own events, and none renewed it.
        whole = build_renewal_intervals(events, Population(components=frozenset({"HVAC"})), cutoff=CUTOFF)
        self.assertEqual(whole.streams, [("L", "HVAC", "")])
        self.assertEqual(whole.failure_count, 0)

    def test_replacing_the_whole_component_renews_its_parts(self):
        events = [
            event("HVAC", "2025-01-01", part="Filter"),
            event("HVAC", "2025-01-20", repaired="2025-01-21"),  # compressor replaced
            event("HVAC", "2025-03-01", part="Filter"),
        ]
        analysis = build_renewal_intervals(events, Population(parts=frozenset({"Filter"})), cutoff=CUTOFF)
        self.assertEqual(lives(analysis)[:2], [(std.SUSPENSION_END, 19), (std.FAILURE_END, 39)])
        self.assertEqual(analysis.intervals[0].note, "Suspension: renewed with the whole component it is part of.")

    def test_replacing_a_part_renews_nothing_else(self):
        events = [
            event("HVAC", "2025-01-01"),
            event("HVAC", "2025-02-01", part="Filter"),  # only the filter replaced
            event("HVAC", "2025-03-02"),
        ]
        analysis = build_renewal_intervals(events, Population(components=frozenset({"HVAC"})), cutoff=CUTOFF)
        self.assertEqual(lives(analysis)[0], (std.FAILURE_END, 60))

    def test_one_part_pooled_across_components_is_one_kind_of_item(self):
        events = [
            event("HVAC", "2025-01-01", part="Filter"),
            event("HVAC", "2025-02-01", part="Filter"),
            event("Fan", "2025-01-05", part="Filter"),
            event("Fan", "2025-02-10", part="Filter"),
        ]
        population = Population(parts=frozenset({"Filter"}))
        analysis = build_renewal_intervals(events, population, cutoff=CUTOFF)
        self.assertEqual(analysis.streams, [("L", "Fan", "Filter"), ("L", "HVAC", "Filter")])
        codes = {warning["code"] for warning in validity_warnings(analysis, population, codebook=CODES)}
        self.assertNotIn("MIXED_COMPONENTS", codes)
        # Two components as wholes are two kinds.
        mixed = Population(components=frozenset({"HVAC", "Fan"}))
        whole = build_renewal_intervals([*events, event("HVAC", "2025-01-03"), event("Fan", "2025-01-04")], mixed, cutoff=CUTOFF)
        self.assertIn("MIXED_COMPONENTS", {warning["code"] for warning in validity_warnings(whole, mixed, codebook=CODES)})

    def test_a_pm_naming_a_part_renews_that_part_once(self):
        events = [
            event("HVAC", "2025-01-01", part="Filter"),
            # Planned work that changed the compressor's filter, listing the compressor too.
            event("HVAC", "2025-02-01", part="Filter", category=std.PM_RESET, action="CLEAN_SERVICE", renewed=["HVAC"]),
            event("HVAC", "2025-03-01", part="Filter"),
        ]
        analysis = build_renewal_intervals(events, Population(parts=frozenset({"Filter"})), cutoff=CUTOFF)
        self.assertEqual(lives(analysis)[:2], [(std.SUSPENSION_END, 31), (std.FAILURE_END, 28)])
        self.assertFalse(any(note.role == std.ROLE_NO_EFFECT for note in analysis.notes))


class PartFieldTests(unittest.TestCase):
    RECORD = RecordContext(mapped_record_id=1, asset_number="4002", sub_asset_choices=())

    def normalize(self, **values):
        return normalize_disposition({**coded(sub_asset=None), **values}, record=self.RECORD, codebook=CODES)

    def test_a_part_is_kept_with_its_component(self):
        self.assertEqual(self.normalize(part_code="Filter")["part_code"], "Filter")
        self.assertIsNone(self.normalize()["part_code"])

    def test_a_part_needs_its_component(self):
        with self.assertRaisesRegex(ValueError, "A part needs its component"):
            self.normalize(component_code=None, part_code="Filter", disposition_category="HELD", disposition_note="Why")

    def test_a_part_must_be_on_the_list_and_not_retired(self):
        with self.assertRaisesRegex(ValueError, "Part 'Gear' is not in the part list"):
            self.normalize(part_code="Gear")
        with self.assertRaisesRegex(ValueError, "Part 'OldSeal' has been retired"):
            self.normalize(part_code="OldSeal")

    def test_a_pm_reset_may_name_the_part_it_renewed_instead_of_listing_items(self):
        result = self.normalize(disposition_category="PM_RESET", part_code="Filter")
        self.assertEqual((result["part_code"], result["renewed_items"]), ("Filter", ()))
        with self.assertRaisesRegex(ValueError, "PM_RESET needs the renewed items, or the part it renewed"):
            self.normalize(disposition_category="PM_RESET")


class CodeFromNameTests(unittest.TestCase):
    def test_a_name_runs_together_into_a_code(self):
        self.assertEqual(code_from_name("Air compressor"), "AirCompressor")
        self.assertEqual(code_from_name("Ball screw / screw drive"), "BallScrewScrewDrive")
        self.assertEqual(code_from_name("O-ring"), "ORing")
        self.assertEqual(code_from_name("???"), "Item")

    def test_a_code_taken_in_any_capitals_is_numbered_on(self):
        self.assertEqual(code_from_name("Air compressor", ["aircompressor"]), "AirCompressor2")
        self.assertEqual(code_from_name("Air compressor", ["AirCompressor", "AirCompressor2"]), "AirCompressor3")
        long = code_from_name("x" * 40, ["X" + "x" * 31])
        self.assertTrue(std.CODE_PATTERN.match(long) and len(long) <= 32 and long.endswith("2"))


class PartServiceTests(StandardDispositionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.add_record(701, PFS, "Compressor filter clogged", "2024-01-01", "2024-01-01")
        self.add_record(702, PFS, "Compressor filter clogged again", "2024-02-01", "2024-02-01")

    def test_a_component_or_part_is_added_by_name_and_listed_by_name(self):
        code = self.std.put_code({
            "code_list": "COMPONENT", "name": "Air compressor", "description": "Plant air",
            "iso_ref": "9.9", "semantic": "UNKNOWN", "sort_order": 5,
        })
        self.assertEqual(code, "AirCompressor")
        entry = self.std.codebook().entry(std.COMPONENT, code)
        self.assertEqual((entry.description, entry.iso_ref, entry.semantic, entry.sort_order), ("Plant air", None, None, 0))
        names = [item["name"].lower() for item in self.std.code_lists()["COMPONENT"]["codes"]]
        self.assertEqual(names, sorted(names))
        self.assertTrue(self.std.code_lists()["PART"]["named"])
        self.assertEqual(self.std.put_code({"code_list": "PART", "name": "Filter"}), "Filter")

    def test_the_same_name_twice_is_refused(self):
        self.std.put_code({"code_list": "PART", "name": "O-ring"})
        with self.assertRaisesRegex(ValueError, "The part list already has 'O-ring'; use that one"):
            self.std.put_code({"code_list": "PART", "name": "o-RING"})

    def test_a_disposition_keeps_its_part_and_the_analyses_read_it(self):
        self.std.put_code({"code_list": "PART", "name": "Filter element"})
        self.std.save_dispositions([
            {"mapped_record_id": 701, **coded(component="HVAC", part_code="FilterElement")},
            {"mapped_record_id": 702, **coded(component="HVAC", part_code="FilterElement")},
        ])
        record = self.std.disposition_record(701)
        self.assertEqual(record["disposition"]["part_code"], "FilterElement")
        self.assertEqual(record["derived"]["renewal_stream_key"], f"{S09} | HVAC | FilterElement")
        summary = self.analysis.summary({"assets": [PFS]}, weibull=True)
        bar = summary["pareto"]["rows"][0]
        self.assertEqual(bar["label"], "HVAC › FilterElement · CONT")
        self.assertEqual(bar["selection"]["parts"], ["FilterElement"])
        # The part Pareto, and a component's bar drilling to its parts.
        by_part = self.analysis.summary({"assets": [PFS]}, group_by="part")["pareto"]
        self.assertEqual([row["key"] for row in by_part["rows"]], ["FilterElement"])
        by_component = self.analysis.summary({"assets": [PFS]}, group_by="component")["pareto"]
        self.assertEqual(by_component["next_group_by"], "part")

    def test_a_component_with_no_parts_drills_straight_to_its_mechanisms(self):
        self.std.save_dispositions([{"mapped_record_id": 701, **coded(component="HVAC")}])
        pareto = self.analysis.summary({"assets": [PFS]}, group_by="component")["pareto"]
        self.assertEqual(pareto["next_group_by"], "failure_mechanism")

    def test_the_workbook_carries_the_part_and_reads_it_by_name(self):
        self.std.put_code({"code_list": "PART", "name": "Filter element"})
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "dispositions.xlsx"
        self.std.export_excel(PFS, path)
        workbook = openpyxl.load_workbook(path)
        sheet = workbook["Dispositions"]
        headings = {cell.value: cell.column for cell in sheet[1]}
        self.assertIn("part_code", headings)
        row = next(cell.row for cell in sheet["A"] if cell.value == 701)
        for heading, value in {**coded(component="HVAC"), "part_code": "filter element"}.items():
            sheet.cell(row, headings[heading]).value = value
        workbook.save(path)
        self.assertEqual(self.std.import_excel(PFS, path)["saved"], 1)
        self.assertEqual(self.std.disposition_record(701)["disposition"]["part_code"], "FilterElement")



class SimilarCodeTests(unittest.TestCase):
    def names(self, code_list, name, description=None, *, codebook=CODES, meaning=None):
        return [
            (match["name"], match["reasons"][0])
            for match in helper_text.similar_codes(codebook, code_list, name, description, meaning=meaning)
        ]

    def test_a_new_component_finds_the_ones_it_may_be(self):
        # A segment of an existing name, a thesaurus phrase, a typo, and plain shared words.
        self.assertEqual(self.names(std.COMPONENT, "Air compressor")[0], ("HVAC / compressor / chiller", "\u201ccompressor\u201d is one of its names"))
        self.assertEqual(self.names(std.COMPONENT, "Combustion air blower")[0][0], "Fan / blower")
        self.assertEqual(self.names(std.COMPONENT, "Fliter")[0], ("Filter / strainer / sieve", "Spelled almost the same"))
        self.assertEqual(self.names(std.COMPONENT, "Mtr")[0][0], "Motor")
        self.assertEqual(self.names(std.COMPONENT, "Glass panel"), [])

    def test_the_same_name_in_other_capitals_or_punctuation_is_the_same(self):
        self.assertEqual(self.names(std.PART, "o ring"), [("O-ring", "The same name")])

    def test_a_retired_code_is_offered_and_marked(self):
        match = helper_text.similar_codes(CODES, std.PART, "Old seals")[0]
        self.assertEqual((match["code"], match["is_active"]), ("OldSeal", False))

    def test_the_model_finds_the_same_thing_in_other_words(self):
        def meaning(asked, texts):
            # A stand-in model: "spark electrode" means much what "Burner / flame rod / igniter" does.
            return [0.8 if text.startswith("Burner") else 0.1 for text in texts]

        self.assertEqual(self.names(std.COMPONENT, "Spark electrode"), [])
        self.assertEqual(
            self.names(std.COMPONENT, "Spark electrode", meaning=meaning),
            [("Burner / flame rod / igniter", "Means much the same")],
        )


class SimilarCodeServiceTests(StandardDispositionFixture):
    def test_the_helper_checks_the_list_as_it_is_now(self):
        from services.disposition_helper import DispositionHelper

        self.std.put_code({"code_list": "PART", "name": "O-ring", "description": "Rubber seal ring"})
        found = DispositionHelper(self.std).similar_codes("part", "O rings")
        self.assertEqual(found["code_list"], "PART")
        self.assertEqual([match["code"] for match in found["matches"]], ["ORing"])
        self.assertIn("enabled", found["meaning"])
        with self.assertRaisesRegex(ValueError, "Choose one of the code lists"):
            DispositionHelper(self.std).similar_codes("GADGETS", "x")


if __name__ == "__main__":
    unittest.main()
