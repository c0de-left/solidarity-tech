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
pip install "solidarity-tech @ git+https://github.com/c0de-left/solidarity-tech@v0.3.0"
```

## Use

```python
from solidarity_tech import Client, Email, PhoneNumber, Session, UserMapper

client = Client()  # reads SOLIDARITY_API_KEY
session = Session(UserMapper(client, external_id="member-id"), apply=False)
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

`external_id` is the key of a custom property that ties each user to a record
elsewhere, and is optional. With it, users are written by `POST /users` on that
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

## Tests

```sh
pip install -e ".[dev]"
pytest
```
