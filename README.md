# solidarity-tech

Data mappers for the [Solidarity Tech](https://www.solidarity.tech) CRM API.

Users, custom properties and notes are plain objects. Mappers turn the API's
JSON into them and turn changes back into requests, and a `Session` holds the
users of one run and writes what changed. The objects know nothing about HTTP,
and the gateway underneath knows nothing about users.

The point is the API's behaviour, most of which is undocumented and was found
by testing against a live account. Each rule is written down where it is
enforced. Some examples:

- **A select only keeps an exact option value.** Anything else is answered
  with a 200 and ignored, so `UserMapper.check` refuses it before it is sent.
- **Text written to a number property is stored empty**, erasing what was
  there. Also refused.
- **`POST /users` with a `lookup_key` that misses** falls back to matching on
  email and phone, and overwrites the external ID of whoever it finds. The
  response is only `{id, message}`, so the Session compares the id with the
  users it read and reports a write that landed on someone else.
- **Adding an email or phone number** means writing each one as the primary in
  turn, then ending on the preferred one. `Contacts` lets the caller just add
  them and name the preferred one; the mapper sends the sequence.
- **An address or number another user holds** is withheld rather than written,
  since writing it would be refused or would take that user over.
- **`/users` must be walked by cursor.** Offset paging reads some users twice
  and skips others when the account is written to during the walk.
- **Merging deletes the duplicates**, and with them a team member's login, so
  the Session refuses to merge a login away.

## Install

```sh
pip install "solidarity-tech @ git+https://github.com/c0de-left/solidarity-tech@v0.7.0"
```

## Use

```python
from solidarity_tech import Client, Email, PhoneNumber, Session, UserMapper

client = Client()  # reads SOLIDARITY_API_KEY
session = Session(users=UserMapper(client, external_id="member-id"), apply=False)
session.load()

user = session.users.get(external_id="1042")
user.first_name = "Wanda"
user.properties["membership-status"] = "Retired"
user.emails.add(Email("wanda@work.example"))
user.phone_numbers.preferred = PhoneNumber("212-555-0101")

result = session.commit()
for user, method, body in result.planned:
    print(user.id, method, body)
```

With `apply=False` nothing is sent; `result.planned` is what would have been.

`session.users.get` takes one of `id`, `external_id`, `email` or
`phone_number`. An email or number finds whoever holds it, preferred or not,
compared as the CRM compares them: case-insensitively, and ignoring how a
number is written.

`external_id` is the key of a custom property that ties each user to a record
elsewhere, and is optional. Its value is `user.external_id`, not one of
`user.properties`. With it, users are written by `POST /users` on that
`lookup_key`, and the Session holds it to one user per value, never changed.
Without it, users are written by id, and a new user is posted bare -- which the
CRM matches on email and phone, so a create that lands on an existing user is
reported in `result.misdirected`.

Custom properties are by key, and a select's value is its option's label. The
mapper reads the account's properties when it first needs them, and refuses a
value the CRM would accept and not keep.

`Client` is a `requests.Session`: `client.get("/chapters").json()`. Pacing and
retries are layers beneath it, `ThrottledSession` and `RetrySession`, set by
attributes such as `requests_per_second` and `retry_attempts`.

To see what an account holds:

```sh
SOLIDARITY_API_KEY=... python -m solidarity_tech check
```

## Loading reads the whole account

`session.load()` reads every user in the account, along with its custom
properties and its team members. At 100 users a page and the API's 2 requests
a second, 40,000 users is about 400 requests and three and a half minutes.

That is deliberate, for two reasons.

**The API cannot select users.** `GET /users` filters on an exact email, an
exact phone number, "updated since", or a saved user list, and nothing else:
not a custom property (so not an external ID), not a chapter, not a tag or a
name. A saved list can express more, but it is built in the dashboard. So the
session holds every user and finds them in memory: `session.users.get(...)`
uses indexes built at load and kept through each commit, and anything else
is a comprehension over `session.users`.

**What the session guarantees needs every user.**

- **Only changes are sent.** Each user is compared with what the CRM held
  when read, so an unchanged account costs no writes.
- **No takeover.** An address or number another user holds is withheld,
  which needs to know who holds every one.
- **A misdirected write is caught.** A `POST /users` answers with an id and
  nothing else, and the session checks that id against every user it read.
- **One user per external ID.** Two users sharing one is an error at load,
  before an upsert can land on either.

The users are walked by cursor from `_cursor=start`. Offset paging reorders as
the account is written to, and has read some users twice and skipped others
mid-walk.

Lookups other than by `id` are as of the last load or commit: a user given an
external ID or an address since is found by it once it is written.

## Tests

```sh
pip install -e ".[dev]"
pytest
```
