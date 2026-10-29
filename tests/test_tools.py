from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from md2pdf.errors import Md2PdfError
from md2pdf.tools import (
    ToolInfo,
    _version_from_install_dir,
    find_browser,
    find_pandoc,
    pandoc_capabilities,
    supports_math_method,
)


class TestVersionParsing(unittest.TestCase):
    def test_parent_is_version_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "156.0.4314.50" / "msedge.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.assertEqual(_version_from_install_dir(exe), "156.0.4314.50")

    def test_version_subdir_of_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Application"
            exe = app / "chrome.exe"
            old = app / "120.0.6099.109"
            new = app / "121.0.6167.85"
            for d in (old, new):
                d.mkdir(parents=True)
                (d / "chrome.dll").write_bytes(b"x")
            exe.write_bytes(b"")
            self.assertEqual(_version_from_install_dir(exe), "121.0.6167.85")

    def test_unknown_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "bin" / "chromium"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.assertEqual(_version_from_install_dir(exe), "")


class TestToolDiscovery(unittest.TestCase):
    def test_pandoc_found(self):
        try:
            tool = find_pandoc()
        except Md2PdfError as exc:
            self.skipTest(str(exc))
        self.assertTrue(tool.path.exists())
        self.assertTrue(tool.version.startswith("3."), tool.version)
        self.assertFalse(tool.supports_margin_boxes())

    def test_browser_found(self):
        try:
            tool = find_browser()
        except Md2PdfError as exc:
            self.skipTest(str(exc))
        self.assertTrue(tool.path.exists())
        self.assertGreaterEqual(tool.major, 100, tool.version)
        self.assertIn(tool.kind, {"edge", "chrome", "chromium"})

    def test_math_method_capability(self):
        try:
            tool = find_pandoc()
        except Md2PdfError as exc:
            self.skipTest(str(exc))
        self.assertIsInstance(supports_math_method(tool.path), bool)

    def test_pandoc_capabilities(self):
        try:
            tool = find_pandoc()
        except Md2PdfError as exc:
            self.skipTest(str(exc))
        caps = pandoc_capabilities(tool.path)
        self.assertIsInstance(caps.math_method, bool)
        self.assertIsInstance(caps.syntax_highlighting, bool)
        # 3.11 应同时支持两个新参数
        if tool.version.startswith("3.11"):
            self.assertTrue(caps.math_method)
            self.assertTrue(caps.syntax_highlighting)

    def test_tool_display(self):
        info = ToolInfo("edge", Path("C:/x/msedge.exe"), "156.0.4314.50")
        self.assertIn("Edge", info.display)
        self.assertIn("156", info.display)
        self.assertTrue(info.supports_margin_boxes())

    def test_old_browser_has_no_margin_boxes(self):
        info = ToolInfo("edge", Path("C:/x/msedge.exe"), "120.0.0.0")
        self.assertFalse(info.supports_margin_boxes())


if __name__ == "__main__":
    unittest.main()
