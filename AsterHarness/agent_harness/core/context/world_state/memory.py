"""Thread memory context, retaining this harness's tool-driven update policy."""

import hashlib
import json

from ...context_manager.agent_context import AgentContext
from . import WorldStateUpdate
from .lifecycle import ContextSection, SectionCompaction


class MemoryState(ContextSection):
    ID = "memory"
    TOOL_NAMES = frozenset({"read_memory", "write_memory", "apply_memory_patch"})

    def __init__(self, context: AgentContext, projection=None, on_error=None) -> None:
        self.context = context
        self.projection = projection
        self.on_error = on_error

    def prefix_items(self):
        return [self.projection.item()] if self.projection is not None else [self.context.memory_item()]

    def validate(self, items):
        if self.projection is None:
            return
        try:
            self.projection.validate(items, self.context)
        except (RuntimeError, ValueError, OSError) as error:
            if self.on_error is not None:
                self.on_error({"error": str(error), "memory": self.projection.to_dict(),
                               "visible_history": items})
            raise

    def plan_compaction(self, items, first, last, whole):
        if self.projection is None:
            return None
        state, checkpoint = self.projection.plan_compaction(items, first, last, whole=whole)
        return SectionCompaction(self.ID, state, (checkpoint,) if checkpoint is not None else (),
                                 state["baseline"] != self.projection.baseline)

    def validate_compaction(self, plan, items):
        from ...context_manager.memory import MemoryProjection
        MemoryProjection.from_dict(plan.state).validate(items, self.context)

    def restore(self, state):
        from ...context_manager.memory import MemoryProjection
        restored = MemoryProjection.from_dict(state)
        self.projection.baseline = restored.baseline
        self.projection.current = restored.current

    def commit_compaction(self, plan):
        self.restore(plan.state)

    @staticmethod
    def matches_retained_fragment(item: dict) -> bool:
        return (item.get("type") == "message" and item.get("role") == "user"
                and item.get("source") is None
                and isinstance(item.get("text"), str)
                and item["text"].startswith("<agent_memory>\n"))

    def render_diff(self, previous: dict | None,
                    items: list[dict]) -> WorldStateUpdate | None:
        start = next((i for i in range(len(items) - 1, -1, -1)
                      if self.matches_retained_fragment(items[i])), None)
        if self.projection is not None:
            evidence, additions = [self.projection.item(), *items], ()
        elif start is None:
            # New context, or legacy history missing its memory anchor. Append once;
            # never replace an earlier prefix with current filesystem contents.
            initial = self.context.memory_item()
            evidence, additions = [initial], (initial,)
        else:
            evidence, additions = items[start:], ()
        initial_bytes = json.dumps(evidence[0], ensure_ascii=False, sort_keys=True).encode()
        digest = hashlib.sha256(initial_bytes)
        calls, observations = {}, 0
        for item in evidence[1:]:
            if item.get("type") == "function_call" and item.get("name") in self.TOOL_NAMES:
                calls[item["call_id"]] = item
            elif item.get("type") == "function_call_output" and item.get("call_id") in calls:
                # Fingerprint model-visible evidence, not live disk state. Include
                # failure/partial-progress receipts without claiming the whole edit succeeded.
                pair = [calls.pop(item["call_id"]), item]
                digest.update(json.dumps(pair, ensure_ascii=False, sort_keys=True).encode())
                observations += 1
        snapshot = {"initial_hash": hashlib.sha256(initial_bytes).hexdigest(),
                    "evidence_hash": digest.hexdigest(), "observations": observations}
        if snapshot == previous and not additions:
            return None
        return WorldStateUpdate(self.ID, snapshot, additions)
