from dataclasses import dataclass
from typing import ClassVar

from ..unified_exec.process import UnifiedExecProcess
from .types import Emit


@dataclass(frozen=True)
class CommandCompletionGenerator:
    session_id: int
    process: UnifiedExecProcess
    kind: ClassVar[str] = "command_completion"

    def describe(self) -> dict:
        return {"session_id": self.session_id}

    async def run(self, emit: Emit) -> None:
        # Keep this reference even if write_stdin collects/removes the process handle.
        # Do not call collect(): notification and output consumption are independent.
        await self.process.complete.wait()
        await self.process.monitor
        await emit("command_completed", {"session_id": self.session_id,
                   "exit_code": self.process.process.returncode,
                   "note": "Completion notification only. Use write_stdin for uncollected "
                           "output unless it has already returned the final result."})
