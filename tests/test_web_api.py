"""Web 桥接层测试：不启动窗口，验证返回结构、扫描、设置往返与错误兜底。"""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

from md2pdf import config as config_module
from md2pdf.config import AppConfig
from md2pdf.services import ConversionService
from md2pdf.styles import PandocOptions, StyleSettings
from md2pdf.web_api import Api


class WebApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="md2pdf-webapi-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        original = config_module.config_path
        config_module.config_path = lambda: self.tmp / "settings.json"
        self.addCleanup(setattr, config_module, "config_path", original)

        self.cfg = AppConfig()
        self.service = ConversionService(
            preview_dir=self.tmp / "preview", attach_logging=False,
        )
        self.addCleanup(self.service.shutdown)
        self.api = Api(self.service, self.cfg)

    # ---------------- 选项表 ----------------

    def test_options_cover_every_style_field(self):
        options = self.api.options()
        style_fields = {f.name for f in dataclasses.fields(StyleSettings)}
        self.assertEqual(set(options["defaults"]["style"]), style_fields)
        pandoc_fields = {f.name for f in dataclasses.fields(PandocOptions)}
        self.assertEqual(set(options["defaults"]["pandoc"]), pandoc_fields)

        for key in ("page_sizes", "orientations", "page_numbers", "page_number_formats",
                    "text_aligns", "table_styles", "code_themes", "conflicts",
                    "math", "languages", "highlights", "font_sizes"):
            self.assertTrue(options[key], key)
            for item in options[key]:
                self.assertIn("value", item)
                self.assertIn("label", item)

        values = [item["value"] for item in options["orientations"]]
        self.assertEqual(values, ["portrait", "landscape"])
        self.assertIn("overwrite", [item["value"] for item in options["conflicts"]])

    def test_presets_expose_full_style(self):
        presets = self.api.presets()
        self.assertEqual(len(presets), 5)
        for item in presets:
            self.assertIsInstance(item["name"], str)
            self.assertEqual(set(item["style"]),
                             {f.name for f in dataclasses.fields(StyleSettings)})

    def test_environment_reports_structure(self):
        env = self.api.environment()
        self.assertIn("ok", env)
        self.assertIn("error", env)
        self.assertIn("preview_available", env)
        self.assertIsInstance(env["config_path"], str)
        if env["ok"]:
            self.assertTrue(env["pandoc"]["path"])
            self.assertIsInstance(env["browser"]["margin_boxes"], bool)
        else:
            self.assertTrue(env["error"])

    def test_fonts_is_list_of_strings(self):
        fonts = self.api.fonts()
        self.assertIsInstance(fonts, list)
        self.assertTrue(all(isinstance(name, str) and name for name in fonts))

    # ---------------- 扫描 ----------------

    def test_scan_filters_and_sorts_naturally(self):
        root = self.tmp / "docs"
        (root / "sub").mkdir(parents=True)
        for name in ("b.md", "a10.md", "a2.md", "note.txt", "skip.pdf"):
            (root / name).write_text("x", encoding="utf-8")
        (root / "sub" / "c.markdown").write_text("x", encoding="utf-8")

        items = self.api.scan([str(root)])
        names = [item["name"] for item in items]
        self.assertEqual(names, ["a2.md", "a10.md", "b.md", "note.txt", "c.markdown"])
        self.assertTrue(all(item["path"].lower().endswith((".md", ".txt", ".markdown"))
                            for item in items))
        self.assertTrue(all(item["size_text"] for item in items))

        # 重复加入同一目录不会重复返回
        again = self.api.scan([str(root), str(root / "a2.md")])
        self.assertEqual([item["name"] for item in again], names)

    def test_scan_ignores_unknown_input(self):
        self.assertEqual(self.api.scan([]), [])
        self.assertEqual(self.api.scan(None), [])
        self.assertEqual(self.api.scan(str(self.tmp / "nope")), [])

    # ---------------- 设置 ----------------

    def test_save_and_load_round_trip(self):
        result = self.api.save_settings({
            "style": {"page_size": "A5", "margin_top": 999, "font_size_pt": 14,
                      "body_color": "not-a-color"},
            "pandoc": {"toc": True, "toc_depth": 99},
            "output_dir": str(self.tmp),
            "conflict": "bogus",
            "pandoc_path": "",
            "browser_path": "",
            "pdf_info": True,
            "show_log": False,
        })
        self.assertTrue(result["ok"], result)

        loaded = self.api.load_settings()
        self.assertEqual(loaded["style"]["page_size"], "A5")
        self.assertEqual(loaded["style"]["margin_top"], 50.0)      # 夹紧
        self.assertEqual(loaded["style"]["font_size_pt"], 14.0)
        self.assertEqual(loaded["style"]["body_color"], "#1a1a1a")  # 非法颜色回默认
        self.assertEqual(loaded["pandoc"]["toc"], True)
        self.assertEqual(loaded["pandoc"]["toc_depth"], 6)          # 夹紧
        self.assertEqual(loaded["conflict"], "overwrite")           # 非法枚举回默认
        self.assertTrue(loaded["pdf_info"])
        self.assertFalse(loaded["show_log"])
        self.assertTrue((self.tmp / "settings.json").exists())

        # 重新加载配置文件（模拟重启）
        reloaded = AppConfig.load()
        self.assertEqual(reloaded.style.page_size, "A5")
        self.assertTrue(reloaded.pandoc.toc)

    def test_save_settings_tolerates_garbage(self):
        for payload in (None, {}, {"style": "not-a-dict"}, {"pandoc": []},
                        {"style": {"margin_top": "abc"}, "conflict": 42}):
            result = self.api.save_settings(payload)
            self.assertTrue(result["ok"], payload)

    # ---------------- 杂项 ----------------

    def test_open_path_missing_reports_error(self):
        result = self.api.open_path(str(self.tmp / "missing.pdf"))
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_open_path_empty_input(self):
        self.assertFalse(self.api.open_path("")["ok"])

    def test_dialogs_without_window_return_empty(self):
        self.assertEqual(self.api.pick_files(), [])
        self.assertEqual(self.api.pick_folder(), "")
        self.assertEqual(self.api.pick_exe(), "")

    def test_file_dialog_filters_are_valid(self):
        """pywebview 对过滤器有严格正则（描述里不能有斜杠等符号）。"""
        from md2pdf import web_api

        try:
            from webview.util import parse_file_type
        except ImportError:  # pragma: no cover
            self.skipTest("未安装 pywebview")

        for item in (*web_api.TEXT_FILE_FILTERS, *web_api.EXE_FILE_FILTERS):
            description, extensions = parse_file_type(item)
            self.assertTrue(description, item)
            self.assertTrue(extensions.startswith("*"), item)
        self.assertIn("*.md", web_api.TEXT_FILE_FILTERS[0])
        self.assertNotIn("/", web_api.TEXT_FILE_FILTERS[0])

    def test_pick_files_passes_filters_and_returns_paths(self):
        from md2pdf import web_api

        class FakeWindow:
            def __init__(self):
                self.calls = []
                self.result = (str(self_path),)

            def create_file_dialog(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return self.result

        self_path = self.tmp / "a.md"
        self_path.write_text("x", encoding="utf-8")
        fake = FakeWindow()
        self.api.bind(fake)

        result = self.api.pick_files()
        self.assertEqual(result, [str(self_path)])
        self.assertEqual(self.api.cfg.last_input_dir, str(self.tmp))
        _, kwargs = fake.calls[0]
        self.assertEqual(kwargs["file_types"], web_api.TEXT_FILE_FILTERS)
        self.assertTrue(kwargs["allow_multiple"])

    def test_pick_files_surfaces_dialog_error(self):
        class BrokenWindow:
            def create_file_dialog(self, *args, **kwargs):
                raise ValueError("bad filter")

        self.api.bind(BrokenWindow())
        with self.assertRaises(RuntimeError):
            self.api.pick_files()

    def test_job_api_returns_job_id(self):
        job_id = self.api.start_job({"files": []})
        self.assertIsInstance(job_id, str)
        self.assertTrue(job_id)
        polled = self.api.poll(job_id, 0, 0)
        self.assertEqual(polled["job_id"], job_id)
        self.assertTrue(any(event["type"] == "done" for event in polled["events"]))


if __name__ == "__main__":
    unittest.main()
