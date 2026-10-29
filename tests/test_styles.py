from __future__ import annotations

import unittest

from md2pdf.styles import (
    PandocOptions,
    StyleSettings,
    build_css,
    font_stack,
    mono_stack,
    pandoc_highlight_for,
    preset,
    preset_names,
)


class TestStyleModel(unittest.TestCase):
    def test_defaults_are_normalized(self):
        s = StyleSettings().normalized()
        self.assertEqual(s.page_size, "A4")
        self.assertEqual(s.orientation, "portrait")

    def test_numeric_clamping(self):
        s = StyleSettings(font_size_pt=999, line_height=0.1, margin_top=-5,
                          first_line_indent_em=99).normalized()
        self.assertEqual(s.font_size_pt, 24.0)
        self.assertEqual(s.line_height, 1.0)
        self.assertEqual(s.margin_top, 5.0)
        self.assertEqual(s.first_line_indent_em, 4.0)

    def test_invalid_enum_falls_back(self):
        s = StyleSettings(page_size="A99", page_number="everywhere",
                          text_align="middle", table_style="x", code_theme="neon").normalized()
        self.assertEqual(s.page_size, "A4")
        self.assertEqual(s.page_number, "none")
        self.assertEqual(s.text_align, "justify")
        self.assertEqual(s.table_style, "grid")
        self.assertEqual(s.code_theme, "light")

    def test_invalid_color_falls_back(self):
        s = StyleSettings(body_color="not-a-color").normalized()
        self.assertEqual(s.body_color, "#1a1a1a")

    def test_from_dict_ignores_garbage(self):
        s = StyleSettings.from_dict({"font_size_pt": "abc", "line_height": None,
                                     "unknown_field": 1, "page_number": "center"})
        self.assertEqual(s.font_size_pt, 10.5)
        self.assertEqual(s.line_height, 1.75)
        self.assertEqual(s.page_number, "center")

    def test_from_dict_handles_non_dict(self):
        self.assertEqual(StyleSettings.from_dict(None).page_size, "A4")
        self.assertEqual(StyleSettings.from_dict([1, 2]).page_size, "A4")

    def test_roundtrip(self):
        s = StyleSettings(body_font="SimSun", font_size_pt=12, page_number="right",
                          custom_css="p { color: red }")
        self.assertEqual(StyleSettings.from_dict(s.to_dict()), s)


class TestCss(unittest.TestCase):
    def test_page_block(self):
        css = build_css(StyleSettings(margin_top=25, margin_left=30))
        self.assertIn("size: A4 portrait", css)
        self.assertIn("margin: 25mm 18mm 20mm 30mm", css)

    def test_no_template_tokens_left(self):
        css = build_css(StyleSettings())
        self.assertNotIn("__FONT_SIZE__", css)
        self.assertNotIn("__TOC_TITLE__", css)

    def test_page_numbers(self):
        css = build_css(StyleSettings(page_number="center", page_number_format="n_of_total"))
        self.assertIn("@bottom-center", css)
        self.assertIn("counter(page)", css)
        self.assertIn("counter(pages)", css)

    def test_page_numbers_simple_format(self):
        css = build_css(StyleSettings(page_number="right", page_number_format="n"))
        self.assertIn("@bottom-right", css)
        self.assertIn("content: counter(page);", css)

    def test_page_numbers_ignored_when_unsupported(self):
        css = build_css(StyleSettings(page_number="center"), supports_margin_boxes=False)
        self.assertNotIn("@bottom-center", css)

    def test_header_and_first_page_override(self):
        css = build_css(StyleSettings(header_text='期中 复习"资料', page_number="center",
                                      header_on_first_page=False))
        self.assertIn("@top-center", css)
        self.assertIn('content: "期中 复习\\"资料"', css)
        self.assertIn("@page :first", css)

    def test_font_variables(self):
        css = build_css(StyleSettings(body_font="SimSun", heading_font="SimHei"))
        self.assertIn('--font-body: "SimSun"', css)
        self.assertIn('--font-heading: "SimHei"', css)
        self.assertIn("serif", css.split("--font-body:")[1].split(";")[0])

    def test_heading_font_falls_back_to_body(self):
        css = build_css(StyleSettings(body_font="Arial", heading_font=""))
        body_line = css.split("--font-body:")[1].split(";")[0]
        heading_line = css.split("--font-heading:")[1].split(";")[0]
        self.assertEqual(body_line.strip(), heading_line.strip())

    def test_custom_css_appended_last(self):
        css = build_css(StyleSettings(custom_css="body { color: #123456 }"))
        self.assertIn("自定义 CSS", css)
        self.assertTrue(css.rstrip().endswith("}"))
        self.assertLess(css.index("自定义 CSS"), len(css))

    def test_dark_code_theme(self):
        css = build_css(StyleSettings(code_theme="dark"))
        self.assertIn("--color-code-bg: #1f2430", css)

    def test_cascade_regressions_found_by_visual_review(self):
        css = build_css(StyleSettings())
        # pandoc 的打印样式把 table 设成 display:block，列宽会退化成内容宽
        self.assertIn("display: table", css)
        self.assertIn("overflow: visible", css)
        # 行内代码是 block container，会继承 text-indent，必须归零并原子化
        self.assertIn("display: inline-block", css)
        # pandoc 在脚注区自带 <hr>，会和我们加的 border-top 叠成两条线
        self.assertIn(".footnotes hr", css)

    def test_html_generated_rules_do_not_use_unsupported_functions(self):
        css = build_css(StyleSettings(page_number="center"))
        # target-counter/leader 在 Chromium 里不支持，写进样式表会整条规则失效
        self.assertNotIn("target-counter", css)
        self.assertNotIn("leader(", css)


