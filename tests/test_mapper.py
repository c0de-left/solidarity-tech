"""Solidarity Tech's users and properties as objects, and the requests a Session
sends for them.

The external ID rules are the ones that cost people when they go wrong: a
POST whose lookup misses lands on whoever holds the phone and takes their ID
(tested 2026-10-14), so which verb goes out, and whether the write landed where
it was aimed, are pinned here.
"""

import datetime

import pytest
import requests

from solidarity_tech.mapper import (
    Contacts,
    DuplicateExternalId,
    Email,
    ExternalIdChanged,
    LoginMergedAway,
    Note,
    NoteMapper,
    Option,
    PhoneNumber,
    Property,
    PropertyMapper,
    Session,
    User,
    UserMapper,
)

PERSON_ID = Property("member-id", "Member ID", "input")
STATUS = Property(
    "membership-status",
    "Membership status",
    "select",
    [Option("Active", "Active"), Option("Retired", "9MApPDC0")],
)
SERIES = Property(
    "title-series",
    "Title series",
    "select",
    [
        Option("Teaching Adjuncts", "Teaching Adjuncts"),
        Option("Non-Teaching Adjuncts", "Non-Teaching Adjuncts"),
    ],
)
HIRED = Property("hire-date", "Hire date", "date")
HOURS = Property("teaching-hours-per-term", "Teaching hours per term", "number")
PROPERTIES = [PERSON_ID, STATUS, SERIES, HIRED, HOURS]


class Reply:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Recorder:
    """The client. A POST /users lands on whoever holds the external ID, a
    created user gets 99, and `lands_on` overrides either."""

    def __init__(self, users=(), lands_on=None):
        self.users_ = list(users)
        self.calls = []
        self.lands_on = {
            u["custom_user_properties"]["member-id"]: u["id"]
            for u in self.users_
            if u["custom_user_properties"].get("member-id")
        }
        self.lands_on.update(lands_on or {})
        self.reject = None

    def paginate_cursor(self, path, params=None):
        assert (path, params) == ("/users", {"_cursor": "start"})
        return iter(self.users_)

    def paginate(self, path):
        if path == "/custom_user_properties":
            self.calls.append(("GET", path, None))
            return [
                {
                    "key": p.key,
                    "label": {"en": p.label},
                    "field_type": p.type,
                    "options": [
                        {"label": {"en": o.label}, "value": o.value} for o in p.options
                    ],
                }
                for p in PROPERTIES
            ]
        assert path == "/team_members"
        return []

    def post(self, path, json):
        self.calls.append(("POST", path, json))
        if self.reject and self.reject in json:
            raise requests.HTTPError(
                f"422 from POST {path}: {self.reject} is undeliverable"
            )
        external = json.get("custom_user_properties", {}).get("member-id")
        return Reply(
            {
                "id": self.lands_on.get(external, 99),
                "message": "User saved successfully",
            }
        )

    def put(self, path, json):
        self.calls.append(("PUT", path, json))
        return Reply({})

    def delete(self, path):
        self.calls.append(("DELETE", path, None))


def raw(user_id, person_id=None, **fields):
    """A user as GET /users returns it: every key, always."""
    properties = {p.key: None for p in PROPERTIES}
    properties["member-id"] = person_id
    properties.update(fields.pop("properties", {}))
    return {
        "id": user_id,
        "chapter_id": 2913,
        "email_permission": True,
        "sms_permission": True,
        "first_name": "Wanda",
        "last_name": "Okonkwo",
        "alternate_name": None,
        "email": None,
        "phone_number": None,
        "other_emails": [],
        "other_phone_numbers": [],
        "address": {"address1": None, "city": None, "country": "US", "latitude": None},
        "custom_user_properties": properties,
        "tags": [],
        "assessment": None,
        **fields,
    }


def session(*users, apply=True, **client):
    recorder = Recorder(users, **client)
    s = Session(UserMapper(recorder, "member-id", PROPERTIES), apply)
    s.load()
    return s, recorder


# --- Values are compared as a request would carry them -----------------------


