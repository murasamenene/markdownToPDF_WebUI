# markdownToPDF WebUI

[简体中文](README.md) | **English**

A desktop app that converts Markdown / plain text into well-typeset PDFs. The UI is built with pywebview (WebView2) + HTML/CSS/JS, and conversion uses **pandoc + Edge/Chrome's print engine**. Everything runs locally; no files are uploaded.

## Features

- Batch conversion for `.md` `.markdown` `.mdown` `.mkd` `.txt` `.rst` `.org`
- Chinese typography: first-line indent, justified text, font fallback, page numbers, headers
- Style panel with six tabs (Page / Font / Paragraph / Element / Document / Advanced) and five presets
- Table of contents, section numbering, three table styles, syntax highlighting, MathML math, footnotes, images
- Built-in PDF preview with paging, fit-to-width and zoom; re-preview after style changes
- Cancel long jobs at any time; the browser process tree is terminated as well
- Conflict handling for existing files: overwrite / auto-rename / skip
- Remembers your styles and paths between runs
- Optional experimental bookmarks and document metadata

## How it works

1. pywebview opens a native window hosting a plain HTML/CSS/JS UI (`web/`); both sides communicate through `js_api`.
2. The front end only collects style options and renders progress/log events. All typesetting knowledge (CSS generation, pandoc arguments, font fallback) lives in Python.
3. Conversion runs serially in a background thread: pandoc produces HTML, then headless Edge/Chrome prints it to PDF. Events are buffered and polled incrementally, so the UI stays responsive and Cancel takes effect immediately.
4. Preview renders PDF pages to PNG with pdfium and displays them as base64 images.

## Requirements

- Windows 10/11 (uses the system WebView2 runtime, usually preinstalled; a download link is shown if missing)
- Python 3.10+, 3.12 / 3.13 recommended
- pandoc 3.x
- Microsoft Edge or Google Chrome 131+ (page numbers require Chromium 131)

## Install & run

```bash
python -m pip install -r requirements.txt
python app.py
```

On Windows you can also double-click `run.bat`.

pandoc and the browser are detected on startup. If detection fails, specify their paths in the **Advanced** tab.

## Configuration & logs

- Settings: `%APPDATA%\md2pdf\settings.json`
- Logs: `%APPDATA%\md2pdf\logs\md2pdf.log`
- Portable mode: place an empty `portable.flag` next to the app; settings go to `config/` inside the app folder.

## FAQ

**Page numbers are missing**: requires Chromium 131+. The environment badge will warn you.

**An "image" text appears under pictures**: this is pandoc's implicit figure caption. Turn off "Use image alt text as figure caption" in the **Document** tab, or write `![](url)` with an empty alt text.

**Writing the PDF fails**: the target file is open in a PDF reader. Close it and retry.

**Math is not rendered**: MathML is used by default and works offline; MathJax requires an internet connection.

**GBK / BOM encoded files**: detected and converted to UTF-8 automatically.
