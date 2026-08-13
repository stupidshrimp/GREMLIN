"""Availability calculation tests, pinned to the Availability Dashboard workbook.

The golden figures come from `Availability_Dashboard_V6TEST.xlsm`, January 2026,
the Salvagnini group -- chosen because it is the group that exercises linked
downtime, and because January contains no Limble type-4 downtime, so the
workbook's (mistaken) type filter and this app's count-everything rule agree
there exactly. Any divergence in these numbers is a real regression, not a
difference of definition.
"""

import unittest
from datetime import date, datetime

from services.availability_service import (
    DEFAULT_GOAL_PERCENT,
    AssetGroup,
    LinkedRule,
    WorkOrder,
    WorkOrderDetail,
    add_months,
    availability_percent,
    build_series,
    compute_rows,
    format_month_labels,
    last_complete_month,
    resolve_window,
    weekday_count,
    work_order_contributions,
)

JAN = date(2026, 1, 1)

# Salvagnini: 24 h/day less 1 break + 2 lunch + 3 setup = 18 net.
SALVAGNINI = AssetGroup(
    asset_group="Salvagnini",
    asset_numbers=("3101", "3102", "3103", "3104", "3105", "3106", "3107"),
    schedule_hours_per_day=24,
    break_hours_per_day=1,
    lunch_hours_per_day=2,
    setup_hours_per_day=3,
)

DISPLAY_NAMES = {
    "3101": "MV", "3102": "PA", "3103": "L3", "3104": "ADL",
    "3105": "S4", "3106": "SMD", "3107": "ACN",
}

# Configurator!tblLinkedDowntimeConfig -- all 13 seeded rules, all at 0.5.
LINKED_RULES = [
    LinkedRule("3102", "3107", 0.5), LinkedRule("3102", "3101", 0.5),
    LinkedRule("3102", "3105", 0.5), LinkedRule("3102", "3106", 0.5),
    LinkedRule("3103", "3104", 0.5), LinkedRule("3103", "3101", 0.5),
    LinkedRule("3104", "3101", 0.5), LinkedRule("3105", "3101", 0.5),
    LinkedRule("3105", "3106", 0.5), LinkedRule("3106", "3101", 0.5),
    LinkedRule("3107", "3105", 0.5), LinkedRule("3107", "3106", 0.5),
    LinkedRule("3107", "3101", 0.5),
]

# Availability Data column G, January 2026: direct downtime hours per asset.
# Full stored precision, not the two decimals the sheet displays -- the workbook
# carries these through unrounded, so rounding here would manufacture a
# mismatch that is not real.
JANUARY_DIRECT = {
    "3101": 10.0,
    "3102": 12.5,
    "3103": 8.866666666666667,
    "3104": 0.0,
    "3105": 29.166666666666668,
    "3106": 3.0,
    "3107": 6.7,
}

# Availability Data columns N (linked), O (adjusted) and M (adjusted availability).
JANUARY_EXPECTED = {
    "3101": (0.0, 10.0, 0.9747474747474747),
    "3102": (24.433333333333334, 36.93333333333334, 0.9067340067340067),
    "3103": (5.0, 13.866666666666667, 0.9649831649831649),
    "3104": (5.0, 5.0, 0.9873737373737373),
    "3105": (6.5, 35.66666666666667, 0.9099326599326599),
    "3106": (5.0, 8.0, 0.9797979797979798),
    "3107": (21.083333333333336, 27.783333333333335, 0.9298400673400673),
}


def january_work_orders() -> list[WorkOrder]:
    """One work order per asset carrying that asset's January downtime."""

    return [
        WorkOrder(asset_number=asset, created_local=datetime(2026, 1, 15, 9, 0), downtime_hours=hours)
        for asset, hours in JANUARY_DIRECT.items()
    ]


def january_rows(**kwargs):
    return compute_rows(
        [SALVAGNINI],
        [JAN],
        kwargs.pop("work_orders", january_work_orders()),
        linked_rules=kwargs.pop("linked_rules", LINKED_RULES),
        display_names=DISPLAY_NAMES,
        **kwargs,
    )


