"""Native PyQt6 desktop GUI for GREMLIN.

GREMLIN stands for Graphical Reliability Engineering, Maintenance,
Life-Data INterface. This module implements a native Qt workspace focused only
on GREMLIN reliability engineering workflows.
"""

from __future__ import annotations

import matplotlib
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt
import seaborn as sns
import difflib
import math
import sqlite3
import sys
from matplotlib.figure import Figure
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
import pandas as pd

from PyQt6.QtCore import QAbstractTableModel, QEasingCurve, QObject, QPoint, QPropertyAnimation, QRect, QSignalBlocker, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableView,
    QHeaderView,
    QTableWidgetItem,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget, QListWidgetItem, QListWidget,
)

from repositories.analysis_repo import AnalysisRepository
from repositories.failure_repo import FailureRepository
from repositories.metrics_repo import MetricsRepository
from services.life_data_service import (
    DISPLAY_COLUMNS,
    database_lock_wait_callback,
    PM_DISPOSITION_CATEGORIES,
    PM_RESET_DECISIONS,
    RECORD_CLASSES,
    WO_DISPOSITION_CATEGORIES,
    AnalysisResultView,
    DatabaseWriteError,
    LifeDataService,
)
from services.reliability_service import ReliabilityService
from availability_dashboard import AvailabilityCalculator, AvailabilityRepository
from availability_dashboard.availability_charts import pct as availability_pct

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

@dataclass(frozen=True)
class Palette:
    """Green GREMLIN reliability workspace colors."""

    canvas: str = "#07130c"
    surface: str = "#0d1f14"
    surface_alt: str = "#102718"
    text: str = "#ecfdf5"
    muted: str = "#9bd8b4"
    border: str = "#1f5f3a"
    border_soft: str = "#18482d"
    accent: str = "#22c55e"
    accent_dark: str = "#16a34a"
    accent_deep: str = "#052e16"
    warning: str = "#facc15"
    danger: str = "#fb7185"
    hover: str = "#12351f"
    input: str = "#06150d"
    sidebar: str = "#06150d"
    selected: str = "#14532d"


COLORS = Palette()
ROOT_DIR = Path(__file__).resolve().parent
IMG_DIR = ROOT_DIR / "static" / "img"
APP_ICON_PATH = IMG_DIR / "GREMLINLOGO.png"
TOPBAR_LOGO_PATH = IMG_DIR / "logo1.jpg"

BASE_QSS = f"""
QMainWindow {{
    background: {COLORS.canvas};
}}
QWidget {{
    color: {COLORS.text};
    font-family: Arial, Helvetica, sans-serif;
    font-size: 14px;
}}
QScrollArea {{
    border: 0;
    background: {COLORS.canvas};
}}
QLabel#mutedLabel {{
    color: {COLORS.muted};
}}
QLabel#linkLabel {{
    color: {COLORS.accent};
}}
QLabel#pillLabel {{
    border: 1px solid {COLORS.border};
    border-radius: 12px;
    padding: 2px 8px;
    color: {COLORS.muted};
    font-size: 12px;
}}
QPushButton {{
    background: {COLORS.surface};
    border: 1px solid {COLORS.border};
    border-radius: 6px;
    padding: 7px 12px;
    color: {COLORS.text};
    text-align: left;
}}
QPushButton:hover {{
    background: {COLORS.hover};
}}
QPushButton#primaryButton {{
    background: {COLORS.accent};
    border-color: {COLORS.accent};
    color: white;
    font-weight: 700;
}}
QPushButton#primaryButton:hover {{
    background: {COLORS.accent_deep};
    color: {COLORS.accent};
}}
QPushButton#navButton {{
    border: 0;
    border-radius: 8px;
    padding: 10px 12px;
    background: transparent;
}}
QPushButton#navButton:checked {{
    background: {COLORS.selected};
    color: {COLORS.text};
    font-weight: 700;
}}
QPushButton#workflowCard {{
    background: {COLORS.surface};
    border: 1px solid {COLORS.border};
    border-radius: 10px;
    color: {COLORS.text};
    padding: 18px;
    text-align: left;
}}
QPushButton#workflowCard:hover {{
    background: {COLORS.hover};
    border: 1px solid {COLORS.accent};
}}
QPushButton#workflowCard:pressed {{
    background: {COLORS.selected};
}}
QFrame#searchBox {{
    background: {COLORS.input};
    border: 1px solid {COLORS.border};
    border-radius: 6px;
}}
QFrame#card {{
    background: {COLORS.surface};
    border: 1px solid {COLORS.border};
    border-radius: 10px;
}}
QFrame#topHeader {{
    background: {COLORS.surface_alt};
    border-bottom: 1px solid {COLORS.border};
}}
QLabel#brandMark {{
    background: {COLORS.surface};
    border: 1px solid {COLORS.border};
    border-radius: 10px;
    padding: 2px;
}}
QFrame#sidebar {{
    background: {COLORS.sidebar};
    border-right: 1px solid {COLORS.border};
}}
QFrame#rule {{
    background: {COLORS.border};
}}
QTableWidget {{
    background: {COLORS.surface};
    border: 1px solid {COLORS.border};
    border-radius: 8px;
    gridline-color: {COLORS.border_soft};
}}
QHeaderView::section {{
    background: {COLORS.canvas};
    border: 0;
    border-bottom: 1px solid {COLORS.border};
    padding: 8px;
    font-weight: 700;
}}
"""


def configure_windows_app_identity() -> None:
    """Set a stable Windows app identity so the taskbar uses the GREMLIN icon."""

    if sys.platform == "win32":
        from ctypes import windll

        windll.shell32.SetCurrentProcessExplicitAppUserModelID("GREMLIN.Desktop.GUI")


def label(
    text: str,
    *,
    size: int = 14,
    weight: QFont.Weight = QFont.Weight.Normal,
    color_role: str | None = None,
    wrap: bool = False,
    alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
) -> QLabel:
    """Create a consistently styled label."""

    widget = QLabel(text)
    widget.setFont(QFont("Arial", size, weight))
    widget.setWordWrap(wrap)
    widget.setAlignment(alignment)
    if color_role is not None:
        widget.setObjectName(color_role)
    return widget


def make_button(text: str, *, primary: bool = False) -> QPushButton:
    """Create a GREMLIN-styled push button."""

    button = AnimatedPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumHeight(34)
    if primary:
        button.setObjectName("primaryButton")
    return button


def make_workflow_card(title: str, description: str) -> QPushButton:
    """Create a compact clickable workflow card for the life-data landing page."""

    card = AnimatedPushButton(f"{title}\n\n{description}")
    card.setObjectName("workflowCard")
    card.setCursor(Qt.CursorShape.PointingHandCursor)
    card.setMinimumHeight(135)
    card.setFont(QFont("Arial", 14, QFont.Weight.DemiBold))
    return card


def hline() -> QFrame:
    """Return a one-pixel horizontal separator."""

    line = QFrame()
    line.setObjectName("rule")
    line.setFixedHeight(1)
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


class AnimatedPushButton(QPushButton):
    """Push button with stable styling for scrollable Qt pages."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().mouseReleaseEvent(event)


class FitWidthTableWidget(QTableWidget):
    """Table widget that allocates columns inside its viewport to avoid horizontal scrolling."""

    def __init__(self, column_ratios: tuple[int, ...], *args) -> None:
        super().__init__(*args)
        self.column_ratios = column_ratios
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().resizeEvent(event)
        self._fit_columns_to_viewport()

    def _fit_columns_to_viewport(self) -> None:
        if self.columnCount() == 0 or not self.column_ratios:
            return
        available_width = max(1, self.viewport().width() - 2)
        total_ratio = sum(self.column_ratios)
        used_width = 0
        last_column = min(self.columnCount(), len(self.column_ratios)) - 1
        for column, ratio in enumerate(self.column_ratios[: self.columnCount()]):
            if column == last_column:
                width = max(1, available_width - used_width)
            else:
                width = max(1, int(available_width * ratio / total_ratio))
                used_width += width
            self.setColumnWidth(column, width)


class WorkerSignals(QObject):
    """Signals emitted by a background worker action."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(object)
    database_lock_waiting = pyqtSignal()


class BackgroundWorker(QObject):
    """Run a long callable away from the Qt GUI thread."""

    def __init__(self, action: Callable[[], object]) -> None:
        super().__init__()
        self.action = action
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            with database_lock_wait_callback(self.signals.database_lock_waiting.emit):
                self.signals.finished.emit(self.action())
        except Exception as exc:  # noqa: BLE001 - marshal user-facing errors back to GUI thread
            self.signals.failed.emit(exc)


