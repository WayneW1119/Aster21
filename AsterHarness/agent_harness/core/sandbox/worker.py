"""Trusted filesystem worker; file IO always occurs inside the OS sandbox."""

import base64
from itertools import islice
import json
import importlib.util
from pathlib import Path
import stat
import sys

# Load this exact trusted package without placing the writable project root
# ahead of stdlib imports (e.g. a workspace dataclasses.py must not shadow it).
package_root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "agent_harness",
    package_root / "__init__.py",
    submodule_search_locations=[str(package_root)],
)
package = importlib.util.module_from_spec(spec)
sys.modules["agent_harness"] = package
spec.loader.exec_module(package)


def perform(request):
    root = Path(request["workspace"])
    operation = request["operation"]
    if operation == "skills":
        from agent_harness.core.skills import scan

        return scan(request["roots"])
    if operation == "patch":
        from agent_harness.apply_patch import apply_patch, format_changes

        return "Success. Updated the following files:\n" + format_changes(
            apply_patch(request["patch"], root)
        )
    path = root / request["path"]
    if operation == "list":
        entries = list(islice(path.iterdir(), 101))
        lines = sorted(p.name + ("/" if p.is_dir() else "") for p in entries)
        return (
            "\n".join(lines[:100])
            + ("\n[more entries omitted]" if len(entries) > 100 else "")
        )[:12000]
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Use a regular file")
    if operation == "bytes":
        with path.open("rb") as stream:
            data = stream.read(request["limit"] + 1)
        if len(data) > request["limit"]:
            raise ValueError("File exceeds read limit")
        return base64.b64encode(data).decode()
    if operation == "read":
        first, last = request["start_line"], request["end_line"]
        if (
            type(first) is not int
            or type(last) is not int
            or not 1 <= first <= last
            or last - first + 1 > 200
        ):
            raise ValueError("Read 1–200 lines using positive inclusive line numbers")
        chunks = []
        remaining = 12000
        with path.open(encoding="utf-8") as stream:
            for number in range(1, last + 1):
                line = stream.readline(12001)
                if not line:
                    break
                if number >= first:
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
    raise ValueError("Unknown filesystem operation")


try:
    print(
        json.dumps(
            {"ok": True, "value": perform(json.load(sys.stdin))}, ensure_ascii=False
        )
    )
except Exception as error:
    print(
        json.dumps(
            {"ok": False, "error": type(error).__name__ + ": " + str(error)},
            ensure_ascii=False,
        )
    )
