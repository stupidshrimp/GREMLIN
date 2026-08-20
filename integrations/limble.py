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
from typing import Any, Callable, Iterator

import requests

DEFAULT_BASE_URL = "https://api.limblecmms.com/v2"


class LimbleResponseError(RuntimeError):
    """Limble answered successfully, but not with the shape the endpoint promises.

    Distinct from a transport failure on purpose. ``requests`` raises for a bad
    status, and the retries above handle that; this is the case where the call
    *succeeded* and the body is still not usable -- an API gateway's wrapper
    object, a maintenance notice, an error document served with a 200. Left
    untyped, those bodies coerce to "the resource is empty", which reads to a
    caller as fact rather than as a failed read.
    """


# Keys that mark an object as a Limble *instruction record*, rather than some
# other object that happened to arrive in an array. Every entry is taken from a
# payload confirmed against this account -- the live endpoint on the left, the
# site's task export on the right:
#
#   instruction          instructionText     the box's prompt
#   instructionID        instructionID       the record's own id
#   parentInstructionID  parentID            the section it belongs to
#   instructionFiles     itemID
#
# Recognising the *record* is deliberately not the same as recognising an
# answer: a work order whose template never asked for the four boxes returns
# forty-odd of these carrying nothing this application wants, and it is still a
# real instruction list. See ``get_task_instructions``.
_INSTRUCTION_MARKER_KEYS = frozenset({
    "instruction", "instructionText", "instruction_text",
    "instructionID", "instructionId", "instruction_id",
    "parentInstructionID", "parentID", "instructionFiles", "itemID",
})


def _describe(item: Any) -> str:
    """Name an unusable item well enough to diagnose it from a log line.

    An object gets its keys listed rather than just "dict": what makes an error
    document distinguishable from an instruction is precisely which keys it
    carries, so that is what an operator needs to see.
    """

    if isinstance(item, dict):
        keys = sorted(str(key) for key in item)
        shown = ", ".join(keys[:6]) + (", ..." if len(keys) > 6 else "")
        return f"an object with keys [{shown}]" if keys else "an empty object"
    return f"a {type(item).__name__}"