def test_a_select_read_by_containment_is_the_option_written():
    """A stored "Non-Teaching Adjuncts" reads back as every option it contains."""
    s, _ = session(
        raw(
            1,
            "101",
            properties={
                "title-series": [
                    {"label": "Teaching Adjuncts", "value": "Teaching Adjuncts"},
                    {
                        "label": "Non-Teaching Adjuncts",
                        "value": "Non-Teaching Adjuncts",
                    },
                ]
            },
        )
    )
    user = s.users.get(id=1)
    assert user.properties["title-series"] == "Non-Teaching Adjuncts"
    user.properties["title-series"] = "Non-Teaching Adjuncts"
    assert s.commit().planned == []


def test_a_select_is_written_as_the_bare_value_its_option_is_keyed_on():
    """`Retired` was added in the dashboard and is stored as `9MApPDC0`; a list
    would be stored as the string `["Retired"]` (#176)."""
    assert UserMapper.write_value(STATUS, "Retired") == "9MApPDC0"
    assert UserMapper.write_value(STATUS, STATUS.option("Retired")) == "9MApPDC0"
    assert UserMapper.write_value(STATUS, "Nonesuch") == "Nonesuch"


def test_a_select_keyed_on_a_code_is_compared_on_the_code():
    """Comparing labels would call a correct pick list wrong (#114)."""
    s, recorder = session(
        raw(
            1,
            "101",
            properties={
                "membership-status": [{"label": "Retired", "value": "9MApPDC0"}]
            },
        )
    )
    s.users.get(id=1).properties["membership-status"] = "Retired"
    assert s.commit().planned == []
    s.users.get(id=1).properties["membership-status"] = "Active"
    s.commit()
    assert recorder.calls[-1][2]["custom_user_properties"] == {
        "membership-status": "Active",
        "member-id": "101",
    }


def test_a_date_reaches_the_crm_as_a_day():
    """A date property stores what it is sent, so a datetime would read as one."""
    assert UserMapper.write_value(HIRED, datetime.datetime(2026, 7, 13)) == "2026-07-13"
    s, _ = session(raw(1, "101", properties={"hire-date": "2026-07-13"}))
    assert s.users.get(id=1).properties["hire-date"] == datetime.date(2026, 7, 13)


def test_an_option_label_is_read_in_either_shape():
    prop = PropertyMapper.hydrate(
        {
            "key": "membership-status",
            "label": "Membership status",
            "field_type": "select",
            "options": [
                {"label": {"en": "Retired"}, "value": "9MApPDC0"},
                {"label": "Active", "value": "Active"},
                "a bare string, which offers nothing",
                {"value": "x"},
            ],
        }
    )
    assert [(o.label, o.value) for o in prop.options] == [
        ("Retired", "9MApPDC0"),
        ("Active", "Active"),
    ]


def test_only_what_changed_is_sent():
    s, recorder = session(raw(1, "101", tags=["Chapter Chair", "Dept Rep"]))
    user = s.users.get(id=1)
    user.tags = {"Chapter Chair", "Delegate"}
    user.first_name = "Wanda"
    s.commit()
    assert recorder.calls == [
        (
            "POST",
            "/users",
            {
                "lookup_key": "member-id",
                "add_tags": ["Delegate"],
                "remove_tags": ["Dept Rep"],
                "custom_user_properties": {"member-id": "101"},
                "phone_number_textable_validation": False,
                "require_contact_info": False,
            },
        )
    ]


def test_an_address_is_sent_whole_when_any_part_changed():
    s, recorder = session(raw(1, "101"))
    user = s.users.get(id=1)
    user.address = {**user.address, "address1": "175 Willoughby St", "city": "Brooklyn"}
    s.commit()
    assert recorder.calls[0][2]["address"] == {
        "address1": "175 Willoughby St",
        "city": "Brooklyn",
        "country": "US",
    }


def test_clearing_a_field_the_api_has_not_been_tested_on_refuses():
    s, _ = session(raw(1, "101", email="wanda@example.org"))
    s.users.get(id=1).emails.preferred = None
    with pytest.raises(ValueError, match="untested"):
        s.commit()


# --- External IDs ------------------------------------------------------------


def test_a_user_read_with_an_external_id_is_written_on_it():
    s, recorder = session(raw(1, "101"))
    s.users.get(id=1).first_name = "Wendy"
    s.commit()
    assert recorder.calls[0][:2] == ("POST", "/users")
    assert recorder.calls[0][2]["lookup_key"] == "member-id"


