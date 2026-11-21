from itertools import islice
from pathlib import Path

from .registry import Tool, ToolRegistry


def build_file_tools(workspace: Path) -> ToolRegistry:
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be a directory")

    def resolve(path: str) -> Path:
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("Path must stay inside the workspace")
        return target

    def list_directory(path: str) -> str:
        # Bound both entry count and returned characters; this is not a full sandbox.
        entries = islice(resolve(path).iterdir(), 101)
        lines = sorted(p.name + ("/" if p.is_dir() else "") for p in entries)
        text = "\n".join(lines[:100])
        if len(lines) > 100:
            text += "\n[more entries omitted]"
        return text[:12000]

    def read_file(path: str, start_line: int, end_line: int) -> str:
        if (type(start_line) is not int or type(end_line) is not int
                or not 1 <= start_line <= end_line
                or end_line - start_line + 1 > 200):
            raise ValueError("Read 1–200 lines using positive inclusive line numbers")
        chunks: list[str] = []
        remaining = 12000
        # readline is bounded even for a file consisting of one gigantic line.
        with resolve(path).open(encoding="utf-8") as stream:
            for number in range(1, end_line + 1):
                line = stream.readline(12001)
                if not line:
                    break
                if number >= start_line:
                    entry = f"{number}: {line}"
                    chunks.append(entry[:remaining])
                    remaining -= len(entry)
                if len(line) == 12001 and not line.endswith("\n"):
                    chunks.append("\n[stopped: oversized line; content truncated]")
                    break
                if remaining <= 0:
                    chunks.append("\n[output truncated]")
                    break
        return "".join(chunks)

    registry = ToolRegistry()
    for name, description, properties, handler in [
        ("list_directory", "List up to 100 entries in a workspace directory, not recursively. "
         "Directory names end in '/'; output is capped at 12000 characters and may be incomplete.",
         {"path": {"type": "string", "description": "Directory path relative to the Session workspace. Use '.' for the workspace root. Must resolve inside the workspace."}}, list_directory),
        ("read_file", "Read a UTF-8 file with line numbers. Reads an inclusive range of up to "
         "200 lines with approximately 12000 characters of output; oversized content is truncated.",
         {"path": {"type": "string", "description": "File path relative to the Session workspace. Must resolve inside the workspace."},
          "start_line": {"type": "integer", "description": "First line to read, inclusive and 1-based. Must be positive."},
          "end_line": {"type": "integer", "description": "Last line to read, inclusive. Must be at least start_line and at most start_line + 199."}}, read_file),
    ]:
        registry.register(Tool({
            "type": "function", "name": name, "description": description,
            "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False},
        }, handler))
    return registry
