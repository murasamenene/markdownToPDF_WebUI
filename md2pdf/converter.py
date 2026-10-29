"""转换核心：Markdown -> HTML -> PDF。

这里刻意把每一步都拆开并做校验，因为 pandoc 和 Chromium 的失败方式大多是
“静默”的：CSS 路径错误会出无样式 PDF，浏览器加载失败会出一张错误页 PDF，
目标文件被占用会直接抛权限异常。宁可多花 50ms 校验，也不要交给用户一个坏文件。
"""

from __future__ import annotations

import html
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .errors import CancelledError, ConversionError, TargetLockedError, ToolNotFoundError
from .fonts import canonicalize_font, is_font_installed
from .logger import get_logger
from .postprocess import apply_pdf_info, extract_document_info, locate_headings
from .styles import PandocOptions, StyleSettings, build_css, pandoc_highlight_for
from .tools import PandocCapabilities, ToolInfo, pandoc_capabilities
from .utils import (
    ProcessRunner,
    ensure_dir,
    is_valid_pdf,
    looks_like_browser_error_page,
    new_token,
    path_to_uri,
    pdf_page_count,
    pdf_page_texts,
    read_text_file,
    remove_tree,
    safe_replace,
    truncate,
    unique_path,
)

log = get_logger("converter")

FORMAT_BY_SUFFIX = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdown": "markdown",
    ".mkd": "markdown",
    ".txt": "markdown",
    ".text": "markdown",
    ".rst": "rst",
    ".org": "org",
}
SUPPORTED_SUFFIXES = tuple(FORMAT_BY_SUFFIX)

CONFLICT_MODES = {"overwrite": "覆盖", "rename": "自动重命名", "skip": "跳过"}

MATH_LABELS = {"mathml": "MathML（离线）", "mathjax": "MathJax（需联网）", "none": "不处理"}

# pandoc 在设置 lang 时会尝试加载翻译文件；缺失时的告警与文档内容无关，直接忽略
_BENIGN_PANDOC_WARNINGS = ("Could not load translations", "has no translation defined")


@dataclass
class ConversionRequest:
    source: Path
    output: Path | None = None
    output_dir: Path | None = None
    style: StyleSettings = field(default_factory=StyleSettings)
    pandoc: PandocOptions = field(default_factory=PandocOptions)
    conflict: str = "overwrite"
    keep_temp: bool = False
    add_pdf_info: bool = False       # 实验功能：写入书签与文档元数据


@dataclass
class ConversionResult:
    source: Path
    output: Path | None
    pages: int | None = None
    duration: float = 0.0
    size_bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    temp_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return self.skipped or (self.output is not None and self.output.exists())


def resolve_output(source: Path, output_dir: Path | None = None, conflict: str = "overwrite") -> Path | None:
    """决定输出路径；冲突模式为 skip 且文件已存在时返回 None。"""
    source = Path(source)
    base = Path(output_dir) if output_dir else source.parent
    target = base / f"{source.stem}.pdf"
    if target.exists():
        if conflict == "skip":
            return None
        if conflict == "rename":
            return unique_path(target)
    return target


def build_pandoc_command(
    pandoc: Path,
    *,
    input_format: str,
    source: Path,
    head_file: Path,
    output_html: Path,
    style: StyleSettings,
    options: PandocOptions,
    document_title: str,
    math_method_supported: bool = False,
    syntax_highlighting_supported: bool = False,
) -> list[str]:
    # pandoc 的 implicit_figures：独占段落的图片会变成 <figure>，alt 文本成为图注。
    # 关闭时改用 markdown-implicit_figures 读取格式（只对 markdown 系输入有效）。
    if options.implicit_figures is False and input_format == "markdown":
        input_format = "markdown-implicit_figures"

    cmd: list[str] = [
        str(pandoc),
        "--from", input_format,
        "--to", "html5",
        "--standalone",
        "--metadata", f"pagetitle={document_title}",
        "--include-in-header", str(head_file),
        "--resource-path", str(Path(source).parent),
        "--output", str(output_html),
    ]
    if options.toc:
        cmd += ["--toc", f"--toc-depth={options.toc_depth}", "--metadata", "toc-title=目录"]
    if options.number_sections:
        cmd.append("--number-sections")
    if options.math == "mathml":
        cmd.append("--math-method=mathml" if math_method_supported else "--mathml")
    elif options.math == "mathjax":
        cmd.append("--math-method=mathjax" if math_method_supported else "--mathjax")
    if options.language:
        cmd += ["--metadata", f"lang={options.language}"]
    if options.citeproc:
        cmd.append("--citeproc")
    if options.embed_resources:
        cmd.append("--embed-resources")
    highlight = pandoc_highlight_for(style.code_theme, options.highlight_style)
    if syntax_highlighting_supported:
        cmd.append(f"--syntax-highlighting={highlight}")
    else:
        cmd.append(f"--highlight-style={highlight}")
    cmd.extend(options.extra_args)
    cmd.append(str(source))
    return cmd


