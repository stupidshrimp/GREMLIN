import io
import math
import os
import tempfile
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from repositories.analysis_repo import AnalysisRepository
from repositories.failure_repo import FailureRepository
from repositories.metrics_repo import MetricsRepository
from services.reliability_service import ReliabilityService
from services.life_data_service import (
    DISPLAY_COLUMNS,
    PM_DISPOSITION_CATEGORIES,
    PM_RESET_DECISIONS,
    WO_DISPOSITION_CATEGORIES,
    DatabaseWriteError,
    LifeDataService,
    resolve_default_db_path,
)

app = Flask(__name__)

ICONS = {
    "home": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.2 3.7 10A1 1 0 0 0 3.3 11.1a1 1 0 0 0 1 .7h1v7.3c0 .5.4.9.9.9h4.8v-5.3c0-.5.4-.9.9-.9h1.8c.5 0 .9.4.9.9V20h4.8c.5 0 .9-.4.9-.9v-7.3h1a1 1 0 0 0 .6-1.8L12 3.2Z"/></svg>',
    "trend": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.7 18.9a1 1 0 0 1 0-1.4l5.6-5.6c.4-.4 1-.4 1.4 0l2.7 2.7 4.9-4.9h-2.4a1 1 0 1 1 0-2h4.8c.5 0 .9.4.9.9v4.8a1 1 0 1 1-2 0V11l-5.6 5.6c-.4.4-1 .4-1.4 0l-2.7-2.7-4.9 4.9a1 1 0 0 1-1.4 0Z"/><path d="M3 5.1c0-.5.4-.9.9-.9h16.2c.5 0 .9.4.9.9s-.4.9-.9.9H3.9A.9.9 0 0 1 3 5.1Z"/></svg>',
    "chart": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 20.8a1 1 0 0 1-1-1V4.2a1 1 0 1 1 2 0v14.6h14.6a1 1 0 1 1 0 2H4.5Z"/><path d="M8.1 16.1a1 1 0 0 1-1-1v-3.4a1 1 0 1 1 2 0v3.4a1 1 0 0 1-1 1Zm4 0a1 1 0 0 1-1-1V8.6a1 1 0 1 1 2 0v6.5a1 1 0 0 1-1 1Zm4 0a1 1 0 0 1-1-1v-5a1 1 0 1 1 2 0v5a1 1 0 0 1-1 1Z"/></svg>',
    "docs": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 2.8h7.8c.2 0 .5.1.6.3l3.8 3.8c.2.2.3.4.3.6v13.7c0 .5-.4.9-.9.9H6c-.5 0-.9-.4-.9-.9V3.7c0-.5.4-.9.9-.9Zm7.2 1.9v2.6c0 .5.4.9.9.9h2.6L13.2 4.7ZM8.2 11.2c0-.5.4-.9.9-.9h5.8a1 1 0 1 1 0 2H9.1a.9.9 0 0 1-.9-.9Zm0 3.8c0-.5.4-.9.9-.9h5.8a1 1 0 1 1 0 2H9.1a.9.9 0 0 1-.9-.9Z"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 2.9a1 1 0 0 1 2 0v1.3a7.8 7.8 0 0 1 2.1.9l.9-.9a1 1 0 1 1 1.4 1.4l-.9.9c.4.6.7 1.4.9 2.1h1.3a1 1 0 1 1 0 2h-1.3a7.8 7.8 0 0 1-.9 2.1l.9.9a1 1 0 1 1-1.4 1.4l-.9-.9c-.6.4-1.4.7-2.1.9v1.3a1 1 0 1 1-2 0v-1.3a7.8 7.8 0 0 1-2.1-.9l-.9.9a1 1 0 1 1-1.4-1.4l.9-.9a7.8 7.8 0 0 1-.9-2.1H3.6a1 1 0 1 1 0-2h1.3c.2-.8.5-1.5.9-2.1l-.9-.9A1 1 0 0 1 6.3 4l.9.9c.6-.4 1.4-.7 2.1-.9V2.9Zm1 5.1a3.8 3.8 0 1 0 0 7.7 3.8 3.8 0 0 0 0-7.7Z"/></svg>',
}