def _is_instruction_record(item: Any) -> bool:
    """True for an object carrying at least one key only an instruction carries."""

    return isinstance(item, dict) and not _INSTRUCTION_MARKER_KEYS.isdisjoint(item)


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

    def __init__(self, config: LimbleConfig, *, log: Callable[[str], None] = print) -> None:
        self.config = config
        # Rate limiting and retries are the two things that make a sync take
        # minutes instead of seconds, so the client reports them through an
        # injectable log rather than straight to stdout: a caller watching the
        # run (the developer dashboard) can then show *why* it is waiting
        # instead of a bar that appears to have stalled.
        self._log = log
        self._session = requests.Session()
        # When the last request went out, so pacing holds across every endpoint
        # rather than only between pages of one. Monotonic: a clock correction
        # mid-sync must not turn the next wait into a long sleep or none at all.
        self._last_request_at = 0.0
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
    def get_tasks(
        self,
        updated_since: int | None = None,
        *,
        on_page: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Return all tasks (work orders, PMs, requests) from ``/tasks``.

        ``updated_since`` is an optional Unix timestamp (seconds). Limble does
        not expose a universal "updated since" filter on this endpoint, so the
        filter is applied client-side against the most recent of each task's
        ``lastEdited`` / ``createdDate`` / ``dateCompleted`` values. Omit it for
        a full pull (the import is idempotent, so full pulls are safe).

        ``on_page`` is called after each page with ``(items_so_far, pages_read)``
        so a caller can report progress while the pull is still running.
        """

        params: dict[str, Any] = dict(self.config.extra_task_params)
        tasks = list(self._paginate("/tasks/", params, on_page=on_page))
        if updated_since is None:
            return tasks
        return [task for task in tasks if task_touched_at(task) >= updated_since]

    def get_assets(self, *, on_page: Callable[[int, int], None] | None = None) -> list[dict[str, Any]]:
        """Return all assets from ``/assets`` (used to enrich task asset info)."""

        return list(self._paginate("/assets/", {}, on_page=on_page))

    def get_task_instructions(self, task_id: Any) -> list[dict[str, Any]]:
        """Return one task's instruction items from ``/tasks/{taskID}/instructions``.

        This is a per-task sub-resource -- the same shape as
        ``/tasks/{taskID}/parts`` -- so the list endpoint does not carry it and
        each task costs its own request. Callers are expected to be selective
        about which tasks they ask for; see ``IngestionService``.

        Each item pairs the prompt with the tech's answer::

            {"instruction": "Affected area", "response": "Building 2A PMG",
             "type": "text box", "instructionID": 48, "parentInstructionID": 47,
             "instructionFiles": [], "options": [], "taskID": 239676}

        Note ``instruction``, not the ``instructionText`` a task export from the
        Limble site uses for the same field; services.wo_narrative reads both.

        This method answers exactly one question: **is this response a usable
        answer about this task's instructions?** It matters because of what the
        caller does with each outcome, and the two are not symmetric:

        * A usable answer -- including an empty one -- is taken as fact. The
          reader concludes the four boxes are blank, writes that over whatever it
          had stored, and stamps the task as read so it stops asking. A blank
          read is deliberately never retried.
        * An unusable one raises, which preserves the stored answers and brings
          the task back after ``RETRY_A_FAILED_READ_AFTER``.

        So mistaking the second for the first destroys evidence permanently and
        silently, while mistaking the first for the second costs one request every
        few hours. The check is drawn accordingly.

        A response is usable when it is a list, and -- if it carries any items at
        all -- at least one of them is a recognisable instruction record (see
        :data:`_INSTRUCTION_MARKER_KEYS`). Everything else raises: a wrapper
        object, ``["scheduled maintenance"]``, ``[{"message": "..."}]``, ``[{}]``.
        Junk *alongside* real records is passed through untouched, because that
        read plainly succeeded and the extractor already ignores what it cannot
        read.

        **Where this stops, and why it stops there.** The question is about the
        *record*, never about the *answers* it carries. Both of these return
        normally and must:

        * ``[]`` -- a task with no instructions.
        * forty instruction records carrying no Area/Condition/Cause/Action --
          a work order whose template never asked for the boxes, which on this
          account is most of the history.

        That is the end of the ladder rather than a place to stop for now. Each
        earlier rung asked a strictly structural question and the next rung asked
        a sharper one: is it a list, are its items objects, are those objects
        instructions. Past this point the only thing left to test is which labels
        the records carry, and that cannot distinguish a failure from a blank
        task -- the pre-template work orders are indistinguishable from an
        imagined "empty response" by content alone. Tightening further would
        turn most of the history into permanently retried failed reads.
        """

        payload = self._request("GET", f"/tasks/{task_id}/instructions/")
        if not isinstance(payload, list):
            raise LimbleResponseError(
                f"GET /tasks/{task_id}/instructions returned "
                f"{type(payload).__name__}, expected a list of instruction items"
            )
        if payload and not any(_is_instruction_record(item) for item in payload):
            raise LimbleResponseError(
                f"GET /tasks/{task_id}/instructions returned {len(payload)} item(s), "
                f"none of them instruction records (first was "
                f"{_describe(payload[0])})"
            )
        return [item for item in payload if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # Pagination + transport
    # ------------------------------------------------------------------
    def _paginate(
        self,
        path: str,
        params: dict[str, Any],
        *,
        on_page: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item from a paginated Limble list endpoint."""

        page = 1
        fetched = 0
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
                    fetched += 1
            if on_page is not None:
                on_page(fetched, page)
            if len(payload) < self.config.page_limit:
                break
            page += 1

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Perform one request with 429 handling and exponential backoff."""

        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"
        attempt = 0
        while True:
            self._pace()
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
                self._log(f"[limble] rate limited; sleeping {retry_after:.0f}s before retrying {path} ...")
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

    def _pace(self) -> None:
        """Hold off until this request is allowed to go out.

        Applied to every request rather than only between pages. Reading task
        instructions is one call per work order, and a run reads hundreds of
        them; unpaced, a backfill bursts through the rate limit, collects 429s
        and then sleeps a minute at a time -- slower than waiting would have
        been, and rude to an API that publishes a modest budget.
        """

        wait = self.config.seconds_per_request - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep_backoff(self, attempt: int, *, reason: str) -> None:
        # 2s, 4s, 8s, 16s ...
        delay = 2.0 ** attempt
        self._log(f"[limble] {reason}; retry {attempt}/{self.config.max_retries} in {delay:.0f}s ...")
        time.sleep(delay)


def _retry_after_seconds(resp: "requests.Response") -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def task_touched_at(task: dict[str, Any]) -> int:
    """Best-effort 'most recently touched' Unix time (seconds) for client-side filtering."""

    candidates = []
    for key in ("lastEdited", "createdDate", "dateCompleted", "startDate"):
        value = task.get(key)
        try:
            if value in (None, "", 0):
                continue
            number = float(value)
        except (TypeError, ValueError):
            continue
        # Normalise millisecond timestamps to seconds (same threshold the
        # transform uses) so the comparison with the seconds-based --since
        # cutoff is correct rather than treating a 13-digit value as far future.
        if number > 10_000_000_000:
            number /= 1000.0
        candidates.append(int(number))
    return max(candidates) if candidates else 0
