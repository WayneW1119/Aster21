"""Dedicated skill reads and event-driven source management."""

import json
from .registry import Tool


def register_skill_tools(registry, service, request_change):
    def read(skill_id, resource='SKILL.md'):
        return json.dumps(service.read(skill_id, resource), ensure_ascii=False)

    async def load(source, expose_path=False):
        return json.dumps(await request_change(service.prepare_load(source, expose_path)), ensure_ascii=False)

    async def unload(skill_id):
        if skill_id not in service.scan()['entries']:
            raise ValueError('Unknown skill_id')
        return json.dumps(await request_change({'action': 'unload', 'skill_id': skill_id}), ensure_ascii=False)

    definitions = [
        ('read', read, 'Read an available skill by skill_id, without needing a filesystem path. '
         'resource defaults to SKILL.md; use package-relative paths for references. '
         'Dedicated skill reads bypass Agent filesystem permissions, within this skill package only. '
         'Reading does not execute scripts or grant permissions.',
         {'skill_id': {'type': 'string', 'description': 'Skill name from the available catalog.'},
          'resource': {'type': 'string', 'description': 'Package-relative text path; defaults to SKILL.md.'}}, ['skill_id']),
        ('load', load, 'Load skills from a host directory or SKILL.md path at runtime. '
         'Runtime sources and their resources require ordinary filesystem read access. '
         'expose_path defaults to false; true includes filesystem paths in the catalog. '
         'Returns accepted; activation occurs at the next Step boundary with an applied/failed event. '
         'Sources survive resume. Loading does not execute code or grant filesystem access.',
         {'source': {'type': 'string', 'description': 'Host directory or SKILL.md path, relative to workspace or absolute.'},
          'expose_path': {'type': 'boolean', 'description': 'Whether to publish source file paths; defaults to false.'}}, ['source']),
        ('unload', unload, 'Remove a skill from this Agent catalog without deleting its files. '
         'Returns accepted; removal occurs at the next Step boundary with an applied/failed event. '
         'Removal survives resume. Loading its source again makes it available again.',
         {'skill_id': {'type': 'string', 'description': 'Skill name to remove from the available catalog.'}}, ['skill_id'])]
    for name, handler, description, properties, required in definitions:
        registry.register(Tool({'type': 'function', 'name': name, 'description': description,
            'strict': False, 'parameters': {'type': 'object', 'properties': properties,
            'required': required, 'additionalProperties': False}}, handler),
            namespace='skills', namespace_description='Discover and read reusable task guides.')
