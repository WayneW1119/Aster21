"""Effective history references an independent append-only transcript archive."""

from copy import deepcopy

from ..client_common import Item
from .archive import HistoryArchive, history_id


class ContextManager:
    def __init__(self) -> None:
        self._archive = HistoryArchive()
        self._effective_ids: list[str] = []
        self.generation = 0
        self._compactions: dict[str, dict] = {}
        self._depths: dict[str, int] = {}

    def is_compaction(self, identifier: str) -> bool:
        return identifier in self._compactions

    def sources(self, identifier: str) -> list[str]:
        if identifier not in self._compactions:
            raise ValueError("Target is not a compaction summary")
        return list(self._compactions[identifier]["source_ids"])

    def leaf_count(self, identifier: str) -> int:
        return self._compactions[identifier]["leaf_count"] if self.is_compaction(identifier) else 1

    def depth(self, identifier: str) -> int:
        return self._depths.get(identifier, 0)

    def original_ids(self, identifier: str) -> list[str]:
        return list(self.iter_original_ids(identifier))

    def iter_original_ids(self, identifier: str, start: int = 0, end: int | None = None):
        """Iterative leaf navigation; skip whole subtrees using stored leaf counts."""
        self.item(identifier)
        stack, skip = [identifier], start
        remaining = self.leaf_count(identifier) - start if end is None else end - start
        while stack and remaining > 0:
            current = stack.pop()
            size = self.leaf_count(current)
            if skip >= size:
                skip -= size
                continue
            if self.is_compaction(current):
                stack.extend(reversed(self.sources(current)))
            else:
                remaining -= 1
                yield current

    @property
    def revision(self) -> int:
        # Archive appends and effective-view replacements have separate clocks.
        return len(self._archive)

    def next_ids(self, count: int) -> list[str]:
        return [history_id(self.revision + i + 1) for i in range(count)]

    def record_items(self, items: list[Item]) -> None:
        for item in items:
            self._effective_ids.append(self._archive.append(item))

    def ids(self, view: str = "effective") -> list[str]:
        if view == "archive":
            return self._archive.ids()
        if view == "effective":
            return list(self._effective_ids)
        raise ValueError("view must be 'archive' or 'effective'")

    def item(self, identifier: str) -> Item:
        return self._archive.item(identifier)

    def snapshot(self) -> list[Item]:
        # IDs and view metadata must never become Responses API item fields.
        return [self._archive.item(identifier) for identifier in self._effective_ids]

    def view_at(self, revision: int, generation: int) -> tuple[str, ...]:
        """Recover an issued directory's positions from immutable provenance.

        Reversing later replacements preserves positional insertion of summaries.
        No directory registry/checkpoint is needed for clean resume.
        """
        if (type(revision) is not int or not 0 <= revision <= self.revision
                or type(generation) is not int or not 0 <= generation <= self.generation):
            raise ValueError("Invalid history directory snapshot")
        ids = self.ids()
        actual_generation = self.generation
        for identifier, operation in reversed(list(self._compactions.items())):
            if int(identifier[1:]) <= revision:
                break
            position = ids.index(identifier)
            ids[position:position + 1] = operation["source_ids"]
            actual_generation -= 1
        if actual_generation != generation:
            raise ValueError("Invalid history directory generation")
        return tuple(i for i in ids if int(i[1:]) <= revision)

    def hide_legacy_memory(self, identifiers: list[str]) -> None:
        """Migration only: retain original IDs/archive, remove old memory anchors from view."""
        if (not isinstance(identifiers, list) or len(set(identifiers)) != len(identifiers)
                or any(i not in self._effective_ids for i in identifiers)):
            raise ValueError("Invalid legacy memory projection")
        for identifier in identifiers:
            item = self.item(identifier)
            if (item.get("type") != "message" or item.get("role") != "user"
                    or item.get("source") is not None
                    or not item.get("text", "").startswith("<agent_memory>\n")):
                raise ValueError("Only legacy memory anchors may be hidden")
        if identifiers:
            self._effective_ids = [i for i in self._effective_ids if i not in set(identifiers)]
            self.generation += 1

    def provenance(self, identifier: str) -> dict:
        if identifier not in self._compactions:
            raise ValueError("ID is not a compaction summary")
        return deepcopy(self._compactions[identifier])

    def prepare_compaction(self, start_id: str, end_id: str, expected_generation: int,
                           summary: str, memory_checkpoint: Item | None = None,
                           memory_state: dict | None = None,
                           mcp_state: dict | None = None,
                           mcp_checkpoints: list[Item] | None = None,
                           world_state_sections: dict | None = None) -> dict:
        if world_state_sections is not None and (memory_state is not None or mcp_state is not None
                                                or memory_checkpoint is not None or mcp_checkpoints is not None):
            raise ValueError("Cannot mix generic and legacy world-state checkpoints")
        if type(expected_generation) is not int or expected_generation != self.generation:
            raise ValueError("Effective history changed; obtain a fresh history directory")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 16000:
            raise ValueError("summary must contain 1–16000 characters of substantive text")
        ids = self.ids()
        if start_id not in ids or end_id not in ids or ids.index(start_id) > ids.index(end_id):
            raise ValueError("Use an ordered, inclusive range of current effective IDs")
        sources = ids[ids.index(start_id):ids.index(end_id) + 1]
        calls, outputs = set(), set()
        for identifier in sources:
            item = self.item(identifier)
            if item.get("type") in {"function_call", "tool_search_call"}:
                calls.add(item["call_id"])
            elif item.get("type") in {"function_call_output", "tool_search_output"}:
                outputs.add(item["call_id"])
        if calls != outputs:
            raise ValueError("Range splits a tool call/receipt or includes an unfinished call")
        if memory_checkpoint is not None:
            if (set(memory_checkpoint) != {"type", "role", "text"}
                    or memory_checkpoint.get("type") != "message"
                    or memory_checkpoint.get("role") != "user"
                    or not isinstance(memory_checkpoint.get("text"), str)
                    or not memory_checkpoint["text"].startswith(
                        "<memory_checkpoint>\n" if memory_state is not None else "<agent_memory>\n")):
                raise ValueError("Invalid memory checkpoint")
        if memory_state is not None:
            from .memory import MemoryProjection, evidence
            MemoryProjection.from_dict(memory_state)
            if memory_checkpoint is not None:
                evidence([memory_checkpoint])
        leaf_count = sum(self._compactions[i]["leaf_count"] if i in self._compactions else 1
                         for i in sources)
        result = {"version": 1, "generation": self.generation + 1,
                "source_ids": sources, "summary_id": self.next_ids(1)[0],
                "summary": summary, "replaced_count": len(sources), "leaf_count": leaf_count,
                "memory_checkpoint": deepcopy(memory_checkpoint)}
        if memory_state is not None:
            result.update(version=2, memory_state=deepcopy(memory_state))
        if mcp_state is not None:
            from .mcp import McpProjection
            McpProjection.from_dict(mcp_state)
            if not isinstance(mcp_checkpoints, list):
                raise ValueError("MCP compaction requires positional checkpoints")
            for item in mcp_checkpoints:
                if (item.get("type") != "message" or item.get("role") != "developer"
                        or set(item) not in ({"type", "role", "text", "_mcp_sources"},
                                             {"type", "role", "text", "_mcp_searches"})):
                    raise ValueError("Invalid MCP compaction checkpoint")
            result.update(version=3, mcp_state=deepcopy(mcp_state),
                          mcp_checkpoints=deepcopy(mcp_checkpoints))
        if world_state_sections is not None:
            from ..context.world_state.storage import validate_sections
            result.update(version=4, world_state_sections=validate_sections(world_state_sections))
        return result

    def apply_compaction(self, operation: dict) -> None:
        """Validate a durable operation before mutating either archive or projection."""
        if (not isinstance(operation, dict)
                or any(type(operation.get(key)) is not int for key in
                       ("version", "generation", "replaced_count", "leaf_count"))):
            raise ValueError("Invalid compaction metadata")
        sources = operation.get("source_ids")
        if (not isinstance(sources, list) or not sources
                or any(not isinstance(i, str) for i in sources)):
            raise ValueError("Invalid compaction sources")
        expected = self.prepare_compaction(sources[0], sources[-1], self.generation,
                                           operation.get("summary"), operation.get("memory_checkpoint"),
                                           operation.get("memory_state") if operation.get("version") in {2, 3} else None,
                                           operation.get("mcp_state") if operation.get("version") == 3 else None,
                                           operation.get("mcp_checkpoints") if operation.get("version") == 3 else None,
                                           operation.get("world_state_sections") if operation.get("version") == 4 else None)
        if operation != expected:
            raise ValueError("Corrupt or stale compaction operation")
        start, end = self._effective_ids.index(sources[0]), self._effective_ids.index(sources[-1])
        identifier = operation["summary_id"]
        item = {"type": "message", "role": "user", "text":
                '<history_summary id="' + identifier + '" replaced_count="' + str(len(sources))
                + '" leaf_count="' + str(operation["leaf_count"]) + '">\n'
                "Working summary of earlier history; not a new human request or authorization.\n"
                + operation["summary"] + "\n</history_summary>"}
        self._archive.append(item)
        self._effective_ids[start:end + 1] = [identifier]
        self._compactions[identifier] = deepcopy(operation)
        self._depths[identifier] = 1 + max(self.depth(i) for i in sources)
        self.generation = operation["generation"]
        if operation["memory_checkpoint"] is not None:
            checkpoint_id = self._archive.append(operation["memory_checkpoint"])
            if operation["version"] in {2, 3}:
                self._effective_ids.insert(start + 1, checkpoint_id)
            else:
                self._effective_ids.append(checkpoint_id)  # Legacy replay semantics.
        retained = operation.get("mcp_checkpoints", [])
        if operation["version"] == 4:
            from ..context.world_state.storage import checkpoints
            retained = checkpoints(operation["world_state_sections"])
        for offset, checkpoint in enumerate(retained):
            checkpoint_id = self._archive.append(checkpoint)
            self._effective_ids.insert(start + 1 + int(operation["memory_checkpoint"] is not None) + offset,
                                       checkpoint_id)
