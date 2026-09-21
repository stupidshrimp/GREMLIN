"""_paginate's stop condition, against a server that caps its own page size.

The loop has to decide "was that the last page?" from the page it got back.
Comparing against the limit we *asked* for only works while the server hands
over exactly that many rows; a server that caps ``limit`` lower makes page one
look short, and a sync that stops after one page still reports success. These
pin the terminator to the server's actual page size instead.
"""

from integrations.limble import LimbleClient, LimbleConfig


class _CappingListEndpoint:
    """A Limble list endpoint that silently caps ``limit`` at ``cap``."""

    def __init__(self, total: int, cap: int) -> None:
        self.total = total
        self.cap = cap
        self.pages_requested: list[int] = []

    def __call__(self, method, path, *, params=None):
        params = params or {}
        limit = min(int(params.get("limit", 0)), self.cap)
        page = int(params.get("page", 1))
        self.pages_requested.append(page)
        start = (page - 1) * limit
        return [{"taskID": i} for i in range(start, min(start + limit, self.total))]


def _client_capped_at(cap: int, *, page_limit: int, total: int) -> tuple[LimbleClient, _CappingListEndpoint]:
    client = LimbleClient(LimbleConfig(client_id="id", client_secret="secret", page_limit=page_limit))
    endpoint = _CappingListEndpoint(total=total, cap=cap)
    client._request = endpoint
    return client, endpoint


def test_pagination_survives_a_server_capping_below_the_requested_limit():
    """Ask for 1000, get 200 a page, still walk all 2500 rows.

    This is the case that silently truncated: page one came back with 200
    rows, 200 < 1000 read as "that was the end", and the sync stopped with
    2300 rows unread and nothing to show for it.
    """

    client, endpoint = _client_capped_at(200, page_limit=1000, total=2500)

    assert len(list(client._paginate("/tasks/", {}))) == 2500
    assert endpoint.pages_requested == list(range(1, 14))


def test_pagination_walks_a_server_that_honours_the_requested_limit():
    client, endpoint = _client_capped_at(1000, page_limit=1000, total=2500)

    assert len(list(client._paginate("/tasks/", {}))) == 2500
    assert endpoint.pages_requested == [1, 2, 3]


def test_a_first_page_holding_everything_ends_pagination():
    """Fewer rows than a page: one extra request to confirm, then done."""

    client, endpoint = _client_capped_at(1000, page_limit=1000, total=50)

    assert len(list(client._paginate("/tasks/", {}))) == 50
    assert endpoint.pages_requested == [1, 2]


def test_an_empty_first_page_yields_nothing():
    client, _ = _client_capped_at(1000, page_limit=1000, total=0)

    assert list(client._paginate("/tasks/", {})) == []
