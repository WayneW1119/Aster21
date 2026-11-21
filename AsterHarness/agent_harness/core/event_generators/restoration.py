"""Decode data, never import code named by a checkpoint.

New source types can add an explicit restoration branch here. Sources with live
external dependencies keep their registration but report unavailability at start.
"""

from copy import deepcopy
import re

from .timer import TimerGenerator
from .program import ProgramGenerator


def decode_registration(snapshot: dict) -> tuple[dict, TimerGenerator | ProgramGenerator | None]:
    if not isinstance(snapshot, dict) or set(snapshot) != {"record", "state"}:
        raise ValueError("Invalid generator checkpoint")
    record = deepcopy(snapshot["record"])
    if (not isinstance(record, dict)
            or not isinstance(record.get("generator_id"), str) or not record["generator_id"]
            or not isinstance(record.get("kind"), str) or not record["kind"]
            or record.get("status") not in {"active", "completed", "cancelled", "failed", "paused"}
            or not isinstance(record.get("configuration"), dict)
            or type(record.get("events_emitted")) is not int or record["events_emitted"] < 0
            or not isinstance(record.get("created_at"), str)):
        raise ValueError("Invalid generator registration")
    generator = None
    if record["kind"] == "timer":
        try:
            generator = TimerGenerator.restore(record["configuration"], snapshot["state"])
        except (TypeError, KeyError):
            raise ValueError("Invalid saved timer configuration") from None
        if record["status"] == "active" and (
                generator.next_fire_at is None or generator.events_emitted != record["events_emitted"]):
            raise ValueError("Inconsistent active timer progress")
    elif record["kind"] == "program":
        if (snapshot["state"] is not None
                or not re.fullmatch(r"gen_[0-9a-f]{32}", record["generator_id"])):
            raise ValueError("Unexpected program checkpoint state")
        try:
            generator = ProgramGenerator.restore(record["configuration"])
        except (TypeError, KeyError):
            raise ValueError("Invalid saved program configuration") from None
    elif record["kind"] == "code_execution":
        if (snapshot["state"] is not None or record["status"] == "paused"
                or set(record["configuration"]) != {"cell_id"}
                or not isinstance(record["configuration"]["cell_id"], str)
                or not re.fullmatch(r"cell_[0-9a-f]{32}", record["configuration"]["cell_id"])):
            raise ValueError("Invalid saved Code Mode execution")
        # A live isolate is never serialized or replayed after restart.
    elif snapshot["state"] is not None or record["status"] == "paused":
        raise ValueError("Unknown generator checkpoint state")
    return record, generator
