"""Codex's generic MCP resource tools; defaults copied from mcp_resource_spec.rs."""

import json
from .registry import Tool
from ..mcp.client import McpCallError


RESOURCE_SPECS = [{'type': 'function',
  'name': 'list_mcp_resources',
  'description': 'Lists resources provided by MCP servers. Resources allow servers to share data '
                 'that provides context to language models, such as files, database schemas, or '
                 'application-specific information. Prefer resources over web search when '
                 'possible.',
  'strict': False,
  'parameters': {'type': 'object',
                 'properties': {'cursor': {'type': 'string',
                                           'description': 'Opaque cursor from a previous '
                                                          'list_mcp_resources call; omit for the '
                                                          'first page.'},
                                'server': {'type': 'string',
                                           'description': 'MCP server name. Omit to list resources '
                                                          'from every configured server.'}},
                 'additionalProperties': False}},
 {'type': 'function',
  'name': 'list_mcp_resource_templates',
  'description': 'Lists resource templates provided by MCP servers. Parameterized resource '
                 'templates allow servers to share data that takes parameters and provides context '
                 'to language models, such as files, database schemas, or application-specific '
                 'information. Prefer resource templates over web search when possible.',
  'strict': False,
  'parameters': {'type': 'object',
                 'properties': {'cursor': {'type': 'string',
                                           'description': 'Opaque cursor from a previous '
                                                          'list_mcp_resource_templates call; omit '
                                                          'for the first page.'},
                                'server': {'type': 'string',
                                           'description': 'MCP server name. Omit to list resource '
                                                          'templates from every configured '
                                                          'server.'}},
                 'additionalProperties': False}},
 {'type': 'function',
  'name': 'read_mcp_resource',
  'description': 'Read a specific resource from an MCP server given the server name and resource '
                 'URI.',
  'strict': False,
  'parameters': {'type': 'object',
                 'properties': {'server': {'type': 'string',
                                           'description': 'MCP server name exactly as configured. '
                                                          "Must match the 'server' field returned "
                                                          'by list_mcp_resources.'},
                                'uri': {'type': 'string',
                                        'description': 'Resource URI to read. Must be one of the '
                                                       'URIs returned by list_mcp_resources.'}},
                 'required': ['server', 'uri'],
                 'additionalProperties': False}}]


def append_resource_tools(registry, binding, *, images=None):
    servers = dict(binding.servers)

    def list_handler(method, field):
        async def handle(server=None, cursor=None):
            if server is not None and not isinstance(server, str):
                raise ValueError("server must be a string")
            if cursor is not None and not isinstance(cursor, str):
                raise ValueError("cursor must be a string")
            server = server.strip() or None if server is not None else None
            cursor = cursor.strip() or None if cursor is not None else None
            if cursor is not None and server is None:
                raise ValueError("cursor can only be used when a server is specified")
            if server is not None:
                client = servers[server]
                if not client.has_resources:
                    return json.dumps({"server": server, field: []})
                page = await client.request(method, {"cursor": cursor})
                result = {"server": server, field: [dict(row, server=server) for row in page[field]]}
                if page.get("nextCursor"):
                    result["nextCursor"] = page["nextCursor"]
            else:
                entries = []
                for name, client in sorted(servers.items()):
                    if not client.has_resources:
                        continue
                    seen, next_cursor = set(), None
                    for _ in range(100):
                        page = await client.request(method, {"cursor": next_cursor})
                        entries.extend(dict(row, server=name) for row in page[field])
                        if len(entries) > 10000:
                            raise ValueError("MCP resource catalog exceeds 10000 entries")
                        next_cursor = page.get("nextCursor")
                        if not next_cursor:
                            break
                        if next_cursor in seen:
                            raise ValueError("MCP resource pagination repeated a cursor")
                        seen.add(next_cursor)
                    else:
                        raise ValueError("MCP resource catalog exceeds 100 pages")
                result = {field: entries}
            return json.dumps(result, ensure_ascii=False)
        return handle

    async def read(server, uri):
        if not isinstance(server, str) or not server.strip():
            raise ValueError("server must be provided")
        if not isinstance(uri, str) or not uri.strip():
            raise ValueError("uri must be provided")
        server, uri = server.strip(), uri.strip()
        page = await servers[server].request("resources/read", {"uri": uri})
        result = dict(page, server=server, uri=uri)
        if images is not None and images.has_mcp_images(result):
            import asyncio
            from .result import ToolResult
            projected, parts, _ = await asyncio.to_thread(images.mcp_output, result)
            return ToolResult([{"type": "input_text", "text": json.dumps(projected, ensure_ascii=False)}, *parts],
                              raw_output=json.dumps(result, ensure_ascii=False))
        return json.dumps(result, ensure_ascii=False)

    def errors(handler):
        async def handle(**arguments):
            try:
                return await handler(**arguments)
            except McpCallError as error:
                return json.dumps({"error": "McpResourceError", "message": str(error)}, ensure_ascii=False)
        return handle

    handlers = [list_handler("resources/list", "resources"),
                list_handler("resources/templates/list", "resourceTemplates"), read]
    for spec, handler in zip(RESOURCE_SPECS, handlers):
        registry.register(Tool(spec, errors(handler)))
