"""样式模型与 CSS 生成。

设计原则：模板里所有可调项都走 CSS 变量（--font-body、--indent 等），
生成时只替换一处 :root 和少量条件规则。这样用户“改字体/字号”不会和
pandoc 自带样式打架，也不需要字符串拼接整份样式表。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from .utils import escape_css_string, is_css_color

PAGE_SIZES = {
    "A4": "A4",
    "A5": "A5",
    "B5": "B5",
    "Letter": "Letter",
    "Legal": "Legal",
}

ORIENTATIONS = {"portrait": "纵向", "landscape": "横向"}

PAGE_NUMBER_MODES = {"none": "不显示", "center": "底部居中", "right": "右下角", "left": "左下角"}
PAGE_NUMBER_FORMATS = {"n": "1", "n_of_total": "第 1 页 / 共 N 页"}

TEXT_ALIGNS = {"justify": "两端对齐", "left": "左对齐", "center": "居中"}

TABLE_STYLES = {"grid": "网格线", "plain": "简洁横线", "zebra": "斑马纹"}

CODE_THEMES = {"light": "浅色", "dark": "深色"}

# 中文字号 -> pt
FONT_SIZE_CHOICES: dict[str, float] = {
    "小五（9pt）": 9.0,
    "五号（10.5pt）": 10.5,
    "小四（12pt）": 12.0,
    "四号（14pt）": 14.0,
    "小三（15pt）": 15.0,
    "三号（16pt）": 16.0,
    "二号（22pt）": 22.0,
}

DEFAULT_BODY_FONT = "Microsoft YaHei"
DEFAULT_MONO_FONT = "Consolas"

_SERIF_HINTS = ("serif", "song", "simsun", "宋", "times", "georgia", "garamond", "cambria",
                "ming", "明", "fangsong", "仿宋", "book antiqua", "palatino")


def _is_serif_family(name: str) -> bool:
    low = (name or "").lower()
    if "sans" in low:
        return False
    return any(hint in low for hint in _SERIF_HINTS)


def font_stack(family: str) -> str:
    """把用户选的字体名扩展成带中文回退的 font-family 值。"""
    family = (family or "").strip()
    if _is_serif_family(family):
        fallbacks = ['"Source Han Serif SC"', '"Noto Serif CJK SC"', '"Songti SC"',
                     '"SimSun"', '"Times New Roman"', "serif"]
    else:
        fallbacks = ['"Microsoft YaHei"', '"PingFang SC"', '"Source Han Sans SC"',
                     '"Noto Sans CJK SC"', '"Segoe UI"', "Arial", "sans-serif"]
    primary = f'"{family}"' if family else fallbacks[0]
    chain = [primary] + [f for f in fallbacks if f.lower() != primary.lower()]
    return ", ".join(chain)


def mono_stack(family: str) -> str:
    family = (family or "").strip() or DEFAULT_MONO_FONT
    chain = [f'"{family}"', '"Cascadia Mono"', '"Sarasa Mono SC"', '"Consolas"',
             '"Courier New"', "monospace"]
    return ", ".join(dict.fromkeys(chain))


@dataclass(frozen=True)
class StyleSettings:
    # --- 页面 ---
    page_size: str = "A4"
    orientation: str = "portrait"
    margin_top: float = 20.0
    margin_right: float = 18.0
    margin_bottom: float = 20.0
    margin_left: float = 18.0
    page_number: str = "none"
    page_number_format: str = "n_of_total"
    header_text: str = ""
    header_on_first_page: bool = True
    break_before_h1: bool = False
    # --- 字体 ---
    body_font: str = DEFAULT_BODY_FONT
    heading_font: str = ""          # 空 = 跟随正文
    mono_font: str = DEFAULT_MONO_FONT
    font_size_pt: float = 10.5
    line_height: float = 1.75
    # --- 段落 ---
    first_line_indent_em: float = 2.0
    paragraph_gap_em: float = 0.45
    text_align: str = "justify"
    # --- 元素 ---
    body_color: str = "#1a1a1a"
    heading_color: str = "#111111"
    accent_color: str = "#2b5797"
    link_color: str = "#0b57d0"
    table_style: str = "grid"
    code_theme: str = "light"
    heading_scale: float = 1.0
    # --- 目录/分页 ---
    toc_break: bool = True
    # --- 高级 ---
    custom_css: str = ""

    # ---------------- 规范化 ----------------

    def normalized(self) -> "StyleSettings":
        """夹紧数值、纠正枚举、补全颜色。所有外部输入都必须先过这里。"""
        d = dataclasses.asdict(self)

        d["page_size"] = self.page_size if self.page_size in PAGE_SIZES else "A4"
        d["orientation"] = self.orientation if self.orientation in ORIENTATIONS else "portrait"
        for key, lo, hi in (
            ("margin_top", 5.0, 50.0), ("margin_right", 5.0, 50.0),
            ("margin_bottom", 5.0, 50.0), ("margin_left", 5.0, 50.0),
            ("font_size_pt", 6.0, 24.0), ("line_height", 1.0, 3.0),
            ("first_line_indent_em", 0.0, 4.0), ("paragraph_gap_em", 0.0, 2.0),
            ("heading_scale", 0.8, 1.6),
        ):
            try:
                value = float(d[key])
            except (TypeError, ValueError):
                value = getattr(StyleSettings, key, lo)
            d[key] = min(hi, max(lo, value))

        if d["page_number"] not in PAGE_NUMBER_MODES:
            d["page_number"] = "none"
        if d["page_number_format"] not in PAGE_NUMBER_FORMATS:
            d["page_number_format"] = "n_of_total"
        if d["text_align"] not in TEXT_ALIGNS:
            d["text_align"] = "justify"
        if d["table_style"] not in TABLE_STYLES:
            d["table_style"] = "grid"
        if d["code_theme"] not in CODE_THEMES:
            d["code_theme"] = "light"

        for key, default in (("body_color", "#1a1a1a"), ("heading_color", "#111111"),
                             ("accent_color", "#2b5797"), ("link_color", "#0b57d0")):
            if not is_css_color(str(d.get(key, ""))):
                d[key] = default

        d["header_text"] = str(d.get("header_text") or "").replace("\r", " ").replace("\n", " ")
        d["custom_css"] = str(d.get("custom_css") or "")
        d["heading_font"] = str(d.get("heading_font") or "")
        return StyleSettings(**d)

    def warnings(self, *, supports_margin_boxes: bool = True) -> list[str]:
        w: list[str] = []
        if self.page_number != "none" and supports_margin_boxes is False:
            w.append("页码需要 Chromium 131 或更新版本的 Edge/Chrome，当前浏览器不支持，页码将被忽略。")
        if self.page_number != "none" and self.margin_bottom < 12:
            w.append("下边距小于 12mm，页码可能贴边或被裁切。")
        if self.header_text and self.margin_top < 15:
            w.append("上边距小于 15mm，页眉可能与正文重叠。")
        if self.font_size_pt < 8:
            w.append("字号小于 8pt，打印可读性较差。")
        return w

    # ---------------- 序列化 ----------------

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StyleSettings":
        if not isinstance(data, dict):
            return cls()
        default = cls()
        values: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in data:
                continue
            raw = data[f.name]
            base = getattr(default, f.name)
            try:
                if isinstance(base, bool):
                    values[f.name] = bool(raw)
                elif isinstance(base, float):
                    values[f.name] = float(raw)
                elif isinstance(base, str):
                    values[f.name] = str(raw)
                else:
                    values[f.name] = raw
            except (TypeError, ValueError):
                continue
        return cls(**values).normalized()

    def merged(self, **changes: Any) -> "StyleSettings":
        data = self.to_dict()
        data.update(changes)
        return StyleSettings.from_dict(data)


# --------------------------------------------------------------------------
# CSS 生成
# --------------------------------------------------------------------------

_ROOT_TEMPLATE = """:root {{
  --font-body: {font_body};
  --font-heading: {font_heading};
  --font-mono: {font_mono};
  --line-height: {line_height};
  --indent: {indent}em;
  --para-gap: {para_gap}em;
  --align: {align};
  --color-body: {color_body};
  --color-heading: {color_heading};
  --color-accent: {color_accent};
  --color-link: {color_link};
  --color-border: #d8d8d8;
  --color-table-head: #f1f3f5;
  --color-code-bg: {code_bg};
  --color-code-border: {code_border};
  --color-code-fg: {code_fg};
}}
"""

_PAGE_TEMPLATE = """@page {{
  size: {size};
  margin: {top}mm {right}mm {bottom}mm {left}mm;
{boxes}}}
"""

_BASE_RULES = """
/* ---------- 覆盖 pandoc 默认样式 ---------- */
html {
  font-size: __FONT_SIZE__pt;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}

