"""Solidarity Tech's users and custom properties as objects, and the requests
that read and write them.

Nothing here knows about any one organisation. Each mapper turns the API's
JSON into objects and objects back into requests, and a Session holds the users
of one run: one object per CRM user, a snapshot of each as read, and the
requests that make the account say what the objects now say.

Values are compared as they would be sent, not as they were read. A select
reads back as a list of every option its stored string contains and is written
as one option's value; a date reads as a string and is assigned as a date. Both
sides go through the same encoding, so a change is a difference in what the
request would carry.

A user is its CRM id. It may also have an external ID: the value of one custom
property, named as the `lookup_key` on writes, that ties the user to a record
elsewhere. Without one, users read are written by `PUT /users/{id}`, and a user
created is posted bare, which the CRM matches on email and phone -- so a
create can land on an existing user, and is reported when it does. With one,
the API enforces nothing about it, so the Session does:

  1. One user per external ID. Two holding the same one is an error at load,
     since an upsert lands on one of them arbitrarily.
  2. A user read with an external ID is written by `POST /users` on it; a user
     read without one by `PUT /users/{id}`. Giving a user its external ID is
     therefore a PUT. A POST would miss, and the CRM would create somebody or
     match somebody else by phone.
  3. An external ID is never changed or cleared once a user holds one: the next
     run would no longer find the user and would create them again.
  4. A POST whose lookup misses falls back to matching on email and phone, and
     overwrites the external ID of whoever holds them (tested 2026-10-14). The
     response is only `{id, message}`, so it is checked against the ids
     already read; a write that lands on another user is reported.

Creating a user is refused without `chapter_id` (unless `chapter_ids` is
given), and without an email or phone unless `require_contact_info` is false.

What the API can carry:

  * **Chapter** is the only first-class grouping of people. A user has one
    primary `chapter_id` and, where the account has the multi-chapter feature,
    a `chapter_ids` set. Chapters, and the organizations above them, are GET
    only: one is created in the dashboard or not at all.
  * **Nothing below chapter exists** -- no department, unit, worksite or turf.
    It is carried as a custom property or a tag, and neither can hold
    attributes of its own.
  * **Filtering users by chapter is indirect.** `GET /users` takes only
    `email`, `phone_number`, `_since` and `user_list_ids`; selecting a
    chapter's members means a user list whose rules name it.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True, eq=False)
class Email:
    """An email address, equal to another however either is cased."""

    address: str

    def __post_init__(self):
        if not self.address or not self.address.strip():
            raise ValueError("an email address cannot be empty")

    @property
    def key(self):
        return self.address.strip().lower()

    def __eq__(self, other):
        return isinstance(other, Email) and self.key == other.key

    def __hash__(self):
        return hash(("email", self.key))

    def __str__(self):
        return self.address


@dataclass(frozen=True, eq=False)
class PhoneNumber:
    """A phone number, equal to another however either is written.

    The CRM hands a number back with a country code it was not given:
    `9178812287` comes back `19178812287`.
    """

    number: str

    def __post_init__(self):
        if not self.number or not self.number.strip():
            raise ValueError("a phone number cannot be empty")

    @property
    def key(self):
        digits = re.sub(r"\D", "", self.number)
        return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits

    def __eq__(self, other):
        return isinstance(other, PhoneNumber) and self.key == other.key

    def __hash__(self):
        return hash(("phone", self.key))

    def __str__(self):
        return self.number


def text(label):
    """A label, whichever shape the endpoint returns it in.

    `/custom_user_properties` returns a localised object, `{"en": "Retired"}`;
    `/users` returns the bare string for the same option.
    """
    if isinstance(label, dict):
        return label.get("en")
    return label


class MatchedSomeoneElse(RuntimeError):
    """A write landed on a different user than the one it was for."""


class DuplicateExternalId(RuntimeError):
    """Two users hold one external ID."""


class ExternalIdChanged(RuntimeError):
    """A user's external ID was reassigned or cleared."""


class LoginMergedAway(RuntimeError):
    """A merge would delete a user a team member signs in as."""


