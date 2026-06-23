# GREMLIN Desktop GUI

Graphical Reliability Engineering, Maintenance, Life-Data INterface.

This project launches as a native **PyQt6 desktop application** focused on GREMLIN reliability engineering workflows. The interface is a green GREMLIN workspace for maintenance analytics, life-data analysis, metrics, standards/documentation, settings, and Limble integration placeholders.

## What the GUI includes

- Green GREMLIN-branded desktop shell with no external service chrome.
- Left-side GREMLIN navigation for Home, Life Data Analysis, Metrics, Standards and Documentation, and Settings.
- Reliability metric cards backed by the repository/service layer.
- Life-data analysis summaries and failure-classification tables.
- A reusable batch launcher so you do not need to download a batch file every time.

## Database location

Life Data Analysis uses the shared SQLite database from the first reachable shared location by default. GREMLIN checks both `Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db` and `FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db` under `Z:` first, then checks those same paths under every other drive letter (`A:` through `Y:`), then checks the `\\sandc.ws\depts\Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db` UNC path.

GREMLIN opens write operations with a 30-second SQLite busy timeout and a `BEGIN IMMEDIATE` transaction so only one user can write at a time. It also keeps SQLite in rollback-journal mode instead of WAL mode because the database is on a shared drive. If a write cannot be completed safely, the desktop app shows a dedicated **Database write failed** popup with the shared path, the plain-language reason, and what the user should do next.

## Run the desktop app

### Windows launcher

Double-click `start_gremlin.bat` from the project folder. The launcher changes into the project directory, installs `requirements.txt`, starts `python app.py`, and leaves the terminal open if there is an error.

### Run manually

```bash
python -m pip install -r requirements.txt
python app.py
```

## Project layout

- `app.py` — tiny entry point that starts the PyQt GUI.
- `gremlin_gui.py` — native PyQt6 window, green GREMLIN shell, desktop pages, tables, and cards.
- `repositories/` — sample data access classes for metrics, failures, analyses, and raw records.
- `services/` — orchestration for reliability dashboards, classification, and ingestion.
- `models/` — dataclass DTOs shared by repositories and GUI widgets.
- `integrations/`, `jobs/`, and `API/` — Limble integration and experimental API/sync scripts.
- `templates/` and `static/` — legacy visual/style references retained from the prior web version.

## Smoke checks

```bash
python -m compileall app.py gremlin_gui.py repositories services models integrations jobs API Archive
python - <<'PY'
from services.reliability_service import ReliabilityService
from repositories.metrics_repo import MetricsRepository
from repositories.failure_repo import FailureRepository
from repositories.analysis_repo import AnalysisRepository

service = ReliabilityService(MetricsRepository(), FailureRepository(), AnalysisRepository())
assert len(service.get_metrics_dashboard_data()["cards"]) == 4
assert service.get_failure_classification_data()["total"] == 61
print("ok")
PY
```

If you are running in a headless environment, the GUI will not display unless a desktop display server is available.
