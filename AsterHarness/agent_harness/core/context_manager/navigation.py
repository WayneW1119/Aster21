"""On-demand snapshot directories over immutable archive/provenance identities."""

import base64
from dataclasses import dataclass
import hashlib
import json
import re
from time import monotonic


@dataclass(frozen=True)
class HistoryView:
    ids: tuple[str, ...]
    generation: int
    revision: int | None = None


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def readable(item):
    """Preserve readable content, never invent hidden reasoning."""
    kind = item.get("type")
    if kind == "message":
        if isinstance(item.get("text"), str):
            return item["text"] + ("\nAttachments: " + encode(item["_attachments"])
                                    if item.get("_attachments") else "")
    if kind == "function_call_output" and isinstance(item.get("output"), str):
        return item["output"]
    if kind == "function_call_output" and isinstance(item.get("output"), list):
        return "\n".join(part.get("text", "[Image pixels retained in archive; use view_image with its saved path.]")
                         for part in item["output"])
    if kind == "function_call":
        return item.get("name", "") + "\n" + item.get("arguments", "")
    if kind == "reasoning":
        return encode({"type": "reasoning", "summary": item.get("summary", []),
                       "note": "Hidden/encrypted reasoning is not readable text."})
    return encode(item)


class HistoryNavigator:
    def __init__(self, history, view, scope):
        self.history, self.view, self.scope = history, view, scope

    def _view_id(self, view):
        scope = hashlib.sha256(self.scope.encode()).hexdigest()[:16]
        return f"v{view.generation}-r{view.revision}-{scope}"

    def directory_view(self, view_id):
        if not isinstance(view_id, str) or len(view_id) > 100:
            raise ValueError("Use a view_id returned by query_history_directory")
        match = re.fullmatch(r"v([0-9]+)-r([0-9]+)-([0-9a-f]{16})", view_id)
        if match is None or match[3] != hashlib.sha256(self.scope.encode()).hexdigest()[:16]:
            raise ValueError("Invalid history directory for this Thread")
        generation, revision = int(match[1]), int(match[2])
        return HistoryView(self.history.view_at(revision, generation), generation, revision)

    def directory(self, view_id=None, offset=0, limit=20, max_chars=6000):
        self.budget(max_chars)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("offset must be nonnegative; limit must be 1–50")
        if view_id is None:
            view = self.view()
            if view is None:
                view = HistoryView(tuple(self.history.ids()), self.history.generation, self.history.revision)
            elif view.revision is None:
                view = HistoryView(view.ids, view.generation, self.history.revision)
            view_id = self._view_id(view)
        else:
            view = self.directory_view(view_id)
        if offset > len(view.ids):
            raise ValueError("offset exceeds directory item count")
        result = {"view_id": view_id, "item_count": len(view.ids), "entries": [],
                  "next_offset": None}
        for index in range(offset, min(offset + limit, len(view.ids))):
            identifier = view.ids[index]
            item = self.history.item(identifier)
            entry = {"position": index + 1, "id": identifier, "type": item["type"]}
            for key in ("role", "name", "call_id"):
                if key in item:
                    entry[key] = item[key]
            if self.history.is_compaction(identifier):
                entry["is_compaction"] = True
            entry["preview"] = readable(item)[:100]
            result["entries"].append(entry)
            result["next_offset"] = index + 1 if index + 1 < len(view.ids) else None
            if len(encode(result)) > max_chars:
                result["entries"].pop()
                result["next_offset"] = index
                if not result["entries"]:
                    raise ValueError("Budget too small for directory metadata")
                break
        return encode(result)

    def resolve(self, target, view_id=None):
        if type(target) is int:
            target = str(target)
        if not isinstance(target, str) or not re.fullmatch(r"(?:h[0-9]+|[1-9][0-9]*)(?:-[1-9][0-9]*)*", target):
            raise ValueError("Use a directory position, stable h-ID, or path such as 3-5 / h000000000030-5")
        root, *children = target.split("-")
        if root.startswith("h"):
            self.history.item(root)  # Validate identity, including archived-only records.
            identifier = root
        else:
            view = self.directory_view(view_id)
            if int(root) > len(view.ids):
                raise ValueError("Position is outside this directory snapshot")
            identifier = view.ids[int(root) - 1]
        for position in children:
            sources = self.history.sources(identifier)
            if int(position) > len(sources):
                raise ValueError("Child position exceeds this summary's direct source count")
            identifier = sources[int(position) - 1]
        return identifier

    @staticmethod
    def budget(max_chars):
        if type(max_chars) is not int or not 1024 <= max_chars <= 24000:
            raise ValueError("max_chars must be 1024–24000 for the whole JSON result")

    def read(self, target, offset=0, max_chars=6000, view_id=None):
        self.budget(max_chars)
        identifier = self.resolve(target, view_id)
        return self._page(identifier, [(1, identifier)], 1, 1, offset, max_chars, single=True)

    def structure(self, target, offset=0, limit=20, view_id=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("offset must be nonnegative; limit must be 1–50")
        identifier = self.resolve(target, view_id)
        sources = self.history.sources(identifier)
        if offset > len(sources):
            raise ValueError("offset exceeds direct source count")
        # Offset scans direct positions; at most `limit` compact children are returned.
        compact, index = [], offset
        while index < len(sources) and len(compact) < limit:
            child = sources[index]
            if self.history.is_compaction(child):
                compact.append({"position": index + 1, "id": child,
                                "original_item_count": self.history.leaf_count(child)})
            index += 1
        return encode({"id": identifier, "item_count": len(sources),
            "original_item_count": self.history.leaf_count(identifier),
            "max_depth": self.history.depth(identifier), "compact_items": compact,
            "next_compact_offset": index if index < len(sources) else None})

    def range(self, target, mode, start, end, offset=0, max_chars=6000, view_id=None):
        self.budget(max_chars)
        identifier = self.resolve(target, view_id)
        if not self.history.is_compaction(identifier):
            raise ValueError("Range target must be a summary; use query_history for one ordinary item")
        if mode == "children":
            ids = self.history.sources(identifier)
            count = len(ids)
        elif mode == "originals":
            count = self.history.leaf_count(identifier)
        else:
            raise ValueError("mode must be children or originals")
        if (type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= count):
            raise ValueError("Use an inclusive 1-based range within the selected sequence")
        selected = (ids[start - 1:end] if mode == "children" else
                    self.history.iter_original_ids(identifier, start - 1, end))
        return self._page(identifier, enumerate(selected, start), start, end,
                          offset, max_chars, mode=mode)

    def _page(self, parent, entries, start, end, offset, budget, single=False, mode=None):
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")
        result = {"id" if single else "parent_id": parent}
        if mode is not None:
            result["mode"] = mode
        result.update(items=[], next=None)
        for position, identifier in entries:
            text = readable(self.history.item(identifier))
            begin = offset if position == start else 0
            if begin > len(text):
                raise ValueError("offset exceeds item content")
            entry = {"position": position, "id": identifier,
                     "is_compaction": self.history.is_compaction(identifier),
                     "offset": begin, "total_chars": len(text), "content": "", "complete": False}
            result["items"].append(entry)

            def candidate(count):
                finish = begin + count
                entry.update(content=text[begin:finish], complete=finish == len(text))
                result["next"] = ({"target": parent, "position": position, "offset": finish}
                                  if finish < len(text) else
                                  {"target": parent, "position": position + 1, "offset": 0}
                                  if position < end else None)
                return encode(result)

            low, high = 0, min(len(text) - begin, budget)
            while low < high:
                mid = (low + high + 1) // 2
                if len(candidate(mid)) <= budget:
                    low = mid
                else:
                    high = mid - 1
            raw = candidate(low)
            if len(raw) > budget or (low == 0 and begin < len(text)):
                result["items"].pop()
                result["next"] = {"target": parent, "position": position, "offset": begin}
                if not result["items"]:
                    raise ValueError("Budget too small for item metadata")
                break
            if not entry["complete"]:
                break
        return encode(result)

    def search(self, query, target="archive", cursor=None, limit=5, max_chars=6000, view_id=None):
        self.budget(max_chars)
        if (not isinstance(query, str) or not query.strip() or len(query) > 200
                or type(limit) is not int or not 1 <= limit <= 20):
            raise ValueError("query must be 1–200 characters; limit must be 1–20")
        digest = hashlib.sha256(query.encode()).hexdigest()
        view = self.view()
        revision = view.revision if view is not None and view.revision is not None else self.history.revision
        root, index = None, 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (not isinstance(data, list) or len(data) != 6
                        or data[:2] != [self.scope, digest]
                        or not isinstance(data[2], str)
                        or not re.fullmatch(r"archive|h[0-9]+", data[2])
                        or type(data[3]) is not int or not 0 <= data[3] <= self.history.revision
                        or type(data[4]) is not int or data[4] < 0 or data[5] != 1):
                    raise ValueError
                root, revision, index = data[2:5]
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise ValueError("Invalid search cursor for this Thread/query") from None
        else:
            root = "archive" if target == "archive" else self.resolve(target, view_id)
        deadline = monotonic() + 1.0
        if root != "archive" and int(root[1:]) > revision:
            raise ValueError("Search root is outside its frozen snapshot")
        if root == "archive":
            ids = self.history.ids("archive")[:revision]
            count = len(ids)
            candidates = iter(ids[index:])
        else:
            self.resolve(root)
            count = self.history.leaf_count(root)
            candidates = self.history.iter_original_ids(root, index)
        if index > count:
            raise ValueError("Invalid search cursor position")

        def continuation(position):
            return base64.urlsafe_b64encode(encode(
                [self.scope, digest, root, revision, position, 1]).encode()).decode()

        # Reserve the largest possible metadata while fitting snippets. Final
        # counts/cursor/status can only shrink, including when a page ends at EOF.
        result = {"target": root, "snapshot_revision": revision, "matches": [],
                  "next_cursor": continuation(count), "complete": False,
                  "stop_reason": "output_budget", "scanned_count": count, "total_count": count}
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        first_index = index
        stop_reason = "match_limit"

        while index < count and len(result["matches"]) < limit:
            # A soft elapsed-time budget, not a fixed record window. Always make
            # progress; a single large record cannot be preempted mid-scan.
            if index > first_index and monotonic() >= deadline:
                stop_reason = "time_budget"
                break
            identifier = next(candidates)
            text = readable(self.history.item(identifier))
            match = pattern.search(text)
            if match:
                snippet = text[max(0, match.start() - 80):match.end() + 160]
                entry = {"id": identifier,
                    "is_compaction": self.history.is_compaction(identifier),
                    "match_offset": match.start(),
                    "snippet": snippet}
                result["matches"].append(entry)
                index += 1
                if len(encode(result)) > max_chars:
                    if len(result["matches"]) > 1:
                        result["matches"].pop()
                        index -= 1
                        stop_reason = "output_budget"
                        break
                    # Even heavily escaped snippets must make bounded progress.
                    entry["snippet"] = text[match.start():match.end() + 80]
                    while entry["snippet"] and len(encode(result)) > max_chars:
                        entry["snippet"] = entry["snippet"][:len(entry["snippet"]) // 2]
                    if len(encode(result)) > max_chars:
                        raise ValueError("Budget too small for search metadata")
            else:
                index += 1
        result.update(next_cursor=continuation(index) if index < count else None,
                      complete=index == count,
                      stop_reason="complete" if index == count else stop_reason,
                      scanned_count=index - first_index)
        return encode(result)
