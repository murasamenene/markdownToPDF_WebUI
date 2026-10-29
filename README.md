# markdownToPDF WebUI

**简体中文** | [English](README.en.md)

一个把 Markdown / 纯文本转成排版良好的 PDF 的桌面小工具。界面由 pywebview（WebView2）+ HTML/CSS/JS 构建，转换链是 **pandoc + Edge/Chrome 打印引擎**，全程在本地完成，不上传任何文件。

## 功能

- 批量转换 `.md` `.markdown` `.mdown` `.mkd` `.txt` `.rst` `.org`
- 中文排版：首行缩进、两端对齐、字体回退、页码、页眉
- 样式面板分「页面 / 字体 / 段落 / 元素 / 文档 / 高级」六组，内置 5 套预设（通用、讲义、论文、报告、英文）
- 目录、章节编号、三种表格样式、代码高亮、MathML 公式、脚注、图片
- 内嵌 PDF 预览：翻页、适应宽度与缩放；改完样式可直接重新预览
- 长文档可随时取消，会一并结束浏览器进程
- 同名文件支持覆盖 / 自动重命名 / 跳过
- 自动记住上次的样式与路径
- 可选实验功能：写入书签与文档元数据

## 实现方式

1. pywebview 打开本地窗口，前端是纯 HTML/CSS/JS（`web/`），通过 `js_api` 调用 Python。
2. 前端只负责收集样式参数、展示进度与日志；所有排版知识（CSS 生成、pandoc 参数、字体回退）都在 Python 侧。
3. 转换在后台线程串行执行：pandoc 生成 HTML，再用 Edge/Chrome 无头模式打印成 PDF。事件写入缓冲区，前端定时增量拉取，因此界面不卡顿，取消也能立即生效。
4. 预览用 pdfium 把 PDF 页面渲染成 PNG，以 base64 显示在界面中。

## 环境要求

- Windows 10/11（依赖系统的 WebView2 运行时，通常已自带；缺失时会弹窗提示下载地址）
- Python 3.10+，推荐 3.12 / 3.13
- pandoc 3.x
- Microsoft Edge 或 Google Chrome 131 及以上（131 起支持 CSS 页码）

## 安装与运行

```bash
python -m pip install -r requirements.txt
python app.py
```

Windows 上也可以直接双击 `run.bat`。

首次启动会自动检测 pandoc 与浏览器；找不到时可在「高级」页手动指定可执行文件路径。

## 配置与日志

- 配置：`%APPDATA%\md2pdf\settings.json`
- 日志：`%APPDATA%\md2pdf\logs\md2pdf.log`
- 便携模式：在程序目录放一个空的 `portable.flag`，配置会改写到程序目录的 `config/`

## 常见问题

**页码不显示**：浏览器需要 131 或更新版本，程序会在环境栏提示。

**图片下方多出 alt 文字**：这是 pandoc 的隐式图注。到「文档」页关闭「自动把图片 alt 文本作为图注」即可；把图片写成 `![](url)`（alt 留空）也不会生成图注。

**写入 PDF 失败**：目标文件正被 PDF 阅读器占用，关闭阅读器后重试。

**公式不显示**：默认使用 MathML，离线可用；选择 MathJax 需要联网加载脚本。

**GBK / 带 BOM 的文件**：会自动识别并转成 UTF-8，无需手动处理。