class KeyNotDerived(RuntimeError):
    """A created property did not get the key it was declared with."""


@dataclass(eq=False)
class Option:
    """One choice in a select. Identified by its label within its property:
    the value is the CRM's, and an option added in the dashboard gets a
    generated one, e.g. `9MApPDC0`."""

    label: str
    value: str | None = None


@dataclass(eq=False)
class Property:
    """A custom user property, identified by its key -- the internal name every
    value is written under.

    The CRM derives the key from the label when the property is created, mostly
    as kebab-case but not always ("Participant - Committee/WG" is
    `participant`), so a declared key is checked rather than assumed.
    """

    key: str
    label: str
    type: str
    options: list[Option] = field(default_factory=list)
    description: str | None = None
    id: int | None = None

    def option(self, label):
        return next((o for o in self.options if o.label == label), None)


class PropertyMapper:
    """`/custom_user_properties`: read, and create. The API has no update and
    no delete, so a wrong type or pick list is repaired in the dashboard."""

    def __init__(self, client):
        self.client = client

    def find_all(self):
        return [
            self.hydrate(raw) for raw in self.client.paginate("/custom_user_properties")
        ]

    @staticmethod
    def hydrate(raw):
        return Property(
            key=raw.get("key") or raw.get("internal_name"),
            label=text(raw.get("label") or raw.get("name")),
            type=raw.get("field_type"),
            # A bare string is accepted as an option on create and read back
            # as one; the dashboard shows it as an empty row.
            options=[
                Option(text(o.get("label")), o.get("value"))
                for o in raw.get("options") or []
                if isinstance(o, dict) and text(o.get("label")) is not None
            ],
            description=raw.get("description"),
            id=raw.get("id"),
        )

    def insert(self, prop):
        body = {"label": prop.label, "field_type": prop.type}
        if prop.options:
            # A label per locale, or the dashboard renders a picker of blank
            # rows. Option payloads are not validated at all.
            body["options"] = [
                {"label": {"en": o.label}, "value": o.label} for o in prop.options
            ]
        if prop.description:
            body["description"] = prop.description
        self.client.post("/custom_user_properties", json=body)
        created = next((p for p in self.find_all() if p.key == prop.key), None)
        if created is None:
            raise KeyNotDerived(
                f"created {prop.label!r}, and no property has the key {prop.key!r};"
                " the CRM derived a different one from the label"
            )
        prop.id = created.id
        for option in prop.options:
            option.value = (created.option(option.label) or option).value


@dataclass
class Note:
    """A note on a user's profile."""

    content: str
    created_at: datetime


class NoteMapper:
    """`/user_notes`: insert, and nothing else.

    There is no GET, so nothing can tell whether a note is already there, and
    inserting one twice writes it twice. The API takes the text and the date
    and no author: a note is attributed to whoever owns the API key.
    """

    def __init__(self, client):
        self.client = client

    def insert(self, user, note):
        self.client.post(
            "/user_notes",
            json={
                "user_id": user.id,
                "content": note.content,
                "created_at": int(note.created_at.timestamp()),
            },
        )


class Contacts:
    """Every email, or every phone number, a user holds, and which one is
    preferred -- the CRM's primary. The preferred one is always held: naming
    it adds it."""

    def __init__(self, held=(), preferred=None):
        self._held = set(held)
        self._preferred = None
        self.preferred = preferred

    @property
    def preferred(self):
        return self._preferred

    @preferred.setter
    def preferred(self, contact):
        if contact is not None:
            self._held.add(contact)
        self._preferred = contact

    def add(self, contact):
        self._held.add(contact)

    def discard(self, contact):
        self._held.discard(contact)
        if self._preferred == contact:
            self._preferred = None

    def __ior__(self, contacts):
        self._held |= set(contacts)
        return self

    def __contains__(self, contact):
        return contact in self._held

    def __iter__(self):
        return iter(self._held)

    def __len__(self):
        return len(self._held)

    def __repr__(self):
        return f"Contacts({sorted(map(str, self._held))}, preferred={self._preferred})"


