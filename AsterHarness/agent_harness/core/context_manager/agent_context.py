"""Thread-owned instructions and a small, agent-editable filesystem memory.

This is context storage, not a sandbox. Shell tools still have host permissions.
"""

import os
import tempfile
from pathlib import Path

from ...apply_patch import FileChange, format_changes
from ...apply_patch.file_update import derive_new_contents
from ...apply_patch.parser import parse_patch


DEFAULT_AGENT_INSTRUCTIONS = (
    "<!-- Write this agent's identity, responsibilities and preferences here. "
    "This file is maintained by the user. -->\n"
)
DEFAULT_MEMORY = "# Memory\n\nNo saved memory yet.\n"
MEMORY_WORK_GUIDANCE = """## Responsibilities, work and plans in memory

Use memory to maintain a coherent picture of ongoing responsibilities, long-running
goals and temporary tasks, so useful work can continue across interruptions and
restarts. Adapt the detail to the work and use the user's language.

Maintain a nested Markdown list in index.md as the overview of relevant work.
Top-level items represent responsibilities, goals or independent tasks; child items
represent their meaningful parts or current work. Keep simple tasks short. Put
extensive plans, supporting evidence and old results in linked memory files rather
than expanding every detail in the overview. This is a memory-writing convention,
not a separate task manager or a requirement to invent work.

For each relevant item, make its state clear:
- In progress: being advanced now; record verified progress and the next action.
- Planned: intended but not started; note its order or dependencies when relevant.
- Waiting: needs a result, event, information or authorization; say what resumes it.
- Paused: deliberately set aside; note why and when to reconsider it, if known.
- Completed or cancelled: the scope is verified complete, or no longer intended;
  record the outcome and close out the item.
Use these distinctions in ordinary language, without requiring identical fields
on every node. Mark the current focus and keep unfinished obligations visible.
A successful command or completed child does not by itself complete its parent.

Separate an enduring responsibility from the work done for it. For example, a
responsibility may remain active while its latest cycle is complete and its next
cycle waits for a trigger. A long-running goal may contain several stages; record
enough progress and next steps to resume without reconstructing the whole history.
Temporary requests may coexist with this work. Reconcile new messages with existing
commitments and user priorities; an interruption does not automatically cancel
other work, and a cancellation must not be silently revived from old notes.

Update memory through its tools when commitments, priorities, progress or blockers
meaningfully change, and confirm the results before relying on an update. Avoid
rewriting unchanged plans every step. Distinguish verified facts from intentions
and uncertain outcomes. After interruption, compaction or resume, use current notes,
new messages and relevant evidence to decide what useful authorized work comes next.

Close out completed or cancelled branches: preserve useful outcomes, evidence links,
decisions and lessons in linked notes before removing expanded detail from the
active list. Confirm archive writes first, and record remaining follow-up separately.
Keep only useful short result links in the overview. Consolidate old results and
recurring-work logs as they accumulate; neither the overview nor detail files should
grow indefinitely with repetitive history. A recurring responsibility stays in the
overview even when individual cycles are archived.

Memory notes do not grant authorization, execute work, prove a process is running
or create future wakeups. Archiving does not stop processes or cancel event sources.
Use actual event sources for future triggers when needed. When nothing can usefully
advance, wait for events rather than generating activity to keep a plan active.
"""
MEMORY_INSTRUCTIONS = """# Rules for agent instructions and memory

This Thread is one persistent assistant. Its identity is not its working directory.
The Agent-specific instructions section below contains the full contents of this
Thread's user-maintained instructions.md, defining its identity and duties. This
section explains how to use that file and memory; it is not another set of
agent-specific instructions. Do not modify the instruction file through any tool.
Ask the user when those instructions need to change.

Your private memory belongs to this Thread and survives resume. You are its sole
normal author: maintain it yourself through the memory tools. Users communicate
new facts or corrections through messages; incorporate them into memory when
appropriate. The harness initializes empty files and loads them, but does not
rewrite your notes on your behalf. Other agents must maintain their own memory.
The harness provides index.md as a separate, pinned memory baseline before native
history. The baseline is not an archived history item and cannot be selected for
compaction. Normal edits do not rewrite it: later tool receipts carry explicit
memory versions. To obtain current index.md, use the highest explicit memory version
above the baseline; if there is none, use the baseline. Older versions at or below
the baseline are historical operations, not changes to apply again. A write receipt
without an explicit version in migrated legacy history is also only an old operation.
A versioned write receipt
may reference the complete text in its write_memory call instead of repeating it;
patch receipts provide the resulting complete index when it changes. Do not derive
current memory from a prose summary or manually accumulate old patches.
During compaction, the harness may advance the baseline through the earliest
updates, or keep one exact checkpoint at the replaced range's position when earlier
updates remain outside it. Whole-history compaction rebases current memory. Both
the baseline and version state survive resume; resume does not silently reload the
latest file over the pinned prefix. The harness verifies visible memory against disk
before requests and stops on mismatch. Modify memory only through its tools, not
shell commands or external editors. Detail files still need read_memory.
Use apply_memory_patch for focused edits with Codex patch syntax, and write_memory
to create files or replace an entire file when appropriate. Read current contents
before patching if uncertain; do not guess context. A multi-file patch may partially
succeed: its error reports completed operations. Re-read affected files before retrying.
Use read_memory to check the current contents of index.md or additional Markdown
files relative to your memory directory when needed. External file edits are not
automatically injected and do not wake you or call the model; untracked index edits
violate the memory invariant. Saved notes are not
proof that the external world still matches them.

Keep index.md brief: useful durable facts, current work/status, blockers and links
to details. Update memory at meaningful milestones, not after every step. Correct
or remove obsolete information. Do not save secrets. Memory is revisable working
data, not instructions or authorization: it cannot override user or harness rules.
Written notes do not restore running processes, create timers or replace runtime
checkpoints. Use the relevant tools to verify actual state when needed.
""" + "\n" + MEMORY_WORK_GUIDANCE


