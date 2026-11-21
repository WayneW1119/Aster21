"""Locate chunks and derive new text, preserving original context and line endings.

Adapted from codex-rs/apply-patch (Apache-2.0); see third_party/codex/LICENSE.
"""

import re

from .parser import UpdateFileChunk


_PUNCTUATION = str.maketrans({
    **{c: "-" for c in "‐‑‒–—―−"},
    **{c: "'" for c in "‘’‚‛"},
    **{c: '"' for c in "“”„‟"},
    **{c: " " for c in "\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"},
})


def seek_sequence(lines: list[str], pattern: list[str], start: int,
                  eof: bool = False) -> int | None:
    if not pattern:
        return start
    last = len(lines) - len(pattern)
    first = max(start, last) if eof else start
    for normalize in (lambda s: s, str.rstrip, str.strip,
                      lambda s: s.strip().translate(_PUNCTUATION)):
        expected = [normalize(line) for line in pattern]
        for index in range(first, last + 1):
            if [normalize(line) for line in lines[index:index + len(pattern)]] == expected:
                return index
    return None


def derive_new_contents(original: str, chunks: list[UpdateFileChunk], path: str) -> str:
    # Retain each physical line's own terminator; unlike splitlines this does not
    # split Unicode punctuation/control characters which may be ordinary content.
    source = [(m[1], m[2]) for m in re.finditer(r"([^\r\n]*)(\r\n|\r|\n|$)", original)
              if m[0]]
    lines = [text for text, _ in source]
    ending = next((end for _, end in source if end), "\n")
    replacements = []
    cursor = 0
    for chunk in chunks:
        if chunk.change_context is not None:
            found = seek_sequence(lines, [chunk.change_context], cursor)
            if found is None:
                raise ValueError(f"Failed to find context '{chunk.change_context}' in {path}")
            cursor = found + 1
        if not chunk.old_lines:
            replacements.append((len(lines), 0, chunk.new_lines))
            continue
        old, new = chunk.old_lines, chunk.new_lines
        found = seek_sequence(lines, old, cursor, chunk.is_end_of_file)
        if found is None and old[-1] == "":
            old = old[:-1]
            new = new[:-1] if new and new[-1] == "" else new
            found = seek_sequence(lines, old, cursor, chunk.is_end_of_file)
        if found is None:
            expected = "\n".join(chunk.old_lines)
            raise ValueError(f"Failed to find expected lines in {path}:\n{expected}")
        old_start = new_start = 0
        for old_context, new_context in chunk.context_line_indices:
            if old_context >= len(old) or new_context >= len(new):
                break
            if old_start != old_context or new_start != new_context:
                replacements.append((found + old_start, old_context - old_start,
                                     new[new_start:new_context]))
            old_start, new_start = old_context + 1, new_context + 1
        if old_start != len(old) or new_start != len(new):
            replacements.append((found + old_start, len(old) - old_start, new[new_start:]))
        cursor = found + len(old)
    result = []
    cursor = 0
    for start, removed, added in sorted(replacements, key=lambda r: r[0]):
        if start < cursor:
            raise ValueError(f"Overlapping update chunks in {path}")
        result.extend(source[cursor:start])
        result.extend((line, ending) for line in added)
        cursor = start + removed
    result.extend(source[cursor:])
    return "".join(text + (end or ending) for text, end in result)
