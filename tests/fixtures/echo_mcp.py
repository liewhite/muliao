"""测试用 stdio MCP server。"""

import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


@server.tool()
def token() -> str:
    """Return the TOKEN environment variable."""
    return os.environ["TOKEN"]


if __name__ == "__main__":
    server.run()