PAGES = [
    {"route": "/", "template": "home.html", "title": "Home", "icon": ICONS["home"]},
    {
        # The sidebar "Life Data Analysis" link goes straight to the Perform an
        # Analysis workspace instead of the "Choose a reliability workflow" landing
        # page. The landing page (/life-data-analysis) still exists and remains
        # reachable from the Home "Explore Analysis" button, so Failure
        # Classification and Standards and Documentation stay accessible.
        "route": "/life-data-analysis/perform-analysis",
        "template": "perform_analysis.html",
        "title": "Life Data Analysis",
        "icon": ICONS["trend"],
    },
    {"route": "/metrics", "template": "metrics.html", "title": "Metrics", "icon": ICONS["chart"]},
    {
        "route": "/standards-and-documentation",
        "template": "standards_and_documentation.html",
        "title": "Standards and Documentation",
        "icon": ICONS["docs"],
    },
    {"route": "/settings", "template": "settings.html", "title": "Settings", "icon": ICONS["settings"]},
]



# Placeholder service wiring. Replace with dependency-injected instances and real DB connections.
reliability_service = ReliabilityService(
    metrics_repo=MetricsRepository(),
    failure_repo=FailureRepository(),
    analysis_repo=AnalysisRepository(),
)

# The Life Data Analysis / Weibull backend reuses the desktop GUI's LifeDataService
# unchanged. The GUI hardcodes the shared Windows database; here the same path can be
# overridden with the GREMLIN_DB_PATH environment variable so the Flask app can run
# wherever GREMLIN.db is reachable. The service is created lazily and any startup
# failure is surfaced to the page instead of crashing the whole app.
MLE_CALCULATION_PASSWORD = "1336"
_life_data_service: LifeDataService | None = None
_life_data_service_error: str | None = None


def get_life_data_service() -> LifeDataService:
    """Return a shared LifeDataService, building it on first use."""

    global _life_data_service, _life_data_service_error
    if _life_data_service is not None:
        return _life_data_service
    db_path_override = os.environ.get("GREMLIN_DB_PATH")
    try:
        if db_path_override:
            db_path = db_path_override
        else:
            # Fall back to the shared default the desktop GUI uses, but never let
            # SQLite create a brand-new empty database at an unreachable share
            # (on Linux a Windows UNC path is just a long local filename).
            resolved = resolve_default_db_path()
            if not Path(resolved).is_file():
                raise RuntimeError(
                    "The shared GREMLIN.db could not be reached. "
                    "Set the GREMLIN_DB_PATH environment variable to a valid GREMLIN.db file."
                )
            db_path = resolved
        _life_data_service = LifeDataService(db_path=db_path, refresh_on_startup=False)
        _life_data_service_error = None
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as JSON
        _life_data_service_error = str(exc)
        raise
    return _life_data_service


class LifeDataApiError(Exception):
    """Wrap a service failure with an HTTP status for the JSON API."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def life_data_api(view):
    """Translate service/validation errors into consistent JSON error payloads."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except LifeDataApiError as exc:
            return jsonify({"error": exc.message}), exc.status_code
        except DatabaseWriteError as exc:
            return jsonify({"error": str(exc)}), 503
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 - last-resort guard for the API
            return jsonify({"error": f"Unexpected error: {exc}"}), 500

    return wrapped


def _service_or_api_error() -> LifeDataService:
    try:
        return get_life_data_service()
    except Exception as exc:  # noqa: BLE001
        raise LifeDataApiError(
            "GREMLIN could not open the shared analysis database. "
            f"Set GREMLIN_DB_PATH to a reachable GREMLIN.db file. Details: {exc}",
            status_code=503,
        ) from exc


