"""Memory tools are only registered for persistent, Thread-owned contexts."""

from ..context_manager.agent_context import AgentContext
from .registry import Tool, ToolRegistry


def register_memory_tools(registry: ToolRegistry, context: AgentContext) -> None:
    path = {"type": "string", "description": "Relative Markdown path inside your private "
            "memory directory, e.g. index.md or topics/data-a.md. No absolute paths or escapes."}
    for name, description, properties, handler in [
        ("read_memory", "Read a private memory Markdown file (at most 32000 characters; "
         "index.md at most 8000). index.md has a pinned, unnumbered context baseline; "
         "read any memory file when its current contents are needed. Missing files return an error.",
         {"path": path}, context.read_memory),
        ("write_memory", "Create or replace a complete private memory Markdown file atomically. "
         "Prefer apply_memory_patch for focused edits. Read existing details first to preserve "
         "useful information. Does not modify user-owned "
         "instructions or replace the initial context snapshot. Successful writes remain in "
         "history with their replacement text and an explicit version receipt when index.md changes. "
         "Compaction may advance the pinned baseline. Returns an ordinary tool-result event; "
         "does not create timers or jobs.",
         {"path": path, "text": {"type": "string", "description": "Complete replacement UTF-8 "
          "text, at most 8000 characters for index.md, otherwise 32000. Keep the index short "
          "and link to detail files. Empty text clears the file."}}, context.write_memory),
        ("apply_memory_patch", "Edit private memory Markdown files using Codex apply_patch syntax. "
         "Paths are relative to this Thread's memory directory: use index.md, not memory/index.md. "
         "Begin with *** Begin Patch and end with *** End Patch. Supports Add File (lines start '+'), "
         "Update File (optional Move to), and Delete File. Use @@ or @@ context text, then lines "
         "starting ' ', '-' or '+', not unified-diff line numbers. *** End of File anchors a chunk "
         "at the end. Read actual contents first; supply focused, distinguishing context. "
         "Uses the same context matching and line-ending preservation as workspace apply_patch. "
         "No absolute, escaping, symlink or non-.md paths. index.md cannot be deleted or moved; "
         "clear it with write_memory if needed. Add and Move may overwrite destinations; inspect first. "
         "Each file write is atomic and enforces memory limits (index.md 8000 characters, "
         "others 32000). Multi-file patches are NOT atomic: earlier operations remain on failure "
         "and are reported; re-read before retrying. Maximum patch 1 MiB and 100 operations. "
         "The patch and receipt enter ordinary history without replacing initial context. "
         "Returns A/M/D summaries and, when index.md changed, its exact complete version "
         "even after a partially successful patch; other files are not repeated.",
         {"patch": {"type": "string", "description": "Complete Codex-format patch with actual "
          "newlines. Example: *** Begin Patch\n*** Update File: index.md\n@@\n-Old note\n+New note\n*** End Patch"}},
         context.apply_memory_patch),
    ]:
        registry.register(Tool({"type": "function", "name": name, "description": description,
            "strict": True, "parameters": {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}}, handler))
