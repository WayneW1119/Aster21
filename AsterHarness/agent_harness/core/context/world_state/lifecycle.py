"""Common context and compaction lifecycle; renderers retain their own protocols."""

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class SectionCompaction:
    section_id: str
    state: dict
    checkpoints: tuple[dict, ...] = ()
    baseline_advanced: bool = False


@dataclass(frozen=True)
class SectionPrefix:
    section_id: str
    items: tuple[dict, ...]


@dataclass(frozen=True)
class WorldStateContext:
    prefix: tuple[SectionPrefix, ...]
    history: tuple[dict, ...]

    def input_items(self):
        return deepcopy([item for section in self.prefix for item in section.items]
                        + list(self.history))

    def to_prompt(self, instructions, tools):
        """Bridge the old Prompt.memory inspection API without changing wire order.

        Section renderers and lifecycle methods use only ordered native items.
        The compatibility slot belongs to this adapter, not the section protocol.
        """
        from ...client_common import Prompt
        memory, prefix = None, []
        for section in self.prefix:
            if section.section_id == "memory" and len(section.items) == 1 and not prefix and memory is None:
                memory = deepcopy(section.items[0])
            else:
                prefix.extend(deepcopy(section.items))
        return Prompt(instructions, prefix + deepcopy(list(self.history)), tools, memory=memory)


class ContextSection:
    """Default hooks for an append-only section with no separate projection.

    A stateful section overrides validation, prefix rendering, observation and
    compaction as needed. Rendering a change need not duplicate existing receipts.
    """

    def prefix_items(self):
        return []

    def project_items(self, items):
        return deepcopy(items)

    def validate(self, items):
        pass

    def snapshot_state(self):
        """Optional JSON projection state for a host-provided section."""
        return None

    def observe(self, items):
        state = self.snapshot_state()
        if state is None or state == getattr(self, "_observed_state", None):
            return []
        self._observed_state = deepcopy(state)
        return [("section_state", {"section_id": self.ID, "state": deepcopy(state)})]

    def plan_compaction(self, items, first, last, whole):
        return None

    def validate_compaction(self, plan, items):
        pass

    def commit_compaction(self, plan):
        pass

    def restore(self, state):
        raise ValueError(f"Section {self.ID} does not support stored projection state")


class WorldStateCompaction:
    def __init__(self, plans):
        self.plans = tuple(deepcopy(plans))

    @property
    def checkpoints(self):
        return [deepcopy(item) for plan in self.plans for item in plan.checkpoints]

    def history_fields(self):
        """Keep v1-v3 writes compatible; new section IDs use the generic v4 form.

        This is a storage compatibility codec, not rendering or scheduler policy.
        """
        if any(plan.section_id not in {"memory", "tools"} for plan in self.plans):
            return {"world_state_sections": {
                plan.section_id: {"state": deepcopy(plan.state),
                                  "checkpoints": deepcopy(list(plan.checkpoints))}
                for plan in self.plans}}
        fields = {}
        for plan in self.plans:
            if plan.section_id == "memory":
                fields.update(memory_state=deepcopy(plan.state),
                    memory_checkpoint=deepcopy(plan.checkpoints[0]) if plan.checkpoints else None)
            elif plan.section_id == "tools":
                fields.update(mcp_state=deepcopy(plan.state),
                              mcp_checkpoints=deepcopy(list(plan.checkpoints)))
        return fields

    def receipt_fields(self):
        memory = next((p for p in self.plans if p.section_id == "memory"), None)
        return {"memory_checkpoint_added": bool(memory and memory.checkpoints),
                "memory_baseline_advanced": bool(memory and memory.baseline_advanced)}