def test_giving_a_user_its_external_id_is_a_put():
    """A POST on an ID nobody holds yet would create somebody, or take over
    whoever holds the phone."""
    s, recorder = session(raw(7))
    s.users.get(id=7).external_id = "102"
    s.commit()
    assert recorder.calls == [
        ("PUT", "/users/7", {"custom_user_properties": {"member-id": "102"}})
    ]
    assert s.users.get(external_id="102").id == 7


def test_an_external_id_is_never_changed():
    s, recorder = session(raw(1, "101"))
    s.users.get(id=1).external_id = "102"
    with pytest.raises(ExternalIdChanged):
        s.commit()
    assert recorder.calls == []


def test_two_users_holding_one_external_id_stop_the_load():
    with pytest.raises(DuplicateExternalId):
        session(raw(1, "101"), raw(2, "101"))


def test_a_create_that_lands_on_somebody_already_held_is_reported():
    """The lookup missed, and the CRM matched an existing user by phone."""
    s, _ = session(raw(1, "101"), lands_on={"102": 1})
    new = s.users.add(
        User(
            chapter_id=2913,
            email_permission=True,
            phone_numbers=Contacts(preferred=PhoneNumber("2125550101")),
        )
    )
    new.external_id = "102"
    result = s.commit()
    assert result.misdirected == [(new, 1)]


def test_a_write_that_lands_on_another_user_is_reported():
    s, _ = session(raw(1, "101"), raw(2, "102"), lands_on={"101": 2})
    s.users.get(id=1).first_name = "Wendy"
    assert [(u.id, landed) for u, landed in s.commit().misdirected] == [(1, 2)]


def test_a_create_gets_the_id_it_landed_on():
    s, _ = session(lands_on={"102": 55})
    new = s.users.add(User(chapter_id=2913, email_permission=True))
    new.external_id = "102"
    s.commit()
    assert new.id == 55
    assert s.users.get(external_id="102") is new


def test_an_undeliverable_email_is_dropped_rather_than_the_person():
    s, recorder = session(raw(1, "101"))
    recorder.reject = "email"
    s.users.get(id=1).emails.preferred = Email("wanda@bounces.example")
    s.users.get(id=1).first_name = "Wendy"
    result = s.commit()
    assert [u.id for u, _ in result.undeliverable] == [1]
    assert "email" not in recorder.calls[-1][2]
    assert recorder.calls[-1][2]["first_name"] == "Wendy"


# --- Dry runs and what the CRM does with a write ------------------------------


def test_a_dry_run_sends_nothing_and_plans_the_same():
    s, recorder = session(raw(1, "101"), apply=False)
    s.users.get(id=1).first_name = "Wendy"
    result = s.commit()
    assert recorder.calls == []
    assert [(u.id, method) for u, method, _ in result.planned] == [(1, "POST")]


def test_a_merge_moves_the_duplicates_contacts_onto_the_survivor():
    s, recorder = session(
        raw(50, "101", email="wokonkwo@work.example"),
        raw(9, email="wanda@home.example", phone_number="13475550102"),
    )
    s.users.merge(s.users.get(id=50), [s.users.get(id=9)])
    assert recorder.calls == [
        ("POST", "/users/merge", {"primary_user_id": 50, "user_ids": [9]})
    ]
    survivor = s.users.get(id=50)
    assert s.users.get(id=9) is None
    assert Email("wanda@home.example") in survivor.emails
    assert survivor.emails.preferred == Email("wokonkwo@work.example")
    assert PhoneNumber("13475550102") in survivor.phone_numbers
    s.commit()
    assert len(recorder.calls) == 1, "what the merge moved is already held"


# --- Contacts -----------------------------------------------------------------


def test_a_contact_is_the_same_however_it_is_written():
    assert PhoneNumber("12125550101") == PhoneNumber("212-555-0101")
    assert Email("WANDA@Example.org") == Email("wanda@example.org")
    assert PhoneNumber("2125550101") != Email("2125550101")
    with pytest.raises(ValueError):
        PhoneNumber(" ")


