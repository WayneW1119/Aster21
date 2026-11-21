"""Minimal dotenv reader: no shell execution or variable expansion."""

import os
import shlex
from pathlib import Path


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    allowed = {"HARNESS_MODEL", "HARNESS_RESPONSES_URL", "OPENAI_API_KEY"}
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw = line.partition("=")
        name = name.strip()
        if name not in allowed:
            continue
        try:
            tokens = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            raise ValueError(f"Invalid dotenv syntax on line {number}") from None
        if not separator or len(tokens) > 1:
            raise ValueError(f"Invalid dotenv syntax on line {number}")
        values[name] = tokens[0] if tokens else ""
    # Explicit process environment overrides .env values.
    for name, value in values.items():
        os.environ.setdefault(name, value)