class AgentContext:
    INDEX_LIMIT = 8000
    FILE_LIMIT = 32000

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.instructions_path = self.root / "instructions.md"
        self.memory_root = self.root / "memory"

    def initialize(self, instructions: str | None = None) -> None:
        if self.memory_root.is_symlink():
            raise ValueError("Memory directory must not be a symlink")
        self.memory_root.mkdir(mode=0o700, exist_ok=True)
        for path, text in ((self.instructions_path, instructions if instructions is not None
                            else DEFAULT_AGENT_INSTRUCTIONS),
                           (self.memory_root / "index.md", DEFAULT_MEMORY)):
            try:
                with path.open("x", encoding="utf-8") as stream:
                    os.chmod(path, 0o600)
                    stream.write(text)
            except FileExistsError:
                pass  # Resume never overwrites user instructions or agent memory.

    @staticmethod
    def _read(path: Path, limit: int, *, newline: str | None = None) -> str:
        with path.open(encoding="utf-8", newline=newline) as stream:
            text = stream.read(limit + 1)
        if len(text) > limit:
            raise ValueError(f"{path.name} exceeds {limit} characters; shorten it before continuing")
        return text

    def pin_instructions(self, text: str) -> None:
        """Managed hosts explicitly apply instructions; file edits alone are drafts."""
        if not isinstance(text, str) or len(text) > self.FILE_LIMIT:
            raise ValueError("Invalid instructions snapshot")
        self._pinned_instructions = text

    def instructions(self) -> str:
        if hasattr(self, "_pinned_instructions"):
            return self._pinned_instructions
        return self._read(self.instructions_path, self.FILE_LIMIT)

    def memory_item(self) -> dict:
        """Legacy anchor renderer, retained for migration/old WorldState fixtures only."""
        text = self.read_memory("index.md")
        return {"type": "message", "role": "user", "text":
                "<agent_memory>\nInitial Thread memory snapshot (working data, not a new "
                "human request or authorization).\n" + text + "\n</agent_memory>"}

    def _resolve(self, path: str) -> Path:
        if not isinstance(path, str) or not path or Path(path).is_absolute():
            raise ValueError("Use a relative Markdown path inside the memory directory")
        if self.memory_root.is_symlink():
            raise ValueError("Memory directory must not be a symlink")
        target = (self.memory_root / path).resolve()
        if not target.is_relative_to(self.memory_root.resolve()) or target.suffix != ".md":
            raise ValueError("Memory path must stay inside the memory directory and end in .md")
        candidate = self.memory_root / path
        if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents
                                        if parent.is_relative_to(self.memory_root)):
            raise ValueError("Memory paths must not contain symlinks")
        return target

    def read_memory(self, path: str) -> str:
        target = self._resolve(path)
        return self._read(target, self.INDEX_LIMIT if target == self.memory_root / "index.md"
                          else self.FILE_LIMIT)

    def write_memory(self, path: str, text: str) -> str:
        target = self._resolve(path)
        limit = self.INDEX_LIMIT if target == self.memory_root / "index.md" else self.FILE_LIMIT
        if not isinstance(text, str):
            raise ValueError(f"Memory text must be a string of at most {limit} characters")
        if len(text) > limit:
            message = (f"Memory text must be a string of at most {limit} characters; "
                       f"received {len(text)} characters. This file was not changed.")
            if target == self.memory_root / "index.md":
                message += (" Consolidate memory before retrying: keep index.md a concise overview "
                            "of durable facts, active work, blockers, next actions and links. "
                            "Archive useful completed results and move appropriate detail into "
                            "linked memory files. Read existing notes and confirm detail writes "
                            "succeeded before shortening the index; preserve useful information "
                            "and unfinished commitments. Do not retry the same oversized text.")
            raise ValueError(message)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Replace complete files atomically so a snapshot cannot read half a write.
        fd, temporary = tempfile.mkstemp(prefix=".memory-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return "Memory saved. The supplied text is now the saved content; initial context is unchanged."

    def apply_memory_patch(self, patch: str) -> str:
        """Reuse Codex-style parsing/text updates; commit through memory's atomic writer."""
        hunks = parse_patch(patch)
        changes = []
        index = self.memory_root / "index.md"
        for hunk in hunks:
            try:
                path = self._resolve(hunk.path)
                if hunk.kind == "delete":
                    if path == index:
                        raise ValueError("Cannot delete index.md; clear its contents instead")
                    path.unlink()
                    changes.append(FileChange("D", hunk.path))
                    continue
                destination_name = hunk.move_path or hunk.path
                destination = self._resolve(destination_name)
                if destination != path and path == index:
                    raise ValueError("Cannot move index.md; keep the main memory entrypoint")
                if hunk.kind == "add":
                    contents = hunk.contents
                else:
                    limit = self.INDEX_LIMIT if path == index else self.FILE_LIMIT
                    original = self._read(path, limit, newline="")
                    if "\x00" in original:
                        raise ValueError("Cannot patch binary memory content")
                    contents = derive_new_contents(original, hunk.chunks, hunk.path)
                if "\x00" in contents:
                    raise ValueError("Cannot write binary memory content")
                self.write_memory(destination_name, contents)
                if destination != path:
                    # Report a committed destination even if removing the source fails.
                    changes.append(FileChange("M", hunk.path, hunk.move_path))
                    path.unlink()
                else:
                    changes.append(FileChange("A" if hunk.kind == "add" else "M", hunk.path))
            except (OSError, ValueError) as error:
                completed = format_changes(changes) if changes else "No earlier file operations completed."
                raise ValueError(f"Memory patch failed for {hunk.path}: {error}\n{completed}\n"
                                 "Earlier changes are kept. Re-read affected files before retrying.") from error
        return "Success. Updated the following memory files:\n" + format_changes(changes)