body {
  max-width: none;
  margin: 0;
  padding: 0;
  font-family: var(--font-body);
  font-size: 1rem;
  line-height: var(--line-height);
  color: var(--color-body);
  text-align: var(--align);
  word-break: normal;
  overflow-wrap: break-word;
  line-break: strict;
  hyphens: manual;
}

/* ---------- 段落与中文排版 ---------- */
p {
  margin: 0 0 var(--para-gap) 0;
  text-indent: var(--indent);
  orphans: 2;
  widows: 2;
}

h1 + p, h2 + p, h3 + p, h4 + p, h5 + p, h6 + p,
li > p, dd > p, blockquote p, td p, th p, figcaption p,
.footnotes p, #TOC p, .abstract p, #title-block-header p {
  text-indent: 0;
}

/* :has() 单独成条：旧引擎不支持时不会连带废掉上面的重置规则 */
p:has(> img:only-child) { text-indent: 0; }

p:empty, div:empty { display: none; }

/* ---------- 标题 ---------- */
h1, h2, h3, h4, h5, h6 {
  font-family: var(--font-heading);
  color: var(--color-heading);
  font-weight: 700;
  line-height: 1.35;
  text-indent: 0;
  break-after: avoid-page;
  page-break-after: avoid;
  break-inside: avoid;
}

