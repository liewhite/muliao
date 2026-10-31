"""图片按 provider 上限整理（agent_runtime.images.fit）：没超的原样，超了的缩小或切段，编码后仍太大的改存 JPEG。"""

import io
import os

import pytest
from PIL import Image

from muliao.agent_runtime import images


def _png(width: int, height: int, mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), "white").save(out, "PNG")
    return out.getvalue()


def _sizes(fitted: images.Fitted) -> list[tuple[int, int]]:
    return [Image.open(io.BytesIO(t.data)).size for t in fitted.tiles]


def test_an_image_within_the_limits_is_passed_through_unchanged():
    data = _png(1568, 800)
    assert images.fit(data) == images.Fitted([images.Tile(data, "image/png")], None)


def test_a_long_screenshot_is_split_into_readable_parts_instead_of_shrunk_to_a_sliver():
    # #336 的那张：1080×11499，整张缩进 1568 的框只剩 147px 宽
    fitted = images.fit(_png(1080, 11499))
    assert len(fitted.tiles) == 6
    assert all(max(size) <= images.BOX for size in _sizes(fitted))
    assert all(880 <= width <= 885 for width, _ in _sizes(fitted))
    assert fitted.note is not None and "split into 6 parts shown top to bottom" in fitted.note


def test_a_wide_image_is_split_left_to_right():
    fitted = images.fit(_png(9000, 300))
    assert _sizes(fitted) == [(1500, 300)] * 6
    assert fitted.note is not None and "left to right" in fitted.note


def test_a_large_but_not_elongated_image_is_only_scaled_down():
    fitted = images.fit(_png(3000, 2000))
    assert _sizes(fitted) == [(1568, 1045)]
    assert fitted.note == "[image 3000x2000 sent as 1568x1045 image/png to fit the model's limits]"


def test_the_number_of_parts_is_capped():
    fitted = images.fit(_png(20, 60000, "L"))
    assert len(fitted.tiles) == images.MAX_TILES
    assert all(max(size) <= images.BOX for size in _sizes(fitted))


def test_an_image_still_too_large_after_scaling_is_sent_as_jpeg(monkeypatch):
    noise = Image.frombytes("RGB", (1000, 1000), os.urandom(3 * 1000 * 1000))
    out = io.BytesIO()
    noise.save(out, "PNG")
    monkeypatch.setattr(images, "MAX_BYTES", len(out.getvalue()) - 1)
    fitted = images.fit(out.getvalue())
    [tile] = fitted.tiles
    assert tile.media_type == "image/jpeg" and len(tile.data) <= images.MAX_BYTES
    assert fitted.note == "[image 1000x1000 sent as 1000x1000 image/jpeg to fit the model's limits]"


def test_a_transparent_gif_is_sent_as_png():
    out = io.BytesIO()
    Image.new("P", (100, 5000)).save(out, "GIF")
    fitted = images.fit(out.getvalue())
    assert {t.media_type for t in fitted.tiles} == {"image/png"}


def test_the_type_sent_follows_the_content_not_the_declared_type():
    # 扩展名是 .jpg、内容是 PNG：按内容发，provider 才收
    data = _png(10, 10)
    assert images.fit(data).tiles == [images.Tile(data, "image/png")]
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, "BMP")
    assert [t.media_type for t in images.fit(out.getvalue()).tiles] == ["image/png"]


def test_a_rotated_phone_photo_is_turned_upright_before_scaling():
    # EXIF 方向 6：存的是 4032×3024，看起来是竖的 3024×4032；缩放后不带 EXIF，必须先转正
    exif = Image.Exif()
    exif[0x0112] = 6
    out = io.BytesIO()
    Image.new("RGB", (4032, 3024)).save(out, "JPEG", exif=exif)
    fitted = images.fit(out.getvalue())
    assert _sizes(fitted) == [(1176, 1568)]
    assert fitted.note == "[image 3024x4032 sent as 1176x1568 image/jpeg to fit the model's limits]"


def test_content_that_is_not_an_image_is_unreadable():
    with pytest.raises(images.Unreadable):
        images.fit(b"not an image")
