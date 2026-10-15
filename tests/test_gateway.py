"""How the cursor pager decides it has reached the end.

Offset paging ends on a short page. Cursor paging does not, and conflating the
two is how a copy silently truncates: the first page under the limit would end
the walk and every activity older than it would simply never arrive, with no
error and a plausible-looking row count.
"""

import pytest
import requests

from solidarity_tech import gateway
from solidarity_tech.gateway import Client, Unanswered


class FakeClient(Client):
    """A client whose pages are scripted rather than fetched."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get(self, path, params=None, attempts=None):
        self.calls.append(dict(params or {}))
        answer = self._pages.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def page(rows, next_cursor=None):
    return {"data": rows, "meta": {"next_cursor": next_cursor}}


def test_it_follows_the_cursor_across_pages():
    client = FakeClient([page([1, 2], "c1"), page([3, 4], "c2"), page([5])])
    assert list(client.paginate_cursor("/activities")) == [1, 2, 3, 4, 5]


def test_a_short_page_is_not_the_end_when_a_cursor_follows_it():
    client = FakeClient([page([1], "c1"), page([2, 3])])
    assert list(client.paginate_cursor("/activities")) == [1, 2, 3]


def test_a_full_page_is_the_end_when_no_cursor_follows_it():
    client = FakeClient([page(list(range(100)))])
    assert len(list(client.paginate_cursor("/activities"))) == 100
    assert len(client.calls) == 1


def test_filters_are_sent_on_every_page_beside_the_cursor():
    client = FakeClient([page([1], "c1"), page([2])])
    list(client.paginate_cursor("/activities", {"_since": 1755000000}))
    assert client.calls[0] == {"_since": 1755000000, "_limit": 100}
    assert client.calls[1] == {"_since": 1755000000, "_limit": 100, "_cursor": "c1"}


def test_an_empty_collection_yields_nothing_rather_than_looping():
    client = FakeClient([page([])])
    assert list(client.paginate_cursor("/activities")) == []


@pytest.mark.parametrize("meta", [{}, {"next_cursor": None}, {"next_cursor": ""}])
def test_a_missing_or_empty_cursor_all_mean_the_end(meta):
    client = FakeClient([{"data": [1], "meta": meta}])
    assert list(client.paginate_cursor("/activities")) == [1]


def test_a_page_that_goes_unanswered_is_asked_for_again_smaller():
    """At the same cursor, then back up a size at a time as pages come through."""
    timed_out = Unanswered("ReadTimeout")
    client = FakeClient(
        [
            page([1], "c1"),
            timed_out,
            timed_out,
            page([2], "c2"),
            page([3], "c3"),
            page([4], "c4"),
            page([5]),
        ]
    )
    assert list(client.paginate_cursor("/activities")) == [1, 2, 3, 4, 5]
    assert [c["_limit"] for c in client.calls] == [100, 100, 25, 5, 25, 100, 100]
    assert [c.get("_cursor") for c in client.calls] == [
        None,
        "c1",
        "c1",
        "c1",
        "c2",
        "c3",
        "c4",
    ]


def test_a_page_keeps_its_filters_when_it_shrinks():
    client = FakeClient([page([1], "c1"), Unanswered("504"), page([2])])
    list(client.paginate_cursor("/activities", {"_since": 1755000000}))
    assert client.calls[2] == {"_since": 1755000000, "_limit": 25, "_cursor": "c1"}


def test_a_page_unanswered_even_at_one_row_fails_the_walk():
    client = FakeClient([Unanswered("504")] * 4)
    with pytest.raises(Unanswered):
        list(client.paginate_cursor("/activities"))
    assert [c["_limit"] for c in client.calls] == [100, 25, 5, 1]


class Reply:
    def __init__(self, status, body=b"{}", headers=None):
        self.status_code = status
        self.content = body
        self.text = body.decode()
        self.headers = headers or {}
        self.ok = status < 400

    def json(self):
        return {}


class ScriptedSession:
    def __init__(self, replies):
        self._replies = list(replies)

    def request(self, *args, **kwargs):
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def scripted_client(replies, monkeypatch):
    monkeypatch.setattr(gateway.time, "sleep", lambda seconds: None)
    client = Client.__new__(Client)
    client._session = ScriptedSession(replies)
    client._pacer = gateway.Pacer(1000)
    client.requests_made = 0
    return client


def test_a_429_is_waited_out_even_on_a_single_attempt(monkeypatch):
    """Being told to slow down is an answer, so it does not shrink a page."""
    client = scripted_client(
        [Reply(429, headers={"Retry-After": "30"}), Reply(200)], monkeypatch
    )
    assert client.get("/activities", attempts=1) == {}
    assert client.requests_made == 2


def test_a_429_that_persists_fails_without_claiming_no_answer(monkeypatch):
    client = scripted_client([Reply(429)] * gateway.MAX_RETRIES, monkeypatch)
    with pytest.raises(RuntimeError) as raised:
        client.get("/activities", attempts=1)
    assert not isinstance(raised.value, Unanswered)


@pytest.mark.parametrize(
    "reply", [Reply(504), requests.Timeout("read timed out")], ids=["504", "timeout"]
)
def test_a_single_attempt_is_unanswered_on_a_5xx_or_a_timeout(reply, monkeypatch):
    client = scripted_client([reply], monkeypatch)
    with pytest.raises(Unanswered):
        client.get("/activities", attempts=1)