class ReadOnlyTableModel(QAbstractTableModel):
    """Lightweight model for large read-only result tables."""

    def __init__(self, headers: tuple[str, ...], rows: list[tuple[object, ...]], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.headers = headers
        self.rows = rows

    def rowCount(self, parent=None) -> int:  # noqa: N802 - Qt override name
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None) -> int:  # noqa: N802 - Qt override name
        return 0 if parent is not None and parent.isValid() else len(self.headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None
        value = self.rows[index.row()][index.column()]
        return "" if value is None else str(value)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802 - Qt override name
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return str(section + 1)


def read_only_table_view(headers: tuple[str, ...], rows: list[tuple[object, ...]], *, min_height: int, column_widths: tuple[int, ...] | None = None) -> QTableView:
    """Create a QTableView backed by a model instead of thousands of cell widgets."""

    table = QTableView()
    model = ReadOnlyTableModel(headers, rows, table)
    table.setModel(model)
    table.setSortingEnabled(True)
    table.setAlternatingRowColors(True)
    table.setMinimumHeight(min_height)
    table.verticalHeader().setDefaultSectionSize(28)
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    if column_widths:
        for column, width in enumerate(column_widths[: len(headers)]):
            table.setColumnWidth(column, width)
    return table


class WeibullGraphWidget(QWidget):
    """Lightweight Qt renderer for required Weibull result curves and KM points."""

    def __init__(self, result: AnalysisResultView, point_selected_callback: Callable[[int], None] | None = None) -> None:
        super().__init__()
        self.result = result
        self.beta = result.beta_mle
        self.eta = result.eta_mle
        self.point_selected_callback = point_selected_callback
        self._point_targets: list[dict[str, object]] = []
        self._zoom_factor = 1.0
        self._pan_x_ratio = 0.0
        self._pan_y_ratio = 0.0
        self._drag_origin: QPoint | None = None
        self._drag_start_pan: tuple[float, float] = (0.0, 0.0)
        self._is_dragging = False
        self.setMouseTracking(True)
        self.setToolTip("Scroll over the graph to zoom. Hold the left mouse button and drag to pan. Click a point to jump to its Weibull data row.")

    def set_parameters(self, beta: float, eta: float) -> None:
        """Update plotted Weibull parameters without replacing empirical points."""

        self.beta = max(0.01, float(beta))
        self.eta = max(0.01, float(eta))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().paintEvent(event)
        self._point_targets = []
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        panes = [
            ("Weibull probability plot", "weibull"),
            ("Weibull CDF", "cdf"),
            ("Hazard function", "hazard"),
            ("Probability density (PDF)", "pdf"),
        ]
        margin = 18
        gap = 14
        pane_width = max(1, (self.width() - 2 * margin - gap) // 2)
        pane_height = max(1, (self.height() - 2 * margin - gap) // 2)
        for index, (title, mode) in enumerate(panes):
            row = index // 2
            column = index % 2
            left = margin + column * (pane_width + gap)
            top = margin + row * (pane_height + gap)
            self._draw_pane(painter, left, top, pane_width, pane_height, title, mode)
        painter.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override name
        current = event.position().toPoint()
        if self._drag_origin is not None:
            delta = current - self._drag_origin
            if abs(delta.x()) > 2 or abs(delta.y()) > 2:
                self._is_dragging = True
            if self._is_dragging:
                width = max(1, self.width())
                height = max(1, self.height())
                start_x, start_y = self._drag_start_pan
                self._pan_x_ratio = max(-0.5, min(0.5, start_x - delta.x() / width))
                self._pan_y_ratio = max(-0.5, min(0.5, start_y + delta.y() / height))
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                event.accept()
                return
        self.setCursor(Qt.CursorShape.PointingHandCursor if self._target_at(current) else Qt.CursorShape.OpenHandCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override name
        self.unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override name
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position().toPoint()
            self._drag_start_pan = (self._pan_x_ratio, self._pan_y_ratio)
            self._is_dragging = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override name
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            if not self._is_dragging:
                target = self._target_at(event.position().toPoint())
                if target and self.point_selected_callback:
                    self.point_selected_callback(int(target["observation_id"]))
            self._drag_origin = None
            self._is_dragging = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override name
        if event.angleDelta().y() > 0:
            self._zoom_factor = min(6.0, self._zoom_factor * 1.2)
        else:
            self._zoom_factor = max(1.0, self._zoom_factor / 1.2)
            if self._zoom_factor <= 1.01:
                self._pan_x_ratio = 0.0
                self._pan_y_ratio = 0.0
        self.update()
        event.accept()

    def _draw_pane(self, painter: QPainter, left: int, top: int, width: int, height: int, title: str, mode: str) -> None:
        painter.setPen(QPen(Qt.GlobalColor.darkGreen, 1))
        painter.drawRoundedRect(left, top, width, height, 8, 8)
        title_font = QFont("Arial", 10, QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QPen(Qt.GlobalColor.white, 1))
        painter.drawText(left + 12, top + 22, title)
        plot_left = left + 94
        plot_top = top + 46
        plot_width = max(40, width - 134)
        plot_height = max(40, height - 132)
        painter.setPen(QPen(Qt.GlobalColor.darkGray, 1))
        painter.drawLine(plot_left, plot_top + plot_height, plot_left + plot_width, plot_top + plot_height)
        painter.drawLine(plot_left, plot_top, plot_left, plot_top + plot_height)
        self._draw_axis_labels(painter, plot_left, plot_top, plot_width, plot_height, mode)
        current_censors = self._current_censor_points(mode)
        if mode == "weibull":
            km = [p for p in self.result.km_points if p.get("weibull_plot_y") is not None]
            if not km:
                return
            censored_weibull = self._historical_censored_points("weibull")
            xs = [float(p["weibull_plot_x"]) for p in km] + [x for x, _, _ in censored_weibull] + [x for x, _, _ in current_censors]
            ys = [float(p["weibull_plot_y"]) for p in km] + [y for _, y, _ in censored_weibull] + [y for _, y, _ in current_censors]
            min_x, max_x = min(xs), max(xs)
            if math.isclose(min_x, max_x):
                min_x -= 0.5
                max_x += 0.5
            curve_xs = [min_x + (max_x - min_x) * index / 79 for index in range(80)]
            curve = [(x, self.beta * x - self.beta * math.log(self.eta)) for x in curve_xs]
            ci_curves = self._weibull_ci_curves(curve_xs)
            x_domain = self._zoomed_domain(curve_xs + xs)
            y_domain = self._zoomed_domain(ys + [y for _, y in curve] + [y for ci_curve in ci_curves for _, y in ci_curve], pan_ratio=self._pan_y_ratio)
            self._draw_axis_values(painter, x_domain, y_domain, plot_left, plot_top, plot_width, plot_height)
            for ci_curve in ci_curves:
                self._draw_polyline(painter, ci_curve, x_domain, y_domain, plot_left, plot_top, plot_width, plot_height, Qt.GlobalColor.yellow)
            self._draw_polyline(painter, curve, x_domain, y_domain, plot_left, plot_top, plot_width, plot_height, Qt.GlobalColor.green)
            self._draw_points(painter, [(float(p["weibull_plot_x"]), float(p["weibull_plot_y"]), self._failure_observation_for_point(p)) for p in km], x_domain, y_domain, plot_left, plot_top, plot_width, plot_height, mode, Qt.GlobalColor.white)
            self._draw_points(painter, censored_weibull, x_domain, y_domain, plot_left, plot_top, plot_width, plot_height, mode, Qt.GlobalColor.red)
            self._draw_vertical_markers(painter, current_censors, x_domain, y_domain, plot_left, plot_top, plot_width, plot_height, Qt.GlobalColor.red)
        else:
            curve = self._parameter_curve(mode)
            if not curve:
                return
            ci_curves = [] if mode in {"pdf", "hazard"} else self._parameter_ci_curves(mode)
            xs = self._zoomed_domain([x for x, _ in curve] + [x for ci_curve in ci_curves for x, _ in ci_curve] + [x for x, _, _ in current_censors])
            ys = [y for _, y in curve] + [y for ci_curve in ci_curves for _, y in ci_curve] + [y for _, y, _ in current_censors]
            y_domain = self._zoomed_domain(ys, pan_ratio=self._pan_y_ratio) if mode in {"pdf", "hazard"} else [0.0, 1.0]
            self._draw_axis_values(painter, xs, y_domain, plot_left, plot_top, plot_width, plot_height)
            for ci_curve in ci_curves:
                self._draw_polyline(painter, ci_curve, xs, y_domain, plot_left, plot_top, plot_width, plot_height, Qt.GlobalColor.yellow)
            self._draw_polyline(painter, curve, xs, y_domain, plot_left, plot_top, plot_width, plot_height, Qt.GlobalColor.green)
            self._draw_vertical_markers(painter, current_censors, xs, y_domain, plot_left, plot_top, plot_width, plot_height, Qt.GlobalColor.red)
            if mode in {"pdf", "hazard"}:
                return
            km_points = [(float(p["life_hours"]), float(p["cdf_estimate"]), self._failure_observation_for_point(p)) for p in self.result.km_points]
            self._draw_points(painter, km_points, xs, y_domain, plot_left, plot_top, plot_width, plot_height, mode, Qt.GlobalColor.white)
            self._draw_points(painter, self._historical_censored_points(mode), xs, y_domain, plot_left, plot_top, plot_width, plot_height, mode, Qt.GlobalColor.red)

    def _draw_axis_labels(self, painter: QPainter, left: int, top: int, width: int, height: int, mode: str) -> None:
        labels = {
            "weibull": ("ln(life hours)", "ln(-ln(1-F(t)))"),
            "cdf": ("Life hours", "CDF"),
            "hazard": ("Life hours", "Hazard rate"),
            "pdf": ("Life hours", "PDF"),
        }
        x_label, y_label = labels[mode]
        axis_font = QFont("Arial", 10, QFont.Weight.Bold)
        painter.setFont(axis_font)
        painter.setPen(QPen(Qt.GlobalColor.lightGray, 1))
        metrics = QFontMetrics(axis_font)
        x_width = metrics.horizontalAdvance(x_label)
        painter.drawText(left + max(0, (width - x_width) // 2), top + height + 54, x_label)
        painter.save()
        painter.translate(left - 76, top + height // 2 + metrics.horizontalAdvance(y_label) // 2)
        painter.rotate(-90)
        painter.drawText(0, 0, y_label)
        painter.restore()

    def _draw_axis_values(self, painter: QPainter, xs: list[float], ys: list[float], left: int, top: int, width: int, height: int) -> None:
        if not xs or not ys:
            return
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        if math.isclose(min_x, max_x):
            max_x = min_x + 1
        if math.isclose(min_y, max_y):
            max_y = min_y + 1
        tick_font = QFont("Arial", 8, QFont.Weight.Normal)
        painter.setFont(tick_font)
        painter.setPen(QPen(Qt.GlobalColor.lightGray, 1))
        tick_length = 4
        for ratio in (0.0, 0.5, 1.0):
            x = left + int(width * ratio)
            value = min_x + (max_x - min_x) * ratio
            text = self._format_axis_value(value)
            text_width = QFontMetrics(tick_font).horizontalAdvance(text)
            painter.drawLine(x, top + height, x, top + height + tick_length)
            painter.drawText(x - text_width // 2, top + height + 20, text)
        for ratio in (0.0, 0.5, 1.0):
            y = top + height - int(height * ratio)
            value = min_y + (max_y - min_y) * ratio
            painter.drawLine(left - tick_length, y, left, y)
            painter.drawText(left - 68, y + 4, self._format_axis_value(value))

    def _format_axis_value(self, value: float) -> str:
        abs_value = abs(value)
        if abs_value >= 100000 or (0 < abs_value < 0.01):
            return f"{value:.1e}"
        if abs_value >= 1000:
            return f"{value:.0f}"
        if abs_value >= 10:
            return f"{value:.1f}"
        return f"{value:.2f}"

    def _ci_parameter_pairs(self) -> list[tuple[float, float]]:
        beta_lo = self.result.beta_lower_ci
        beta_hi = self.result.beta_upper_ci
        eta_lo = self.result.eta_lower_ci
        eta_hi = self.result.eta_upper_ci
        if None in (beta_lo, beta_hi, eta_lo, eta_hi):
            return []
        return [(max(0.01, float(beta_lo)), max(0.01, float(eta_lo))), (max(0.01, float(beta_hi)), max(0.01, float(eta_hi)))]

    def _weibull_ci_curves(self, curve_xs: list[float]) -> list[list[tuple[float, float]]]:
        curves = []
        for beta, eta in self._ci_parameter_pairs():
            curves.append([(x, beta * x - beta * math.log(eta)) for x in curve_xs])
        return curves

    def _parameter_ci_curves(self, mode: str) -> list[list[tuple[float, float]]]:
        source = [float(p["life_hours"]) for p in self.result.curve_points if p.get("life_hours") is not None]
        if not source:
            source = [float(p["life_hours"]) for p in self.result.km_points if p.get("life_hours") is not None]
        if not source or max(source) <= 0:
            return []
        max_life = max(source)
        curves = []
        for beta, eta in self._ci_parameter_pairs():
            values = []
            for index in range(1, 101):
                life = max_life * index / 100
                ratio = (life / eta) ** beta
                reliability = math.exp(-ratio)
                cdf = 1 - reliability
                pdf = (beta / eta) * (life / eta) ** (beta - 1) * reliability
                hazard = (beta / eta) * (life / eta) ** (beta - 1)
                values.append((life, {"cdf": cdf, "hazard": hazard, "pdf": pdf}[mode]))
            curves.append(values)
        return curves

    def _parameter_curve(self, mode: str) -> list[tuple[float, float]]:
        source = [float(p["life_hours"]) for p in self.result.curve_points if p.get("life_hours") is not None]
        if not source:
            source = [float(p["life_hours"]) for p in self.result.km_points if p.get("life_hours") is not None]
        if not source:
            return []
        max_life = max(source)
        if max_life <= 0:
            return []
        values = []
        for index in range(1, 101):
            life = max_life * index / 100
            ratio = (life / self.eta) ** self.beta
            reliability = math.exp(-ratio)
            cdf = 1 - reliability
            pdf = (self.beta / self.eta) * (life / self.eta) ** (self.beta - 1) * reliability
            hazard = (self.beta / self.eta) * (life / self.eta) ** (self.beta - 1)
            y = {"cdf": cdf, "hazard": hazard, "pdf": pdf}[mode]
            values.append((life, y))
        return values

    def _zoomed_domain(self, values: list[float], *, pan_ratio: float | None = None) -> list[float]:
        if not values:
            return values
        min_value, max_value = min(values), max(values)
        if self._zoom_factor <= 1.01 or math.isclose(min_value, max_value):
            return values
        full_span = max_value - min_value
        center = (min_value + max_value) / 2
        shift = full_span * (self._pan_x_ratio if pan_ratio is None else pan_ratio)
        half_span = full_span / (2 * self._zoom_factor)
        return [center + shift - half_span, center + shift + half_span]

    def _scale(self, x: float, y: float, xs: list[float], ys: list[float], left: int, top: int, width: int, height: int) -> tuple[int, int]:
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        if math.isclose(min_x, max_x):
            max_x = min_x + 1
        if math.isclose(min_y, max_y):
            max_y = min_y + 1
        px = left + int((x - min_x) / (max_x - min_x) * width)
        py = top + height - int((y - min_y) / (max_y - min_y) * height)
        return px, py

    def _draw_polyline(self, painter: QPainter, values: list[tuple[float, float]], xs: list[float], ys: list[float], left: int, top: int, width: int, height: int, color: Qt.GlobalColor) -> None:
        painter.setPen(QPen(color, 1 if color == Qt.GlobalColor.yellow else 2))
        previous = None
        for x, y in values:
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                previous = None
                continue
            point = self._scale(x, y, xs, ys, left, top, width, height)
            if previous is not None:
                painter.drawLine(previous[0], previous[1], point[0], point[1])
            previous = point

    def _draw_points(self, painter: QPainter, values: list[tuple[float, float, dict[str, object]]], xs: list[float], ys: list[float], left: int, top: int, width: int, height: int, mode: str, color: Qt.GlobalColor) -> None:
        if not values:
            return
        painter.setPen(QPen(color, 2))
        painter.setBrush(color)
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        for x, y, point in values:
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                continue
            px, py = self._scale(x, y, xs, ys, left, top, width, height)
            painter.drawEllipse(px - 4, py - 4, 8, 8)
            observation_id = int(point.get("weibull_observation_id") or 0)
            if observation_id:
                self._point_targets.append({
                    "rect": QRect(px - 8, py - 8, 16, 16),
                    "observation_id": observation_id,
                })
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_vertical_markers(self, painter: QPainter, values: list[tuple[float, float, dict[str, object]]], xs: list[float], ys: list[float], left: int, top: int, width: int, height: int, color: Qt.GlobalColor) -> None:
        """Draw current right-censored observations as vertical now markers instead of point markers."""

        if not values or not xs or not ys:
            return
        min_x, max_x = min(xs), max(xs)
        painter.setPen(QPen(color, 2))
        for x, _y, point in values:
            if not (min_x <= x <= max_x):
                continue
            px, _py = self._scale(x, min(ys), xs, ys, left, top, width, height)
            painter.drawLine(px, top, px, top + height)
            observation_id = int(point.get("weibull_observation_id") or 0)
            if observation_id:
                self._point_targets.append({
                    "rect": QRect(px - 8, top, 16, height),
                    "observation_id": observation_id,
                })
        painter.setPen(QPen(Qt.GlobalColor.white, 1))

    def _failure_observation_for_point(self, point: dict[str, object]) -> dict[str, object]:
        life_hours = float(point.get("life_hours") or 0)
        for observation in self.result.observations:
            if int(observation.get("failure_indicator") or 0) != 1:
                continue
            if math.isclose(float(observation.get("life_hours_for_weibull") or 0), life_hours):
                return dict(observation)
        return dict(point)

    def _historical_censored_points(self, mode: str) -> list[tuple[float, float, dict[str, object]]]:
        return [point for point in self._censored_points(mode) if not self._is_current_censor(point[2])]

    def _current_censor_points(self, mode: str) -> list[tuple[float, float, dict[str, object]]]:
        return [point for point in self._censored_points(mode) if self._is_current_censor(point[2])]

    def _is_current_censor(self, observation: dict[str, object]) -> bool:
        return (
            int(observation.get("is_right_censored") or 0) == 1
            and str(observation.get("observation_type") or "").upper() == "RIGHT_CENSORED_LIFE"
            and not observation.get("end_datetime")
        )

    def _censored_points(self, mode: str) -> list[tuple[float, float, dict[str, object]]]:
        points = []
        for observation in self.result.observations:
            if int(observation.get("is_right_censored") or 0) != 1:
                continue
            life_hours = float(observation.get("life_hours_for_weibull") or 0)
            if life_hours <= 0:
                continue
            ratio = (life_hours / self.eta) ** self.beta
            reliability = math.exp(-ratio)
            if mode == "weibull":
                x = math.log(life_hours)
                y = self.beta * x - self.beta * math.log(self.eta)
            elif mode == "cdf":
                x = life_hours
                y = 1 - reliability
            elif mode in {"hazard", "pdf"}:
                x = life_hours
                pdf = (self.beta / self.eta) * (life_hours / self.eta) ** (self.beta - 1) * reliability
                hazard = (self.beta / self.eta) * (life_hours / self.eta) ** (self.beta - 1)
                y = {"hazard": hazard, "pdf": pdf}[mode]
            else:
                continue
            points.append((x, y, dict(observation)))
        return points

    def _target_at(self, point: QPoint) -> dict[str, object] | None:
        for target in self._point_targets:
            rect = target["rect"]
            if isinstance(rect, QRect) and rect.contains(point):
                return target
        return None

    def _point_summary(self, point: dict[str, object], mode: str, *, multiline: bool = False, include_all: bool = False) -> str:
        sep = "\n" if multiline else " | "
        values = [
            f"Plot: {mode.title()}",
            f"Point #{point.get('ordered_index', '—')}",
            f"Life hours: {self._format_axis_value(float(point.get('life_hours') or 0))}",
            f"Failures at time: {point.get('failure_count_at_time', '—')}",
            f"Censored at time: {point.get('censored_count_at_time', '—')}",
            f"At risk: {point.get('at_risk_count', '—')}",
            f"CDF: {float(point.get('cdf_estimate') or 0):.4f}",
            f"Reliability: {float(point.get('reliability_estimate') or 0):.4f}",
        ]
        if include_all:
            values.extend([
                f"Weibull X: {float(point.get('weibull_plot_x') or 0):.4f}",
                f"Weibull Y: {float(point.get('weibull_plot_y') or 0):.4f}",
                f"Current beta: {self.beta:.4g}",
                f"Current eta: {self.eta:.4g}",
            ])
        return sep.join(values)


class ParetoGraphWidget(QWidget):
    """Draw a compact failure-mechanism Pareto chart for the selected asset."""

    mechanism_selected = pyqtSignal(dict)

    def __init__(self, rows: list[dict[str, float | int | str]]) -> None:
        super().__init__()
        self.rows = rows
        self.metric_key = "downtime_hours"
        self.metric_label = "Downtime (h)"
        self._bar_hitboxes: list[tuple[QRect, dict[str, float | int | str]]] = []
        self.setMinimumHeight(360)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click a mechanism bar to run its Weibull analysis.")

    def set_rows(self, rows: list[dict[str, float | int | str]]) -> None:
        self.rows = rows
        self._bar_hitboxes = []
        self.update()

    def set_metric(self, metric_key: str) -> None:
        if metric_key == "failure_count":
            self.metric_key = "failure_count"
            self.metric_label = "Failures"
        else:
            self.metric_key = "downtime_hours"
            self.metric_label = "Downtime (h)"
        self._bar_hitboxes = []
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override name
        if event.button() == Qt.MouseButton.LeftButton:
            click_position = event.position().toPoint()
            for hitbox, row in self._bar_hitboxes:
                if hitbox.contains(click_position):
                    self.mechanism_selected.emit(dict(row))
                    return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        left, right, top, bottom = 120, 72, 56, 164
        width = max(80, self.width() - left - right)
        height = max(90, self.height() - top - bottom)
        painter.setPen(QPen(Qt.GlobalColor.darkGreen, 1))
        painter.drawRoundedRect(10, 10, self.width() - 20, self.height() - 20, 8, 8)
        painter.setPen(QPen(Qt.GlobalColor.white, 1))
        painter.drawText(24, 28, f"Failure Mechanism Pareto by {self.metric_label.lower()}")
        if not self.rows:
            painter.setPen(QPen(Qt.GlobalColor.lightGray, 1))
            painter.drawText(24, top + 48, "No included failure mechanisms are available for this asset yet.")
            painter.end()
            return

        chart_rows = sorted(
            self.rows,
            key=lambda row: (-self._metric_value(row), str(row.get("failure_mechanism_name") or "")),
        )[:8]
        self._bar_hitboxes = []
        values = [self._metric_value(row) for row in chart_rows]
        all_total = sum(self._metric_value(row) for row in self.rows)
        total = all_total if all_total > 0 else 1.0
        max_value = max(values) if values else 1.0
        max_value = max_value if max_value > 0 else 1.0

        axis_pen = QPen(Qt.GlobalColor.darkGray, 1)
        grid_pen = QPen(Qt.GlobalColor.darkGray, 1)
        grid_pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(axis_pen)
        painter.drawLine(left, top + height, left + width, top + height)
        painter.drawLine(left, top, left, top + height)
        painter.drawLine(left + width, top, left + width, top + height)

        tick_count = 4
        for tick in range(tick_count + 1):
            ratio = tick / tick_count
            y = top + height - int(ratio * height)
            painter.setPen(grid_pen if tick else axis_pen)
            painter.drawLine(left, y, left + width, y)
            painter.setPen(QPen(Qt.GlobalColor.lightGray, 1))
            metric_text = self._format_metric(max_value * ratio)
            percent_text = f"{int(ratio * 100)}%"
            painter.drawText(12, y - 8, left - 20, 16, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, metric_text)
            painter.drawText(left + width + 8, y - 8, right - 16, 16, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, percent_text)

        bar_gap = 10
        bar_width = max(14, (width - bar_gap * (len(chart_rows) + 1)) // len(chart_rows))
        cumulative = 0.0
        previous = None
        label_font = QFont(painter.font())
        label_font.setPointSize(max(7, label_font.pointSize() - 1))
        for index, row in enumerate(chart_rows):
            value = self._metric_value(row)
            cumulative += value
            x = left + bar_gap + index * (bar_width + bar_gap)
            bar_height = int(value / max_value * (height - 10)) if value else 0
            y = top + height - bar_height
            painter.fillRect(x, y, bar_width, bar_height, Qt.GlobalColor.green)
            hitbox_top = min(y, top + height - 1)
            hitbox_height = max(1, top + height - hitbox_top)
            self._bar_hitboxes.append((QRect(x, hitbox_top, bar_width, hitbox_height), row))

            painter.setPen(QPen(Qt.GlobalColor.white, 1))
            painter.drawText(x - 10, max(top + 4, y - 18), bar_width + 20, 16, Qt.AlignmentFlag.AlignCenter, self._format_metric(value))
            painter.drawText(x, top + height + 4, bar_width, 16, Qt.AlignmentFlag.AlignCenter, str(index + 1))
            self._draw_slanted_label(painter, x + bar_width // 2, top + height + 24, str(row.get("failure_mechanism_name") or "Unknown")[:28], label_font)

            point = (x + bar_width // 2, top + height - int((cumulative / total) * height))
            painter.setPen(QPen(Qt.GlobalColor.yellow, 2))
            if previous is not None:
                painter.drawLine(previous[0], previous[1], point[0], point[1])
            painter.drawEllipse(point[0] - 3, point[1] - 3, 6, 6)
            previous = point

        painter.setPen(QPen(Qt.GlobalColor.lightGray, 1))
        painter.save()
        painter.setPen(QPen(Qt.GlobalColor.lightGray, 1))
        painter.translate(22, top + height // 2)
        painter.rotate(-90)
        painter.drawText(-60, 0, 120, 18, Qt.AlignmentFlag.AlignCenter, self.metric_label)
        painter.restore()
        painter.drawText(left + width - 98, top - 8, "Cumulative %")
        painter.drawText(left, self.height() - 34, width, 18, Qt.AlignmentFlag.AlignCenter, "Failure mechanism")
        painter.end()

    def _metric_value(self, row: dict[str, float | int | str]) -> float:
        if self.metric_key == "failure_count":
            return float(int(row.get("failure_count") or 0))
        return float(row.get("downtime_hours") or 0.0)

    def _format_metric(self, value: float) -> str:
        if self.metric_key == "failure_count":
            return str(int(round(value)))
        if value >= 100:
            return f"{value:.0f}"
        if value >= 10:
            return f"{value:.1f}"
        return f"{value:.2f}".rstrip("0").rstrip(".")

    def _draw_slanted_label(self, painter: QPainter, center_x: int, baseline_y: int, text: str, font: QFont) -> None:
        painter.save()
        painter.setFont(font)
        painter.setPen(QPen(Qt.GlobalColor.lightGray, 1))
        painter.translate(center_x, baseline_y)
        painter.rotate(-45)
        painter.drawText(-132, 0, 132, 18, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


class LoadingIndicator(QWidget):
    """Compact animated loading icon that can be shown for any blocking action."""

    FRAMES = ("◐", "◓", "◑", "◒")

    def __init__(self) -> None:
        super().__init__()

        self._frame_index = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.icon = label(self.FRAMES[0], size=18, weight=QFont.Weight.Bold)
        self.message = label("Loading…", size=12, color_role="mutedLabel")
        layout.addWidget(self.icon)
        layout.addWidget(self.message)
        self.setVisible(False)

    def start(self, message: str) -> None:
        self.message.setText(message)
        self._frame_index = 0
        self.icon.setText(self.FRAMES[self._frame_index])
        self.setVisible(True)
        self._timer.start(120)

    def stop(self) -> None:
        self._timer.stop()
        self.setVisible(False)

    def _advance(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(self.FRAMES)
        self.icon.setText(self.FRAMES[self._frame_index])


class GremlinGui(QMainWindow):
    """Main GREMLIN desktop window built with PyQt widgets."""

    DISPOSITION_PAGE_SIZE = 50

    NAV_ICON_GAP = "  "
    NAV_ITEMS = (
        ("⌂", "Home", "Home"),
        ("↗", "Life Data Analysis", "Life Data Analysis"),
        ("📊", "Availability", "Availability"),
        ("▣", "Metrics", "Metrics"),
        ("☰", "Standards and Documentation", "Standards and Documentation"),
        ("⚙", "Settings", "Settings"),

    )

    def __init__(self) -> None:
        super().__init__()

        self.df = pd.read_excel(
            r"\\sandc.ws\depts\Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\Availability Dashboard.xlsx",
            sheet_name="Availability Data"
        )
        self.df.columns = self.df.columns.str.strip().str.lower()

        self.reliability_service = ReliabilityService(
            metrics_repo=MetricsRepository(),
            failure_repo=FailureRepository(),
            analysis_repo=AnalysisRepository(),
        )
        self.nav_buttons: list[QPushButton] = []
        self._card_animations: list[QPropertyAnimation] = []
        self._card_reveal_timers: list[QTimer] = []
        try:
            self.life_data_service = LifeDataService(refresh_on_startup=False)
            self.availability_repository = AvailabilityRepository()
        except DatabaseWriteError as exc:
            QMessageBox.critical(None, "Shared database startup failed", str(exc))
            raise
        except (sqlite3.Error, OSError) as exc:
            QMessageBox.critical(None, "Shared database startup failed", f"GREMLIN could not initialize availability database tables.\n\n{exc}")
            raise
        self.selected_asset_number: str | None = None
        self.selected_analysis_type: str | None = None
        self.latest_analysis_result: AnalysisResultView | None = None
        self.loading_indicator = LoadingIndicator()
        self._loading_depth = 0
        self._background_threads: list[QThread] = []
        self._background_workers: list[BackgroundWorker] = []
        self._page_builders: dict[int, Callable[[], QWidget]] = {}
        self._built_page_indexes: set[int] = set()
        self._life_selection_refresh_token = 0
        self._life_selection_timer = QTimer(self)
        self._life_selection_timer.setSingleShot(True)
        self._life_selection_timer.setInterval(300)
        self._life_selection_timer.timeout.connect(self._life_selection_changed)

        self.setWindowTitle("GREMLIN — Reliability Engineering Desktop GUI")
        self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.resize(1360, 900)
        self.setMinimumSize(1125, 750)
        self.setStyleSheet(BASE_QSS)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_topbar())
        root_layout.addWidget(self._build_body(), stretch=1)
        self.setCentralWidget(root)
        self._select_page(0)

    def _show_write_failure(self, fallback_title: str, exc: Exception) -> None:
        """Show dedicated user-friendly database-write failure messages."""

        if isinstance(exc, DatabaseWriteError):
            QMessageBox.critical(self, "Database write failed", str(exc))
        else:
            QMessageBox.critical(self, fallback_title, str(exc))

    def _begin_loading(self, message: str = "Loading…") -> None:
        """Show the shared loading icon before a blocking GUI operation."""

        was_idle = self._loading_depth == 0
        self._loading_depth += 1
        self.loading_indicator.start(message)
        if was_idle:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

    def _end_loading(self) -> None:
        """Hide the shared loading icon when the current blocking operation finishes."""

        if self._loading_depth == 0:
            return
        self._loading_depth -= 1
        if self._loading_depth == 0:
            self.loading_indicator.stop()
            QApplication.restoreOverrideCursor()
            QApplication.processEvents()

    def _run_with_loading(self, message: str, action):
        """Run a short action while the top-bar loading icon and wait cursor are visible."""

        self._begin_loading(message)
        try:
            return action()
        finally:
            self._end_loading()

    def _run_in_background(self, message: str, action: Callable[[], object], on_finished: Callable[[object], None], *, error_title: str = "Operation failed") -> None:
        """Run long database/import/export work in a QThread and update UI on completion."""

        self._begin_loading(message)
        thread = QThread(self)
        worker = BackgroundWorker(action)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        def cleanup() -> None:
            self._end_loading()
            if thread in self._background_threads:
                self._background_threads.remove(thread)
            if worker in self._background_workers:
                self._background_workers.remove(worker)
            thread.quit()

        def handle_finished(result: object) -> None:
            try:
                on_finished(result)
            finally:
                cleanup()

        def handle_failed(exc: object) -> None:
            try:
                self._show_write_failure(error_title, exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            finally:
                cleanup()

        worker.signals.finished.connect(handle_finished)
        worker.signals.failed.connect(handle_failed)
        worker.signals.database_lock_waiting.connect(lambda: self.loading_indicator.start("Waiting for database lock…"))
        self._background_threads.append(thread)
        self._background_workers.append(worker)
        thread.start()

    def _dispose_animation(self, animation: QPropertyAnimation | None) -> None:
        """Stop and release an old animation so repeated GUI use does not accumulate QObjects."""

        if animation is None:
            return
        animation.stop()
        animation.deleteLater()

    def _reset_card_animations(self) -> None:
        """Release previous card-reveal animations before starting a new page reveal."""

        for timer in self._card_reveal_timers:
            timer.stop()
            timer.deleteLater()
        self._card_reveal_timers = []
        for animation in self._card_animations:
            self._dispose_animation(animation)
        self._card_animations = []

    def _remove_dynamic_stack_pages(self) -> None:
        """Remove temporary workflow pages so repeatedly opening tools does not grow the stack."""

        fixed_count = getattr(self, "_fixed_stack_count", self.stack.count())
        while self.stack.count() > fixed_count:
            widget = self.stack.widget(fixed_count)
            self.stack.removeWidget(widget)
            widget.deleteLater()

    def _build_topbar(self) -> QWidget:
        top = QFrame()
        top.setObjectName("topHeader")
        layout = QHBoxLayout(top)
        layout.setContentsMargins(24, 14, 24, 14)
        layout.setSpacing(14)

        mark = QLabel()
        mark.setObjectName("brandMark")
        mark.setPixmap(
            QPixmap(str(TOPBAR_LOGO_PATH)).scaled(
                44,
                44,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        mark.setFixedSize(52, 52)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(mark)

        title_block = QWidget()
        title_layout = QVBoxLayout(title_block)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(2)
        title_layout.addWidget(label("GREMLIN", size=20, weight=QFont.Weight.Bold))
        title_layout.addWidget(label("Graphical Reliability Engineering, Maintenance, Life-Data INterface", size=12, color_role="mutedLabel"))
        layout.addWidget(title_block)
        layout.addStretch(1)
        layout.addWidget(self.loading_indicator)
        return top

    def _build_body(self) -> QWidget:
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        self._page_builders = {
            0: self._build_home_page,
            1: self._build_life_data_page,
            2: self._build_availability_page,  #NEW BT
            3: self._build_metrics_page,
            4: self._build_docs_page,
            5: self._build_settings_page,
            6: self._build_perform_analysis_page,
            7: self._build_failure_classification_page,

        }
        for index in range(len(self._page_builders)):
            if index == 0:
                self.stack.addWidget(self._scrollable(self._build_home_page()))
                self._built_page_indexes.add(0)
            else:
                self.stack.addWidget(self._lazy_page_placeholder(index))
        self._fixed_stack_count = self.stack.count()
        layout.addWidget(self.stack, stretch=1)
        return body


    def _lazy_page_placeholder(self, index: int) -> QWidget:
        placeholder = QWidget()
        placeholder.setProperty("lazy_page_index", index)
        layout = QVBoxLayout(placeholder)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.addWidget(label("Loading page…", size=18, color_role="mutedLabel"))
        layout.addStretch(1)
        return placeholder

    def _ensure_page_built(self, index: int) -> None:
        if index in self._built_page_indexes or index not in self._page_builders:
            return
        builder = self._page_builders[index]
        page = self._scrollable(builder())
        old_widget = self.stack.widget(index)
        self.stack.removeWidget(old_widget)
        old_widget.deleteLater()
        self.stack.insertWidget(index, page)
        self._built_page_indexes.add(index)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(270)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 22, 18, 18)
        layout.setSpacing(8)
        for index, (icon, text, _title) in enumerate(self.NAV_ITEMS):
            button = AnimatedPushButton(self._format_nav_text(icon, text))
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, page=index: self._select_page(page))
            self.nav_buttons.append(button)
            layout.addWidget(button)
        layout.addStretch(1)
        layout.addWidget(label("GREMLIN reliability tools", size=12, color_role="mutedLabel"))
        layout.addWidget(label("Maintenance analytics, life-data studies, metrics, standards, and integration settings.", size=12, color_role="mutedLabel", wrap=True))
        return sidebar

    def _format_nav_text(self, icon: str, text: str) -> str:
        """Return sidebar navigation text with a consistent icon/text gap."""

        return f"{icon}{self.NAV_ICON_GAP}{text}"

    def _scrollable(self, page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _select_page(self, index: int) -> None:
        self._ensure_page_built(index)
        self.stack.setCurrentIndex(index)
        if index < getattr(self, "_fixed_stack_count", self.stack.count()):
            self._remove_dynamic_stack_pages()
        self._animate_current_page()
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)

    def _animate_current_page(self) -> None:
        """Fade and slide in newly selected pages, then reveal cards in sequence."""

        self._dispose_animation(getattr(self, "_page_animation", None))
        self._dispose_animation(getattr(self, "_page_slide_animation", None))
        self._page_animation = None
        self._page_slide_animation = None
        current = self.stack.currentWidget()
        effect = QGraphicsOpacityEffect(current)
        current.setGraphicsEffect(effect)

        fade = QPropertyAnimation(effect, b"opacity", self)
        fade.setDuration(320)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade.finished.connect(lambda: current.setGraphicsEffect(None))

        start_pos = current.pos() + QPoint(18, 0)
        end_pos = current.pos()
        current.move(start_pos)
        slide = QPropertyAnimation(current, b"pos", self)
        slide.setDuration(320)
        slide.setStartValue(start_pos)
        slide.setEndValue(end_pos)
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._page_animation = fade
        self._page_slide_animation = slide
        fade.start()
        slide.start()
        self._animate_card_reveal(current)

    def _animate_card_reveal(self, current: QWidget) -> None:
        """Stagger visible cards for a more dynamic dashboard load."""

        self._reset_card_animations()
        reveal_widgets = [
            widget
            for widget in current.findChildren(QWidget)
            if widget.objectName() in {"card", "workflowCard"}
        ]
        for index, widget in enumerate(reveal_widgets):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            effect.setOpacity(0.0)
            animation = QPropertyAnimation(effect, b"opacity", self)
            animation.setDuration(360)
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.finished.connect(lambda target=widget: self._restore_widget_effect(target))
            self._card_animations.append(animation)
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(animation.start)
            self._card_reveal_timers.append(timer)
            timer.start(80 + index * 70)

    def _restore_widget_effect(self, widget: QWidget) -> None:
        """Restore button micro-interaction effects after reveal animations finish."""

        widget.setGraphicsEffect(None)

    def _page_shell(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setStyleSheet(f"background: {COLORS.canvas};")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(18)
        layout.addWidget(label(title, size=28, weight=QFont.Weight.Bold))
        layout.addWidget(label(subtitle, size=15, color_role="mutedLabel", wrap=True))
        return page, layout

    def _card(self, title: str, body: QWidget | QLabel, *, footer: str | None = None) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(label(title, size=17, weight=QFont.Weight.DemiBold))
        layout.addWidget(body)
        if footer:
            layout.addWidget(label(footer, size=12, color_role="mutedLabel", wrap=True))
        return card

    def _build_home_page(self) -> QWidget:
        page, layout = self._page_shell(
            "Graphical Reliability Engineering, Maintenance, Life-Data INterface",
            "A native PyQt desktop workspace for maintenance analytics, life-data analysis, Limble integration placeholders, and reliability reporting.",
        )
        hero_text = label(
            "GREMLIN is focused on reliability engineering content: maintenance analytics, life-data studies, failure classification, metrics, documentation, and integration settings in a green desktop workspace.",
            size=16,
            wrap=True,
        )
        layout.addWidget(self._card("GREMLIN reliability workspace", hero_text))

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        items = (
            ("Life Data Analysis", "Prepare Weibull fits, survival curves, and reliability study drafts."),
            ("Metrics", "Review MTBF, availability, PM backlog, and critical asset snapshots."),
            ("Availability", "Review Asset Availability Since The Beginning of Time"),
            ("Failure Classification", "Summarize infant mortality, random, and wear-out failure patterns."),
            ("Integrations", "Keep Limble and API/sync scripts visible as future production hooks."),
        )
        for position, (title, description) in enumerate(items):
            grid.addWidget(self._card(title, label(description, color_role="mutedLabel", wrap=True)), position // 2, position % 2)
        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    def _build_life_data_page(self) -> QWidget:
        page, layout = self._page_shell(
            "Life Data Analysis",
            "Select an Asset Number from mapped CMMS data, disposition corrective WOs and PM resets, then run a REL-style Weibull analysis using only valid observations.",
        )

        selectors = QWidget()
        selectors_layout = QGridLayout(selectors)
        selectors_layout.setContentsMargins(0, 0, 0, 0)
        selectors_layout.setHorizontalSpacing(14)
        selectors_layout.setVerticalSpacing(6)
        selectors_layout.setColumnStretch(0, 1)
        selectors_layout.setColumnStretch(1, 1)

        selectors_layout.addWidget(label("Asset Number", size=13, weight=QFont.Weight.DemiBold), 0, 0)
        selectors_layout.addWidget(label("Analysis Type", size=13, weight=QFont.Weight.DemiBold), 0, 1)
        self.asset_combo = QComboBox()
        self.asset_combo.setEditable(True)
        self.asset_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.asset_combo.setMaximumWidth(360)
        self.asset_combo.setMinimumWidth(240)
        self.asset_combo.lineEdit().setPlaceholderText("Search imported Asset Number values…")
        self.asset_combo.addItem("", "")
        self._populate_asset_combo()
        self.asset_combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.asset_combo.completer().activated[str].connect(self._asset_completion_selected)
        self.asset_combo.activated.connect(self._asset_dropdown_selected)
        self.asset_combo.lineEdit().textEdited.connect(self._asset_text_edited)
        self.asset_combo.lineEdit().returnPressed.connect(self._asset_search_return_pressed)
        selectors_layout.addWidget(self.asset_combo, 1, 0)

        self.analysis_type_combo = QComboBox()
        self.analysis_type_combo.addItems(("Weibull Analysis", "Availability Analysis"))
        self.analysis_type_combo.currentTextChanged.connect(self._schedule_life_selection_changed)
        selectors_layout.addWidget(self.analysis_type_combo, 1, 1)
        layout.addWidget(self._card("1. Select asset and analysis type", selectors))

        self.summary_grid = QGridLayout()
        self.summary_labels: dict[str, QLabel] = {}
        summary_titles = (
            ("total_entries", "Total entries for this asset"),
            ("usable_wos_for_weibull", "Usable WOs for Weibull"),
            ("usable_pms_for_weibull", "Usable PMs for Weibull"),
            ("wos_dispositioned", "WOs dispositioned"),
            ("wos_not_dispositioned", "WOs not dispositioned"),
            ("pms_dispositioned", "PMs dispositioned"),
            ("pms_not_dispositioned", "PMs not dispositioned"),
        )
        for index, (key, title) in enumerate(summary_titles):
            metric_box = QWidget()
            metric_layout = QVBoxLayout(metric_box)
            metric_layout.setContentsMargins(0, 0, 0, 0)
            value = label("—", size=25, weight=QFont.Weight.Bold)
            self.summary_labels[key] = value
            metric_layout.addWidget(value)
            metric_layout.addWidget(label(title, size=12, color_role="mutedLabel", wrap=True))
            self.summary_grid.addWidget(self._card(title, metric_box), index // 3, index % 3)
        summary_widget = QWidget()
        summary_layout = QVBoxLayout(summary_widget)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setSpacing(12)
        summary_grid_widget = QWidget()
        summary_grid_widget.setLayout(self.summary_grid)
        summary_layout.addWidget(summary_grid_widget)

        self.top_beta_labels: list[QLabel] = []
        top_beta_widget = QWidget()
        top_beta_layout = QVBoxLayout(top_beta_widget)
        top_beta_layout.setContentsMargins(0, 0, 0, 0)
        top_beta_layout.setSpacing(4)
        top_beta_layout.addWidget(label("Top 5 failure mechanisms by beta", size=14, weight=QFont.Weight.DemiBold))
        for _ in range(5):
            beta_label = label("—", color_role="mutedLabel", wrap=True)
            self.top_beta_labels.append(beta_label)
            top_beta_layout.addWidget(beta_label)
        summary_layout.addWidget(self._card("Highest-beta mechanisms", top_beta_widget, footer="Uses the latest saved Weibull result for each failure-mechanism population on this asset."))

        pareto_widget = QWidget()
        pareto_layout = QVBoxLayout(pareto_widget)
        pareto_layout.setContentsMargins(0, 0, 0, 0)
        pareto_layout.setSpacing(8)
        self.pareto_failure_count_toggle = QCheckBox("Show failure count instead of downtime hours")
        self.pareto_failure_count_toggle.setToolTip("Unchecked shows Pareto by downtime hours. Checked switches the bars and cumulative line to failure count.")
        self.pareto_failure_count_toggle.toggled.connect(
            lambda checked: self.failure_mechanism_pareto.set_metric("failure_count" if checked else "downtime_hours")
        )
        pareto_layout.addWidget(self.pareto_failure_count_toggle)
        self.failure_mechanism_pareto = ParetoGraphWidget([])
        self.failure_mechanism_pareto.mechanism_selected.connect(self._perform_pareto_mechanism_analysis)
        pareto_layout.addWidget(self.failure_mechanism_pareto)
        summary_layout.addWidget(self._card("Failure mechanism Pareto", pareto_widget))
        self.summary_card = self._card("2. Asset Weibull readiness summary", summary_widget)
        self.summary_card.setVisible(False)
        layout.addWidget(self.summary_card)

        self.action_bar = QWidget()
        action_layout = QHBoxLayout(self.action_bar)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(12)
        perform = make_button("Perform Analysis", primary=True)
        perform.clicked.connect(self._perform_life_analysis)
        disposition_wos = make_button("Disposition Work Orders")
        disposition_wos.clicked.connect(lambda: self._open_disposition_page("wo"))
        disposition_pms = make_button("Disposition PMs")
        disposition_pms.clicked.connect(lambda: self._open_disposition_page("pm"))
        action_layout.addWidget(perform)
        action_layout.addWidget(disposition_wos)
        action_layout.addWidget(disposition_pms)
        action_layout.addStretch(1)
        self.action_bar.setVisible(False)
        layout.addWidget(self.action_bar)

        self.life_workspace = QWidget()
        self.life_workspace_layout = QVBoxLayout(self.life_workspace)
        self.life_workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.life_workspace_layout.setSpacing(14)
        layout.addWidget(self.life_workspace)

        self.calculate_all_button = make_button("Calculate MLE beta/eta for all modes and mechanisms", primary=True)
        self.calculate_all_button.setToolTip("Runs and saves Weibull MLE fits for every available failure mode and failure mechanism on the selected asset.")
        self.calculate_all_button.clicked.connect(self._calculate_all_life_mle_results)
        self.calculate_all_button.setVisible(False)
        layout.addWidget(self.calculate_all_button)
        layout.addStretch(1)
        self._life_selection_changed()
        return page

    def _schedule_life_selection_changed(self) -> None:
        self._life_selection_timer.start()


    def _refresh_mapped_records(self) -> None:
        current_asset = self._selected_asset_details()[0] if hasattr(self, "asset_combo") else ""

        def finished(mapped_count: object) -> None:
            if hasattr(self, "asset_combo"):
                self.asset_combo.blockSignals(True)
                self.asset_combo.clear()
                self.asset_combo.addItem("", "")
                self._populate_asset_combo()
                if current_asset:
                    index = self._find_asset_combo_index(current_asset)
                    if index >= 0:
                        self.asset_combo.setCurrentIndex(index)
                    else:
                        self.asset_combo.setCurrentText("")
                self.asset_combo.blockSignals(False)
                self._life_selection_changed()
            QMessageBox.information(self, "CMMS mapping refreshed", f"Refreshed {int(mapped_count or 0)} mapped CMMS row(s).")

        self._run_in_background(
            "Refreshing CMMS mapping…",
            self.life_data_service.refresh_mapped_cmms_records,
            finished,
            error_title="CMMS mapping refresh failed",
        )

    def _populate_asset_combo(self) -> None:
        for option in self.life_data_service.asset_number_options():
            asset_number = option["asset_number"]
            asset_name = option.get("asset_name") or ""
            display = f"{asset_number} — {asset_name}" if asset_name else asset_number
            self.asset_combo.addItem(display, {"asset_number": asset_number, "asset_name": asset_name})

    def _find_asset_combo_index(self, asset_number: str) -> int:
        for index in range(self.asset_combo.count()):
            data = self.asset_combo.itemData(index)
            if isinstance(data, dict) and data.get("asset_number") == asset_number:
                return index
            if self.asset_combo.itemText(index) == asset_number:
                return index
        return -1

    def _asset_dropdown_selected(self, _index: int | None = None) -> None:
        """Refresh asset-dependent data only after the user selects a dropdown option."""

        self._schedule_life_selection_changed()

    def _asset_completion_selected(self, completion: str) -> None:
        """Refresh asset-dependent data after the user accepts a search suggestion."""

        completion_text = str(completion).strip()
        if completion_text:
            index = self.asset_combo.findText(completion_text)
            if index >= 0:
                self.asset_combo.setCurrentIndex(index)
            else:
                self.asset_combo.setCurrentText(completion_text)
        self._schedule_life_selection_changed()

    def _asset_text_edited(self, _text: str) -> None:
        """Clear stale asset results while users type without fetching new data."""

        if self.selected_asset_number is not None:
            self.selected_asset_number = None
            self.latest_analysis_result = None
            if hasattr(self, "life_workspace_layout"):
                self._clear_life_workspace()
        if hasattr(self, "summary_card"):
            self.summary_card.setVisible(False)
            self.action_bar.setVisible(False)

    def _asset_search_return_pressed(self) -> None:
        """Select the closest Asset Number dropdown match when the user presses Enter."""

        search_text = self.asset_combo.currentText().strip()
        if not search_text:
            return
        index = self._closest_asset_combo_index(search_text)
        if index < 0:
            return
        self.asset_combo.setCurrentIndex(index)
        self._schedule_life_selection_changed()

    def _closest_asset_combo_index(self, search_text: str) -> int:
        normalized_search = search_text.casefold()
        candidates: list[tuple[int, str, str]] = []
        for index in range(self.asset_combo.count()):
            data = self.asset_combo.itemData(index)
            display_text = self.asset_combo.itemText(index)
            if not display_text:
                continue
            asset_number = str(data.get("asset_number") or "") if isinstance(data, dict) else display_text
            candidates.append((index, asset_number, display_text))
        for index, asset_number, display_text in candidates:
            if asset_number.casefold().startswith(normalized_search) or display_text.casefold().startswith(normalized_search):
                return index
        for index, asset_number, display_text in candidates:
            if normalized_search in asset_number.casefold() or normalized_search in display_text.casefold():
                return index
        choices = [display_text for _, _, display_text in candidates]
        closest = difflib.get_close_matches(search_text, choices, n=1, cutoff=0.0)
        if not closest:
            return -1
        return next((index for index, _, display_text in candidates if display_text == closest[0]), -1)

    def _selected_asset_details(self) -> tuple[str, str]:
        if not hasattr(self, "asset_combo"):
            return "", ""
        data = self.asset_combo.currentData()
        text = self.asset_combo.currentText().strip()
        current_index = self.asset_combo.currentIndex()
        if current_index >= 0 and text == self.asset_combo.itemText(current_index) and isinstance(data, dict) and data.get("asset_number"):
            return str(data.get("asset_number") or "").strip(), str(data.get("asset_name") or "").strip()
        if " — " in text:
            asset_number, asset_name = text.split(" — ", 1)
            return asset_number.strip(), asset_name.strip()
        index = self._find_asset_combo_index(text)
        if index >= 0:
            data = self.asset_combo.itemData(index)
            if isinstance(data, dict):
                return str(data.get("asset_number") or text).strip(), str(data.get("asset_name") or "").strip()
        return text, ""

    def _life_selection_changed(self) -> None:
        if hasattr(self, "_life_selection_timer"):
            self._life_selection_timer.stop()
        asset_number, _asset_name = self._selected_asset_details()
        analysis_type = self.analysis_type_combo.currentText().strip() if hasattr(self, "analysis_type_combo") else ""
        ready = bool(asset_number and analysis_type in {"Weibull Analysis", "Availability Analysis"})
        previous_asset_number = self.selected_asset_number
        previous_analysis_type = self.selected_analysis_type
        self.selected_asset_number = asset_number if ready else None
        self.selected_analysis_type = analysis_type if ready else None
        if (previous_asset_number != self.selected_asset_number or previous_analysis_type != self.selected_analysis_type) and hasattr(self, "life_workspace_layout"):
            self.latest_analysis_result = None
            self._clear_life_workspace()
        if hasattr(self, "summary_card"):
            self.summary_card.setVisible(ready and analysis_type == "Weibull Analysis")
            self.action_bar.setVisible(ready)
            if hasattr(self, "calculate_all_button"):
                self.calculate_all_button.setVisible(ready and analysis_type == "Weibull Analysis")
        if ready and analysis_type == "Weibull Analysis":
            self._life_selection_refresh_token += 1
            self._refresh_life_summary(self._life_selection_refresh_token)

    def _refresh_life_summary(self, refresh_token: int | None = None) -> None:
        if not self.selected_asset_number:
            return
        asset_number = self.selected_asset_number
        if refresh_token is None:
            self._life_selection_refresh_token += 1
            refresh_token = self._life_selection_refresh_token

        def load_summary() -> dict[str, object]:
            data: dict[str, object] = {"summary": self.life_data_service.summary_for_asset(asset_number)}
            if hasattr(self, "top_beta_labels"):
                data["rankings"] = self.life_data_service.latest_failure_mechanism_beta_rankings(asset_number, limit=5)
            if hasattr(self, "failure_mechanism_pareto"):
                data["pareto"] = self.life_data_service.failure_mechanism_pareto(asset_number)
            return data

        def apply_summary(data: object) -> None:
            if refresh_token != self._life_selection_refresh_token or self.selected_asset_number != asset_number or not isinstance(data, dict):
                return
            summary = data["summary"]
            for key, value_label in self.summary_labels.items():
                value_label.setText(str(getattr(summary, key)))
            if hasattr(self, "top_beta_labels"):
                rankings = data.get("rankings") or []
                for index, value_label in enumerate(self.top_beta_labels):
                    if index < len(rankings):
                        row = rankings[index]
                        value_label.setText(
                            f"{index + 1}. {row['failure_mechanism_name']} — beta {row['beta_mle']:.4g} "
                            f"({row['failure_count']} failures, eta {row['eta_mle']:.4g} h)"
                        )
                    else:
                        value_label.setText("—")
            if hasattr(self, "failure_mechanism_pareto"):
                self.failure_mechanism_pareto.set_rows(data.get("pareto") or [])

        self._run_in_background("Refreshing Weibull readiness…", load_summary, apply_summary, error_title="Weibull readiness refresh failed")

    def _clear_life_workspace(self) -> None:
        while self.life_workspace_layout.count():
            item = self.life_workspace_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _combo_id(self, combo: QComboBox) -> int | None:
        value = combo.currentData()
        if value in (None, ""):
            return None
        return int(value)

    def _add_taxonomy_options(self, combo: QComboBox, options: list[dict], id_key: str, name_key: str, current_id: int | None, *, editable: bool) -> None:
        combo.setEditable(editable)
        combo.addItem("", None)
        current_index = 0
        for option in options:
            combo.addItem(str(option[name_key]), int(option[id_key]))
            if current_id is not None and int(option[id_key]) == int(current_id):
                current_index = combo.count() - 1
        combo.setCurrentIndex(current_index)
        if editable and combo.lineEdit():
            combo.lineEdit().setPlaceholderText("Select existing or type new…")
        if combo.completer():
            combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)

    def _open_disposition_page(self, kind: str) -> None:
        if not self.selected_asset_number:
            QMessageBox.warning(self, "Select an asset", "Select an Asset Number before opening a disposition page.")
            return
        disposition_scope = self._ask_disposition_scope(kind)
        if disposition_scope is None:
            return
        title = "Disposition PMs" if kind == "pm" else "Disposition Work Orders"
        subtitle = (
            "REL-compliant PM reset dispositioning for the selected asset. PMs cannot create new reset targets and must point at existing WO modes/mechanisms."
            if kind == "pm"
            else "REL-compliant corrective WO dispositioning for the selected asset. Assign defensible failure modes/mechanisms and save asset-specific dropdown options."
        )
        self._remove_dynamic_stack_pages()
        page, layout = self._page_shell(title, subtitle)
        back = make_button("← Back to Life Data Analysis")
        back.clicked.connect(lambda: self._select_page(1))
        layout.addWidget(back)
        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        layout.addWidget(editor_panel)
        self._run_with_loading(
            "Loading disposition editor…",
            lambda: self._build_disposition_editor(editor_layout, kind, disposition_scope, page_index=0),
        )
        layout.addStretch(1)
        scroll = self._scrollable(page)
        self.stack.addWidget(scroll)
        self.stack.setCurrentWidget(scroll)
        self._animate_current_page()

    def _ask_disposition_scope(self, kind: str) -> str | None:
        record_label = "PM reset events" if kind == "pm" else "work orders"
        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Icon.Question)
        prompt.setWindowTitle("Choose disposition rows")
        prompt.setText(f"Which {record_label} do you want to disposition?")
        prompt.setInformativeText(
            "Choose only new/undispositioned rows to show records with a blank failure mode or failure mechanism, "
            "or choose all to show every eligible row for the selected asset."
        )
        new_button = prompt.addButton("Only new/undispositioned", QMessageBox.ButtonRole.AcceptRole)
        all_button = prompt.addButton("All", QMessageBox.ButtonRole.ActionRole)
        prompt.addButton(QMessageBox.StandardButton.Cancel)
        prompt.exec()
        clicked = prompt.clickedButton()
        if clicked == new_button:
            return "new"
        if clicked == all_button:
            return "all"
        return None

    def _row_needs_disposition(self, row: dict, is_pm: bool) -> bool:
        if is_pm:
            mode_keys = ("reset_target_failure_mode_id", "reset_target_failure_mode")
            mechanism_keys = ("reset_target_failure_mechanism_id", "reset_target_failure_mechanism")
        else:
            mode_keys = ("failure_mode_id", "failure_mode")
            mechanism_keys = ("failure_mechanism_id", "failure_mechanism")
        has_mode = any(row.get(key) not in (None, "") for key in mode_keys)
        has_mechanism = any(row.get(key) not in (None, "") for key in mechanism_keys)
        return not (has_mode and has_mechanism)

    def _show_disposition_table(self, kind: str) -> None:
        self._open_disposition_page(kind)

    def _build_disposition_editor(self, layout: QVBoxLayout, kind: str, disposition_scope: str = "all", *, page_index: int = 0) -> None:
        is_pm = kind == "pm"
        page_size = self.DISPOSITION_PAGE_SIZE
        only_needing_disposition = disposition_scope == "new"
        all_row_count = self.life_data_service.disposition_row_count(self.selected_asset_number, kind)
        displayed_row_count = self.life_data_service.disposition_row_count(
            self.selected_asset_number,
            kind,
            only_needing_disposition=only_needing_disposition,
        )
        max_page_index = max(0, math.ceil(displayed_row_count / page_size) - 1) if displayed_row_count else 0
        page_index = min(max(page_index, 0), max_page_index)
        offset = page_index * page_size
        rows = self.life_data_service.disposition_rows(
            self.selected_asset_number,
            kind,
            only_needing_disposition=only_needing_disposition,
            limit=page_size,
            offset=offset,
        )
        mode_options = self.life_data_service.get_asset_failure_mode_options(self.selected_asset_number)
        mechanism_options = self.life_data_service.get_asset_failure_mechanism_options(self.selected_asset_number)
        intro = "If no asset-specific WO modes/mechanisms are available, approve no PM resets until relevant WOs are dispositioned." if is_pm else ""
        if is_pm and not mode_options and not mechanism_options:
            intro = "No WO failure modes or mechanisms have been dispositioned for this asset yet. Disposition relevant Work Orders before approving PM reset events."
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(10)
        panel_layout.addWidget(label(f"Selected asset: {self.selected_asset_number}", size=16, weight=QFont.Weight.DemiBold))
        start_row = offset + 1 if rows else 0
        end_row = offset + len(rows)
        if disposition_scope == "new":
            missing_label = "blank reset target failure mode or mechanism" if is_pm else "blank failure mode or failure mechanism"
            panel_layout.addWidget(label(
                f"Showing rows {start_row}-{end_row} of {displayed_row_count} rows with a {missing_label} ({all_row_count} eligible rows total).",
                color_role="mutedLabel",
                wrap=True,
            ))
        else:
            panel_layout.addWidget(label(f"Showing rows {start_row}-{end_row} of {displayed_row_count} eligible rows for this asset.", color_role="mutedLabel", wrap=True))
        if intro:
            panel_layout.addWidget(label(intro, color_role="mutedLabel", wrap=True))
        panel_layout.addWidget(label("Keyboard shortcut: press Shift+W, then A, then D to check every Include in Weibull Candidate box in this table.", color_role="mutedLabel", wrap=True))

        if is_pm:
            extra_headers = [
                "Disposition Notes", "Disposition Category", "Record Class", "PM Reset Decision",
                "Reset Target Failure Mode", "Reset Target Failure Mechanism", "Modeled Population",
                "Include in Weibull Candidate", "PM Reset Renewal Rationale / Evidence",
            ]
        else:
            extra_headers = [
                "Disposition Notes", "Disposition Category", "Record Class", "Failure Mode",
                "Failure Mechanism", "Modeled Population", "Include in Weibull Candidate",
            ]
        table = QTableWidget()
        table.setUpdatesEnabled(False)
        table.setSortingEnabled(False)
        table_blocker = QSignalBlocker(table)
        table.setColumnCount(len(DISPLAY_COLUMNS) + len(extra_headers))
        table.setHorizontalHeaderLabels(tuple(DISPLAY_COLUMNS) + tuple(extra_headers))
        table.setRowCount(len(rows))
        table.setVerticalHeaderLabels([str(offset + row_number + 1) for row_number in range(len(rows))])
        table.setProperty("disposition_kind", kind)
        table.setProperty("mapped_ids", [row["mapped_record_id"] for row in rows])
        columns = {name: len(DISPLAY_COLUMNS) + i for i, name in enumerate(extra_headers)}
        table.setProperty("field_columns", columns)
        initial_payloads = {}

        for row_index, row in enumerate(rows):
            for col_index, key in enumerate(DISPLAY_COLUMNS):
                item = QTableWidgetItem(str(row.get(key) or ""))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row_index, col_index, item)

            notes = QTextEdit(str(row.get("disposition_notes") or row.get("disposition_text") or ""))
            notes.setMinimumHeight(58)
            table.setCellWidget(row_index, columns["Disposition Notes"], notes)

            category = QComboBox()
            category.addItems(PM_DISPOSITION_CATEGORIES if is_pm else WO_DISPOSITION_CATEGORIES)
            current_category = row.get("disposition_category") or "UNKNOWN"
            category.setCurrentText(current_category if category.findText(current_category) >= 0 else "UNKNOWN")
            table.setCellWidget(row_index, columns["Disposition Category"], category)

            record_class = QComboBox()
            record_class.addItems(["PM", "PM_RESET_CANDIDATE", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN"] if is_pm else ["CORRECTIVE_WO", "PM", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN"])
            default_class = "PM" if is_pm else "CORRECTIVE_WO"
            record_class.setCurrentText(row.get("effective_record_class") or default_class)
            table.setCellWidget(row_index, columns["Record Class"], record_class)

            if is_pm:
                decision = QComboBox()
                decision.addItems(PM_RESET_DECISIONS)
                current_decision = row.get("pm_reset_inclusion_decision") or "NEEDS_REVIEW"
                decision.setCurrentText(current_decision if decision.findText(current_decision) >= 0 else "NEEDS_REVIEW")
                table.setCellWidget(row_index, columns["PM Reset Decision"], decision)

                mode = QComboBox()
                self._add_taxonomy_options(mode, mode_options, "failure_mode_id", "failure_mode_name", row.get("reset_target_failure_mode_id"), editable=False)
                table.setCellWidget(row_index, columns["Reset Target Failure Mode"], mode)
                mech = QComboBox()
                self._add_taxonomy_options(mech, mechanism_options, "failure_mechanism_id", "failure_mechanism_name", row.get("reset_target_failure_mechanism_id"), editable=False)
                table.setCellWidget(row_index, columns["Reset Target Failure Mechanism"], mech)

                rationale = QTextEdit(str(row.get("pm_reset_renewal_rationale") or ""))
                rationale.setMinimumHeight(58)
                table.setCellWidget(row_index, columns["PM Reset Renewal Rationale / Evidence"], rationale)
            else:
                mode = QComboBox()
                self._add_taxonomy_options(mode, mode_options, "failure_mode_id", "failure_mode_name", row.get("failure_mode_id"), editable=True)
                table.setCellWidget(row_index, columns["Failure Mode"], mode)
                mech = QComboBox()
                self._add_taxonomy_options(mech, mechanism_options, "failure_mechanism_id", "failure_mechanism_name", row.get("failure_mechanism_id"), editable=True)
                table.setCellWidget(row_index, columns["Failure Mechanism"], mech)

            population = QLineEdit(str(row.get("modeled_population_name") or "Auto-create from selected asset + mode/mechanism on save"))
            population.setReadOnly(True)
            table.setCellWidget(row_index, columns["Modeled Population"], population)

            include = QCheckBox()
            include.setChecked(bool(row.get("include_in_weibull_candidate")) or (not is_pm and current_category == "INCLUDED_FAILURE") or (is_pm and current_category == "INCLUDED_PM_RESET_EVENT" and row.get("pm_reset_inclusion_decision") == "APPROVED_RESET"))
            table.setCellWidget(row_index, columns["Include in Weibull Candidate"], include)
            initial_payloads[int(row["mapped_record_id"])] = self._disposition_payload_from_table(table, row_index)

        table.setProperty("initial_payloads", initial_payloads)
        self._install_check_all_weibull_candidate_shortcut(table)
        for col_index in range(len(DISPLAY_COLUMNS)):
            table.setColumnWidth(col_index, 170)
        for col_index in range(len(DISPLAY_COLUMNS), table.columnCount()):
            table.setColumnWidth(col_index, 210)
        table.setMinimumHeight(460)
        table.horizontalHeader().setStretchLastSection(True)
        table_blocker.unblock()
        table.setUpdatesEnabled(True)
        panel_layout.addWidget(table)

        disposition_actions = QWidget()
        disposition_actions_layout = QHBoxLayout(disposition_actions)
        disposition_actions_layout.setContentsMargins(0, 0, 0, 0)
        disposition_actions_layout.setSpacing(10)
        previous_page = make_button("← Previous Page")
        previous_page.setEnabled(page_index > 0)
        previous_page.clicked.connect(lambda: self._change_disposition_page(layout, kind, disposition_scope, page_index - 1, table))
        next_page = make_button("Next Page →")
        next_page.setEnabled(end_row < displayed_row_count)
        next_page.clicked.connect(lambda: self._change_disposition_page(layout, kind, disposition_scope, page_index + 1, table))
        page_status = label(
            f"Page {page_index + 1} of {max_page_index + 1}",
            size=12,
            color_role="mutedLabel",
        )
        disposition_actions_layout.addWidget(previous_page)
        disposition_actions_layout.addStretch(1)
        disposition_actions_layout.addWidget(page_status, alignment=Qt.AlignmentFlag.AlignCenter)
        disposition_actions_layout.addStretch(1)
        disposition_actions_layout.addWidget(next_page)
        panel_layout.addWidget(disposition_actions)
        layout.addWidget(self._card("REL disposition editor", panel))

        bottom_actions = QWidget()
        bottom_actions_layout = QHBoxLayout(bottom_actions)
        bottom_actions_layout.setContentsMargins(0, 12, 0, 0)
        bottom_actions_layout.setSpacing(10)
        download_excel = make_button("Download Excel")
        download_excel.clicked.connect(lambda: self._download_disposition_excel(kind))
        upload_excel = make_button("Disposition via Excel")
        upload_excel.clicked.connect(lambda: self._upload_disposition_excel(kind))
        save = make_button("Save Dispositions", primary=True)
        save.clicked.connect(lambda: self._save_disposition_table(table))
        bottom_actions_layout.addStretch(1)
        bottom_actions_layout.addWidget(download_excel)
        bottom_actions_layout.addWidget(upload_excel)
        bottom_actions_layout.addWidget(save)
        layout.addWidget(bottom_actions)

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _disposition_table_changed(self, table: QTableWidget) -> bool:
        mapped_ids = table.property("mapped_ids") or []
        initial_payloads = table.property("initial_payloads") or {}
        for row_index, mapped_record_id in enumerate(mapped_ids):
            if self._disposition_payload_from_table(table, row_index) != initial_payloads.get(int(mapped_record_id)):
                return True
        return False

    def _change_disposition_page(self, layout: QVBoxLayout, kind: str, disposition_scope: str, page_index: int, table: QTableWidget) -> None:
        if self._disposition_table_changed(table):
            answer = QMessageBox.question(
                self,
                "Unsaved disposition changes",
                "This page has unsaved disposition changes. Continue without saving them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._clear_layout(layout)
        self._run_with_loading(
            "Loading disposition page…",
            lambda: self._build_disposition_editor(layout, kind, disposition_scope, page_index=page_index),
        )

    def _install_check_all_weibull_candidate_shortcut(self, table: QTableWidget) -> None:
        shortcut = QShortcut(QKeySequence("Shift+W,A,D"), table)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda: self._check_all_weibull_candidate_boxes(table))
        table._check_all_weibull_candidate_shortcut = shortcut

    def _check_all_weibull_candidate_boxes(self, table: QTableWidget) -> None:
        columns = table.property("field_columns") or {}
        include_column = columns.get("Include in Weibull Candidate")
        if include_column is None:
            return
        checked_count = 0
        for row_index in range(table.rowCount()):
            include_widget = table.cellWidget(row_index, include_column)
            if isinstance(include_widget, QCheckBox):
                include_widget.setChecked(True)
                checked_count += 1
        self.statusBar().showMessage(f"Checked {checked_count} Include in Weibull Candidate box(es).", 4000)

    def _download_disposition_excel(self, kind: str) -> None:
        if not self.selected_asset_number:
            QMessageBox.warning(self, "Select an asset", "Select an Asset Number before downloading Excel.")
            return
        asset_number = self.selected_asset_number
        safe_asset = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in asset_number).strip("_") or "asset"
        default_name = f"{safe_asset}_{kind}_dispositions.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Download Excel", default_name, "Excel Files (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path = f"{path}.xlsx"

        def finished(count: object) -> None:
            QMessageBox.information(self, "Excel downloaded", f"Downloaded {int(count or 0)} disposition rows to:\n{path}")

        self._run_in_background(
            "Downloading disposition Excel…",
            lambda: self.life_data_service.export_disposition_excel(asset_number, kind, path),
            finished,
            error_title="Excel download failed",
        )

    def _upload_disposition_excel(self, kind: str) -> None:
        if not self.selected_asset_number:
            QMessageBox.warning(self, "Select an asset", "Select an Asset Number before uploading Excel.")
            return
        asset_number = self.selected_asset_number
        path, _ = QFileDialog.getOpenFileName(self, "Disposition via Excel", "", "Excel Files (*.xlsx)")
        if not path:
            return

        def finished(count: object) -> None:
            self._refresh_life_summary()
            QMessageBox.information(self, "Excel dispositions imported", f"Imported {int(count or 0)} disposition rows from:\n{path}")
            if self.selected_asset_number == asset_number:
                self._open_disposition_page(kind)

        self._run_in_background(
            "Importing disposition Excel…",
            lambda: self.life_data_service.import_disposition_excel(asset_number, kind, path),
            finished,
            error_title="Excel import failed",
        )

    def _disposition_payload_from_table(self, table: QTableWidget, row_index: int) -> dict:
        kind = table.property("disposition_kind")
        mapped_ids = table.property("mapped_ids") or []
        columns = table.property("field_columns") or {}
        is_pm = kind == "pm"
        notes_widget = table.cellWidget(row_index, columns["Disposition Notes"])
        category_widget = table.cellWidget(row_index, columns["Disposition Category"])
        class_widget = table.cellWidget(row_index, columns["Record Class"])
        include_widget = table.cellWidget(row_index, columns["Include in Weibull Candidate"])
        payload = {
            "mapped_record_id": int(mapped_ids[row_index]),
            "kind": kind,
            "disposition_category": category_widget.currentText(),
            "disposition_text": notes_widget.toPlainText(),
            "record_class_final": class_widget.currentText(),
            "include_in_weibull_candidate": include_widget.isChecked(),
        }
        if is_pm:
            decision_widget = table.cellWidget(row_index, columns["PM Reset Decision"])
            mode_widget = table.cellWidget(row_index, columns["Reset Target Failure Mode"])
            mech_widget = table.cellWidget(row_index, columns["Reset Target Failure Mechanism"])
            rationale_widget = table.cellWidget(row_index, columns["PM Reset Renewal Rationale / Evidence"])
            payload.update({
                "pm_reset_decision": decision_widget.currentText(),
                "pm_reset_rationale": rationale_widget.toPlainText(),
                "reset_target_failure_mode_id": self._combo_id(mode_widget),
                "reset_target_failure_mechanism_id": self._combo_id(mech_widget),
            })
        else:
            mode_widget = table.cellWidget(row_index, columns["Failure Mode"])
            mech_widget = table.cellWidget(row_index, columns["Failure Mechanism"])
            payload.update({
                "failure_mode_id": self._combo_id(mode_widget),
                "failure_mechanism_id": self._combo_id(mech_widget),
                "failure_mode_text": mode_widget.currentText(),
                "failure_mechanism_text": mech_widget.currentText(),
            })
        return payload

    def _save_disposition_table(self, table: QTableWidget) -> None:
        mapped_ids = table.property("mapped_ids") or []
        initial_payloads = table.property("initial_payloads") or {}
        changed_payloads = []
        for row_index, mapped_record_id in enumerate(mapped_ids):
            payload = self._disposition_payload_from_table(table, row_index)
            if payload != initial_payloads.get(int(mapped_record_id)):
                changed_payloads.append(payload)
        if not changed_payloads:
            QMessageBox.information(self, "No changes", "No disposition rows changed, so nothing needed to be saved.")
            return

        def finished(saved_count: object) -> None:
            table.setProperty("initial_payloads", initial_payloads | {payload["mapped_record_id"]: payload for payload in changed_payloads})
            self._refresh_life_summary()
            QMessageBox.information(self, "Dispositions saved", f"Saved {int(saved_count or 0)} changed REL disposition row(s) to event_disposition in GREMLIN.db.")

        self._run_in_background(
            "Saving dispositions…",
            lambda: self.life_data_service.save_dispositions(changed_payloads),
            finished,
            error_title="Disposition save failed",
        )

    def _calculate_all_life_mle_results(self) -> None:
        if not self.selected_asset_number:
            QMessageBox.warning(self, "Select an asset", "Select an Asset Number before calculating all Weibull MLE results.")
            return
        password, accepted = QInputDialog.getText(
            self,
            "Password required",
            "Enter password to calculate MLE beta/eta for every available failure mode and mechanism on this asset:",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        if password != "1336":
            QMessageBox.warning(self, "Incorrect password", "The password was incorrect. No Weibull MLE calculations were performed.")
            return
        asset_number = self.selected_asset_number

        def finished(summary_obj: object) -> None:
            if self.selected_asset_number != asset_number or not isinstance(summary_obj, dict):
                return
            cleared_current_result = self.latest_analysis_result is not None
            self.latest_analysis_result = None
            self._clear_life_workspace()
            self._refresh_life_summary()
            completed = int(summary_obj.get("completed", 0) or 0)
            failed = int(summary_obj.get("failed", 0) or 0)
            total = int(summary_obj.get("total", 0) or 0)
            message = f"Calculated and saved Weibull MLE beta/eta results for {completed} of {total} available failure mode/mechanism group(s)."
            if cleared_current_result:
                message += "\n\nThe displayed analysis was cleared because its saved result IDs may have been rebuilt during the bulk run. Re-run that group to view the refreshed MLE values."
            errors = summary_obj.get("errors") or []
            if failed and errors:
                message += "\n\nGroups needing review:\n" + "\n".join(str(error) for error in errors[:8])
                if len(errors) > 8:
                    message += f"\n…and {len(errors) - 8} more."
            QMessageBox.information(self, "All MLE calculations complete", message)

        self._run_in_background(
            "Calculating all Weibull MLE results…",
            lambda: self.life_data_service.calculate_all_weibull_results(asset_number),
            finished,
            error_title="All MLE calculations failed",
        )

    def _perform_life_analysis(self) -> None:
        if not self.selected_asset_number:
            return
        analysis_type = self.analysis_type_combo.currentText().strip() if hasattr(self, "analysis_type_combo") else "Weibull Analysis"
        if analysis_type == "Availability Analysis":
            self._perform_availability_analysis()
            return
        asset_number = self.selected_asset_number

        def choose_group(group_options_obj: object) -> None:
            group_options = list(group_options_obj or [])
            if self.selected_asset_number != asset_number:
                return
            if not group_options:
                QMessageBox.warning(
                    self,
                    "Select a failure group",
                    "No failure modes or failure mechanisms are ready for Weibull analysis. Disposition failures and PM resets with failure-mode/mechanism selections first.",
                )
                return
            display_items = [
                f"{('Failure mechanism' if option['grouping_level'] == 'FAILURE_MECHANISM' else 'Failure mode')}: "
                f"{option['label']} ({option['failure_count']} failures, {option['reset_count']} PM resets)"
                for option in group_options
            ]
            selected_item, accepted = QInputDialog.getItem(
                self,
                "Select failure group",
                "Choose the failure mechanism or failure mode to analyze:",
                display_items,
                0,
                False,
            )
            if not accepted or not selected_item:
                return
            selected_group = group_options[display_items.index(selected_item)]
            self._run_life_analysis_for_group(selected_group)

        self._run_in_background(
            "Loading Weibull groups…",
            lambda: self.life_data_service.weibull_group_options(asset_number),
            choose_group,
            error_title="Analysis groups unavailable",
        )

    def _perform_pareto_mechanism_analysis(self, pareto_row: dict) -> None:
        """Run and render Weibull results for the clicked Pareto failure mechanism."""

        if not self.selected_asset_number:
            return
        failure_mode_id = pareto_row.get("failure_mode_id")
        failure_mechanism_id = pareto_row.get("failure_mechanism_id")
        if failure_mode_id is None or failure_mechanism_id is None:
            QMessageBox.warning(self, "Analysis group unavailable", "The selected Pareto bar does not have a complete failure mode/mechanism selection.")
            return
        selected_group = {
            "grouping_level": "FAILURE_MECHANISM",
            "failure_mode_id": int(failure_mode_id),
            "failure_mechanism_id": int(failure_mechanism_id),
            "label": f"{pareto_row.get('failure_mode_name') or 'Failure mode'} / {pareto_row.get('failure_mechanism_name') or 'Failure mechanism'}",
        }
        self._run_life_analysis_for_group(selected_group, loading_message="Running clicked mechanism Weibull analysis…")

    def _run_life_analysis_for_group(self, selected_group: dict, *, loading_message: str = "Running Weibull analysis…") -> None:
        if not self.selected_asset_number:
            return
        asset_number = self.selected_asset_number

        def finished(result: object) -> None:
            if self.selected_asset_number == asset_number and isinstance(result, AnalysisResultView):
                self._render_life_analysis_result(result)

        self._run_in_background(
            loading_message,
            lambda: self.life_data_service.perform_weibull_analysis(
                asset_number,
                grouping_level=selected_group["grouping_level"],
                failure_mode_id=selected_group["failure_mode_id"],
                failure_mechanism_id=selected_group["failure_mechanism_id"],
            ),
            finished,
            error_title="Analysis not available",
        )

    def _render_life_analysis_result(self, result: AnalysisResultView) -> None:
        self.latest_analysis_result = result
        self._clear_life_workspace()
        analysis = QWidget()
        analysis_layout = QVBoxLayout(analysis)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        analysis_layout.setSpacing(12)
        analysis_layout.addWidget(label(result.analysis_label or "Selected failure group", size=16, weight=QFont.Weight.DemiBold))
        analysis_layout.addWidget(label(f"MLE beta: {result.beta_mle:.4g}    MLE eta: {result.eta_mle:.4g} hours", size=18, weight=QFont.Weight.DemiBold))
        analysis_layout.addWidget(label(self._confidence_interval_text(result), color_role="mutedLabel", wrap=True))
        analysis_layout.addWidget(label(f"Observations: {result.total_observation_count} total, {result.failure_count} failures, {result.censored_count} right-censored.", color_role="mutedLabel", wrap=True))
        weibull_data_table = self._build_weibull_data_table(result)
        graph = WeibullGraphWidget(result, lambda observation_id: self._select_weibull_data_row(weibull_data_table, observation_id))

        adjust_row = QWidget()
        adjust_layout = QHBoxLayout(adjust_row)
        adjust_layout.setContentsMargins(0, 0, 0, 0)
        beta_input = QDoubleSpinBox()
        beta_input.setRange(0.01, 100.0)
        beta_input.setDecimals(4)
        beta_input.setSingleStep(0.2)
        beta_input.setValue(result.beta_mle)
        eta_input = QDoubleSpinBox()
        eta_input.setRange(0.01, 1_000_000_000.0)
        eta_input.setDecimals(2)
        eta_input.setSingleStep(100.0)
        eta_input.setValue(result.eta_mle)
        beta_input.valueChanged.connect(lambda value: graph.set_parameters(value, eta_input.value()))
        eta_input.valueChanged.connect(lambda value: graph.set_parameters(beta_input.value(), value))
        reason_input = QLineEdit()
        reason_input.setPlaceholderText("Adjustment reason based on empirical data points…")
        save_adjusted = make_button("Save Adjusted Parameters", primary=True)
        save_adjusted.clicked.connect(lambda: self._save_adjusted_parameters(result.result_id, beta_input.value(), eta_input.value(), reason_input.text()))
        adjust_layout.addWidget(label("Adjusted beta (±0.2)"))
        adjust_layout.addWidget(beta_input)
        adjust_layout.addWidget(label("Adjusted eta (±100 h)"))
        adjust_layout.addWidget(eta_input)
        adjust_layout.addWidget(reason_input, stretch=1)
        adjust_layout.addWidget(save_adjusted)
        analysis_layout.addWidget(adjust_row)

        graph.setMinimumHeight(650)
        analysis_layout.addWidget(graph)
        analysis_layout.addWidget(label("Green lines show the MLE fit; yellow lines show approximate 95% confidence-interval fits where shown. The hazard and PDF panes intentionally show only the MLE curve. Click any plotted failure or censored point to jump to the source Weibull data row below.", color_role="mutedLabel", wrap=True))
        analysis_layout.addWidget(self._card("Results Interpretation Summary", self._build_interpretation_table(result), footer="Recommendations are based on beta, eta, MTTF, and approximate 95% confidence intervals for the fitted Weibull parameters."))
        analysis_layout.addWidget(self._card("Weibull Data Used for Graphs", weibull_data_table, footer="Rows are the observations included in the Weibull fit. White points are completed failures; red points are right-censored observations."))
        self.life_workspace_layout.addWidget(self._card("Weibull Analysis Results", analysis, footer="Weibull fits are built for the selected failure mode/mechanism population. Adjusted beta/eta are saved separately in weibull_parameter_adjustment and never overwrite weibull_result.beta_mle or eta_mle."))
        self._refresh_life_summary()


    def _perform_availability_analysis(self) -> None:
        """Run the Availability Analysis type for the currently selected asset."""

        if not self.selected_asset_number:
            return
        asset_number = self.selected_asset_number
        settings = self.availability_repository.load_settings()
        selected_year = int(settings.get("selected_year") or datetime.now().year)
        utc_offset_setting = settings.get("utc_offset_hours")
        utc_offset_hours = 5.0 if utc_offset_setting is None else float(utc_offset_setting)

        def calculate() -> list[object]:
            repository = AvailabilityRepository(self.availability_repository.db_path)
            results = AvailabilityCalculator(repository).calculate_availability(selected_year, utc_offset_hours)
            repository.save_results(selected_year, results)
            return [result for result in results if result.asset_number == asset_number]

        def render(asset_results_obj: object) -> None:
            asset_results = list(asset_results_obj or [])
            if self.selected_asset_number != asset_number:
                return
            self._render_availability_results(asset_number, asset_results)

        self._run_in_background("Running availability analysis…", calculate, render, error_title="Availability analysis not available")

    def _render_availability_results(self, asset_number: str, asset_results: list[object]) -> None:
        self._clear_life_workspace()
        if not asset_results:
            QMessageBox.warning(
                self,
                "Availability asset not configured",
                "This asset is not currently assigned to an included Availability Analysis asset group. "
                "Add it to an included availability asset group, then run Availability Analysis again.",
            )
            return
        analysis = QWidget()
        analysis_layout = QVBoxLayout(analysis)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        analysis_layout.setSpacing(12)
        group_names = ", ".join(sorted({result.asset_group for result in asset_results}))
        total_wo = sum(result.total_wo_count for result in asset_results)
        total_zero = sum(result.zero_downtime_wo_count for result in asset_results)
        total_adjusted_downtime = sum(result.adjusted_downtime_hours for result in asset_results)
        avg_adjusted_availability = sum(result.adjusted_availability_percent for result in asset_results) / len(asset_results)
        lowest_adjusted_availability = min(result.adjusted_availability_percent for result in asset_results)
        analysis_layout.addWidget(label(f"Availability Analysis — Asset {asset_number}", size=16, weight=QFont.Weight.DemiBold))
        analysis_layout.addWidget(label(f"Configured group(s): {group_names}", color_role="mutedLabel", wrap=True))
        metrics = QWidget()
        metrics_layout = QGridLayout(metrics)
        metrics_layout.setContentsMargins(0, 0, 0, 0)
        metric_values = (
            ("Total Work Orders", str(total_wo)),
            ("0-Downtime WOs", str(total_zero)),
            ("0-Downtime WO %", availability_pct(total_zero / total_wo if total_wo else 0.0)),
            ("Total Adjusted Downtime", f"{total_adjusted_downtime:.1f} h"),
            ("Average Adjusted Availability", availability_pct(avg_adjusted_availability)),
            ("Lowest Adjusted Availability", availability_pct(lowest_adjusted_availability)),
        )
        for index, (title, value) in enumerate(metric_values):
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.addWidget(label(value, size=22, weight=QFont.Weight.Bold))
            box_layout.addWidget(label(title, size=12, color_role="mutedLabel", wrap=True))
            metrics_layout.addWidget(self._card(title, box), index // 3, index % 3)
        analysis_layout.addWidget(metrics)
        headers = (
            "Month", "Scheduled Hours", "Manual OT", "Adjusted Scheduled", "Direct Downtime",
            "Linked Downtime", "Adjusted Downtime", "Raw Availability", "Adjusted Availability",
            "WO Count", "0-Downtime WOs", "Overlap Count", "Note",
        )
        table_rows = [
            (
                result.month_label,
                f"{result.scheduled_hours:.1f}",
                f"{result.manual_ot_hours:.1f}",
                f"{result.adjusted_scheduled_hours:.1f}",
                f"{result.downtime_hours:.1f}",
                f"{result.linked_downtime_hours:.1f}",
                f"{result.adjusted_downtime_hours:.1f}",
                availability_pct(result.availability_percent),
                availability_pct(result.adjusted_availability_percent),
                result.total_wo_count,
                result.zero_downtime_wo_count,
                result.overlap_count,
                result.zero_no_entry_note,
            )
            for result in asset_results
        ]
        table = read_only_table_view(headers, table_rows, min_height=min(460, 95 + max(1, len(asset_results)) * 34), column_widths=(115, 125, 95, 145, 130, 130, 135, 120, 140, 85, 115, 105, 250))
        analysis_layout.addWidget(table)
        analysis_layout.addWidget(label(
            "Availability Analysis uses adjusted availability from the SQLite-backed availability calculator: type 4 rows are excluded, "
            "manual OT and linked downtime rules are applied, and results are saved to availability_results.",
            color_role="mutedLabel",
            wrap=True,
        ))
        self.life_workspace_layout.addWidget(self._card("Availability Analysis Results", analysis))

    def _confidence_interval_text(self, result: AnalysisResultView) -> str:
        beta_ci = "not available"
        eta_ci = "not available"
        if result.beta_lower_ci is not None and result.beta_upper_ci is not None:
            beta_ci = f"{result.beta_lower_ci:.4g} to {result.beta_upper_ci:.4g}"
        if result.eta_lower_ci is not None and result.eta_upper_ci is not None:
            eta_ci = f"{result.eta_lower_ci:.4g} to {result.eta_upper_ci:.4g} hours"
        mttf_text = f"{result.mean_time_to_failure:.4g} hours" if result.mean_time_to_failure is not None else "not available"
        return f"Approx. 95% CI — beta: {beta_ci}; eta: {eta_ci}; MTTF: {mttf_text}."

    def _build_interpretation_table(self, result: AnalysisResultView) -> QWidget:
        rows = result.interpretation_summary or []
        summary = QWidget()
        layout = QGridLayout(summary)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        layout.setColumnStretch(0, 18)
        layout.setColumnStretch(1, 16)
        layout.setColumnStretch(2, 66)

        headers = ("Metric", "Value", "Interpretation / Recommended Action")
        for column_index, header in enumerate(headers):
            layout.addWidget(label(header, size=12, weight=QFont.Weight.Bold, wrap=True), 0, column_index)

        if not rows:
            layout.addWidget(label("No interpretation summary is available for this Weibull result.", color_role="mutedLabel", wrap=True), 1, 0, 1, 3)
            return summary

        for row_index, row in enumerate(rows, start=1):
            values = (row.get("metric", ""), row.get("value", ""), row.get("recommendation", ""))
            for column_index, value in enumerate(values):
                cell = label(str(value or "—"), size=12, color_role="mutedLabel" if column_index == 2 else None, wrap=True)
                cell.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                layout.addWidget(cell, row_index, column_index, alignment=Qt.AlignmentFlag.AlignTop)
        return summary

    def _build_weibull_data_table(self, result: AnalysisResultView) -> QTableView:
        headers = (
            "#",
            "Observation ID",
            "Type",
            "Life Hours",
            "Failure",
            "Right Censored",
            "Start Datetime",
            "End/Cutoff Datetime",
            "Note",
        )
        table_rows = []
        observation_rows: dict[int, int] = {}
        for row_index, observation in enumerate(result.observations):
            end_or_cutoff = observation.get("end_datetime") or observation.get("analysis_cutoff_datetime") or ""
            observation_id = int(observation.get("weibull_observation_id") or 0)
            observation_rows[observation_id] = row_index
            table_rows.append((
                observation.get("ordered_index"),
                observation_id,
                observation.get("observation_type"),
                self._format_table_number(observation.get("life_hours_for_weibull")),
                "Yes" if int(observation.get("failure_indicator") or 0) else "No",
                "Yes" if int(observation.get("is_right_censored") or 0) else "No",
                observation.get("start_datetime") or "",
                end_or_cutoff,
                observation.get("weibull_life_note") or "",
            ))
        table = read_only_table_view(
            headers,
            table_rows,
            min_height=min(420, 95 + max(1, len(result.observations)) * 32),
            column_widths=(55, 110, 130, 95, 75, 120, 155, 165, 320),
        )
        table.setProperty("observation_rows", observation_rows)
        return table

    def _select_weibull_data_row(self, table: QTableView, observation_id: int) -> None:
        observation_rows = table.property("observation_rows") or {}
        row_index = observation_rows.get(int(observation_id))
        if row_index is None:
            return
        model_index = table.model().index(row_index, 0)
        table.clearSelection()
        table.selectRow(row_index)
        table.scrollTo(model_index, QTableView.ScrollHint.PositionAtCenter)
        ancestor = table.parent()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                ancestor.ensureWidgetVisible(table, 0, 80)
                break
            ancestor = ancestor.parent()
        table.setFocus()
        self.statusBar().showMessage(f"Selected Weibull observation {observation_id} in the data table.", 4000)

    def _format_table_number(self, value: object) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        if abs(number) >= 1000:
            return f"{number:,.2f}"
        return f"{number:.4g}"

    def _save_adjusted_parameters(self, result_id: int, beta: float, eta: float, reason: str) -> None:
        self._begin_loading("Saving adjusted Weibull parameters…")
        try:
            self.life_data_service.save_parameter_adjustment(result_id, beta, eta, reason)
            self._end_loading()
            QMessageBox.information(self, "Adjusted parameters saved", "Adjusted beta and eta were saved without overwriting the MLE result.")
        except Exception as exc:  # noqa: BLE001
            self._show_write_failure("Adjustment save failed", exc)
        finally:
            self._end_loading()

    def _build_perform_analysis_page(self) -> QWidget:
        page, layout = self._page_shell(
            "Perform an Analysis",
            "This page is reserved for the future analysis workflow. No calculation functions are connected yet.",
        )
        layout.addWidget(self._card(
            "Analysis workspace placeholder",
            label("Life-data fitting tools will be added here in a later build.", color_role="mutedLabel", wrap=True),
        ))
        layout.addStretch(1)
        return page

    def _build_failure_classification_page(self) -> QWidget:
        page, layout = self._page_shell(
            "Failure Classification",
            "This page is reserved for the future failure-mode classification workflow. No classification functions are connected yet.",
        )
        layout.addWidget(self._card(
            "Classification workspace placeholder",
            label("Failure tagging and categorization controls will be added here in a later build.", color_role="mutedLabel", wrap=True),
        ))
        layout.addStretch(1)
        return page

    def _build_metrics_page(self) -> QWidget:
        page, layout = self._page_shell(
            "Metrics",
            "Dashboard-style reliability indicators sourced from the GREMLIN repository/service layer.",
        )
        data = self.reliability_service.get_metrics_dashboard_data()
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        for position, metric in enumerate(data["cards"]):
            metric_widget = QWidget()
            metric_layout = QVBoxLayout(metric_widget)
            metric_layout.setContentsMargins(0, 0, 0, 0)
            metric_layout.addWidget(label(metric.value, size=30, weight=QFont.Weight.Bold))
            metric_layout.addWidget(label(metric.detail, color_role="mutedLabel", wrap=True))
            grid.addWidget(self._card(metric.label, metric_widget), position // 2, position % 2)
        layout.addLayout(grid)
        layout.addWidget(self._build_analysis_table(data["analyses"]))
        layout.addStretch(1)
        return page

    def _build_availability_page(self) -> QWidget:
        page, layout = self._page_shell(
            "Availability",
            "Availability dashboard and reliability metrics."
        )

        # layout.addWidget(self._card(
        #     "Availability Dashboard",
        #     label("✅ Availability page connected successfully (TEST)")
        # ))

        layout.addStretch(1)



        #Run button
        run_btn = make_button("Run Availability Analysis", primary=True)
        run_btn.clicked.connect(self._run_availability_analysis) #when button is clicked, runs the "run_availability_analysis function

        # Creates the dropdown with all assets in .db file
        self.availability_asset_combo = QComboBox()

        asset_options = self.life_data_service.asset_number_options()  ##call on life_data_service to retrieve "asset_number_options()", which is list of all assets

        for asset in asset_options:  # For loop that creates rows for dropdown
            display_text = f"{asset['asset_number']} - {asset['asset_name']}"
            self.availability_asset_combo.addItem(display_text, asset['asset_number'])

            asset_number = self.availability_asset_combo.currentData()  # This line fills the dropdown

        dropdown_label = QLabel("Select Assets that you want to view Availability for:")
        self.selected_assets_list = QListWidget()
        # add to Asset List
        add_btn = make_button("Add Asset")
        add_btn.clicked.connect(self._add_selected_asset) #when button is clicked, runs the "run_availability_analysis function

        # Row: dropdown + button
        row = QHBoxLayout()
        row.addWidget(self.availability_asset_combo)
        row.addWidget(add_btn)



        #"Remove asset" button
        remove_btn = make_button("Remove Selected")
        remove_btn.clicked.connect(
            lambda: self.selected_assets_list.takeItem(
                self.selected_assets_list.currentRow()
            )
        )

        #"Clear all" button

        clear_btn = make_button("Clear All")
        clear_btn.clicked.connect(self.selected_assets_list.clear)

        #Dedicated graph area
        self.availability_results_container = QWidget()
        self.availability_results_layout = QVBoxLayout(self.availability_results_container)
        self.availability_results_container.setMinimumHeight(400)




        layout.addWidget(run_btn)
        layout.addWidget(dropdown_label)
        layout.addWidget(self.availability_asset_combo)
        layout.addLayout(row)
        layout.addWidget(QLabel("Selected Assets:")) # Label
        layout.addWidget(self.selected_assets_list) # List box
        layout.addWidget(remove_btn)
        layout.addWidget(clear_btn)
        layout.addWidget(self.availability_results_container)

        return page

    def _add_selected_asset(self):
        asset_number = self.availability_asset_combo.currentData()
        display_text = self.availability_asset_combo.currentText()

        if not asset_number:
            return

        # Prevent duplicates
        for i in range(self.selected_assets_list.count()):
            if self.selected_assets_list.item(i).data(1) == asset_number:
                return

        # Limit to 10
        if self.selected_assets_list.count() >= 10:
            QMessageBox.warning(self, "Limit Reached", "You can select up to 10 assets.")
            return

        item = QListWidgetItem(display_text)
        item.setData(1, asset_number)  # store real value

        self.selected_assets_list.addItem(item)

    def _run_availability_analysis(self):

            print("RUN CLICKED")

            print("DF EXISTS:", hasattr(self, "df"))
            if hasattr(self, "df"):
                print("DF COLUMNS:", self.df.columns.tolist())

            asset_numbers = []

            # ✅ 1. Get selected assets
            for i in range(self.selected_assets_list.count()):
                item = self.selected_assets_list.item(i)
                asset_numbers.append(str(item.data(1)))  # ensure string

            # ✅ fallback to dropdown
            if not asset_numbers:
                asset = self.availability_asset_combo.currentData()
                if asset:
                    asset_numbers = [str(asset)]
            asset_numbers = [str(int(float(x))) for x in asset_numbers]

            if not asset_numbers:
                QMessageBox.warning(self, "No Selection", "Please select at least one asset.")
                return

            # ✅ 2. Prepare dataframe
            df = self.df.copy()

            # (STEP 1 assumed already done in __init__)
            # df.columns are lowercase

            df["asset number"] = (
                pd.to_numeric(df["asset number"], errors="coerce")
                .fillna(0)
                .astype(int)
                .astype(str)
            )

            # ✅ Filter selected assets

            df = df[df["asset number"].isin(asset_numbers)]

            # ✅ DEBUG PRINTS (ADD RIGHT HERE)
            print("SELECTED:", asset_numbers)
            print("UNIQUE DF ASSETS:", df["asset number"].unique()[:10])
            print("DF AFTER FILTER:", df.shape)

            if df.empty:
                QMessageBox.warning(self, "No Data", "No data found for selected assets.")
                return

            # ✅ STEP 2 — PREP FOR SEABORN

            # ✅ Proper datetime handling (CRITICAL)
            df["month_dt"] = pd.to_datetime(df["month date"], errors="coerce")

            # ✅ Safe label for plotting (DO NOT use datetime directly)
            df["month"] = df["month_dt"].dt.strftime("%b %Y")

            df["asset"] = df["asset number"]

            # ✅ CRITICAL: force numeric (prevents crash)
            df["downtime"] = pd.to_numeric(
                df["adjusted downtime hours"],
                errors="coerce"
            )

            df["availability"] = pd.to_numeric(
                df["adjusted availability %"],
                errors="coerce"
            ) * 100  # ✅ convert to actual percent

            # ✅ REMOVE bad rows (prevents crash)
            df = df.dropna(subset=["month_dt", "month", "asset", "downtime"])

            # ✅ Sort months correctly
            df = df.sort_values("month_dt")

            # ✅ Store clean dataframe (used in plotting)
            self._current_df = df

            results = []

            for asset in asset_numbers:
                asset_df = df[df["asset"] == asset]

                total_sched = asset_df["adjusted scheduled hours"].sum()
                total_down = asset_df["adjusted downtime hours"].sum()

                availability = (
                    (total_sched - total_down) / total_sched
                    if total_sched > 0 else 0
                )

                results.append({
                    "asset": asset,
                    "availability": availability * 100,
                    "downtime": total_down,
                    "scheduled_hours": total_sched
                })

            # ✅ THIS WAS MISSING (CRITICAL)
            self._display_results(results)

    def _display_results(self, results):

        # ✅ Convert results → DataFrame (CRITICAL FIX)
        df_results = pd.DataFrame(results)

        # ✅ Create table
        table = QTableWidget(len(df_results), 3)
        table.setHorizontalHeaderLabels(["Asset", "Availability (%)", "Downtime"])

        for row, r in df_results.iterrows():
            table.setItem(row, 0, QTableWidgetItem(str(r["asset"])))
            table.setItem(row, 1, QTableWidgetItem(f"{r['availability']:.2f}%"))
            table.setItem(row, 2, QTableWidgetItem(f"{r['downtime']:.1f}"))

        # ✅ Clear previous results safely
        self._clear_availability_results()

        # ✅ Add table to correct layout (NOT self.layout())
        self.availability_results_layout.addWidget(table)

        # ✅ Create chart (SAFE call)
        chart = self._plot_multi_asset(self._current_df)
        chart.setMinimumHeight(400)

        self.availability_results_layout.addWidget(chart)

        # ✅ Store for interaction (click → highlight)
        self._plot_data = df_results.to_dict("records")
        self.availability_table = table

    def _plot_multi_asset(self, df):

        fig = Figure()
        ax = fig.add_subplot(111)

        # ✅ ✅ REPLACE OLD GROUPBY WITH THIS

        df_grouped = (
            df.groupby(["month_dt", "month", "asset"], as_index=False)["availability"]
            .mean()  # ✅ use mean instead of sum
            .sort_values("month_dt")
        )

        # ✅ Plot

        sns.barplot(
            data=df_grouped,
            x="month",
            y="availability",  # ✅ now plotting availability
            hue="asset",
            order=df_grouped["month"].unique(),
            ax=ax
        )

        # ✅ Store bars for click interaction (you’ll need this later)
        self._bars = ax.patches

        ax.set_title("Monthly Adjusted Downtime by Asset")
        ax.set_ylabel("Adjusted Availability %")
        ax.set_xlabel("Month")

        ax.tick_params(axis='x', rotation=45)

        fig.tight_layout()

        canvas = FigureCanvas(fig)

        # ✅ Connect click event
        canvas.mpl_connect("button_press_event", self._on_availability_plot_click)

        return canvas

    def _clear_availability_results(self):
                        if not hasattr(self, "availability_results_layout"):
                            return

                        while self.availability_results_layout.count():
                            item = self.availability_results_layout.takeAt(0)
                            widget = item.widget()
                            if widget:
                                widget.setParent(None)  # ✅ REQUIRED
                                widget.deleteLater()

    def _on_availability_plot_click(self, event):

                if event.inaxes is None:
                    return

                index = int(round(event.xdata))

                if index < 0 or index >= len(self._plot_data):
                    return

                # ✅ Reset all bars
                for bar in self._bars:
                    bar.set_color("blue")

                # ✅ Highlight selected bar
                self._bars[index].set_color("red")

                event.canvas.draw()

                selected = self._plot_data[index]
                self._highlight_availability_row(selected)


    def _highlight_availability_row(self, selected):
        if not hasattr(self, "availability_table"):
            return

        asset_name = str(selected["asset"])

        for row in range(self.availability_table.rowCount()):
            item = self.availability_table.item(row, 0)  # assuming col 0 = asset name

            if item and item.text() == asset_name:
                self.availability_table.selectRow(row)
                self.availability_table.scrollToItem(item)
                break



    def _build_docs_page(self) -> QWidget:
        page, layout = self._page_shell(
            "Standards and Documentation",
            "This page is reserved for future standards references and documentation artifacts. No documentation functions are connected yet.",
        )
        layout.addWidget(self._card(
            "Documentation workspace placeholder",
            label("Standards, procedures, and reference links will be added here in a later build.", color_role="mutedLabel", wrap=True),
        ))
        layout.addStretch(1)
        return page

    def _build_settings_page(self) -> QWidget:
        page, layout = self._page_shell(
            "Settings",
            "Desktop configuration placeholders for data paths, Limble credentials, export folders, and visual preferences.",
        )
        cmms_mapping_panel = QWidget()
        cmms_mapping_layout = QVBoxLayout(cmms_mapping_panel)
        cmms_mapping_layout.setContentsMargins(0, 0, 0, 0)
        cmms_mapping_layout.setSpacing(10)
        cmms_mapping_layout.addWidget(label(
            "Refresh mapped CMMS rows from the shared GREMLIN.db source before analyzing or dispositioning life data.",
            color_role="mutedLabel",
            wrap=True,
        ))
        refresh_mapping = make_button("Refresh CMMS mapping")
        refresh_mapping.clicked.connect(self._refresh_mapped_records)
        cmms_mapping_layout.addWidget(refresh_mapping)
        cmms_mapping_layout.addStretch(1)
        layout.addWidget(self._card("CMMS mapping", cmms_mapping_panel))

        settings = (
            ("Data source", "Local sample repositories are active. External database/API connections can be wired into the repository classes."),
            ("Limble integration", "Integration modules remain available under integrations/ and jobs/ for future sync commands."),
            ("Launcher", "Run start_gremlin.bat from the project folder instead of downloading a new batch file each time."),
        )
        for title, description in settings:
            layout.addWidget(self._card(title, label(description, color_role="mutedLabel", wrap=True)))
        layout.addStretch(1)
        return page

    def _build_analysis_table(self, analyses: Iterable[object]) -> QTableWidget:
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(("Analysis", "Status", "Summary", "Values"))
        rows = list(analyses)
        table.setRowCount(len(rows))
        for row, analysis in enumerate(rows):
            values = ", ".join(f"{key}: {value}" for key, value in analysis.values.items())
            for column, value in enumerate((analysis.title, analysis.status, analysis.summary, values)):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, column, item)
        table.resizeColumnsToContents()
        table.setMinimumHeight(170)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _build_failure_table(self) -> QTableWidget:
        data = self.reliability_service.get_failure_classification_data()
        rows = data["classifications"]
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(("Classification", "Count", "Severity", "Description"))
        table.setRowCount(len(rows))
        for row, failure in enumerate(rows):
            for column, value in enumerate((failure.name, failure.count, failure.severity, failure.description)):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, column, item)
        table.resizeColumnsToContents()
        table.setMinimumHeight(165)
        table.horizontalHeader().setStretchLastSection(True)
        return table


def main() -> int:
    """Launch the native GREMLIN desktop GUI."""

    configure_windows_app_identity()
    app = QApplication(sys.argv)
    app.setApplicationName("GREMLIN Desktop GUI")
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window = GremlinGui()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