def test_the_preferred_contact_is_always_held():
    phones = Contacts()
    phones.preferred = PhoneNumber("2125550101")
    assert PhoneNumber("12125550101") in phones


def test_a_number_held_in_another_spelling_is_not_written_again():
    s, recorder = session(raw(1, "101", phone_number="12125550101"))
    s.users.get(id=1).phone_numbers.add(PhoneNumber("212-555-0101"))
    s.commit()
    assert recorder.calls == []


def test_adding_numbers_writes_each_then_ends_on_the_preferred():
    """The API rejects other_phone_numbers, but a new primary displaces the old
    one into it: so each is written as the primary, and the preferred last."""
    s, recorder = session(
        raw(1, "101", phone_number="2125550101", email="wanda@example.org")
    )
    user = s.users.get(id=1)
    user.phone_numbers |= [PhoneNumber("7185551212"), PhoneNumber("3475550101")]
    user.emails.add(Email("wokonkwo@work.example"))
    s.commit()
    assert [
        {k: v for k, v in body.items() if k in ("email", "phone_number")}
        for _, _, body in recorder.calls
    ] == [
        {"email": "wokonkwo@work.example", "phone_number": "3475550101"},
        {"phone_number": "7185551212"},
        {"email": "wanda@example.org", "phone_number": "2125550101"},
    ]


def test_a_new_preferred_number_is_one_write():
    """The old primary is displaced into the others by the CRM itself."""
    s, recorder = session(raw(1, "101", phone_number="17185550103"))
    s.users.get(id=1).phone_numbers.preferred = PhoneNumber("13475550102")
    s.commit()
    assert len(recorder.calls) == 1
    assert recorder.calls[0][2]["phone_number"] == "13475550102"
    assert PhoneNumber("17185550103") in s.users.get(id=1).phone_numbers


def test_a_new_user_is_created_then_given_the_rest():
    s, recorder = session(lands_on={"102": 55})
    new = s.users.add(User(chapter_id=2913, email_permission=True))
    new.external_id = "102"
    new.phone_numbers |= [PhoneNumber("7185551212")]
    new.phone_numbers.preferred = PhoneNumber("2125550101")
    s.commit()
    assert [body.get("phone_number") for _, _, body in recorder.calls] == [
        "2125550101",
        "7185551212",
        "2125550101",
    ]
    assert recorder.calls[0][2]["chapter_id"] == 2913


def test_taking_a_number_off_refuses():
    s, _ = session(
        raw(1, "101", phone_number="2125550101", other_phone_numbers=["7185551212"])
    )
    s.users.get(id=1).phone_numbers.discard(PhoneNumber("7185551212"))
    with pytest.raises(ValueError, match="untested"):
        s.commit()


def test_a_number_another_user_holds_is_withheld():
    """Written to a second user it is a 422 at best, and a takeover at worst."""
    s, recorder = session(
        raw(1, "101", phone_number="2125550101"),
        raw(2, "102", other_phone_numbers=["7185551212"]),
    )
    s.users.get(id=1).phone_numbers.add(PhoneNumber("718-555-1212"))
    result = s.commit()
    assert recorder.calls == []
    assert [(u.id, str(c)) for u, c in result.withheld] == [(1, "718-555-1212")]
    assert PhoneNumber("7185551212") not in s.users.get(id=1).phone_numbers


def test_a_number_two_users_want_goes_to_neither():
    s, recorder = session(raw(1, "101"), raw(2, "102"))
    for user_id in (1, 2):
        s.users.get(id=user_id).phone_numbers.preferred = PhoneNumber("7185551212")
    result = s.commit()
    assert recorder.calls == []
    assert sorted(u.id for u, _ in result.withheld) == [1, 2]
    assert s.users.get(id=1).phone_numbers.preferred is None


def test_a_failed_create_sends_none_of_the_rest():
    """A lookup on a user who was never created would create somebody else."""
    s, recorder = session()
    recorder.reject = "chapter_id"
    new = s.users.add(User(chapter_id=2913, email_permission=True))
    new.external_id = "102"
    new.phone_numbers |= [PhoneNumber("7185551212")]
    new.phone_numbers.preferred = PhoneNumber("2125550101")
    result = s.commit()
    assert len(recorder.calls) == 1
    assert len(result.rejected) == 1


