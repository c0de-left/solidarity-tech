"""Solidarity Tech's API as objects: a gateway for the transport, mappers for
users, custom properties and notes, and a Session for the users of one run."""

from solidarity_tech.gateway import (
    Client,
    HTTPError,
    RetrySession,
    ThrottledSession,
    Unanswered,
)
from solidarity_tech.mapper import (
    Commit,
    Contacts,
    DuplicateExternalId,
    Email,
    ExternalIdChanged,
    KeyNotDerived,
    LoginMergedAway,
    MatchedSomeoneElse,
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

__all__ = [
    "Client",
    "Commit",
    "Contacts",
    "DuplicateExternalId",
    "Email",
    "ExternalIdChanged",
    "HTTPError",
    "KeyNotDerived",
    "LoginMergedAway",
    "MatchedSomeoneElse",
    "Note",
    "NoteMapper",
    "Option",
    "PhoneNumber",
    "Property",
    "PropertyMapper",
    "RetrySession",
    "Session",
    "ThrottledSession",
    "Unanswered",
    "User",
    "UserMapper",
]
