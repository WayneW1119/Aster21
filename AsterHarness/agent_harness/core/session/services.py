"""Session-owned runtime services, following Codex's SessionServices boundary.

Services own external resources, not history or scheduling. Keep startup, source
pause/close and process cleanup separate: their order is part of our suspend and
close-drain behavior. This is deliberately not a new lifecycle policy.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..code_mode.runtime import CodeModeRuntime

from ..event_generators import EventGeneratorManager
from ..unified_exec import UnifiedExecProcessManager
from ..mcp import McpConnectionManager


@dataclass(frozen=True)
class SessionServices:
    generator_manager: EventGeneratorManager
    process_manager: UnifiedExecProcessManager
    mcp: McpConnectionManager
    code_mode: "CodeModeRuntime | None" = None

    async def start_restored(self) -> None:
        await self.generator_manager.start_restored()

    async def stop_event_sources(self, *, preserve: bool) -> None:
        if preserve:
            await self.generator_manager.pause()
        else:
            await self.generator_manager.close()

    async def close_processes(self) -> None:
        try:
            if self.code_mode is not None:
                await self.code_mode.close()
            await self.process_manager.close()
        finally:
            await self.mcp.close()
