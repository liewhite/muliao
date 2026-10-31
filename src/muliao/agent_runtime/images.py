"""把一张图片整理成 provider 一定收的样子（纯函数，不碰 IO）。

provider 对单张图片有硬上限，超了整次请求 400（Anthropic：任一边超过 8000px、一次请求超过 20 张时任一边超过
2000px、base64 后超过 5MB）。图片在会话历史里每次请求都要再发，所以一张超限的图会让这个会话之后的每次请求都失败
（#336：1080×11499 的长截图）。这里在发给模型的唯一出口（`attachments.AttachmentsForModel`）统一整理，原件不动。

规则：长边不超过 `BOX`、编码后不超过 `MAX_BYTES` 的原样交回；否则沿长边切成几段（每段的长边不短于短边的两倍、
也不短于 `BOX`，最多 `MAX_TILES` 段），每段等比缩进 `BOX`×`BOX` 的框。`BOX` 取 1568：Anthropic 会把长边超过它的
图片在服务端缩小，发更大的没有收益；1568 也在多图请求的 2000px 之内。长截图切段而不是整张缩小：1080×11499 整张缩进
框里只剩 147px 宽，字看不清。编码沿用原格式（GIF 取第一帧存 PNG），仍超过 `MAX_BYTES` 的改存 JPEG。
"""

import io
import math
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

BOX = 1568
MAX_BYTES = 5 * 1024 * 1024 * 3 // 4  # Anthropic 单张 5MB 按 base64 编码后计
MAX_TILES = 8

_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "PNG"}
_MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass(frozen=True)
class Tile:
    data: bytes
    media_type: str


@dataclass(frozen=True)
class Fitted:
    """整理的结果：`note` 为空表示原样交回（`tiles` 就是原图一张）；否则说明做了什么，放在图片前面给模型看。"""

    tiles: list[Tile]
    note: str | None


class Unreadable(Exception):
    """内容解不成图片（损坏或类型不符）。"""


def fit(data: bytes, media_type: str) -> Fitted:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise Unreadable(str(e)) from e
    width, height = image.size
    if max(width, height) <= BOX and len(data) <= MAX_BYTES:
        return Fitted([Tile(data, media_type)], None)
    short, long = sorted((width, height))
    count = min(math.ceil(long / max(2 * short, BOX)), MAX_TILES)
    pieces = [_shrink(piece) for piece in _split(image, count)]
    tiles = [_encode(piece, _FORMATS[media_type]) for piece in pieces]
    if count == 1:
        scaled = "x".join(map(str, pieces[0].size))
        note = f"[image {width}x{height} sent as {scaled} {tiles[0].media_type} to fit the model's limits]"
    else:
        direction = "top to bottom" if height >= width else "left to right"
        note = (
            f"[image {width}x{height} split into {count} parts shown {direction}, "
            f"each scaled to fit {BOX}x{BOX}, to fit the model's limits]"
        )
    return Fitted(tiles, note)


def _split(image: Image.Image, count: int) -> list[Image.Image]:
    width, height = image.size
    if height >= width:
        cuts = [height * i // count for i in range(count + 1)]
        return [image.crop((0, top, width, bottom)) for top, bottom in zip(cuts, cuts[1:], strict=False)]
    cuts = [width * i // count for i in range(count + 1)]
    return [image.crop((left, 0, right, height)) for left, right in zip(cuts, cuts[1:], strict=False)]


def _shrink(piece: Image.Image) -> Image.Image:
    width, height = piece.size
    scale = min(1.0, BOX / max(width, height))
    if scale == 1.0:
        return piece
    return piece.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS
    )


def _encode(piece: Image.Image, fmt: str) -> Tile:
    data = _save(piece, fmt)
    if len(data) > MAX_BYTES and fmt != "JPEG":
        fmt, data = "JPEG", _save(piece, "JPEG")
    return Tile(data, _MEDIA_TYPES[fmt])


def _save(piece: Image.Image, fmt: str) -> bytes:
    if fmt == "JPEG" and piece.mode != "RGB":
        piece = piece.convert("RGB")
    out = io.BytesIO()
    piece.save(out, fmt, **({"quality": 85} if fmt in ("JPEG", "WEBP") else {}))
    return out.getvalue()