class ScheduledHoursTests(unittest.TestCase):
    def test_net_hours_derive_from_the_parts(self):
        # Configurator!G9 = MAX(0, C9 - SUM(D9:F9)) -> 18.
        self.assertEqual(SALVAGNINI.net_scheduled_hours_per_day, 18.0)

    def test_net_hours_never_go_negative(self):
        group = AssetGroup("X", ("1",), schedule_hours_per_day=4, break_hours_per_day=9)
        self.assertEqual(group.net_scheduled_hours_per_day, 0.0)

    def test_weekday_count_matches_the_workbook(self):
        # January 2026 has 22 weekdays, February 20, May 21.
        self.assertEqual(weekday_count(2026, 1), 22)
        self.assertEqual(weekday_count(2026, 2), 20)
        self.assertEqual(weekday_count(2026, 5), 21)

    def test_scheduled_hours_are_weekdays_times_net_hours(self):
        row = next(r for r in january_rows() if r.asset_number == "3101")
        self.assertEqual(row.scheduled_hours, 396.0)  # 22 x 18


class WorkbookParityTests(unittest.TestCase):
    """Every Salvagnini asset for January 2026 must match the workbook."""

    def test_direct_downtime_matches(self):
        rows = {r.asset_number: r for r in january_rows()}
        for asset, expected in JANUARY_DIRECT.items():
            self.assertAlmostEqual(rows[asset].direct_downtime_hours, expected, places=9)

    def test_linked_adjusted_and_availability_match(self):
        rows = {r.asset_number: r for r in january_rows()}
        for asset, (linked, adjusted, availability) in JANUARY_EXPECTED.items():
            with self.subTest(asset=asset):
                self.assertAlmostEqual(rows[asset].linked_downtime_hours, linked, places=9)
                self.assertAlmostEqual(rows[asset].adjusted_downtime_hours, adjusted, places=9)
                self.assertAlmostEqual(rows[asset].availability, availability, places=12)

    def test_group_average_matches_chart_data(self):
        series = build_series(january_rows(), [SALVAGNINI], [JAN])[0]
        # 'Chart Data'!B10 =AVERAGE(B3:B9)
        self.assertAlmostEqual(series.average[0], 0.95048701298701299, places=12)

    def test_goal_defaults_to_95_percent(self):
        series = build_series(january_rows(), [SALVAGNINI], [JAN])[0]
        self.assertEqual(series.goal[0], DEFAULT_GOAL_PERCENT)
        self.assertEqual(series.goal[0], 0.95)

    def test_explicit_goal_overrides_the_default(self):
        series = build_series(
            january_rows(), [SALVAGNINI], [JAN], goals={("Salvagnini", JAN): 0.97}
        )[0]
        self.assertEqual(series.goal[0], 0.97)

    def test_display_names_reach_the_series(self):
        series = build_series(january_rows(), [SALVAGNINI], [JAN])[0]
        self.assertEqual([a["display_name"] for a in series.assets],
                         ["MV", "PA", "L3", "ADL", "S4", "SMD", "ACN"])


class LinkedDowntimeTests(unittest.TestCase):
    def test_linked_downtime_is_a_share_of_direct_downtime(self):
        # 3102 <- 3107 6.70 + 3101 10.00 + 3105 29.17 + 3106 3.00, each at 0.5.
        rows = {r.asset_number: r for r in january_rows()}
        expected = (JANUARY_DIRECT["3107"] + JANUARY_DIRECT["3101"]
                    + JANUARY_DIRECT["3105"] + JANUARY_DIRECT["3106"]) * 0.5
        self.assertAlmostEqual(rows["3102"].linked_downtime_hours, expected, places=9)

    def test_linked_downtime_does_not_cascade(self):
        """A parent's share reads the child's *direct* downtime, never its adjusted.

        3103 links only to 3104 and 3101. 3104 itself carries 5.00 h of linked
        downtime from 3101, but contributes 0.00 h of direct downtime, so 3103
        must see only 3101's half -- 5.00, not 7.50.
        """

        rows = {r.asset_number: r for r in january_rows()}
        self.assertAlmostEqual(rows["3104"].linked_downtime_hours, 5.00, places=6)
        self.assertAlmostEqual(rows["3103"].linked_downtime_hours, 5.00, places=6)

    def test_a_pure_source_asset_keeps_its_own_downtime(self):
        # 3101 is charged to five other assets but has no parent rules itself.
        row = next(r for r in january_rows() if r.asset_number == "3101")
        self.assertEqual(row.linked_downtime_hours, 0.0)
        self.assertEqual(row.downtime_logic, "Direct downtime only")

    def test_downtime_logic_labels_linked_rows(self):
        row = next(r for r in january_rows() if r.asset_number == "3102")
        self.assertEqual(row.downtime_logic, "Direct + linked downtime")

    def test_simultaneous_outages_are_counted_twice(self):
        """No overlap merging: two linked assets down at once both charge the parent.

        Documented in design doc §2.4 as intentional. Pinned so the behaviour
        cannot change silently.
        """

        group = AssetGroup("G", ("P", "A", "B"), schedule_hours_per_day=8)
        when = datetime(2026, 1, 5, 8, 0)
        rows = {
            r.asset_number: r
            for r in compute_rows(
                [group],
                [JAN],
                [
                    WorkOrder("A", when, 4.0),
                    WorkOrder("B", when, 4.0),  # exactly the same four hours
                ],
                linked_rules=[LinkedRule("P", "A", 0.5), LinkedRule("P", "B", 0.5)],
            )
        }
        self.assertEqual(rows["P"].linked_downtime_hours, 4.0)


