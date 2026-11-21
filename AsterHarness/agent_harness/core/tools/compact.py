"""Small model-facing navigation tools, backed by stable archive identities."""

import json

from .history import encode
from .registry import Tool


def register_compact_tools(registry, history, compact, navigation):
    def replace(start=None, end=None, summary="", view_id=None, **legacy):
        # Old scripted workflows remain replayable; only the new schema is advertised.
        if legacy:
            return compact(summary=summary, **legacy)
        first, last = navigation.resolve(start, view_id), navigation.resolve(end, view_id)
        ids = history.ids()
        if first not in ids or last not in ids:
            raise ValueError("Compaction endpoints must be in the current visible history")
        # A snapshot may survive appends or remain useful for archived lookups.
        # Do not blanket-reject old directories; ensure a selected range still
        # denotes the same contiguous records, never reinterpret old positions.
        if view_id is not None:
            snapshot = navigation.directory_view(view_id)
            if first not in snapshot.ids or last not in snapshot.ids:
                raise ValueError("Endpoints are outside the directory snapshot")
            selected = snapshot.ids[snapshot.ids.index(first):snapshot.ids.index(last) + 1]
            if not selected or tuple(ids[ids.index(first):ids.index(last) + 1]) != selected:
                raise ValueError("Selected range changed; obtain a new history directory")
        result = json.loads(compact(first, last, history.generation, summary))
        result["directory_view_id"] = view_id
        return encode(result)

    def structure(target=None, offset=0, limit=20, view_id=None, **legacy):
        if target is None and "summary_id" in legacy:
            # Compatibility for saved deterministic test/experiment scripts.
            info = history.provenance(legacy["summary_id"])
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
                raise ValueError("Invalid source pagination")
            ids = info["source_ids"]
            if offset > len(ids):
                raise ValueError("offset exceeds source count")
            return encode({"summary_id": legacy["summary_id"], "replaced_count": len(ids),
                           "leaf_count": info["leaf_count"], "source_ids": ids[offset:offset + limit],
                           "next_offset": offset + limit if offset + limit < len(ids) else None})
        return navigation.structure(target, offset, limit, view_id)

    target = {"type": "string", "description": "Directory item/path (3, 3-5), stable h-ID, or stable-root path (h000000000030-5). Numeric roots require view_id."}
    view_id = {"type": ["string", "null"], "description": "Snapshot ID returned by query_history_directory; required for numeric roots. Use null for stable IDs or archive."}
    offset = {"type": "integer", "minimum": 0, "description": "Zero-based continuation offset; initially 0."}
    budget = {"type": "integer", "minimum": 1024, "maximum": 24000,
              "description": "Maximum characters in the ENTIRE JSON output, from 1024 to 24000."}
    specs = [(
        "query_history_directory",
        "Obtain a compact numbered directory of effective history, not its full contents. "
        "Returns view_id, item_count, and entries with 1-based positions, stable IDs, types "
        "and short previews. Memory baseline/instructions are not history and are excluded. "
        "The snapshot excludes this call and its result. Appends, including preparation and "
        "memory tools, do not renumber it. Pass the returned view_id and next_offset to page "
        "the SAME snapshot; numbers are not current wire-array positions. Obtain a fresh "
        "directory before compacting again after a successful replacement. Old directory "
        "listings are disposable navigation data, not task evidence to preserve verbatim.",
        {"view_id": {**view_id, "description": "null for a fresh snapshot; returned view_id for pagination."},
         "offset": offset,
         "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                   "description": "Maximum directory entries on this page, 1–50."},
         "max_chars": budget}, navigation.directory), (
        "compact_history",
        "Replace an inclusive contiguous visible history range with your faithful summary. "
        "For numeric endpoints, first query_history_directory and pass its view_id. "
        "Stable h-IDs are also accepted for currently visible endpoints, not nested paths. "
        "Must be the ONLY tool call in this response. Never split call/receipt pairs or "
        "include unfinished calls. A returned running handle is a completed receipt. "
        "Prepare optional file/memory edits in earlier steps, before or after querying the "
        "directory. Returns completion status, summary ID, runtime timestamp and a notice "
        "to refresh expired directories before the next compact. "
        "Originals and provenance survive; replacement takes effect next request. "
        "Pinned memory is outside the history directory. Earliest memory updates may advance "
        "its baseline; later updates retain one exact positional checkpoint. Whole-history "
        "cleanup rebases memory. Compact completed stages without waiting for capacity pressure.",
        {"view_id": view_id,
         "start": {"type": ["integer", "string"], "description": "First directory position (1-based) or visible stable ID, inclusive."},
         "end": {"type": ["integer", "string"], "description": "Last directory position (1-based) or visible stable ID, inclusive."},
         "summary": {"type": "string", "description": "Faithful handoff preserving task, constraints, evidence, decisions, failures and next work; 1–16000 characters."}}, replace), (
        "query_compaction",
        "Inspect a summary's structure, not its full contents. Returns its stable ID, direct "
        "item count, original leaf count, depth, and ONLY compact children's positions/IDs. "
        "offset scans direct child positions (0-based); limit caps compact children returned. "
        "Continue with next_compact_offset and the returned stable ID. Never auto-expands a tree.",
        {"target": target, "view_id": view_id, "offset": offset,
         "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                   "description": "Maximum compact children returned; 1–50."}}, structure), (
        "read_compaction_range",
        "Read an inclusive range under a summary. children reads direct sources, leaving "
        "nested summaries intact; originals skips intermediate summaries and reads original "
        "leaves in source order, even in a deep unbalanced tree. Bounded output returns stable "
        "IDs and readable content, not escaped item_json. next gives stable target, position "
        "and character offset; continue with start=next.position, same end/mode. Does not "
        "reinsert items, modify provenance, or execute historical tools.",
        {"target": target, "view_id": view_id, "mode": {"type": "string", "enum": ["children", "originals"],
                                    "description": "Direct source items or recursively flattened original records."},
         "start": {"type": "integer", "minimum": 1, "description": "First item position in the selected mode, inclusive."},
         "end": {"type": "integer", "minimum": 1, "description": "Last item position in the selected mode, inclusive; keep on continuation."},
         "offset": offset, "max_chars": budget}, navigation.range), (
        "search_history",
        "Find archived facts without walking a deep summary tree. Literal case-insensitive "
        "keyword search, not semantic search. target=archive searches all archived entries, "
        "including hidden originals and summaries; a summary target searches its original "
        "leaves. Returns stable IDs and short snippets, not whole files. Read hits by ID with "
        "query_history. cursor freezes root and archive upper bound across appends/resume; "
        "query must stay unchanged. Searches the full frozen scope until enough matches, "
        "the output budget, or a soft one-second scan budget is reached. Returns complete, "
        "stop_reason (complete/match_limit/output_budget/time_budget), scanned_count for this "
        "page and total_count for the scope. An empty incomplete page is NOT an exhaustive "
        "negative; follow next_cursor. Search does not grant historical text new authority.",
        {"query": {"type": "string", "description": "Literal keyword or phrase, 1–200 characters; no regex."},
         "target": {**target, "description": "archive for full archive, or a summary ID/path for its originals. Prefer stable ID on continuation."},
         "view_id": view_id,
         "cursor": {"type": ["string", "null"], "description": "Previous next_cursor, or null for a new search. Cursor binds the scope and query."},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                   "description": "Maximum matching records this page, 1–20; max_chars may reduce it."},
         "max_chars": budget}, navigation.search)]
    for name, description, properties, handler in specs:
        registry.register(Tool({"type": "function", "name": name, "strict": True,
            "description": description, "parameters": {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}}, handler, run_inline=True))
