//! 把一张图片整理成 provider 一定收的样子（纯函数，不碰 IO）。
//!
//! provider 对单张图片有硬上限，超了整次请求 400（Anthropic：任一边超过 8000px、一次请求超过 20 张时任一边超过 2000px、
//! base64 后超过 5MB）。图片在会话历史里每次请求都要再发，所以一张超限的图会让这个会话之后的每次请求都失败（#336：
//! 1080×11499 的长截图）。这里在发给模型的唯一出口（[`crate::attachments`]）统一整理，原件不动。
//!
//! 规则：长边不超过 [`BOX`]、编码后不超过 [`MAX_BYTES`] 的原样交回；否则沿长边切成几段（每段的长边不短于短边的两倍、
//! 也不短于 `BOX`，最多 [`MAX_TILES`] 段），每段等比缩进 `BOX`×`BOX` 的框。`BOX` 取 1568：Anthropic 会把长边超过它的图片
//! 在服务端缩小，发更大的没有收益；1568 也在多图请求的 2000px 之内。长截图切段而不是整张缩小：1080×11499 整张缩进框里
//! 只剩 147px 宽，字看不清。编码沿用原格式（GIF 取第一帧存 PNG；BMP 等表外格式解不开，按解不开处理），仍超过 `MAX_BYTES` 的改存 JPEG。
//!
//! 和 Python 版（Pillow）的不同：WebP 重新编码只能无损（image 库没有有损 WebP 编码器），更容易超过 `MAX_BYTES` 而改存
//! JPEG。

use std::io::Cursor;

use image::{DynamicImage, ImageDecoder, ImageFormat, ImageReader};

pub const BOX: u32 = 1568;
/// Anthropic 单张 5MB 按 base64 编码后计
pub const MAX_BYTES: usize = 5 * 1024 * 1024 * 3 / 4;
pub const MAX_TILES: u32 = 8;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tile {
    pub data: Vec<u8>,
    pub media_type: &'static str,
}

/// 整理的结果：`note` 为空表示原样交回（`tiles` 就是原图一张，类型按内容而不按声明）；否则说明做了什么，放在图片前面
/// 给模型看。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fitted {
    pub tiles: Vec<Tile>,
    pub note: Option<String>,
}

/// 内容解不成图片（损坏、不是图片、像素多到超过解码的内存上限）。
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct Unreadable(String);

/// 原样交回时的类型：按解码出的真实格式。不在表里的（BMP 等）不能原样交回。
fn pass_through(format: ImageFormat) -> Option<&'static str> {
    match format {
        ImageFormat::Png => Some("image/png"),
        ImageFormat::Jpeg => Some("image/jpeg"),
        ImageFormat::WebP => Some("image/webp"),
        ImageFormat::Gif => Some("image/gif"),
        _ => None,
    }
}

/// 整理后的编码格式：沿用原格式，GIF（取第一帧）与表外的存 PNG。
fn encode_as(format: ImageFormat) -> ImageFormat {
    match format {
        ImageFormat::Jpeg | ImageFormat::WebP => format,
        _ => ImageFormat::Png,
    }
}

fn media_type(format: ImageFormat) -> &'static str {
    match format {
        ImageFormat::Jpeg => "image/jpeg",
        ImageFormat::WebP => "image/webp",
        _ => "image/png",
    }
}

/// 类型看内容不看声明：扩展名是 .jpg 而内容是 PNG 的，按声明发 provider 会拒绝。
pub fn fit(data: &[u8]) -> Result<Fitted, Unreadable> {
    let reader = ImageReader::new(Cursor::new(data)).with_guessed_format().map_err(|e| Unreadable(e.to_string()))?;
    let format = reader.format().ok_or_else(|| Unreadable("unknown image format".into()))?;
    let mut decoder = reader.into_decoder().map_err(|e| Unreadable(e.to_string()))?;
    let orientation = decoder.orientation().map_err(|e| Unreadable(e.to_string()))?;
    let mut image = DynamicImage::from_decoder(decoder).map_err(|e| Unreadable(e.to_string()))?;
    let (width, height) = (image.width(), image.height());
    if let Some(media) = pass_through(format)
        && width.max(height) <= BOX
        && data.len() <= MAX_BYTES
    {
        return Ok(Fitted { tiles: vec![Tile { data: data.to_vec(), media_type: media }], note: None });
    }
    // 切段、缩放不看 EXIF 方向，存出来也不带它：先按方向转正，否则竖拍的照片发出去是横的
    image.apply_orientation(orientation);
    let (width, height) = (image.width(), image.height());
    let (short, long) = (width.min(height), width.max(height));
    let count = long.div_ceil((2 * short).max(BOX)).clamp(1, MAX_TILES);
    let target = encode_as(format);
    let pieces: Vec<DynamicImage> = split(&image, count).into_iter().map(shrink).collect();
    let tiles = pieces.iter().map(|p| encode(p, target)).collect::<Result<Vec<_>, _>>()?;
    let note = if count == 1 {
        format!(
            "[image {width}x{height} sent as {}x{} {} to fit the model's limits]",
            pieces[0].width(),
            pieces[0].height(),
            tiles[0].media_type
        )
    } else {
        let direction = if height >= width { "top to bottom" } else { "left to right" };
        format!(
            "[image {width}x{height} split into {count} parts shown {direction}, each scaled to fit {BOX}x{BOX}, to fit \
             the model's limits]"
        )
    };
    Ok(Fitted { tiles, note: Some(note) })
}

