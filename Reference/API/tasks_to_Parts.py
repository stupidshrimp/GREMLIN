"""Map Limble task records to required parts."""


def tasks_to_parts(tasks: list[dict[str, object]]) -> dict[str, list[str]]:
    mapped: dict[str, list[str]] = {}
    for task in tasks:
        task_id = str(task.get("id", "unknown"))
        parts = task.get("parts", [])
        mapped[task_id] = [str(part) for part in parts] if isinstance(parts, list) else []
    return mapped
