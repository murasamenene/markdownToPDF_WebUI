"""服务层：把 Converter 包装成适合 Web 前端调用的后台任务。

设计要点
--------
1. 转换在后台线程串行执行，事件写进 ``JobState.events``；
   前端用 ``poll(job_id, cursor)`` 增量读取，避免跨线程操作 UI。
2. 这一层不依赖 pywebview，可以独立测试。
3. 所有跨线程共享的数据都在锁内读写；事件只包含 JSON 可序列化的值。
4. 同一时刻只允许一个转换任务；重复启动返回一个“忙”任务，而不是抛异常。
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import app_data_dir
from .converter import ConversionRequest, Converter
from .errors import CancelledError, ConversionError, Md2PdfError, ToolNotFoundError
from .logger import get_logger
from .preview import AVAILABLE as PREVIEW_AVAILABLE
from .preview import PdfPreview, PreviewError
from .styles import PandocOptions, StyleSettings
from .tools import ToolInfo, find_browser, find_pandoc
from .utils import new_token, remove_tree

log = get_logger("services")

ToolFinder = Callable[[str | None, str | None], tuple[ToolInfo, ToolInfo]]
ConverterFactory = Callable[..., Converter]

MAX_EVENTS_PER_JOB = 20000
MAX_RETAINED_JOBS = 4


@dataclass
class JobState:
    job_id: str
    running: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    converter: Converter | None = None
    last_output: Path | None = None
    preview: bool = False
    thread: threading.Thread | None = None


def describe_error(exc: BaseException) -> dict[str, str]:
    """把异常整理成前端可直接展示的三段式文案。"""
    if isinstance(exc, ConversionError):
        return {"message": exc.message, "detail": exc.detail, "hint": exc.hint}
    hint = ""
    if isinstance(exc, ToolNotFoundError):
        hint = "请在“高级”页手动指定 pandoc 或浏览器的可执行文件路径。"
    return {"message": str(exc) or exc.__class__.__name__, "detail": "", "hint": hint}


def _default_tool_finder(pandoc_path: str | None, browser_path: str | None) -> tuple[ToolInfo, ToolInfo]:
    return find_pandoc(pandoc_path or None), find_browser(browser_path or None)


def _default_converter_factory(
    pandoc: ToolInfo, browser: ToolInfo, *, timeout_pandoc: float = 120.0,
    timeout_browser: float = 180.0,
) -> Converter:
    return Converter(
        pandoc, browser,
        timeout_pandoc=timeout_pandoc, timeout_browser=timeout_browser,
    )


class _ServiceLogHandler(logging.Handler):
    """把 md2pdf 的日志记录进服务层缓冲区，供前端轮询显示。"""

    def __init__(self, service: "ConversionService") -> None:
        super().__init__(level=logging.INFO)
        self._service = service
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._service._append_log(record.levelno, self.format(record))
        except Exception:  # 日志失败绝不能影响主流程
            pass


class ConversionService:
    """任务生命周期、事件缓冲、取消与 PDF 预览。"""

    LOG_LIMIT = 3000

    def __init__(
        self,
        *,
        tool_finder: ToolFinder | None = None,
        converter_factory: ConverterFactory | None = None,
        preview_dir: Path | str | None = None,
        attach_logging: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, JobState] = {}
        self._active_id: str | None = None
        self._tool_finder = tool_finder or _default_tool_finder
        self._converter_factory = converter_factory or _default_converter_factory
        self._preview_dir = Path(preview_dir) if preview_dir else Path(app_data_dir()) / "preview"

        self._preview: PdfPreview | None = None
        self._preview_path: Path | None = None
        self._preview_lock = threading.Lock()

        self._log_records: deque[dict[str, Any]] = deque(maxlen=self.LOG_LIMIT)
        self._log_next_seq = 1

        self._log_handler: _ServiceLogHandler | None = None
        if attach_logging:
            self._log_handler = _ServiceLogHandler(self)
            logging.getLogger("md2pdf").addHandler(self._log_handler)

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------

    def start(self, payload: dict[str, Any] | None) -> str:
        """立即返回 job_id；真正的转换在后台线程里跑。"""
        data = payload if isinstance(payload, dict) else {}
        files = [str(f) for f in (data.get("files") or []) if str(f).strip()]
        preview = bool(data.get("preview_only"))

        with self._lock:
            busy = self._active_id is not None
            job = JobState(job_id=new_token(), preview=preview)
            self._jobs[job.job_id] = job
            self._prune_jobs()
            if not busy and files:
                self._active_id = job.job_id

        if busy:
            # 不抛异常：前端从事件里看到提示即可
            self._emit(job, {"type": "error", "message": "已有转换任务在进行中，请等待完成或先取消。",
                             "hint": "", "detail": ""})
            self._finish(job, started=time.monotonic(), ok=0, failed=1, skipped=0,
                         cancelled=False, error="busy", total=0)
            return job.job_id

        if not files:
            self._emit(job, {"type": "error", "message": "没有需要转换的文件。",
                             "hint": "请先添加 Markdown 文本文件。", "detail": ""})
            self._finish(job, started=time.monotonic(), ok=0, failed=1, skipped=0,
                         cancelled=False, error="empty", total=0)
            return job.job_id

        self._emit(job, {"type": "queued", "total": len(files), "preview": preview})
        job.thread = threading.Thread(
            target=self._run_job, args=(job, data, files, preview),
            name=f"md2pdf-job-{job.job_id}", daemon=True,
        )
        job.thread.start()
        return job.job_id

    def poll(self, job_id: str | None, cursor: int = 0, log_cursor: int = 0) -> dict[str, Any]:
        """返回自 cursor 之后的新事件，以及新的日志记录。"""
        cursor = _as_int(cursor, 0)
        log_cursor = _as_int(log_cursor, 0)
        with self._lock:
            job = self._jobs.get(str(job_id)) if job_id else None
            if job is None:
                events: list[dict[str, Any]] = []
                new_cursor = 0
                running = False
                preview = False
                last_output = ""
            else:
                start = max(0, min(cursor, len(job.events)))
                events = list(job.events[start:])
                new_cursor = len(job.events)
                running = job.running
                preview = job.preview
                last_output = str(job.last_output) if job.last_output else ""
            logs, new_log_cursor = self._logs_since(log_cursor)
        return {
            "job_id": str(job_id or ""),
            "cursor": new_cursor,
            "running": running,
            "preview": preview,
            "last_output": last_output,
            "events": events,
            "log_cursor": new_log_cursor,
            "logs": logs,
        }

    def cancel(self, job_id: str | None = None) -> None:
        """置位取消标志并杀掉正在运行的浏览器进程树。"""
        with self._lock:
            job = self._jobs.get(str(job_id)) if job_id else None
            if job is None and self._active_id:
                job = self._jobs.get(self._active_id)
            if job is None:
                return
            job.cancel.set()
            converter = job.converter
        if converter is not None:
            try:
                converter.cancel()
            except Exception:
                log.debug("取消时终止进程失败", exc_info=True)

    @property
    def active_job_id(self) -> str | None:
        with self._lock:
            return self._active_id

    # ------------------------------------------------------------------
    # 任务线程
    # ------------------------------------------------------------------

    def _run_job(self, job: JobState, data: dict[str, Any],
                 files: list[str], preview: bool) -> None:
        started = time.monotonic()
        ok = failed = skipped = 0
        cancelled = False
        error_text = ""
        last_output: Path | None = None
        total = len(files)
        try:
            style = StyleSettings.from_dict(data.get("style"))
            options = PandocOptions.from_dict(data.get("pandoc"))
            output_dir_text = str(data.get("output_dir") or "").strip()
            output_dir = Path(output_dir_text) if output_dir_text else None
            conflict = str(data.get("conflict") or "overwrite")
            keep_temp = bool(data.get("keep_temp"))
            add_pdf_info = bool(data.get("add_pdf_info"))
            timeout_pandoc = _clamp(_as_float(data.get("timeout_pandoc"), 120.0), 10.0, 600.0)
            timeout_browser = _clamp(_as_float(data.get("timeout_browser"), 180.0), 10.0, 900.0)

            try:
                pandoc, browser = self._tool_finder(
                    str(data.get("pandoc_path") or "").strip() or None,
                    str(data.get("browser_path") or "").strip() or None,
                )
                converter = self._converter_factory(
                    pandoc, browser,
                    timeout_pandoc=timeout_pandoc, timeout_browser=timeout_browser,
                )
            except Md2PdfError as exc:
                error_text = str(exc)
                failed += 1
                self._emit(job, {"type": "error", **describe_error(exc)})
                return

            with self._lock:
                job.converter = converter

            if preview:
                self._clean_preview_dir()

            for index, raw in enumerate(files, 1):
                if job.cancel.is_set():
                    cancelled = True
                    break
                source = Path(raw)
                self._emit(job, {
                    "type": "row", "file": str(source), "status": "running",
                    "index": index, "total": total, "kind": "preview" if preview else "convert",
                })

                try:
                    request = self._build_request(
                        source, preview=preview, style=style, options=options,
                        output_dir=output_dir, conflict=conflict,
                        keep_temp=keep_temp, add_pdf_info=add_pdf_info,
                    )
                    result = converter.convert(
                        request,
                        cancel=job.cancel,
                        progress=lambda stage, name=source.name: self._emit(job, {
                            "type": "stage", "file": str(source), "text": str(stage),
                            "index": index, "total": total,
                        }),
                    )
                except CancelledError:
                    self._emit(job, {"type": "row", "file": str(source), "status": "cancelled",
                                     "index": index, "total": total})
                    cancelled = True
                    break
                except Md2PdfError as exc:
                    failed += 1
                    self._emit(job, {"type": "row", "file": str(source), "status": "failed",
                                     "index": index, "total": total})
                    self._emit(job, {"type": "error", "file": str(source),
                                     "index": index, "total": total, **describe_error(exc)})
                    continue
                except Exception as exc:  # 兜底：后台线程绝不能静默消失
                    failed += 1
                    log.exception("转换 %s 时出现未预期错误", source)
                    self._emit(job, {"type": "row", "file": str(source), "status": "failed",
                                     "index": index, "total": total})
                    self._emit(job, {
                        "type": "error", "file": str(source), "index": index, "total": total,
                        "message": f"转换 {source.name} 时出现未预期错误：{exc}",
                        "detail": "", "hint": "详细信息已写入日志。",
                    })
                    continue

                if result.skipped:
                    skipped += 1
                    self._emit(job, {"type": "row", "file": str(source), "status": "skipped",
                                     "index": index, "total": total})
                else:
                    ok += 1
                    last_output = result.output
                    self._emit(job, {
                        "type": "row", "file": str(source), "status": "done",
                        "pages": int(result.pages or 0),
                        "output": str(result.output) if result.output else "",
                        "index": index, "total": total,
                    })
                for warning in result.warnings:
                    self._emit(job, {"type": "warning", "file": str(source), "text": str(warning)})

                if job.cancel.is_set():
                    cancelled = True
                    break
        except Exception as exc:  # tool_finder/参数解析等外围错误
            error_text = str(exc)
            failed = max(failed, 1)
            log.exception("任务初始化失败")
            self._emit(job, {"type": "error", "message": error_text, "detail": "", "hint": ""})
        finally:
            with self._lock:
                job.converter = None
            self._finish(job, started=started, ok=ok, failed=failed, skipped=skipped,
                         cancelled=cancelled, error=error_text, total=total,
                         last_output=last_output)

    def _build_request(self, source: Path, *, preview: bool, style: StyleSettings,
                       options: PandocOptions, output_dir: Path | None, conflict: str,
                       keep_temp: bool, add_pdf_info: bool) -> ConversionRequest:
        if preview:
            self._preview_dir.mkdir(parents=True, exist_ok=True)
            output = self._preview_dir / f"{source.stem}-{new_token()}.pdf"
            return ConversionRequest(
                source=source, output=output, style=style, pandoc=options,
                conflict="overwrite", keep_temp=keep_temp, add_pdf_info=add_pdf_info,
            )
        return ConversionRequest(
            source=source, output_dir=output_dir, style=style, pandoc=options,
            conflict=conflict, keep_temp=keep_temp, add_pdf_info=add_pdf_info,
        )

    def _finish(self, job: JobState, *, started: float, ok: int, failed: int,
                skipped: int, cancelled: bool, error: str, total: int,
                last_output: Path | None = None) -> None:
        elapsed = max(0.0, time.monotonic() - started)
        with self._lock:
            job.running = False
            if last_output is not None:
                job.last_output = Path(last_output)
        self._emit(job, {
            "type": "done", "ok": ok, "failed": failed, "skipped": skipped,
            "total": total, "elapsed": round(elapsed, 2), "cancelled": bool(cancelled),
            "preview": job.preview, "error": error,
            "last_output": str(job.last_output) if job.last_output else "",
        })
        if self._active_id == job.job_id:
            with self._lock:
                if self._active_id == job.job_id:
                    self._active_id = None

    def _emit(self, job: JobState, event: dict[str, Any]) -> None:
        with self._lock:
            # done 是恢复按钮的关键事件，即使达到上限也必须保留
            if len(job.events) >= MAX_EVENTS_PER_JOB and event.get("type") != "done":
                return
            job.events.append(event)

    def _prune_jobs(self) -> None:
        if len(self._jobs) <= MAX_RETAINED_JOBS:
            return
        finished = [j for j in self._jobs.values() if not j.running]
        for job in finished[: max(0, len(self._jobs) - MAX_RETAINED_JOBS)]:
            self._jobs.pop(job.job_id, None)

    # ------------------------------------------------------------------
    # 日志缓冲
    # ------------------------------------------------------------------

    def _append_log(self, levelno: int, text: str) -> None:
        with self._lock:
            seq = self._log_next_seq
            self._log_next_seq += 1
            self._log_records.append({
                "seq": seq,
                "level": _level_name(levelno),
                "text": text,
            })

    def _logs_since(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            if not self._log_records:
                return [], self._log_next_seq - 1
            first = self._log_records[0]["seq"]
            if cursor < first - 1:
                cursor = first - 1
            start = max(0, cursor - (first - 1))
            logs = list(self._log_records)[start:]
            return logs, self._log_next_seq - 1

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------

    def open_preview(self, pdf_path: str) -> dict[str, Any]:
        path = Path(str(pdf_path or ""))
        if not path.is_file():
            return {"ok": False, "error": f"找不到 PDF：{path}", "pages": 0}
        if not PREVIEW_AVAILABLE:
            return {"ok": False, "error": "缺少 pypdfium2，无法预览 PDF。", "pages": 0}
        with self._preview_lock:
            self._close_preview_locked()
            try:
                preview = PdfPreview(path)
            except PreviewError as exc:
                return {"ok": False, "error": str(exc), "pages": 0}
            self._preview = preview
            self._preview_path = path
            return {"ok": True, "pages": preview.page_count, "path": str(path), "page": 1}

    def preview_page(self, page: int, width: int = 900,
                     scale: float | None = None) -> dict[str, Any]:
        """page 为 1 起始；超出范围时夹紧到首/末页。"""
        with self._preview_lock:
            preview = self._preview
            if preview is None:
                return {"ok": False, "error": "还没有打开预览。", "pages": 0}
            total = preview.page_count
            if total <= 0:
                return {"ok": False, "error": "PDF 没有页面。", "pages": 0}
            index = max(1, min(total, _as_int(page, 1))) - 1
            try:
                if scale is not None:
                    png, px_w, px_h = preview.render_png(
                        index, scale=_clamp(_as_float(scale, 1.0), 0.1, 4.0)
                    )
                else:
                    target = _clamp(_as_float(width, 900.0), 120.0, 4000.0)
                    png, px_w, px_h = preview.render_png(index, target_width_px=target)
            except PreviewError as exc:
                return {"ok": False, "error": str(exc), "pages": total}
            except Exception as exc:
                log.exception("渲染预览失败")
                return {"ok": False, "error": f"渲染失败：{exc}", "pages": total}
            return {
                "ok": True,
                "page": index + 1,
                "pages": total,
                "width": px_w,
                "height": px_h,
                "png": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            }

    def close_preview(self) -> None:
        with self._preview_lock:
            self._close_preview_locked()

    def _close_preview_locked(self) -> None:
        if self._preview is not None:
            try:
                self._preview.close()
            except Exception:
                pass
        self._preview = None
        self._preview_path = None

    def _clean_preview_dir(self) -> None:
        try:
            if self._preview_dir.is_dir():
                for item in self._preview_dir.iterdir():
                    if item.is_file():
                        try:
                            item.unlink()
                        except OSError:
                            pass
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """取消任务、关闭预览、摘掉日志 handler。可重复调用。"""
        self.cancel()
        with self._lock:
            job = self._jobs.get(self._active_id) if self._active_id else None
            thread = job.thread if job else None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self.close_preview()
        self._clean_preview_dir()
        try:
            remove_tree(self._preview_dir)
        except OSError:
            pass
        if self._log_handler is not None:
            try:
                logging.getLogger("md2pdf").removeHandler(self._log_handler)
            except Exception:
                pass
            self._log_handler = None


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _level_name(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    return "info"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))
