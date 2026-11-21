"""Minimal local MCP service. Run through stdio, not as an HTTP website."""

from mcp.server.fastmcp import FastMCP


server = FastMCP("example")


@server.tool()
def echo(text: str) -> str:
    """Return the supplied text unchanged."""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
