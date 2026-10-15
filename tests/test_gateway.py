"""How the cursor pager decides it has reached the end.

Offset paging ends on a short page. Cursor paging does not, and conflating the
two is how a copy silently truncates: the first page under the limit would end
the walk and every activity older than it would simply never arrive, with no
error and a plausible-looking row count.
"""

import json

import pytest
import requests
from requests.adapters import BaseAdapter

from solidarity_tech.gateway import Client, HTTPError, Unanswered


class Scripted(BaseAdapter):
    """A transport that answers from a script: a page, a status, or an
    exception to raise."""

    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, payload, headers = (
            reply if isinstance(reply, tuple) else (200, reply, {})
        )
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        response.headers.update(headers)
        response.request, response.url = request, request.url
        return response

    def close(self):
        pass


class FakeClient(Client):
    requests_per_second = 0
    retry_wait_seconds = 0

    def __init__(self, replies):
        super().__init__(token="t")
        self.adapter = Scripted(replies)
        self.mount("https://", self.adapter)

    @property
    def calls(self):
        return [
            {
                k: (int(v) if v.isdigit() else v)
                for k, v in (
                    pair.split("=") for pair in (r.url.split("?") + [""])[1].split("&")
                )
            }
            for r in self.adapter.sent
        ]


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
    timed_out = (504, {}, {})
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
    client = FakeClient([page([1], "c1"), (504, {}, {}), page([2])])
    list(client.paginate_cursor("/activities", {"_since": 1755000000}))
    assert client.calls[2] == {"_since": 1755000000, "_limit": 25, "_cursor": "c1"}


def test_a_page_unanswered_even_at_one_row_fails_the_walk():
    client = FakeClient([(504, {}, {})] * 8)
    with pytest.raises(Unanswered):
        list(client.paginate_cursor("/activities"))
    assert [c["_limit"] for c in client.calls] == [100, 25, 5, 1, 1, 1, 1, 1]


def test_a_429_is_waited_out_even_with_retries_off(monkeypatch):
    """Being told to slow down is an answer, so it does not shrink a page."""
    waits = []
    monkeypatch.setattr("time.sleep", waits.append)
    client = FakeClient([(429, {}, {"Retry-After": "30"}), {"data": []}])
    assert client.get("/activities", retry_attempts=0).json() == {"data": []}
    assert client.requests_made == 2
    assert waits == [30.0]


def test_a_429_that_persists_is_a_refusal_not_no_answer():
    client = FakeClient([(429, {}, {"Retry-After": "0"})] * 5)
    with pytest.raises(HTTPError):
        client.get("/activities", retry_attempts=0)


@pytest.mark.parametrize(
    "reply", [(504, {}, {}), requests.Timeout("read timed out")], ids=["504", "timeout"]
)
def test_with_retries_off_a_5xx_or_a_timeout_is_unanswered(reply):
    client = FakeClient([reply])
    with pytest.raises(Unanswered):
        client.get("/activities", retry_attempts=0)


def test_no_answer_is_asked_again_and_an_answer_is_returned():
    client = FakeClient([requests.ConnectionError("reset"), (502, {}, {}), {"id": 7}])
    assert client.get("/users/7").json() == {"id": 7}


def test_a_refusal_carries_the_apis_explanation():
    client = FakeClient([(422, {"error": "Phone number is not textable"}, {})])
    with pytest.raises(HTTPError, match="422 .*not textable"):
        client.post("/users", json={})