class ClampingAndFlagTests(unittest.TestCase):
    def test_availability_clamps_to_zero_and_flags(self):
        group = AssetGroup("G", ("A",), schedule_hours_per_day=1)  # 22 x 1 = 22 h
        row = compute_rows([group], [JAN], [WorkOrder("A", datetime(2026, 1, 2, 8, 0), 500.0)])[0]
        self.assertEqual(row.availability, 0.0)
        self.assertTrue(row.flagged)

    def test_availability_is_undefined_without_scheduled_hours(self):
        group = AssetGroup("G", ("A",), schedule_hours_per_day=0)
        row = compute_rows([group], [JAN], [])[0]
        self.assertIsNone(row.availability)
        self.assertIsNone(availability_percent(0.0, 0.0))

    def test_negative_downtime_is_discarded(self):
        group = AssetGroup("G", ("A",), schedule_hours_per_day=1)
        row = compute_rows([group], [JAN], [WorkOrder("A", datetime(2026, 1, 2, 8, 0), -40.0)])[0]
        self.assertEqual(row.direct_downtime_hours, 0.0)
        self.assertEqual(row.availability, 1.0)


class DataQualityTests(unittest.TestCase):
    def test_an_asset_with_no_work_orders_reports_full_availability_and_says_so(self):
        """100% with no evidence must be distinguishable from a genuinely perfect month."""

        group = AssetGroup("G", ("A",), schedule_hours_per_day=8)
        row = compute_rows([group], [JAN], [])[0]
        self.assertEqual(row.availability, 1.0)
        self.assertTrue(row.no_wo_entries)
        self.assertEqual(row.total_wo_count, 0)
        self.assertEqual(row.note, "No WO entries this month")

    def test_zero_downtime_work_orders_are_counted_and_noted(self):
        # Availability Data Q/R for 3101 January: 13 work orders, 3 with no downtime.
        group = AssetGroup("G", ("A",), schedule_hours_per_day=8)
        when = datetime(2026, 1, 9, 10, 0)
        orders = [WorkOrder("A", when, 0.0) for _ in range(3)] + [WorkOrder("A", when, 2.0) for _ in range(10)]
        row = compute_rows([group], [JAN], orders)[0]
        self.assertEqual(row.total_wo_count, 13)
        self.assertEqual(row.zero_downtime_wo_count, 3)
        self.assertFalse(row.no_wo_entries)
        self.assertEqual(row.note, "3 WO(s) with 0 downtime")

    def test_a_clean_month_carries_no_note(self):
        group = AssetGroup("G", ("A",), schedule_hours_per_day=8)
        row = compute_rows([group], [JAN], [WorkOrder("A", datetime(2026, 1, 9, 10, 0), 2.0)])[0]
        self.assertEqual(row.note, "")


