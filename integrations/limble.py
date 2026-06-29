"""Limble CMMS API client.

This module should contain only API communication concerns:
- authentication
- pagination
- retries/rate limiting
- endpoint-specific fetch methods
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import os
import time
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError

from flask.cli import load_dotenv

load_dotenv()


@dataclass
class LimbleConfig:
    """Runtime configuration required for Limble API access."""

    base_url: str
    client_id: str
    api_key: str
    timeout_seconds: int = 30
    page_size: int = 200
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> "LimbleConfig":
        """Build config from environment variables."""
        base_url = os.getenv("LIMBLE_API_BASE_URL", "https://api.limblecmms.com")
        client_id = os.getenv("LIMBLE_API_CLIENTID", "")
        api_key = os.getenv("LIMBLE_API_KEY", "")

        if not client_id:
            raise ValueError("Missing LIMBLE_API_CLIENTID in environment")
        if not api_key:
            raise ValueError("Missing LIMBLE_API_KEY in environment")

        return cls(base_url=base_url.rstrip("/"), client_id=client_id, api_key=api_key)


class LimbleClient:
    """API client for Limble."""

    def __init__(self, config: LimbleConfig) -> None:
        self.config = config

    def _build_auth_header(self) -> str:
        token = f"{self.config.client_id}:{self.config.api_key}".encode("utf-8")
        encoded = base64.b64encode(token).decode("utf-8")
        return f"Basic {encoded}"

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = parse.urlencode(params or {})
        url = f"{self.config.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        req = request.Request(
            url,
            headers={
                "Authorization": self._build_auth_header(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="GET",
        )

        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                    payload = response.read().decode("utf-8")
                    return json.loads(payload) if payload else {}
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    break
                time.sleep(self.config.retry_backoff_seconds * attempt)

        raise RuntimeError(f"Limble request failed for {url}") from last_error

    def _extract_records(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("data", "results", "items", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
        return []

    def _fetch_paginated(self, path: str, updated_since: str | None = None) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        page = 1

        while True:
            params: dict[str, Any] = {"page": page, "perPage": self.config.page_size}
            if updated_since:
                params["updated_since"] = updated_since

            payload = self._request_json(path=path, params=params)
            rows = self._extract_records(payload)
            if not rows:
                break

            all_rows.extend(rows)

            # stop when explicit pagination metadata says no next page
            pagination = payload.get("pagination")
            if isinstance(pagination, dict):
                current = pagination.get("page", page)
                total_pages = pagination.get("totalPages")
                if isinstance(total_pages, int) and isinstance(current, int) and current >= total_pages:
                    break

            # fallback stop condition for APIs with no pagination metadata
            if len(rows) < self.config.page_size:
                break

            page += 1

        return all_rows

    def get_assets(self, updated_since: str | None = None) -> list[dict[str, Any]]:
        """Fetch assets from Limble."""
        return self._fetch_paginated("/v2/assets/", updated_since=updated_since)

    def get_work_orders(self, updated_since: str | None = None) -> list[dict[str, Any]]:
        """Fetch work orders/tasks from Limble."""
        return self._fetch_paginated("/v2/tasks/", updated_since=updated_since)

    def get_failure_events(self, updated_since: str | None = None) -> list[dict[str, Any]]:
        """Fetch failure-related records from Limble.

        Limble doesn't expose a dedicated "failure events" endpoint,
        so this currently returns task/work-order data.
        """
        return self.get_work_orders(updated_since=updated_since)
