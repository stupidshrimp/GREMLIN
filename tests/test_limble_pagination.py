"""LimbleClient pagination: when a pull stops asking for more pages.

No HTTP here -- `_request` is replaced with a fake server that holds a fixed
list of records and hands them out a page at a time, capped at whatever page
size it chooses, the way a real API may cap `limit` below what was asked for.
"""

from integrations.limble import LimbleClient, LimbleConfig


def _client_against(records, *, page_limit, server_cap=None):
    """A client whose requests are served from `records`, `server_cap` per page."""

    client = LimbleClient(LimbleConfig(client_id="id", client_secret="secret", page_limit=page_limit))
    requested_pages = []

    def fake_request(method, path, *, params=None):
        page, limit = params["page"], params["limit"]
        requested_pages.append(page)
        size = min(limit, server_cap) if server_cap else limit
        start = (page - 1) * size
        return records[start:start + size]

    client._request = fake_request
    return client, requested_pages


def _records(count):
    return [{"taskID": i} for i in range(count)]


def test_a_server_that_caps_pages_below_the_limit_is_still_read_to_the_end():
    # Asking for 1000 a page from a server that only sends 200: the first
    # page coming back "short" must not be taken for the last one.
    client, _ = _client_against(_records(450), page_limit=1000, server_cap=200)

    assert len(list(client._paginate("/tasks/", {}))) == 450


def test_full_pages_are_followed_until_a_short_one():
    client, requested = _client_against(_records(250), page_limit=100)

    assert len(list(client._paginate("/tasks/", {}))) == 250
    # 100, 100, then 50 -- short of the pages before it, so that's the end.
    assert requested == [1, 2, 3]


def test_everything_on_one_short_page_costs_one_empty_request():
    client, requested = _client_against(_records(30), page_limit=1000)

    assert len(list(client._paginate("/tasks/", {}))) == 30
    assert requested == [1, 2]


def test_an_exact_multiple_of_the_page_size_stops_on_the_empty_page():
    client, requested = _client_against(_records(400), page_limit=1000, server_cap=200)

    assert len(list(client._paginate("/tasks/", {}))) == 400
    assert requested == [1, 2, 3]