class DowntimeAttributionTests(unittest.TestCase):
    def test_downtime_lands_in_the_created_month_not_the_completed_month(self):
        """Design doc §3: created-date bucketing, so work can bleed across months."""

        group = AssetGroup("G", ("A",), schedule_hours_per_day=8)
        order = WorkOrder("A", datetime(2026, 1, 31, 20, 0), 10.0, completed_local=datetime(2026, 2, 1, 6, 0))
        rows = {r.month: r for r in compute_rows([group], [JAN, date(2026, 2, 1)], [order])}
        self.assertEqual(rows[JAN].direct_downtime_hours, 10.0)
        self.assertEqual(rows[date(2026, 2, 1)].direct_downtime_hours, 0.0)

    def test_month_crossing_work_orders_are_flagged_as_overlaps(self):
        group = AssetGroup("G", ("A",), schedule_hours_per_day=8)
        order = WorkOrder("A", datetime(2026, 1, 31, 20, 0), 10.0, completed_local=datetime(2026, 2, 1, 6, 0))
        row = compute_rows([group], [JAN], [order])[0]
        self.assertEqual(row.overlap_count, 1)

    def test_a_within_month_work_order_is_not_an_overlap(self):
        group = AssetGroup("G", ("A",), schedule_hours_per_day=8)
        order = WorkOrder("A", datetime(2026, 1, 5, 8, 0), 2.0, completed_local=datetime(2026, 1, 5, 10, 0))
        self.assertEqual(compute_rows([group], [JAN], [order])[0].overlap_count, 0)

    def test_duration_alone_can_make_an_overlap(self):
        # No completion date, but the downtime itself runs past month end.
        group = AssetGroup("G", ("A",), schedule_hours_per_day=8)
        order = WorkOrder("A", datetime(2026, 1, 31, 20, 0), 10.0)
        self.assertEqual(compute_rows([group], [JAN], [order])[0].overlap_count, 1)


class NoClassificationFilterTests(unittest.TestCase):
    """Design doc §2.1 -- every work order with downtime counts."""

    def test_work_order_carries_no_type_or_classification_field(self):
        """Filtering by PM/corrective must be impossible by construction.

        The 623.7 h regression came from excluding `is_pm_candidate`, which a
        `\\bpm\\b` regex sets from the clock time in return-to-service notes.
        Keeping type and classification out of the calculator's input type means
        no future change can reintroduce that filter here.
        """

        fields = set(WorkOrder.__dataclass_fields__)
        self.assertEqual(fields & {"type", "type_raw", "record_class", "is_pm_candidate"}, set())

    def test_every_supplied_work_order_contributes(self):
        group = AssetGroup("G", ("A",), schedule_hours_per_day=8)
        when = datetime(2026, 1, 9, 10, 0)
        row = compute_rows([group], [JAN], [WorkOrder("A", when, 3.0), WorkOrder("A", when, 4.0)])[0]
        self.assertEqual(row.direct_downtime_hours, 7.0)


class WindowTests(unittest.TestCase):
    def test_default_window_is_five_complete_months(self):
        months = resolve_window(date(2026, 8, 6))
        self.assertEqual(months, [date(2026, m, 1) for m in (3, 4, 5, 6, 7)])

    def test_the_current_partial_month_is_excluded(self):
        self.assertNotIn(date(2026, 8, 1), resolve_window(date(2026, 8, 6)))
        self.assertEqual(last_complete_month(date(2026, 8, 6)), date(2026, 7, 1))

    def test_window_clamps_to_available_history(self):
        months = resolve_window(date(2026, 8, 6), data_earliest=date(2026, 5, 20))
        self.assertEqual(months, [date(2026, m, 1) for m in (5, 6, 7)])

    def test_window_clamps_to_the_latest_month_with_data(self):
        months = resolve_window(date(2026, 8, 6), data_latest=date(2026, 5, 9))
        self.assertEqual(months, [date(2026, m, 1) for m in (1, 2, 3, 4, 5)])

    def test_window_length_is_configurable(self):
        self.assertEqual(len(resolve_window(date(2026, 8, 6), months=12)), 12)

    def test_month_arithmetic_crosses_year_boundaries(self):
        self.assertEqual(add_months(date(2026, 1, 1), -1), date(2025, 12, 1))
        self.assertEqual(add_months(date(2026, 12, 1), 1), date(2027, 1, 1))
        self.assertEqual(last_complete_month(date(2026, 1, 20)), date(2025, 12, 1))


