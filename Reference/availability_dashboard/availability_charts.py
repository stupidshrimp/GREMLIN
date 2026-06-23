"""Qt chart rendering for Availability Dashboard results."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget

MONTH_LABELS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


class AvailabilityChartWidget(QWidget):
    """Excel-like combo chart: asset bars plus average and goal lines."""

    def __init__(self, asset_group: str, results: list[dict], goals: dict[str, float], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.asset_group = asset_group
        self.results = results
        self.goals = goals
        self.setMinimumHeight(390)
        self.setMinimumWidth(900)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0d1f14"))
        left, top, right, bottom = 68, 50, 28, 64
        width = max(1, self.width() - left - right)
        height = max(1, self.height() - top - bottom)
        x0, y0 = left, top + height
        painter.setPen(QPen(QColor("#ecfdf5")))
        painter.setFont(QFont("Arial", 15, QFont.Weight.Bold))
        painter.drawText(16, 26, f"{self.asset_group} Availability % by Month")
        painter.setFont(QFont("Arial", 9))
        grid_pen = QPen(QColor("#1f5f3a"), 1)
        painter.setPen(grid_pen)
        for i in range(0, 6):
            y = y0 - int(height * i / 5)
            painter.drawLine(x0, y, x0 + width, y)
            painter.setPen(QPen(QColor("#9bd8b4")))
            painter.drawText(8, y + 4, f"{i * 20}%")
            painter.setPen(grid_pen)
        painter.setPen(QPen(QColor("#9bd8b4")))
        painter.drawLine(x0, top, x0, y0)
        painter.drawLine(x0, y0, x0 + width, y0)

        by_month: dict[str, list[dict]] = defaultdict(list)
        for row in self.results:
            by_month[row["month_label"]].append(row)
        visible_months = [label for label in MONTH_LABELS if by_month.get(label)]
        if not visible_months:
            painter.drawText(left, top + 40, "No calculated availability results to display.")
            painter.end()
            return
        colors = [QColor(c) for c in ("#22c55e", "#84cc16", "#14b8a6", "#38bdf8", "#a78bfa", "#f59e0b", "#f472b6", "#c084fc")]
        month_w = width / len(visible_months)
        avg_points: list[tuple[int, int, float]] = []
        goal_points: list[tuple[int, int]] = []
        for month_index, month_label in enumerate(visible_months):
            rows = sorted(by_month[month_label], key=lambda row: row["asset_number"])
            bar_gap = 3
            group_x = x0 + month_index * month_w
            bar_w = max(6, (month_w - 18) / max(1, len(rows)) - bar_gap)
            values = []
            month_date = rows[0]["month_date"]
            for asset_index, row in enumerate(rows):
                value = max(0.0, min(1.0, float(row["adjusted_availability_percent"])))
                values.append(value)
                bar_h = height * value
                x = int(group_x + 9 + asset_index * (bar_w + bar_gap))
                y = int(y0 - bar_h)
                painter.fillRect(x, y, int(bar_w), int(bar_h), colors[asset_index % len(colors)])
                painter.setPen(QPen(QColor("#ecfdf5")))
                painter.save()
                painter.translate(x + int(bar_w / 2), max(top + 12, y - 5))
                painter.rotate(-45 if len(rows) > 4 else 0)
                painter.drawText(-20, 0, pct(value))
                painter.restore()
            avg = mean(values) if values else 0.0
            avg_points.append((int(group_x + month_w / 2), int(y0 - height * avg), avg))
            goal = max(0.0, min(1.0, float(self.goals.get((self.asset_group, month_date), 0.95))))
            goal_points.append((int(group_x + month_w / 2), int(y0 - height * goal)))
            painter.setPen(QPen(QColor("#9bd8b4")))
            painter.drawText(int(group_x + month_w / 2 - 12), y0 + 24, month_label)
        self._draw_polyline(painter, goal_points, QColor("#ef4444"), 2, markers=False)
        self._draw_polyline(painter, [(x, y) for x, y, _avg in avg_points], QColor("#3b82f6"), 3, markers=True)
        painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        for x, y, avg in avg_points:
            rect_w, rect_h = 48, 18
            painter.fillRect(x - rect_w // 2, y + 8, rect_w, rect_h, QColor("#2563eb"))
            painter.setPen(QPen(QColor("white")))
            painter.drawText(x - rect_w // 2, y + 21, rect_w, rect_h, Qt.AlignmentFlag.AlignCenter, pct(avg))
        painter.setPen(QPen(QColor("#3b82f6")))
        painter.drawText(x0 + width - 190, 24, "● Average")
        painter.setPen(QPen(QColor("#ef4444")))
        painter.drawText(x0 + width - 100, 24, "━ Goal")
        painter.end()

    @staticmethod
    def _draw_polyline(painter: QPainter, points: list[tuple[int, int]], color: QColor, width: int, *, markers: bool) -> None:
        if len(points) < 1:
            return
        painter.setPen(QPen(color, width))
        for start, end in zip(points, points[1:]):
            painter.drawLine(start[0], start[1], end[0], end[1])
        if markers:
            painter.setBrush(color)
            for x, y in points:
                painter.drawEllipse(x - 4, y - 4, 8, 8)
