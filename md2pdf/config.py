"""配置持久化。

默认写到 %APPDATA%\\md2pdf\\settings.json；如果程序目录里存在 portable.flag，
则写到程序目录的 config/ 下（便携版，U 盘可用）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logger import get_logger
from .styles import PandocOptions, StyleSettings
from .utils import IS_WINDOWS, atomic_write_text

log = get_logger("config")

SETTINGS_FILENAME = "settings.json"
PORTABLE_FLAG = "portable.flag"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def is_portable() -> bool:
    return (project_root() / PORTABLE_FLAG).exists()


def app_data_dir() -> Path:
    if is_portable():
        return project_root() / "config"
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "md2pdf"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "md2pdf"


def config_path() -> Path:
    return app_data_dir() / SETTINGS_FILENAME


def log_dir() -> Path:
    return app_data_dir() / "logs"


@dataclass
class AppConfig:
    style: StyleSettings = field(default_factory=StyleSettings)
    pandoc: PandocOptions = field(default_factory=PandocOptions)
    output_dir: str = ""
    conflict: str = "overwrite"          # overwrite | rename | skip
    pandoc_path: str = ""
    browser_path: str = ""
    last_input_dir: str = ""
    last_output_dir: str = ""
    show_log: bool = True
    pdf_info: bool = False
    schema_version: int = 1

    # ---------------- 读写 ----------------

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = Path(path) if path else config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("配置文件损坏，使用默认设置：%s", exc)
            _backup_broken(path)
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        config = cls()
        config.style = StyleSettings.from_dict(data.get("style"))
        config.pandoc = PandocOptions.from_dict(data.get("pandoc"))
        for f in dataclasses.fields(cls):
            if f.name in ("style", "pandoc"):
                continue
            if f.name not in data:
                continue
            raw = data[f.name]
            default = getattr(config, f.name)
            try:
                if isinstance(default, bool):
                    setattr(config, f.name, bool(raw))
                elif isinstance(default, int):
                    setattr(config, f.name, int(raw))
                elif isinstance(default, float):
                    setattr(config, f.name, float(raw))
                elif isinstance(default, str):
                    setattr(config, f.name, str(raw))
            except (TypeError, ValueError):
                continue
        config.conflict = config.conflict if config.conflict in ("overwrite", "rename", "skip") else "overwrite"
        return config

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["style"] = self.style.to_dict()
        data["pandoc"] = self.pandoc.to_dict()
        return data

    def save(self, path: Path | None = None) -> None:
        path = Path(path) if path else config_path()
        try:
            atomic_write_text(path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        except OSError as exc:
            log.warning("保存配置失败：%s", exc)


def _backup_broken(path: Path) -> None:
    try:
        shutil.copy2(path, path.with_suffix(".broken.json"))
    except OSError:
        pass
