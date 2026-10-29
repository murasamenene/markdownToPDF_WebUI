"""pywebview 的 js_api 薄桥接层。

原则：
- 每个方法都必须快速返回；耗时工作交给 ConversionService 的后台线程。
- 返回值只能是 JSON 可序列化的普通值（dict/list/str/number/bool/None）。
- 任何可预期错误都转成 ``{"ok": False, "error": "..."}``，不把异常抛给 JS。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import APP_TITLE, __version__
from .config import AppConfig, config_path, log_dir
from .converter import CONFLICT_MODES, MATH_LABELS, SUPPORTED_SUFFIXES
from .errors import Md2PdfError
from .fonts import list_fonts
from .logger import get_logger
from .preview import AVAILABLE as PREVIEW_AVAILABLE
from .preview import UNAVAILABLE_REASON
from .services import ConversionService
from .styles import (
    CODE_THEMES,
    FONT_SIZE_CHOICES,
    ORIENTATIONS,
    PAGE_NUMBER_FORMATS,
    PAGE_NUMBER_MODES,
    PAGE_SIZES,
    TABLE_STYLES,
    TEXT_ALIGNS,
    PandocOptions,
    StyleSettings,
    preset_names,
    PRESETS,
)
from .tools import find_browser, find_pandoc
from .utils import format_size, iter_files
from .utils import open_path as open_system_path

log = get_logger("web_api")

HIGHLIGHT_STYLES = [
    "pygments", "tango", "espresso", "zenburn", "kate",
    "monochrome", "breezedark", "haddock",
]
LANGUAGES = [
    ("", "不设置（推荐）"),
    ("zh-CN", "简体中文 (zh-CN)"),
    ("zh-TW", "繁體中文 (zh-TW)"),
    ("en", "English (en)"),
]
MAX_SCAN_FILES = 2000

# pywebview 对过滤器格式有严格正则：描述里只能出现字母/数字/下划线/空格/中文，
# 不能有 "/" 等符号，否则 create_file_dialog 会直接抛 ValueError。
_TEXT_PATTERNS = ";".join("*" + suffix for suffix in SUPPORTED_SUFFIXES)
TEXT_FILE_FILTERS = (f"Markdown 与文本 ({_TEXT_PATTERNS})", "所有文件 (*.*)")
EXE_FILE_FILTERS = ("可执行文件 (*.exe)", "所有文件 (*.*)")


class Api:
    """暴露给 JS 的方法集合。"""

    def __init__(self, service: ConversionService, config: AppConfig) -> None:
        self.service = service
        self.cfg = config
        self._window = None
        self._env_cache: dict[str, dict[str, Any]] = {}
        self._fonts_cache: list[str] | None = None

    def bind(self, window) -> None:
        self._window = window

    # ------------------------------------------------------------------
    # 环境与选项
    # ------------------------------------------------------------------

    def environment(self, pandoc_path: str = "", browser_path: str = "",
                    refresh: bool = False) -> dict[str, Any]:
        pandoc_path = str(pandoc_path or "").strip()
        browser_path = str(browser_path or "").strip()
        key = f"{pandoc_path}|{browser_path}"
        if not refresh and key in self._env_cache:
            return self._env_cache[key]

        pandoc = browser = None
        errors: list[str] = []
        try:
            pandoc = find_pandoc(pandoc_path or self.cfg.pandoc_path or None)
        except Md2PdfError as exc:
            errors.append(str(exc))
        try:
            browser = find_browser(browser_path or self.cfg.browser_path or None)
        except Md2PdfError as exc:
            errors.append(str(exc))

        result: dict[str, Any] = {
            "ok": bool(pandoc and browser),
            "error": "\n".join(errors),
            "version": __version__,
            "app_title": APP_TITLE,
            "pandoc": {
                "path": str(pandoc.path), "version": pandoc.version, "source": pandoc.source,
            } if pandoc else None,
            "browser": {
                "path": str(browser.path), "version": browser.version, "kind": browser.kind,
                "source": browser.source, "margin_boxes": browser.supports_margin_boxes(),
                "label": {"edge": "Microsoft Edge", "chrome": "Google Chrome",
                          "chromium": "Chromium"}.get(browser.kind, browser.kind),
            } if browser else None,
            "preview_available": PREVIEW_AVAILABLE,
            "preview_reason": UNAVAILABLE_REASON,
            "config_path": str(config_path()),
            "log_dir": str(log_dir()),
        }
        self._env_cache[key] = result
        return result

    def options(self) -> dict[str, Any]:
        """把 Python 侧的枚举表交给前端，避免 JS 里再抄一份。"""
        def pairs(mapping: dict[str, str]) -> list[dict[str, str]]:
            return [{"value": str(k), "label": str(v)} for k, v in mapping.items()]

        sizes = [{"value": value, "label": label} for value, label in PAGE_SIZES.items()]
        sizes_labels = {"A4": "A4（210×297mm）", "A5": "A5（148×210mm）",
                        "B5": "B5（176×250mm）", "Letter": "Letter（8.5×11in）",
                        "Legal": "Legal（8.5×14in）"}
        for item in sizes:
            item["label"] = sizes_labels.get(item["value"], item["label"])

        font_sizes = [
            {"value": pt, "label": name} for name, pt in FONT_SIZE_CHOICES.items()
        ]
        return {
            "page_sizes": sizes,
            "orientations": pairs(ORIENTATIONS),
            "page_numbers": pairs(PAGE_NUMBER_MODES),
            "page_number_formats": pairs(PAGE_NUMBER_FORMATS),
            "text_aligns": pairs(TEXT_ALIGNS),
            "table_styles": pairs(TABLE_STYLES),
            "code_themes": pairs(CODE_THEMES),
            "conflicts": pairs(CONFLICT_MODES),
            "math": pairs(MATH_LABELS),
            "languages": [{"value": v, "label": label} for v, label in LANGUAGES],
            "highlights": [{"value": v, "label": v} for v in HIGHLIGHT_STYLES],
            "font_sizes": font_sizes,
            "supported_suffixes": list(SUPPORTED_SUFFIXES),
            "defaults": {
                "style": StyleSettings().to_dict(),
                "pandoc": PandocOptions().to_dict(),
            },
            "limits": {
                "margin": [5, 50], "font_size": [6, 24], "line_height": [1.0, 3.0],
                "indent": [0, 4], "paragraph_gap": [0, 2], "heading_scale": [0.8, 1.6],
                "toc_depth": [1, 6], "timeout_pandoc": [10, 600], "timeout_browser": [10, 900],
            },
        }

    def presets(self) -> list[dict[str, Any]]:
        return [{"name": name, "style": PRESETS[name].to_dict()} for name in preset_names()]

    def fonts(self) -> list[str]:
        if self._fonts_cache is None:
            try:
                self._fonts_cache = list(list_fonts())
            except Exception:
                log.exception("枚举系统字体失败")
                self._fonts_cache = []
        return self._fonts_cache

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def load_settings(self) -> dict[str, Any]:
        cfg = self.cfg
        return {
            "style": cfg.style.to_dict(),
            "pandoc": cfg.pandoc.to_dict(),
            "output_dir": cfg.output_dir,
            "conflict": cfg.conflict,
            "pandoc_path": cfg.pandoc_path,
            "browser_path": cfg.browser_path,
            "pdf_info": cfg.pdf_info,
            "show_log": cfg.show_log,
            "preview_available": PREVIEW_AVAILABLE,
            "preview_reason": UNAVAILABLE_REASON,
        }

    def save_settings(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data if isinstance(data, dict) else {}
        cfg = self.cfg
        cfg.style = StyleSettings.from_dict(data.get("style"))
        cfg.pandoc = PandocOptions.from_dict(data.get("pandoc"))
        cfg.output_dir = str(data.get("output_dir") or "")[:1000]
        conflict = str(data.get("conflict") or "overwrite")
        cfg.conflict = conflict if conflict in CONFLICT_MODES else "overwrite"
        cfg.pandoc_path = str(data.get("pandoc_path") or "")[:1000]
        cfg.browser_path = str(data.get("browser_path") or "")[:1000]
        cfg.pdf_info = bool(data.get("pdf_info"))
        cfg.show_log = bool(data.get("show_log", True))
        if cfg.output_dir:
            cfg.last_output_dir = cfg.output_dir
        try:
            cfg.save()
        except Exception as exc:
            log.warning("保存设置失败：%s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------

    def scan(self, paths: Any) -> list[dict[str, Any]]:
        if isinstance(paths, (str, Path)):
            paths = [paths]
        if not isinstance(paths, (list, tuple)):
            return []
        raw = [Path(str(p)) for p in paths if str(p or "").strip()]
        try:
            files = iter_files(raw, SUPPORTED_SUFFIXES)
        except OSError as exc:
            log.warning("扫描文件失败：%s", exc)
            return []
        files.sort(key=lambda p: (str(p.parent).casefold(), _natural_key(p.name)))
        if len(files) > MAX_SCAN_FILES:
            log.warning("文件过多，只保留前 %d 个（共 %d 个）", MAX_SCAN_FILES, len(files))
            files = files[:MAX_SCAN_FILES]
        result: list[dict[str, Any]] = []
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            result.append({
                "path": str(path),
                "name": path.name,
                "dir": str(path.parent),
                "size": size,
                "size_text": format_size(size),
            })
        return result

    def pick_files(self) -> list[str]:
        window = self._window
        if window is None:
            return []
        try:
            from webview import FileDialog
        except ImportError:  # pragma: no cover
            return []
        try:
            selected = window.create_file_dialog(
                FileDialog.OPEN,
                directory=self.cfg.last_input_dir or "",
                allow_multiple=True,
                file_types=TEXT_FILE_FILTERS,
            )
        except Exception as exc:
            log.exception("打开文件对话框失败")
            raise RuntimeError(f"打开文件对话框失败：{exc}") from exc
        paths = [str(p) for p in (selected or [])]
        if paths:
            self.cfg.last_input_dir = str(Path(paths[0]).parent)
        return paths

    def pick_folder(self, purpose: str = "input", initial: str = "") -> str:
        window = self._window
        if window is None:
            return ""
        try:
            from webview import FileDialog
        except ImportError:  # pragma: no cover
            return ""
        directory = str(initial or "").strip()
        if not directory:
            directory = (self.cfg.last_output_dir if purpose == "output"
                         else self.cfg.last_input_dir) or ""
        try:
            selected = window.create_file_dialog(FileDialog.FOLDER, directory=directory)
        except Exception as exc:
            log.exception("打开文件夹对话框失败")
            raise RuntimeError(f"打开文件夹对话框失败：{exc}") from exc
        if not selected:
            return ""
        path = str(selected[0])
        if purpose == "output":
            self.cfg.last_output_dir = path
        else:
            self.cfg.last_input_dir = path
        return path

    def pick_exe(self, kind: str = "pandoc") -> str:
        window = self._window
        if window is None:
            return ""
        try:
            from webview import FileDialog
        except ImportError:  # pragma: no cover
            return ""
        try:
            selected = window.create_file_dialog(
                FileDialog.OPEN,
                directory=str(Path(self.cfg.pandoc_path or self.cfg.browser_path or "").parent)
                if (self.cfg.pandoc_path or self.cfg.browser_path) else "",
                file_types=EXE_FILE_FILTERS,
            )
        except Exception as exc:
            log.exception("打开可执行文件对话框失败")
            raise RuntimeError(f"打开可执行文件对话框失败：{exc}") from exc
        return str(selected[0]) if selected else ""

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------

    def start_job(self, payload: dict[str, Any] | None) -> str:
        data = payload if isinstance(payload, dict) else {}
        files = data.get("files") or []
        log.info("开始%s：%d 个文件", "预览" if data.get("preview_only") else "转换",
                 len(files) if isinstance(files, list) else 0)
        return self.service.start(data)

    def poll(self, job_id: str = "", cursor: int = 0,
             log_cursor: int = 0) -> dict[str, Any]:
        return self.service.poll(job_id, cursor, log_cursor)

    def cancel_job(self, job_id: str = "") -> dict[str, Any]:
        self.service.cancel(job_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------

    def open_preview(self, path: str) -> dict[str, Any]:
        return self.service.open_preview(str(path or ""))

    def preview_page(self, page: int = 1, width: int = 900,
                     scale: float | None = None) -> dict[str, Any]:
        return self.service.preview_page(page, width, scale)

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------

    def open_path(self, path: str) -> dict[str, Any]:
        target = Path(str(path or ""))
        if not str(path or "").strip() or not target.exists():
            return {"ok": False, "error": f"路径不存在：{target}"}
        try:
            open_system_path(target)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def on_closing(self) -> bool:
        """窗口关闭事件：停掉任务、保存最后一版设置。"""
        try:
            self.service.shutdown()
        except Exception:
            log.exception("关闭服务层时出错")
        try:
            self.cfg.save()
        except Exception:
            pass
        return True


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _natural_key(name: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", name)]
