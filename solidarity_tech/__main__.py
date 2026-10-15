"""Inventory a Solidarity Tech account: what exists to write into.

Usage:
    SOLIDARITY_API_KEY=... python -m solidarity_tech check [--json PATH]
"""

import argparse
import json
import logging

from solidarity_tech.gateway import Client
from solidarity_tech.mapper import PropertyMapper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("solidarity")


def check(client):
    """Inventory what already exists in the account.

    Read-only, and the thing to run before writing to an account. Three of
    these four collections are account state that has to be read, since a
    write targets them by id or by name:

      * chapters, because `chapter_id` is required to create a user and the
        API exposes no way to create a chapter -- `/chapters` is GET only, so
        whatever chapters exist were made in the UI and are the only ones a
        load can write into.
      * custom user properties, because their `internal_name` is the key both
        `custom_user_properties` and `lookup_key` are written against.
      * user lists, for their `parameters`. A list is a saved QueryBuilder
        query, and `user_list_ids` on `GET /users` is the only server-side
        filter beyond email and phone -- there is no `chapter_id` query
        parameter. The spec documents the rule *envelope* but not the field
        identifiers that go in it (its one example is `time.account_created`),
        so an existing list is the only written-down source for how to name a
        chapter in a rule. A list can filter a custom date property by a
        relative range ("in the last six months") and stays current by
        itself (2026-10-02), which is why a hire date is pushed as a date
        rather than as a recomputed new-hire flag.
    """
    organizations = list(client.paginate("/organizations"))
    log.info("organizations: %d", len(organizations))
    for org in organizations:
        log.info(
            "  %-6s %-40s parent=%s",
            org.get("id"),
            org.get("name"),
            org.get("parent_organization_id"),
        )

    chapters = list(client.paginate("/chapters"))
    log.info("chapters: %d", len(chapters))
    for chapter in sorted(chapters, key=lambda c: c.get("name") or ""):
        log.info("  %-6s %s", chapter.get("id"), chapter.get("name"))

    properties = list(client.paginate("/custom_user_properties"))
    log.info("custom user properties: %d", len(properties))
    for prop in map(PropertyMapper.hydrate, properties):
        log.info(
            "  %-24s %-10s %s%s",
            prop.key,
            prop.type,
            prop.label,
            f"  ({len(prop.options)} options)" if prop.options else "",
        )

    lists = list(client.paginate("/user_lists"))
    log.info("user lists: %d", len(lists))
    for user_list in lists:
        rules = (user_list.get("parameters") or {}).get("rules") or []
        log.info(
            "  %-6s %-40s %s/%s  %s",
            user_list.get("id"),
            user_list.get("name"),
            user_list.get("scope_type"),
            user_list.get("scope_id"),
            ", ".join(sorted({str(r.get("id")) for r in rules})) or "(no rules)",
        )

    log.info("%d requests", client.requests_made)
    return {
        "organizations": organizations,
        "chapters": chapters,
        "custom_user_properties": properties,
        "user_lists": lists,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check"])
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="also write the inventory to this file, for the loader to map against",
    )
    args = parser.parse_args()

    client = Client()
    inventory = check(client)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(inventory, f, indent=2)
        log.info("wrote %s", args.json)


if __name__ == "__main__":
    main()
