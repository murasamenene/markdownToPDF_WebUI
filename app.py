#!/usr/bin/env python3
"""Markdown 转 PDF —— Web 界面启动器。

运行：
    python app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _asset_root() -> Path:
    """PyInstaller 打包后前端资源在 sys._MEIPASS 下。"""
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen) if frozen else BASE_DIR


def _message_box(title: str, text: str) -> None:
    """没有控制台时也能让用户看到启动失败原因。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def _work_area() -> tuple[int, int]:
    """桌面可用工作区（扣除任务栏），单位与 create_window 一致。"""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        if ok and rect.right > rect.left and rect.bottom > rect.top:
            return rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    try:
        import webview

        screen = webview.screens[0]
        return int(screen.width), max(480, int(screen.height) - 48)
    except Exception:
        return 0, 0


def _fit_window_size(width: int, height: int) -> tuple[int, int]:
    """把窗口夹到工作区内，避免小屏笔记本上超出屏幕。"""
    work_w, work_h = _work_area()
    if work_w <= 0 or work_h <= 0:
        return width, height
    return max(720, min(width, work_w - 24)), max(520, min(height, work_h - 24))


def main() -> int:
    try:
        import webview
    except ImportError as exc:
        _message_box(
            "缺少 pywebview",
            "运行本程序需要 pywebview：\n\n    python -m pip install -r requirements.txt\n\n"
            + str(exc),
        )
        return 2

    from md2pdf.config import AppConfig, log_dir
    from md2pdf.logger import setup_logging
    from md2pdf.services import ConversionService
    from md2pdf.web_api import Api

    setup_logging(log_dir())
    config = AppConfig.load()
    service = ConversionService()
    api = Api(service, config)

    index = _asset_root() / "web" / "index.html"
    if not index.is_file():
        _message_box(
            "缺少前端文件",
            f"找不到前端页面：\n{index}\n\n请确认 web/ 目录与程序放在一起。",
        )
        return 3

    width, height = _fit_window_size(1400, 900)
    min_width, min_height = _fit_window_size(1080, 680)
    work_w, work_h = _work_area()
    pos_x = max(0, (work_w - width) // 2) if work_w else None
    pos_y = max(0, (work_h - height) // 2) if work_h else None

    window = webview.create_window(
        "Markdown 转 PDF",
        str(index),
        js_api=api,
        width=width,
        height=height,
        x=pos_x,
        y=pos_y,
        min_size=(min(1080, min_width), min(680, min_height)),
        text_select=True,
    )
    api.bind(window)
    window.events.closing += api.on_closing

    try:
        webview.start(http_server=True)
    except Exception as exc:
        _message_box(
            "无法启动界面",
            "启动 WebView2 界面失败：\n\n"
            f"{exc}\n\n"
            "Windows 10 1803+ 通常自带 WebView2 运行时；若缺失，请从\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/\n"
            "安装后重试。",
        )
        return 4
    finally:
        try:
            service.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
