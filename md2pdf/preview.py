"""PDF 预览：用 pdfium 把页面渲染成 PNG。

不依赖 Pillow（有则用，没有就走内置 PNG 编码器），
这样即使换台机器只装了 Python，预览功能也能工作。
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

try:
    import pypdfium2 as _pdfium

    AVAILABLE = True
    UNAVAILABLE_REASON = ""
except Exception as _exc:  # pragma: no cover - 取决于运行环境
    _pdfium = None
    AVAILABLE = False
    UNAVAILABLE_REASON = f"未安装 pypdfium2（{_exc}）"


class PreviewError(RuntimeError):
    pass


class PdfPreview:
    """保持文档打开，便于翻页时快速渲染。"""

    def __init__(self, path: Path) -> None:
        if not AVAILABLE:
            raise PreviewError(UNAVAILABLE_REASON)
        self.path = Path(path)
        try:
            self._doc = _pdfium.PdfDocument(str(self.path))
        except Exception as exc:
            raise PreviewError(f"无法打开 PDF：{exc}") from exc

    @property
    def page_count(self) -> int:
        return len(self._doc)

    def page_size_pts(self, index: int) -> tuple[float, float]:
        return tuple(self._doc[index].get_size())

    def render_png(
        self,
        index: int,
        *,
        target_width_px: float | None = None,
        scale: float | None = None,
        max_scale: float = 3.0,
    ) -> tuple[bytes, int, int]:
        """返回 (PNG 字节, 宽, 高)。"""
        if not 0 <= index < self.page_count:
            raise PreviewError(f"页码超出范围：{index}")
        page = self._doc[index]
        width_pts, _height_pts = page.get_size()
        if scale is None:
            if target_width_px and width_pts > 0:
                scale = max(0.1, min(max_scale, target_width_px / width_pts))
            else:
                scale = 1.5
        bitmap = page.render(scale=scale)
        try:
            width, height = bitmap.width, bitmap.height
            return _bitmap_to_png(bitmap), width, height
        finally:
            try:
                bitmap.close()
            except Exception:
                pass

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass

    def __enter__(self) -> "PdfPreview":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _bitmap_to_png(bitmap) -> bytes:
    try:
        from PIL import Image  # type: ignore

        image = bitmap.to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return _raw_bitmap_to_png(bitmap)


def _raw_bitmap_to_png(bitmap) -> bytes:
    """把 pdfium 的 BGR/BGRA 缓冲区编码成 24 位 PNG（无需 Pillow）。"""
    width, height, stride = bitmap.width, bitmap.height, bitmap.stride
    if width <= 0 or height <= 0:
        raise PreviewError("页面尺寸无效")
    data = bytes(bitmap.buffer)
    channels = max(1, stride // width)
    rows = bytearray()
    for y in range(height):
        row = data[y * stride:(y + 1) * stride]
        rgb = bytearray(width * 3)
        if channels >= 3:
            rgb[0::3] = row[2::3]
            rgb[1::3] = row[1::3]
            rgb[2::3] = row[0::3]
        else:
            for x in range(width):
                value = row[x]
                rgb[3 * x:3 * x + 3] = bytes((value, value, value))
        rows.append(0)  # PNG 过滤类型 None
        rows.extend(rgb)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(rows), 6)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def page_count(path: Path) -> int | None:
    try:
        with PdfPreview(path) as preview:
            return preview.page_count
    except PreviewError:
        return None
