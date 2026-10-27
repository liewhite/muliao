"""网页领域对外公开的接口：网页搜索与抓取的服务（实现见 muliao.web.provider）与它的工具组（muliao.web.tools）。"""

from muliao.web.provider import WebProvider, build_web_provider

__all__ = ["WebProvider", "build_web_provider"]
