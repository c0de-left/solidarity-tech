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
pip install "solidarity-tech @ git+https://github.com/c0de-left/solidarity-tech@v0.1.0"
```

## Use

Identity is an external ID: one custom property whose value ties a CRM user to
a record elsewhere.

```python
from solidarity_tech import Client, Email, PhoneNumber, PropertyMapper, Session, UserMapper

client = Client()  # reads SOLIDARITY_API_KEY
properties = PropertyMapper(client).find_all()
member_id = next(p for p in properties if p.key == "member-id")

session = Session(UserMapper(client, properties, member_id), apply=False)
session.load()

user = session.by_external_id()["1042"]
user.first_name = "Wanda"
user.emails.add(Email("wanda@work.example"))
user.phone_numbers.preferred = PhoneNumber("212-555-0101")

result = session.commit()
for user, method, body in result.planned:
    print(user.id, method, body)
```

With `apply=False` nothing is sent; `result.planned` is what would have been.

To see what an account holds:

```sh
SOLIDARITY_API_KEY=... python -m solidarity_tech check
```

## Tests

```sh
pip install -e ".[dev]"
pytest
```
