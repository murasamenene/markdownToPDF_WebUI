/* ============================================================
   markdownToPDF WebUI · 前端逻辑
   与 Python 的唯一通道是 window.pywebview.api（薄桥接层）。
   渲染、转换、CSS 生成全部在 Python 侧完成，这里只收集参数、展示事件。
   ============================================================ */

(() => {
  "use strict";

  const STATUS_CLASS = {
    "等待": "waiting",
    "转换中…": "running",
    "预览中…": "running",
    "完成": "done",
    "预览完成": "done",
    "失败": "failed",
    "跳过": "skipped",
    "已取消": "cancelled",
  };

  const state = {
    api: null,
    opts: null,
    presets: [],
    env: null,
    settings: null,
    files: [],
    selected: new Set(),
    rowEls: new Map(),
    jobId: "",
    cursor: 0,
    logCursor: 0,
    running: false,
    currentPreview: false,
    job: { total: 0, done: 0, failed: 0, skipped: 0, cancelled: 0 },
    preview: { open: false, page: 0, pages: 0, path: "", seq: 0 },
    previewAvailable: true,
    saveTimer: null,
    pollTimer: null,
    resizeTimer: null,
    dialogCtx: null,
    initialized: false,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function normPath(path) {
    return String(path == null ? "" : path).replace(/\//g, "\\").toLowerCase();
  }

  function basename(path) {
    const text = String(path == null ? "" : path);
    const index = Math.max(text.lastIndexOf("\\"), text.lastIndexOf("/"));
    return index >= 0 ? text.slice(index + 1) : text;
  }

  function shortDir(dir) {
    const text = String(dir == null ? "" : dir);
    return text.length <= 34 ? text : "…" + text.slice(-33);
  }

  function fmtDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return "0 秒";
    if (value < 1) return Math.round(value * 1000) + " ms";
    if (value < 60) return value.toFixed(1) + " 秒";
    return Math.floor(value / 60) + " 分 " + Math.round(value % 60) + " 秒";
  }

  function numOr(raw, fallback) {
    const value = parseFloat(raw);
    return Number.isFinite(value) ? value : fallback;
  }

  function fillSelect(select, items) {
    if (!select) return;
    select.innerHTML = (items || [])
      .map((item) => `<option value="${esc(item.value)}">${esc(item.label)}</option>`)
      .join("");
  }

  // ==========================================================
  // 启动
  // ==========================================================

  document.addEventListener("DOMContentLoaded", () => {
    bindStaticEvents();
    if (window.pywebview && window.pywebview.api) {
      boot();
    } else {
      window.addEventListener("pywebviewready", boot, { once: true });
      setTimeout(() => {
        if (state.initialized) return;
        $("#env-dot").className = "dot bad";
        $("#env-text").textContent = "未连接 Python 后端";
        setStatus("请通过 app.py 或 run.bat 启动本程序", "error");
      }, 2500);
    }
  });

  async function boot() {
    if (state.initialized) return;
    state.api = window.pywebview.api;
    try {
      const [opts, presets, env, settings, fonts] = await Promise.all([
        state.api.options(),
        state.api.presets(),
        state.api.environment("", "", false),
        state.api.load_settings(),
        state.api.fonts(),
      ]);
      state.opts = opts;
      state.presets = presets || [];
      state.settings = settings || {};
      state.previewAvailable = !!(env && env.preview_available) && settings.preview_available !== false;

      fillStaticOptions(opts);
      buildFontList(fonts || []);
      buildPresets(state.presets);
      applyLoadedSettings(settings || {});
      setEnv(env);
      startPolling();
      renderFiles();
      updateSizeHint();
      state.initialized = true;
      setStatus("就绪");
      if (env && env.ok) {
        addLog("info", `环境就绪：Pandoc ${env.pandoc.version || "?"} · ${env.browser.label} ${env.browser.version || "?"}`);
      } else {
        addLog("error", "环境检测未通过：" + ((env && env.error) || "未知错误"));
      }
    } catch (err) {
      console.error(err);
      setStatus("初始化失败", "error");
      showDialog({
        title: "初始化失败",
        message: String((err && err.message) || err),
        hint: "请查看日志文件，或重启应用后重试。",
      });
    }
  }

  function fillStaticOptions(opts) {
    if (!opts) return;
    fillSelect($('[data-style="page_size"]'), opts.page_sizes);
    fillSelect($('[data-style="page_number"]'), opts.page_numbers);
    fillSelect($('[data-style="page_number_format"]'), opts.page_number_formats);
    fillSelect($('[data-style="text_align"]'), opts.text_aligns);
    fillSelect($('[data-style="table_style"]'), opts.table_styles);
    fillSelect($('[data-style="code_theme"]'), opts.code_themes);
    fillSelect($("#conflict"), opts.conflicts);
    fillSelect($('[data-pandoc="math"]'), opts.math);
    fillSelect($('[data-pandoc="language"]'), opts.languages);
    fillSelect($('[data-pandoc="highlight_style"]'), opts.highlights);

    const sizeList = $("#size-list");
    if (sizeList) {
      sizeList.innerHTML = (opts.font_sizes || [])
        .map((item) => `<option value="${item.value}">${esc(item.label)}</option>`)
        .join("");
    }
  }

  function buildFontList(fonts) {
    const list = $("#font-list");
    if (!list) return;
    list.innerHTML = fonts.map((name) => `<option value="${esc(name)}"></option>`).join("");
  }

  function buildPresets(presets) {
    fillSelect($("#preset"), presets.map((item) => ({ value: item.name, label: item.name })));
  }

  // ==========================================================
  // 设置读写
  // ==========================================================

  function applyLoadedSettings(settings) {
    applyStyleToForm(settings.style || {});
    applyPandocToForm(settings.pandoc || {});
    $("#output-dir").value = settings.output_dir || "";
    $("#conflict").value = settings.conflict || "overwrite";
    $("#pandoc-path").value = settings.pandoc_path || "";
    $("#browser-path").value = settings.browser_path || "";
    $("#pdf-info").checked = !!settings.pdf_info;
    $("#show-log").checked = settings.show_log !== false;
    applyLogVisibility();
  }

  function applyStyleToForm(style) {
    $$("[data-style]").forEach((el) => {
      const key = el.dataset.style;
      if (!(key in style)) return;
      const value = style[key];
      if (el.type === "checkbox") {
        el.checked = !!value;
      } else if (el.type === "radio") {
        el.checked = String(value) === el.value;
      } else {
        el.value = value == null ? "" : String(value);
      }
    });
    ["body_color", "heading_color", "accent_color", "link_color"].forEach((key) => {
      if (key in style) applyColorValue(key, style[key]);
    });
    updateSizeHint();
  }

  function applyPandocToForm(pandoc) {
    $$("[data-pandoc]").forEach((el) => {
      const key = el.dataset.pandoc;
      if (!(key in pandoc)) return;
      const value = pandoc[key];
      if (el.type === "checkbox") el.checked = !!value;
      else if (el.type === "number") el.value = value == null ? "" : String(value);
      else el.value = value == null ? "" : String(value);
    });
  }

  function applyColorValue(key, value) {
    const colorEl = $(`input[type="color"][data-style="${key}"]`);
    const textEl = $(`[data-color-text="${key}"]`);
    if (!colorEl || !textEl) return;
    const text = String(value == null ? "" : value).trim();
    textEl.value = text;
    textEl.classList.toggle("invalid", !!text && !/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(text));
    if (/^#[0-9a-f]{6}$/i.test(text)) colorEl.value = text;
    else if (/^#[0-9a-f]{3}$/i.test(text)) colorEl.value = expandHex(text);
  }

  function expandHex(value) {
    const digits = value.slice(1);
    return "#" + digits.split("").map((ch) => ch + ch).join("");
  }

  function syncColorFromText(textEl) {
    const key = textEl.dataset.colorText;
    const colorEl = $(`input[type="color"][data-style="${key}"]`);
    if (!colorEl) return;
    const value = textEl.value.trim();
    if (/^#[0-9a-f]{6}$/i.test(value)) {
      colorEl.value = value;
      textEl.classList.remove("invalid");
    } else if (/^#[0-9a-f]{3}$/i.test(value)) {
      colorEl.value = expandHex(value);
      textEl.classList.remove("invalid");
    } else {
      textEl.classList.toggle("invalid", value.length > 0);
    }
  }

  function collectStyle() {
    const out = {};
    $$("[data-style]").forEach((el) => {
      const key = el.dataset.style;
      if (el.type === "radio") {
        if (el.checked) out[key] = el.value;
        return;
      }
      if (el.type === "checkbox") {
        out[key] = el.checked;
        return;
      }
      if (el.type === "number") {
        const value = parseFloat(el.value);
        if (Number.isFinite(value)) out[key] = value;
        return;
      }
      if (key === "custom_css" || key === "header_text") {
        out[key] = el.value;
        return;
      }
      const value = el.value.trim();
      if (key.endsWith("_color")) {
        if (/^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(value)) out[key] = value;
      } else if (key.endsWith("_font")) {
        out[key] = value;
      } else {
        out[key] = value;
      }
    });
    return out;
  }

  function collectPandoc() {
    const out = {};
    $$("[data-pandoc]").forEach((el) => {
      const key = el.dataset.pandoc;
      if (el.type === "checkbox") {
        out[key] = el.checked;
      } else if (el.type === "number") {
        const value = parseInt(el.value, 10);
        if (Number.isFinite(value)) out[key] = value;
      } else {
        out[key] = el.value;
      }
    });
    return out;
  }

  function collectSettings() {
    return {
      style: collectStyle(),
      pandoc: collectPandoc(),
      output_dir: $("#output-dir").value.trim(),
      conflict: $("#conflict").value || "overwrite",
      pandoc_path: $("#pandoc-path").value.trim(),
      browser_path: $("#browser-path").value.trim(),
      pdf_info: $("#pdf-info").checked,
      show_log: $("#show-log").checked,
    };
  }

  function collectPayload(files, previewOnly) {
    const settings = collectSettings();
    return {
      files,
      output_dir: settings.output_dir,
      conflict: settings.conflict,
      style: settings.style,
      pandoc: settings.pandoc,
      pandoc_path: settings.pandoc_path,
      browser_path: settings.browser_path,
      timeout_pandoc: numOr($("#timeout-pandoc").value, 120),
      timeout_browser: numOr($("#timeout-browser").value, 180),
      keep_temp: $("#keep-temp").checked,
      add_pdf_info: settings.pdf_info,
      preview_only: !!previewOnly,
    };
  }

  function scheduleSave() {
    if (state.saveTimer) clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(() => {
      state.saveTimer = null;
      persistSettings();
    }, 150);
  }

  async function persistSettings() {
    if (!state.api) return;
    try {
      await state.api.save_settings(collectSettings());
    } catch (err) {
      console.error("保存设置失败", err);
    }
  }

  function markDirty() {
    if (state.preview.open) {
      $("#preview-hint").textContent = "样式已修改，点“预览选中文件”查看效果";
    }
  }

  function updateSizeHint() {
    const input = $('[data-style="font_size_pt"]');
    const hint = $("#size-hint");
    if (!input || !hint) return;
    const value = parseFloat(input.value);
    const sizes = (state.opts && state.opts.font_sizes) || [];
    const match = sizes.find((item) => Math.abs(Number(item.value) - value) < 0.01);
    hint.textContent = match ? `≈ ${match.label}` : "";
  }

  function applyLogVisibility() {
    $("#log-panel").classList.toggle("hidden", !$("#show-log").checked);
  }

  // ==========================================================
  // 事件绑定
  // ==========================================================

  function bindStaticEvents() {
    $$("#tabs .tab").forEach((btn) => {
      btn.addEventListener("click", () => activateTab(btn.dataset.tab));
    });

    $("#btn-add-files").addEventListener("click", addFilesDialog);
    $("#btn-add-folder").addEventListener("click", addFolderDialog);
    $("#btn-remove").addEventListener("click", removeSelected);
    $("#btn-clear").addEventListener("click", clearFiles);
    $("#btn-output-browse").addEventListener("click", browseOutput);
    $("#btn-output-open").addEventListener("click", openOutputDir);

    $("#file-body").addEventListener("click", onFileClick);
    $("#file-body").addEventListener("dblclick", onFileDoubleClick);

    $("#btn-apply-preset").addEventListener("click", applyPreset);
    $("#btn-pick-pandoc").addEventListener("click", () => pickExe("pandoc"));
    $("#btn-pick-browser").addEventListener("click", () => pickExe("browser"));

    $$("[data-font-reset]").forEach((btn) => {
      btn.addEventListener("click", () => resetFont(btn.dataset.fontReset));
    });

    $$('input[type="color"][data-style]').forEach((el) => {
      el.addEventListener("input", () => {
        const textEl = $(`[data-color-text="${el.dataset.style}"]`);
        if (textEl) {
          textEl.value = el.value;
          textEl.classList.remove("invalid");
        }
      });
    });

    $("#app").addEventListener("input", onFormInput);
    $("#app").addEventListener("change", onFormInput);

    $("#btn-preview").addEventListener("click", previewSelected);
    $("#btn-prev").addEventListener("click", () => gotoPage(-1));
    $("#btn-next").addEventListener("click", () => gotoPage(1));
    $("#zoom").addEventListener("change", () => renderPreview());
    $("#btn-open-pdf").addEventListener("click", openPdf);

    $("#btn-start").addEventListener("click", () => startJob(false));
    $("#btn-cancel").addEventListener("click", cancelJob);

    $("#show-log").addEventListener("change", applyLogVisibility);
    $("#btn-clear-log").addEventListener("click", () => { $("#log-list").innerHTML = ""; });
    $("#btn-open-log").addEventListener("click", openLogDir);

    $("#btn-redetect").addEventListener("click", () => redetect(true));
    $("#env-text").addEventListener("click", () => {
      if (state.env && !state.env.ok) showEnvError();
    });

    $("#dialog-ok").addEventListener("click", () => closeDialog(true));
    $("#dialog-cancel").addEventListener("click", () => closeDialog(false));
    $("#dialog-close").addEventListener("click", () => closeDialog(false));
    $("#dialog-copy").addEventListener("click", copyDialog);
    $("#dialog-backdrop").addEventListener("click", (event) => {
      if (event.target === $("#dialog-backdrop")) closeDialog(false);
    });

    window.addEventListener("resize", onResize);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("beforeunload", () => {
      if (state.api && state.saveTimer) {
        clearTimeout(state.saveTimer);
        state.saveTimer = null;
        persistSettings();
      }
    });
  }

  function activateTab(name) {
    $$("#tabs .tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === name));
    $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
  }

  function onFormInput(event) {
    const el = event.target;
    if (el.matches("[data-color-text]")) {
      syncColorFromText(el);
      markDirty();
      scheduleSave();
      return;
    }
    if (el.matches("[data-style]")) {
      markDirty();
      if (el.dataset.style === "font_size_pt") updateSizeHint();
      scheduleSave();
      return;
    }
    if (el.matches("[data-pandoc]")) {
      markDirty();
      scheduleSave();
      return;
    }
    if (el.matches("#output-dir, #conflict, #pandoc-path, #browser-path, #pdf-info, #show-log")) {
      scheduleSave();
    }
  }

  function onKeyDown(event) {
    const tag = (event.target && event.target.tagName) || "";
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag);
    if (event.key === "Escape" && !$("#dialog-backdrop").hidden) {
      closeDialog(false);
      return;
    }
    if (event.ctrlKey && event.key.toLowerCase() === "o") {
      event.preventDefault();
      addFilesDialog();
      return;
    }
    if (event.ctrlKey && event.key === "Enter") {
      event.preventDefault();
      startJob(false);
      return;
    }
    if (!typing && (event.key === "Delete" || event.key === "Backspace") && state.selected.size) {
      event.preventDefault();
      removeSelected();
    }
  }

  function onResize() {
    if (state.resizeTimer) clearTimeout(state.resizeTimer);
    state.resizeTimer = setTimeout(() => {
      state.resizeTimer = null;
      if (state.preview.open && $("#zoom").value === "fit") renderPreview();
    }, 220);
  }

  // ==========================================================
  // 环境
  // ==========================================================

  function setEnv(env) {
    state.env = env;
    const dot = $("#env-dot");
    const text = $("#env-text");
    if (env && env.ok) {
      const pandoc = env.pandoc || {};
      const browser = env.browser || {};
      let label = `Pandoc ${pandoc.version || "?"} · ${browser.label || "浏览器"} ${browser.version || "?"}`;
      if (browser.margin_boxes === false) label += "（版本过旧，页码不可用）";
      text.textContent = label;
      text.title = `${pandoc.path || ""}\n${browser.path || ""}`;
      dot.className = "dot ok";
    } else {
      const error = (env && env.error) || "未找到 pandoc 或浏览器";
      text.textContent = "环境异常：" + error.split("\n")[0];
      text.title = error;
      dot.className = "dot bad";
    }
    state.previewAvailable = !!(env && env.preview_available) && state.settings.preview_available !== false;
    setRunning(state.running);
  }

  async function redetect(showResult) {
    if (!state.api) return null;
    setStatus("正在检测环境…");
    try {
      const env = await state.api.environment(
        $("#pandoc-path").value.trim(),
        $("#browser-path").value.trim(),
        true,
      );
      setEnv(env);
      setStatus(env.ok ? "环境就绪" : "环境检测失败", env.ok ? "ok" : "error");
      if (env.ok) {
        addLog("info", `环境就绪：Pandoc ${env.pandoc.version || "?"} · ${env.browser.label} ${env.browser.version || "?"}`);
      } else {
        addLog("error", "环境检测失败：" + env.error);
        if (showResult) showEnvError();
      }
      return env;
    } catch (err) {
      setStatus("环境检测失败", "error");
      addLog("error", "环境检测失败：" + String(err));
      if (showResult) showDialog({ title: "环境检测失败", message: String(err) });
      return null;
    }
  }

  function showEnvError() {
    const env = state.env;
    if (!env || env.ok) return;
    showDialog({
      title: "环境检测失败",
      message: env.error || "未找到 pandoc 或浏览器",
      hint: "安装 pandoc 与 Edge/Chrome，或在“高级”页手动指定可执行文件路径，然后点“重新检测”。",
    });
  }

  // ==========================================================
  // 文件列表
  // ==========================================================

  async function addFilesDialog() {
    if (!state.api) return;
    try {
      const paths = await state.api.pick_files();
      await addPaths(paths);
    } catch (err) {
      showDialog({ title: "添加文件失败", message: String((err && err.message) || err) });
    }
  }

  async function addFolderDialog() {
    if (!state.api) return;
    try {
      const dir = await state.api.pick_folder("input");
      if (dir) await addPaths([dir]);
    } catch (err) {
      showDialog({ title: "添加文件夹失败", message: String((err && err.message) || err) });
    }
  }

  async function addPaths(paths) {
    if (!paths || !paths.length) return;
    const scanned = await state.api.scan(paths);
    const existing = new Set(state.files.map((file) => normPath(file.path)));
    let added = 0;
    let duplicated = 0;
    (scanned || []).forEach((item) => {
      const key = normPath(item.path);
      if (existing.has(key)) {
        duplicated++;
        return;
      }
      existing.add(key);
      state.files.push({
        ...item,
        status: "等待",
        pages: "",
        output: "",
        error: null,
      });
      added++;
    });
    renderFiles();
    if (added) setStatus(`已添加 ${added} 个文件`);
    else if (duplicated) setStatus("这些文件已经在列表里了");
    else setStatus("没有找到支持的 Markdown / 文本文件", "warning");
  }

  function renderFiles() {
    const body = $("#file-body");
    body.innerHTML = "";
    state.rowEls.clear();
    state.files.forEach((file) => {
      const tr = document.createElement("tr");
      tr.dataset.path = file.path;
      tr.title = file.error
        ? `${file.error.message}${file.error.hint ? "（" + file.error.hint + "）" : ""}`
        : file.path;
      if (state.selected.has(file.path)) tr.classList.add("selected");
      tr.innerHTML = '<td class="c-name"></td><td class="c-dir muted"></td>'
        + '<td class="c-status"><span class="badge"></span></td><td class="c-pages"></td>';
      tr.children[0].textContent = file.name;
      tr.children[1].textContent = shortDir(file.dir);
      updateRowContent(file, tr);
      body.appendChild(tr);
      state.rowEls.set(file.path, tr);
    });
    $("#file-empty").hidden = state.files.length > 0;
    updateFileCount();
    updateCounters();
  }

  function updateRowContent(file, tr) {
    const badge = tr.querySelector(".badge");
    if (badge) {
      badge.className = "badge " + (STATUS_CLASS[file.status] || "waiting");
      badge.textContent = file.status;
    }
    const pages = tr.querySelector(".c-pages");
    if (pages) pages.textContent = file.pages ? `${file.pages} 页` : "";
    tr.title = file.error
      ? `${file.error.message}${file.error.hint ? "（" + file.error.hint + "）" : ""}`
      : file.path;
  }

  function updateRow(file) {
    const tr = state.rowEls.get(file.path);
    if (!tr) {
      renderFiles();
      return;
    }
    updateRowContent(file, tr);
  }

  function updateFileCount() {
    $("#file-count").textContent = `${state.files.length} 个文件`;
  }

  function updateCounters() {
    const job = state.job;
    const parts = [];
    if (job.done) parts.push(`完成 ${job.done}`);
    if (job.failed) parts.push(`失败 ${job.failed}`);
    if (job.skipped) parts.push(`跳过 ${job.skipped}`);
    if (job.cancelled) parts.push(`取消 ${job.cancelled}`);
    $("#job-counters").textContent = parts.join(" · ");
  }

  function onFileClick(event) {
    const tr = event.target.closest("tr[data-path]");
    if (!tr) return;
    const file = findFile(tr.dataset.path);
    if (!file) return;
    if (event.target.closest(".badge.failed")) {
      showFileError(file);
      return;
    }
    if (event.ctrlKey || event.metaKey) toggleSelected(file.path);
    else selectOnly(file.path);
  }

  function onFileDoubleClick(event) {
    const tr = event.target.closest("tr[data-path]");
    if (!tr) return;
    const file = findFile(tr.dataset.path);
    if (!file) return;
    state.selected = new Set([file.path]);
    renderFiles();
    previewFile(file);
  }

  function findFile(path) {
    const key = normPath(path);
    return state.files.find((file) => normPath(file.path) === key) || null;
  }

  function selectOnly(path) {
    state.selected = new Set([path]);
    renderFiles();
  }

  function toggleSelected(path) {
    if (state.selected.has(path)) state.selected.delete(path);
    else state.selected.add(path);
    renderFiles();
  }

  function removeSelected() {
    if (!state.selected.size) {
      setStatus("请先选中要移除的文件", "warning");
      return;
    }
    state.files = state.files.filter((file) => !state.selected.has(file.path));
    state.selected.clear();
    renderFiles();
    setStatus("已移除所选文件");
  }

  async function clearFiles() {
    if (!state.files.length) return;
    if (state.running) {
      showDialog({ title: "任务进行中", message: "请先取消当前任务，再清空文件列表。" });
      return;
    }
    const ok = await showConfirm(
      "清空文件列表",
      `确定要移除列表中的 ${state.files.length} 个文件吗？不会删除磁盘上的文件。`,
    );
    if (!ok) return;
    state.files = [];
    state.selected.clear();
    renderFiles();
    setStatus("已清空文件列表");
  }

  function previewTarget() {
    for (const file of state.files) {
      if (state.selected.has(file.path)) return file;
    }
    return state.files[0] || null;
  }

  function showFileError(file) {
    if (!file || !file.error) return;
    showDialog({
      title: `转换失败：${file.name}`,
      message: file.error.message,
      detail: file.error.detail,
      hint: file.error.hint,
    });
  }

  async function browseOutput() {
    if (!state.api) return;
    try {
      const dir = await state.api.pick_folder("output", $("#output-dir").value.trim());
      if (dir) {
        $("#output-dir").value = dir;
        scheduleSave();
      }
    } catch (err) {
      showDialog({ title: "选择输出目录失败", message: String((err && err.message) || err) });
    }
  }

  async function openOutputDir() {
    let target = $("#output-dir").value.trim();
    if (!target && state.files.length) target = state.files[0].dir;
    if (!target) {
      showDialog({ title: "没有可打开的目录", message: "请先添加文件或指定输出目录。" });
      return;
    }
    await openPath(target);
  }

  async function openPath(path) {
    if (!state.api) return;
    const result = await state.api.open_path(String(path || ""));
    if (!result || !result.ok) {
      showDialog({
        title: "无法打开",
        message: (result && result.error) || `路径不存在：${path}`,
      });
    }
  }

  async function openLogDir() {
    const dir = state.env && state.env.log_dir;
    if (!dir) {
      setStatus("还没有日志目录", "warning");
      return;
    }
    await openPath(dir);
  }

  async function pickExe(kind) {
    if (!state.api) return;
    try {
      const path = await state.api.pick_exe(kind);
      if (!path) return;
      $(kind === "pandoc" ? "#pandoc-path" : "#browser-path").value = path;
      scheduleSave();
      await redetect(false);
    } catch (err) {
      showDialog({ title: "选择可执行文件失败", message: String((err && err.message) || err) });
    }
  }

  function applyPreset() {
    const name = $("#preset").value;
    const item = state.presets.find((preset) => preset.name === name);
    if (!item) return;
    applyStyleToForm(item.style || {});
    markDirty();
    scheduleSave();
    setStatus(`已应用预设：${name}`);
  }

  function resetFont(key) {
    const defaults = (state.opts && state.opts.defaults && state.opts.defaults.style) || {};
    const el = $(`[data-style="${key}"]`);
    if (!el) return;
    el.value = defaults[key] == null ? "" : String(defaults[key]);
    markDirty();
    scheduleSave();
  }

  // ==========================================================
  // 任务
  // ==========================================================

  function setRunning(running) {
    state.running = running;
    $("#btn-start").disabled = running;
    $("#btn-preview").disabled = running || !state.previewAvailable;
    $("#btn-cancel").disabled = !running;
  }

  function setStatus(text, kind) {
    const el = $("#status");
    el.textContent = text;
    el.dataset.kind = kind || "";
  }

  function setProgress(percent) {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    $("#progress-bar").style.width = value + "%";
  }

  async function previewSelected() {
    if (!state.api || state.running) return;
    if (!state.previewAvailable) {
      showDialog({
        title: "无法预览",
        message: "缺少 pypdfium2，无法渲染 PDF 预览。",
        hint: "安装可选依赖：python -m pip install -r requirements-optional.txt",
      });
      return;
    }
    if (!state.env || !state.env.ok) {
      const env = await redetect(false);
      if (!env || !env.ok) {
        showEnvError();
        return;
      }
    }
    const target = previewTarget();
    if (!target) {
      showDialog({ title: "没有文件", message: "请先添加并选中一个文件。" });
      return;
    }
    await startJob(true, [target.path]);
  }

  function previewFile(file) {
    if (state.running) {
      setStatus("任务进行中，请稍候", "warning");
      return;
    }
    startJob(true, [file.path]);
  }

  async function startJob(previewOnly, onlyFiles) {
    if (!state.api || state.running) return;
    const files = onlyFiles || state.files.map((file) => file.path);
    if (!files.length) {
      showDialog({
        title: previewOnly ? "没有可预览的文件" : "没有可转换的文件",
        message: previewOnly ? "请先添加并选中一个文件。" : "请先添加要转换的文件。",
      });
      return;
    }
    if (!previewOnly && (!state.env || !state.env.ok)) {
      const env = await redetect(false);
      if (!env || !env.ok) {
        showEnvError();
        return;
      }
    }

    const targets = new Set(files.map(normPath));
    state.files.forEach((file) => {
      if (!targets.has(normPath(file.path))) return;
      file.status = "等待";
      file.pages = "";
      file.output = "";
      file.error = null;
      updateRow(file);
    });

    state.jobId = "";
    state.cursor = 0;
    state.job = { total: files.length, done: 0, failed: 0, skipped: 0, cancelled: 0 };
    state.currentPreview = !!previewOnly;
    setRunning(true);
    setProgress(0);
    updateCounters();
    setStatus(previewOnly ? "正在生成预览…" : "正在准备…");
    $("#preview-hint").textContent = "";
    $("#preview-meta").textContent = "";

    try {
      state.jobId = await state.api.start_job(collectPayload(files, previewOnly));
      scheduleSave();
    } catch (err) {
      setRunning(false);
      setStatus("无法启动任务", "error");
      showDialog({ title: "无法启动任务", message: String((err && err.message) || err) });
    }
  }

  async function cancelJob() {
    if (!state.api || !state.running || !state.jobId) return;
    setStatus("正在取消…", "warning");
    try {
      await state.api.cancel_job(state.jobId);
    } catch (err) {
      console.error(err);
    }
  }

  // ==========================================================
  // 轮询
  // ==========================================================

  function startPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(pollOnce, 140);
  }

  let pollInFlight = false;

  async function pollOnce() {
    if (!state.api || pollInFlight) return;
    pollInFlight = true;
    try {
      const res = await state.api.poll(state.jobId || "", state.cursor, state.logCursor);
      if (!res) return;
      if (typeof res.log_cursor === "number") state.logCursor = res.log_cursor;
      (res.logs || []).forEach((item) => addLog(item.level, item.text));
      if (res.job_id && res.job_id === state.jobId) {
        state.cursor = res.cursor || 0;
        (res.events || []).forEach(handleEvent);
        if (!res.running && state.running) {
          // 兜底：done 事件丢失（例如事件缓冲被截断）时恢复按钮
          setRunning(false);
        }
      }
    } catch (err) {
      // 窗口关闭或后端重启时静默
    } finally {
      pollInFlight = false;
    }
  }

  function handleEvent(event) {
    switch (event.type) {
      case "queued":
        state.job.total = Number(event.total) || state.job.total;
        setStatus(`共 ${state.job.total} 个文件，准备开始…`);
        break;
      case "row":
        applyRowEvent(event);
        break;
      case "stage": {
        const file = findFile(event.file);
        setStatus(`${file ? file.name : basename(event.file)}：${event.text}`);
        break;
      }
      case "warning":
        addLog("warning", `[${basename(event.file)}] ${event.text}`);
        break;
      case "error": {
        const file = event.file ? findFile(event.file) : null;
        if (file) {
          file.error = { message: event.message || "", detail: event.detail || "", hint: event.hint || "" };
          updateRow(file);
        }
        addLog("error", (event.file ? `[${basename(event.file)}] ` : "")
          + (event.message || "")
          + (event.hint ? `（${event.hint}）` : ""));
        break;
      }
      case "done":
        finishJob(event);
        break;
      default:
        break;
    }
  }

  function applyRowEvent(event) {
    const file = findFile(event.file);
    const isPreview = !!event.kind ? event.kind === "preview" : state.currentPreview;
    if (file) {
      switch (event.status) {
        case "running":
          file.status = isPreview ? "预览中…" : "转换中…";
          break;
        case "done":
          file.status = isPreview ? "预览完成" : "完成";
          file.pages = event.pages || "";
          file.output = event.output || "";
          break;
        case "failed":
          file.status = "失败";
          break;
        case "skipped":
          file.status = "跳过";
          break;
        case "cancelled":
          file.status = "已取消";
          break;
        default:
          break;
      }
      updateRow(file);
    }
    if (event.status === "running") return;
    if (event.status === "done") state.job.done++;
    else if (event.status === "failed") state.job.failed++;
    else if (event.status === "skipped") state.job.skipped++;
    else if (event.status === "cancelled") state.job.cancelled++;
    if (event.total) state.job.total = Number(event.total) || state.job.total;
    const finished = state.job.done + state.job.failed + state.job.skipped + state.job.cancelled;
    setProgress((finished / Math.max(1, state.job.total)) * 100);
    updateCounters();
  }

  function finishJob(event) {
    setRunning(false);
    const isPreview = !!event.preview;
    if (event.cancelled) {
      setStatus("已取消", "warning");
      addLog("warning", "任务已取消。");
    } else if (event.error === "busy") {
      setStatus("已有任务在进行中", "warning");
    } else if (event.error === "empty") {
      setStatus("没有可转换的文件", "warning");
    } else if (event.error) {
      setStatus(isPreview ? "预览失败" : "转换失败", "error");
      showDialog({ title: isPreview ? "预览失败" : "转换失败", message: event.error });
    } else if (isPreview) {
      if (event.failed > 0) setStatus("预览失败", "error");
      else setStatus(`预览完成（${fmtDuration(event.elapsed)}）`, "ok");
    } else {
      setProgress(100);
      const text = `完成 ${event.ok} 个，失败 ${event.failed} 个，跳过 ${event.skipped} 个（${fmtDuration(event.elapsed)}）`;
      setStatus(text, event.failed ? "warning" : "ok");
      addLog(event.failed ? "warning" : "info", text);
    }
    if (event.last_output) openPreview(event.last_output);
  }

  // ==========================================================
  // 预览
  // ==========================================================

  async function openPreview(path) {
    if (!state.api || !path) return;
    try {
      const result = await state.api.open_preview(String(path));
      if (!result || !result.ok) {
        setPreviewPlaceholder("无法预览：" + ((result && result.error) || "未知错误"));
        return;
      }
      const preview = state.preview;
      preview.open = true;
      preview.path = String(path);
      preview.page = 1;
      preview.pages = result.pages || 0;
      $("#preview-hint").textContent = "";
      $("#preview-body").scrollTop = 0;
      await renderPreview();
    } catch (err) {
      setPreviewPlaceholder("无法预览：" + String((err && err.message) || err));
    }
  }

  function setPreviewPlaceholder(text) {
    state.preview.open = false;
    state.preview.pages = 0;
    state.preview.page = 0;
    const img = $("#preview-img");
    img.hidden = true;
    img.removeAttribute("src");
    img.style.width = "";
    const placeholder = $("#preview-placeholder");
    placeholder.hidden = false;
    placeholder.textContent = text;
    $("#page-label").textContent = "第 0 / 0 页";
    $("#preview-meta").textContent = "";
  }

  async function renderPreview() {
    const preview = state.preview;
    if (!preview.open || !state.api) return;
    const seq = ++preview.seq;
    const zoom = $("#zoom").value;
    const body = $("#preview-body");
    const fitWidth = Math.max(240, Math.floor(body.clientWidth - 40));
    try {
      const result = zoom === "fit"
        ? await state.api.preview_page(preview.page, fitWidth, null)
        : await state.api.preview_page(preview.page, 0, (96 / 72) * parseFloat(zoom));
      if (seq !== preview.seq) return;
      if (!result || !result.ok) {
        setPreviewPlaceholder("渲染失败：" + ((result && result.error) || "未知错误"));
        return;
      }
      preview.page = result.page;
      preview.pages = result.pages;
      const img = $("#preview-img");
      img.hidden = false;
      img.src = result.png;
      img.style.width = result.width + "px";
      img.style.height = "auto";
      $("#preview-placeholder").hidden = true;
      $("#page-label").textContent = `第 ${preview.page} / ${preview.pages} 页`;
      $("#preview-meta").textContent = `${result.width} × ${result.height} px`;
    } catch (err) {
      if (seq === preview.seq) setPreviewPlaceholder("渲染失败：" + String((err && err.message) || err));
    }
  }

  function gotoPage(delta) {
    const preview = state.preview;
    if (!preview.open) return;
    const next = preview.page + delta;
    if (next < 1 || next > preview.pages) return;
    preview.page = next;
    renderPreview();
  }

  function openPdf() {
    if (!state.preview.open || !state.preview.path) {
      setStatus("还没有可打开的 PDF", "warning");
      return;
    }
    openPath(state.preview.path);
  }

  // ==========================================================
  // 日志
  // ==========================================================

  function addLog(level, text) {
    const message = String(text == null ? "" : text).trim();
    if (!message) return;
    const list = $("#log-list");
    if (!list) return;
    const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 10;
    const line = document.createElement("div");
    line.className = "log-line " + (level === "error" || level === "warning" ? level : "info");
    line.textContent = message;
    list.appendChild(line);
    while (list.childElementCount > 2000) list.firstElementChild.remove();
    if (atBottom) list.scrollTop = list.scrollHeight;
  }

  // ==========================================================
  // 对话框
  // ==========================================================

  function showDialog(options) {
    return openDialog({ ...options, confirm: false });
  }

  function showConfirm(title, message, hint) {
    return openDialog({ title, message, hint: hint || "", confirm: true });
  }

  function openDialog({ title = "提示", message = "", detail = "", hint = "", confirm = false }) {
    if (state.dialogCtx) {
      const previous = state.dialogCtx;
      state.dialogCtx = null;
      previous.resolve(false);
    }
    $("#dialog-title").textContent = title || "提示";
    const body = $("#dialog-body");
    body.innerHTML = "";
    if (message) {
      const p = document.createElement("p");
      p.className = "dialog-message";
      p.textContent = message;
      body.appendChild(p);
    }
    if (hint) {
      const p = document.createElement("p");
      p.className = "dialog-hint";
      p.textContent = "建议：" + hint;
      body.appendChild(p);
    }
    if (detail) {
      const pre = document.createElement("pre");
      pre.className = "dialog-detail";
      pre.textContent = detail;
      body.appendChild(pre);
    }
    $("#dialog-ok").textContent = confirm ? "确定" : "知道了";
    $("#dialog-cancel").hidden = !confirm;
    $("#dialog-copy").hidden = !(message || hint || detail);
    $("#dialog-backdrop").hidden = false;
    $("#dialog-ok").focus();
    return new Promise((resolve) => {
      state.dialogCtx = { resolve };
    });
  }

  function closeDialog(ok) {
    const ctx = state.dialogCtx;
    state.dialogCtx = null;
    $("#dialog-backdrop").hidden = true;
    if (ctx) ctx.resolve(ok);
  }

  async function copyDialog() {
    const parts = [
      $("#dialog-title").textContent,
      $("#dialog-body").innerText,
    ].filter((part) => part && part.trim());
    const ok = await copyText(parts.join("\n"));
    if (ok) setStatus("已复制到剪贴板");
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      try {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
        return true;
      } catch (inner) {
        return false;
      }
    }
  }
})();
