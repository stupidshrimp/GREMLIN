"""The work-order disposition standard's rules and its renewal-stream life engine.

Pure functions, so each rule is pinned without a database: the field rules of
spec section 2, the derived fields of section 4, the migration of section 2.2,
and above all section 5 -- every life measured on one location's one component,
and only then pooled.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from services import disposition_standard as std
from services.disposition_standard import (
    CodeBook,
    Population,
    RecordContext,
    StreamEvent,
    build_renewal_intervals,
    derive_fields,
    legacy_auto_conversion,
    normalize_disposition,
    population_definition,
    validity_warnings,
)

CODES = CodeBook.from_seed()


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


_ids = iter(range(1, 10_000))


def failure(location, component, day, *, mechanism="CONT", mode="FTS", action="CLEAN_SERVICE", renewed=(),
            repaired=None, cause="EXP-WEAR", qty=None, record_id=None):
    renews = CODES.renews(action)
    return StreamEvent(
        record_id=record_id or next(_ids),
        category=std.FAILURE,
        location=location,
        component=component,
        mechanism=mechanism,
        mode=mode,
        cause=cause,
        action=action,
        renews_item=renews,
        renewed_items=tuple(renewed),
        event_date=at(day),
        renewal_date=at(repaired or day),
        qty=qty,
        mechanism_unknown=CODES.has_semantic(std.FAILURE_MECHANISM, mechanism, std.SEMANTIC_UNKNOWN),
        cause_induced=CODES.has_semantic(std.FAILURE_CAUSE, cause, std.SEMANTIC_INDUCED),
    )


def pm_reset(location, day, renewed, *, record_id=None):
    return StreamEvent(
        record_id=record_id or next(_ids),
        category=std.PM_RESET,
        location=location,
        component=renewed[0],
        action="CLEAN_SERVICE",
        renews_item=True,
        renewed_items=tuple(renewed),
        event_date=at(day) - timedelta(days=3),
        renewal_date=at(day),
    )


FILTER_CONT = Population(components=frozenset({"Filter"}), failure_mechanisms=frozenset({"CONT"}))
CUTOFF = at("2025-12-31T00:00:00")


def failure_days(analysis, location=None):
    return [
        round(interval.raw_days)
        for interval in analysis.intervals
        if interval.end_type == std.FAILURE_END and (location is None or interval.location == location)
    ]


# --- spec section 5.2: the worked example -----------------------------------


def test_the_worked_example_measures_each_filter_on_its_own_clock():
    events = [
        pm_reset("4002-S09", "2025-01-01", ["Filter"]),
        pm_reset("4002-S11", "2025-01-10", ["Filter"]),
        failure("4002-S09", "Filter", "2025-02-01"),
        failure("4002-S11", "Filter", "2025-03-11"),
    ]

    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-03-31"))

    assert failure_days(analysis, "4002-S09") == [31]
    assert failure_days(analysis, "4002-S11") == [60]
    # The mixed list -- 22 d and 38 d -- is never produced.
    assert sorted(failure_days(analysis)) == [31, 60]


def test_no_interval_ever_spans_two_streams():
    events = [
        pm_reset("4002-S09", "2025-01-01", ["Filter"]),
        pm_reset("4002-S11", "2025-01-10", ["Filter"]),
        failure("4002-S09", "Filter", "2025-02-01"),
        failure("4002-S11", "Filter", "2025-03-11"),
        failure("4002-S09", "Filter", "2025-04-20"),
        pm_reset("4002-S11", "2025-05-02", ["Filter", "Belt"]),
        failure("4002-S11", "Filter", "2025-07-01"),
    ]
    stream_of = {}
    for event in events:
        for key, _ in std.stream_touches(event):
            stream_of.setdefault(event.record_id, set()).add(key)

    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=CUTOFF)

    for interval in analysis.intervals:
        assert interval.stream in stream_of[interval.start_record_id]
        if interval.end_record_id is not None:
            assert interval.stream in stream_of[interval.end_record_id]
    assert analysis.streams == [("4002-S09", "Filter"), ("4002-S11", "Filter")]


# --- spec section 5.2: what each event does to an interval --------------------


def test_the_first_renewal_only_starts_the_clock():
    analysis = build_renewal_intervals(
        [failure("L", "Filter", "2025-01-01"), failure("L", "Filter", "2025-02-01")], FILTER_CONT, cutoff=at("2025-02-01")
    )
    assert failure_days(analysis) == [31]
    assert analysis.notes[0].role == std.ROLE_STARTS_LIFE


def test_a_failure_of_the_mechanism_counts_whatever_mode_it_presented_as():
    events = [
        pm_reset("L", "2025-01-01", ["Filter"]),
        failure("L", "Filter", "2025-02-01", mode="UST"),
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-02-01"))
    assert analysis.failure_count == 1


def test_a_renewing_failure_of_another_mechanism_is_a_suspension_that_resets_the_clock():
    events = [
        pm_reset("L", "2025-01-01", ["Filter"]),
        failure("L", "Filter", "2025-01-21", mechanism="DEFM", action="REPLACE"),
        failure("L", "Filter", "2025-03-02"),
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-03-02"))
    assert [(i.end_type, round(i.raw_days)) for i in analysis.intervals] == [
        (std.SUSPENSION_END, 20),
        (std.FAILURE_END, 40),
    ]


def test_a_failure_of_another_mechanism_that_renewed_nothing_changes_nothing():
    events = [
        pm_reset("L", "2025-01-01", ["Filter"]),
        failure("L", "Filter", "2025-01-21", mechanism="LOOS", action="ADJUST"),
        failure("L", "Filter", "2025-03-02"),
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-03-02"))
    assert [(i.end_type, round(i.raw_days)) for i in analysis.intervals] == [(std.FAILURE_END, 60)]


def test_an_unknown_mechanism_is_run_both_ways():
    events = [
        pm_reset("L", "2025-01-01", ["Filter"]),
        failure("L", "Filter", "2025-01-31", mechanism="UNK"),
        failure("L", "Filter", "2025-03-02"),
    ]
    as_suspension = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-03-02"))
    as_failure = build_renewal_intervals(
        events, FILTER_CONT, cutoff=at("2025-03-02"), unknown_mechanism_as=std.UNKNOWN_AS_FAILURE
    )
    assert [i.end_type for i in as_suspension.intervals] == [std.SUSPENSION_END, std.FAILURE_END]
    assert [i.end_type for i in as_failure.intervals] == [std.FAILURE_END, std.FAILURE_END]
    assert len(as_suspension.unknown_decided) == 1


def test_a_failure_fixed_without_renewal_ends_the_life_and_starts_none():
    events = [
        pm_reset("L", "2025-01-01", ["Filter"]),
        failure("L", "Filter", "2025-01-11", action="RESET_ONLY"),
        failure("L", "Filter", "2025-01-21", action="RESET_ONLY"),
        failure("L", "Filter", "2025-02-10", action="REPLACE"),
        failure("L", "Filter", "2025-03-12"),
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-03-12"))
    # The first reset ends the first life; nothing is a life again until the
    # replacement on Feb 10 renews the filter.
    assert failure_days(analysis) == [10, 30]
    assert analysis.recurrent_streams == [("L", "Filter")]


def test_a_pm_reset_resets_only_the_items_it_lists():
    events = [
        pm_reset("L", "2025-01-01", ["Filter"]),
        pm_reset("L", "2025-01-15", ["Belt"]),
        failure("L", "Filter", "2025-02-01"),
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-02-01"))
    assert failure_days(analysis) == [31]
    assert analysis.streams == [("L", "Filter")]

    events.insert(2, pm_reset("L", "2025-01-21", ["Filter", "Belt"]))
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-02-01"))
    assert [(i.end_type, round(i.raw_days)) for i in analysis.intervals] == [
        (std.SUSPENSION_END, 20),
        (std.FAILURE_END, 11),
    ]


def test_items_renewed_alongside_a_failure_get_a_suspension_in_their_own_stream():
    events = [
        pm_reset("L", "2025-01-01", ["Filter", "Burner"]),
        failure("L", "Filter", "2025-02-01", renewed=["Burner"]),
    ]
    both = Population(components=frozenset({"Filter", "Burner"}), failure_mechanisms=frozenset({"CONT"}))
    analysis = build_renewal_intervals(events, both, cutoff=at("2025-02-01"))
    by_stream = {(i.component, i.end_type) for i in analysis.intervals}
    assert by_stream == {("Filter", std.FAILURE_END), ("Burner", std.SUSPENSION_END)}


def test_categories_with_no_life_role_never_reach_a_stream():
    def other(category):
        return StreamEvent(
            record_id=next(_ids), category=category, location="L", component="Filter", mechanism="CONT",
            renews_item=True, renewed_items=("Filter",), event_date=at("2025-01-20"), renewal_date=at("2025-01-20"),
        )

    events = [pm_reset("L", "2025-01-01", ["Filter"]), failure("L", "Filter", "2025-02-01")] + [
        other(category) for category in (std.EXTERNAL, std.NON_FAILURE, std.HELD, std.NOT_REVIEWED, std.DUPLICATE_MISFILED)
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-02-01"))
    assert failure_days(analysis) == [31]


def test_a_life_still_running_at_the_cutoff_is_a_suspension_there():
    analysis = build_renewal_intervals([pm_reset("L", "2025-01-01", ["Filter"])], FILTER_CONT, cutoff=at("2025-01-11"))
    [interval] = analysis.intervals
    assert interval.end_type == std.SUSPENSION_END
    assert interval.end_record_id is None
    assert round(interval.raw_days) == 10


def test_the_window_leaves_out_events_outside_it_and_a_renewal_after_the_cutoff():
    events = [
        failure("L", "Filter", "2024-11-01"),
        pm_reset("L", "2025-01-01", ["Filter"]),
        failure("L", "Filter", "2025-02-01", repaired="2025-02-20"),
        failure("L", "Filter", "2025-03-01"),
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, start=at("2024-12-01"), cutoff=at("2025-02-10"))
    # The failure before the start is not seen; the repair finishing after the
    # cutoff starts no new life, so nothing is censored at the cutoff either.
    assert [(i.end_type, round(i.raw_days)) for i in analysis.intervals] == [(std.FAILURE_END, 31)]
    roles = [note.role for note in analysis.notes]
    assert roles.count(std.ROLE_EXCLUDED) == 2


def test_a_life_is_measured_from_the_repair_to_the_next_failure_found():
    # Found Feb 1, repaired Feb 5; found again Mar 7: the life is Feb 5 to Mar 7.
    events = [
        failure("L", "Filter", "2025-02-01", repaired="2025-02-05"),
        failure("L", "Filter", "2025-03-07"),
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-03-07"))
    assert failure_days(analysis) == [30]


def test_a_failure_dated_inside_the_previous_repair_makes_no_life():
    events = [
        failure("L", "Filter", "2025-02-01", repaired="2025-02-10"),
        failure("L", "Filter", "2025-02-05", repaired="2025-02-12"),
        failure("L", "Filter", "2025-03-14"),
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-03-14"))
    assert failure_days(analysis) == [30]
    assert any("overlapping or duplicate" in note.note for note in analysis.notes)


def test_induced_failures_can_be_treated_as_suspensions():
    events = [
        pm_reset("L", "2025-01-01", ["Filter"]),
        failure("L", "Filter", "2025-01-11", cause="MNT-ERR", action="REPLACE"),
        failure("L", "Filter", "2025-02-10"),
    ]
    kept = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-02-10"))
    excluded = build_renewal_intervals(
        events, Population(**{**FILTER_CONT.__dict__, "exclude_induced": True}), cutoff=at("2025-02-10")
    )
    assert kept.failure_count == 2
    assert [i.end_type for i in excluded.intervals] == [std.SUSPENSION_END, std.FAILURE_END]


def test_life_hours_come_from_the_callback_per_stream():
    seen = []

    def hours(stream, start, end):
        seen.append(stream)
        return 1.0

    build_renewal_intervals(
        [pm_reset("L", "2025-01-01", ["Filter"]), failure("L", "Filter", "2025-02-01")],
        FILTER_CONT,
        cutoff=at("2025-02-01"),
        life_hours=hours,
    )
    assert seen == [("L", "Filter")]


# --- spec section 5.3: warnings -------------------------------------------------


def warning_codes(analysis, population, **kwargs):
    return {warning["code"] for warning in validity_warnings(analysis, population, codebook=CODES, **kwargs)}


def test_warnings_name_each_way_a_population_can_mislead():
    events = [
        pm_reset("L", "2025-01-01", ["Filter", "Belt"]),
        failure("L", "Filter", "2025-02-01", qty=5),
        failure("L", "Belt", "2025-02-15", mechanism="WEAR", action="REPLACE", cause="MNT-ERR"),
        failure("L", "Filter", "2025-03-01", action="RESET_ONLY"),
    ]
    mixed = Population(failure_modes=frozenset({"FTS"}))
    analysis = build_renewal_intervals(events, mixed, cutoff=at("2025-03-31"))
    codes = warning_codes(analysis, mixed, uncoded_count=2, unreviewed_count=3)
    assert codes == {
        "MIXED_COMPONENTS", "MODE_KEYED", "MIXED_MECHANISMS", "SMALL_N", "MULTI_UNIT",
        "INDUCED_FAILURES", "RECURRENT_EVENTS", "UNCODED_RECORDS", "NOT_REVIEWED",
    }


def test_a_clean_population_warns_only_of_its_size():
    events = [pm_reset("L", "2025-01-01", ["Filter"])] + [
        failure("L", "Filter", f"2025-{month:02d}-01") for month in range(2, 10)
    ]
    analysis = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-09-01"))
    assert analysis.failure_count == 8
    assert warning_codes(analysis, FILTER_CONT) == set()


def test_the_unknown_share_reads_the_run_that_counts_unknowns():
    events = [
        pm_reset("L", "2025-01-01", ["Filter"]),
        failure("L", "Filter", "2025-02-01", mechanism="UNK"),
        failure("L", "Filter", "2025-03-01"),
    ]
    main = build_renewal_intervals(events, FILTER_CONT, cutoff=at("2025-03-01"))
    sensitivity = build_renewal_intervals(
        events, FILTER_CONT, cutoff=at("2025-03-01"), unknown_mechanism_as=std.UNKNOWN_AS_FAILURE
    )
    [warning] = [
        w for w in validity_warnings(main, FILTER_CONT, sensitivity=sensitivity, codebook=CODES)
        if w["code"] == "UNKNOWN_SHARE"
    ]
    assert warning["message"].startswith("1 of 2 failures (50%)")
    assert "1 failures counting them as suspensions, 2 counting them as failures" in warning["message"]


# --- spec section 5.4: the population definition --------------------------------


def test_the_population_definition_reads_as_the_spec_writes_it():
    population = Population(
        locations=frozenset({"4002-S09"}), components=frozenset({"Filter"}), failure_mechanisms=frozenset({"CONT"})
    )
    assert population_definition(population, date(2022, 4, 21), date(2026, 10, 15)) == (
        "POP: location=4002-S09; component=Filter; failure_mech=CONT; suspensions=other renewals; "
        "window=2022-04-21..2026-10-15"
    )


def test_a_population_round_trips_through_its_mapping():
    population = Population.from_mapping({
        "assets": ["4002"], "components": "Filter, Belt", "categories": ["failure"], "exclude_induced": "true",
    })
    assert population.components == {"Filter", "Belt"}
    assert population.categories == {"FAILURE"}
    assert population.exclude_induced is True
    assert Population.from_mapping(population.to_mapping()) == population
    with pytest.raises(ValueError):
        Population.from_mapping({"categories": ["BOGUS"]})


# --- spec section 2: the field rules ---------------------------------------------

RECORD = RecordContext(mapped_record_id=7, asset_number="4002", sub_asset_choices=("4002-S09", "4002-S11"))
COMPLETE_FAILURE = {
    "disposition_category": "FAILURE",
    "sub_asset": "4002-S09",
    "component_code": "Filter",
    "failure_mechanism_code": "CONT",
    "failure_mode_code": "FTS",
    "action_code": "CLEAN_SERVICE",
    "evidence": "EXPLICIT",
}


def normalize(values, previous=None):
    return normalize_disposition(values, record=RECORD, codebook=CODES, previous=previous)


def test_a_typical_failure_is_five_picks_and_a_location():
    saved = normalize(COMPLETE_FAILURE)
    assert saved["sub_asset"] == "4002-S09"
    assert saved["failure_cause_code"] is None
    assert saved["renewed_items"] == ()


def test_the_coded_categories_need_every_coded_field():
    for category in std.CODED_CATEGORIES:
        with pytest.raises(ValueError, match="needs Component, Failure mechanism, Failure mode, Action, Evidence"):
            normalize({"disposition_category": category, "renewed_items": ["Filter"]})
    # Held and not reviewed may carry partial codes.
    assert normalize({"disposition_category": "NOT_REVIEWED", "component_code": "Filter"})["component_code"] == "Filter"


def test_a_duplicate_keeps_only_its_link_and_note():
    saved = normalize({**COMPLETE_FAILURE, "disposition_category": "DUPLICATE_MISFILED", "linked_mapped_record_id": "12"})
    assert saved["linked_mapped_record_id"] == 12
    assert saved["component_code"] is None and saved["sub_asset"] is None
    with pytest.raises(ValueError, match="duplicate of itself"):
        normalize({"disposition_category": "DUPLICATE_MISFILED", "linked_mapped_record_id": 7})
    with pytest.raises(ValueError, match="only for DUPLICATE_MISFILED"):
        normalize({**COMPLETE_FAILURE, "linked_mapped_record_id": 12})


def test_held_and_a_cause_override_need_a_note():
    with pytest.raises(ValueError, match="HELD needs a disposition note"):
        normalize({"disposition_category": "HELD"})
    with pytest.raises(ValueError, match="Overriding the failure cause"):
        normalize({**COMPLETE_FAILURE, "failure_cause_code": "MNT-INT"})
    saved = normalize({**COMPLETE_FAILURE, "failure_cause_code": "MNT-INT", "disposition_note": "PM overdue 3 weeks"})
    assert saved["failure_cause_code"] == "MNT-INT"
    # Choosing the mechanism's own default is not an override.
    assert normalize({**COMPLETE_FAILURE, "failure_cause_code": "EXP-WEAR"})["failure_cause_code"] is None


def test_renewed_items_rules():
    with pytest.raises(ValueError, match="PM_RESET needs the renewed items"):
        normalize({**COMPLETE_FAILURE, "disposition_category": "PM_RESET"})
    assert normalize({**COMPLETE_FAILURE, "disposition_category": "PM_RESET", "renewed_items": "Filter,Belt"})[
        "renewed_items"
    ] == ("Filter", "Belt")
    with pytest.raises(ValueError, match="repeats the component"):
        normalize({**COMPLETE_FAILURE, "renewed_items": ["Filter"]})
    with pytest.raises(ValueError, match="only for FAILURE and PM_RESET"):
        normalize({**COMPLETE_FAILURE, "disposition_category": "EXTERNAL", "renewed_items": ["Belt"]})
    with pytest.raises(ValueError, match="not in the component list"):
        normalize({**COMPLETE_FAILURE, "renewed_items": ["Flux capacitor"]})


def test_no_fault_found_is_never_a_failure():
    with pytest.raises(ValueError, match="NON_FAILURE"):
        normalize({**COMPLETE_FAILURE, "failure_mode_code": "NFF"})
    assert normalize({**COMPLETE_FAILURE, "disposition_category": "NON_FAILURE", "failure_mode_code": "NFF"})


def test_sub_asset_must_be_one_of_the_assets_children():
    assert normalize({**COMPLETE_FAILURE, "sub_asset": "4002"})["sub_asset"] is None
    with pytest.raises(ValueError, match="not one of asset 4002's sub-assets"):
        normalize({**COMPLETE_FAILURE, "sub_asset": "3101-S01"})


def test_a_retired_code_is_kept_only_while_nobody_changes_it():
    retired = CodeBook([
        entry if entry.code != "Filter" else std.CodeEntry(**{**entry.__dict__, "is_active": False})
        for entries in std.SEED_CODES.values()
        for entry in entries
    ])
    with pytest.raises(ValueError, match="retired"):
        normalize_disposition(COMPLETE_FAILURE, record=RECORD, codebook=retired)
    assert normalize_disposition(COMPLETE_FAILURE, record=RECORD, codebook=retired, previous=COMPLETE_FAILURE)


def test_qty_is_a_positive_whole_number():
    assert normalize({**COMPLETE_FAILURE, "qty": "5"})["qty"] == 5
    for bad in ("0", "-1", "2.5", "five"):
        with pytest.raises(ValueError, match="Qty"):
            normalize({**COMPLETE_FAILURE, "qty": bad})


# --- spec section 4: derived fields ------------------------------------------------


def test_derived_fields():
    derived = derive_fields(normalize(COMPLETE_FAILURE), asset_number="4002", record_class="CORRECTIVE_WO", codebook=CODES)
    assert derived == {
        "location": "4002-S09",
        "failure_cause_effective": "EXP-WEAR",
        "failure_cause_is_override": False,
        "detection_method": std.DETECTION_PRODUCTION_INTERFERENCE,
        "renews_item": True,
        "renewal_stream_key": "4002-S09 | Filter",
    }
    found = derive_fields({**COMPLETE_FAILURE, "failure_mode_code": "DEG"}, asset_number="4002", record_class=None, codebook=CODES)
    assert found["detection_method"] == std.DETECTION_INSPECTION
    planned = derive_fields({**COMPLETE_FAILURE, "sub_asset": None}, asset_number="4002", record_class="PM", codebook=CODES)
    assert planned["detection_method"] == std.DETECTION_PERIODIC_MAINTENANCE
    assert planned["location"] == "4002"


# --- spec section 2.2: migration ----------------------------------------------------


def test_only_an_unambiguous_legacy_mechanism_is_converted():
    convert = lambda text, choices=("4002-S09", "4002-S11"), asset="4002": legacy_auto_conversion(
        text, asset_number=asset, sub_asset_choices=choices, codebook=CODES
    )
    assert convert("S09 clogged air filter") == {
        "sub_asset": "4002-S09", "component_code": "Filter", "failure_mechanism_code": "CONT",
    }
    assert convert("S09 clogged air filter", asset="4002-S09", choices=()) == {
        "sub_asset": None, "component_code": "Filter", "failure_mechanism_code": "CONT",
    }
    assert convert("S07 clogged air filter") is None
    assert convert("S09 clogged air filter", choices=("4002-S09", "4003-S09")) is None
    assert convert("PFS oven/heating/temperature failure") is None


def test_every_legacy_category_has_a_home():
    assert set(std.LEGACY_CATEGORY_MAP.values()) <= set(std.DISPOSITION_CATEGORIES)
    assert std.LEGACY_CATEGORY_MAP["INCLUDED_FAILURE"] == std.FAILURE
    assert std.LEGACY_CATEGORY_MAP["EXCLUDED_MIXED_CONTAMINATING"] == std.DUPLICATE_MISFILED


def test_the_seed_lists_are_internally_consistent():
    for code_list, entries in std.SEED_CODES.items():
        codes = [entry.code for entry in entries]
        assert len(codes) == len(set(codes)), code_list
        for entry in entries:
            assert std.CODE_PATTERN.match(entry.code), entry.code
            assert entry.semantic in (None, *std.CODE_LIST_SEMANTICS[code_list])
            if entry.default_cause_code:
                assert CODES.entry(std.FAILURE_CAUSE, entry.default_cause_code), entry.code
    assert {e.code for e in CODES.entries(std.ACTION) if e.renews_item} == {"REPLACE", "REPAIR", "CLEAN_SERVICE"}
