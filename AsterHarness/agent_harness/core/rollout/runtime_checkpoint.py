"""Validation of the event-system checkpoint, independent of a live Session."""

from copy import deepcopy

from ..event_generators.restoration import decode_registration


def validate_checkpoint(payload: dict) -> tuple[list[dict], list[dict]]:
    if payload.get("version") != 1:
        raise ValueError("Unsupported runtime checkpoint version")
    events, generators = payload.get("pending_events"), payload.get("generators")
    if not isinstance(events, list) or not isinstance(generators, list):
        raise ValueError("Invalid runtime checkpoint collections")
    event_ids = set()
    for event in events:
        if (not isinstance(event, dict) or set(event) != {"event_id", "kind", "item"}
                or not isinstance(event["event_id"], str) or not event["event_id"]
                or event["event_id"] in event_ids
                or not isinstance(event["kind"], str) or not event["kind"]
                or not isinstance(event["item"], dict)
                or not isinstance(event["item"].get("type"), str)):
            raise ValueError("Invalid pending event or duplicate event ID")
        event_ids.add(event["event_id"])
    decoded = [decode_registration(snapshot) for snapshot in generators]
    identifiers = [record["generator_id"] for record, _ in decoded]
    if (len(identifiers) != len(set(identifiers))
            or sum(record["status"] == "active" for record, _ in decoded) > 64
            or sum(record["status"] != "active" for record, _ in decoded) > 256):
        raise ValueError("Invalid generator checkpoint size or duplicate ID")
    return deepcopy(events), deepcopy(generators)
