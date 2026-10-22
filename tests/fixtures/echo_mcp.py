"""测试用 stdio MCP server。"""

import os

from mcp.server.mcpserver import Image, MCPServer

server = MCPServer("echo")

# 一个最小的 PNG 文件头加几个字节：测试只看它按原样存下、原样交给模型
PNG = b"\x89PNG\r\n\x1a\n" + b"screenshot"


@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


@server.tool()
def token() -> str:
    """Return the TOKEN environment variable."""
    return os.environ["TOKEN"]


@server.tool()
def cwd() -> str:
    """Return the working directory of this server process."""
    return os.getcwd()


@server.tool()
def screenshot() -> list:
    """Return a caption and a PNG image, like a browser screenshot."""
    return ["page shot", Image(data=PNG, format="png")]


if __name__ == "__main__":
    server.run()
