"""Parse Codex-style patches independently of tools, transport and filesystem IO.

Adapted from codex-rs/apply-patch (Apache-2.0); see third_party/codex/LICENSE.
"""

from dataclasses import dataclass, field


MAX_PATCH_BYTES = 1024 * 1024
MAX_FILES = 100


@dataclass
class UpdateFileChunk:
    change_context: str | None = None
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    context_line_indices: list[tuple[int, int]] = field(default_factory=list)
    is_end_of_file: bool = False


@dataclass
class Hunk:
    kind: str
    path: str
    contents: str = ""
    move_path: str | None = None
    chunks: list[UpdateFileChunk] = field(default_factory=list)


def parse_patch(patch: str) -> list[Hunk]:
    if not isinstance(patch, str) or len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise ValueError("patch must be a string of at most 1 MiB")
    lines = [line.removesuffix("\r") for line in patch.strip().split("\n")]
    if (len(lines) >= 4 and lines[0] in ("<<EOF", "<<'EOF'", '<<"EOF"')
            and lines[-1].strip() == "EOF"):
        lines = lines[1:-1]
    if (len(lines) < 2 or lines[0].strip() != "*** Begin Patch"
            or lines[-1].strip() != "*** End Patch"):
        raise ValueError("Invalid patch: expected *** Begin Patch and *** End Patch boundaries")
    hunks = []
    index = 1

    def fail(message, line):
        raise ValueError(f"Invalid hunk at line {line + 1}: {message}")

    def path_after(line, marker):
        path = line[len(marker):].strip()
        if not path or "\x00" in path or len(path) > 4096:
            fail("expected a nonempty file path of at most 4096 characters", index)
        return path

    while index < len(lines) - 1:
        header = lines[index].strip()
        if not header:
            index += 1
            continue
        if len(hunks) >= MAX_FILES:
            fail("patch may contain at most 100 file operations", index)
        if header.startswith("*** Add File: "):
            hunk = Hunk("add", path_after(header, "*** Add File: "))
        elif header.startswith("*** Delete File: "):
            hunk = Hunk("delete", path_after(header, "*** Delete File: "))
        elif header.startswith("*** Update File: "):
            hunk = Hunk("update", path_after(header, "*** Update File: "))
        else:
            fail("expected Add File, Delete File or Update File", index)
        hunks.append(hunk)
        index += 1
        if hunk.kind == "delete":
            continue
        if hunk.kind == "add":
            added = []
            while index < len(lines) - 1 and not lines[index].strip().startswith("*** "):
                if not lines[index].startswith("+"):
                    fail("every added line must start with '+'", index)
                added.append(lines[index][1:])
                index += 1
            hunk.contents = "".join(line + "\n" for line in added)
            continue
        if index < len(lines) - 1 and lines[index].strip().startswith("*** Move to: "):
            hunk.move_path = path_after(lines[index].strip(), "*** Move to: ")
            index += 1
        chunk = None
        while index < len(lines) - 1:
            raw, marker = lines[index], lines[index].strip()
            if marker == "*** End of File":
                if chunk is None or not (chunk.old_lines or chunk.new_lines) or chunk.is_end_of_file:
                    fail("End of File must follow a nonempty update chunk", index)
                chunk.is_end_of_file = True
            elif marker.startswith("*** "):
                break
            elif marker == "@@" or marker.startswith("@@ "):
                if chunk is not None and not (chunk.old_lines or chunk.new_lines):
                    fail("update chunk does not contain any lines", index)
                chunk = UpdateFileChunk(change_context=marker[3:] if marker != "@@" else None)
                hunk.chunks.append(chunk)
            else:
                if chunk is not None and chunk.is_end_of_file:
                    if not raw:
                        index += 1
                        continue
                    fail("expected @@ after End of File", index)
                if raw and raw[0] not in " +-":
                    fail("update lines must start with ' ', '+' or '-'; @@ headers have no line numbers", index)
                if chunk is None:
                    chunk = UpdateFileChunk()
                    hunk.chunks.append(chunk)
                prefix, text = (raw[0], raw[1:]) if raw else (" ", "")
                if prefix == " ":
                    chunk.context_line_indices.append((len(chunk.old_lines), len(chunk.new_lines)))
                    chunk.old_lines.append(text)
                    chunk.new_lines.append(text)
                elif prefix == "-":
                    chunk.old_lines.append(text)
                else:
                    chunk.new_lines.append(text)
            index += 1
        if not hunk.chunks or any(not (c.old_lines or c.new_lines) for c in hunk.chunks):
            fail(f"Update File hunk for '{hunk.path}' is empty", index)
    if not hunks:
        raise ValueError("Invalid patch: no files were modified")
    return hunks
