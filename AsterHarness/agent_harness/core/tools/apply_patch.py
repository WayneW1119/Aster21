"""Tool schema/handler adapter; patch parsing and file IO live outside core."""

from pathlib import Path

from ...apply_patch import apply_patch, format_changes
from .registry import Tool, ToolRegistry


def register_apply_patch_tool(registry: ToolRegistry, workspace: Path, sandbox=None) -> None:
    root = workspace.resolve(strict=True)

    def handle(patch: str) -> str:
        if sandbox is not None and sandbox.enabled:
            return sandbox.file_operation("patch", patch=patch)
        changes = apply_patch(patch, root)
        return "Success. Updated the following files:\n" + format_changes(changes)

    registry.register(Tool({
        "type": "function", "name": "apply_patch", "strict": True,
        "description": "Edit UTF-8 workspace files using Codex patch syntax. Prefer this tool "
            "over shell/Python text rewrites for manual edits. This adapter is a JSON function: "
            "put the complete patch text in the patch string (not a shell command or Markdown fence). "
            "Begin with *** Begin Patch and end with *** End Patch. Operations: *** Add File: path "
            "(each content line starts '+'); *** Delete File: path; *** Update File: path "
            "(optional *** Move to: new_path, followed by chunks). Use @@ or @@ context text to "
            "start chunks, not unified-diff line numbers. Chunk lines start ' ' for unchanged "
            "context, '-' for removed lines, '+' for added lines. *** End of File anchors a chunk "
            "to the file end. Supply focused, distinguishing context from actual reads; do not "
            "guess old contents. Existing context is preserved, including line endings. "
            "Paths resolve from the Session workspace; absolute paths inside it are accepted. "
            "Outside-workspace and symlink paths are rejected. Add File and Move to can overwrite "
            "existing destinations; inspect them first. Deletes are nonrecursive. "
            "Maximum patch 1 MiB, 100 file operations, 8 MiB per updated file. A multi-file patch "
            "is NOT atomic: earlier changes remain on later failure. Re-read before retrying. "
            "Returns A/M/D summaries, not proof that code/tests or the business task succeeded.",
        "parameters": {"type": "object", "properties": {
            "patch": {"type": "string", "description": "Complete Codex-format patch, including Begin/End Patch markers and actual newlines. Example: *** Begin Patch\n*** Add File: note.md\n+Hello\n*** End Patch"}},
            "required": ["patch"], "additionalProperties": False},
    }, handle))
