"""底层工具：解码、路径、子进程、PDF 校验。

这里集中处理 Windows 上最容易出错的三件事：
1. 控制台代码页导致的中文乱码（一律按 UTF-8 解码，失败再退 GB18030）；
2. 中文/空格路径转 file:// URI（必须 percent-encode，否则 Edge 静默失败）；
3. 子进程残留（超时/取消时按进程树杀，否则 Edge 会留下僵尸进程锁住 profile）。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Sequence

from .errors import CancelledError, ProcessTimeout

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------

def decode_bytes(data: bytes) -> tuple[str, str]:
    """按 BOM → UTF-8 → GB18030 的顺序解码，返回 (文本, 编码名)。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8 (replace)"


def decode_output(data: bytes) -> str:
    """子进程 stdout/stderr 解码。Windows 控制台常常是 GBK。"""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def read_text_file(path: Path) -> tuple[str, str]:
    """读取文本文件，自动识别 UTF-8 / UTF-16 / GB18030。

    返回 (文本, 编码名)。文本统一为 Unicode，后续写盘一律用 UTF-8，
    避免 pandoc 收到非 UTF-8 输入时直接报错。
    """
    data = Path(path).read_bytes()
    return decode_bytes(data)


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def path_to_uri(path: Path, directory: bool = False) -> str:
    """转成带 percent-encoding 的 file:// URI（中文、空格、# 都能安全传递）。"""
    uri = Path(path).resolve().as_uri()
    if directory and not uri.endswith("/"):
        uri += "/"
    return uri


