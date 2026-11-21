"""Manager-launched example: emit when one file's metadata changes.

register_program_generator(script_path="examples/watch_file.py",
                           args=["inbox/report.csv"])
Paths are relative to the Session workspace, not to this script.
Polling coalesces changes; this is not a lossless filesystem change journal.
"""

import json
import os
from pathlib import Path
import sys
import time

from aster.events import emit_event, state_directory


def main():
    target = Path(sys.argv[1]).resolve()
    saved = state_directory() / "cursor.json"
    previous = json.loads(saved.read_text()) if saved.exists() else None
    while True:
        try:
            info = target.stat()
            current = {"path": str(target), "exists": True,
                       "mtime_ns": info.st_mtime_ns, "size": info.st_size}
        except FileNotFoundError:
            current = {"path": str(target), "exists": False}
        if current != previous:
            result = emit_event("watched_file_changed", current)
            if result["status"] == "accepted":
                temporary = saved.with_suffix(".tmp")
                with temporary.open("w") as stream:
                    json.dump(current, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, saved)
                previous = current
            # Rejected data is not acknowledged locally: retry after backing off.
        time.sleep(5)


if __name__ == "__main__":
    main()
