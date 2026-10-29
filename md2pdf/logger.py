"""日志配置：文件轮转 + 控制台；GUI 可另外挂自己的 handler。"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(log_dir: Path | None = None, *, level: int = logging.INFO) -> logging.Logger:
    global _configured
    root = logging.getLogger("md2pdf")
    if _configured:
        return root

    root.setLevel(level)
    root.propagate = False

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    root.addHandler(console)

    if log_dir:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "md2pdf.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
            root.addHandler(file_handler)
        except OSError:
            root.warning("无法创建日志文件，仅输出到控制台")

    _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"md2pdf.{name}")
