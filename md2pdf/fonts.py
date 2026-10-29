"""系统字体枚举。

字体名必须来自系统真实安装的列表，否则 CSS 会静默回退到默认字体——
用户以为“设了思源宋体”，实际出来的是雅黑，这类问题在 PDF 上无法自动发现。
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from .utils import CREATE_NO_WINDOW, IS_WINDOWS, decode_output

# 中文名 -> 常见英文注册名
_FONT_ALIASES = {
    "微软雅黑": "Microsoft YaHei",
    "微软雅黑 light": "Microsoft YaHei Light",
    "宋体": "SimSun",
    "新宋体": "NSimSun",
    "黑体": "SimHei",
    "楷体": "KaiTi",
    "楷体_gb2312": "KaiTi",
    "仿宋": "FangSong",
    "仿宋_gb2312": "FangSong",
    "等线": "DengXian",
    "隶书": "LiSu",
    "幼圆": "YouYuan",
    "华文宋体": "STSong",
    "华文黑体": "STHeiti",
    "华文楷体": "STKaiti",
    "华文仿宋": "STFangsong",
    "思源黑体": "Source Han Sans SC",
    "思源宋体": "Source Han Serif SC",
}

# 排序时优先展示的字体（中文文档最常用）
_FONT_PRIORITY = [
    "Microsoft YaHei", "Microsoft YaHei UI", "Microsoft YaHei Light",
    "SimSun", "NSimSun", "SimHei", "KaiTi", "FangSong", "DengXian",
    "Source Han Sans SC", "Source Han Serif SC", "Noto Sans CJK SC", "Noto Serif CJK SC",
    "Sarasa Mono SC", "LXGW WenKai",
    "Times New Roman", "Arial", "Georgia", "Calibri", "Cambria", "Segoe UI",
    "Consolas", "Cascadia Mono", "Courier New", "JetBrains Mono",
]

_PAREN_RE = re.compile(r"\s*\((?:TrueType|OpenType|All res|VGA res|PostScript|Type 1)\)\s*$", re.I)
_STYLE_SUFFIXES = (
    "bold italic", "bold oblique", "semibold italic", "light italic",
    "bold", "italic", "oblique", "regular", "light", "semilight",
    "semibold", "demibold", "extralight", "ultralight", "extra bold",
    "black", "heavy", "medium", "thin",
)


def _clean_family(name: str) -> str:
    name = _PAREN_RE.sub("", name or "").strip()
    if not name or name.startswith("@"):
        return ""
    low = name.casefold()
    for suffix in _STYLE_SUFFIXES:
        if low.endswith(" " + suffix):
            candidate = name[: -(len(suffix) + 1)].strip()
            if len(candidate) >= 2:
                name = candidate
                low = name.casefold()
                break
    return name


def _windows_registry_fonts() -> set[str]:
    names: set[str] = set()
    if not IS_WINDOWS:
        return names
    try:
        import winreg
    except ImportError:
        return names

    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    hives = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
    views = [getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0), 0]
    for hive in hives:
        for view in views:
            try:
                with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view) as key:
                    count = winreg.QueryInfoKey(key)[1]
                    for i in range(count):
                        try:
                            value_name, _, _ = winreg.EnumValue(key, i)
                        except OSError:
                            continue
                        # 注册表里常见 "Microsoft YaHei & Microsoft YaHei UI (TrueType)"
                        for part in (value_name or "").split(" & "):
                            cleaned = _clean_family(part)
                            # 只保留看起来像字体名的值（有些值名是 arial.ttf 这样的文件名）
                            if cleaned and not cleaned.lower().endswith((".ttf", ".otf", ".ttc", ".fon")):
                                names.add(cleaned)
            except OSError:
                continue
    return names


def _fc_list_fonts() -> set[str]:
    """macOS/Linux 用 fontconfig。Windows 上一般没有，直接跳过。"""
    names: set[str] = set()
    try:
        proc = subprocess.run(
            ["fc-list", "--format", "%{family}\n"],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return names
    if proc.returncode != 0:
        return names
    for line in decode_output(proc.stdout).splitlines():
        for family in line.split(","):
            cleaned = family.strip()
            if cleaned:
                names.add(cleaned)
    return names


def _windows_fonts_dir() -> set[str]:
    """兜底：从字体文件名粗推字体名（无法精确，只在注册表读取失败时用）。"""
    names: set[str] = set()
    if not IS_WINDOWS:
        return names
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if not fonts_dir.is_dir():
        return names
    for path in fonts_dir.iterdir():
        stem = path.stem
        if stem.lower() in {"arial", "times", "cour", "verdana", "tahoma", "georgia"}:
            names.add(stem.title())
    return names


@lru_cache(maxsize=1)
def system_fonts() -> tuple[str, ...]:
    """不依赖 Tk 的系统字体列表（缓存）。"""
    raw = set()
    raw |= _windows_registry_fonts()
    raw |= _fc_list_fonts()
    if not raw:
        raw |= _windows_fonts_dir()
    deduped = _dedupe(raw)
    return tuple(sorted(deduped, key=_sort_key))


def _dedupe(names) -> list[str]:
    seen: dict[str, str] = {}
    for name in names:
        key = name.casefold()
        if key not in seen:
            seen[key] = name
        elif name.isascii() and not seen[key].isascii():
            # 同一字体有中英文名时，优先保留英文注册名
            seen[key] = name
    return list(seen.values())


def _sort_key(name: str):
    low = name.casefold()
    for i, preferred in enumerate(_FONT_PRIORITY):
        if low == preferred.casefold():
            return (0, i, low)
    # 中文字体排在拉丁字体前面，便于中文用户查找
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in name)
    return (1 if has_cjk else 2, 0, low)


def list_fonts() -> list[str]:
    """系统字体去重、清理后按中文常用度排序。"""
    names = {_clean_family(n) for n in system_fonts()}
    names.discard("")
    return sorted(_dedupe(names), key=_sort_key)


def resolve_font_name(name: str, known: list[str] | None = None) -> str | None:
    """把用户输入的字体名解析为系统里真实存在的名称；找不到返回 None。

    同时兼容：英文注册名、中文别名、以及只在系统里注册了中文名的情况。
    """
    if not name:
        return None
    pool = known if known is not None else list(system_fonts())
    lookup = {n.casefold(): n for n in pool}
    key = name.strip().casefold()
    if key in lookup:
        return lookup[key]

    alias = _FONT_ALIASES.get(name.strip()) or _FONT_ALIASES.get(key)
    if alias and alias.casefold() in lookup:
        return lookup[alias.casefold()]

    # 反向：用户给英文名，但系统只注册了中文名
    for zh_name, en_name in _FONT_ALIASES.items():
        if en_name.casefold() == key and zh_name.casefold() in lookup:
            return lookup[zh_name.casefold()]

    # 去掉空格再比一次（有些字体名带空格差异）
    compact = {n.replace(" ", "").casefold(): n for n in pool}
    return compact.get(key.replace(" ", ""))


def is_font_installed(name: str, known: list[str] | None = None) -> bool:
    return resolve_font_name(name, known) is not None


def canonicalize_font(name: str, known: list[str] | None = None) -> str:
    """返回系统里真实存在的字体名；解析不到就原样返回，交给 CSS 回退链。"""
    return resolve_font_name(name, known) or name
