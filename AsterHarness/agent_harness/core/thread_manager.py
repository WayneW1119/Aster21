"""Create/resume persistent Threads; Session owns live scheduling and resources."""

from dataclasses import asdict
from pathlib import Path

from .context_manager.agent_context import AgentContext
from .rollout import RolloutRecorder
from .session import Session, SessionConfiguration
from .tools.handlers import build_file_tools


class ThreadManager:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def create(self, client, workspace: Path, configuration: SessionConfiguration | None = None,
               agent_instructions: str | None = None,
               **kwargs) -> Session:
        configuration = configuration or SessionConfiguration()
        if agent_instructions is not None and (not isinstance(agent_instructions, str)
                or len(agent_instructions) > AgentContext.FILE_LIMIT):
            raise ValueError("Agent instructions must be a string of at most 32000 characters")
        saved = asdict(configuration)
        # Keep non-MCP Thread metadata byte-for-byte compatible with the earlier
        # runtime contract. Old Threads also resume with an empty MCP config.
        if saved["filesystem_policy"] is None:
            saved.pop("filesystem_policy")
        if saved["workspace_skills"]:
            saved.pop("workspace_skills")
        if not saved["skill_sources"]:
            saved.pop("skill_sources")
        if not saved["skill_directories"]:
            saved.pop("skill_directories")
        if not saved["code_mode_enabled"]:
            saved.pop("code_mode_enabled")
        if not saved["mcp_servers"]:
            saved.pop("mcp_servers")
        if saved["tool_output_token_limit"] == 10000:
            saved.pop("tool_output_token_limit")  # Legacy default metadata stays compatible.
        recorder, meta = RolloutRecorder.create(self.root, workspace, saved)
        try:
            context = AgentContext(recorder.path.parent)
            context.initialize(agent_instructions)
            return Session(client, build_file_tools(Path(meta["cwd"])), configuration=configuration,
                           workspace=Path(meta["cwd"]), rollout=recorder,
                           thread_id=meta["thread_id"], agent_context=context, **kwargs)
        except BaseException:
            recorder.close()
            raise

    def resume(self, client, thread_id: str, *, configuration_override: SessionConfiguration | None = None,
               configuration_from_meta=None, **kwargs) -> Session:
        recorder, history = RolloutRecorder.resume(self.root, thread_id)
        try:
            if configuration_from_meta is not None:
                configuration_override = configuration_from_meta(history.meta)
            workspace = Path(history.meta["cwd"]).resolve()
            context = AgentContext(recorder.path.parent)
            context.initialize()  # Add context files to legacy Threads without replacing anything.
            session = Session(client, build_file_tools(workspace),
                           configuration=configuration_override or SessionConfiguration(**history.meta["configuration"]),
                           workspace=workspace, rollout=recorder, thread_id=thread_id,
                           agent_context=context,
                           resumed_history=history, **kwargs)
            if configuration_override is not None:
                recorder.append("session_configuration", {"configuration": asdict(configuration_override)})
            return session
        except BaseException:
            recorder.close()
            raise

    def list_threads(self) -> list[dict]:
        return RolloutRecorder.list_threads(self.root)