@dataclass(eq=False)
class User:
    """One CRM user.

    Adding to `emails` or `phone_numbers` and naming the preferred one is all
    a caller does; how the CRM is made to hold that is the mapper's.

    `id` is None until the user is inserted, and `sms_permission` is None until
    then too: left out of the create, it is whatever the CRM defaults to, which
    is granted. Everything else the API returns on every user.

    `properties` are by key, a select's by option label:
    `user.properties["membership-status"] = "Retired"`.

    `login` is whether a team member signs in as this user. The user record
    does not say; only `/team_members` does. It is read, never written.
    """

    chapter_id: int
    email_permission: bool
    id: int | None = None
    sms_permission: bool | None = None
    first_name: str | None = None
    last_name: str | None = None
    alternate_name: str | None = None
    emails: Contacts = field(default_factory=Contacts)
    phone_numbers: Contacts = field(default_factory=Contacts)
    address: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    tags: set[str] = field(default_factory=set)
    assessment: str | None = None
    login: bool = False

    def holds(self):
        return {*self.emails, *self.phone_numbers}


# What a write may carry besides contacts, in the order a request lists it.
SCALARS = (
    "first_name",
    "last_name",
    "alternate_name",
    "email_permission",
    "sms_permission",
    "assessment",
    "chapter_id",
)

# The preferred field and the set it is one of.
CONTACTS = (("email", "emails"), ("phone_number", "phone_numbers"))

# The address parts a write sends. latitude and longitude come back too, and
# are the CRM's geocoding of these.
ADDRESS_PARTS = ("address1", "address2", "city", "state", "zip_code", "country")


