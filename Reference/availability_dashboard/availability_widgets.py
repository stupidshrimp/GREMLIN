"""PyQt widgets for the GREMLIN Availability Dashboard page."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import mean, median

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .availability_calculator import AssetGroup, AvailabilityCalculator, LinkedDowntimeRule, month_range_for_year
from .availability_charts import AvailabilityChartWidget, pct
from .availability_repository import AvailabilityRepository


class AvailabilityDashboardPage(QWidget):
    """Full Availability Dashboard module with dashboard, configuration, and summary tabs."""

    def __init__(self, repository: AvailabilityRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.settings = self.repository.load_settings()
        self.tabs = QTabWidget()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.tabs)
        self._build_dashboard_tab()
        self._build_configurator_tab()
        self._build_display_names_tab()
        self._build_linked_rules_tab()
        self._build_manual_goals_tab()
        self._build_results_tab()
        self.refresh_all()

    def _build_dashboard_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        self.year_spin = QSpinBox()
        self.year_spin.setRange(2000, 2100)
        self.year_spin.setValue(int(self.settings["selected_year"]))
        self.utc_offset_spin = QDoubleSpinBox()
        self.utc_offset_spin.setRange(-24, 24)
        self.utc_offset_spin.setDecimals(2)
        self.utc_offset_spin.setValue(float(self.settings["utc_offset_hours"]))
        self.group_filter = QComboBox()
        recalc = QPushButton("Recalculate")
        recalc.setObjectName("primaryButton")
        recalc.clicked.connect(self.recalculate)
        self.last_updated_label = QLabel("Last updated: —")
        controls.addWidget(QLabel("Selected year"))
        controls.addWidget(self.year_spin)
        controls.addWidget(QLabel("UTC offset hours"))
        controls.addWidget(self.utc_offset_spin)
        controls.addWidget(QLabel("Show"))
        controls.addWidget(self.group_filter)
        controls.addWidget(recalc)
        controls.addStretch(1)
        controls.addWidget(self.last_updated_label)
        layout.addLayout(controls)
        self.metric_grid = QGridLayout()
        layout.addLayout(self.metric_grid)
        self.chart_container = QWidget()
        self.chart_layout = QVBoxLayout(self.chart_container)
        self.chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_scroll = QScrollArea()
        chart_scroll.setWidgetResizable(True)
        chart_scroll.setWidget(self.chart_container)
        layout.addWidget(chart_scroll, stretch=1)
        self.group_filter.currentTextChanged.connect(self.refresh_dashboard)
        self.tabs.addTab(tab, "Dashboard")

    def _build_configurator_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.config_table = QTableWidget(0, 9)
        self.config_table.setHorizontalHeaderLabels(("Asset Group", "Asset Numbers", "Schedule Hours/Day", "Break Hours/Day", "Lunch Hours/Day", "Setup Hours/Day", "Net Scheduled Hours/Day", "Include?", "Notes"))
        layout.addWidget(self.config_table)
        buttons = QHBoxLayout()
        add = QPushButton("Add Group")
        remove = QPushButton("Remove Selected")
        save = QPushButton("Save Configuration")
        save.setObjectName("primaryButton")
        add.clicked.connect(lambda: self.config_table.insertRow(self.config_table.rowCount()))
        remove.clicked.connect(lambda: self._remove_selected_rows(self.config_table))
        save.clicked.connect(self.save_configurator)
        buttons.addWidget(add); buttons.addWidget(remove); buttons.addWidget(save); buttons.addStretch(1)
        layout.addLayout(buttons)
        self.tabs.addTab(tab, "Configurator")

    def _build_display_names_tab(self) -> None:
        tab = QWidget(); layout = QVBoxLayout(tab)
        self.display_table = QTableWidget(0, 2)
        self.display_table.setHorizontalHeaderLabels(("Asset Number", "Display Name"))
        layout.addWidget(self.display_table)
        buttons = QHBoxLayout(); add = QPushButton("Add Display Name"); remove = QPushButton("Remove Selected"); save = QPushButton("Save Display Names"); save.setObjectName("primaryButton")
        add.clicked.connect(lambda: self.display_table.insertRow(self.display_table.rowCount()))
        remove.clicked.connect(lambda: self._remove_selected_rows(self.display_table))
        save.clicked.connect(self.save_display_names)
        buttons.addWidget(add); buttons.addWidget(remove); buttons.addWidget(save); buttons.addStretch(1); layout.addLayout(buttons)
        self.tabs.addTab(tab, "Asset Display Names")

    def _build_linked_rules_tab(self) -> None:
        tab = QWidget(); layout = QVBoxLayout(tab)
        self.rules_table = QTableWidget(0, 4)
        self.rules_table.setHorizontalHeaderLabels(("Rule Group", "Parent Asset Number", "Linked Asset Number", "Impact Factor"))
        layout.addWidget(self.rules_table)
        buttons = QHBoxLayout(); add = QPushButton("Add Rule"); remove = QPushButton("Remove Selected"); save = QPushButton("Save Rules"); save.setObjectName("primaryButton")
        add.clicked.connect(lambda: self.rules_table.insertRow(self.rules_table.rowCount()))
        remove.clicked.connect(lambda: self._remove_selected_rows(self.rules_table))
        save.clicked.connect(self.save_linked_rules)
        buttons.addWidget(add); buttons.addWidget(remove); buttons.addWidget(save); buttons.addStretch(1); layout.addLayout(buttons)
        self.tabs.addTab(tab, "Linked Downtime Rules")

    def _build_manual_goals_tab(self) -> None:
        tab = QWidget(); layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("Manual OT is entered per asset/month. Goal percentages are entered per asset group/month; values above 1 are saved as percentages (95 = 0.95)."))
        self.ot_table = QTableWidget(0, 4); self.ot_table.setHorizontalHeaderLabels(("Asset Group", "Asset Number", "Month Date", "Manual OT Hours"))
        self.goal_table = QTableWidget(0, 3); self.goal_table.setHorizontalHeaderLabels(("Asset Group", "Month Date", "Goal Percent"))
        layout.addWidget(QLabel("Manual OT")); layout.addWidget(self.ot_table)
        layout.addWidget(QLabel("Goals")); layout.addWidget(self.goal_table)
        buttons = QHBoxLayout(); save = QPushButton("Save Manual OT / Goals"); save.setObjectName("primaryButton"); seed = QPushButton("Load editable rows for year")
        save.clicked.connect(self.save_manual_goals); seed.clicked.connect(self.seed_manual_goal_rows)
        buttons.addWidget(seed); buttons.addWidget(save); buttons.addStretch(1); layout.addLayout(buttons)
        self.tabs.addTab(tab, "Manual OT / Goals")

    def _build_results_tab(self) -> None:
        tab = QWidget(); layout = QVBoxLayout(tab)
        self.summary_table = QTableWidget(0, 10)
        self.summary_table.setHorizontalHeaderLabels(("Month", "Total WO Count", "Zero Downtime WO Count", "Zero Downtime %", "Asset-Month Rows", "No WO Entry Rows", "Median Availability", "Average Availability", "Lowest Availability", "Total Downtime Hours"))
        self.results_table = QTableWidget(0, 12)
        self.results_table.setHorizontalHeaderLabels(("Group", "Asset", "Month", "Scheduled", "Downtime", "Raw Availability", "Manual OT", "Linked Downtime", "Adjusted Downtime", "Adjusted Availability", "WO Count", "Note"))
        layout.addWidget(QLabel("Monthly Summary")); layout.addWidget(self.summary_table)
        layout.addWidget(QLabel("Result Rows")); layout.addWidget(self.results_table)
        self.tabs.addTab(tab, "Results / Summary Table")

    def refresh_all(self) -> None:
        self.settings = self.repository.load_settings()
        last = self.settings.get("last_updated") or "—"
        self.last_updated_label.setText(f"Last updated: {last}")
        self._load_config_table()
        self._load_display_table()
        self._load_rules_table()
        self.seed_manual_goal_rows()
        self.refresh_dashboard()
        self.refresh_results()

    def recalculate(self) -> None:
        try:
            year = int(self.year_spin.value())
            offset = float(self.utc_offset_spin.value())
            self.repository.save_settings(year, offset)
            results = AvailabilityCalculator(self.repository).calculate_availability(year, offset)
            self.repository.save_results(year, results)
            self.refresh_all()
            QMessageBox.information(self, "Availability recalculated", f"Saved {len(results)} availability result row(s).")
        except Exception as exc:
            QMessageBox.critical(self, "Availability calculation failed", str(exc))

    def refresh_dashboard(self) -> None:
        self._clear_layout(self.chart_layout)
        self._clear_layout(self.metric_grid)
        year = int(self.year_spin.value())
        rows = [dict(row) for row in self.repository.load_results(year)]
        groups = sorted({row["asset_group"] for row in rows})
        current = self.group_filter.currentText() or "All groups"
        self.group_filter.blockSignals(True)
        self.group_filter.clear(); self.group_filter.addItem("All groups"); self.group_filter.addItems(groups)
        self.group_filter.setCurrentText(current if current in groups else "All groups")
        self.group_filter.blockSignals(False)
        filtered = rows if self.group_filter.currentText() == "All groups" else [row for row in rows if row["asset_group"] == self.group_filter.currentText()]
        metrics = self._overall_metrics(filtered)
        for i, (name, value) in enumerate(metrics):
            self.metric_grid.addWidget(QLabel(f"<b>{name}</b><br>{value}"), i // 4, i % 4)
        goals = self.repository.load_goal_percent(year)
        for group in groups:
            if self.group_filter.currentText() != "All groups" and self.group_filter.currentText() != group:
                continue
            group_rows = [row for row in rows if row["asset_group"] == group]
            self.chart_layout.addWidget(AvailabilityChartWidget(group, group_rows, goals))
        self.chart_layout.addStretch(1)

    def refresh_results(self) -> None:
        rows = [dict(row) for row in self.repository.load_results(int(self.year_spin.value()))]
        self._populate_results_table(rows)
        self._populate_summary_table(rows)

    def save_configurator(self) -> None:
        try:
            groups = []
            for r in range(self.config_table.rowCount()):
                name = self._text(self.config_table, r, 0)
                if not name:
                    continue
                schedule, brk, lunch, setup = [self._float(self.config_table, r, c) for c in (2, 3, 4, 5)]
                assets = [part.strip() for part in self._text(self.config_table, r, 1).split(",") if part.strip()]
                groups.append(AssetGroup(None, name, assets, schedule, brk, lunch, setup, max(0, schedule - brk - lunch - setup), self._text(self.config_table, r, 7).lower() in {"yes", "true", "1", "y"}, self._text(self.config_table, r, 8), r + 1))
            self.repository.save_asset_groups(groups); self.refresh_all()
        except Exception as exc:
            QMessageBox.critical(self, "Configuration save failed", str(exc))

    def save_display_names(self) -> None:
        names = {self._text(self.display_table, r, 0): self._text(self.display_table, r, 1) for r in range(self.display_table.rowCount()) if self._text(self.display_table, r, 0)}
        self.repository.save_display_names(names); self.refresh_all()

    def save_linked_rules(self) -> None:
        try:
            rules = [LinkedDowntimeRule(self._text(self.rules_table, r, 0), self._text(self.rules_table, r, 1), self._text(self.rules_table, r, 2), self._float(self.rules_table, r, 3)) for r in range(self.rules_table.rowCount()) if self._text(self.rules_table, r, 0)]
            self.repository.save_linked_rules(rules); self.refresh_all()
        except Exception as exc:
            QMessageBox.critical(self, "Rule save failed", str(exc))

    def save_manual_goals(self) -> None:
        try:
            year = int(self.year_spin.value())
            ot_entries = [
                (self._text(self.ot_table, r, 0), self._text(self.ot_table, r, 1), self._text(self.ot_table, r, 2), self._float(self.ot_table, r, 3))
                for r in range(self.ot_table.rowCount())
                if self._text(self.ot_table, r, 0) and self._text(self.ot_table, r, 1) and self._text(self.ot_table, r, 2) and self._text(self.ot_table, r, 3)
            ]
            goal_entries = [
                (self._text(self.goal_table, r, 0), self._text(self.goal_table, r, 1), self._float(self.goal_table, r, 2))
                for r in range(self.goal_table.rowCount())
                if self._text(self.goal_table, r, 0) and self._text(self.goal_table, r, 1) and self._text(self.goal_table, r, 2)
            ]
            self.repository.replace_manual_ot_entries_for_year(year, ot_entries)
            self.repository.replace_goal_entries_for_year(year, goal_entries)
            self.refresh_all()
        except Exception as exc:
            QMessageBox.critical(self, "Manual OT / goals save failed", str(exc))

    def seed_manual_goal_rows(self) -> None:
        year = int(self.year_spin.value())
        months = month_range_for_year(year) or [date(year, m, 1) for m in range(1, 13)]
        groups = self.repository.load_asset_groups(include_only=True)
        ot_existing = self.repository.load_manual_ot(year); goals_existing = self.repository.load_goal_percent(year)
        ot_rows = []
        for group in groups:
            for asset in group.asset_numbers:
                for month in months:
                    ot_rows.append((group.asset_group, asset, month.isoformat(), ot_existing.get((group.asset_group, asset, month.isoformat()), 0.0)))
        goal_rows = [(group.asset_group, month.isoformat(), goals_existing.get((group.asset_group, month.isoformat()), 0.95)) for group in groups for month in months]
        self._set_rows(self.ot_table, ot_rows); self._set_rows(self.goal_table, goal_rows)

    def _load_config_table(self) -> None:
        rows = [(g.asset_group, ",".join(g.asset_numbers), g.schedule_hours_per_day, g.break_hours_per_day, g.lunch_hours_per_day, g.setup_hours_per_day, max(0, g.schedule_hours_per_day - g.break_hours_per_day - g.lunch_hours_per_day - g.setup_hours_per_day), "Yes" if g.include_flag else "No", g.notes) for g in self.repository.load_asset_groups(False)]
        self._set_rows(self.config_table, rows)

    def _load_display_table(self) -> None:
        self._set_rows(self.display_table, sorted(self.repository.load_display_names().items()))

    def _load_rules_table(self) -> None:
        self._set_rows(self.rules_table, [(r.rule_group, r.parent_asset_number, r.linked_asset_number, r.impact_factor) for r in self.repository.load_linked_rules()])

    def _populate_results_table(self, rows: list[dict]) -> None:
        values = [(r["asset_group"], r["asset_display_name"] or r["asset_number"], r["month_label"], f'{r["scheduled_hours"]:.1f}', f'{r["downtime_hours"]:.1f}', pct(r["availability_percent"]), f'{r["manual_ot_hours"]:.1f}', f'{r["linked_downtime_hours"]:.1f}', f'{r["adjusted_downtime_hours"]:.1f}', pct(r["adjusted_availability_percent"]), r["total_wo_count"], r["zero_no_entry_note"] or "") for r in rows]
        self._set_rows(self.results_table, values)

    def _populate_summary_table(self, rows: list[dict]) -> None:
        by_month = defaultdict(list)
        for row in rows: by_month[row["month_date"]].append(row)
        values = []
        for month, month_rows in sorted(by_month.items()):
            avails = [r["adjusted_availability_percent"] for r in month_rows]
            total = sum(r["total_wo_count"] for r in month_rows); zero = sum(r["zero_downtime_wo_count"] for r in month_rows)
            values.append((month, total, zero, pct(zero / total if total else 0), len(month_rows), sum(r["no_wo_entries_flag"] for r in month_rows), pct(median(avails) if avails else 0), pct(mean(avails) if avails else 0), pct(min(avails) if avails else 0), f'{sum(r["adjusted_downtime_hours"] for r in month_rows):.1f}'))
        self._set_rows(self.summary_table, values)

    def _overall_metrics(self, rows: list[dict]) -> list[tuple[str, str]]:
        avails = [r["adjusted_availability_percent"] for r in rows]
        total = sum(r["total_wo_count"] for r in rows); zero = sum(r["zero_downtime_wo_count"] for r in rows)
        return [("Total Work Orders Examined", str(total)), ("Work Orders with 0 Downtime", str(zero)), ("% of WOs with 0 Downtime", pct(zero / total if total else 0)), ("Asset-Months with No WO Entries", str(sum(r["no_wo_entries_flag"] for r in rows))), ("Total Adjusted Downtime Hours", f'{sum(r["adjusted_downtime_hours"] for r in rows):.1f}'), ("Overall Median Availability", pct(median(avails) if avails else 0)), ("Lowest Availability Shown", pct(min(avails) if avails else 0))]

    @staticmethod
    def _set_rows(table: QTableWidget, rows) -> None:
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(str(value)))
        table.resizeColumnsToContents()

    @staticmethod
    def _text(table: QTableWidget, row: int, col: int) -> str:
        item = table.item(row, col)
        return item.text().strip() if item else ""

    @classmethod
    def _float(cls, table: QTableWidget, row: int, col: int) -> float:
        text = cls._text(table, row, col)
        return float(text) if text else 0.0

    @staticmethod
    def _remove_selected_rows(table: QTableWidget) -> None:
        for row in sorted({index.row() for index in table.selectedIndexes()}, reverse=True):
            table.removeRow(row)

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
