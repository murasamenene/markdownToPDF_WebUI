from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from md2pdf.utils import (
    decode_bytes,
    escape_css_string,
    format_duration,
    format_size,
    is_css_color,
    is_valid_pdf,
    iter_files,
    path_to_uri,
    unique_path,
)


class TestDecode(unittest.TestCase):
    def test_utf8(self):
        text, enc = decode_bytes("中文 abc".encode("utf-8"))
        self.assertEqual(text, "中文 abc")
        self.assertEqual(enc, "utf-8")

    def test_utf8_bom(self):
        text, enc = decode_bytes("\ufeff中文".encode("utf-8"))
        self.assertEqual(text, "中文")
        self.assertEqual(enc, "utf-8-sig")

    def test_gbk(self):
        text, enc = decode_bytes("中文内容".encode("gbk"))
        self.assertEqual(text, "中文内容")
        self.assertEqual(enc, "gb18030")

    def test_invalid_bytes_do_not_raise(self):
        text, enc = decode_bytes(b"\xff\xfe\x00\x01")
        self.assertIsInstance(text, str)


class TestPaths(unittest.TestCase):
    def test_file_uri_percent_encodes_chinese_and_specials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "中文 目录#1.md"
            path.write_text("x", encoding="utf-8")
            uri = path_to_uri(path)
            self.assertTrue(uri.startswith("file:///"))
            self.assertIn("%23", uri)  # '#' 必须编码，否则会被当成片段截断
            self.assertNotIn(" ", uri)

    def test_directory_uri_has_trailing_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            uri = path_to_uri(Path(tmp), directory=True)
            self.assertTrue(uri.endswith("/"))

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.pdf"
            target.write_text("x", encoding="utf-8")
            self.assertEqual(unique_path(target).name, "a (1).pdf")
            unique_path(target).write_text("y", encoding="utf-8")
            self.assertEqual(unique_path(target).name, "a (2).pdf")

    def test_iter_files_filters_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("x", encoding="utf-8")
            (root / "b.txt").write_text("x", encoding="utf-8")
            (root / "c.png").write_text("x", encoding="utf-8")
            files = iter_files([root, root / "a.md"], (".md", ".txt"))
            names = sorted(p.name for p in files)
            self.assertEqual(names, ["a.md", "b.txt"])


class TestMisc(unittest.TestCase):
    def test_escape_css_string(self):
        self.assertEqual(escape_css_string('a"b\\c\nd'), 'a\\"b\\\\c d')

    def test_is_css_color(self):
        self.assertTrue(is_css_color("#fff"))
        self.assertTrue(is_css_color("#1a1a1a"))
        self.assertFalse(is_css_color("red"))
        self.assertFalse(is_css_color("#12"))

    def test_is_valid_pdf_rejects_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake.pdf"
            fake.write_bytes(b"not a pdf" * 100)
            self.assertFalse(is_valid_pdf(fake))
            self.assertFalse(is_valid_pdf(Path(tmp) / "missing.pdf"))

    def test_format_helpers(self):
        self.assertIn("KB", format_size(2048))
        self.assertTrue(format_duration(0.5).endswith("ms"))
        self.assertIn("s", format_duration(2.5))


if __name__ == "__main__":
    unittest.main()