h1 { font-size: calc(1.55rem * var(--hscale)); margin: 1.4em 0 0.7em;
     padding-bottom: 0.25em; border-bottom: 2px solid var(--color-accent); }
h2 { font-size: calc(1.28rem * var(--hscale)); margin: 1.3em 0 0.6em; }
h3 { font-size: calc(1.12rem * var(--hscale)); margin: 1.15em 0 0.5em; }
h4 { font-size: calc(1.0rem * var(--hscale));  margin: 1.0em 0 0.45em; }
h5 { font-size: calc(0.95rem * var(--hscale)); margin: 0.9em 0 0.4em; }
h6 { font-size: calc(0.9rem * var(--hscale));  margin: 0.9em 0 0.4em; }

h1:first-child, h2:first-child, h3:first-child { margin-top: 0; }

/* ---------- 标题块 ---------- */
#title-block-header {
  text-align: center;
  margin: 0 0 2.2em;
  break-after: avoid-page;
}
#title-block-header h1.title {
  font-size: calc(2rem * var(--hscale));
  border: none;
  margin: 0 0 0.5em;
  padding: 0;
}
#title-block-header .subtitle { font-size: 1.1rem; color: #555; margin: 0.2em 0; }
#title-block-header .author, #title-block-header .date {
  font-size: 0.95rem;
  margin: 0.2em 0;
  color: #555;
}

/* ---------- 目录 ---------- */
#TOC {
  font-size: 0.95rem;
  margin: 0 0 1.6em;
  padding: 0.8em 1.1em;
  border: 1px solid var(--color-border);
  background: #fafbfc;
  border-radius: 4px;
}
#TOC ul { list-style: none; margin: 0.15em 0; padding-left: 1.2em; }
#TOC > ul { padding-left: 0; }
#TOC li { margin: 0.1em 0; text-indent: 0; }
#TOC a { color: inherit; text-decoration: none; }
#TOC h2 {
  font-size: 1.05rem;
  margin: 0 0 0.5em;
  padding: 0;
  border: none;
  text-align: left;
}