class UserMapper:
    """`/users` and `/users/merge`.

    `external_id` is the key of the property that ties a user to a record
    elsewhere, if any. The account's properties are read when first needed,
    unless given.
    """

    def __init__(self, client, external_id=None, properties=None):
        self.client = client
        self.external_id_key = external_id
        self._properties = (
            None if properties is None else {p.key: p for p in properties}
        )

    @property
    def properties(self):
        if self._properties is None:
            self._properties = {
                p.key: p for p in PropertyMapper(self.client).find_all()
            }
        return self._properties

    def property(self, key):
        try:
            return self.properties[key]
        except KeyError:
            raise KeyError(f"the account has no custom property {key!r}") from None

    def find_all(self):
        """Every user, walked by cursor.

        `/users` takes a cursor as well as an offset, ordered by creation and
        then id, so a user updated during the walk stays where it was and one
        created during it lands at the end. Offset paging read 29 users twice
        on 2026-10-06; on 2026-10-08 it read one twice and missed another,
        updated two minutes into the walk. The first page is asked for with
        `_cursor=start`; no other collection accepts one.
        """
        logins = {m["user_id"] for m in self.client.paginate("/team_members")}
        for raw in self.client.paginate_cursor("/users", {"_cursor": "start"}):
            user = self.hydrate(raw)
            user.login = user.id in logins
            yield user

    def hydrate(self, raw):
        properties = {}
        for key, value in (raw.get("custom_user_properties") or {}).items():
            prop = self.properties.get(key)
            properties[key] = value if prop is None else self.read_value(prop, value)
        assessment = raw.get("assessment")
        return User(
            id=raw["id"],
            chapter_id=raw["chapter_id"],
            email_permission=raw["email_permission"],
            sms_permission=raw["sms_permission"],
            first_name=raw.get("first_name"),
            last_name=raw.get("last_name"),
            alternate_name=raw.get("alternate_name"),
            emails=Contacts(
                [Email(v) for v in raw.get("other_emails") or [] if v],
                Email(raw["email"]) if raw.get("email") else None,
            ),
            phone_numbers=Contacts(
                [PhoneNumber(v) for v in raw.get("other_phone_numbers") or [] if v],
                PhoneNumber(raw["phone_number"]) if raw.get("phone_number") else None,
            ),
            address=dict(raw.get("address") or {}),
            properties=properties,
            tags=set(raw.get("tags") or []),
            assessment=assessment.get("key") if isinstance(assessment, dict) else None,
        )

    @staticmethod
    def read_value(prop, value):
        """A property's value as read, in the terms it is assigned in.

        A select reads back as every option whose value the stored string
        contains, so a stored "Non-Teaching Adjuncts" reads as that and
        "Teaching Adjuncts" both. Each is a substring of what was written, so
        the longest is what was. Checkboxes hold several on purpose.
        """
        if prop.type == "checkboxes" and isinstance(value, list):
            return [text(o.get("label")) for o in value if isinstance(o, dict)]
        if isinstance(value, list):
            held = [o for o in value if isinstance(o, dict)]
            if not held:
                return None
            written = max(held, key=lambda o: len(o.get("value") or ""))
            return next(
                (o.label for o in prop.options if o.value == written.get("value")),
                written.get("value"),
            )
        if prop.type == "date" and isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return value
        return value

    @staticmethod
    def write_value(prop, value):
        """A property's value as a request carries it.

        Everything crosses as a string, and "" is what clears one. A select is
        written as the bare value its option is keyed on: a list is stored as
        the string `["Active"]` and charted as an option of its own.
        """
        if value is None:
            return ""
        if isinstance(value, Option):
            return value.value if value.value is not None else value.label
        if isinstance(value, list):
            return [UserMapper.write_value(prop, v) for v in value]
        if prop.type == "select" and isinstance(value, str):
            option = prop.option(value)
            return (
                option.value
                if option is not None and option.value is not None
                else value
            )
        if prop.type == "date" and hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        return str(value)

    @staticmethod
    def check(prop, value):
        """Refuse a value the CRM would take with a 200 and not keep.

        Tested 2026-10-14. A select string that is not exactly an option's
        value -- another spelling, another case, a longer string containing
        one -- is ignored, so the property keeps what it held and the next run
        sends it again. A number property given something that is not a number
        stores it as empty, wiping what it held.
        """
        if value is None:
            return
        if prop.type == "select":
            written = UserMapper.write_value(prop, value)
            if not any(
                UserMapper.write_value(prop, o) == written for o in prop.options
            ):
                raise ValueError(
                    f"{prop.key}: {written!r} is not one of its options, and the"
                    " CRM would ignore it"
                )
        if prop.type == "number" and not isinstance(value, (int, float)):
            try:
                float(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"{prop.key}: {value!r} is not a number, and the CRM would"
                    " store it as empty, wiping what it held"
                ) from None

    def external_id(self, user):
        if self.external_id_key is None:
            return None
        value = user.properties.get(self.external_id_key)
        return None if value in (None, "") else str(value)

    def state(self, user):
        """What a request would say about this user, field by field."""
        return {
            **{name: getattr(user, name) for name in SCALARS},
            "email": user.emails.preferred,
            "phone_number": user.phone_numbers.preferred,
            "emails": frozenset(user.emails),
            "phone_numbers": frozenset(user.phone_numbers),
            "address": {part: user.address.get(part) for part in ADDRESS_PARTS},
            "properties": {
                key: self.write_value(self.property(key), value)
                for key, value in user.properties.items()
            },
            "tags": frozenset(user.tags),
        }

    @staticmethod
    def blank():
        """The state of a user that does not exist yet."""
        return {
            **{name: None for name in SCALARS},
            "email": None,
            "phone_number": None,
            "emails": frozenset(),
            "phone_numbers": frozenset(),
            "address": {part: None for part in ADDRESS_PARTS},
            "properties": {},
            "tags": frozenset(),
        }

    def body(self, before, after):
        """Everything but the contacts that turns `before` into `after`.

        The address is sent whole when any part of it changed: it is one
        object to the CRM, and what a write of only some parts does to the rest
        has not been tested.
        """
        body = {}
        for name in SCALARS:
            if after[name] == before[name]:
                continue
            if after[name] is None:
                raise ValueError(f"{name}: clearing it over the API is untested")
            body[name] = after[name]
        if after["address"] != before["address"]:
            body["address"] = {
                k: v for k, v in after["address"].items() if v not in (None, "")
            }
        properties = {
            k: v
            for k, v in after["properties"].items()
            if before["properties"].get(k, "") != v
        }
        if properties:
            body["custom_user_properties"] = properties
        if after["tags"] - before["tags"]:
            body["add_tags"] = sorted(after["tags"] - before["tags"])
        if before["tags"] - after["tags"]:
            body["remove_tags"] = sorted(before["tags"] - after["tags"])
        return body

    def writes(self, before, after, new=False):
        """The requests, in order, that turn `before` into `after`.

        The API takes one email and one phone per write and rejects
        `other_emails` and `other_phone_numbers`, but every new primary
        displaces the old one into them. So an address is added by writing it
        as the primary, and the sequence ends on the preferred one: max(new
        emails, new numbers) writes, then one more. A user being created is
        created first, with their preferred contacts, and then the same.

        Taking an address off needs promoting it and then `clear_email`, which
        has not been tested; neither has clearing a primary.
        """
        main = self.body(before, after)
        added, preferred = {}, {}
        for one, many in CONTACTS:
            removed = before[many] - after[many]
            if removed:
                raise ValueError(
                    f"{many}: taking {sorted(map(str, removed))} off over the API"
                    " is untested"
                )
            if after[one] is None and before[one] is not None:
                raise ValueError(f"{one}: clearing it over the API is untested")
            added[one] = sorted(
                after[many] - before[many] - {after[one]}, key=lambda c: c.key
            )
            if after[one] is not None and (after[one] != before[one] or added[one]):
                preferred[one] = str(after[one])
        steps = [
            {one: str(added[one][i]) for one, _ in CONTACTS if i < len(added[one])}
            for i in range(max(len(added[one]) for one, _ in CONTACTS))
        ]
        if new:
            return [{**main, **preferred}, *steps, *([preferred] if steps else [])]
        last = {**main, **preferred}
        return [*steps, *([last] if last else [])]

    def upsert(self, external_id, body):
        """`POST /users`, on an external ID if there is one. Returns the id
        written to."""
        # Or a landline rejects the whole user with a 422.
        body = {**body, "phone_number_textable_validation": False}
        if external_id is not None:
            key = self.external_id_key
            body = {
                "lookup_key": key,
                **body,
                "custom_user_properties": {
                    **body.get("custom_user_properties", {}),
                    key: external_id,
                },
            }
        if not (body.get("email") or body.get("phone_number")):
            body["require_contact_info"] = False
        return written_id(self.client.post("/users", json=body).json())

    def update(self, user, body):
        self.client.put(f"/users/{user.id}", json=body)

    def delete(self, user):
        """Destroys the user's activities with them -- notes, calls,
        assessments -- and the CRM keeps no tombstone (tested: a user with a
        note and a create_account went from two activities to none)."""
        self.client.delete(f"/users/{user.id}")

    def merge(self, survivor, duplicates):
        """Fold the duplicates into the survivor; the CRM deletes them.

        Tested 2026-10-02 and 2026-10-13: the survivor's values stay, its empty
        fields fill from the duplicates, their emails and numbers join its
        other_emails and other_phone_numbers, and calls, work-list attempts,
        notes, RSVPs, property edits and tags move. Permissions are the
        survivor's, whatever the duplicates held.
        """
        self.client.post(
            "/users/merge",
            json={
                "primary_user_id": survivor.id,
                "user_ids": [d.id for d in duplicates],
            },
        )


