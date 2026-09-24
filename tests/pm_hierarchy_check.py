"""Read-only check of the PM calendar's parent/child data.

Run from the GREMLIN folder in PyCharm:

    python pm_hierarchy_check.py                 # looks at 4002
    python pm_hierarchy_check.py "salvagnini"    # any search term
    python pm_hierarchy_check.py 4002 "D:\\path\\to\\PM_Calendar_local.db"

With no path it uses GREMLIN_PM_CALENDAR_DB_PATH if set, otherwise
C:\\GREMLIN\\PM_Calendar_local.db. It opens the file read-only and changes
nothing. Paste everything it prints back into the chat.
"""

import os
import sqlite3
import sys


def chain(asset_id, parents, names, seen=None):
    """The asset and everything above it: child > parent > ... > root."""
    seen = seen or set()
    parts = []
    current = asset_id
    while current and current not in seen:
        seen.add(current)
        parts.append(f"{names.get(current, '?')} [{current}]")
        current = parents.get(current)
    return "  >  ".join(parts)


def main() -> None:
    term = sys.argv[1] if len(sys.argv) > 1 else "4002"
    if len(sys.argv) > 2:
        path = sys.argv[2]
    else:
        path = os.environ.get("GREMLIN_PM_CALENDAR_DB_PATH") or r"C:\GREMLIN\PM_Calendar_local.db"

    print(f"Database: {path}")
    if not os.path.exists(path):
        print("  File not found. Pass the path to PM_Calendar_local.db as the second argument.")
        return

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if "pm_asset" not in tables:
        print("  No pm_asset table: the parent/child code has never run against this file.")
        return

    columns = [r[1] for r in conn.execute("PRAGMA table_info(pm_asset)")]
    print(f"  pm_asset columns: {columns}")
    if "level" not in columns:
        print("  -> This file predates the reworked hierarchy. Restart GREMLIN and sync.")

    total, with_parent = conn.execute(
        "SELECT COUNT(*), SUM(parent_asset_id IS NOT NULL) FROM pm_asset"
    ).fetchone()
    last_sync = conn.execute("SELECT MAX(synced_at) FROM pm_task").fetchone()[0]
    print(f"  {total} assets in the hierarchy, {with_parent or 0} with a parent")
    print(f"  pm_task last written: {last_sync}")

    parents, names = {}, {}
    for row in conn.execute("SELECT asset_id, asset_name, parent_asset_id FROM pm_asset"):
        parents[row["asset_id"]] = row["parent_asset_id"]
        names[row["asset_id"]] = row["asset_name"]

    children = {}
    for asset_id, parent_id in parents.items():
        if parent_id:
            children.setdefault(parent_id, []).append(asset_id)

    print(f"\n=== Assets whose name contains '{term}' ===")
    matches = [a for a, n in names.items() if term.lower() in str(n or "").lower()]
    for asset_id in sorted(matches, key=lambda a: str(names.get(a) or "")):
        kids = children.get(asset_id, [])
        print(f"\n  {names.get(asset_id)}  [{asset_id}]  -- {len(kids)} direct children")
        print(f"    up:   {chain(asset_id, parents, names)}")
        for kid in sorted(kids, key=lambda a: str(names.get(a) or ""))[:12]:
            print(f"    down: {names.get(kid)} [{kid}]")
        if len(kids) > 12:
            print(f"    down: ... and {len(kids) - 12} more")

    print("\n=== The five assets with the most direct children ===")
    for parent_id, kids in sorted(children.items(), key=lambda kv: -len(kv[1]))[:5]:
        print(f"  {names.get(parent_id, '?')} [{parent_id}]: {len(kids)} children")

    conn.close()


if __name__ == "__main__":
    main()