def _required_asset() -> str:
    asset_number = (request.values.get("asset") or "").strip()
    if not asset_number:
        raise LifeDataApiError("Select an Asset Number first.", status_code=400)
    return asset_number


def _disposition_kind() -> str:
    kind = (request.values.get("kind") or "").strip().lower()
    if kind not in {"wo", "pm"}:
        raise LifeDataApiError("Disposition kind must be 'wo' or 'pm'.", status_code=400)
    return kind


NAV_LINKS = [
    {"label": page["title"], "url": page["route"], "icon": page["icon"]}
    for page in PAGES
    if page["route"] != "/standards-and-documentation"
]


@app.route("/")
def home():
    return render_template("home.html", page_title="Home", nav_links=NAV_LINKS)


@app.route("/life-data-analysis")
def life_data_analysis():
    return render_template(
        "life_data_analysis.html",
        page_title="Life Data Analysis",
        nav_links=NAV_LINKS,
    )




@app.route("/life-data-analysis/perform-analysis")
def perform_analysis():
    return render_template(
        "perform_analysis.html",
        page_title="Perform an Analysis",
        nav_links=NAV_LINKS,
    )


@app.route("/life-data-analysis/disposition")
def disposition():
    return render_template(
        "disposition.html",
        page_title="Disposition",
        nav_links=NAV_LINKS,
    )


@app.route("/life-data-analysis/failure-classification")
def failure_classification():
    classification_data = reliability_service.get_failure_classification_data()
    return render_template(
        "failure_classification.html",
        page_title="Failure Classification",
        nav_links=NAV_LINKS,
        classification_data=classification_data,
    )

@app.route("/metrics")
def metrics():
    metrics_data = reliability_service.get_metrics_dashboard_data()
    return render_template(
        "metrics.html",
        page_title="Metrics",
        nav_links=NAV_LINKS,
        metrics_data=metrics_data,
    )


@app.route("/standards-and-documentation")
def standards_and_documentation():
    return render_template(
        "standards_and_documentation.html",
        page_title="Standards and Documentation",
        nav_links=NAV_LINKS,
    )


@app.route("/settings")
def settings():
    return render_template("settings.html", page_title="Settings", nav_links=NAV_LINKS)


# ---------------------------------------------------------------------------
# Life Data Analysis / Weibull JSON API
#
# These endpoints mirror, one-to-one, the LifeDataService calls the desktop GUI
# makes, so the browser workspace performs the exact same backend operations:
# asset selection, readiness summary + Pareto + beta rankings, REL disposition
# editing (with Excel round-trips), Weibull MLE fitting, bulk calculation, and
# saving manual parameter adjustments.
# ---------------------------------------------------------------------------


@app.route("/life-data-analysis/api/assets")
@life_data_api
def api_assets():
    service = _service_or_api_error()
    # Auto-map only happens when the mapped table is empty, so an already-populated
    # database does not silently pick up later Limble syncs. Allow the client to
    # request an explicit re-map (mirroring the desktop "Refresh CMMS mapping"
    # action) before reading the asset list.
    if request.values.get("refresh") in ("1", "true", "yes"):
        service.refresh_mapped_cmms_records()
    return jsonify({"assets": service.asset_number_options()})


@app.route("/life-data-analysis/api/refresh-mapping", methods=["POST"])
@life_data_api
def api_refresh_mapping():
    service = _service_or_api_error()
    mapped = service.refresh_mapped_cmms_records()
    return jsonify({"mapped": int(mapped or 0), "assets": service.asset_number_options()})


@app.route("/life-data-analysis/api/summary")
@life_data_api
def api_summary():
    service = _service_or_api_error()
    asset_number = _required_asset()
    summary = service.summary_for_asset(asset_number)
    return jsonify(
        {
            "asset_number": asset_number,
            "summary": {field: getattr(summary, field) for field in summary.__dataclass_fields__},
            "rankings": service.latest_failure_mechanism_beta_rankings(asset_number, limit=5),
            "pareto": service.failure_mechanism_pareto(asset_number),
            "trend": service.failure_mode_trend(asset_number),
        }
    )