class TestFontStacks(unittest.TestCase):
    def test_serif_detection(self):
        stack = font_stack("SimSun")
        self.assertTrue(stack.startswith('"SimSun"'))
        self.assertTrue(stack.rstrip().endswith("serif"))

    def test_sans_detection(self):
        stack = font_stack("Microsoft YaHei")
        self.assertTrue(stack.rstrip().endswith("sans-serif"))

    def test_mono_stack(self):
        stack = mono_stack("Consolas")
        self.assertTrue(stack.startswith('"Consolas"'))
        self.assertTrue(stack.rstrip().endswith("monospace"))

    def test_empty_font_gives_valid_stack(self):
        self.assertTrue(font_stack("").endswith("sans-serif"))


class TestPandocOptions(unittest.TestCase):
    def test_normalize(self):
        opts = PandocOptions(toc_depth=99, math="katex").normalized()
        self.assertEqual(opts.toc_depth, 6)
        self.assertEqual(opts.math, "mathml")

    def test_roundtrip(self):
        opts = PandocOptions(toc=True, extra_args=("--foo", "--bar=1"))
        self.assertEqual(PandocOptions.from_dict(opts.to_dict()), opts)

    def test_implicit_figures_defaults_on(self):
        self.assertTrue(PandocOptions().implicit_figures)
        # 旧配置文件没有这个字段时保持默认开启
        self.assertTrue(PandocOptions.from_dict({}).implicit_figures)
        self.assertFalse(PandocOptions.from_dict({"implicit_figures": False}).implicit_figures)
        self.assertFalse(PandocOptions(implicit_figures=False).normalized().implicit_figures)

    def test_highlight_for_dark_theme(self):
        self.assertEqual(pandoc_highlight_for("dark", "pygments"), "breezedark")
        self.assertEqual(pandoc_highlight_for("dark", "zenburn"), "zenburn")
        self.assertEqual(pandoc_highlight_for("light", "pygments"), "pygments")


class TestPresets(unittest.TestCase):
    def test_all_presets_normalize(self):
        self.assertIn("默认（通用）", preset_names())
        for name in preset_names():
            normalized = preset(name).normalized()
            self.assertIn(normalized.page_size, {"A4", "A5", "B5", "Letter", "Legal"})

    def test_unknown_preset_returns_default(self):
        self.assertEqual(preset("不存在的预设"), StyleSettings())


if __name__ == "__main__":
    unittest.main()