def build_browser_command(
    browser: Path,
    *,
    html_file: Path,
    pdf_file: Path,
    profile_dir: Path,
    virtual_time_budget_ms: int = 10000,
) -> list[str]:
    return [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--font-render-hinting=none",
        "--disable-lcd-text",
        "--run-all-compositor-stages-before-draw",
        f"--user-data-dir={profile_dir}",
        f"--print-to-pdf={pdf_file}",
        "--no-pdf-header-footer",
        f"--virtual-time-budget={int(virtual_time_budget_ms)}",
        path_to_uri(html_file),
    ]


class Converter:
    """一次转换一个文件；同一实例可复用，内部串行。"""

    def __init__(
        self,
        pandoc: ToolInfo,
        browser: ToolInfo,
        *,
        timeout_pandoc: float = 120.0,
        timeout_browser: float = 180.0,
        virtual_time_budget_ms: int = 10000,
    ) -> None:
        if pandoc is None or browser is None:
            raise ToolNotFoundError("缺少 pandoc 或浏览器")
        self.pandoc = pandoc
        self.browser = browser
        self.timeout_pandoc = timeout_pandoc
        self.timeout_browser = timeout_browser
        self.virtual_time_budget_ms = virtual_time_budget_ms
        self._runner = ProcessRunner()
        self._current_temp_pdf: Path | None = None
        self._capabilities: PandocCapabilities | None = None

    @property
    def capabilities(self) -> PandocCapabilities:
        if self._capabilities is None:
            self._capabilities = pandoc_capabilities(self.pandoc.path)
        return self._capabilities

    # ---------------- 后处理 ----------------

    def _postprocess_pdf(self, pdf_path: Path, html_path: Path, warnings: list[str],
                         *, skip_toc_pages: bool = False) -> None:
        """补书签与元数据。任何失败都只记警告，不阻断转换。"""
        backup = pdf_path.with_name(pdf_path.name + ".bak")
        try:
            info = extract_document_info(html_path)
            # 没有标题就不必做全页文本提取（大文档上这是最贵的一步）
            outline = (
                locate_headings(
                    pdf_page_texts(pdf_path), info.headings, skip_toc_pages=skip_toc_pages
                )
                if info.headings
                else []
            )
            shutil.copy2(pdf_path, backup)
            ok, message = apply_pdf_info(pdf_path, info.meta, outline)
            if not ok or not is_valid_pdf(pdf_path):
                if backup.exists():
                    os.replace(backup, pdf_path)
                warnings.append(f"未能写入书签/元数据：{message}")
                log.warning("PDF 后处理失败：%s", message)
                return
            log.debug("PDF 后处理完成：%s", message)
        except Exception as exc:
            log.warning("PDF 后处理异常，已回滚：%s", exc)
            warnings.append(f"书签/元数据写入失败，已保留原 PDF：{exc}")
            try:
                if backup.exists():
                    os.replace(backup, pdf_path)
            except OSError:
                pass
        finally:
            try:
                backup.unlink()
            except OSError:
                pass

    # ---------------- 取消 ----------------

    def cancel(self) -> None:
        """从 GUI/另一线程调用，会杀掉正在跑的浏览器进程树。"""
        self._runner.cancel()

    def convert(
        self,
        request: ConversionRequest,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> ConversionResult:
        cancel = cancel or threading.Event()
        progress = progress or (lambda _msg: None)
        started = time.monotonic()

        source = Path(request.source)
        if not source.is_file():
            raise ConversionError(f"找不到输入文件：{source}")

        input_format = FORMAT_BY_SUFFIX.get(source.suffix.lower())
        if input_format is None:
            supported = " / ".join(sorted(set(SUPPORTED_SUFFIXES)))
            raise ConversionError(
                f"不支持的文件类型：{source.suffix or '（无扩展名）'}",
                hint=f"目前支持 {supported}。",
            )

        target = request.output or resolve_output(source, request.output_dir, request.conflict)
        if target is None:
            log.info("跳过（目标已存在）：%s", source.name)
            return ConversionResult(source=source, output=None, skipped=True,
                                    duration=time.monotonic() - started)

        target = Path(target)
        ensure_dir(target.parent)
        _check_writable(target.parent)

        style = request.style.normalized()
        options = request.pandoc.normalized()
        warnings = list(style.warnings(supports_margin_boxes=self.browser.supports_margin_boxes()))
        warnings.extend(_font_warnings(style))
        # 把用户输入的中文/别名解析成系统真实注册的字体名，减少静默回退
        style = _canonicalize_style_fonts(style)
        if options.math == "mathjax":
            warnings.append("已选择 MathJax，需要联网加载脚本；离线环境下公式可能不显示，建议改用 MathML。")

        job_dir = Path(tempfile.mkdtemp(prefix="md2pdf-"))
        temp_pdf = target.parent / f".{target.stem}.{new_token()}.pdf"
        self._current_temp_pdf = temp_pdf
        doc_html = job_dir / "doc.html"
        result = ConversionResult(source=source, output=target, warnings=warnings)

        try:
            _check_cancel(cancel)

            progress("读取源文件")
            text, encoding = read_text_file(source)
            if encoding not in ("utf-8", "utf-8-sig"):
                warnings.append(f"源文件编码为 {encoding}，已转换为 UTF-8 后再处理。")
            # 统一写成 UTF-8 副本：既消除 BOM/GBK 问题，也避免 pandoc 直接读到 GBK
            normalized_input = job_dir / f"input{_safe_suffix(source)}"
            normalized_input.write_text(text, encoding="utf-8")

            progress("生成样式")
            css = build_css(style, supports_margin_boxes=self.browser.supports_margin_boxes())
            head_file = job_dir / "head.html"
            base_href = html.escape(path_to_uri(source.parent, directory=True), quote=True)
            head_file.write_text(
                f'<base href="{base_href}" />\n<style>\n{css}\n</style>\n',
                encoding="utf-8",
            )

            _check_cancel(cancel)
            progress("调用 pandoc")
            pandoc_cmd = build_pandoc_command(
                self.pandoc.path,
                input_format=input_format,
                source=normalized_input,
                head_file=head_file,
                output_html=doc_html,
                style=style,
                options=options,
                document_title=source.stem,
                math_method_supported=self.capabilities.math_method,
                syntax_highlighting_supported=self.capabilities.syntax_highlighting,
            )
            proc = self._runner.run(
                pandoc_cmd, timeout=self.timeout_pandoc, cancel=cancel, what="pandoc"
            )
            if proc.returncode != 0:
                detail = truncate(proc.stderr or proc.stdout)
                raise ConversionError(
                    "pandoc 转换失败",
                    detail=detail,
                    hint="检查文档中是否有 pandoc 无法解析的结构，或缺失的图片文件。",
                )
            for line in (proc.stderr or "").splitlines():
                if "[WARNING]" not in line:
                    continue
                if any(marker in line for marker in _BENIGN_PANDOC_WARNINGS):
                    continue
                warnings.append(line.strip().replace("[WARNING]", "pandoc 警告："))
                break  # 只保留第一条，避免日志淹没

            if not doc_html.exists() or doc_html.stat().st_size < 100:
                raise ConversionError("pandoc 没有生成有效的 HTML", hint="请检查输入文件是否为空。")

            _check_cancel(cancel)
            progress("浏览器渲染 PDF")
            profile_dir = job_dir / "browser-profile"
            browser_cmd = build_browser_command(
                self.browser.path,
                html_file=doc_html,
                pdf_file=temp_pdf,
                profile_dir=profile_dir,
                virtual_time_budget_ms=self.virtual_time_budget_ms,
            )
            proc = self._runner.run(
                browser_cmd, timeout=self.timeout_browser, cancel=cancel, what="浏览器打印"
            )
            _check_cancel(cancel)

            if not temp_pdf.exists() or not is_valid_pdf(temp_pdf):
                detail = truncate(proc.stderr or proc.stdout)
                hint = (
                    "浏览器可能被其他实例占用（请关闭所有 Edge 窗口后重试）、"
                    "或输出目录不可写。"
                )
                raise ConversionError(
                    f"浏览器未生成有效的 PDF（退出码 {proc.returncode}）", detail=detail, hint=hint
                )
            if looks_like_browser_error_page(temp_pdf):
                raise ConversionError(
                    "浏览器渲染出的是一张错误页",
                    detail="PDF 首页包含浏览器错误提示文本。",
                    hint="这通常表示 HTML 文件未被正确加载，请保留临时文件并查看日志。",
                )

            if request.add_pdf_info:
                warnings.append("已启用实验功能：书签页码由标题文本匹配推算，可能不准确。")
                progress("写入书签与元数据（实验功能）")
                # 只有目录单独占页时才需要跳过目录页，否则首屏标题会被误跳
                self._postprocess_pdf(
                    temp_pdf, doc_html, warnings,
                    skip_toc_pages=bool(options.toc and style.toc_break),
                )

            result.pages = pdf_page_count(temp_pdf)
            result.size_bytes = temp_pdf.stat().st_size

            progress("写入目标文件")
            try:
                safe_replace(temp_pdf, target)
            except TargetLockedError:
                raise ConversionError(
                    f"无法写入 {target.name}：文件正被其他程序占用",
                    hint="请关闭正在查看该 PDF 的阅读器后重试。",
                ) from None

            result.output = target
            result.duration = time.monotonic() - started
            result.temp_dir = job_dir if request.keep_temp else None
            log.info(
                "完成 %s -> %s（%s 页，%.1f KB，%.1fs）",
                source.name, target.name, result.pages,
                result.size_bytes / 1024, result.duration,
            )
            return result

        except (CancelledError, ConversionError, ToolNotFoundError):
            _silent_unlink(temp_pdf)
            result.duration = time.monotonic() - started
            raise
        except Exception as exc:  # 未预期异常统一包装，避免 GUI 线程静默崩溃
            _silent_unlink(temp_pdf)
            log.exception("转换出现未预期错误：%s", source)
            raise ConversionError(
                f"转换 {source.name} 时出现未预期错误：{exc}",
                hint="详细信息已写入日志。",
            ) from exc
        finally:
            self._current_temp_pdf = None
            if not request.keep_temp:
                remove_tree(job_dir)
            else:
                log.info("已保留临时目录：%s", job_dir)


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _check_writable(directory: Path) -> None:
    try:
        fd, name = tempfile.mkstemp(dir=str(directory), prefix=".md2pdf-write-", suffix=".tmp")
        os.close(fd)
        os.unlink(name)
    except OSError as exc:
        raise ConversionError(
            f"输出目录不可写：{directory}",
            detail=str(exc),
            hint="请换一个输出目录，或检查文件夹权限。",
        ) from exc


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise CancelledError()


def _font_warnings(style: StyleSettings) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for label, name in (
        ("正文字体", style.body_font),
        ("标题字体", style.heading_font or style.body_font),
        ("等宽字体", style.mono_font),
    ):
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            if not is_font_installed(name):
                warnings.append(f"{label}“{name}”未在系统中找到，已回退到相近字体。")
        except Exception:  # 字体枚举失败不该阻断转换
            pass
    return warnings


def _canonicalize_style_fonts(style: StyleSettings) -> StyleSettings:
    changes: dict[str, str] = {}
    for key in ("body_font", "heading_font", "mono_font"):
        name = getattr(style, key)
        if not name:
            continue
        try:
            resolved = canonicalize_font(name)
        except Exception:
            continue
        if resolved and resolved != name:
            changes[key] = resolved
    return style.merged(**changes) if changes else style


def _safe_suffix(source: Path) -> str:
    suffix = source.suffix.lower()
    return suffix if suffix in FORMAT_BY_SUFFIX else ".md"


def _silent_unlink(path: Path) -> None:
    try:
        os.unlink(str(path))
    except OSError:
        pass
