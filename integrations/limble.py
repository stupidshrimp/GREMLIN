"""Limble CMMS API client.

This module owns *only* API-communication concerns:

- authentication (HTTP Basic with the API client id + secret)
- pagination across the Limble v2 list endpoints
- rate-limiting (one request per ``seconds_per_request``) and 429 handling
- transient-error retries with exponential backoff

It returns raw, un-transformed Limble payloads. Turning those payloads into the
shapes GREMLIN.db expects is the job of :mod:`services.ingestion_service`.

Auth matches the working API scripts under ``API/`` (Basic ``client_id:secret``
base64-encoded) and points at the documented v2 base URL
``https://api.limblecmms.com/v2``.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

DEFAULT_BASE_URL = "https://api.limblecmms.com/v2"


@dataclass
class LimbleConfig:
    """Runtime configuration required for Limble API access."""

    client_id: str
    client_secret: str
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: int = 30
    # Limble publishes a low request budget; the working scripts space requests
    # ~1.1s apart, so we keep the same conservative default.
    seconds_per_request: float = 1.1
    page_limit: int = 200
    max_retries: int = 4
    # Optional extra query params passed verbatim to the /tasks list endpoint
    # (e.g. {"locations": "5"}). Kept open so callers can narrow a pull without
    # this client needing to know every Limble filter.
    extra_task_params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: Any) -> "LimbleConfig":
        """Build a config from environment variables, with optional overrides.

        Recognised variables (first non-empty wins):

        - client id:     ``LIMBLE_CLIENT_ID`` or ``LIMBLE_API_CLIENTID``
        - client secret: ``LIMBLE_CLIENT_SECRET`` or ``LIMBLE_API_KEY``
        - base url:      ``LIMBLE_BASE_URL``
        """

        client_id = overrides.pop("client_id", None) or _first_env("LIMBLE_CLIENT_ID", "LIMBLE_API_CLIENTID")
        client_secret = overrides.pop("client_secret", None) or _first_env("LIMBLE_CLIENT_SECRET", "LIMBLE_API_KEY")
        base_url = overrides.pop("base_url", None) or os.getenv("LIMBLE_BASE_URL") or DEFAULT_BASE_URL
        if not client_id or not client_secret:
            raise ValueError(
                "Limble credentials are missing. Set LIMBLE_CLIENT_ID and LIMBLE_CLIENT_SECRET "
                "(or LIMBLE_API_CLIENTID and LIMBLE_API_KEY), or pass --client-id/--client-secret."
            )
        return cls(client_id=client_id, client_secret=client_secret, base_url=base_url, **overrides)


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return None


class LimbleClient:
    """Thin, paginating HTTP client for the Limble CMMS v2 API."""

    def __init__(self, config: LimbleConfig) -> None:
        self.config = config
        self._session = requests.Session()
        credentials = f"{config.client_id}:{config.client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        self._session.headers.update(
            {
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Public endpoint helpers
    # ------------------------------------------------------------------
    def get_tasks(self, updated_since: int | None = None) -> list[dict[str, Any]]:
        """Return all tasks (work orders, PMs, requests) from ``/tasks``.

        ``updated_since`` is an optional Unix timestamp (seconds). Limble does
        not expose a universal "updated since" filter on this endpoint, so the
        filter is applied client-side against the most recent of each task's
        ``lastEdited`` / ``createdDate`` / ``dateCompleted`` values. Omit it for
        a full pull (the import is idempotent, so full pulls are safe).
        """

        params: dict[str, Any] = dict(self.config.extra_task_params)
        tasks = list(self._paginate("/tasks/", params))
        if updated_since is None:
            return tasks
        return [task for task in tasks if _task_touched_at(task) >= updated_since]

    def get_assets(self) -> list[dict[str, Any]]:
        """Return all assets from ``/assets`` (used to enrich task asset info)."""

        return list(self._paginate("/assets/", {}))

    # ------------------------------------------------------------------
    # Pagination + transport
    # ------------------------------------------------------------------
    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield every item from a paginated Limble list endpoint."""

        page = 1
        while True:
            page_params = {**params, "limit": self.config.page_limit, "page": page}
            payload = self._request("GET", path, params=page_params)
            # Limble list endpoints return a JSON array; a non-list (or empty
            # list) marks the end of pagination.
            if not isinstance(payload, list) or not payload:
                break
            for item in payload:
                if isinstance(item, dict):
                    yield item
            if len(payload) < self.config.page_limit:
                break
            page += 1
            # Space out page requests to stay under the rate limit.
            time.sleep(self.config.seconds_per_request)

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Perform one request with 429 handling and exponential backoff."""

        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"
        attempt = 0
        while True:
            try:
                resp = self._session.request(method, url, params=params, timeout=self.config.timeout_seconds)
            except requests.exceptions.RequestException as exc:
                attempt += 1
                if attempt > self.config.max_retries:
                    raise
                self._sleep_backoff(attempt, reason=f"network error ({exc})")
                continue

            if resp.status_code == 429:
                # Respect Retry-After when present; otherwise back off a full
                # minute as the working scripts do.
                retry_after = _retry_after_seconds(resp) or 60.0
                print(f"[limble] rate limited; sleeping {retry_after:.0f}s before retrying {path} ...")
                time.sleep(retry_after)
                continue

            if 500 <= resp.status_code < 600:
                attempt += 1
                if attempt > self.config.max_retries:
                    resp.raise_for_status()
                self._sleep_backoff(attempt, reason=f"server error {resp.status_code}")
                continue

            resp.raise_for_status()
            if not resp.content:
                return []
            return resp.json()

    def _sleep_backoff(self, attempt: int, *, reason: str) -> None:
        # 2s, 4s, 8s, 16s ...
        delay = 2.0 ** attempt
        print(f"[limble] {reason}; retry {attempt}/{self.config.max_retries} in {delay:.0f}s ...")
        time.sleep(delay)


def _retry_after_seconds(resp: "requests.Response") -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _task_touched_at(task: dict[str, Any]) -> int:
    """Best-effort 'most recently touched' Unix time for client-side filtering."""

    candidates = []
    for key in ("lastEdited", "createdDate", "dateCompleted", "startDate"):
        value = task.get(key)
        try:
            if value not in (None, "", 0):
                candidates.append(int(float(value)))
        except (TypeError, ValueError):
            continue
    return max(candidates) if candidates else 0