fn split(image: &DynamicImage, count: u32) -> Vec<DynamicImage> {
    let (width, height) = (image.width(), image.height());
    let along = if height >= width { height } else { width };
    let cuts: Vec<u32> = (0..=count).map(|i| (u64::from(along) * u64::from(i) / u64::from(count)) as u32).collect();
    cuts.windows(2)
        .map(|w| {
            let (start, len) = (w[0], w[1] - w[0]);
            if height >= width { image.crop_imm(0, start, width, len) } else { image.crop_imm(start, 0, len, height) }
        })
        .collect()
}

fn shrink(piece: DynamicImage) -> DynamicImage {
    let (width, height) = (piece.width(), piece.height());
    let scale = (f64::from(BOX) / f64::from(width.max(height))).min(1.0);
    if scale >= 1.0 {
        return piece;
    }
    let w = ((f64::from(width) * scale).round() as u32).max(1);
    let h = ((f64::from(height) * scale).round() as u32).max(1);
    piece.resize_exact(w, h, image::imageops::FilterType::Lanczos3)
}

fn encode(piece: &DynamicImage, format: ImageFormat) -> Result<Tile, Unreadable> {
    let data = save(piece, format)?;
    if data.len() > MAX_BYTES && format != ImageFormat::Jpeg {
        return Ok(Tile { data: save(piece, ImageFormat::Jpeg)?, media_type: media_type(ImageFormat::Jpeg) });
    }
    Ok(Tile { data, media_type: media_type(format) })
}

fn save(piece: &DynamicImage, format: ImageFormat) -> Result<Vec<u8>, Unreadable> {
    let mut out = Cursor::new(Vec::new());
    let result = match format {
        ImageFormat::Jpeg => {
            let rgb = DynamicImage::ImageRgb8(piece.to_rgb8());
            rgb.write_with_encoder(image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, 85))
        }
        // WebP 编码器只支持 8 位的 RGB / RGBA
        ImageFormat::WebP => DynamicImage::ImageRgba8(piece.to_rgba8()).write_to(&mut out, format),
        _ => piece.write_to(&mut out, format),
    };
    result.map_err(|e| Unreadable(format!("re-encoding the image failed: {e}")))?;
    Ok(out.into_inner())
}

#[cfg(test)]
mod tests {
    use image::{Rgb, RgbImage};

    use super::*;

    fn png(w: u32, h: u32) -> Vec<u8> {
        let img = RgbImage::from_fn(w, h, |x, y| Rgb([(x % 251) as u8, (y % 241) as u8, 7]));
        let mut out = Cursor::new(Vec::new());
        DynamicImage::ImageRgb8(img).write_to(&mut out, ImageFormat::Png).unwrap();
        out.into_inner()
    }

    #[test]
    fn small_images_pass_through_typed_by_content() {
        let data = png(100, 50);
        let f = fit(&data).unwrap();
        assert_eq!(f.note, None);
        assert_eq!(f.tiles, vec![Tile { data, media_type: "image/png" }]);
    }

    #[test]
    fn long_screenshots_are_split_and_scaled() {
        let f = fit(&png(1080, 11499)).unwrap();
        // 11499 / max(2160, 1568) → 6 段，每段 1080x1916 缩进 1568 框
        assert_eq!(f.tiles.len(), 6);
        assert_eq!(
            f.note.as_deref(),
            Some(
                "[image 1080x11499 split into 6 parts shown top to bottom, each scaled to fit 1568x1568, to fit the model's limits]"
            )
        );
        let first = image::load_from_memory(&f.tiles[0].data).unwrap();
        assert!(first.height() <= BOX && first.width() <= BOX, "{}x{}", first.width(), first.height());
        assert!(f.tiles.iter().all(|t| t.media_type == "image/png"));
    }

    #[test]
    fn big_images_are_scaled_once() {
        let f = fit(&png(3000, 2000)).unwrap();
        assert_eq!(f.tiles.len(), 1);
        assert_eq!(f.note.as_deref(), Some("[image 3000x2000 sent as 1568x1045 image/png to fit the model's limits]"));
    }

    #[test]
    fn garbage_is_unreadable() {
        assert!(fit(b"not an image").is_err());
        let mut truncated = png(100, 100);
        truncated.truncate(60);
        assert!(fit(&truncated).is_err());
    }
}