def ensure_dir(path: Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """先写同目录临时文件再 os.replace，避免半截文件。"""
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp = tempfile_name_in(path.parent, prefix=".tmp-write-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        _silent_unlink(tmp)
        raise


def tempfile_name_in(directory: Path, prefix: str = "tmp", suffix: str = "") -> tuple[int, str]:
    """在指定目录创建临时文件名（与目标同盘，保证 os.replace 可原子替换）。"""
    ensure_dir(directory)
    fd, name = __import__("tempfile").mkstemp(dir=str(directory), prefix=prefix, suffix=suffix)
    return fd, name


def safe_replace(src: Path, dst: Path) -> None:
    """把 src 移到 dst；目标被占用时给出可操作的错误。"""
    try:
        os.replace(str(src), str(dst))
    except PermissionError as exc:
        from .errors import TargetLockedError

        raise TargetLockedError(dst) from exc
    except OSError as exc:
        # Windows 上跨盘 replace 会报 EXDEV；调用方保证同盘，这里兜底复制
        if getattr(exc, "errno", None) == 18:
            shutil.copy2(str(src), str(dst))
            _silent_unlink(src)
        else:
            raise


def unique_path(path: Path) -> Path:
    """为一个已存在的路径生成 `name (1).pdf` 形式的新名称。"""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法为 {path} 生成不冲突的文件名")


def _silent_unlink(path) -> None:
    try:
        os.unlink(str(path))
    except OSError:
        pass


def remove_tree(path: Path, attempts: int = 3) -> None:
    """删除目录；Windows 上文件句柄释放有延迟，重试几次。"""
    for _ in range(attempts):
        try:
            shutil.rmtree(str(path), ignore_errors=True)
            if not Path(path).exists():
                return
        except OSError:
            pass
        time.sleep(0.15)


# --------------------------------------------------------------------------
# 子进程
# --------------------------------------------------------------------------

def kill_process_tree(pid: int) -> None:
    """按进程树强杀。Windows 用 taskkill /T，否则会留下 renderer 子进程。"""
    if pid <= 0:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class ProcessRunner:
    """可超时、可取消的单进程执行器。

    轮询式等待（而不是一次性 communicate(timeout)），是为了在用户点“取消”
    时能立即杀掉进程树——Edge 在等待网络或字体时会卡很久。
    """

    POLL_INTERVAL = 0.08

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            kill_process_tree(proc.pid)

    def run(
        self,
        cmd: Sequence[str | os.PathLike],
        *,
        timeout: float | None = None,
        cancel: threading.Event | None = None,
        cwd: Path | str | None = None,
        what: str = "子进程",
    ) -> subprocess.CompletedProcess:
        argv = [str(c) for c in cmd]
        kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        if cwd is not None:
            kwargs["cwd"] = str(cwd)

        try:
            proc = subprocess.Popen(argv, **kwargs)
        except FileNotFoundError as exc:
            from .errors import ToolNotFoundError

            raise ToolNotFoundError(f"无法启动 {argv[0]}") from exc
        except OSError as exc:
            from .errors import ConversionError

            raise ConversionError(f"无法启动 {what}", detail=str(exc)) from exc

        with self._lock:
            self._proc = proc

        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                try:
                    out, err = proc.communicate(timeout=self.POLL_INTERVAL)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        kill_process_tree(proc.pid)
                        proc.communicate()
                        raise CancelledError()
                    if deadline is not None and time.monotonic() >= deadline:
                        kill_process_tree(proc.pid)
                        proc.communicate()
                        raise ProcessTimeout(what, timeout or 0)
            return subprocess.CompletedProcess(
                argv, proc.returncode, decode_output(out), decode_output(err)
            )
        finally:
            with self._lock:
                self._proc = None


# --------------------------------------------------------------------------
# PDF 校验
# --------------------------------------------------------------------------

def is_valid_pdf(path: Path, min_size: int = 400) -> bool:
    """检查 PDF 魔数、结尾标记与最小体积。"""
    try:
        size = Path(path).stat().st_size
        if size < min_size:
            return False
        with open(path, "rb") as fh:
            if not fh.read(5).startswith(b"%PDF-"):
                return False
            fh.seek(max(0, size - 4096))
            tail = fh.read()
        return b"%%EOF" in tail
    except OSError:
        return False


def pdf_page_count(path: Path) -> int | None:
    try:
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(str(path)) as doc:
            return len(doc)
    except Exception:
        return None


def first_page_text(path: Path, limit: int = 2000) -> str:
    try:
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(str(path)) as doc:
            if len(doc) == 0:
                return ""
            page = doc[0]
            textpage = page.get_textpage()
            try:
                return textpage.get_text_range(0, limit)
            finally:
                textpage.close()
    except Exception:
        return ""


def pdf_page_texts(path: Path, max_pages: int = 400) -> list[str]:
    """逐页提取文本，供书签定位使用。优先 pypdfium2，其次 PyMuPDF。"""
    try:
        import pypdfium2 as pdfium

        texts: list[str] = []
        with pdfium.PdfDocument(str(path)) as doc:
            for index in range(min(len(doc), max_pages)):
                page = doc[index]
                textpage = page.get_textpage()
                try:
                    texts.append(textpage.get_text_range())
                finally:
                    textpage.close()
        return texts
    except Exception:
        pass

    try:
        import pymupdf

        with pymupdf.open(str(path)) as doc:
            return [page.get_text() for page in doc[:max_pages]]
    except Exception:
        return []


_ERROR_PAGE_MARKERS = (
    "ERR_FILE_NOT_FOUND",
    "ERR_INVALID_URL",
    "ERR_ACCESS_DENIED",
    "ERR_NAME_NOT_RESOLVED",
    "This site can’t be reached",
    "This site can't be reached",
    "网页无法访问",
    "无法访问此网站",
)


def looks_like_browser_error_page(path: Path) -> bool:
    """Edge 加载失败时也会生成一个“合法”PDF，需要识别出来。"""
    if pdf_page_count(path) != 1:
        return False
    text = first_page_text(path)
    return any(marker in text for marker in _ERROR_PAGE_MARKERS)


# --------------------------------------------------------------------------
# 杂项
# --------------------------------------------------------------------------

def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} 分 {seconds % 60:.0f} 秒"


def truncate(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（已截断，共 {len(text)} 字符）"


def escape_css_string(value: str) -> str:
    """转义为 CSS 字符串字面量内容（不含两侧引号）。"""
    out = []
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch in ("\n", "\r", "\f"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


_CSS_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def is_css_color(value: str) -> bool:
    return bool(_CSS_COLOR_RE.match((value or "").strip()))


def open_path(path: Path) -> None:
    """用系统默认程序打开文件/文件夹。"""
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606  (Windows 专用)
    elif sys_platform_darwin():
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def sys_platform_darwin() -> bool:
    import sys

    return sys.platform == "darwin"


def new_token() -> str:
    return uuid.uuid4().hex[:12]


def iter_files(paths: Iterable[Path], suffixes: Iterable[str]) -> list[Path]:
    """把文件/目录混合列表展开成去重后的文件列表。"""
    allowed = {s.lower() for s in suffixes}
    result: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        p = Path(raw)
        candidates = [p] if p.is_file() else sorted(p.rglob("*")) if p.is_dir() else []
        for item in candidates:
            if not item.is_file():
                continue
            if allowed and item.suffix.lower() not in allowed:
                continue
            key = str(item.resolve()).casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result
