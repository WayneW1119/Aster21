"""Compose built-in tool bindings in their stable, model-visible order."""

from typing import TYPE_CHECKING

from ..tools.apply_patch import register_apply_patch_tool
from ..tools.compact import register_compact_tools
from ..tools.control import register_control_tools
from ..tools.event_generators import register_generator_tools
from ..tools.history import register_history_tools
from ..tools.memory import register_memory_tools
from ..tools.registry import ToolRegistry
from ..tools.router import ToolRouter
from ..tools.unified_exec import register_exec_tools
from ..tools.custom import register_custom_management
from ..tools.images import register_image_tools
from ..tools.output import register_output_tools
from ..tools.skills import register_skill_tools

if TYPE_CHECKING:
    from .session import Session


def build_tool_router(session: "Session", registry: ToolRegistry) -> ToolRouter:
    tools = registry.copy()
    if session.agent_context is not None:
        register_memory_tools(tools, session.agent_context)
    for name, operation in (("read_file", "read"), ("list_directory", "list")):
        try:
            tool = tools.get_tool(name)
        except KeyError:
            continue  # SDK hosts may supply a registry without file tools.
        def bind(op, original):
            return lambda **kwargs: (session.sandbox.file_operation(op, **kwargs)
                                     if session.sandbox.enabled else original(**kwargs))
        tool.handler = bind(operation, tool.handler)
        if session.sandbox.enabled:
            tool.spec['parameters']['properties']['path']['description'] = (
                'Path relative to the workspace or absolute path; effective filesystem permissions apply.')
    register_apply_patch_tool(tools, session.process_manager.workspace, session.sandbox)
    register_exec_tools(tools, session.process_manager, session.generator_manager)
    register_generator_tools(tools, session.generator_manager, session.process_manager.workspace)
    register_control_tools(tools, session._accept_message)
    register_history_tools(tools, session.state.history, session.thread_id or session.session_id,
                           session.history_navigation)
    register_compact_tools(tools, session.state.history, session.compact_history,
                           session.history_navigation)
    register_custom_management(tools, session.custom_tools, session.request_custom_tool_change)
    register_skill_tools(tools, session.skill_service, session.request_skill_change)
    register_image_tools(tools, session.images)
    register_output_tools(tools, session.tool_outputs, lambda: (
        session.tool_runtime.current_invocation.step.settings.tool_output_token_limit
        if session.tool_runtime.current_invocation is not None else session.configuration.tool_output_token_limit))
    if session.services.code_mode is not None:
        from ..tools.code_mode import register_code_mode_tools
        register_code_mode_tools(tools, session.services.code_mode)
    return ToolRouter(tools)
