"""异常类型。所有面向用户的错误都带中文说明与处置建议。"""

from __future__ import annotations


class Md2PdfError(Exception):
    """本工具所有可预期错误的基类。"""


class ToolNotFoundError(Md2PdfError):
    """找不到 pandoc 或浏览器。"""


class ConversionError(Md2PdfError):
    """转换流程中的具体失败。"""

    def __init__(self, message: str, *, detail: str = "", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint

    def __str__(self) -> str:
        parts = [self.message]
        if self.hint:
            parts.append(f"建议：{self.hint}")
        return "；".join(parts)


class TargetLockedError(Md2PdfError):
    """目标文件被其他程序占用（通常是被 PDF 阅读器打开）。"""

    def __init__(self, path) -> None:
        super().__init__(f"无法写入 {path}：文件被占用")
        self.path = path


class ProcessTimeout(Md2PdfError):
    """子进程超时。"""

    def __init__(self, what: str, seconds: float) -> None:
        super().__init__(f"{what} 超过 {seconds:.0f} 秒未完成")
        self.what = what
        self.seconds = seconds


class CancelledError(Md2PdfError):
    """用户取消了任务。"""

    def __init__(self, message: str = "已取消") -> None:
        super().__init__(message)