class MonthLabelTests(unittest.TestCase):
    """Labels head the editable Goal and OT columns, so they must be unambiguous."""

    def test_a_single_year_window_uses_bare_month_names(self):
        months = [date(2026, m, 1) for m in (3, 4, 5, 6, 7)]
        self.assertEqual(format_month_labels(months), ["Mar", "Apr", "May", "Jun", "Jul"])

    def test_a_window_spanning_years_carries_the_year(self):
        months = [date(2025, 12, 1), date(2026, 1, 1)]
        self.assertEqual(format_month_labels(months), ["Dec 25", "Jan 26"])

    def test_a_long_window_never_repeats_a_label(self):
        months = [add_months(date(2025, 6, 1), i) for i in range(24)]
        labels = format_month_labels(months)
        self.assertEqual(len(labels), len(set(labels)))

    def test_every_label_carries_the_year_not_only_the_repeats(self):
        """A mix of 'Jun' and 'Jun 25' reads as two kinds of thing."""

        months = [add_months(date(2025, 6, 1), i) for i in range(13)]
        labels = format_month_labels(months)
        self.assertTrue(all(len(label.split()) == 2 for label in labels), labels)

    def test_the_series_uses_the_disambiguated_labels(self):
        months = [date(2025, 12, 1), date(2026, 1, 1)]
        orders = [WorkOrder("3101", datetime(2025, 12, 5, 9, 0), 4.0)]
        rows = compute_rows([SALVAGNINI], months, orders)
        series = build_series(rows, [SALVAGNINI], months)[0]
        self.assertEqual(series.month_labels, ["Dec 25", "Jan 26"])

    def test_an_empty_window_has_no_labels(self):
        self.assertEqual(format_month_labels([]), [])


class GroupHandlingTests(unittest.TestCase):
    def test_excluded_groups_produce_no_rows(self):
        """Dilo & Enervac is configured with Include? = N -- nine charts, not ten."""

        excluded = AssetGroup("Dilo & Enervac", (), schedule_hours_per_day=20, include=False)
        rows = compute_rows([SALVAGNINI, excluded], [JAN], january_work_orders())
        self.assertEqual({r.asset_group for r in rows}, {"Salvagnini"})
        series = build_series(rows, [SALVAGNINI, excluded], [JAN])
        self.assertEqual([s.asset_group for s in series], ["Salvagnini"])

    def test_series_reports_the_schedule_basis(self):
        """The chart states its own assumptions so screenshots carry them."""

        series = build_series(january_rows(), [SALVAGNINI], [JAN])[0]
        self.assertEqual(series.net_scheduled_hours_per_day, 18.0)

    def test_manual_ot_adds_to_scheduled_hours(self):
        rows = {r.asset_number: r for r in january_rows(manual_ot={("3101", JAN): 40.0})}
        self.assertEqual(rows["3101"].manual_ot_hours, 40.0)
        self.assertEqual(rows["3101"].adjusted_scheduled_hours, 436.0)
        self.assertAlmostEqual(rows["3101"].availability, (436.0 - 10.0) / 436.0, places=9)

    def test_average_ignores_months_with_no_defined_availability(self):
        group = AssetGroup("G", ("A",), schedule_hours_per_day=0)
        series = build_series(compute_rows([group], [JAN], []), [group], [JAN])[0]
        self.assertIsNone(series.average[0])


def detail(asset, when, hours, **text):
    return WorkOrderDetail(
        order=WorkOrder(asset_number=asset, created_local=when, downtime_hours=hours), **text
    )


def january_details():
    """The workbook's January work orders, as the drill-down loads them."""

    return [
        detail(asset, datetime(2026, 1, 15, 9, 0), hours, task_name=f"{asset} breakdown")
        for asset, hours in JANUARY_DIRECT.items()
    ]


