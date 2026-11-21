"""Codex's bounded deferred namespace summaries in our WorldState interface."""

from copy import deepcopy

from . import WorldStateUpdate
from .lifecycle import ContextSection, SectionCompaction


def bounded_namespaces(namespaces):
    # Reserve names first, as Codex does; long instructions must not hide peers.
    result, descriptions, budget = {}, {}, 3900
    for name, text in sorted(namespaces.items()):
        cost = len(("- " + name + "\n").encode())
        if cost > budget:
            continue
        budget -= cost
        result[name] = ""
        descriptions[name] = text.splitlines()[0].strip()[:250] if text.splitlines() else ""
    positions = {name: 0 for name in result}
    while budget:
        progressed = False
        for name, description in descriptions.items():
            index = positions[name]
            if index == len(description):
                continue
            cost = len(description[index].encode()) + (2 if index == 0 else 0)
            if cost <= budget:
                result[name] += description[index]
                positions[name] += 1
                budget -= cost
                progressed = True
        if not progressed:
            break
    return result


def sources_item(current, previous=None):
    if previous is None:
        groups = [("Deferred tool namespaces", current)]
    else:
        groups = [("Added deferred tool namespaces", {k: v for k, v in current.items()
                    if previous.get(k) != v}),
                  ("Removed deferred tool namespaces", {k: v for k, v in previous.items()
                    if k not in current})]
    lines = []
    for title, values in groups:
        if values:
            lines.append(title + ":")
            lines.extend(f"- {name}" + (f": {text}" if text else "")
                         for name, text in sorted(values.items()))
    if not current:
        lines.append("No deferred tool namespaces remain.")
    return {"type": "message", "role": "developer", "text":
            "<tools>\n" + "\n".join(lines) + "\n</tools>",
            "_mcp_sources": deepcopy(current)}


class ToolsState(ContextSection):
    ID = "tools"

    def __init__(self, namespaces, projection, active=True):
        self.namespaces = bounded_namespaces(namespaces)
        self.projection = projection
        self.active = active

    def prefix_items(self):
        return self.projection.prefix_items()

    def project_items(self, items):
        from ...context_manager.mcp import request_items
        return request_items(items)

    def validate(self, items):
        self.projection.validate(items)

    def observe(self, items):
        before = self.projection.to_dict()
        self.projection.observe(items)
        after = self.projection.to_dict()
        return [("mcp_context", after)] if before != after else []

    def plan_compaction(self, items, first, last, whole):
        state, checkpoints = self.projection.plan_compaction(items, first, last, whole=whole)
        return SectionCompaction(self.ID, state, tuple(checkpoints),
                                 state["baseline"] != self.projection.baseline)

    def validate_compaction(self, plan, items):
        from ...context_manager.mcp import McpProjection
        McpProjection.from_dict(plan.state).validate(items)

    def restore(self, state):
        self.projection.restore(state)

    def commit_compaction(self, plan):
        self.restore(plan.state)

    def render_diff(self, previous, items):
        if not self.active:
            return None
        # Internal snapshots alone cannot prove that context survived compaction.
        visible = self.projection.reconstruct(items)["namespaces"]
        additions = () if visible == self.namespaces else (sources_item(self.namespaces, visible),)
        snapshot = {"namespaces": self.namespaces}
        if previous == snapshot and not additions:
            return None
        return WorldStateUpdate(self.ID, snapshot, additions)
