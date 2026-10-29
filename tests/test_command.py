"""命令行构造的纯单元测试（不依赖 pandoc/Edge，可离线运行）。

重点是参数版本兼容：pandoc 3.9 起用 --math-method，3.11 起用
--syntax-highlighting；旧版本要回退到 --mathml / --highlight-style，
否则日志里会一直出现弃用警告。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from md2pdf.converter import build_pandoc_command
from md2pdf.styles import PandocOptions, StyleSettings


def build(**overrides) -> list[str]:
    params = dict(
        pandoc=Path("pandoc"),
        input_format="markdown",
        source=Path("C:/docs/a.md"),
        head_file=Path("C:/tmp/head.html"),
        output_html=Path("C:/tmp/doc.html"),
        style=StyleSettings(),
        options=PandocOptions(),
        document_title="a",
    )
    params.update(overrides)
    return build_pandoc_command(**params)


class TestBuildPandocCommand(unittest.TestCase):
    def test_modern_flags_are_used_when_supported(self):
        cmd = build(math_method_supported=True, syntax_highlighting_supported=True)
        self.assertIn("--math-method=mathml", cmd)
        self.assertIn("--syntax-highlighting=pygments", cmd)
        self.assertFalse(any(c.startswith("--highlight-style=") for c in cmd))
        self.assertNotIn("--mathml", cmd)

    def test_legacy_flags_are_used_on_old_pandoc(self):
        cmd = build(math_method_supported=False, syntax_highlighting_supported=False)
        self.assertIn("--mathml", cmd)
        self.assertIn("--highlight-style=pygments", cmd)
        self.assertFalse(any(c.startswith("--syntax-highlighting=") for c in cmd))

    def test_dark_theme_switches_highlight_theme(self):
        cmd = build(style=StyleSettings(code_theme="dark"),
                    syntax_highlighting_supported=True)
        self.assertIn("--syntax-highlighting=breezedark", cmd)

    def test_mathjax_and_none(self):
        self.assertIn("--math-method=mathjax", build(
            options=PandocOptions(math="mathjax"), math_method_supported=True))
        self.assertFalse(any("math" in c for c in build(
            options=PandocOptions(math="none"), math_method_supported=True)))

    def test_toc_and_numbering(self):
        cmd = build(options=PandocOptions(toc=True, toc_depth=2, number_sections=True))
        self.assertIn("--toc", cmd)
        self.assertIn("--toc-depth=2", cmd)
        self.assertIn("--number-sections", cmd)
        self.assertIn("toc-title=目录", cmd)

    def test_implicit_figures_can_be_disabled(self):
        default_cmd = build()
        self.assertEqual(default_cmd[default_cmd.index("--from") + 1], "markdown")

        disabled = build(options=PandocOptions(implicit_figures=False))
        self.assertEqual(disabled[disabled.index("--from") + 1],
                         "markdown-implicit_figures")

        # 非 markdown 输入（rst/org）不受影响
        rst = build(input_format="rst", options=PandocOptions(implicit_figures=False))
        self.assertEqual(rst[rst.index("--from") + 1], "rst")

    def test_metadata_and_header_injection(self):
        cmd = build(document_title="文档")
        self.assertIn("pagetitle=文档", cmd)
        index = cmd.index("--include-in-header")
        self.assertEqual(cmd[index + 1], str(Path("C:/tmp/head.html")))

    def test_embed_resources_keeps_standalone(self):
        cmd = build(options=PandocOptions(embed_resources=True))
        self.assertIn("--embed-resources", cmd)
        self.assertEqual(cmd.count("--standalone"), 1)

    def test_extra_args_come_before_input(self):
        cmd = build(options=PandocOptions(extra_args=("--foo", "--bar=1")))
        self.assertIn("--foo", cmd)
        self.assertIn("--bar=1", cmd)
        self.assertEqual(cmd[-1], str(Path("C:/docs/a.md")))
        self.assertLess(cmd.index("--foo"), cmd.index(str(Path("C:/docs/a.md"))))

    def test_resource_path_uses_source_dir(self):
        cmd = build()
        index = cmd.index("--resource-path")
        self.assertEqual(cmd[index + 1], str(Path("C:/docs")))


if __name__ == "__main__":
    unittest.main()