/* ---------- 列表 ---------- */
ul, ol { margin: 0 0 var(--para-gap) 0; padding-left: 1.9em; }
li { margin: 0.12em 0; }
li::marker { color: var(--color-accent); }
dl { margin: 0.8em 0; }
dt { font-weight: 700; }
dd { margin: 0 0 0.4em 1.6em; }

/* ---------- 表格 ---------- */
table {
  /* pandoc 的打印样式把 table 设成 display:block，列宽会退化成内容宽；
     必须显式还原为表格盒，width:100% 才能真正把列撑开 */
  display: table;
  overflow: visible;
  border-collapse: collapse;
  width: 100%;
  margin: 1em 0 1.2em;
  font-size: 0.95rem;
  line-height: 1.5;
}
thead { display: table-header-group; }
tfoot { display: table-footer-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td { padding: 0.4em 0.6em; text-indent: 0; vertical-align: top; }
table caption { caption-side: top; text-align: center; font-size: 0.9rem; color: #666; padding-bottom: 0.35em; }
table p { margin: 0; text-indent: 0; }

/* ---------- 代码 ---------- */
code, kbd, samp { font-family: var(--font-mono); font-size: 0.88em; }

:not(pre) > code {
  /* inline-block 让代码成为“原子”行内盒：两端对齐不会拉伸代码内部的空格。
     text-indent 必须归零：block container 会继承段落的 2em 首行缩进。 */
  display: inline-block;
  max-width: 100%;
  white-space: pre-wrap;
  text-indent: 0;
  background: var(--color-code-bg);
  border: 1px solid var(--color-code-border);
  color: var(--color-code-fg);
  padding: 0.08em 0.32em;
  border-radius: 3px;
}

pre {
  font-family: var(--font-mono);
  background: var(--color-code-bg);
  border: 1px solid var(--color-code-border);
  border-radius: 4px;
  padding: 0.75em 0.9em;
  margin: 0.9em 0;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  tab-size: 4;
  font-size: 0.85rem;
  line-height: 1.55;
  break-inside: avoid;
  page-break-inside: avoid;
}
pre code { background: none; border: none; padding: 0; font-size: 1em; }

/* ---------- 引用、图片、分隔线 ---------- */
blockquote {
  margin: 1em 0;
  padding: 0.1em 0 0.1em 1em;
  border-left: 3px solid var(--color-accent);
  background: #f8f9fa;
  color: #444;
}
blockquote > :first-child { margin-top: 0.45em; }
blockquote > :last-child { margin-bottom: 0.45em; }

img { max-width: 100%; height: auto; }
figure { margin: 1em auto; text-align: center; break-inside: avoid; page-break-inside: avoid; }
figure img { display: block; margin: 0 auto; }
figcaption { font-size: 0.9rem; color: #666; margin-top: 0.4em; }

a { color: var(--color-link); text-decoration: none; }

hr { border: 0; border-top: 1px solid var(--color-border); margin: 1.6em 0; }

/* ---------- 脚注 ---------- */
.footnotes {
  margin-top: 2em;
  padding-top: 0.8em;
  border-top: 1px solid var(--color-border);
  font-size: 0.9rem;
  color: #444;
}
/* pandoc 在脚注区里还会输出一个 <hr>，这里隐藏，避免出现两条横线 */
.footnotes hr, #footnotes hr { display: none; }
.footnotes ol { padding-left: 1.4em; }
sup { font-size: 0.75em; line-height: 0; }

/* ---------- 数学 ---------- */
math { font-size: 1.05em; }

/* ---------- 分页控制 ---------- */
h1, h2, h3 { break-after: avoid-page; page-break-after: avoid; }
pre, figure, blockquote { break-inside: avoid; page-break-inside: avoid; }
"""

_TABLE_RULES = {
    "grid": """
th, td { border: 1px solid var(--color-border); }
th { background: var(--color-table-head); font-weight: 700; }
""",
    "plain": """
table { border-top: 1.5px solid #999; border-bottom: 1.5px solid #999; }
th, td { border: none; border-bottom: 1px solid #e4e4e4; }
thead th { border-bottom: 1px solid #999; }
th { font-weight: 700; }
""",
    "zebra": """
th, td { border: 1px solid var(--color-border); }
th { background: var(--color-table-head); font-weight: 700; }
tbody tr:nth-child(even) td { background: #f6f7f9; }
""",
}

_CODE_COLORS = {
    "light": ("#f6f8fa", "#e4e7ea", "#24292f"),
    "dark": ("#1f2430", "#333a4a", "#e6e6e6"),
}


def build_css(
    style: StyleSettings,
    *,
    supports_margin_boxes: bool = True,
) -> str:
    """生成完整 CSS。不含任何外部依赖，可直接内联进 <style>。"""
    s = style.normalized()
    page_boxes: list[str] = []

    if supports_margin_boxes:
        if s.header_text.strip():
            text = escape_css_string(s.header_text.strip())
            page_boxes.append(
                '  @top-center {\n'
                f'    content: "{text}";\n'
                '    font-size: 8.5pt;\n'
                '    color: #808080;\n'
                '  }'
            )
        if s.page_number != "none":
            pos = {"center": "bottom-center", "right": "bottom-right", "left": "bottom-left"}[s.page_number]
            if s.page_number_format == "n_of_total":
                content = '"第 " counter(page) " 页 / 共 " counter(pages) " 页"'
            else:
                content = "counter(page)"
            page_boxes.append(
                f'  @{pos} {{\n'
                f'    content: {content};\n'
                '    font-size: 9pt;\n'
                '    color: #666666;\n'
                '  }'
            )

    box_text = "\n".join(page_boxes)
    page_css = _PAGE_TEMPLATE.format(
        size=f"{PAGE_SIZES[s.page_size]} {s.orientation}",
        top=_fmt_mm(s.margin_top), right=_fmt_mm(s.margin_right),
        bottom=_fmt_mm(s.margin_bottom), left=_fmt_mm(s.margin_left),
        boxes=(box_text + "\n") if box_text else "",
    )

    # 首页不显示页眉（页码保留，符合常见排版习惯）
    if page_boxes and s.header_text.strip() and not s.header_on_first_page:
        page_css += "@page :first {\n  @top-center { content: none; }\n}\n"

    code_bg, code_border, code_fg = _CODE_COLORS[s.code_theme]
    root_css = _ROOT_TEMPLATE.format(
        font_body=font_stack(s.body_font),
        font_heading=font_stack(s.heading_font or s.body_font),
        font_mono=mono_stack(s.mono_font),
        line_height=f"{s.line_height:g}",
        indent=f"{s.first_line_indent_em:g}",
        para_gap=f"{s.paragraph_gap_em:g}",
        align=s.text_align,
        color_body=s.body_color,
        color_heading=s.heading_color,
        color_accent=s.accent_color,
        color_link=s.link_color,
        code_bg=code_bg,
        code_border=code_border,
        code_fg=code_fg,
    )

    extra_rules = [f":root {{ --hscale: {s.heading_scale:g}; }}"]
    if s.toc_break:
        extra_rules.append("#TOC { break-after: page; page-break-after: always; }")
    if s.break_before_h1:
        extra_rules.append(
            "h1 { break-before: page; page-break-before: always; }\n"
            "#title-block-header + h1, #TOC + h1, body > h1:first-child "
            "{ break-before: auto; page-break-before: auto; }"
        )

    base_rules = _BASE_RULES.replace("__FONT_SIZE__", _fmt_pt(s.font_size_pt))

    parts = [
        "/* 由 markdownToPDF WebUI 生成 */",
        page_css,
        root_css,
        base_rules,
        _TABLE_RULES.get(s.table_style, _TABLE_RULES["grid"]).strip() + "\n",
        "\n".join(extra_rules) + "\n",
    ]
    if s.custom_css.strip():
        parts.append("/* ---------- 自定义 CSS ---------- */")
        parts.append(s.custom_css.strip() + "\n")

    return "\n".join(parts)


def _fmt_mm(value: float) -> str:
    return f"{value:g}"


def _fmt_pt(value: float) -> str:
    return f"{value:g}"


# --------------------------------------------------------------------------
# 预设
# --------------------------------------------------------------------------

PRESETS: dict[str, StyleSettings] = {
    "默认（通用）": StyleSettings(),
    "讲义（紧凑）": StyleSettings(
        font_size_pt=10.5, line_height=1.6, first_line_indent_em=0.0,
        paragraph_gap_em=0.55, margin_top=18, margin_bottom=18,
        margin_left=16, margin_right=16, table_style="grid",
        text_align="left",
    ),
    "论文（宋体）": StyleSettings(
        body_font="SimSun", heading_font="SimHei", font_size_pt=12.0,
        line_height=1.5, first_line_indent_em=2.0, paragraph_gap_em=0.2,
        text_align="justify", page_number="center", page_number_format="n_of_total",
        accent_color="#000000", table_style="plain", break_before_h1=False,
    ),
    "报告（现代）": StyleSettings(
        body_font="Microsoft YaHei", heading_font="Microsoft YaHei", font_size_pt=11.0,
        line_height=1.7, first_line_indent_em=2.0, accent_color="#b03a2e",
        page_number="right", table_style="grid", heading_scale=1.05,
    ),
    "英文默认": StyleSettings(
        body_font="Georgia", heading_font="Arial", mono_font="Consolas",
        font_size_pt=11.0, line_height=1.5, first_line_indent_em=0.0,
        paragraph_gap_em=0.8, text_align="left", table_style="plain",
    ),
}


def preset(name: str) -> StyleSettings:
    return PRESETS.get(name, StyleSettings())


def preset_names() -> list[str]:
    return list(PRESETS)


@dataclass(frozen=True)
class PandocOptions:
    toc: bool = False
    toc_depth: int = 3
    number_sections: bool = False
    math: str = "mathml"          # mathml（离线）| mathjax（需联网）| none
    highlight_style: str = "pygments"
    language: str = ""            # 空字符串 = 不设置（pandoc 缺少 zh-CN 翻译文件，设置后会告警）
    citeproc: bool = False
    embed_resources: bool = False
    # 独占段落的图片是否升级为 <figure>，并用 alt 文本作为图注（pandoc 默认行为）
    implicit_figures: bool = True
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    def normalized(self) -> "PandocOptions":
        return PandocOptions(
            toc=bool(self.toc),
            toc_depth=max(1, min(6, int(self.toc_depth or 3))),
            number_sections=bool(self.number_sections),
            math=self.math if self.math in ("mathml", "mathjax", "none") else "mathml",
            highlight_style=str(self.highlight_style or "pygments"),
            language=str(self.language or ""),
            citeproc=bool(self.citeproc),
            embed_resources=bool(self.embed_resources),
            implicit_figures=bool(self.implicit_figures),
            extra_args=tuple(str(a) for a in (self.extra_args or ())),
        )

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["extra_args"] = list(self.extra_args)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PandocOptions":
        if not isinstance(data, dict):
            return cls()
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in data:
                continue
            raw = data[f.name]
            default = getattr(cls(), f.name)
            try:
                if isinstance(default, bool):
                    kwargs[f.name] = bool(raw)
                elif isinstance(default, int):
                    kwargs[f.name] = int(raw)
                elif isinstance(default, tuple):
                    kwargs[f.name] = tuple(str(x) for x in (raw or []))
                else:
                    kwargs[f.name] = str(raw)
            except (TypeError, ValueError):
                continue
        return cls(**kwargs).normalized()

    def merged(self, **changes: Any) -> "PandocOptions":
        data = self.to_dict()
        data.update(changes)
        return PandocOptions.from_dict(data)


def pandoc_highlight_for(code_theme: str, requested: str) -> str:
    """深色代码主题时自动换成深色高亮，避免浅色语法色糊在深底上。"""
    if code_theme == "dark" and requested in ("pygments", "tango", "kate", "breezedark"):
        return "breezedark"
    return requested