def test_a_refused_write_is_still_owed():
    """The CRM did not take it, so the next commit sends it again."""
    s, recorder = session(raw(1, "101"))
    recorder.reject = "first_name"
    s.users.get(id=1).first_name = "Wendy"
    assert [u.id for u, _ in s.commit().rejected] == [1]
    recorder.reject = None
    s.commit()
    assert recorder.calls[-1][2]["first_name"] == "Wendy"


def test_a_misdirected_create_does_not_replace_the_user_it_landed_on():
    s, _ = session(raw(1, "101"), lands_on={"102": 1})
    held = s.users.get(id=1)
    new = s.users.add(User(chapter_id=2913, email_permission=True))
    new.external_id = "102"
    s.commit()
    assert s.users.get(id=1) is held
    assert new.id is None


# --- Writes the CRM takes with a 200 and does not keep (tested 2026-10-14) ----


def test_a_select_value_outside_its_options_is_refused_not_sent():
    """The CRM ignores it, so it would be sent again every run, unnoticed."""
    s, recorder = session(raw(1, "101"), raw(2, "102"))
    s.users.get(id=1).properties["membership-status"] = "retired"
    s.users.get(id=2).properties["membership-status"] = "Retired"
    result = s.commit()
    assert [u.id for u, _ in result.rejected] == [1]
    assert "not one of its options" in result.rejected[0][1]
    assert [body["custom_user_properties"] for _, _, body in recorder.calls] == [
        {"membership-status": "9MApPDC0", "member-id": "102"}
    ]


def test_text_in_a_number_is_refused_because_it_would_wipe_the_value():
    s, recorder = session(raw(1, "101"))
    s.users.get(id=1).properties["teaching-hours-per-term"] = "three courses"
    assert "not a number" in s.commit().rejected[0][1]
    assert recorder.calls == []
    s.users.get(id=1).properties["teaching-hours-per-term"] = 45.0
    s.commit()
    assert recorder.calls[-1][2]["custom_user_properties"][HOURS.key] == "45.0"


def test_a_value_the_crm_already_holds_is_not_second_guessed():
    """Only what is about to be sent is checked; the account holds what it holds."""
    s, recorder = session(
        raw(
            1,
            "101",
            properties={"membership-status": [{"label": "Gone", "value": "x"}]},
        )
    )
    s.users.get(id=1).first_name = "Wendy"
    assert s.commit().rejected == []
    assert len(recorder.calls) == 1


def test_a_login_is_never_merged_away():
    """The merge deletes the user, and the team member's dashboard access."""
    client = Recorder([raw(1, "101"), raw(2)])
    client.paginate = lambda path: [{"user_id": 2}]
    s = Session(UserMapper(client, "member-id", PROPERTIES), apply=True)
    s.load()
    assert s.users.get(id=2).login and not s.users.get(id=1).login
    with pytest.raises(LoginMergedAway):
        s.users.merge(s.users.get(id=1), [s.users.get(id=2)])
    assert client.calls == []


def test_a_note_keeps_its_date_in_seconds():
    client = Recorder()
    when = datetime.datetime(2026, 3, 2, 15, 30, tzinfo=datetime.timezone.utc)
    NoteMapper(client).insert(
        User(chapter_id=1, email_permission=True, id=7), Note("Met at the rally", when)
    )
    assert client.calls == [
        (
            "POST",
            "/user_notes",
            {"user_id": 7, "content": "Met at the rally", "created_at": 1772465400},
        )
    ]


# --- Lookups, properties, and users with no external ID ----------------------


def test_users_are_found_by_id_or_external_id_and_by_nothing_else():
    s, _ = session(raw(1, "101"), raw(2))
    assert s.users.get(id=1).id == 1
    assert s.users.get(external_id="101") is s.users.get(id=1)
    assert s.users.get(external_id="102") is None
    assert len(s.users) == 2 and {u.id for u in s.users} == {1, 2}
    with pytest.raises(TypeError):
        s.users.get(1)
    with pytest.raises(TypeError):
        s.users.get(id=1, external_id="101")


