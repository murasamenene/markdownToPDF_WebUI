from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from md2pdf.config import AppConfig
from md2pdf.styles import PandocOptions, StyleSettings


class TestAppConfig(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            config = AppConfig(
                style=StyleSettings(body_font="SimSun", font_size_pt=12, page_number="center"),
                pandoc=PandocOptions(toc=True, toc_depth=2),
                output_dir=str(Path(tmp) / "out"),
                conflict="rename",
            )
            config.save(path)
            loaded = AppConfig.load(path)
            self.assertEqual(loaded.style, config.style)
            self.assertEqual(loaded.pandoc, config.pandoc)
            self.assertEqual(loaded.conflict, "rename")

    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = AppConfig.load(Path(tmp) / "nope.json")
            self.assertEqual(loaded.conflict, "overwrite")
            self.assertEqual(loaded.style.page_size, "A4")

    def test_corrupt_file_returns_defaults_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{ this is not json", encoding="utf-8")
            loaded = AppConfig.load(path)
            self.assertEqual(loaded.style.page_size, "A4")
            self.assertTrue((Path(tmp) / "settings.broken.json").exists())

    def test_unknown_and_bad_fields_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "style": {"font_size_pt": "abc", "page_size": "A4"},
                "pandoc": {"toc": True},
                "unknown": {"a": 1},
                "conflict": "explode",
                "legacy_field": {"a": 1},
            }), encoding="utf-8")
            loaded = AppConfig.load(path)
            self.assertEqual(loaded.style.font_size_pt, 10.5)
            self.assertTrue(loaded.pandoc.toc)
            self.assertEqual(loaded.conflict, "overwrite")


if __name__ == "__main__":
    unittest.main()
