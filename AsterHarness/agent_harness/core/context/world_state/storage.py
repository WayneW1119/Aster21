"""Validation for generic section checkpoints, independent of model rendering."""

from copy import deepcopy


def validate_sections(sections):
    if not isinstance(sections, dict) or not sections:
        raise ValueError("World-state compaction requires section states")
    for name, value in sections.items():
        if (not isinstance(name, str) or not name or not isinstance(value, dict)
                or set(value) != {"state", "checkpoints"} or not isinstance(value["state"], dict)
                or not isinstance(value["checkpoints"], list)):
            raise ValueError("Invalid world-state section checkpoint")
        for item in value["checkpoints"]:
            if (not isinstance(item, dict) or item.get("type") != "message"
                    or item.get("role") not in {"user", "developer"}
                    or not isinstance(item.get("text"), str)):
                raise ValueError("Invalid world-state positional checkpoint")
        # Preserve validation strength for known historical formats. Unknown
        # sections validate their own schema when attached before a model request.
        if name == "memory":
            from ...context_manager.memory import MemoryProjection, evidence
            MemoryProjection.from_dict(value["state"])
            evidence(value["checkpoints"])
        elif name == "environment":
            from .environment import checked, environment_item
            state=value['state']
            if set(state)!={'version','baseline','current'} or type(state['version']) is not int or state['version']!=1:
                raise ValueError('Invalid environment checkpoint')
            checked(state['baseline']);checked(state['current'])
            for item in value['checkpoints']:
                if '_environment_state' not in item or item!=environment_item(item['_environment_state']):
                    raise ValueError('Invalid environment positional checkpoint')
        elif name == "skills":
            from .skills import SkillsState
            section = SkillsState((), initialize=False)
            section.restore(value['state'])
            for item in value['checkpoints']:
                if '_skills_state' not in item:
                    raise ValueError('Invalid skills positional checkpoint')
            section.reconstruct(value['checkpoints'])
        elif name == "tools":
            from ...context_manager.mcp import McpProjection
            McpProjection.from_dict(value["state"])
    return deepcopy(sections)


def checkpoints(sections):
    return [deepcopy(item) for value in sections.values() for item in value["checkpoints"]]
