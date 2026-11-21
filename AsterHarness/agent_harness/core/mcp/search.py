"""Client-executed BM25 tool search, following Codex's ToolSearchHandler.

The catalog is captured with a Step; search returns native loadable definitions,
never a prose list of guessed function names. No remote embedding requests.
"""

from collections import Counter
from copy import deepcopy
import math
import re


SEARCH_SPEC = {
    "type": "tool_search", "name": "tool_search", "execution": "client",
    "description": "# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools for the next model call.\n\nSome of the tools may not have been provided to you upfront, and you should use this tool (`tool_search`) to search for the required tools. For MCP tool discovery, always use `tool_search` instead of `list_mcp_resources` or `list_mcp_resource_templates`.",
    "parameters": {"type": "object", "properties": {
        "limit": {"type": "number", "description": "Maximum number of tools to return. Defaults to 8."},
        "query": {"type": "string", "description": "Search query for deferred tools."}},
        "required": ["query"], "additionalProperties": False}}


def tokens(text):
    return re.findall(r"[^\W_]+", text.casefold())


class ToolSearch:
    def __init__(self, binding):
        self.entries = binding.tools
        self.documents = []
        for tool in self.entries:
            self.documents.append(Counter(tokens(tool.search_text)))
        self.lengths = [sum(doc.values()) for doc in self.documents]
        self.average = sum(self.lengths) / max(1, len(self.lengths)) or 1
        self.frequency = Counter(term for doc in self.documents for term in doc)

    def search(self, query, limit=8):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        ranked = []
        for index, doc in enumerate(self.documents):
            score = 0
            for term in set(tokens(query)):
                frequency = doc.get(term, 0)
                if frequency:
                    inverse = math.log(1 + (len(self.entries) - self.frequency[term] + .5)
                                       / (self.frequency[term] + .5))
                    score += inverse * frequency * 2.2 / (frequency + 1.2 * (
                        .25 + .75 * self.lengths[index] / self.average))
            if score > 0:
                ranked.append((-score, index))
        namespaces = {}
        for _, index in sorted(ranked)[:limit]:
            spec = self.entries[index].loadable_spec
            if spec["name"] in namespaces:
                namespaces[spec["name"]]["tools"].extend(spec["tools"])
            else:
                namespaces[spec["name"]] = deepcopy(spec)
        return list(namespaces.values())
