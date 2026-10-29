"""外部工具发现：pandoc 与 Edge/Chrome。

版本号用于判断能力（例如 @page 边距盒需要 Chromium >= 131 才有页码），
所以浏览器版本尽量走“安装目录名”这条零成本路径，失败再查注册表/调用 PowerShell。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .errors import ToolNotFoundError
from .utils import CREATE_NO_WINDOW, IS_WINDOWS, decode_output, path_to_uri

PANEL_BOX_MIN_MAJOR = 131  # Chromium 131 起支持 @page 边距盒（页眉/页脚/页码）

_VERSION_DIR_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")


@dataclass
class ToolInfo:
    kind: str                 # "pandoc" | "edge" | "chrome" | "chromium"
    path: Path
    version: str = ""
    source: str = ""          # 发现来源，便于排错
    extra: dict = field(default_factory=dict)

    @property
    def major(self) -> int:
        m = re.match(r"(\d+)", self.version or "")
        return int(m.group(1)) if m else 0

    @property
    def display(self) -> str:
        label = {"pandoc": "Pandoc", "edge": "Microsoft Edge",
                 "chrome": "Google Chrome", "chromium": "Chromium"}.get(self.kind, self.kind)
        ver = f" {self.version}" if self.version else ""
        return f"{label}{ver}"

    def supports_margin_boxes(self) -> bool:
        if self.kind == "pandoc":
            return False
        return self.major >= PANEL_BOX_MIN_MAJOR


# --------------------------------------------------------------------------
# 发现逻辑
# --------------------------------------------------------------------------

def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip().strip('"')
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


def find_pandoc(preferred: str | Path | None = None) -> ToolInfo:
    candidates: list[tuple[Path, str]] = []
    if preferred:
        candidates.append((Path(preferred), "设置"))
    env = _env_path("MD2PDF_PANDOC")
    if env:
        candidates.append((env, "环境变量 MD2PDF_PANDOC"))
    which = shutil.which("pandoc")
    if which:
        candidates.append((Path(which), "PATH"))

    here = Path(__file__).resolve().parent.parent
    local = [
        here / "vendor" / "pandoc" / "pandoc.exe",
        here / "vendor" / "pandoc" / "pandoc",
    ]
    program_files = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")]
    for base in program_files:
        if not base:
            continue
        local.append(Path(base) / "Pandoc" / "pandoc.exe")
    user_profile = Path.home()
    local += [
        Path(os.environ.get("LOCALAPPDATA", user_profile)) / "Pandoc" / "pandoc.exe",
        user_profile / "scoop" / "shims" / "pandoc.exe",
        Path("C:/ProgramData/chocolatey/bin/pandoc.exe"),
    ]
    for p in local:
        if p.exists():
            candidates.append((p, "常见安装位置"))

    for path, source in candidates:
        if path and path.exists():
            version = _pandoc_version(path)
            return ToolInfo("pandoc", path.resolve(), version, source)
    raise ToolNotFoundError(
        "未找到 pandoc。请安装 pandoc（https://pandoc.org/installing.html），"
        "或在设置中手动指定 pandoc.exe 的路径。"
    )


def _pandoc_version(path: Path) -> str:
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            timeout=20,
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        text = decode_output(proc.stdout)
        m = re.search(r"pandoc(?:\.exe)?\s+(\d+\.\d+(?:\.\d+)?)", text)
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


@lru_cache(maxsize=8)
def _pandoc_help_text(path_str: str) -> str:
    try:
        proc = subprocess.run(
            [path_str, "--help"],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        return decode_output(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass(frozen=True)
class PandocCapabilities:
    math_method: bool = False          # 3.9+：--math-method=mathml，旧版 --mathml
    syntax_highlighting: bool = False  # 3.11+：--syntax-highlighting，旧版 --highlight-style


def pandoc_capabilities(path: Path | str) -> PandocCapabilities:
    """按 --help 探测可用的新参数，避免踩到弃用警告。"""
    text = _pandoc_help_text(str(path))
    return PandocCapabilities(
        math_method="--math-method" in text,
        syntax_highlighting="--syntax-highlighting" in text,
    )


def supports_math_method(path: Path | str) -> bool:
    return pandoc_capabilities(path).math_method


def find_browser(preferred: str | Path | None = None) -> ToolInfo:
    candidates: list[tuple[Path, str, str]] = []

    def add(path: Path | None, source: str, kind: str) -> None:
        if path and path.exists():
            candidates.append((path, source, kind))

    if preferred:
        add(Path(preferred), "设置", _browser_kind_from_name(Path(preferred)))
    env = _env_path("MD2PDF_BROWSER")
    if env:
        add(env, "环境变量 MD2PDF_BROWSER", _browser_kind_from_name(env))

    if IS_WINDOWS:
        for key, kind in (
            (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe", "edge"),
            (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", "chrome"),
        ):
            found = _registry_app_path(key)
            add(found, "注册表 App Paths", kind)

        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        add(Path(pf86) / "Microsoft/Edge/Application/msedge.exe", "默认安装路径", "edge")
        add(Path(pf) / "Microsoft/Edge/Application/msedge.exe", "默认安装路径", "edge")
        add(Path(pf) / "Google/Chrome/Application/chrome.exe", "默认安装路径", "chrome")
        add(Path(pf86) / "Google/Chrome/Application/chrome.exe", "默认安装路径", "chrome")
        add(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            "默认安装路径", "chrome")

    for name in ("msedge", "chrome", "chromium", "google-chrome", "chromium-browser"):
        which = shutil.which(name)
        if which:
            add(Path(which), "PATH", _browser_kind_from_name(Path(which)))

    if not IS_WINDOWS:
        for p in (
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/usr/bin/microsoft-edge"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
        ):
            add(p, "常见安装路径", _browser_kind_from_name(p))

    for path, source, kind in candidates:
        version = detect_browser_version(path)
        return ToolInfo(kind, path.resolve(), version, source)

    raise ToolNotFoundError(
        "未找到 Microsoft Edge 或 Google Chrome。请安装其中之一，"
        "或在设置中手动指定浏览器可执行文件。"
    )


def _browser_kind_from_name(path: Path | str) -> str:
    name = Path(str(path)).name.lower()
    if "edge" in name:
        return "edge"
    if "chrome" in name and "chromium" not in name:
        return "chrome"
    return "chromium"


def detect_browser_version(exe: Path) -> str:
    """优先从安装目录名解析，其次注册表，最后 PowerShell。"""
    version = _version_from_install_dir(exe)
    if version:
        return version
    if IS_WINDOWS:
        version = _version_from_registry(exe)
        if version:
            return version
        version = _version_by_powershell(exe)
        if version:
            return version
    return ""


def _version_from_install_dir(exe: Path) -> str:
    """Edge 在 Application\\msedge.exe，Chrome 在 Application\\chrome.exe。
    版本目录要么是父目录，要么是父目录的子目录（取最大版本）。"""
    parent = exe.parent
    if _VERSION_DIR_RE.match(parent.name):
        return parent.name
    if parent.name.lower() == "application":
        try:
            dirs = [d for d in parent.iterdir() if d.is_dir() and _VERSION_DIR_RE.match(d.name)]
        except OSError:
            return ""
        if not dirs:
            return ""
        dirs.sort(key=lambda d: tuple(int(x) for x in d.name.split(".")), reverse=True)
        for d in dirs:
            if any(d.iterdir()):
                return d.name
    return ""


_EDGE_CLIENT_IDS = ("{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}",)
_CHROME_CLIENT_IDS = ("{8A69D345-D564-463c-AFF1-A69D9E530F96}",)


def _version_from_registry(exe: Path) -> str:
    kind = _browser_kind_from_name(exe)
    ids = _EDGE_CLIENT_IDS if kind == "edge" else _CHROME_CLIENT_IDS
    vendor = "Microsoft\\EdgeUpdate\\Clients" if kind == "edge" else "Google\\Update\\Clients"
    try:
        import winreg
    except ImportError:
        return ""

    roots = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
    views = [getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0), 0]
    for root in roots:
        for view in views:
            for client_id in ids:
                sub = f"SOFTWARE\\{vendor}\\{client_id}"
                try:
                    with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view) as key:
                        pv, _ = winreg.QueryValueEx(key, "pv")
                        if isinstance(pv, str) and pv.strip():
                            return pv.strip()
                except OSError:
                    continue
    return ""


def _version_by_powershell(exe: Path) -> str:
    script = (
        f"(Get-Item -LiteralPath '{str(exe).replace(chr(39), chr(39) * 2)}')"
        ".VersionInfo.ProductVersion"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        text = decode_output(proc.stdout).strip()
        m = re.search(r"\d+\.\d+\.\d+\.\d+", text)
        return m.group(0) if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _registry_app_path(subkey: str) -> Path | None:
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0), 0):
            try:
                with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value and Path(value).exists():
                        return Path(value)
            except OSError:
                continue
    return None


def describe_tools(pandoc: ToolInfo | None, browser: ToolInfo | None) -> str:
    lines = []
    lines.append(f"Pandoc：{pandoc.path}（{pandoc.version or '版本未知'}，来源：{pandoc.source}）"
                 if pandoc else "Pandoc：未找到")
    lines.append(f"浏览器：{browser.path}（{browser.version or '版本未知'}，来源：{browser.source}）"
                 if browser else "浏览器：未找到")
    return "\n".join(lines)
