"""Small Codex-style patch engine, independent of the model API and event loop.

Adapted from codex-rs/apply-patch (Apache-2.0); see third_party/codex/LICENSE.
"""

from dataclasses import dataclass
from pathlib import Path

from .file_update import derive_new_contents
from .parser import parse_patch


MAX_FILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FileChange:
    kind: str
    path: str
    move_path: str | None = None


def _resolve(root: Path, name: str) -> Path:
    candidate = root / name
    target = candidate.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError(f"Patch path must stay inside the workspace: {name}")
    # No symlink redirection, including aliases that happen to point inside root.
    if candidate.is_symlink() or any(p.is_symlink() for p in candidate.parents
                                    if p.is_relative_to(root)):
        raise ValueError(f"Patch paths must not contain symlinks: {name}")
    return target


def apply_patch(patch: str, cwd: Path) -> list[FileChange]:
    hunks = parse_patch(patch)  # Parse the complete patch before the first write.
    root = cwd.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Patch workspace must be a directory")
    changes = []
    for hunk in hunks:
        try:
            path = _resolve(root, hunk.path)
            if hunk.kind == "delete":
                path.unlink()  # Never recursive; missing files and directories fail.
                changes.append(FileChange("D", hunk.path))
                continue
            destination = _resolve(root, hunk.move_path) if hunk.move_path else path
            if hunk.kind == "add":
                contents = hunk.contents
            else:
                with path.open("rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError(f"File exceeds 8 MiB update limit: {hunk.path}")
                original = data.decode("utf-8")
                if "\x00" in original:
                    raise ValueError(f"Cannot update binary file: {hunk.path}")
                contents = derive_new_contents(original, hunk.chunks, hunk.path)
            if "\x00" in contents:
                raise ValueError(f"Cannot write binary content: {hunk.path}")
            encoded = contents.encode("utf-8")
            if len(encoded) > MAX_FILE_BYTES:
                raise ValueError(f"Updated file exceeds 8 MiB limit: {hunk.path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(encoded)
            # Record a successful destination write even if subsequent source
            # removal fails. Multi-file patches are not a filesystem transaction.
            if destination != path:
                changes.append(FileChange("M", hunk.path, hunk.move_path))
                path.unlink()
            else:
                changes.append(FileChange("A" if hunk.kind == "add" else "M", hunk.path))
        except (OSError, ValueError) as exc:
            completed = format_changes(changes) if changes else "No earlier file operations completed."
            raise ValueError(f"Patch failed for {hunk.path}: {exc}\n{completed}\n"
                             "Earlier changes are kept; a failed write may also have modified its "
                             "target. Re-read affected files before retrying.") from exc
    return changes


def format_changes(changes: list[FileChange]) -> str:
    return "\n".join(f"{c.kind} {c.path}" + (f" -> {c.move_path}" if c.move_path else "")
                     for c in changes)
