"""测试用 stdio MCP server。"""

import os

from mcp.server.mcpserver import Image, MCPServer

server = MCPServer("echo")

# 一张 1×1 的 PNG：没超 provider 上限，测试看它按原样存下、原样交给模型（其他测试也用它当一张真图片）
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0c"
    b"IDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


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
