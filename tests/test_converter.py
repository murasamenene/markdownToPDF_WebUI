"""集成测试：真实调用 pandoc + Edge 生成 PDF。

这些测试比较慢（每次约 2–3 秒），但能覆盖纯单元测试抓不到的问题：
CSS 是否真的生效、图片是否真的加载、页码是否真的渲染、目标文件锁是否被正确识别。
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from md2pdf.converter import ConversionRequest, Converter
from md2pdf.errors import CancelledError, ConversionError, Md2PdfError, ToolNotFoundError
from md2pdf.styles import PandocOptions, StyleSettings
from md2pdf.tools import ToolInfo, find_browser, find_pandoc
from md2pdf.utils import is_valid_pdf, remove_tree

from tests.helpers import WORK, write_sample

PANDOC = None
BROWSER = None


def setUpModule() -> None:
    global PANDOC, BROWSER
    try:
        PANDOC = find_pandoc()
        BROWSER = find_browser()
    except Md2PdfError as exc:
        raise unittest.SkipTest(f"缺少转换依赖：{exc}")


def pdf_page_texts(path: Path) -> list[str]:
    import pypdfium2 as pdfium

    texts: list[str] = []
    with pdfium.PdfDocument(str(path)) as doc:
        for i in range(len(doc)):
            textpage = doc[i].get_textpage()
            try:
                texts.append(textpage.get_text_range())
            finally:
                textpage.close()
    return texts


def pdf_text(path: Path) -> str:
    return "\n".join(pdf_page_texts(path))


def _flatten_outline(items):
    for item in items:
        if isinstance(item, list):
            yield from _flatten_outline(item)
        else:
            yield item


def pages_with_red(path: Path) -> int:
    """统计出现明显红色的页数，用来验证图片确实被加载了。"""
    import pypdfium2 as pdfium

    count = 0
    with pdfium.PdfDocument(str(path)) as doc:
        for i in range(len(doc)):
            bitmap = doc[i].render(scale=0.5)
            try:
                raw = bitmap.to_pil().convert("RGB").tobytes()
            finally:
                bitmap.close()
            reds = sum(
                1 for j in range(0, len(raw), 3)
                if raw[j] > 170 and raw[j + 1] < 120 and raw[j + 2] < 120
            )
            if reds > 100:
                count += 1
    return count


class ConverterIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.converter = Converter(PANDOC, BROWSER, timeout_browser=240)

    # ---------------- 辅助 ----------------

    def make_case(self, case: str, **kwargs) -> tuple[Path, Path]:
        directory = WORK / case
        source = write_sample(directory, **kwargs)
        return source, directory

    def convert(self, source: Path, **kwargs):
        request = ConversionRequest(source=source, **kwargs)
        return self.converter.convert(request)

    # ---------------- 基本流程 ----------------

    def test_basic_conversion(self):
        source, directory = self.make_case("basic")
        result = self.convert(source)
        self.assertTrue(result.output.exists())
        self.assertEqual(result.output.parent, directory)
        self.assertTrue(is_valid_pdf(result.output))
        self.assertGreaterEqual(result.pages, 2, "示例文档至少应渲染出 2 页")
        self.assertGreater(result.size_bytes, 5000)
        self.assertGreater(result.duration, 0)

    def test_chinese_and_spaced_paths(self):
        source, _ = self.make_case("中文路径", name="中文 文件（测试）.md")
        out_dir = WORK / "输出 目录"
        result = self.convert(source, output_dir=out_dir)
        self.assertEqual(result.output.parent, out_dir)
        self.assertEqual(result.output.stem, "中文 文件（测试）")
        self.assertTrue(is_valid_pdf(result.output))

    def test_keep_temp_writes_base_and_style(self):
        source, _ = self.make_case("keeptemp")
        result = self.convert(source, keep_temp=True)
        try:
            self.assertIsNotNone(result.temp_dir)
            html_path = result.temp_dir / "doc.html"
            self.assertTrue(html_path.exists())
            content = html_path.read_text(encoding="utf-8")
            self.assertIn("<base href=", content)
            self.assertIn("<style>", content)
            self.assertIn("--font-body", content)
        finally:
            remove_tree(result.temp_dir)

    # ---------------- 样式是否真的生效 ----------------

    def test_toc_occupies_first_page(self):
        source, _ = self.make_case("toc")
        result = self.convert(source, pandoc=PandocOptions(toc=True))
        texts = pdf_page_texts(result.output)
        self.assertIn("目录", texts[0])
        self.assertIn("第一单元", texts[0])

    def test_page_numbers_render(self):
        source, _ = self.make_case("pagenum")
        style = StyleSettings(page_number="center", page_number_format="n_of_total")
        result = self.convert(source, style=style)
        text = pdf_text(result.output)
        self.assertRegex(text, r"第\s*\d+\s*页\s*/\s*共\s*\d+\s*页")

    def test_font_size_changes_layout(self):
        source, directory = self.make_case("fontsize", long_paragraphs=40)
        small = self.convert(
            source, output=directory / "small.pdf",
            style=StyleSettings(font_size_pt=8),
        )
        large = self.convert(
            source, output=directory / "large.pdf",
            style=StyleSettings(font_size_pt=18),
        )
        self.assertGreater(large.pages, small.pages,
                           f"字号变大后页数应增加：8pt={small.pages}, 18pt={large.pages}")

    def test_image_is_loaded_through_base_href(self):
        source, _ = self.make_case("image")
        result = self.convert(source)
        self.assertGreaterEqual(pages_with_red(result.output), 1,
                                "PDF 中未找到红色图片，说明相对路径图片没有被加载")

    def test_implicit_figures_switch(self):
        """默认把独立图片的 alt 文本作为图注；关闭后不再生成 figcaption。"""
        source, _ = self.make_case("figures")

        with_figures = self.convert(
            source, pandoc=PandocOptions(implicit_figures=True), keep_temp=True)
        try:
            html = (with_figures.temp_dir / "doc.html").read_text(encoding="utf-8")
            self.assertIn("<figcaption", html)
            self.assertIn("测试图片", html)
        finally:
            remove_tree(with_figures.temp_dir)

        without_figures = self.convert(
            source, pandoc=PandocOptions(implicit_figures=False), keep_temp=True)
        try:
            html = (without_figures.temp_dir / "doc.html").read_text(encoding="utf-8")
            self.assertNotIn("<figcaption", html)
            self.assertIn('alt="测试图片"', html)  # alt 属性仍然保留
        finally:
            remove_tree(without_figures.temp_dir)

    def test_math_does_not_break_conversion(self):
        source, _ = self.make_case("math")
        result = self.convert(source, pandoc=PandocOptions(math="mathml"))
        self.assertTrue(is_valid_pdf(result.output))
        text = pdf_text(result.output)
        self.assertTrue("∫" in text or "mc" in text or "E" in text)

    # ---------------- 后处理：书签与元数据 ----------------

    def test_bookmarks_and_metadata_written(self):
        from pypdf import PdfReader

        directory = WORK / "pdfinfo"
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / "info.md"
        source.write_text(
            "---\ntitle: 元数据测试\nauthor: 张三\nkeywords: [排版, 测试]\n---\n\n"
            "# 第一章\n\n正文段落。\n\n## 小节一\n\n更多正文。\n\n"
            "# 第二章\n\n结尾内容。\n",
            encoding="utf-8",
        )
        result = self.convert(source, pandoc=PandocOptions(toc=True), add_pdf_info=True)
        reader = PdfReader(str(result.output))
        self.assertEqual(reader.metadata.get("/Author"), "张三")
        self.assertIn("排版", str(reader.metadata.get("/Keywords")))
        self.assertEqual(reader.metadata.get("/Creator"), "markdownToPDF WebUI")

        items = list(_flatten_outline(reader.outline))
        self.assertTrue(items, "没有生成任何书签")
        titles = [item.title for item in items]
        self.assertIn("第一章", titles)
        self.assertIn("第二章", titles)
        pages = [reader.get_destination_page_number(item) for item in items]
        self.assertGreater(max(pages), 0, "书签全部指向目录页，说明目录页没有被跳过")

    def test_bookmarks_off_by_default(self):
        from pypdf import PdfReader

        source, _ = self.make_case("noinfo", long_paragraphs=1)
        result = self.convert(source)
        reader = PdfReader(str(result.output))
        self.assertEqual(list(_flatten_outline(reader.outline)), [])
        self.assertNotEqual(reader.metadata.get("/Creator"), "markdownToPDF WebUI")

    def test_bookmarks_fallback_to_pypdf(self):
        """强制走 pypdf 后端，确认兜底路径也能写出书签。"""
        from pypdf import PdfReader

        from md2pdf.postprocess import DocMeta, apply_pdf_info

        source, directory = self.make_case("pypdf-fallback", long_paragraphs=1)
        result = self.convert(source)
        ok, message = apply_pdf_info(
            result.output, DocMeta(title="兜底"), [(1, "起点", 0)], backend="pypdf")
        self.assertTrue(ok, message)
        items = list(_flatten_outline(PdfReader(str(result.output)).outline))
        self.assertIn("起点", [item.title for item in items])

    # ---------------- 输入健壮性 ----------------

    def test_gbk_source_is_transcoded(self):
        directory = WORK / "gbk"
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / "gbk.md"
        source.write_bytes("# 编码测试\n\n这是一段 GBK 编码的中文正文。\n".encode("gbk"))
        result = self.convert(source)
        self.assertIn("编码", pdf_text(result.output))
        self.assertTrue(any("gb18030" in w for w in result.warnings), result.warnings)

    def test_utf8_bom_source(self):
        directory = WORK / "bom"
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / "bom.md"
        source.write_bytes("\ufeff# BOM 测试\n\n正文内容。\n".encode("utf-8"))
        result = self.convert(source)
        self.assertIn("BOM", pdf_text(result.output))

    def test_txt_file_supported(self):
        directory = WORK / "txt"
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / "plain.txt"
        source.write_text("纯文本内容\n\n第二段。\n", encoding="utf-8")
        result = self.convert(source)
        self.assertTrue(is_valid_pdf(result.output))

    # ---------------- 冲突与错误 ----------------

    def test_skip_conflict(self):
        source, directory = self.make_case("skip")
        target = directory / "sample.pdf"
        target.write_bytes(b"%PDF-1.4\n%existing\n")
        result = self.convert(source, conflict="skip")
        self.assertTrue(result.skipped)
        self.assertEqual(target.read_bytes(), b"%PDF-1.4\n%existing\n")

    def test_rename_conflict(self):
        source, directory = self.make_case("rename")
        (directory / "sample.pdf").write_bytes(b"%PDF-1.4\n%existing\n")
        result = self.convert(source, conflict="rename")
        self.assertEqual(result.output.name, "sample (1).pdf")
        self.assertTrue(is_valid_pdf(result.output))

    def test_locked_target_gives_friendly_error(self):
        source, directory = self.make_case("locked", long_paragraphs=1)
        target = directory / "sample.pdf"
        target.write_bytes(b"%PDF-1.4\n%existing\n")
        with open(target, "rb") as handle:
            self.assertIsNotNone(handle.read(1))
            with self.assertRaises(ConversionError) as ctx:
                self.convert(source)
            self.assertIn("占用", str(ctx.exception))

    def test_missing_source(self):
        with self.assertRaises(ConversionError):
            self.convert(WORK / "does-not-exist.md")

    def test_unsupported_suffix(self):
        path = WORK / "data.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        with self.assertRaises(ConversionError) as ctx:
            self.convert(path)
        self.assertIn("不支持", str(ctx.exception))

    def test_missing_pandoc_binary(self):
        bogus = Converter(
            ToolInfo("pandoc", Path("C:/definitely/not/here/pandoc.exe")),
            BROWSER,
        )
        source, _ = self.make_case("boguspandoc", long_paragraphs=1)
        with self.assertRaises(ToolNotFoundError):
            bogus.convert(ConversionRequest(source=source))

    def test_preemptive_cancel(self):
        source, _ = self.make_case("cancel", long_paragraphs=1)
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledError):
            self.converter.convert(ConversionRequest(source=source), cancel=event)


if __name__ == "__main__":
    unittest.main()