def written_id(response):
    if isinstance(response, dict):
        response = response.get("data", response)
    return response.get("id") if isinstance(response, dict) else None


def merged(survivor, duplicates):
    """The contacts a merge leaves on the survivor: its own, and all of theirs."""
    for duplicate in duplicates:
        survivor.emails |= duplicate.emails
        survivor.phone_numbers |= duplicate.phone_numbers


@dataclass
class Commit:
    """What a commit sent, or in a dry run would have."""

    planned: list[tuple[User, str, dict]] = field(default_factory=list)
    rejected: list[tuple[User, str]] = field(default_factory=list)
    undeliverable: list[tuple[User, str]] = field(default_factory=list)
    misdirected: list[tuple[User, int]] = field(default_factory=list)
    withheld: list[tuple[User, Email | PhoneNumber]] = field(default_factory=list)

    def users_written(self):
        return len({id(user) for user, _, _ in self.planned})


class Session:
    """The users of one run: one object per CRM user, and what changed.

    Nothing is sent unless `apply`. A dry run goes through the same steps and
    leaves the objects as the account would be, so what follows reads the
    account as it would be too.

    An email or phone is on one user per organisation. Written to a second, it
    is a 422 at best; posted as the primary of a user whose lookup misses, it
    finds the user who holds it and takes them over. So an address or number
    another user holds, or that two users in one commit want, is withheld --
    left off the user it was added to -- rather than written.
    """

    def __init__(self, mapper, apply):
        self.mapper = mapper
        self.apply = apply
        self._users = {}
        self._before = {}
        self._keyed = {}
        self._new = []
        self._removed = []

    def load(self, users=None):
        """Read every user, or take these. Raises on a shared external ID."""
        found = self.mapper.find_all() if users is None else users
        self._users = {u.id: u for u in found}
        self._before = {id_: self.mapper.state(u) for id_, u in self._users.items()}
        self._index()
        holders = {}
        for user in self._users.values():
            external = self.mapper.external_id(user)
            if external is not None:
                holders.setdefault(external, []).append(user.id)
        shared = {k: ids for k, ids in holders.items() if len(ids) > 1}
        if shared:
            raise DuplicateExternalId(
                f"{len(shared)} external IDs are held by more than one user, e.g. "
                + ", ".join(f"{k}: {ids}" for k, ids in list(shared.items())[:5])
            )

    def __iter__(self):
        return iter(list(self._users.values()))

    def __len__(self):
        return len(self._users)

    def get(self, id=None, *, external_id=None):
        """The user with this CRM id, or this external ID, or None.

        By external ID as of the last load or commit: a user given one since
        is found by it once it is written.
        """
        if external_id is not None:
            return self._keyed.get(str(external_id))
        return self._users.get(int(id))

    def keyed(self):
        """Every user with an external ID, by it, as of the last load or
        commit."""
        return dict(self._keyed)

    def external_id(self, user):
        return self.mapper.external_id(user)

    def _index(self):
        self._keyed = {
            self.mapper.external_id(u): u
            for u in self._users.values()
            if self.mapper.external_id(u) is not None
        }

    def holders(self):
        """Which user holds each address and number, as last read or written."""
        return {
            c: self._users[id_]
            for id_, before in self._before.items()
            for c in before["emails"] | before["phone_numbers"]
        }

    def add(self, user):
        self._new.append(user)
        return user

    def remove(self, user):
        self._removed.append(user)

    def merge(self, survivor, duplicates):
        """Merged now rather than at commit: a write to the survivor of what
        the duplicates held -- a number they consented on -- would collide with
        the duplicates if it went first.

        Raises rather than merge away a user somebody signs in as: the merge
        deletes the user, and their dashboard access with it."""
        logins = [d.id for d in duplicates if d.login]
        if logins:
            raise LoginMergedAway(f"users {logins} are team members' logins")
        if self.apply:
            self.mapper.merge(survivor, duplicates)
        merged(survivor, duplicates)
        for duplicate in duplicates:
            del self._users[duplicate.id]
            del self._before[duplicate.id]
            self._keyed.pop(self.mapper.external_id(duplicate), None)
        state = self.mapper.state(survivor)
        self._before[survivor.id] = {
            **self._before[survivor.id],
            "emails": state["emails"],
            "phone_numbers": state["phone_numbers"],
        }

    def commit(self, progress_every=100):
        """Send what changed: writes to users read, creates, then deletes."""
        result = Commit()
        for user in self._users.values():
            was = self._external_before(self._before[user.id])
            if was is not None and self.mapper.external_id(user) != was:
                raise ExternalIdChanged(
                    f"user {user.id}: {was} -> {self.mapper.external_id(user)}"
                )
        changed = [(u, self._before[u.id], False) for u in self._users.values()]
        changed += [(u, self.mapper.blank(), True) for u in self._new]
        self._withhold_contested(changed, result)

        writes = []
        for user, before, new in changed:
            try:
                after = self.mapper.state(user)
                for key, value in user.properties.items():
                    if after["properties"][key] != before["properties"].get(key, ""):
                        self.mapper.check(self.mapper.property(key), value)
            except (KeyError, ValueError) as error:
                result.rejected.append((user, str(error)))
                continue
            keyed = (new and self.mapper.external_id(user) is not None) or (
                self._external_before(before) is not None
            )
            for n, body in enumerate(self.mapper.writes(before, after, new=new)):
                # Without an external ID, a new user is posted once and is
                # then written by the id the create returned.
                method = "POST" if keyed or (new and n == 0) else "PUT"
                writes.append((user, body, method))

        # A user whose write failed gets none of the rest of their sequence: a
        # lookup on a user who was never created would create somebody else.
        stopped = set()
        for n, (user, body, method) in enumerate(writes, 1):
            result.planned.append((user, method, body))
            if self.apply and id(user) not in stopped:
                if not self._send(user, body, method, result):
                    stopped.add(id(user))
            if progress_every and n % progress_every == 0:
                log.info("  %d/%d", n, len(writes))

        for user in self._removed:
            result.planned.append((user, "DELETE", {}))
            if self.apply:
                try:
                    self.mapper.delete(user)
                except requests.RequestException as error:
                    result.rejected.append((user, str(error)[-200:]))
            self._users.pop(user.id, None)
            self._before.pop(user.id, None)

        for user in self._new:
            if user.id is not None:
                self._users[user.id] = user
        refused = {u.id for u, _ in result.rejected}
        self._before = {
            id_: self._before[id_] if id_ in refused else self.mapper.state(u)
            for id_, u in self._users.items()
        }
        self._index()
        self._new, self._removed = [], []
        return result

    def _withhold_contested(self, changed, result):
        holders = self.holders()
        wanted = {}
        for user, before, _ in changed:
            for contact in user.holds() - (before["emails"] | before["phone_numbers"]):
                wanted.setdefault(contact, []).append((user, before))
        for contact, users in wanted.items():
            holder = holders.get(contact)
            if len(users) == 1 and (holder is None or holder is users[0][0]):
                continue
            for user, before in users:
                for one, many in CONTACTS:
                    contacts = getattr(user, many)
                    if contact in contacts:
                        contacts.discard(contact)
                        if contacts.preferred is None:
                            contacts.preferred = before[one]
                result.withheld.append((user, contact))

    def _external_before(self, before):
        if self.mapper.external_id_key is None:
            return None
        value = before["properties"].get(self.mapper.external_id_key)
        return None if value in (None, "") else str(value)

    def _send(self, user, body, method, result):
        """Whether the write landed on the user it was for."""
        try:
            if method == "PUT":
                self.mapper.update(user, body)
                return True
            try:
                written = self.mapper.upsert(self.mapper.external_id(user), body)
            except requests.HTTPError as error:
                # An address the CRM will not deliver to rejects the whole
                # user. Better the user without it than no user.
                if "email" not in str(error) or "email" not in body:
                    raise
                result.undeliverable.append((user, body["email"]))
                body = {k: v for k, v in body.items() if k != "email"}
                if not body:
                    return True
                written = self.mapper.upsert(self.mapper.external_id(user), body)
        except requests.RequestException as error:
            result.rejected.append((user, str(error)[-200:]))
            return False
        return self._check_landed(user, written, result)

    def _check_landed(self, user, written, result):
        if written is None:
            return True
        if user.id is None:
            if written in self._users:
                result.misdirected.append((user, written))
                log.error(
                    "  a create for %s landed on existing user %s",
                    self.mapper.external_id(user),
                    written,
                )
                return False
            user.id = written
        elif written != user.id:
            result.misdirected.append((user, written))
            log.error("  a write for user %s landed on user %s", user.id, written)
            return False
        return True
