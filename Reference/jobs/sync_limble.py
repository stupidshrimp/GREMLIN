"""Scheduled Limble synchronization entry point."""

from integrations.limble import LimbleClient


def sync_limble() -> dict[str, int]:
    client = LimbleClient()
    return {"assets": len(client.list_assets()), "tasks": len(client.list_tasks())}


if __name__ == "__main__":
    print(sync_limble())
