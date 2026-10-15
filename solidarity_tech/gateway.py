"""A paced, retrying gateway to the Solidarity Tech v1 API.

The transport and nothing else: authentication, the rate limit, retries, and
walking a collection by offset or by cursor. It knows no resource. What a
request carries and what a response means is `solidarity_tech.mapper`'s; a job
that only copies collections as they are can use this alone.

Each concern is a `requests.Session` that overrides `request` and hands on to
the next, so a `Client` is a session like any other: `client.get(path)`
returns a `Response`, and a test mounts a transport adapter rather than
patching anything. Pacing sits below retrying, so every attempt is paced.

Rate limit is 60 requests per 30 seconds, which is 2/second averaged, enforced
with a 429 carrying `Retry-After`. There is no documented daily cap. At 2/s
40,000 people written one request each is five and a half hours, so a first
load of a large account belongs in the CSV importer.
"""

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.solidarity.tech/v1"

# Their maximum; the default is 20.
PAGE_SIZE = 100

# What a cursor page falls back to when the API cannot answer one in time. See
# paginate_cursor.
SMALLER_PAGES = (25, 5, 1)


class HTTPError(requests.HTTPError):
    """A request the API answered and refused."""


class Unanswered(requests.RequestException):
    """A request that timed out or drew a 5xx on every attempt."""


class ThrottledSession(requests.Session):
    """Bound how often requests start.

    The documented ceiling is 60 requests per 30 seconds. Paced to the average
    rather than the burst: the limit bounds how fast requests start, and a
    burst that arrives together still counts together on their side.
    """

    requests_per_second = 2.0

    def __init__(self):
        super().__init__()
        self.requests_made = 0
        self._last_request = 0.0

    def request(self, method, url, **kwargs):
        if self.requests_per_second:
            delay = self._last_request + 1 / self.requests_per_second - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        self._last_request = time.monotonic()
        self.requests_made += 1
        return super().request(method, url, **kwargs)


class RetrySession(ThrottledSession):
    """Ask again when there was no answer, and wait when told to.

    A timeout, a dropped connection and a 5xx are the same situation -- no
    verdict -- and are retried `retry_attempts` times, waiting twice as long
    each time. A reset is not rare over tens of thousands of calls: one ended a
    run of 26,000 writes at the 3,700th. A retried write may repeat one that
    landed, which is harmless for an upsert on `lookup_key` or a PUT, surfaces
    as a 404 for a DELETE, and writes a note twice.

    A 429 is an answer, not the absence of one, so it is waited out for as long
    as `Retry-After` says, up to `throttled_attempts` times, whatever
    `retry_attempts` is: a caller that turns retries off to ask for a smaller
    page has no reason to stop slowing down.
    """

    retry_attempts = 4
    retry_wait_seconds = 2.0
    throttled_attempts = 5

    def request(self, method, url, retry_attempts=None, **kwargs):
        attempts = self.retry_attempts if retry_attempts is None else retry_attempts
        tries = throttled = 0
        while True:
            try:
                response = super().request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                failure, cause = f"{type(exc).__name__}: {exc}", exc
            else:
                if response.status_code == 429:
                    throttled += 1
                    if throttled < self.throttled_attempts:
                        wait = float(response.headers.get("Retry-After", 2**throttled))
                        log.warning(
                            "  429 from %s %s, waiting %.0fs", method, url, wait
                        )
                        time.sleep(wait)
                        continue
                    return response
                if response.status_code < 500:
                    return response
                failure, cause = f"{response.status_code}: {response.text[:500]}", None
            if tries == attempts:
                raise Unanswered(
                    f"{method} {url}, {tries + 1} attempts: {failure}"
                ) from cause
            tries += 1
            wait = self.retry_wait_seconds * 2 ** (tries - 1)
            log.warning(
                "  %s from %s %s, retrying in %.0fs", failure, method, url, wait
            )
            time.sleep(wait)


class Client(RetrySession):
    """The v1 API, by path: `client.get("/users/7").json()`.

    The token is `SOLIDARITY_API_KEY` unless given. A refusal raises
    `HTTPError` carrying the API's own explanation, which is usually the only
    account of what was wrong with a write.
    """

    timeout = 60

    def __init__(self, token=None):
        super().__init__()
        token = token or os.environ["SOLIDARITY_API_KEY"]
        self.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    def request(self, method, url, **kwargs):
        if url.startswith("/"):
            url = API_BASE + url
        kwargs.setdefault("timeout", self.timeout)
        response = super().request(method, url, **kwargs)
        if not response.ok:
            raise HTTPError(
                f"{response.status_code} from {method} {url}: {response.text[:500]}",
                response=response,
            )
        return response

    def paginate_cursor(self, path, params=None):
        """Yield every object in a collection, following `meta.next_cursor`.

        `/activities` is why this exists. It rejects `_offset` past 10,000, and
        that ceiling is not a depth limit in practice: an integration writing
        a dozen properties for each of tens of thousands of people logs an
        activity per edit, and 10,000 can be a day and a half. Offset paging
        cannot reach last week.

        The cursor is the id of the last activity returned, and `_since` is
        sent with it on every page rather than trusted to ride along. A page
        shorter than the limit is not the end here -- `next_cursor` being
        absent is.

        A page the API cannot answer in time is asked for again, at the same
        cursor, in a smaller size, rather than repeated. From 2026-09-22 a nightly
        walk of `/activities` failed on one page of 100 that timed out five times
        running (the gateway gives up at 90 seconds), while 5 at the same cursor
        came back in under a second; a minute later all 100 did. Only the
        smallest page is retried as other requests are. A success steps back
        up one size, not straight to 100: the slow pages come in runs, and each
        page of 100 that fails costs the full timeout.
        """
        first = dict(params or {})
        sizes = (PAGE_SIZE, *SMALLER_PAGES)
        cursor = None
        step = 0
        while True:
            request = {**first, "_limit": sizes[step]}
            if cursor is not None:
                request["_cursor"] = cursor
            smallest = step == len(sizes) - 1
            try:
                page = self.get(
                    path, params=request, retry_attempts=None if smallest else 0
                ).json()
            except Unanswered as exc:
                if smallest:
                    raise
                step += 1
                log.warning("  %s; asking for %d instead", exc, sizes[step])
                continue
            step = max(step - 1, 0)
            yield from page.get("data") or []
            cursor = (page.get("meta") or {}).get("next_cursor")
            if not cursor:
                return

    def paginate(self, path, params=None):
        """Yield every object in a collection, walking `_offset`.

        Only sound for a collection nothing writes to during the walk. The API
        returns rows in no stable order -- not by id, not by update -- and an
        update moves a row, so a write mid-walk shifts every offset after it:
        one row is read twice and another never. `/users` is written to all
        day and is walked by cursor instead (see UserMapper.find_all).

        The last page is the one that comes back short, which is also the only
        signal available: `meta.total_count` is populated on some collections
        and omitted on others.
        """
        params = dict(params or {})
        params["_limit"] = PAGE_SIZE
        offset = 0
        while True:
            params["_offset"] = offset
            page = self.get(path, params=params).json()
            rows = page.get("data") or []
            yield from rows
            if len(rows) < PAGE_SIZE:
                return
            offset += PAGE_SIZE
