"""Pinned source summaries; discovered definitions belong only to effective history.

Search call/output pairs are ordinary native history. Compaction drops covered
pairs, without definition checkpoints or prefix replay. Legacy v1 checkpoints are
validated on resume, then retired without altering the append-only archive.
"""

from copy import deepcopy

from ..context.world_state.tools import sources_item


def empty_state():
    return {"namespaces": {}, "searches": []}


def apply_items(state, items, *, legacy=False):
    state, calls = deepcopy(state), {}
    if not legacy:
        state["searches"] = []  # Only actual, currently visible search pairs load definitions.
    known = {pair[0]["call_id"]: pair for pair in state["searches"]}
    for item in items:
        if "_mcp_sources" in item:
            state["namespaces"] = deepcopy(item["_mcp_sources"])
        for pair in item.get("_mcp_searches", []) if legacy else []:
            if pair[0]["call_id"] not in known:
                state["searches"].append(deepcopy(pair))
                known[pair[0]["call_id"]] = pair
            elif known[pair[0]["call_id"]] != pair:
                raise RuntimeError("Conflicting retained MCP search definitions")
        if item.get("type") == "tool_search_call":
            calls[item["call_id"]] = item
        if item.get("type") == "tool_search_output":
            call = calls.get(item["call_id"])
            if call is None:
                raise RuntimeError("MCP context has an unpaired tool search result")
            if item["call_id"] not in known:
                state["searches"].append(deepcopy([call, item]))
                known[item["call_id"]] = [call, item]
            elif known[item["call_id"]] != [call, item]:
                raise RuntimeError("Conflicting visible MCP search definitions")
    return state


class McpProjection:
    def __init__(self, baseline=None, current=None):
        self.baseline = deepcopy(baseline if baseline is not None else empty_state())
        if self.baseline["searches"]:
            raise ValueError("Search definitions cannot be pinned in a v2 tool baseline")
        self.current = deepcopy(current if current is not None else self.baseline)
        self._legacy_state = None

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {"version", "baseline", "current"}
                or type(value["version"]) is not int or value["version"] not in {1, 2}):
            raise ValueError("Invalid MCP context projection")
        for state in (value["baseline"], value["current"]):
            if (not isinstance(state, dict) or set(state) != {"namespaces", "searches"}
                    or not isinstance(state["namespaces"], dict)
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in state["namespaces"].items())
                    or not isinstance(state["searches"], list)):
                raise ValueError("Invalid MCP context state")
            ids = set()
            for pair in state["searches"]:
                if (not isinstance(pair, list) or len(pair) != 2
                        or any(not isinstance(item, dict) for item in pair)
                        or pair[0].get("type") != "tool_search_call"
                        or pair[1].get("type") != "tool_search_output"
                        or not isinstance(pair[0].get("call_id"), str)
                        or pair[0]["call_id"] != pair[1].get("call_id")
                        or not isinstance(pair[1].get("tools"), list)
                        or pair[0]["call_id"] in ids):
                    raise ValueError("Invalid MCP search checkpoint")
                for namespace in pair[1]["tools"]:
                    if (not isinstance(namespace, dict) or namespace.get("type") != "namespace"
                            or not isinstance(namespace.get("name"), str)
                            or not isinstance(namespace.get("tools"), list)):
                        raise ValueError("Invalid retained MCP namespace")
                    for tool in namespace["tools"]:
                        if (not isinstance(tool, dict) or tool.get("type") != "function"
                                or not isinstance(tool.get("name"), str)
                                or not isinstance(tool.get("parameters"), dict)
                                or tool["parameters"].get("type") != "object"):
                            raise ValueError("Invalid retained MCP tool schema")
                ids.add(pair[0]["call_id"])
        if value["version"] == 2 and value["baseline"]["searches"]:
            raise ValueError("Search definitions cannot be pinned in a v2 tool baseline")
        result = cls()
        result.baseline, result.current = deepcopy(value["baseline"]), deepcopy(value["current"])
        result._legacy_state = deepcopy(value) if value["version"] == 1 else None
        return result

    def to_dict(self):
        if self._legacy_state is not None:
            return deepcopy(self._legacy_state)  # Read-only storage validation keeps old records exact.
        return {"version": 2, "baseline": deepcopy(self.baseline), "current": deepcopy(self.current)}

    def restore(self, state):
        restored = type(self).from_dict(state)
        self.baseline, self.current = restored.baseline, restored.current
        self._legacy_state = restored._legacy_state

    def migrate_legacy(self, items):
        if self._legacy_state is None:
            return False
        if apply_items(self.baseline, items, legacy=True) != self.current:
            raise RuntimeError("MCP context invariant failed during legacy migration")
        self.baseline = {"namespaces": deepcopy(self.baseline["namespaces"]), "searches": []}
        self.current = apply_items(self.baseline, items)
        self._legacy_state = None
        return True

    def reconstruct(self, items):
        return apply_items(self.baseline, items)

    def loaded_definitions(self):
        loaded = {}
        for _, output in self.current["searches"]:
            for namespace in output["tools"]:
                for tool in namespace["tools"]:
                    loaded[(namespace["name"], tool["name"])] = deepcopy(tool)
        return loaded

    def validate(self, items):
        if self._legacy_state is not None:
            raise RuntimeError("Legacy MCP context requires migration before model requests")
        if self.reconstruct(items) != self.current:
            raise RuntimeError("MCP context invariant failed: visible definitions differ from tracked state")

    def observe(self, items):
        self.current = self.reconstruct(items)

    def prefix_items(self):
        return [sources_item(self.baseline["namespaces"])] if self.baseline["namespaces"] else []

    def plan_compaction(self, items, first, last, whole=False):
        self.validate(items)
        state = self.to_dict()
        checkpoints = []
        if whole:
            state["baseline"] = {"namespaces": deepcopy(self.current["namespaces"]), "searches": []}
        else:
            # Namespace availability is WorldState; discovered schemas are history.
            source_items = [item for item in items[first:last + 1] if "_mcp_sources" in item]
            if source_items:
                checkpoints.append(sources_item(source_items[-1]["_mcp_sources"]))
        remaining = items[:first] + checkpoints + items[last + 1:]
        state["current"] = apply_items(state["baseline"], remaining)
        return state, checkpoints


def request_items(items):
    """Strip internal metadata and retire legacy search checkpoints from model input."""
    result = []
    for original in items:
        if "_mcp_searches" in original:
            continue  # Legacy archive content remains queryable, but no longer loads tools.
        item = deepcopy(original)
        item.pop("_mcp_sources", None)
        item.pop("_mcp_configuration", None)
        item.pop("_custom_tool_change", None)
        result.append(item)
    return result