def test_the_accounts_properties_are_read_when_first_needed():
    client = Recorder(
        [
            raw(
                1,
                "101",
                properties={
                    "membership-status": [{"label": "Retired", "value": "9MApPDC0"}]
                },
            )
        ]
    )
    s = Session(UserMapper(client, "member-id"), apply=True)
    s.load()
    assert client.calls == [("GET", "/custom_user_properties", None)]
    assert s.users.get(id=1).properties["membership-status"] == "Retired"
    s.users.get(id=1).properties["membership-status"] = "Active"
    s.commit(progress_every=0)
    assert client.calls[-1][2]["custom_user_properties"] == {
        "membership-status": "Active",
        "member-id": "101",
    }


def test_a_property_the_account_lacks_rejects_the_user():
    s, recorder = session(raw(1, "101"), raw(2, "102"))
    s.users.get(id=1).properties["no-such-thing"] = "x"
    s.users.get(id=2).first_name = "Wendy"
    result = s.commit(progress_every=0)
    assert [(u.id, "no-such-thing" in e) for u, e in result.rejected] == [(1, True)]
    assert [c[2]["first_name"] for c in recorder.calls] == ["Wendy"]


def test_without_an_external_id_users_are_written_by_id():
    recorder = Recorder([raw(1, phone_number="2125550101")])
    s = Session(UserMapper(recorder, properties=PROPERTIES), apply=True)
    s.load()
    s.users.get(id=1).first_name = "Wendy"
    new = s.users.add(User(chapter_id=2913, email_permission=True, first_name="Ben"))
    new.phone_numbers |= [PhoneNumber("7185551212")]
    new.phone_numbers.preferred = PhoneNumber("3475550101")
    s.commit(progress_every=0)
    assert [(m, path) for m, path, _ in recorder.calls] == [
        ("PUT", "/users/1"),
        ("POST", "/users"),
        ("PUT", "/users/99"),
        ("PUT", "/users/99"),
    ]
    assert all("lookup_key" not in body for _, _, body in recorder.calls)
    assert s.users.get(id=99) is new


def test_without_an_external_id_a_create_matched_to_someone_is_reported():
    """A bare POST matches on email and phone, and may update somebody."""
    recorder = Recorder([raw(1, phone_number="2125550101")], lands_on={None: 1})
    s = Session(UserMapper(recorder, properties=PROPERTIES), apply=True)
    s.load()
    new = s.users.add(User(chapter_id=2913, email_permission=True, first_name="Ben"))
    result = s.commit(progress_every=0)
    assert result.misdirected == [(new, 1)]


def test_a_number_reads_as_the_number_written_and_unset_as_none():
    s, recorder = session(
        raw(1, "101", properties={"teaching-hours-per-term": "45.0"}),
        raw(2, "102", properties={"teaching-hours-per-term": "90"}),
        raw(3, "103", properties={"teaching-hours-per-term": None, "hire-date": None}),
    )
    assert s.users.get(id=1).properties["teaching-hours-per-term"] == 45.0
    assert s.users.get(id=2).properties["teaching-hours-per-term"] == 90
    assert s.users.get(id=3).external_id == "103"
    assert s.users.get(id=3).properties == {
        "membership-status": None,
        "title-series": None,
        "hire-date": None,
        "teaching-hours-per-term": None,
    }
    s.commit(progress_every=0)
    assert recorder.calls == []


def test_the_external_id_is_an_attribute_and_not_a_property():
    s, recorder = session(raw(1, "101"), raw(2, "102"))
    assert s.users.get(id=1).external_id == "101"
    assert "member-id" not in s.users.get(id=1).properties
    s.users.get(id=1).properties["member-id"] = "999"
    s.users.get(id=2).first_name = "Wendy"
    result = s.commit(progress_every=0)
    assert [u.id for u, _ in result.rejected] == [1]
    assert [body["first_name"] for _, _, body in recorder.calls] == ["Wendy"]


def test_an_external_id_with_no_property_for_it_rejects_the_user():
    recorder = Recorder([raw(1)])
    s = Session(UserMapper(recorder, properties=PROPERTIES), apply=True)
    s.load()
    s.users.get(id=1).external_id = "101"
    assert [u.id for u, _ in s.commit(progress_every=0).rejected] == [1]
    assert recorder.calls == []
