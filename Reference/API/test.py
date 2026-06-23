"""Small API smoke test script."""

from API.tasks_to_Parts import tasks_to_parts


def test_tasks_to_parts() -> None:
    assert tasks_to_parts([{"id": 1, "parts": ["belt", "bearing"]}]) == {"1": ["belt", "bearing"]}


if __name__ == "__main__":
    test_tasks_to_parts()
    print("ok")