@app.route("/life-data-analysis/api/dispositions")
@life_data_api
def api_dispositions():
    service = _service_or_api_error()
    asset_number = _required_asset()
    kind = _disposition_kind()
    scope = (request.values.get("scope") or "all").strip().lower()
    only_needing = scope == "new"
    search = (request.values.get("search") or "").strip()
    page_size = 50
    try:
        page_index = max(0, int(request.values.get("page", 0)))
    except (TypeError, ValueError):
        page_index = 0

    all_count = service.disposition_row_count(asset_number, kind)
    displayed_count = service.disposition_row_count(
        asset_number, kind, only_needing_disposition=only_needing, search=search or None
    )
    max_page_index = max(0, math.ceil(displayed_count / page_size) - 1) if displayed_count else 0
    page_index = min(page_index, max_page_index)
    offset = page_index * page_size
    rows = service.disposition_rows(
        asset_number,
        kind,
        only_needing_disposition=only_needing,
        limit=page_size,
        offset=offset,
        search=search or None,
    )

    wo_record_classes = ["CORRECTIVE_WO", "PM", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN"]
    pm_record_classes = ["PM", "PM_RESET_CANDIDATE", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN"]
    return jsonify(
        {
            "asset_number": asset_number,
            "kind": kind,
            "scope": scope,
            "search": search,
            "rows": rows,
            "display_columns": list(DISPLAY_COLUMNS),
            "mode_options": service.get_asset_failure_mode_options(asset_number),
            "mechanism_options": service.get_asset_failure_mechanism_options(asset_number),
            "categories": list(PM_DISPOSITION_CATEGORIES if kind == "pm" else WO_DISPOSITION_CATEGORIES),
            "record_classes": pm_record_classes if kind == "pm" else wo_record_classes,
            "pm_reset_decisions": list(PM_RESET_DECISIONS),
            "page_index": page_index,
            "max_page_index": max_page_index,
            "page_size": page_size,
            "offset": offset,
            "displayed_count": displayed_count,
            "all_count": all_count,
        }
    )


@app.route("/life-data-analysis/api/dispositions/save", methods=["POST"])
@life_data_api
def api_save_dispositions():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    dispositions = payload.get("dispositions")
    if not isinstance(dispositions, list) or not dispositions:
        raise LifeDataApiError("No disposition rows were provided to save.", status_code=400)
    saved = service.save_dispositions(dispositions)
    return jsonify({"saved": saved})


@app.route("/life-data-analysis/api/dispositions/excel")
@life_data_api
def api_download_disposition_excel():
    service = _service_or_api_error()
    asset_number = _required_asset()
    kind = _disposition_kind()
    safe_asset = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in asset_number).strip("_") or "asset"
    # Build the workbook in a temp file, then serve it from memory and delete the
    # temp file immediately so repeated downloads never orphan files on disk.
    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix=f"{safe_asset}_{kind}_")
    os.close(fd)
    try:
        service.export_disposition_excel(asset_number, kind, path)
        with open(path, "rb") as handle:
            workbook_bytes = handle.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return send_file(
        io.BytesIO(workbook_bytes),
        as_attachment=True,
        download_name=f"{safe_asset}_{kind}_dispositions.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/life-data-analysis/api/dispositions/excel", methods=["POST"])
