"""Codex-inspired sections and comparison baselines for recorded model context.

Sections own their rendering policy. A state change may require no extra message
when ordinary tool history already carries the evidence. Snapshots are internal
rollout metadata, never additional fields on Responses input items.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol

from .lifecycle import (ContextSection, SectionCompaction, SectionPrefix,
                        WorldStateContext, WorldStateCompaction)


@dataclass(frozen=True)
class WorldStateSnapshot:
    sections: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"version": 1, "sections": deepcopy(self.sections)}

    @classmethod
    def from_dict(cls, value: dict) -> "WorldStateSnapshot":
        if (not isinstance(value, dict) or set(value) != {"version", "sections"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["sections"], dict)
                or any(not isinstance(key, str) or not key or not isinstance(snapshot, dict)
                       for key, snapshot in value["sections"].items())):
            raise ValueError("Invalid or unsupported world-state snapshot")
        return cls(deepcopy(value["sections"]))


@dataclass(frozen=True)
class WorldStateUpdate:
    section_id: str
    snapshot: dict
    items: tuple[dict, ...] = ()


class WorldStateSection(Protocol):
    ID: str

    def prefix_items(self) -> list[dict]: ...
    def project_items(self, items: list[dict]) -> list[dict]: ...
    def validate(self, items: list[dict]) -> None: ...
    def observe(self, items: list[dict]) -> list[tuple[str, dict]]: ...
    def snapshot_state(self) -> dict | None: ...
    def plan_compaction(self, items: list[dict], first: int, last: int,
                        whole: bool) -> SectionCompaction | None: ...
    def validate_compaction(self, plan: SectionCompaction, items: list[dict]) -> None: ...
    def commit_compaction(self, plan: SectionCompaction) -> None: ...
    def restore(self, state: dict) -> None: ...

    def render_diff(self, previous: dict | None,
                    items: list[dict]) -> WorldStateUpdate | None: ...


class WorldState:
    def __init__(self) -> None:
        self._sections: dict[str, WorldStateSection] = {}

    def add_section(self, section: WorldStateSection) -> None:
        if not isinstance(section.ID, str) or not section.ID or section.ID in self._sections:
            raise ValueError("World-state section IDs must be nonempty and unique")
        self._sections[section.ID] = section

    def render_context(self, items: list[dict]) -> WorldStateContext:
        """Render pinned section baselines, then the effective history, without I/O."""
        self.validate(items)
        prefixes = [SectionPrefix(key, tuple(deepcopy(section.prefix_items())))
                    for key, section in self._sections.items()]
        history = deepcopy(items)
        for section in self._sections.values():
            history = section.project_items(history)
            prefixes = [SectionPrefix(prefix.section_id,
                tuple(section.project_items(list(prefix.items)))) for prefix in prefixes]
        return WorldStateContext(tuple(prefixes), tuple(history))

    def validate(self, items: list[dict]) -> None:
        for section in self._sections.values():
            section.validate(deepcopy(items))

    def observe(self, items: list[dict]) -> list[tuple[str, dict]]:
        records = []
        for section in self._sections.values():
            records.extend(section.observe(deepcopy(items)))
        return deepcopy(records)

    def plan_compaction(self, items: list[dict], first: int, last: int,
                        whole: bool = False) -> WorldStateCompaction:
        self.validate(items)
        plans = []
        for key, section in self._sections.items():
            plan = section.plan_compaction(deepcopy(items), first, last, whole)
            if plan is not None:
                if plan.section_id != key or not isinstance(plan.state, dict):
                    raise ValueError("Invalid section compaction plan")
                plans.append(plan)
        result = WorldStateCompaction(plans)
        prospective = items[:first] + result.checkpoints + items[last + 1:]
        for plan in result.plans:
            self._sections[plan.section_id].validate_compaction(plan, deepcopy(prospective))
        return result

    def commit_compaction(self, plan: WorldStateCompaction, items: list[dict]) -> None:
        # Call only after the complete operation has been durably committed.
        for update in plan.plans:
            self._sections[update.section_id].commit_compaction(deepcopy(update))
        self.validate(items)

    def render_full(self) -> tuple[WorldStateSnapshot, list[WorldStateUpdate]]:
        """Initial diff snapshot; render_context provides ordered request baselines."""
        return self.render_history_diff(None, [])

    def render_history_diff(self, previous: WorldStateSnapshot | None,
                            items: list[dict]) -> tuple[WorldStateSnapshot, list[WorldStateUpdate]]:
        sections = deepcopy(previous.sections) if previous is not None else {}
        updates = []
        for key, section in self._sections.items():
            update = section.render_diff(deepcopy(sections.get(key)), deepcopy(items))
            if update is None:
                continue
            if update.section_id != key or not isinstance(update.snapshot, dict):
                raise ValueError("World-state update requires its section ID and an object snapshot")
            sections[key] = deepcopy(update.snapshot)
            updates.append(deepcopy(update))
        return WorldStateSnapshot(sections), updates


__all__ = ["WorldState", "WorldStateSection", "WorldStateSnapshot", "WorldStateUpdate",
           "ContextSection", "SectionCompaction", "WorldStateContext", "WorldStateCompaction"]
