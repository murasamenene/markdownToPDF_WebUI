"""测试夹具：生成包含中文、表格、公式、图片、脚注的示例文档。"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="md2pdf-tests-"))
atexit.register(lambda: shutil.rmtree(WORK, ignore_errors=True))

_LONG_PARAGRAPH = (
    "这是一段用于测试排版的中文正文，包含 English words、数字 12345 与标点符号。"
    "两端对齐时中文与西文混排容易出现字间距异常，这是一个很好的观察样本。"
    "如果需要验证分页行为，可以把这段文字重复多次，观察段落的断行与孤行控制是否合理。"
)


def make_image(path: Path, color: tuple[int, int, int] = (220, 30, 30), size=(240, 120)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def sample_markdown(image_name: str = "pic.png", long_paragraphs: int = 6) -> str:
    long_body = "\n\n".join(_LONG_PARAGRAPH for _ in range(long_paragraphs))
    return f"""# 第一单元 测试文档

这是一段中文正文，用来验证首行缩进、行距与两端对齐。正文里混排 English words 和数字 123。

## 列表

- 第一项
- 第二项
  - 嵌套项

1. 有序一
2. 有序二

## 表格

| 项目 | 说明 | 数值 |
|:-----|:-----|-----:|
| A | 中文单元格 | 1 |
| B | 另一个单元格 | 22 |

## 代码

```python
def hello(name: str) -> str:
    return f"你好，{{name}}"
```

行内代码 `pandoc --standalone` 与长链接 https://example.com/this/is/a/very/long/url/that/should/wrap/without/overflowing/the/page

> 引用块：这段话应当左对齐且不首行缩进。

## 图片

![测试图片]({image_name})

## 公式

行内公式 $E = mc^2$，独立公式：

$$ \\int_0^1 x^2 \\, dx = \\frac{{1}}{{3}} $$

## 脚注

这里有一个脚注[^1]。

[^1]: 脚注内容，验证脚注是否渲染在文末。

## 长文

{long_body}
"""


def write_sample(directory: Path, *, name: str = "sample.md", with_image: bool = True,
                 long_paragraphs: int = 6) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if with_image:
        make_image(directory / "pic.png")
    path = directory / name
    path.write_text(sample_markdown(long_paragraphs=long_paragraphs), encoding="utf-8")
    return path
