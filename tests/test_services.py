"""服务层测试：不依赖 pandoc / Edge，用假 Converter 验证事件、取消与预览。"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from md2pdf.converter import ConversionResult
from md2pdf.errors import CancelledError, ConversionError
from md2pdf.services import ConversionService
from md2pdf.tools import ToolInfo


class FakeConverter:
    """按 behavior 模拟各种转换结果。"""

    def __init__(self, behavior: str = "ok", *, pages: int = 3,
                 warnings: list[str] | None = None, delay: float = 0.0) -> None:
        self.behavior = behavior
        self.pages = pages
        self.warnings = list(warnings or [])
        self.delay = delay
        self.calls: list[Path] = []

    def convert(self, request, *, cancel=None, progress=None):
        self.calls.append(Path(request.source))
        if progress:
            progress("读取源文件")
            progress("调用 pandoc")
        if self.delay:
            time.sleep(self.delay)
        if self.behavior == "fail":
            raise ConversionError("pandoc 转换失败", detail="stderr", hint="检查文档结构。")
        if self.behavior == "unexpected":
            raise RuntimeError("boom")
        if self.behavior == "block":
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    raise CancelledError()
                time.sleep(0.01)
            raise AssertionError("测试中任务没有被取消")
        if self.behavior == "cancel-on-next":
            if cancel is not None and cancel.is_set():
                raise CancelledError()
        if self.behavior == "skip":
            return ConversionResult(source=Path(request.source), output=None, skipped=True,
                                    warnings=list(self.warnings))
        output = Path(request.output) if request.output else Path(request.source).with_suffix(".pdf")
        return ConversionResult(
            source=Path(request.source), output=output, pages=self.pages,
            duration=0.01, size_bytes=1234, warnings=list(self.warnings),
        )

    def cancel(self) -> None:
        pass


def fake_tools(pandoc_path=None, browser_path=None):
    return (
        ToolInfo("pandoc", Path("C:/fake/pandoc.exe"), "3.11", "测试"),
        ToolInfo("edge", Path("C:/fake/msedge.exe"), "156.0.0.0", "测试"),
    )


def failing_tools(pandoc_path=None, browser_path=None):
    from md2pdf.errors import ToolNotFoundError

    raise ToolNotFoundError("未找到 pandoc。请安装或在设置中指定路径。")


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="md2pdf-services-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.services: list[ConversionService] = []
        self.files = []
        for name in ("a.md", "b.md", "c.md"):
            path = self.tmp / name
            path.write_text("# 测试\n\n正文。\n", encoding="utf-8")
            self.files.append(str(path))

    def make_service(self, converter=None, *, finder=fake_tools) -> ConversionService:
        converter = converter or FakeConverter()
        service = ConversionService(
            tool_finder=finder,
            converter_factory=lambda *args, **kwargs: converter,
            preview_dir=self.tmp / "preview",
        )
        self.services.append(service)
        self.addCleanup(service.shutdown)
        return service

    def payload(self, **overrides) -> dict:
        data = {
            "files": list(self.files),
            "style": {},
            "pandoc": {},
            "output_dir": "",
            "conflict": "overwrite",
            "keep_temp": False,
            "add_pdf_info": False,
        }
        data.update(overrides)
        return data

    def wait_done(self, service: ConversionService, job_id: str,
                  *, cursor: int = 0, timeout: float = 8.0) -> tuple[list[dict], int, int]:
        events: list[dict] = []
        log_cursor = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = service.poll(job_id, cursor, log_cursor)
            events.extend(result["events"])
            cursor = result["cursor"]
            log_cursor = result["log_cursor"]
            if any(e.get("type") == "done" for e in events):
                return events, cursor, log_cursor
            time.sleep(0.02)
        self.fail(f"任务 {job_id} 在 {timeout}s 内没有结束；已收到事件：{events}")

    # ---------------- 事件序列 ----------------

    def test_event_sequence_for_successful_batch(self):
        service = self.make_service()
        job_id = service.start(self.payload(files=self.files[:2]))
        events, cursor, _ = self.wait_done(service, job_id)

        self.assertEqual(events[0]["type"], "queued")
        self.assertEqual(events[0]["total"], 2)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["ok"], 2)
        self.assertEqual(events[-1]["failed"], 0)
        self.assertFalse(events[-1]["cancelled"])

        rows = [e for e in events if e["type"] == "row"]
        self.assertEqual(len(rows), 4, rows)
        self.assertEqual([r["status"] for r in rows],
                         ["running", "done", "running", "done"])
        self.assertTrue(all(r["pages"] == 3 for r in rows if r["status"] == "done"))

        stages = [e for e in events if e["type"] == "stage"]
        self.assertGreaterEqual(len(stages), 4)
        self.assertIn("调用 pandoc", [s["text"] for s in stages])

        # cursor 之后没有新事件，且重复 poll 不重复返回
        again = service.poll(job_id, cursor)
        self.assertEqual(again["events"], [])
        self.assertFalse(again["running"])

    def test_poll_incremental_does_not_duplicate(self):
        converter = FakeConverter(delay=0.05)
        service = self.make_service(converter)
        job_id = service.start(self.payload(files=self.files[:1]))
        first = service.poll(job_id, 0)
        self.assertGreaterEqual(len(first["events"]), 1)
        second = service.poll(job_id, first["cursor"])
        ids = [id(e) for e in first["events"] + second["events"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.wait_done(service, job_id, cursor=second["cursor"])

    # ---------------- 失败与警告 ----------------

    def test_failure_event_carries_hint(self):
        service = self.make_service(FakeConverter("fail"))
        job_id = service.start(self.payload(files=self.files[:1]))
        events, _, _ = self.wait_done(service, job_id)
        errors = [e for e in events if e["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["message"], "pandoc 转换失败")
        self.assertEqual(errors[0]["hint"], "检查文档结构。")
        self.assertEqual(events[-1]["failed"], 1)
        self.assertEqual(events[-1]["ok"], 0)
        rows = [e for e in events if e["type"] == "row"]
        self.assertEqual(rows[-1]["status"], "failed")

    def test_unexpected_exception_is_reported(self):
        service = self.make_service(FakeConverter("unexpected"))
        job_id = service.start(self.payload(files=self.files[:1]))
        events, _, _ = self.wait_done(service, job_id)
        errors = [e for e in events if e["type"] == "error"]
        self.assertTrue(errors)
        self.assertIn("未预期错误", errors[0]["message"])
        self.assertEqual(events[-1]["failed"], 1)

    def test_skipped_and_warnings(self):
        service = self.make_service(FakeConverter("skip", warnings=["字体未安装…"]))
        job_id = service.start(self.payload(files=self.files[:1]))
        events, _, _ = self.wait_done(service, job_id)
        self.assertEqual(events[-1]["skipped"], 1)
        rows = [e for e in events if e["type"] == "row"]
        self.assertEqual(rows[-1]["status"], "skipped")
        warnings = [e for e in events if e["type"] == "warning"]
        self.assertEqual(warnings[0]["text"], "字体未安装…")

    def test_missing_tools_reports_error_and_done(self):
        service = self.make_service(finder=failing_tools)
        job_id = service.start(self.payload())
        events, _, _ = self.wait_done(service, job_id)
        self.assertEqual(events[0]["type"], "queued")
        self.assertTrue(any(e["type"] == "error" for e in events))
        self.assertIn("pandoc", events[-1]["error"])
        self.assertEqual(events[-1]["failed"], 1)

    # ---------------- 取消与并发保护 ----------------

    def test_cancel_running_job(self):
        converter = FakeConverter("block")
        service = self.make_service(converter)
        job_id = service.start(self.payload(files=self.files[:1]))

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if service.poll(job_id, 0)["running"] and converter.calls:
                break
            time.sleep(0.02)
        service.cancel(job_id)
        events, _, _ = self.wait_done(service, job_id)
        self.assertTrue(events[-1]["cancelled"])
        self.assertTrue(events[-1]["preview"] is False)
        rows = [e for e in events if e["type"] == "row"]
        self.assertEqual(rows[-1]["status"], "cancelled")

    def test_second_start_returns_busy_job(self):
        converter = FakeConverter("block")
        service = self.make_service(converter)
        first = service.start(self.payload(files=self.files[:1]))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not converter.calls:
            time.sleep(0.02)

        second = service.start(self.payload(files=self.files[:1]))
        self.assertNotEqual(first, second)
        events, _, _ = self.wait_done(service, second)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("已有转换任务", events[0]["message"])
        self.assertEqual(events[-1]["error"], "busy")
        self.assertTrue(service.poll(first, 0)["running"])
        service.cancel(first)
        self.wait_done(service, first)

    def test_empty_file_list_is_reported(self):
        service = self.make_service()
        job_id = service.start({"files": []})
        events, _, _ = self.wait_done(service, job_id)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[-1]["error"], "empty")

    def test_payload_is_normalized(self):
        """style/pandoc 里的脏数据交给 from_dict 兜底，不应抛异常。"""
        service = self.make_service()
        job_id = service.start(self.payload(
            files=self.files[:1],
            style={"page_size": "不存在的纸型", "margin_top": 999, "font_size_pt": "abc"},
            pandoc={"toc_depth": 99, "math": "nonsense"},
        ))
        events, _, _ = self.wait_done(service, job_id)
        self.assertEqual(events[-1]["ok"], 1)

    # ---------------- 日志缓冲 ----------------

    def test_log_buffer_and_incremental_read(self):
        from md2pdf.logger import get_logger

        service = self.make_service()
        log = get_logger("tests")
        log.warning("第一条日志")
        first = service.poll("", 0, 0)
        self.assertTrue(any("第一条日志" in item["text"] for item in first["logs"]))
        level = [item["level"] for item in first["logs"] if "第一条日志" in item["text"]][0]
        self.assertEqual(level, "warning")

        log.error("第二条日志")
        second = service.poll("", 0, first["log_cursor"])
        texts = [item["text"] for item in second["logs"]]
        self.assertIn("第二条日志", texts)
        self.assertNotIn("第一条日志", texts)

    # ---------------- 预览 ----------------

    def _write_blank_pdf(self, pages: int = 2) -> Path:
        try:
            from pypdf import PdfWriter
        except ImportError:  # pragma: no cover
            self.skipTest("缺少 pypdf")
        path = self.tmp / "blank.pdf"
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=300, height=400)
        with open(path, "wb") as handle:
            writer.write(handle)
        return path

    def test_preview_renders_pages(self):
        service = self.make_service()
        pdf = self._write_blank_pdf(2)
        opened = service.open_preview(str(pdf))
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["pages"], 2)

        first = service.preview_page(1, 300)
        self.assertTrue(first["ok"], first)
        self.assertTrue(first["png"].startswith("data:image/png;base64,"))
        self.assertGreater(first["width"], 0)
        self.assertGreater(first["height"], 0)

        second = service.preview_page(2, 300)
        self.assertEqual(second["page"], 2)

        # 越界夹紧
        clamped = service.preview_page(99, 300)
        self.assertEqual(clamped["page"], 2)
        self.assertEqual(service.preview_page(-5, 300)["page"], 1)

    def test_preview_scale_mode(self):
        service = self.make_service()
        pdf = self._write_blank_pdf(1)
        self.assertTrue(service.open_preview(str(pdf))["ok"])
        result = service.preview_page(1, 0, scale=1.5)
        self.assertTrue(result["ok"], result)
        self.assertGreater(result["width"], 300)

    def test_preview_without_document(self):
        service = self.make_service()
        result = service.preview_page(1, 300)
        self.assertFalse(result["ok"])
        self.assertIn("预览", result["error"])

    def test_preview_missing_file(self):
        service = self.make_service()
        result = service.open_preview(str(self.tmp / "nope.pdf"))
        self.assertFalse(result["ok"])

    def test_preview_job_targets_preview_dir(self):
        service = self.make_service()
        job_id = service.start(self.payload(files=self.files[:1], preview_only=True))
        events, _, _ = self.wait_done(service, job_id)
        self.assertTrue(events[0]["preview"])
        done = events[-1]
        self.assertTrue(done["preview"])
        self.assertEqual(done["ok"], 1)
        self.assertIn("preview", done["last_output"])
        self.assertTrue(Path(done["last_output"]).name.startswith("a-"))


if __name__ == "__main__":
    unittest.main()
