"""A paced, retrying gateway to the Solidarity Tech v1 API.

The transport and nothing else: authentication, the rate limit, retries, and
walking a collection by offset or by cursor. It knows no resource. What a
request carries and what a response means is `solidarity_tech.mapper`'s; a job
that only copies collections as they are can use this alone.

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

# Documented ceiling is 60 requests per 30 seconds. Pace to the average rather
# than the burst: the limiter bounds how fast requests start, and a burst that
# arrives together still counts together on their side.
REQUESTS_PER_SECOND = 2.0

REQUEST_TIMEOUT = 60
MAX_RETRIES = 5

# Their maximum; the default is 20.
PAGE_SIZE = 100

# What a cursor page falls back to when the API cannot answer one in time. See
# paginate_cursor.
SMALLER_PAGES = (25, 5, 1)


class Pacer:
    """Bound how often requests start, to stay under the per-second limit."""

    def __init__(self, per_second):
        self._interval = 1.0 / per_second
        self._last = 0.0

    def wait(self):
        delay = self._last + self._interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()


class Unanswered(RuntimeError):
    """A request that timed out or drew a 5xx on every attempt."""


class Client:
    """A paced, retrying reader and writer for the v1 API."""

    def __init__(self, token=None):
        token = token or os.environ["SOLIDARITY_API_KEY"]
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        self._pacer = Pacer(REQUESTS_PER_SECOND)
        self.requests_made = 0

    def _call(self, method, path, params=None, json_body=None, attempts=MAX_RETRIES):
        url = f"{API_BASE}{path}"
        attempt = throttled = 0
        while True:
            self._pacer.wait()
            self.requests_made += 1
            try:
                response = self._session.request(
                    method, url, params=params, json=json_body, timeout=REQUEST_TIMEOUT
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                attempt += 1
                # The request never got a verdict, which is the same situation a
                # 5xx describes and is handled the same way. A reset is not rare
                # over a run of tens of thousands of calls -- one ended a run of
                # 26,000 writes at the 3,700th -- and it escaped the retry below
                # because requests raises before there is a response to inspect.
                #
                # A retried write may be a repeat of one that landed. That is
                # harmless for an upsert on lookup_key or a PUT, which resolve to
                # the same user; a repeated note is written twice.
                if attempt == attempts:
                    raise Unanswered(
                        f"{type(exc).__name__} from {method} {url} after "
                        f"{attempts} attempts: {exc}"
                    ) from exc
                pause = float(2**attempt)
                log.warning(
                    "  %s from %s %s, retrying in %.0fs (attempt %d/%d)",
                    type(exc).__name__,
                    method,
                    url,
                    pause,
                    attempt,
                    attempts,
                )
                time.sleep(pause)
                continue

            if response.status_code == 429:
                # An answer, not the absence of one, so it is retried in full
                # whatever `attempts` says: paginate_cursor shortens that to ask
                # for a smaller page, and slowing down is no reason to.
                throttled += 1
                if throttled == MAX_RETRIES:
                    raise RuntimeError(
                        f"429 from {method} {url} after {MAX_RETRIES} attempts: "
                        f"{response.text[:500]}"
                    )
                pause = float(response.headers.get("Retry-After", 2**throttled))
                log.warning(
                    "  429 from %s %s, retrying in %.0fs (attempt %d/%d)",
                    method,
                    url,
                    pause,
                    throttled,
                    MAX_RETRIES,
                )
                time.sleep(pause)
                continue

            if response.status_code >= 500:
                attempt += 1
                if attempt == attempts:
                    raise Unanswered(
                        f"{response.status_code} from {method} {url} after "
                        f"{attempts} attempts: {response.text[:500]}"
                    )
                pause = float(response.headers.get("Retry-After", 2**attempt))
                log.warning(
                    "  %s from %s %s, retrying in %.0fs (attempt %d/%d)",
                    response.status_code,
                    method,
                    url,
                    pause,
                    attempt,
                    attempts,
                )
                time.sleep(pause)
                continue

            if not response.ok:
                raise RuntimeError(
                    f"{response.status_code} from {method} {url}: {response.text[:500]}"
                )
            return response.json() if response.content else None

    def get(self, path, params=None, attempts=MAX_RETRIES):
        return self._call("GET", path, params=params, attempts=attempts)

    def post(self, path, json_body):
        return self._call("POST", path, json_body=json_body)

    def put(self, path, json_body):
        return self._call("PUT", path, json_body=json_body)

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
        smallest page is retried as the other calls are. A success steps back
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
                page = (
                    self.get(
                        path, params=request, attempts=MAX_RETRIES if smallest else 1
                    )
                    or {}
                )
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

    def delete(self, path, params=None):
        """Remove an object. Not an upsert, and not recoverable.

        The retry in `_call` is safe on the writes above because they are
        upserts -- a repeat resolves to the same person. A repeated DELETE is
        harmless for a different reason: the second one 404s, which surfaces as
        an error rather than as damage.
        """
        return self._call("DELETE", path, params=params)

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
            page = self.get(path, params=params) or {}
            rows = page.get("data") or []
            yield from rows
            if len(rows) < PAGE_SIZE:
                return
            offset += PAGE_SIZE
