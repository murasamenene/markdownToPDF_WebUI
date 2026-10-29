from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from md2pdf.postprocess import (
    DocMeta,
    Heading,
    apply_pdf_info,
    extract_document_info,
    locate_headings,
    normalize_text,
)

HTML = """<!DOCTYPE html>
<html><head>
<title>页面标题</title>
<meta name="keywords" content="测试, 排版" />
</head>
<body>
<header id="title-block-header">
<h1 class="title">真正的标题</h1>
<p class="author">张三</p>
<p class="author">李四</p>
<p class="date">2026-10-29</p>
</header>
<nav id="TOC" role="doc-toc">
<h2 id="toc-title">目录</h2>
<ul><li><a href="#s1">第一节</a></li><li><a href="#s2">第二节</a></li></ul>
</nav>
<h1 id="s1">第一节</h1>
<p>正文段落，里面提到第一节但不应被当成标题。</p>
<h2 id="s2">第二节 <code>代码</code> 标题</h2>
<h3>1.1 小节</h3>
</body></html>
"""


class TestHtmlParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "doc.html"
        self.path.write_text(HTML, encoding="utf-8")
        self.info = extract_document_info(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_title_block_wins_over_head_title(self):
        self.assertEqual(self.info.meta.title, "真正的标题")

    def test_authors(self):
        self.assertEqual(self.info.meta.authors, ["张三", "李四"])

    def test_keywords(self):
        self.assertEqual(self.info.meta.keywords, ["测试", "排版"])

    def test_headings_exclude_toc_and_title(self):
        texts = [h.text for h in self.info.headings]
        self.assertEqual(texts, ["第一节", "第二节 代码 标题", "1.1 小节"])
        self.assertEqual([h.level for h in self.info.headings], [1, 2, 3])

    def test_missing_file_does_not_raise(self):
        info = extract_document_info(Path(self.tmp.name) / "nope.html")
        self.assertEqual(info.headings, [])

    def test_falls_back_to_head_title(self):
        path = Path(self.tmp.name) / "only-title.html"
        path.write_text("<html><head><title>只有标题</title></head><body></body></html>",
                        encoding="utf-8")
        self.assertEqual(extract_document_info(path).meta.title, "只有标题")


class TestHeadingLocation(unittest.TestCase):
    def test_skips_toc_page(self):
        pages = [
            "目录\n第一节\n第二节\n第三节\n第四节",
            "第一节 正文内容",
            "第二节 正文内容",
            "第三节 正文内容",
            "第四节 正文内容",
        ]
        headings = [Heading(1, "第一节"), Heading(1, "第二节"),
                    Heading(1, "第三节"), Heading(1, "第四节")]
        outline = locate_headings(pages, headings, skip_toc_pages=True)
        self.assertEqual([page for _lvl, _t, page in outline], [1, 2, 3, 4])

    def test_toc_start_uses_second_occurrence(self):
        pages = ["目录 第一节 第二节", "第一节 正文", "第二节 正文"]
        headings = [Heading(1, "第一节"), Heading(1, "第二节")]
        skipped = locate_headings(pages, headings, skip_toc_pages=True)
        self.assertEqual([p for _l, _t, p in skipped], [1, 2])
        # 未开启时，第一页（含目录）会被当成正文
        plain = locate_headings(pages, headings)
        self.assertEqual([p for _l, _t, p in plain], [0, 0])

    def test_no_toc_document_unaffected(self):
        pages = ["第一节 内容", "第二节 内容"]
        outline = locate_headings(
            pages, [Heading(1, "第一节"), Heading(1, "第二节")], skip_toc_pages=True)
        self.assertEqual([p for _l, _t, p in outline], [0, 1])

    def test_missing_heading_is_skipped(self):
        pages = ["第一节 内容", "第三节 内容"]
        headings = [Heading(1, "第一节"), Heading(1, "第二节"), Heading(1, "第三节")]
        outline = locate_headings(pages, headings)
        self.assertEqual([t for _l, t, _p in outline], ["第一节", "第三节"])

    def test_whitespace_and_case_insensitive(self):
        pages = ["第 一 节\nINTRODUCTION"]
        outline = locate_headings(pages, [Heading(1, "第一节 introduction")])
        self.assertEqual(outline[0][2], 0)

    def test_repeated_headings_advance(self):
        pages = ["小结 内容一", "小结 内容二"]
        headings = [Heading(1, "小结"), Heading(1, "小结")]
        outline = locate_headings(pages, headings)
        self.assertEqual([page for _l, _t, page in outline], [0, 1])

    def test_empty_inputs(self):
        self.assertEqual(locate_headings([], [Heading(1, "x")]), [])
        self.assertEqual(locate_headings(["x"], []), [])

    def test_normalize(self):
        self.assertEqual(normalize_text(" A\u00a0b\nC "), "abc")


class TestPdfInfoWriteback(unittest.TestCase):
    def _make_pdf(self, path: Path, pages: int = 2) -> None:
        from reportlab.pdfgen import canvas

        pdf = canvas.Canvas(str(path))
        for i in range(pages):
            pdf.drawString(100, 700, f"page {i + 1}")
            pdf.showPage()
        pdf.save()

    def _read_back(self, path: Path):
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return reader

    def _flatten(self, items):
        for item in items:
            if isinstance(item, list):
                yield from self._flatten(item)
            else:
                yield item

    def test_pypdf_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            self._make_pdf(path, pages=3)
            ok, message = apply_pdf_info(
                path,
                DocMeta(title="标题", authors=["张三"], keywords=["a", "b"]),
                [(1, "第一章", 0), (2, "第一节", 1)],
                backend="pypdf",
            )
            self.assertTrue(ok, message)
            reader = self._read_back(path)
            self.assertEqual(reader.metadata.get("/Title"), "标题")
            self.assertEqual(reader.metadata.get("/Author"), "张三")
            self.assertEqual(reader.metadata.get("/Keywords"), "a, b")
            items = list(self._flatten(reader.outline))
            self.assertEqual([i.title for i in items], ["第一章", "第一节"])
            self.assertEqual(reader.get_destination_page_number(items[1]), 1)

    def test_pymupdf_backend(self):
        try:
            import pymupdf  # noqa: F401
        except ImportError:
            self.skipTest("未安装 PyMuPDF")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            self._make_pdf(path, pages=3)
            ok, message = apply_pdf_info(
                path,
                DocMeta(title="标题", authors=["张三"]),
                [(1, "第一章", 1)],
                backend="pymupdf",
            )
            self.assertTrue(ok, message)
            reader = self._read_back(path)
            self.assertEqual(reader.metadata.get("/Title"), "标题")
            self.assertEqual(reader.metadata.get("/Creator"), "markdownToPDF WebUI")
            items = list(self._flatten(reader.outline))
            self.assertEqual([i.title for i in items], ["第一章"])
            self.assertEqual(reader.get_destination_page_number(items[0]), 1)

    def test_no_outline_still_writes_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            self._make_pdf(path, pages=1)
            ok, _ = apply_pdf_info(path, DocMeta(title="只有元数据"), [], backend="auto")
            self.assertTrue(ok)
            self.assertEqual(self._read_back(path).metadata.get("/Title"), "只有元数据")

    def test_unknown_backend_still_tries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            self._make_pdf(path, pages=1)
            ok, _ = apply_pdf_info(path, DocMeta(title="x"), [], backend="nonsense")
            self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