@life_data_api
def api_upload_disposition_excel():
    service = _service_or_api_error()
    asset_number = _required_asset()
    kind = _disposition_kind()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise LifeDataApiError("Choose an .xlsx file exported from this disposition screen.", status_code=400)
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        upload.save(path)
        imported = service.import_disposition_excel(asset_number, kind, path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return jsonify({"imported": imported})


@app.route("/life-data-analysis/api/pm-effectiveness")
@life_data_api
def api_pm_effectiveness():
    service = _service_or_api_error()
    asset_number = _required_asset()
    mechanism_raw = request.values.get("failure_mechanism_id")
    if mechanism_raw in (None, ""):
        raise LifeDataApiError("Select a failure mechanism to evaluate PM effectiveness.", status_code=400)
    try:
        failure_mechanism_id = int(mechanism_raw)
    except (TypeError, ValueError):
        raise LifeDataApiError("failure_mechanism_id must be an integer.", status_code=400)
    mode_raw = request.values.get("failure_mode_id")
    failure_mode_id = None
    if mode_raw not in (None, ""):
        try:
            failure_mode_id = int(mode_raw)
        except (TypeError, ValueError):
            raise LifeDataApiError("failure_mode_id must be an integer.", status_code=400)
    return jsonify(
        {
            "pm_effectiveness": service.pm_effectiveness(
                asset_number,
                failure_mechanism_id=failure_mechanism_id,
                failure_mode_id=failure_mode_id,
            )
        }
    )


@app.route("/life-data-analysis/api/weibull-groups")
@life_data_api
def api_weibull_groups():
    service = _service_or_api_error()
    asset_number = _required_asset()
    return jsonify({"groups": service.weibull_group_options(asset_number)})


def _serialize_analysis_result(result) -> dict:
    return {field: getattr(result, field) for field in result.__dataclass_fields__}


@app.route("/life-data-analysis/api/perform-analysis", methods=["POST"])
@life_data_api
def api_perform_analysis():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    asset_number = (payload.get("asset") or "").strip()
    if not asset_number:
        raise LifeDataApiError("Select an Asset Number first.", status_code=400)
    grouping_level = (payload.get("grouping_level") or "").strip()
    failure_mode_id = payload.get("failure_mode_id")
    failure_mechanism_id = payload.get("failure_mechanism_id")
    if failure_mode_id is None:
        raise LifeDataApiError("Select a failure mode or failure mechanism first.", status_code=400)
    result = service.perform_weibull_analysis(
        asset_number,
        grouping_level=grouping_level,
        failure_mode_id=int(failure_mode_id),
        failure_mechanism_id=int(failure_mechanism_id) if failure_mechanism_id is not None else None,
    )
    return jsonify({"result": _serialize_analysis_result(result)})


@app.route("/life-data-analysis/api/calculate-all", methods=["POST"])
@life_data_api
def api_calculate_all():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    asset_number = (payload.get("asset") or "").strip()
    if not asset_number:
        raise LifeDataApiError("Select an Asset Number first.", status_code=400)
    if str(payload.get("password") or "") != MLE_CALCULATION_PASSWORD:
        raise LifeDataApiError("The password was incorrect. No Weibull MLE calculations were performed.", status_code=403)
    return jsonify({"summary": service.calculate_all_weibull_results(asset_number)})


@app.route("/life-data-analysis/api/parameter-adjustment", methods=["POST"])
@life_data_api
def api_parameter_adjustment():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    try:
        result_id = int(payload.get("result_id"))
        beta = float(payload.get("beta"))
        eta = float(payload.get("eta"))
    except (TypeError, ValueError):
        raise LifeDataApiError("Adjusted beta, eta, and the source result id are required.", status_code=400)
    if not (math.isfinite(beta) and math.isfinite(eta) and beta > 0 and eta > 0):
        raise LifeDataApiError("Adjusted beta and eta must both be finite and greater than zero.", status_code=400)
    reason = str(payload.get("reason") or "")
    adjustment_id = service.save_parameter_adjustment(result_id, beta, eta, reason)
    return jsonify({"parameter_adjustment_id": adjustment_id})


@app.errorhandler(404)
def not_found(_err):
    return render_template("home.html", page_title="Not Found", nav_links=NAV_LINKS), 404


if __name__ == "__main__":
    app.run(debug=True)
