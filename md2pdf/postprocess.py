"""PDF 后处理：书签大纲 + 文档元数据。

Edge/Chromium 打印出来的 PDF 只有 /Title 和一串 UA 形式的 /Creator，没有书签。
这里做两件事：

1. 从 pandoc 生成的 HTML 里取标题块（YAML 的 title/author/keywords）；
2. 用 PDF 文本层定位每个标题所在页，生成层级书签。

标题定位是启发式的：按文档顺序在页面文本里做“去空白子串匹配”，并跳过目录页
（目录页会同时命中大量标题，用命中率识别）。找不到的标题直接跳过，绝不因为
书签定位失败而让转换失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .logger import get_logger

log = get_logger("postprocess")

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
MAX_HEADINGS = 400
MAX_MATCH_PAGES = 400

CREATOR = "markdownToPDF WebUI"
PRODUCER = "markdownToPDF WebUI (pandoc + Chromium)"


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class DocMeta:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class DocInfo:
    meta: DocMeta = field(default_factory=DocMeta)
    headings: list[Heading] = field(default_factory=list)


# --------------------------------------------------------------------------
# HTML 解析
# --------------------------------------------------------------------------

class _DocInfoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta = DocMeta()
        self.headings: list[Heading] = []
        self._doctitle = ""
        self._head_depth = 0
        self._nav_depth = 0
        self._header_depth = 0
        self._field: str | None = None
        self._field_tag = ""
        self._field_level = 0
        self._buf: list[str] = []

    # -- 解析回调 --------------------------------------------------

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = {str(k).lower(): (v or "") for k, v in attrs_list}

        if tag == "head":
            self._head_depth += 1
            return
        if tag == "nav" and attrs.get("id") == "TOC":
            self._nav_depth += 1
            return
        if tag == "header" and attrs.get("id") == "title-block-header":
            self._header_depth += 1
            return
        if tag == "meta":
            if (attrs.get("name") or "").lower() == "keywords":
                for keyword in (attrs.get("content") or "").split(","):
                    keyword = keyword.strip()
                    if keyword and keyword not in self.meta.keywords:
                        self.meta.keywords.append(keyword)
            return
        if tag == "br":
            if self._field:
                self._buf.append(" ")
            return
        if self._field is not None:
            return

        if tag == "title" and self._head_depth:
            self._begin("doctitle", tag, 0)
            return
        if tag in HEADING_TAGS and not self._nav_depth:
            classes = (attrs.get("class") or "").split()
            if "title" in classes or "subtitle" in classes:
                self._begin("title", tag, 0)
            else:
                self._begin("heading", tag, int(tag[1]))
            return
        if self._header_depth and tag in ("p", "div"):
            classes = (attrs.get("class") or "").split()
            if "author" in classes:
                self._begin("author", tag, 0)
            elif "date" in classes:
                self._begin("date", tag, 0)
            elif "title" in classes:
                self._begin("title", tag, 0)

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._head_depth = max(0, self._head_depth - 1)
            return
        if tag == "nav" and self._nav_depth:
            self._nav_depth -= 1
            return
        if tag == "header" and self._header_depth:
            self._header_depth -= 1
            return
        if self._field is not None and tag == self._field_tag:
            self._finish_field()

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._buf.append(data)

    # -- 内部 ------------------------------------------------------

    def _begin(self, kind: str, tag: str, level: int) -> None:
        self._field = kind
        self._field_tag = tag
        self._field_level = level
        self._buf = []

    def _finish_field(self) -> None:
        kind, text = self._field, " ".join("".join(self._buf).split())
        self._field = None
        self._field_tag = ""
        self._buf = []
        if not text:
            return
        if kind == "heading":
            if len(self.headings) < MAX_HEADINGS:
                self.headings.append(Heading(self._field_level, text))
        elif kind == "title":
            if not self.meta.title:
                self.meta.title = text
        elif kind == "doctitle":
            self._doctitle = text
        elif kind == "author":
            if text not in self.meta.authors:
                self.meta.authors.append(text)
        # date 不写入元数据：PDF 的日期字段语义不同，避免误导

    def finalize(self) -> DocInfo:
        if not self.meta.title:
            self.meta.title = self._doctitle
        return DocInfo(self.meta, self.headings)


def extract_document_info(html_path: Path) -> DocInfo:
    parser = _DocInfoParser()
    try:
        parser.feed(Path(html_path).read_text(encoding="utf-8", errors="replace"))
        parser.close()
    except Exception as exc:  # 解析失败不应该影响转换
        log.warning("解析 HTML 元数据失败：%s", exc)
    return parser.finalize()


# --------------------------------------------------------------------------
# 标题 -> 页码
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"[\s\u00a0\u200b\ufeff]+")


def normalize_text(text: str) -> str:
    return _WS_RE.sub("", text or "").casefold()


def locate_headings(
    page_texts: list[str],
    headings: list[Heading],
    *,
    skip_toc_pages: bool = False,
) -> list[tuple[int, str, int]]:
    """返回 [(level, title, page_index)]，page_index 从 0 开始。

    skip_toc_pages 只在“目录单独占页”时开启：目录页会命中大量标题，
    必须先跳过，否则所有书签都会指向目录页。
    """
    if not page_texts or not headings:
        return []

    pages = [normalize_text(text) for text in page_texts[:MAX_MATCH_PAGES]]
    keys = [normalize_text(h.text) for h in headings]
    start = _body_start_after_toc(pages, keys) if skip_toc_pages else 0

    outline: list[tuple[int, str, int]] = []
    cursor_page = start
    cursor_offset = 0
    for heading, key in zip(headings, keys):
        if not key:
            continue
        found: tuple[int, int] | None = None
        for index in range(cursor_page, len(pages)):
            start_at = cursor_offset if index == cursor_page else 0
            position = pages[index].find(key, start_at)
            if position >= 0:
                found = (index, position + len(key))
                break
        if found is not None:
            page_index, offset = found
            outline.append((heading.level, heading.text, page_index))
            cursor_page, cursor_offset = page_index, offset
    return outline


def _body_start_after_toc(pages: list[str], keys: list[str]) -> int:
    """目录页一定包含第一个标题，正文里也会再出现一次。

    取“第二次出现”的位置作为正文起点，比按标题命中率判断更稳：
    短文档的正文页也可能同时出现多个标题，用命中率会把正文页也误跳。
    """
    key = keys[0] if keys else ""
    if not key:
        return 0
    occurrences = [index for index, page in enumerate(pages) if key in page]
    if len(occurrences) >= 2:
        return occurrences[1]
    return occurrences[0] if occurrences else 0


# --------------------------------------------------------------------------
# 写回 PDF
# --------------------------------------------------------------------------

def apply_pdf_info(
    pdf_path: Path,
    meta: DocMeta,
    outline: list[tuple[int, str, int]],
    *,
    backend: str = "auto",
) -> tuple[bool, str]:
    """把元数据和书签写回 PDF。返回 (是否成功, 说明)。

    backend: auto（优先 PyMuPDF，失败或缺失时用 pypdf）/ pymupdf / pypdf
    """
    order = {
        "auto": ("pymupdf", "pypdf"),
        "pymupdf": ("pymupdf",),
        "pypdf": ("pypdf",),
    }.get(backend, ("pymupdf", "pypdf"))

    errors: list[str] = []
    for name in order:
        try:
            if name == "pymupdf":
                return _apply_with_pymupdf(pdf_path, meta, outline)
            return _apply_with_pypdf(pdf_path, meta, outline)
        except ImportError:
            errors.append(f"{name} 未安装")
        except Exception as exc:
            errors.append(f"{name} 失败：{exc}")
            log.debug("%s 写入 PDF 失败", name, exc_info=True)
    return False, "；".join(errors) or "没有可用的 PDF 库"


def _apply_with_pymupdf(
    pdf_path: Path, meta: DocMeta, outline: list[tuple[int, str, int]]
) -> tuple[bool, str]:
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        current = dict(doc.metadata or {})
        if meta.title:
            current["title"] = meta.title
        if meta.authors:
            current["author"] = "; ".join(meta.authors)
        if meta.keywords:
            current["keywords"] = ", ".join(meta.keywords)
        current["creator"] = CREATOR
        current["producer"] = PRODUCER
        doc.set_metadata(current)
        if outline:
            doc.set_toc([[level, title, page + 1] for level, title, page in outline])
        # 增量保存：只在文件尾部追加对象，不重写正文，速度快且不动原有字节
        doc.save(str(pdf_path), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        return True, f"PyMuPDF：{len(outline)} 个书签"
    finally:
        doc.close()


def _apply_with_pypdf(
    pdf_path: Path, meta: DocMeta, outline: list[tuple[int, str, int]]
) -> tuple[bool, str]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter(clone_from=reader)

    metadata: dict[str, str] = {}
    if meta.title:
        metadata["/Title"] = meta.title
    if meta.authors:
        metadata["/Author"] = "; ".join(meta.authors)
    if meta.keywords:
        metadata["/Keywords"] = ", ".join(meta.keywords)
    metadata["/Creator"] = CREATOR
    metadata["/Producer"] = PRODUCER
    writer.add_metadata(metadata)

    stack: list[tuple[int, object]] = []
    for level, title, page in outline:
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else None
        item = writer.add_outline_item(title, page, parent=parent)
        stack.append((level, item))

    tmp = pdf_path.with_name(pdf_path.name + ".info.tmp")
    try:
        with open(tmp, "wb") as handle:
            writer.write(handle)
        tmp.replace(pdf_path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return True, f"pypdf：{len(outline)} 个书签"
