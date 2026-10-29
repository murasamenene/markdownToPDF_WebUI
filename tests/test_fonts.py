from __future__ import annotations

import unittest

from md2pdf.fonts import (
    canonicalize_font,
    is_font_installed,
    list_fonts,
    resolve_font_name,
    system_fonts,
)


class TestFonts(unittest.TestCase):
    def test_system_fonts_not_empty(self):
        fonts = system_fonts()
        self.assertGreater(len(fonts), 5, "未能枚举到系统字体")

    def test_common_fonts_present(self):
        fonts = list(system_fonts())
        self.assertIn("Arial", fonts)
        self.assertIn("Times New Roman", fonts)

    def test_chinese_fonts_present(self):
        fonts = list(system_fonts())
        self.assertIn("Microsoft YaHei", fonts)
        self.assertIn("SimSun", fonts)

    def test_resolve_by_english_name(self):
        self.assertEqual(resolve_font_name("SimSun"), "SimSun")
        self.assertEqual(resolve_font_name("consolas"), "Consolas")

    def test_resolve_by_chinese_alias(self):
        self.assertEqual(resolve_font_name("微软雅黑"), "Microsoft YaHei")
        self.assertEqual(resolve_font_name("宋体"), "SimSun")

    def test_alias_case_insensitive(self):
        self.assertEqual(resolve_font_name("Microsoft YaHei"), "Microsoft YaHei")
        self.assertEqual(resolve_font_name("microsoft yahei"), "Microsoft YaHei")

    def test_unknown_font(self):
        self.assertIsNone(resolve_font_name("Definitely Not A Font 12345"))
        self.assertFalse(is_font_installed("Definitely Not A Font 12345"))
        self.assertEqual(canonicalize_font("Definitely Not A Font 12345"),
                         "Definitely Not A Font 12345")

    def test_merged_list_has_no_duplicates(self):
        fonts = list_fonts()
        self.assertEqual(len(fonts), len({f.casefold() for f in fonts}))

    def test_no_at_prefix(self):
        for name in list_fonts():
            self.assertFalse(name.startswith("@"), f"竖排字体应被过滤：{name}")

    def test_style_suffix_stripped(self):
        fonts = system_fonts()
        # 注册表里 "Arial Bold (TrueType)" 不应单独成为一个字体项
        self.assertNotIn("Arial Bold", fonts)


if __name__ == "__main__":
    unittest.main()
