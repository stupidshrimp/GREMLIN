"""Dump the real structure of a GREMLIN.db file as Markdown.

The command-line counterpart to the developer dashboard's Schema and Drift
panels: both read through :mod:`services.schema_service`, so they always agree.

Because GREMLIN keeps no ``.sql`` file and its schema code only ever adds to an
existing database, the output is the only accurate description of a given
GREMLIN.db -- including legacy columns and tables from removed features that the
source no longer mentions.

Usage::

    python tools/dump_schema.py                       # uses GREMLIN_DB_PATH or the default
    python tools/dump_schema.py --db /path/GREMLIN.db
    python tools/dump_schema.py --out docs/schema.md
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.life_data_service import DEFAULT_DB_PATH  # noqa: E402
from services.schema_service import SchemaService, SchemaServiceError  # noqa: E402


def _default_db_path() -> Path:
    override = os.environ.get("GREMLIN_DB_PATH")
    return Path(override) if override else DEFAULT_DB_PATH


def _format_bytes(size: int | None) -> str:
    if not isinstance(size, int):
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def build_report(service: SchemaService) -> str:
    lines: list[str] = []
    add = lines.append

    overview = service.overview()
    add("# GREMLIN.db structure")
    add("")
    add(f"- **File:** `{overview['db_path']}`")
    add(f"- **Size:** {_format_bytes(overview.get('size_bytes'))}")
    add(f"- **Last modified:** {overview.get('modified_at', 'unknown')}")
    add(f"- **SQLite version:** {overview.get('sqlite_version')}")
    add(f"- **Journal mode:** {overview.get('journal_mode')}")
    counts = overview.get("object_counts", {})
    add(
        f"- **Objects:** {counts.get('table', 0)} tables, "
        f"{counts.get('index', 0)} indexes, {counts.get('view', 0)} views, "
        f"{counts.get('trigger', 0)} triggers"
    )
    add("")

    # Pipeline ---------------------------------------------------------------
    pipeline = service.pipeline()
    add("## Pipeline")
    add("")
    add("Row counts along the ingestion → analysis path.")
    add("")
    add("| Stage | Table | Rows |")
    add("| --- | --- | --- |")
    for stage in pipeline["stages"]:
        rows = "missing" if not stage["present"] else f"{stage['row_count']:,}"
        add(f"| {stage['label']} | `{stage['table']}` | {rows} |")
    add("")

    # Drift ------------------------------------------------------------------
    drift = service.drift_report()
    add("## Schema drift (code vs file)")
    add("")
    if not drift.get("available"):
        add(f"_Unavailable: {drift.get('error')}_")
        add("")
    else:
        add(
            f"Current schema code defines {drift['reference_table_count']} tables; "
            f"this file contains {drift['live_table_count']}."
        )
        add("")
        if drift["missing_tables"]:
            add("**Defined in code, missing from this file:**")
            add("")
            for entry in drift["missing_tables"]:
                add(f"- `{entry['name']}`")
            add("")
        if drift["extra_tables"]:
            add("**In this file, not defined by current code:**")
            add("")
            for entry in drift["extra_tables"]:
                note = f" — {entry['note']}" if entry.get("note") else ""
                add(f"- `{entry['name']}`{note}")
            add("")
        if drift["column_drift"]:
            add("**Column differences:**")
            add("")
            add("| Table | Missing in file | Extra in file |")
            add("| --- | --- | --- |")
            for entry in drift["column_drift"]:
                missing = ", ".join(f"`{name}`" for name in entry["missing_in_file"]) or "—"
                extra = ", ".join(f"`{name}`" for name in entry["extra_in_file"]) or "—"
                add(f"| `{entry['table']}` | {missing} | {extra} |")
            add("")
        if not (drift["missing_tables"] or drift["extra_tables"] or drift["column_drift"]):
            add("No drift: this file matches what the current code would build.")
            add("")

    # Tables -----------------------------------------------------------------
    tables = service.tables()
    add("## Tables")
    add("")
    add("| Table | Origin | Rows | Columns |")
    add("| --- | --- | --- | --- |")
    for table in tables:
        rows = f"{table['row_count']:,}" if isinstance(table["row_count"], int) else "—"
        add(f"| `{table['name']}` | {table['origin']} | {rows} | {table['column_count']} |")
    add("")

    for table in tables:
        if table["origin"] == "internal":
            continue
        detail = service.table_detail(table["name"])
        add(f"### `{detail['name']}`")
        add("")
        if detail.get("note"):
            add(f"> {detail['note']}")
            add("")
        rows = f"{detail['row_count']:,}" if isinstance(detail["row_count"], int) else "unknown"
        add(f"{rows} rows.")
        add("")

        add("| Column | Type | Not null | Default | PK |")
        add("| --- | --- | --- | --- | --- |")
        for column in detail["columns"]:
            default = "—" if column["default"] is None else f"`{column['default']}`"
            add(
                f"| `{column['name']}` | {column['type'] or '—'} | "
                f"{'yes' if column['not_null'] else ''} | {default} | "
                f"{'yes' if column['primary_key'] else ''} |"
            )
        add("")

        if detail["foreign_keys"]:
            add("**Foreign keys:**")
            add("")
            for fk in detail["foreign_keys"]:
                add(
                    f"- `{fk['column']}` → `{fk['references_table']}({fk['references_column']})` "
                    f"(update {fk['on_update']}, delete {fk['on_delete']})"
                )
            add("")

        if detail["indexes"]:
            add("**Indexes:**")
            add("")
            for index in detail["indexes"]:
                unique = "unique " if index["unique"] else ""
                add(f"- `{index['name']}` — {unique}({', '.join(index['columns'])})")
            add("")

        drift_entry = detail.get("drift") or {}
        if drift_entry.get("missing_in_file") or drift_entry.get("extra_in_file"):
            add("**Drift:**")
            add("")
            if drift_entry.get("missing_in_file"):
                add(f"- In code but not in this file: {', '.join(drift_entry['missing_in_file'])}")
            if drift_entry.get("extra_in_file"):
                add(f"- In this file but not in code: {', '.join(drift_entry['extra_in_file'])}")
            add("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump the structure of a GREMLIN.db file as Markdown.")
    parser.add_argument("--db", type=Path, default=None, help="Path to GREMLIN.db (default: GREMLIN_DB_PATH or built-in default).")
    parser.add_argument("--out", type=Path, default=None, help="Write the report to this file instead of stdout.")
    args = parser.parse_args(argv)

    db_path = args.db or _default_db_path()
    service = SchemaService(db_path)
    try:
        report = build_report(service)
    except SchemaServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote {args.out} ({len(report):,} chars).")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