class WorkOrderContributionTests(unittest.TestCase):
    """The rows a reader is shown under a bar must add up to that bar.

    Every assertion here is really the same one: whatever ``compute_rows``
    counted, this lists, at the same hours, and nothing else.
    """

    def contributions(self, asset, **kwargs):
        return work_order_contributions(
            asset,
            JAN,
            kwargs.pop("details", january_details()),
            linked_rules=kwargs.pop("linked_rules", LINKED_RULES),
        )

    def test_the_rows_sum_to_the_bar_for_every_asset(self):
        rows = {row.asset_number: row for row in january_rows()}
        for asset in SALVAGNINI.asset_numbers:
            with self.subTest(asset=asset):
                found = self.contributions(asset)
                direct = sum(c.counted_hours for c in found if c.source == "direct")
                linked = sum(c.counted_hours for c in found if c.source == "linked")
                self.assertAlmostEqual(direct, rows[asset].direct_downtime_hours, places=12)
                self.assertAlmostEqual(linked, rows[asset].linked_downtime_hours, places=12)

    def test_the_workbook_linked_case_is_itemised(self):
        """3102's 24.43 h, broken back out into the four rules that made it."""

        found = {
            c.order.asset_number: c for c in self.contributions("3102") if c.source == "linked"
        }
        self.assertEqual(set(found), {"3101", "3105", "3106", "3107"})
        self.assertAlmostEqual(found["3107"].counted_hours, 3.35, places=12)
        self.assertAlmostEqual(found["3101"].counted_hours, 5.0, places=12)
        self.assertAlmostEqual(
            sum(c.counted_hours for c in found.values()), 24.433333333333334, places=12
        )

    def test_a_linked_row_reports_its_own_downtime_and_its_share_separately(self):
        """The two differ, and a reader tracing a number needs to see both."""

        linked = next(c for c in self.contributions("3102") if c.order.asset_number == "3101")
        self.assertEqual(linked.order.downtime_hours, 10.0)
        self.assertEqual(linked.counted_hours, 5.0)
        self.assertEqual(linked.impact_factor, 0.5)

    def test_an_asset_with_no_rules_lists_only_its_own_work_orders(self):
        found = self.contributions("3101")
        self.assertEqual([c.source for c in found], ["direct"])
        self.assertEqual(found[0].counted_hours, 10.0)

    def test_the_biggest_contributor_comes_first(self):
        counted = [c.counted_hours for c in self.contributions("3102")]
        self.assertEqual(counted, sorted(counted, reverse=True))

    def test_zero_downtime_orders_are_listed_and_count_nothing(self):
        """An asset at 100% is either clean or unlogged; only the rows say which."""

        details = [detail("3101", datetime(2026, 1, 8, 7, 0), 0.0, task_name="Inspection")]
        found = self.contributions("3101", details=details, linked_rules=[])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].counted_hours, 0.0)
        self.assertEqual(found[0].detail.task_name, "Inspection")

    def test_negative_downtime_is_listed_as_contributing_nothing(self):
        """Discarded from the sum, not from the evidence -- it is a data error."""

        details = [detail("3101", datetime(2026, 1, 8, 7, 0), -4.0)]
        found = self.contributions("3101", details=details, linked_rules=[])
        self.assertEqual(found[0].order.downtime_hours, -4.0)
        self.assertEqual(found[0].counted_hours, 0.0)

    def test_other_months_are_excluded(self):
        details = january_details() + [detail("3101", datetime(2026, 2, 3, 9, 0), 99.0)]
        found = self.contributions("3101", details=details, linked_rules=[])
        self.assertEqual([c.order.downtime_hours for c in found], [10.0])

    def test_a_rule_with_no_impact_contributes_no_rows(self):
        found = self.contributions("3102", linked_rules=[LinkedRule("3102", "3101", 0.0)])
        self.assertEqual([c.source for c in found], ["direct"])

    def test_an_asset_linked_to_itself_is_listed_twice(self):
        """Because the calculator charges it twice; §2.4 caps and merges nothing.

        Collapsing the pair here would show a bar of 15 h explained by rows
        summing to 10 h, which is the one thing this view may not do.
        """

        rules = [LinkedRule("3101", "3101", 0.5)]
        row = next(
            r for r in compute_rows([SALVAGNINI], [JAN], january_work_orders(), linked_rules=rules)
            if r.asset_number == "3101"
        )
        found = self.contributions("3101", linked_rules=rules)
        self.assertEqual([c.source for c in found], ["direct", "linked"])
        self.assertAlmostEqual(
            sum(c.counted_hours for c in found), row.adjusted_downtime_hours, places=12
        )

    def test_the_crossing_rows_count_matches_the_overlap_column(self):
        details = [
            detail("3101", datetime(2026, 1, 31, 20, 0), 10.0),  # runs into February
            detail("3101", datetime(2026, 1, 8, 7, 0), 2.0),
        ]
        row = compute_rows(
            [SALVAGNINI], [JAN], [d.order for d in details], linked_rules=[]
        )[0]
        found = self.contributions("3101", details=details, linked_rules=[])
        self.assertEqual(sum(1 for c in found if c.crosses_month), row.overlap_count)
        self.assertEqual(row.overlap_count, 1)

    def test_the_descriptive_text_never_reaches_the_calculator(self):
        """Design doc §2.1, kept structural rather than remembered.

        A ``WorkOrderDetail`` carries ``type_raw`` and ``record_class`` because
        the drill-down displays them. It is a different type from the one
        ``compute_rows`` consumes precisely so that no future change can filter
        on either: passing one in does not quietly work, it fails.
        """

        self.assertIn("type_raw", WorkOrderDetail.__dataclass_fields__)
        with self.assertRaises(AttributeError):
            compute_rows([SALVAGNINI], [JAN], january_details())


if __name__ == "__main__":
    unittest.main()
