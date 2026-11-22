(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = { config: null, bots: [], tasks: [], activeId: null, view: "chat" };

  function applySidebarCollapsed(collapsed) {
    var app = $("app"), side = document.querySelector(".sidebar");
    var btn = $("sidebar-toggle");
    if (app) app.classList.toggle("sidebar-collapsed", !!collapsed);
    if (side) side.classList.toggle("collapsed", !!collapsed);
    if (btn) {
      btn.textContent = collapsed ? "›" : "‹";
      btn.title = collapsed ? "Развернуть левую панель" : "Свернуть левую панель";
      btn.setAttribute("aria-label", btn.title);
    }
  }
  if (window.SYNX_UI_STATE && window.SYNX_UI_STATE.sidebar_collapsed) {
    applySidebarCollapsed(true);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtTime(v) {
    if (!v) return "";
    var d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function fmtSec(v) {
    v = Number(v || 0);
    if (v < 60) return v.toFixed(0) + "с";
    return Math.floor(v / 60) + "м " + Math.round(v % 60) + "с";
  }
  function toast(msg, err) {
    var t = $("toast");
    t.textContent = msg;
    t.className = "toast show" + (err ? " err" : "");
    clearTimeout(t._timer);
    t._timer = setTimeout(function () { t.className = "toast"; }, 4200);
  }
  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    var r = await fetch(path, opts);
    var text = await r.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
    if (!r.ok) throw new Error((data && data.detail) ? data.detail : (text || r.statusText));
    return data;
  }

  // ---------- navigation ----------
  function setView(v) {
    state.view = v;
    document.querySelectorAll(".nav-btn").forEach(function (b) { b.classList.toggle("active", b.dataset.view === v); });
    document.querySelectorAll(".view").forEach(function (x) { x.classList.toggle("active", x.id === "view-" + v); });
    var titles = {
      tasks: ["Задачи", "Очередь производства и этапы выполнения"],
      chat: ["Чат", "Диалог с моделью, референсы и генерация изображений в ComfyUI"],
      settings: ["Настройки", "AI, synx, ComfyUI, боты и системный промпт"]
    };
    $("view-title").textContent = titles[v][0];
    $("view-subtitle").textContent = titles[v][1];
    if (v === "tasks") loadTasks();
    if (v === "chat") loadChat();
    if (v === "settings") loadSettings();
  }
  document.querySelectorAll(".nav-btn").forEach(function (b) {
    b.addEventListener("click", function () { setView(b.dataset.view); });
  });
  var sidebarToggle = $("sidebar-toggle");
  if (sidebarToggle) {
    sidebarToggle.addEventListener("click", function () {
      var collapsed = !$("app").classList.contains("sidebar-collapsed");
      applySidebarCollapsed(collapsed);
      saveSidebarCollapsed(collapsed);
    });
  }


  // ---------- chat ----------
  var chatAttachments = [];
  var chatSession = "default";

  function mdHtml(text) {
    if (!text) return "";
    if (window.marked) {
      var h = window.marked.parse(text);
      return window.DOMPurify ? window.DOMPurify.sanitize(h) : h;
    }
    return esc(text).replace(/\n/g, "<br>");
  }
  function isImageRef(im) {
    if (!im) return false;
    if (im.kind === "file") return false;
    if (im.kind === "image") return true;
    var mime = String(im.mime || "").toLowerCase();
    if (mime) return mime.indexOf("image/") === 0;
    var p = String(im.path || im.name || im.url || "").toLowerCase();
    return /\.(png|jpe?g|webp|gif|bmp|tiff?)$/.test(p);
  }
  function fileSizeText(n) {
    n = Number(n || 0);
    if (!n) return "";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " МБ";
    if (n >= 1024) return Math.round(n / 1024) + " КБ";
    return n + " Б";
  }
  function chatFileHtml(im) {
    var path = im.path || "";
    var size = fileSizeText(im.size);
    var meta = [im.mime || "файл", size].filter(Boolean).join(" · ");
    return '<div class="msg-file" data-chat-file-path="' + esc(path) + '" title="Открыть файл">' +
      '<span class="msg-file-icon">▤</span><span class="msg-file-body">' +
      '<span class="msg-file-name">' + esc(im.name || "файл") + '</span>' +
      '<span class="msg-file-meta">' + esc(meta) + '</span></span></div>';
  }
  function messageMarkdown(m) {
    var md = String((m && m.content) || "");
    if (m && m.role === "user" && (m.images || []).length) {
      md += "\n";
      (m.images || []).forEach(function (im) {
        var url = im.url || ("/api/chat/file/uploads/" + (im.name || ""));
        if (isImageRef(im)) {
          md += "\n![" + (im.name || "image") + "](" + url + ")";
        } else {
          md += "\n[Файл: " + (im.name || "file") + "](" + url + ")";
        }
        if (im.path) md += "\nФайл: `" + im.path + "`";
      });
    }
    return md;
  }
  async function exportChat() {
    try {
      var d = await api("/api/chat/export?session_id=" + encodeURIComponent(chatSession));
      if (window.pywebview && window.pywebview.api && window.pywebview.api.save_text_file) {
        var r = await window.pywebview.api.save_text_file(d.markdown, d.filename);
        if (r && r.ok) toast("Экспортировано: " + r.path);
        else if (r && !r.cancelled) toast("Экспорт: " + (r.error || "ошибка"), true);
      } else {
        var blob = new Blob([d.markdown], {type: "text/markdown;charset=utf-8"});
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob); a.download = d.filename; a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
      }
    } catch (e) { toast("Экспорт: " + e.message, true); }
  }
  $("chat-export").addEventListener("click", exportChat);
  $("chat-context").addEventListener("click", function () { $("chat-context-file").click(); });
  $("chat-context-file").addEventListener("change", async function () {
    var f = this.files && this.files[0];
    if (!f) return;
    try {
      var text = await f.text();
      var d = await api("/api/chat/context", {method: "POST",
        body: JSON.stringify({session_id: chatSession, markdown: text})});
      toast("Контекст загружен: " + d.length + " символов");
      this.value = "";
    } catch (e) { toast("Контекст: " + e.message, true); }
  });
  function chatImageHtml(im, generated) {
    var url = im.url || ("/api/chat/file/images/" + im.name);
    return '<img src="' + esc(url) + '" data-chat-image="1" data-title="' + esc(im.name || "image") + '" alt="">';
  }
  function renderChatMessages(msgs) {
    var box = $("chat-messages");
    if (!msgs || !msgs.length) {
      box.innerHTML = '<div class="chat-empty"><b>Чат с моделью</b><br><br>Спроси что-нибудь или попроси:<br>«Нарисуй кадр в стиле чёрной туши по этим референсам».<br>Референсы любого типа можно прикрепить кнопкой «+ Референс» или перетащить в поле ввода.</div>';
      return;
    }
    box.innerHTML = "";
    msgs.forEach(function (m) {
      var isUser = m.role === "user";
      var wrap = document.createElement("div");
      wrap.className = "msg " + (isUser ? "user" : "assistant");
      wrap.__msg = m;
      var refs = (isUser ? m.images : m.generated) || [];
      var imgs = refs.filter(isImageRef);
      var files = refs.filter(function (im) { return !isImageRef(im); });
      var imgHtml = imgs.length ? '<div class="msg-images">' + imgs.map(function (im) { return chatImageHtml(im, !isUser); }).join("") + "</div>" : "";
      var filesHtml = files.length ? '<div class="msg-files">' + files.map(chatFileHtml).join("") + "</div>" : "";
      var textHtml = isUser ? '<div class="msg-text">' + esc(m.content || "").replace(/\n/g, "<br>") + "</div>" : '<div class="msg-text markdown-body">' + mdHtml(m.content || "") + "</div>";
      var usage = m.usage && (m.usage.input || m.usage.output) ? '<div class="msg-meta">токены: ' + (m.usage.input || 0) + " / " + (m.usage.output || 0) + "</div>" : "";
      var progressEvents = m.progress || [];
      var usageByStep = {};
      progressEvents.forEach(function (e) { if (e.type === "step_usage") usageByStep[e.step] = e; });
      var progressRows = progressEvents.filter(function (e) { return e.type === "stage"; }).map(function (e) {
        var u = usageByStep[e.step];
        var tin = u ? Number(u.input || 0).toLocaleString("ru-RU") : "";
        var tout = u ? Number(u.output || 0).toLocaleString("ru-RU") : "";
        return '<div class="job-log-line"><span class="job-log-text">• ' + esc(e.text || "") + '</span>' +
          '<span class="job-log-in">' + tin + '</span><span class="job-log-out">' + tout + '</span></div>';
      });
      var progressHtml = progressRows.length ? '<details class="msg-progress"><summary>Этапы выполнения</summary><div>' +
        '<div class="job-log-head"><span>Действие</span><span>вход</span><span>выход</span></div>' +
        progressRows.join("") + '</div></details>' : "";
      var playlist = m.playlist_text ? '<div class="playlist-card"><div class="playlist-card-head"><b>Плейлист Synx</b><button class="btn" data-copy-playlist>Скопировать</button></div><details><summary>Показать текст плейлиста</summary><pre>' + esc(m.playlist_text) + '</pre></details></div>' : "";
      wrap.innerHTML = '<div class="msg-avatar">' + (isUser ? "Я" : "AI") + '</div><div class="msg-bubble">' + textHtml + imgHtml + filesHtml + progressHtml + playlist + usage + "</div>";
      box.appendChild(wrap);
    });
    box.querySelectorAll(".msg-text code").forEach(function (c) {
      var t = (c.textContent || "").trim();
      if (/^(\/|~\/).+\.(pdf|png|jpe?g|webp|gif|mp4|mov|webm|mkv|json|md|txt)$/i.test(t)) {
        c.classList.add("local-file-link");
        c.title = "Открыть файл";
      }
    });
    box.scrollTop = box.scrollHeight;
  }
  async function loadChatSessions(preferred) {
    try {
      var d = await api("/api/chat/sessions");
      var sessions = d.sessions || [];
      if (!sessions.length) {
        var c = await api("/api/chat/sessions", { method: "POST", body: JSON.stringify({ title: "Основной чат" }) });
        sessions = [c.session];
      }
      if (preferred) chatSession = preferred;
      if (!sessions.some(function (s) { return s.id === chatSession; })) chatSession = sessions[0].id;
      var sel = $("chat-session"); sel.innerHTML = "";
      sessions.forEach(function (s) {
        var o = document.createElement("option"); o.value = s.id; o.textContent = s.title; o.selected = s.id === chatSession; sel.appendChild(o);
      });
    } catch (e) { toast("Сессии чата: " + e.message, true); }
  }
  async function loadChat() {
    try {
      var d = await api("/api/chat/messages?session_id=" + encodeURIComponent(chatSession));
      renderChatMessages(d.messages || []);
    } catch (e) { toast("Чат: " + e.message, true); }
    renderThinkingForCurrent();
  }
  function renderChatChips() {
    var box = $("chat-chips"); box.innerHTML = "";
    chatAttachments.forEach(function (im, i) {
      var d = document.createElement("div");
      if (isImageRef(im)) {
        d.className = "chat-chip";
        d.innerHTML = '<img src="' + esc(im.url) + '" alt=""><button data-chip="' + i + '">×</button>';
      } else {
        d.className = "chat-chip chat-chip-file";
        d.title = "Открыть файл: " + (im.name || "");
        d.dataset.chipOpen = im.path || "";
        d.innerHTML = '<span class="chat-chip-file-icon">▤</span>' +
          '<span class="chat-chip-file-name">' + esc(im.name || "файл") + '</span>' +
          '<button data-chip="' + i + '">×</button>';
      }
      box.appendChild(d);
    });
    $("chat-chips").querySelectorAll("[data-chip]").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        chatAttachments.splice(Number(b.dataset.chip), 1); renderChatChips();
      });
    });
    $("chat-chips").querySelectorAll("[data-chip-open]").forEach(function (el) {
      el.addEventListener("click", function () {
        if (el.dataset.chipOpen) openPath(el.dataset.chipOpen);
      });
    });
  }
  // ---------- chat splitter: resize input vs history ----------
  var chatComposerHeight = 0;
  var CHAT_COMPOSER_MIN = 115;
  var CHAT_MESSAGES_MIN = 120;

  function appRenderScale() {
    var app = $("app");
    if (!app || !app.offsetWidth) return 1;
    return app.getBoundingClientRect().width / app.offsetWidth || 1;
  }
  function clampChatComposer(h) {
    var shell = document.querySelector(".chat-shell");
    var head = document.querySelector(".chat-head");
    if (!shell) return Math.max(CHAT_COMPOSER_MIN, h);
    var scale = appRenderScale();
    var shellH = shell.getBoundingClientRect().height / scale;
    var headH = head ? head.getBoundingClientRect().height / scale : 0;
    var maxH = Math.max(CHAT_COMPOSER_MIN, shellH - headH - 7 - CHAT_MESSAGES_MIN);
    return Math.max(CHAT_COMPOSER_MIN, Math.min(maxH, h));
  }
  function applyChatComposerHeight(h) {
    var comp = $("chat-composer");
    if (!comp) return;
    if (!h || h < 40) {
      comp.style.height = "";
      comp.style.flex = "";
      chatComposerHeight = 0;
      return;
    }
    var height = clampChatComposer(h);
    comp.style.flex = "0 0 auto";
    comp.style.height = Math.round(height) + "px";
    chatComposerHeight = height;
  }
  async function saveChatComposerHeight(h) {
    try {
      await api("/api/ui/chat-panels", {
        method: "POST", body: JSON.stringify({composer_height: h || 0})
      });
    } catch (e) { /* panel size is cosmetic */ }
  }
  async function saveSidebarCollapsed(collapsed) {
    try {
      await api("/api/ui/chat-panels", {
        method: "POST", body: JSON.stringify({sidebar_collapsed: !!collapsed})
      });
    } catch (e) { /* panel state is cosmetic */ }
  }
  async function loadChatPanels() {
    try {
      var d = await api("/api/ui/chat-panels");
      if (typeof d.sidebar_collapsed === "boolean") {
        applySidebarCollapsed(d.sidebar_collapsed);
      }
      var h = Number(d.composer_height || 0);
      if (h > 0) applyChatComposerHeight(h);
    } catch (e) { /* defaults */ }
  }
  (function initChatSplitter() {
    var split = $("chat-splitter");
    var comp = $("chat-composer");
    if (!split || !comp) return;
    var drag = null;
    split.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      var scale = appRenderScale();
      drag = {y: e.clientY, h: comp.getBoundingClientRect().height / scale};
      split.classList.add("dragging");
      document.body.classList.add("chat-resizing");
      try { split.setPointerCapture(e.pointerId); } catch (err) {}
    });
    split.addEventListener("pointermove", function (e) {
      if (!drag) return;
      e.preventDefault();
      var scale = appRenderScale();
      applyChatComposerHeight(drag.h - (e.clientY - drag.y) / scale);
    });
    function finish() {
      if (!drag) return;
      drag = null;
      split.classList.remove("dragging");
      document.body.classList.remove("chat-resizing");
      saveChatComposerHeight(chatComposerHeight);
    }
    split.addEventListener("pointerup", finish);
    split.addEventListener("pointercancel", finish);
    split.addEventListener("lostpointercapture", finish);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    split.addEventListener("dblclick", function () {
      applyChatComposerHeight(0);
      saveChatComposerHeight(0);
    });
    window.addEventListener("resize", function () {
      if (chatComposerHeight > 0) applyChatComposerHeight(chatComposerHeight);
    });
  })();

  function insertIntoChatInput(text) {
    if (!text) return;
    var ta = $("chat-input");
    var val = ta.value;
    var start = ta.selectionStart != null ? ta.selectionStart : val.length;
    var end = ta.selectionEnd != null ? ta.selectionEnd : val.length;
    if (document.activeElement !== ta) { start = end = val.length; }
    var before = val.slice(0, start), after = val.slice(end);
    var sep1 = before && !/\s$/.test(before) ? "\n\n" : "";
    var sep2 = after && !/^\s/.test(after) ? "\n" : "";
    var ins = sep1 + text + sep2;
    ta.value = before + ins + after;
    var pos = (before + ins).length;
    ta.focus();
    ta.setSelectionRange(pos, pos);
    ta.dispatchEvent(new Event("input", {bubbles: true}));
    toast("Текст добавлен в запрос");
  }

  // Drag selected text into the composer; the caret shows the exact spot.
  var chatDrag = {text: "", mirror: null, offset: 0};
  function buildMirror(ta) {
    if (chatDrag.mirror) chatDrag.mirror.remove();
    var cs = getComputedStyle(ta);
    var m = document.createElement("div");
    m.style.cssText = "position:fixed;left:-9999px;top:0;visibility:hidden;pointer-events:none;z-index:-1;";
    m.style.width = Math.max(40, ta.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)) + "px";
    ["fontFamily", "fontSize", "fontWeight", "fontStyle", "letterSpacing", "lineHeight",
     "textTransform", "textAlign", "wordSpacing", "tabSize", "overflowWrap", "wordBreak"
    ].forEach(function (k) { m.style[k] = cs[k]; });
    m.style.whiteSpace = "pre-wrap";
    m.style.padding = "0";
    m.style.border = "0";
    document.body.appendChild(m);
    chatDrag.mirror = m;
    return m;
  }
  function caretFromPoint(ta, clientX, clientY) {
    var text = ta.value;
    var m = buildMirror(ta);
    var before = document.createTextNode(""), span = document.createElement("span"),
        after = document.createTextNode("");
    span.textContent = "\u200b";
    m.appendChild(before); m.appendChild(span); m.appendChild(after);
    var mrect = m.getBoundingClientRect();
    var trect = ta.getBoundingClientRect();
    var tx = clientX - trect.left;
    var ty = clientY - trect.top + ta.scrollTop;
    var best = 0, bestD = Infinity, bestRect = null;
    var step = text.length > 4000 ? Math.ceil(text.length / 4000) : 1;
    for (var i = 0; i <= text.length; i += step) {
      before.data = text.slice(0, i);
      after.data = text.slice(i);
      var r = span.getBoundingClientRect();
      var dx = r.left - mrect.left, dy = r.top - mrect.top;
      var d = Math.abs(dy - ty) * 8 + Math.abs(dx - tx);
      if (d < bestD) { bestD = d; best = i; bestRect = r; }
    }
    return {offset: best, rect: bestRect, trect: trect};
  }
  function showDropCaret(rect) {
    var comp = $("chat-composer"), marker = $("drop-caret");
    var cs = getComputedStyle($("chat-input"));
    if (!rect) { marker.classList.add("hidden"); return; }
    var cr = comp.getBoundingClientRect();
    var lh = parseFloat(cs.lineHeight) || 18;
    marker.style.left = (rect.left - cr.left) + "px";
    marker.style.top = (rect.top - cr.top) + "px";
    marker.style.height = lh + "px";
    marker.classList.remove("hidden");
  }
  function hideDropCaret() {
    $("drop-caret").classList.add("hidden");
    $("chat-composer").classList.remove("drop-active");
    if (chatDrag.mirror) { chatDrag.mirror.remove(); chatDrag.mirror = null; }
  }
  $("chat-messages").addEventListener("dragstart", function (e) {
    var sel = selectedText();
    if (!sel) return;
    chatDrag.text = sel;
    try {
      e.dataTransfer.setData("text/plain", sel);
      e.dataTransfer.effectAllowed = "copy";
    } catch (err) { /* ignore */ }
    $("chat-composer").classList.add("drop-active");
  });
  $("chat-composer").addEventListener("dragover", function (e) {
    var dt = e.dataTransfer || {};
    var hasFiles = Array.from(dt.types || []).indexOf("Files") >= 0;
    var hasText = chatDrag.text || Array.from(dt.types || []).indexOf("text/plain") >= 0;
    if (!hasFiles && !hasText) return;
    e.preventDefault();
    if (dt.dropEffect !== undefined) dt.dropEffect = "copy";
    if (hasFiles) {
      showDropCaret(null);
      $("chat-composer").classList.add("drop-active");
      return;
    }
    var ta = $("chat-input"), r = ta.getBoundingClientRect();
    var over = e.clientX >= r.left && e.clientX <= r.right &&
               e.clientY >= r.top && e.clientY <= r.bottom;
    if (over) {
      var res = caretFromPoint(ta, e.clientX, e.clientY);
      chatDrag.offset = res.offset;
      showDropCaret(res.rect);
    } else {
      chatDrag.offset = ta.value.length;
      showDropCaret(null);
    }
    $("chat-composer").classList.add("drop-active");
  });
  $("chat-composer").addEventListener("dragleave", function (e) {
    if (e.target === $("chat-composer")) hideDropCaret();
  });
  $("chat-composer").addEventListener("drop", function (e) {
    e.preventDefault();
    var files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (files.length) {
      hideDropCaret();
      uploadChatFiles(files, "Референс");
      return;
    }
    var text = chatDrag.text || (e.dataTransfer && e.dataTransfer.getData("text/plain")) || "";
    var offset = chatDrag.offset;
    hideDropCaret();
    if (!text) return;
    var ta = $("chat-input"), val = ta.value;
    if (offset == null || offset < 0 || offset > val.length) offset = val.length;
    var before = val.slice(0, offset), after = val.slice(offset);
    var sep1 = before && !/\s$/.test(before) ? "\n\n" : "";
    var sep2 = after && !/^\s/.test(after) ? "\n" : "";
    var ins = sep1 + text + sep2;
    ta.value = before + ins + after;
    var pos = (before + ins).length;
    ta.focus();
    ta.setSelectionRange(pos, pos);
    ta.dispatchEvent(new Event("input", {bubbles: true}));
    chatDrag.text = "";
  });
  document.addEventListener("dragend", function () { chatDrag.text = ""; hideDropCaret(); });

  // Clipboard images while the mouse is over the chat.
  var chatHover = false;
  $("view-chat").addEventListener("mouseenter", function () { chatHover = true; });
  $("view-chat").addEventListener("mouseleave", function () { chatHover = false; });
  async function uploadChatFile(file) {
    var fd = new FormData();
    fd.append("file", file);
    var r = await fetch("/api/chat/upload", { method: "POST", body: fd });
    var d = await r.json();
    if (!r.ok) throw new Error(d.detail || "upload failed");
    return d;
  }
  async function uploadChatFiles(files, label) {
    var added = 0;
    for (var i = 0; i < files.length; i++) {
      try { chatAttachments.push(await uploadChatFile(files[i])); added++; }
      catch (err) { toast((label || "Референс") + ": " + err.message, true); }
    }
    if (added) {
      renderChatChips();
      $("chat-input").focus();
      toast(added === 1 ? "Референс прикреплён" : "Референсов прикреплено: " + added);
    }
    return added;
  }
  document.addEventListener("paste", async function (e) {
    if (!chatHover) return;
    var items = Array.from((e.clipboardData || {}).items || []);
    var files = items.filter(function (it) { return it.type && it.type.indexOf("image/") === 0; })
      .map(function (it) { return it.getAsFile(); }).filter(Boolean);
    if (!files.length) return;
    e.preventDefault();
    await uploadChatFiles(files, "Вставка");
  });

  $("chat-files").addEventListener("change", async function () {
    var files = Array.from(this.files || []); this.value = "";
    await uploadChatFiles(files, "Референс");
  });
  function appendUserBubble(text, images) {
    var box = $("chat-messages");
    var empty = box.querySelector(".chat-empty"); if (empty) box.innerHTML = "";
    var wrap = document.createElement("div"); wrap.className = "msg user";
    var refs = images || [];
    var imgs = refs.filter(isImageRef);
    var files = refs.filter(function (im) { return !isImageRef(im); });
    var imgHtml = imgs.map(function (im) { return '<img src="' + esc(im.url) + '" data-chat-image="1" alt="">'; }).join("");
    var filesHtml = files.length ? '<div class="msg-files">' + files.map(chatFileHtml).join("") + '</div>' : "";
    wrap.innerHTML = '<div class="msg-avatar">Я</div><div class="msg-bubble"><div class="msg-text">' + esc(text || "").replace(/\n/g, "<br>") + '</div>' +
      (imgHtml ? '<div class="msg-images">' + imgHtml + '</div>' : "") + filesHtml + '</div>';
    box.appendChild(wrap); box.scrollTop = box.scrollHeight;
    return wrap;
  }
  function appendThinkingBubble() {
    var box = $("chat-messages");
    var wrap = document.createElement("div"); wrap.className = "msg assistant"; wrap.id = "chat-thinking";
    wrap.innerHTML = '<div class="msg-avatar">AI</div><div class="msg-bubble">' +
      '<div class="thinking-row"><span class="thinking-dots"><i></i><i></i><i></i></span><span id="thinking-text">Анализирую запрос...</span></div>' +
      '<div class="job-log" id="job-log"><div class="job-log-head"><span>Действие</span><span>вход</span><span>выход</span></div></div>' +
      '<div class="job-images" id="job-images"></div>' +
      '<div class="thinking-footer"><button class="btn danger hidden" id="chat-stop" title="Остановить ответ">■ Стоп</button></div>' +
      '</div>';
    var stop = wrap.querySelector("#chat-stop");
    if (stop) stop.addEventListener("click", stopChatJob);
    box.appendChild(wrap); box.scrollTop = box.scrollHeight;
    return wrap;
  }
  var chatJobs = {};
  function currentChatJob() { return chatJobs[chatSession] || null; }
  function setChatRunning(on) {
    $("chat-send").disabled = !!on;
    $("chat-send").textContent = on ? "Думаю..." : "Отправить";
    var stop = $("chat-stop");
    if (stop) stop.classList.toggle("hidden", !on);
  }
  function syncChatUi() {
    var job = currentChatJob();
    setChatRunning(!!job && job.status !== "done");
  }
  function fmtJobTokens(n) {
    return Number(n || 0).toLocaleString("ru-RU");
  }
  function applyJobEvent(evt, state) {
    var th = $("thinking-text"); if (th && evt.text) th.textContent = evt.text;
    if (evt.type === "stage") {
      var log = $("job-log");
      if (log) {
        var line = document.createElement("div");
        line.className = "job-log-line";
        var txt = document.createElement("span");
        txt.className = "job-log-text";
        txt.textContent = "• " + (evt.text || "");
        var tin = document.createElement("span");
        tin.className = "job-log-in";
        var tout = document.createElement("span");
        tout.className = "job-log-out";
        line.appendChild(txt); line.appendChild(tin); line.appendChild(tout);
        log.appendChild(line);
        if (evt.step) state.stepLines[evt.step] = {in: tin, out: tout};
      }
    }
    if (evt.type === "step_usage") {
      var tk = state.stepLines[evt.step];
      if (!tk) {
        var log2 = $("job-log");
        if (log2) {
          var l2 = document.createElement("div"); l2.className = "job-log-line";
          var t2 = document.createElement("span"); t2.className = "job-log-text";
          t2.textContent = "• Шаг " + evt.step;
          tk = {in: document.createElement("span"), out: document.createElement("span")};
          tk.in.className = "job-log-in"; tk.out.className = "job-log-out";
          l2.appendChild(t2); l2.appendChild(tk.in); l2.appendChild(tk.out);
          log2.appendChild(l2);
          state.stepLines[evt.step] = tk;
        }
      }
      if (tk) {
        tk.in.textContent = fmtJobTokens(evt.input);
        tk.out.textContent = fmtJobTokens(evt.output);
      }
    }
    if (evt.type === "image" && evt.image) {
      var jb = $("job-images");
      if (jb) {
        var im = document.createElement("img");
        im.src = evt.image.url; im.dataset.chatImage = "1";
        im.dataset.title = evt.image.name || "image"; im.alt = "";
        jb.appendChild(im);
      }
    }
  }
  function renderJobEvents(job) {
    var log = $("job-log");
    if (!log) return;
    log.innerHTML = '<div class="job-log-head"><span>Действие</span><span>вход</span><span>выход</span></div>';
    var jb = $("job-images"); if (jb) jb.innerHTML = "";
    var state = {stepLines: {}};
    (job.events || []).forEach(function (evt) { applyJobEvent(evt, state); });
    var box = $("chat-messages"); if (box) box.scrollTop = box.scrollHeight;
  }
  function renderThinkingForCurrent() {
    var old = $("chat-thinking"); if (old) old.remove();
    var job = currentChatJob();
    if (!job || job.status === "done") { setChatRunning(false); return; }
    appendThinkingBubble();
    renderJobEvents(job);
    setChatRunning(true);
  }
  async function stopChatJob() {
    var job = currentChatJob();
    if (!job || !job.id) return;
    var th = $("thinking-text"); if (th) th.textContent = "Прерываю...";
    try { await api("/api/chat/jobs/" + encodeURIComponent(job.id) + "/stop", { method: "POST" }); }
    catch (e) { toast("Стоп: " + e.message, true); }
  }
  async function pollChatJob(jobId, sid) {
    var job = chatJobs[sid];
    if (!job) return;
    for (;;) {
      var d;
      try { d = await api("/api/chat/jobs/" + encodeURIComponent(jobId)); }
      catch (e) {
        if (chatSession === sid) toast("Чат: " + e.message, true);
        job.status = "error";
        break;
      }
      var changed = false;
      (d.events || []).forEach(function (evt) {
        if ((evt.seq || 0) <= job.seen) return;
        job.seen = evt.seq || job.seen;
        job.events.push(evt);
        changed = true;
      });
      if (changed && chatSession === sid) {
        if (!$("chat-thinking")) renderThinkingForCurrent();
        else renderJobEvents(job);
      }
      if (d.status === "done") { job.status = "done"; break; }
      if (d.status === "cancelled") { job.status = "cancelled"; break; }
      if (d.status === "error") { job.status = "error"; if (chatSession === sid) toast("Ошибка чата: " + (d.error || ""), true); break; }
      await new Promise(function (r) { setTimeout(r, 800); });
    }
    var wasCurrent = chatSession === sid;
    if (wasCurrent) {
      delete chatJobs[sid];
      var th = $("chat-thinking"); if (th) th.remove();
      setChatRunning(false);
      await loadChatSessions(chatSession);
      await loadChat();
    } else {
      delete chatJobs[sid];
    }
  }
  async function sendChat() {
    var sid = chatSession;
    if (chatJobs[sid]) return;
    var text = $("chat-input").value.trim();
    if (!text && !chatAttachments.length) return;
    var images = chatAttachments.slice();
    var payload = { session_id: sid, text: text, images: images };
    chatAttachments = []; renderChatChips(); $("chat-input").value = "";
    appendUserBubble(text, images);
    var job = {id: null, events: [], seen: 0, status: "starting"};
    chatJobs[sid] = job;
    appendThinkingBubble();
    setChatRunning(true);
    try {
      var d = await api("/api/chat/send", { method: "POST", body: JSON.stringify(payload) });
      job.id = d.job_id;
      job.status = "running";
      await pollChatJob(d.job_id, sid);
    } catch (e) {
      delete chatJobs[sid];
      if (chatSession === sid) {
        var th = $("chat-thinking"); if (th) th.remove();
        toast("Чат: " + e.message, true);
        await loadChat();
        setChatRunning(false);
      }
    }
  }
  // ---------- local voice input (hold to talk) ----------
  var voiceState = {enabled: false, recording: false, thinking: false,
                    polling: null, before: "", after: "", dotTimer: null, dotIndex: 0};
  function voiceUi() {
    var on = voiceState.enabled;
    $("voice-enabled").checked = on;
    $("voice-btn").classList.toggle("hidden", !on);
  }
  async function loadVoiceStatus() {
    try {
      var d = await api("/api/voice/status");
      voiceState.enabled = !!d.enabled;
      voiceUi();
      if (d.status === "error" && d.error) console.warn("voice:", d.error);
    } catch (e) { /* voice is optional */ }
  }
  $("voice-enabled").addEventListener("change", async function () {
    try {
      var d = await api("/api/voice/toggle", {method: "POST",
        body: JSON.stringify({enabled: this.checked})});
      voiceState.enabled = !!d.enabled;
      voiceUi();
      toast(d.enabled ? "Голосовой ввод включён" : "Голосовой ввод выключен, модели выгружены");
    } catch (e) { toast("Голос: " + e.message, true); this.checked = !this.checked; }
  });
  function voiceStopDots() {
    if (voiceState.dotTimer) { clearInterval(voiceState.dotTimer); voiceState.dotTimer = null; }
  }
  function voiceStartDots() {
    voiceStopDots();
    var seq = [1, 2, 3, 4, 5, 4, 3, 2];
    voiceState.dotIndex = 0;
    $("voice-btn").textContent = "Думаю.";
    voiceState.dotTimer = setInterval(function () {
      voiceState.dotIndex = (voiceState.dotIndex + 1) % seq.length;
      $("voice-btn").textContent = "Думаю" + ".".repeat(seq[voiceState.dotIndex]);
    }, 100);
  }
  function voiceRestoreButton() {
    voiceStopDots();
    voiceState.thinking = false;
    $("voice-btn").classList.remove("recording", "thinking");
    $("voice-btn").textContent = "Нажмите";
    $("chat-input").readOnly = false;
  }
  async function voiceStart() {
    if (!voiceState.enabled || voiceState.recording || voiceState.thinking) return;
    var ta = $("chat-input");
    var start = ta.selectionStart != null ? ta.selectionStart : ta.value.length;
    var end = ta.selectionEnd != null ? ta.selectionEnd : ta.value.length;
    voiceState.before = ta.value.slice(0, start);
    voiceState.after = ta.value.slice(end);
    voiceState.recording = true;
    ta.readOnly = true;
    $("voice-btn").classList.remove("thinking");
    $("voice-btn").classList.add("recording");
    $("voice-btn").textContent = "Говорите";
    try {
      var d = await api("/api/voice/start", {method: "POST"});
      if (d.error) throw new Error(d.error);
      voiceState.polling = setInterval(async function () {
        try {
          var p = await api("/api/voice/partial");
          if (!voiceState.recording) return;
          if (p.partial) {
            $("chat-input").value = voiceState.before + p.partial + voiceState.after;
          }
        } catch (e) { /* keep partial */ }
      }, 250);
    } catch (e) {
      voiceState.recording = false;
      voiceRestoreButton();
      toast("Голос: " + e.message, true);
    }
  }
  async function voiceStop() {
    if (!voiceState.recording) return;
    var stopStarted = Date.now();
    voiceState.recording = false;
    voiceState.thinking = true;
    if (voiceState.polling) { clearInterval(voiceState.polling); voiceState.polling = null; }
    $("voice-btn").classList.remove("recording");
    $("voice-btn").classList.add("thinking");
    voiceStartDots();
    var ta = $("chat-input");
    try {
      var d = await api("/api/voice/stop", {method: "POST"});
      var text = (d.final || d.partial || "").trim();
      ta.value = voiceState.before + text + voiceState.after;
      var pos = (voiceState.before + text).length;
      ta.focus();
      ta.setSelectionRange(pos, pos);
      if (d.error) toast("Голос: " + d.error, true);
      else if (text) toast("Голос распознан" +
        (d.correct_seconds ? " · коррекция " + d.correct_seconds + " с" : ""));
    } catch (e) {
      toast("Голос: " + e.message, true);
    } finally {
      var remain = 550 - (Date.now() - stopStarted);
      if (remain > 0) await new Promise(function (r) { setTimeout(r, remain); });
      voiceRestoreButton();
    }
  }
  $("voice-btn").addEventListener("pointerdown", function (e) {
    e.preventDefault();
    voiceStart();
  });
  window.addEventListener("pointerup", function () { voiceStop(); });
  window.addEventListener("pointercancel", function () { voiceStop(); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && voiceState.recording) { e.preventDefault(); voiceStop(); }
  });
  loadVoiceStatus();

  $("chat-send").addEventListener("click", sendChat);
  $("chat-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  $("chat-clear").addEventListener("click", async function () {
    if (!confirm("Очистить историю чата?")) return;
    try { await api("/api/chat/clear?session_id=" + encodeURIComponent(chatSession), { method: "POST" }); chatAttachments = []; renderChatChips(); await loadChat(); }
    catch (e) { toast(e.message, true); }
  });
  var folderRoots = [];
  async function openFoldersModal() {
    try { folderRoots = (await api("/api/filesystem/roots")).roots || []; }
    catch (e) { toast(e.message, true); return; }
    var rows = folderRoots.map(function (p) {
      return '<div class="folder-row"><span class="mono">' + esc(p) + '</span><button class="btn danger" data-folder-remove="' + esc(p) + '">Убрать</button></div>';
    }).join("") || '<div class="muted">Пока нет рабочих папок.</div>';
    var body = '<div class="folder-list">' + rows + '</div>' +
      '<div class="folder-add"><input class="input grow" id="folder-path" placeholder="/mnt/projects/..."> ' +
      '<button class="btn" id="folder-pick">Выбрать...</button> <button class="btn primary" id="folder-add-btn">Добавить</button></div>' +
      '<div class="muted" style="margin-top:8px">Внутри этих папок робот имеет полный доступ: читать, создавать, изменять и удалять файлы.</div>';
    openModal("Рабочие папки робота", body, []);
    setTimeout(function () {
      var add = $("folder-add-btn"), pick = $("folder-pick"), input = $("folder-path");
      async function addPath(path) {
        if (!path) return;
        try { var d = await api("/api/filesystem/roots", { method: "POST", body: JSON.stringify({ path: path }) }); folderRoots = d.roots; toast("Папка добавлена"); closeModal(); openFoldersModal(); }
        catch (e) { toast(e.message, true); }
      }
      add && add.addEventListener("click", function () { addPath(input.value.trim()); });
      pick && pick.addEventListener("click", async function () {
        if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
          var r = await window.pywebview.api.pick_folder();
          if (r && r.ok) await addPath(r.path);
        } else {
          var p = prompt("Путь к папке:"); if (p) await addPath(p);
        }
      });
      document.querySelectorAll("[data-folder-remove]").forEach(function (b) {
        b.addEventListener("click", async function () {
          try { var d = await api("/api/filesystem/roots?path=" + encodeURIComponent(b.dataset.folderRemove), { method: "DELETE" }); folderRoots = d.roots; toast("Папка убрана"); closeModal(); openFoldersModal(); }
          catch (e) { toast(e.message, true); }
        });
      });
    }, 0);
  }
  $("chat-folders").addEventListener("click", openFoldersModal);
  $("chat-session").addEventListener("change", function () {
    chatSession = this.value;
    syncChatUi();
    loadChat();
  });
  $("chat-new").addEventListener("click", async function () {
    var title = "Чат " + new Date().toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    try {
      var d = await api("/api/chat/sessions", { method: "POST", body: JSON.stringify({ title: title }) });
      await loadChatSessions(d.session.id);
      await loadChat();
    } catch (e) { toast(e.message, true); }
  });
  function copyText(text) {
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast("Скопировано в буфер обмена"); }, function () { fallbackCopy(text); });
    } else fallbackCopy(text);
  }
  function selectedText() {
    var sel = window.getSelection();
    return sel ? String(sel.toString() || "").trim() : "";
  }
  // Copy works with any keyboard layout: use e.code (physical key), not e.key.
  document.addEventListener("keydown", function (e) {
    var mod = e.ctrlKey || e.metaKey;
    if (!mod || e.altKey) return;
    var isCopy = (e.code === "KeyC" && !e.shiftKey) ||
                 (e.code === "Insert" && !e.shiftKey);
    if (!isCopy) return;
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) {
      if (t.type === "password") return;
      var start = t.selectionStart, end = t.selectionEnd;
      if (start != null && end != null && end > start) {
        e.preventDefault();
        copyText(t.value.slice(start, end));
      }
      return;
    }
    if (t && t.isContentEditable) return;
    var sel = selectedText();
    if (!sel) return;
    e.preventDefault();
    copyText(sel);
  });
  function fallbackCopy(text) {
    var sel = window.getSelection();
    var saved = [];
    if (sel) {
      for (var i = 0; i < sel.rangeCount; i++) saved.push(sel.getRangeAt(i).cloneRange());
    }
    var ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.left = "-9999px";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast("Скопировано"); } catch (e) { toast("Не удалось скопировать", true); }
    document.body.removeChild(ta);
    if (sel && saved.length) {
      sel.removeAllRanges();
      saved.forEach(function (r) { sel.addRange(r); });
    }
  }
  $("chat-messages").addEventListener("click", function (e) {
    var cp = e.target.closest("[data-copy-playlist]");
    if (cp) {
      var pre = cp.closest(".playlist-card").querySelector("pre");
      copyText(pre ? pre.textContent : "");
      return;
    }
    var f = e.target.closest("[data-chat-file-path]");
    if (f) { openPath(f.dataset.chatFilePath); return; }
    var im = e.target.closest("[data-chat-image]");
    if (im) openViewer(im.src, im.dataset.title || "image");
  });
  $("chat-messages").addEventListener("contextmenu", function (e) {
    var msg = e.target.closest(".msg");
    if (!msg) return;
    var txt = msg.querySelector(".msg-text");
    if (!txt) return;
    e.preventDefault();
    var text = (txt.innerText || txt.textContent || "").trim();
    var isUser = msg.classList.contains("user");
    var items = [];
    var selected = selectedText();
    if (selected) {
      items.push({label: "Вставить в запрос", fn: function () { insertIntoChatInput(selected); }});
      items.push({label: "Вставить в запрос как цитату", fn: function () {
        insertIntoChatInput(selected.split("\n").map(function (line) { return "> " + line; }).join("\n"));
      }});
      items.push({sep: true});
      items.push({label: "Копировать выделенное", fn: function () { copyText(selected); }});
      items.push({label: "Копировать выделенное как цитату", fn: function () {
        copyText(selected.split("\n").map(function (line) { return "> " + line; }).join("\n"));
      }});
      items.push({sep: true});
    }
    var link = e.target.closest("a[href]");
    if (link) {
      var href = link.getAttribute("href") || "";
      items.push({label: "Копировать ссылку", fn: function () { copyText(href); }});
      items.push({label: "Открыть ссылку", fn: function () { openHref(href); }});
      items.push({sep: true});
    }
    var codeEl = e.target.closest("code, .mono");
    if (codeEl) {
      var codeText = (codeEl.textContent || "").trim();
      if (/^(\/|~\/).+\.pdf$/i.test(codeText)) {
        items.push({label: "Открыть PDF в роботе", fn: function () { openPdfViewer(codeText, "PDF"); }});
        items.push({label: "Открыть во внешнем просмотрщике", fn: function () { openPath(codeText); }});
        items.push({sep: true});
      }
    }
    items.push({label: "Копировать сообщение (.md)", fn: function () { copyText(messageMarkdown(msg.__msg || {})); }});
    if (text) {
      items.push({label: isUser ? "Копировать запрос" : "Копировать ответ", fn: function () { copyText(text); }});
      items.push({label: "Копировать как цитату", fn: function () {
        copyText(text.split("\n").map(function (line) { return "> " + line; }).join("\n"));
      }});
    }
    items.push({sep: true});
    items.push({label: "Экспортировать чат в .md", fn: exportChat});
    openContextMenu(e.clientX, e.clientY, items);
  });

  // ---------- MCP / skills ----------
  var mcpServers = [];
  async function openMCPModal() {
    try { mcpServers = (await api("/api/mcp/servers")).servers || []; }
    catch (e) { toast(e.message, true); return; }
    var list = mcpServers.map(function (s, i) {
      return '<div class="mcp-row">' +
        '<div class="mcp-row-head"><b>' + esc(s.name || "MCP") + '</b>' +
        '<label class="check inline-check"><input type="checkbox" data-mcp-enabled="' + i + '" ' + (s.enabled ? "checked" : "") + '> активен</label></div>' +
        '<div class="mcp-fields">' +
        '<input class="input mono" data-mcp-command="' + i + '" placeholder="команда запуска" value="' + esc(s.command || "") + '">' +
        '<input class="input mono" data-mcp-args="' + i + '" placeholder="аргументы через пробел" value="' + esc((s.args || []).join(" ")) + '">' +
        '<span><button class="btn" data-mcp-save="' + i + '">Сохранить</button> <button class="btn danger" data-mcp-remove="' + i + '">×</button></span>' +
        '</div>' +
        '<small class="muted mono">' + esc(s.path || s.source_url || "") + '</small></div>';
    }).join("") || '<div class="muted">Нет MCP-серверов. Найди и установи ниже.</div>';
    var body = '<h3 style="margin:0 0 8px">Активные навыки / MCP</h3>' +
      '<div class="mcp-list">' + list + '</div>' +
      '<div class="mcp-search"><input class="input grow" id="mcp-q" placeholder="Поиск MCP-серверов на GitHub...">' +
      '<button class="btn" id="mcp-search-btn">Искать</button>' +
      '<button class="btn" id="mcp-add-manual">Добавить вручную</button></div>' +
      '<div id="mcp-results"></div>' +
      '<div class="muted" style="margin-top:8px">Установка клонирует репозиторий в data/mcp. Команду запуска и аргументы нужно проверить и включить вручную.</div>';
    openModal("MCP / навыки", body, []);
    setTimeout(function () {
      document.querySelectorAll("[data-mcp-save]").forEach(function (b) {
        b.addEventListener("click", async function () {
          var i = Number(b.dataset.mcpSave); var srv = mcpServers[i];
          var cmd = document.querySelector('[data-mcp-command="' + i + '"]').value.trim();
          var args = document.querySelector('[data-mcp-args="' + i + '"]').value.trim().split(/\s+/).filter(Boolean);
          var enabled = document.querySelector('[data-mcp-enabled="' + i + '"]').checked;
          try { await api("/api/mcp/servers", { method: "POST", body: JSON.stringify({ server: Object.assign({}, srv, { command: cmd, args: args, enabled: enabled }) }) }); toast("MCP сохранён"); openMCPModal(); }
          catch (e) { toast(e.message, true); }
        });
      });
      document.querySelectorAll("[data-mcp-remove]").forEach(function (b) {
        b.addEventListener("click", async function () {
          var i = Number(b.dataset.mcpRemove); if (!confirm("Убрать " + (mcpServers[i].name || "MCP") + "?")) return;
          try { await api("/api/mcp/servers/" + encodeURIComponent(mcpServers[i].id), { method: "DELETE" }); toast("Убрано"); openMCPModal(); }
          catch (e) { toast(e.message, true); }
        });
      });
      document.querySelectorAll("[data-mcp-enabled]").forEach(function (cb) {
        cb.addEventListener("change", function () {
          var i = Number(cb.dataset.mcpEnabled); mcpServers[i].enabled = cb.checked;
        });
      });
      $("mcp-search-btn").addEventListener("click", async function () {
        var q = $("mcp-q").value.trim(); if (!q) return;
        var box = $("mcp-results"); box.innerHTML = '<div class="muted">Ищу...</div>';
        try {
          var d = await api("/api/mcp/search", { method: "POST", body: JSON.stringify({ query: q, limit: 12 }) });
          box.innerHTML = (d.results || []).map(function (r, i) {
            return '<div class="mcp-result"><div><b>' + esc(r.full_name) + '</b> <small>★ ' + (r.stars || 0) + '</small><br><small>' + esc(r.description || "") + '</small></div><button class="btn" data-mcp-install="' + i + '">Установить</button></div>';
          }).join("") || '<div class="muted">Ничего не найдено.</div>';
          box.querySelectorAll("[data-mcp-install]").forEach(function (b) {
            b.addEventListener("click", async function () {
              var r = d.results[Number(b.dataset.mcpInstall)]; b.disabled = true; b.textContent = "Клонирую...";
              try { await api("/api/mcp/install", { method: "POST", body: JSON.stringify({ url: r.clone_url, name: r.name }) }); toast("Установлено"); openMCPModal(); }
              catch (e) { b.disabled = false; b.textContent = "Установить"; toast(e.message, true); }
            });
          });
        } catch (e) { box.innerHTML = '<div class="muted">' + esc(e.message) + '</div>'; }
      });
      $("mcp-add-manual").addEventListener("click", async function () {
        var name = prompt("Название MCP/навыка:"); if (!name) return;
        var command = prompt("Команда запуска (например python или npx):"); if (!command) return;
        var args = prompt("Аргументы через пробел:", "") || "";
        try { await api("/api/mcp/servers", { method: "POST", body: JSON.stringify({ server: { name: name, command: command, args: args.split(/\s+/).filter(Boolean), enabled: false, transport: "stdio" } }) }); toast("Добавлено"); openMCPModal(); }
        catch (e) { toast(e.message, true); }
      });
    }, 0);
  }
  $("mcp-btn").addEventListener("click", openMCPModal);

  // ---------- image viewer ----------
  var viewer = { scale: 1, x: 0, y: 0, src: "", filename: "", down: false, moved: false, sx: 0, sy: 0, px: 0, py: 0 };
  function viewerApply() {
    var img = $("viewer-img");
    img.style.transform = "translate(" + viewer.x + "px," + viewer.y + "px) scale(" + viewer.scale + ")";
  }
  function viewerFit() {
    var img = $("viewer-img");
    var stage = $("viewer-stage");
    if (!img.naturalWidth) return;
    var sw = stage.clientWidth, sh = stage.clientHeight;
    var s = Math.min(sw / img.naturalWidth, sh / img.naturalHeight, 1);
    viewer.scale = s;
    viewer.x = (sw - img.naturalWidth * s) / 2;
    viewer.y = (sh - img.naturalHeight * s) / 2;
    viewerApply();
  }
  function openViewer(src, title) {
    viewer.src = src;
    viewer.filename = (title || "image").replace(/[^\w.\-]+/g, "_");
    if (!/\.[a-z0-9]+$/i.test(viewer.filename)) viewer.filename += ".png";
    $("viewer-title").textContent = title || "Просмотр";
    var img = $("viewer-img");
    img.onload = viewerFit;
    img.src = src;
    $("image-viewer").classList.remove("hidden");
    if (img.complete) viewerFit();
  }
  function closeViewer() {
    $("image-viewer").classList.add("hidden");
    $("image-ctx").classList.remove("open");
    $("viewer-img").src = "";
  }
  $("viewer-close").addEventListener("click", closeViewer);
  $("image-viewer").addEventListener("click", function (e) { if (e.target === $("image-viewer")) closeViewer(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closeViewer(); closeModal(); } });
  $("viewer-stage").addEventListener("wheel", function (e) {
    e.preventDefault();
    var stage = $("viewer-stage"), rect = stage.getBoundingClientRect();
    var mx = e.clientX - rect.left, my = e.clientY - rect.top;
    var factor = e.deltaY < 0 ? 1.14 : 1 / 1.14;
    var ns = Math.max(0.05, Math.min(12, viewer.scale * factor));
    viewer.x = mx - (mx - viewer.x) * (ns / viewer.scale);
    viewer.y = my - (my - viewer.y) * (ns / viewer.scale);
    viewer.scale = ns; viewerApply();
  }, { passive: false });
  $("viewer-stage").addEventListener("mousedown", function (e) {
    if (e.button !== 1) return;
    e.preventDefault();
    viewer.down = true; viewer.moved = false;
    viewer.sx = e.clientX; viewer.sy = e.clientY; viewer.px = viewer.x; viewer.py = viewer.y;
    $("viewer-stage").classList.add("dragging");
  });
  window.addEventListener("mousemove", function (e) {
    if (!viewer.down) return;
    var dx = e.clientX - viewer.sx, dy = e.clientY - viewer.sy;
    if (Math.abs(dx) + Math.abs(dy) > 4) viewer.moved = true;
    viewer.x = viewer.px + dx; viewer.y = viewer.py + dy; viewerApply();
  });
  window.addEventListener("mouseup", function (e) {
    if (e.button !== 1 || !viewer.down) return;
    viewer.down = false;
    $("viewer-stage").classList.remove("dragging");
    if (!viewer.moved) viewerFit();
  });
  $("viewer-stage").addEventListener("contextmenu", function (e) {
    e.preventDefault();
    if (e.target.id !== "viewer-img") return;
    var m = $("image-ctx");
    m.style.left = Math.min(e.clientX, window.innerWidth - 240) + "px";
    m.style.top = Math.min(e.clientY, window.innerHeight - 110) + "px";
    m.classList.add("open");
    setTimeout(function () { document.addEventListener("mousedown", function close(ev) { if (!m.contains(ev.target)) m.classList.remove("open"); }, { once: true }); }, 0);
  });
  function imageDataURL(src) {
    return fetch(src).then(function (r) { return r.blob(); }).then(function (b) {
      return new Promise(function (resolve, reject) {
        var fr = new FileReader();
        fr.onload = function () { resolve(fr.result); };
        fr.onerror = reject;
        fr.readAsDataURL(b);
      });
    });
  }
  async function saveImageAs() {
    if (!viewer.src) return;
    try {
      var data = await imageDataURL(viewer.src);
      if (window.pywebview && window.pywebview.api && window.pywebview.api.save_chat_image) {
        var res = await window.pywebview.api.save_chat_image(data, viewer.filename);
        if (res && res.ok) toast("Сохранено: " + res.path);
        else if (res && !res.cancelled) toast("Не сохранено: " + (res.error || ""), true);
      } else {
        var a = document.createElement("a"); a.href = data; a.download = viewer.filename; a.click();
      }
    } catch (e) { toast("Сохранить: " + e.message, true); }
  }
  async function copyImage() {
    if (!viewer.src) return;
    try {
      var data = await imageDataURL(viewer.src);
      if (window.pywebview && window.pywebview.api && window.pywebview.api.copy_chat_image) {
        var res = await window.pywebview.api.copy_chat_image(data);
        if (res && res.ok) { toast("Изображение скопировано"); return; }
      }
      var blob = await (await fetch(viewer.src)).blob();
      if (navigator.clipboard && window.ClipboardItem) {
        await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
        toast("Изображение скопировано");
      } else {
        await navigator.clipboard.writeText(viewer.src);
        toast("Скопирована ссылка на изображение");
      }
    } catch (e) { toast("Копировать: " + e.message, true); }
  }
  $("img-save").addEventListener("click", function () { $("image-ctx").classList.remove("open"); saveImageAs(); });
  $("img-copy").addEventListener("click", function () { $("image-ctx").classList.remove("open"); copyImage(); });

  // ---------- PDF viewer (parallel page render cache) ----------
  var pdfView = {jobId: null, total: 0, current: 1, pages: [], status: "",
                 poll: null, scale: 1, x: 0, y: 0, down: false, sx: 0, sy: 0, px: 0, py: 0, moved: false};
  function pdfApply() {
    var im = $("pdf-viewer-img");
    im.style.transform = "translate(" + pdfView.x + "px," + pdfView.y + "px) scale(" + pdfView.scale + ")";
  }
  function pdfReset() {
    pdfView.scale = 1; pdfView.x = 0; pdfView.y = 0; pdfApply();
  }
  function pdfUpdate() {
    $("pdf-viewer-page").textContent = pdfView.current + " / " + (pdfView.total || 0);
    var p = $("pdf-viewer-progress");
    if (pdfView.status === "ready") p.textContent = "готово: " + pdfView.total + " страниц";
    else if (pdfView.status === "error") p.textContent = "ошибка рендера";
    else p.textContent = "рендер страниц: " + pdfView.pages.length + " / " + pdfView.total;
  }
  function pdfLoadCurrent() {
    var im = $("pdf-viewer-img");
    if (!pdfView.jobId) return;
    if (pdfView.status !== "ready" && pdfView.pages.indexOf(pdfView.current) === -1) {
      pdfUpdate(); return;
    }
    if (im.dataset.page === String(pdfView.current)) return;
    im.dataset.page = String(pdfView.current);
    im.src = "/api/pdf/page/" + encodeURIComponent(pdfView.jobId) + "/" + pdfView.current;
    pdfReset();
  }
  function pdfSetPage(n) {
    n = Math.max(1, Math.min(pdfView.total || 1, n));
    pdfView.current = n;
    var im = $("pdf-viewer-img");
    if (pdfView.status === "ready" || pdfView.pages.indexOf(n) !== -1) {
      im.dataset.page = "";
      pdfLoadCurrent();
    } else {
      im.removeAttribute("src"); im.dataset.page = "";
    }
    pdfUpdate();
  }
  function pdfStopPoll() {
    if (pdfView.poll) { clearInterval(pdfView.poll); pdfView.poll = null; }
  }
  function pdfStartPoll() {
    pdfStopPoll();
    pdfView.poll = setInterval(async function () {
      if (!pdfView.jobId) return;
      try {
        var d = await api("/api/pdf/render/" + encodeURIComponent(pdfView.jobId));
        pdfView.status = d.status; pdfView.total = d.total; pdfView.pages = d.pages || [];
        pdfUpdate(); pdfLoadCurrent();
        if (d.status === "ready" || d.status === "error") pdfStopPoll();
      } catch (e) { /* retry */ }
    }, 800);
  }
  async function openPdfViewer(path, title) {
    if (!path) return;
    pdfStopPoll();
    pdfView = {jobId: null, total: 0, current: 1, pages: [], status: "running",
               poll: null, scale: 1, x: 0, y: 0, down: false, sx: 0, sy: 0, px: 0, py: 0, moved: false};
    $("pdf-viewer-title").textContent = title || "PDF";
    $("pdf-viewer-img").removeAttribute("src");
    $("pdf-viewer-img").dataset.page = "";
    $("pdf-viewer-progress").textContent = "запускаю рендер...";
    $("pdf-viewer").classList.remove("hidden");
    pdfUpdate();
    try {
      var d = await api("/api/pdf/render", {method: "POST",
        body: JSON.stringify({path: path, dpi: 200, workers: 4})});
      pdfView.jobId = d.id; pdfView.total = d.total; pdfView.pages = d.pages || [];
      pdfView.status = d.status || "running";
      pdfSetPage(1); pdfUpdate(); pdfStartPoll();
    } catch (e) {
      $("pdf-viewer-progress").textContent = "ошибка: " + e.message;
    }
  }
  function closePdfViewer() {
    pdfStopPoll();
    $("pdf-viewer").classList.add("hidden");
    $("pdf-viewer-img").removeAttribute("src");
    pdfView.jobId = null;
  }
  $("pdf-viewer-close").addEventListener("click", closePdfViewer);
  $("pdf-viewer-prev").addEventListener("click", function () { pdfSetPage(pdfView.current - 1); });
  $("pdf-viewer-next").addEventListener("click", function () { pdfSetPage(pdfView.current + 1); });
  $("pdf-viewer-img").addEventListener("error", function () {
    if (pdfView.status !== "ready") $("pdf-viewer-progress").textContent = "страница рендерится...";
  });
  $("pdf-viewer-stage").addEventListener("wheel", function (e) {
    e.preventDefault();
    var factor = e.deltaY < 0 ? 1.14 : 1 / 1.14;
    pdfView.scale = Math.max(0.4, Math.min(8, pdfView.scale * factor));
    pdfApply();
  }, {passive: false});
  $("pdf-viewer-stage").addEventListener("mousedown", function (e) {
    if (e.button !== 1) return;
    e.preventDefault(); pdfView.down = true; pdfView.moved = false;
    pdfView.sx = e.clientX; pdfView.sy = e.clientY; pdfView.px = pdfView.x; pdfView.py = pdfView.y;
    $("pdf-viewer-stage").classList.add("dragging");
  });
  window.addEventListener("mousemove", function (e) {
    if (!pdfView.down) return;
    var dx = e.clientX - pdfView.sx, dy = e.clientY - pdfView.sy;
    if (Math.abs(dx) + Math.abs(dy) > 4) pdfView.moved = true;
    pdfView.x = pdfView.px + dx; pdfView.y = pdfView.py + dy; pdfApply();
  });
  window.addEventListener("mouseup", function (e) {
    if (e.button !== 1 || !pdfView.down) return;
    pdfView.down = false; $("pdf-viewer-stage").classList.remove("dragging");
    if (!pdfView.moved) pdfReset();
  });
  document.addEventListener("keydown", function (e) {
    if ($("pdf-viewer").classList.contains("hidden")) return;
    if (e.key === "Escape") closePdfViewer();
    else if (e.key === "ArrowLeft") pdfSetPage(pdfView.current - 1);
    else if (e.key === "ArrowRight") pdfSetPage(pdfView.current + 1);
  });

  // ---------- status bar: active models ----------
  function updateModelStatus() {
    var c = state.config || {};
    var ai = c.ai || {};
    var voice = c.voice || {};
    var corr = voice.correct || {};
    var mainEl = $("main-model-status");
    var mainDot = $("main-model-dot");
    if (mainEl && mainDot) {
      var mainModel = String(ai.model || "").trim();
      mainEl.textContent = "модель: " + (mainModel || "не задана");
      mainEl.title = "Основная модель: " + (mainModel || "не задана") +
        (ai.base_url ? " · " + ai.base_url : "");
      mainDot.className = "dot " + (mainModel ? "on" : "err");
    }
    var corrEl = $("correct-model-status");
    var corrDot = $("correct-dot");
    if (corrEl && corrDot) {
      var enabled = voice.llm_correct !== false;
      var provider = String(corr.provider || "ollama").toLowerCase();
      var label;
      if (!enabled || provider === "none" || provider === "off") label = "выключен";
      else if (provider === "ai") label = "основная модель · " + ((c.ai || {}).model || "AI");
      else label = String(corr.model || "ollama");
      corrEl.textContent = "корректор: " + label;
      corrEl.title = "Коррекция речи: " + label +
        (corr.base_url && provider === "ollama" ? " · " + corr.base_url : "");
      corrDot.className = "dot " + (enabled && provider !== "none" && provider !== "off" ? "on" : "");
      updateCorrectorStatus();
    }
  }

  async function updateCorrectorStatus() {
    var corrEl = $("correct-model-status");
    var corrDot = $("correct-dot");
    if (!corrEl || !corrDot) return;
    var c = state.config || {};
    var voice = c.voice || {};
    var corr = voice.correct || {};
    var enabled = voice.llm_correct !== false;
    var provider = String(corr.provider || "ollama").toLowerCase();
    var label;
    if (!enabled || provider === "none" || provider === "off") {
      corrEl.textContent = "корректор: выключен";
      corrEl.title = "Коррекция распознанной речи выключена";
      corrDot.className = "dot";
      return;
    }
    if (provider === "ai") {
      var mainModel = String((c.ai || {}).model || "AI");
      corrEl.textContent = "корректор: основная модель · " + mainModel;
      corrEl.title = "Коррекция речи выполняется основной AI-моделью: " + mainModel;
      corrDot.className = "dot on";
      return;
    }
    var name = String(corr.model || "");
    try {
      var d = await api("/api/voice/models");
      var models = d.models || [];
      var m = models.find(function (x) { return x.name === name; });
      if (!m) {
        corrEl.textContent = "корректор: " + (name || "ollama") + " · нет в Ollama";
        corrEl.title = "Модель " + (name || "ollama") + " не найдена в Ollama";
        corrDot.className = "dot err";
        return;
      }
      var device, vram;
      if (!m.loaded) {
        device = "не загружена";
        vram = "";
      } else if (Number(m.vram_mb) > 0) {
        device = "GPU";
        vram = " · " + fmtModelSize(m.vram_mb) + " VRAM";
      } else {
        device = "CPU";
        vram = "";
      }
      corrEl.textContent = "корректор: " + m.name + " · " + device + vram;
      corrEl.title = "Коррекция речи: " + m.name +
        " · " + (m.params || "?") + " · " + fmtModelSize(m.size_mb) + " на диске" +
        (m.loaded ? " · " + device + vram : " · сейчас не загружена в память") +
        (corr.base_url ? " · " + corr.base_url : "");
      corrDot.className = "dot " + (m.loaded ? "on" : "");
    } catch (e) {
      corrEl.textContent = "корректор: " + (name || "ollama") + " · Ollama недоступен";
      corrEl.title = "Ollama недоступен: " + e.message;
      corrDot.className = "dot err";
    }
  }

  // ---------- config/settings ----------
  async function loadSettings() {
    try {
      state.config = await api("/api/config");
      state.bots = (await api("/api/bots")).bots || [];
      fillSettings();
      loadVoiceModels();
      updateModelStatus();
    } catch (e) { toast("Настройки: " + e.message, true); }
  }
  function fillSettings() {
    var c = state.config || {};
    var ai = c.ai || {}, sx = c.synx || {}, co = c.comfy || {}, pi = c.pipeline || {};
    $("ai-base").value = ai.base_url || "";
    $("ai-key").value = ai.api_key || "";
    $("ai-model").value = ai.model || "";
    $("ai-temp").value = ai.temperature != null ? ai.temperature : 0.2;
    $("ai-max").value = ai.max_tokens || 4000;
    $("synx-base").value = sx.base_url || "";
    $("synx-login").value = sx.login || "";
    $("synx-password").value = sx.password || "";
    $("synx-projects").value = (sx.projects || []).join(", ");
    $("synx-scan").value = sx.scan_interval_sec || 180;
    $("synx-workers").value = sx.scan_workers || 12;
    $("synx-auto-scan").checked = sx.auto_scan !== false;
    $("synx-auto-analyze").checked = sx.auto_analyze !== false;
    $("synx-task-comments").checked = !!sx.include_task_comments;
    $("comfy-root").value = co.root || "";
    $("comfy-base").value = co.base_url || "";
    $("comfy-input").value = co.input_dir || "";
    $("comfy-workflow").value = co.workflow_path || "";
    $("comfy-output").value = co.output_dir || "";
    var pa = c.paths || {}; var lays = pa.layouts || {};
    $("paths-root").value = pa.projects_root || "";
    $("layout-colossus").value = lays.colossus || "";
    $("layout-liberia").value = lays.liberia || "";
    $("layout-default").value = pa.default_layout || "";
    $("obsidian-vault").value = pa.obsidian_vault || "";
    $("comfy-timeout").value = co.timeout_sec || 1800;
    var vo = c.voice || {}, vc = vo.correct || {};
    $("voice-llm-correct").checked = vo.llm_correct !== false;
    $("voice-correct-provider").value = vc.provider || "ollama";
    $("voice-ollama-url").value = vc.base_url || "";
    $("voice-ollama-model").value = vc.model || "";
    $("voice-keep-alive").value = vc.keep_alive || "5m";
    $("voice-correct-timeout").value = vc.timeout || 30;
    $("voice-main-model-info").textContent = "Основная модель: " + (ai.model || "не задана");
    voiceProviderUi();
    $("pipe-publish").checked = !!pi.auto_publish;
    $("pipe-poll").value = pi.poll_seconds || 5;
    $("pipe-work").value = pi.work_root || "";
    $("system-prompt").value = ai.system_prompt || "";
    renderBots();
  }
  function collectConfig() {
    var c = JSON.parse(JSON.stringify(state.config || {}));
    c.ai = c.ai || {}; c.synx = c.synx || {}; c.comfy = c.comfy || {}; c.pipeline = c.pipeline || {};
    c.ai.base_url = $("ai-base").value.trim();
    c.ai.api_key = $("ai-key").value.trim();
    c.ai.model = $("ai-model").value.trim();
    c.ai.temperature = Number($("ai-temp").value || 0.2);
    c.ai.max_tokens = Number($("ai-max").value || 4000);
    c.ai.system_prompt = $("system-prompt").value;
    c.synx.base_url = $("synx-base").value.trim();
    c.synx.login = $("synx-login").value.trim();
    c.synx.password = $("synx-password").value;
    c.synx.projects = $("synx-projects").value.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    c.synx.scan_interval_sec = Number($("synx-scan").value || 180);
    c.synx.scan_workers = Number($("synx-workers").value || 12);
    c.synx.auto_scan = $("synx-auto-scan").checked;
    c.synx.auto_analyze = $("synx-auto-analyze").checked;
    c.synx.include_task_comments = $("synx-task-comments").checked;
    c.comfy.root = $("comfy-root").value.trim();
    c.comfy.base_url = $("comfy-base").value.trim();
    c.comfy.input_dir = $("comfy-input").value.trim();
    c.comfy.workflow_path = $("comfy-workflow").value.trim();
    c.comfy.output_dir = $("comfy-output").value.trim();
    c.paths = c.paths || {}; c.paths.layouts = c.paths.layouts || {};
    c.paths.projects_root = $("paths-root").value.trim();
    c.paths.layouts.colossus = $("layout-colossus").value.trim();
    c.paths.layouts.liberia = $("layout-liberia").value.trim();
    c.paths.default_layout = $("layout-default").value.trim();
    c.paths.obsidian_vault = $("obsidian-vault").value.trim();
    c.comfy.timeout_sec = Number($("comfy-timeout").value || 1800);
    c.voice = c.voice || {}; c.voice.correct = c.voice.correct || {};
    c.voice.llm_correct = $("voice-llm-correct").checked;
    c.voice.correct.provider = $("voice-correct-provider").value;
    c.voice.correct.base_url = $("voice-ollama-url").value.trim();
    c.voice.correct.model = $("voice-ollama-model").value.trim();
    c.voice.correct.keep_alive = $("voice-keep-alive").value.trim() || "5m";
    c.voice.correct.timeout = Number($("voice-correct-timeout").value || 30);
    c.pipeline.auto_publish = $("pipe-publish").checked;
    c.pipeline.poll_seconds = Number($("pipe-poll").value || 5);
    c.pipeline.work_root = $("pipe-work").value.trim();
    return c;
  }
  var voiceModels = [];
  function fmtModelSize(mb) {
    mb = Number(mb || 0);
    if (mb >= 1024) return (mb / 1024).toFixed(1) + " ГБ";
    return Math.round(mb) + " МБ";
  }
  function voiceProviderUi() {
    var provider = ($("voice-correct-provider").value || "ollama").toLowerCase();
    var local = provider === "ollama";
    ["voice-ollama-url", "voice-ollama-model", "voice-model-pick",
     "voice-models-refresh", "voice-model-unload", "voice-keep-alive"].forEach(function (id) {
      var el = $(id);
      if (el) el.disabled = !local;
    });
    var info = $("voice-main-model-info");
    if (info) {
      info.textContent = "Основная модель: " +
        ((state.config && state.config.ai && state.config.ai.model) || "не задана");
    }
    updateVoiceModelInfo();
  }

  function updateVoiceModelInfo() {
    var el = $("voice-model-info");
    if (!el) return;
    var provider = ($("voice-correct-provider").value || "ollama").toLowerCase();
    if (provider === "ai") {
      el.textContent = "Используется основная модель: " + ((state.config && state.config.ai && state.config.ai.model) || "—");
      return;
    }
    if (provider === "none") { el.textContent = "Коррекция отключена"; return; }
    var name = ($("voice-ollama-model").value || "").trim();
    if (!name) { el.textContent = "—"; return; }
    var m = voiceModels.find(function (x) { return x.name === name; });
    if (!m) { el.textContent = "Модель не найдена в Ollama — проверь имя или обнови список."; return; }
    var bits = [m.name, fmtModelSize(m.size_mb) + " на диске"];
    if (m.params) bits.push(m.params);
    if (m.loaded) bits.push("в VRAM: " + fmtModelSize(m.vram_mb));
    else bits.push("не загружена в VRAM");
    el.textContent = bits.join(" · ");
  }
  async function loadVoiceModels() {
    var sel = $("voice-model-pick");
    if (!sel) return;
    try {
      var d = await api("/api/voice/models");
      voiceModels = d.models || [];
      sel.innerHTML = '<option value="">Установленные модели…</option>';
      voiceModels.forEach(function (m) {
        var o = document.createElement("option");
        o.value = m.name;
        o.textContent = m.name + " · " + fmtModelSize(m.size_mb) +
          (m.params ? " · " + m.params : "") +
          (m.loaded ? " · VRAM " + fmtModelSize(m.vram_mb) : "");
        sel.appendChild(o);
      });
      var cur = ($("voice-ollama-model").value || d.current || "").trim();
      if (cur && voiceModels.some(function (m) { return m.name === cur; })) sel.value = cur;
      updateVoiceModelInfo();
      updateCorrectorStatus();
    } catch (e) {
      voiceModels = [];
      sel.innerHTML = '<option value="">Ollama недоступен</option>';
      updateVoiceModelInfo();
    }
  }
  $("voice-correct-provider").addEventListener("change", voiceProviderUi);
  $("ai-model").addEventListener("input", function () {
    voiceProviderUi();
  });
  $("voice-model-pick").addEventListener("change", function () {
    if (this.value) {
      $("voice-ollama-model").value = this.value;
      updateVoiceModelInfo();
    }
  });
  $("voice-ollama-model").addEventListener("input", updateVoiceModelInfo);
  $("voice-models-refresh").addEventListener("click", loadVoiceModels);
  $("voice-model-unload").addEventListener("click", async function () {
    var model = ($("voice-ollama-model").value || "").trim();
    try {
      var d = await api("/api/voice/models/unload", {
        method: "POST", body: JSON.stringify({ model: model })
      });
      toast("Выгружено из VRAM: " + ((d.unloaded || []).join(", ") || "—"));
      await loadVoiceModels();
    } catch (e) { toast("Выгрузка: " + e.message, true); }
  });

  $("settings-save").addEventListener("click", async function () {
    try {
      state.config = await api("/api/config", { method: "PUT", body: JSON.stringify({ config: collectConfig() }) });
      updateModelStatus();
      toast("Настройки сохранены");
    } catch (e) { toast("Ошибка сохранения: " + e.message, true); }
  });
  $("ai-test").addEventListener("click", async function () {
    var el = $("ai-test-result");
    el.textContent = "проверяю...";
    try {
      var d = await api("/api/ai/test", { method: "POST" });
      el.textContent = "OK: " + (d.reply || "").slice(0, 80);
    } catch (e) { el.textContent = "ошибка: " + e.message; }
  });
  $("voice-test").addEventListener("click", async function () {
    var el = $("voice-test-result");
    el.textContent = "проверяю...";
    try {
      var d = await api("/api/voice/correct/test", { method: "POST" });
      var bits = [d.ok ? "OK" : "без изменений"];
      if (d.seconds != null) bits.push(d.seconds + " с");
      if (d.vram_mb) bits.push("GPU " + d.vram_mb + " МБ");
      else if (d.provider === "ollama") bits.push("модель не в VRAM");
      if (d.model) bits.push(d.model);
      el.textContent = bits.join(" · ") + (d.text ? ": " + d.text.slice(0, 70) : "");
      loadVoiceModels();
    } catch (e) { el.textContent = "ошибка: " + e.message; }
  });

  // ---------- bots ----------
  function renderBots() {
    var tb = $("bots-table").tBodies[0];
    tb.innerHTML = "";
    state.bots.forEach(function (b, i) {
      var tr = document.createElement("tr");
      tr.dataset.i = i;
      tr.innerHTML =
        '<td><input class="input" data-bot="login" value="' + esc(b.login || "") + '"></td>' +
        '<td><input class="input" data-bot="password" value="' + esc(b.password || "") + '"></td>' +
        '<td><input class="input mono" data-bot="user_id" value="' + esc(b.user_id || "") + '"></td>' +
        '<td><input class="input" data-bot="role" value="' + esc(b.role || "") + '"></td>' +
        '<td class="center"><input type="checkbox" data-bot="enabled" ' + (b.enabled !== false ? "checked" : "") + '></td>' +
        '<td><button class="btn danger" data-bot-del="' + i + '">×</button></td>';
      tb.appendChild(tr);
    });
    RobotTables.refresh($("bots-table"));
  }
  $("bot-add").addEventListener("click", function () {
    state.bots.push({ login: "", password: "", role: "artist", enabled: true, user_id: "" });
    renderBots();
  });
  $("bots-table").addEventListener("click", function (e) {
    var b = e.target.closest("[data-bot-del]");
    if (!b) return;
    state.bots.splice(Number(b.dataset.botDel), 1);
    renderBots();
  });
  $("bots-table").addEventListener("input", function (e) {
    var tr = e.target.closest("tr"); if (!tr) return;
    var i = Number(tr.dataset.i); var key = e.target.dataset.bot;
    if (!key) return;
    state.bots[i][key] = (e.target.type === "checkbox") ? e.target.checked : e.target.value;
  });
  $("settings-save-bots").addEventListener("click", async function () {
    try {
      var d = await api("/api/bots", { method: "PUT", body: JSON.stringify({ bots: state.bots }) });
      state.bots = d.bots; renderBots(); toast("Боты сохранены");
    } catch (e) { toast("Ошибка ботов: " + e.message, true); }
  });

  // ---------- tasks ----------
  async function loadTasks() {
    try {
      var d = await api("/api/tasks");
      state.tasks = d.tasks || [];
      renderTasks();
    } catch (e) { toast("Задачи: " + e.message, true); }
  }
  function taskBadge(s) {
    var map = { queued: "info", running: "warn", paused: "warn", skipped: "", done: "ok", error: "bad" };
    return '<span class="badge ' + (map[s] || "") + '">' + esc(s || "") + "</span>";
  }
  function renderTasks() {
    var tb = $("task-table").tBodies[0];
    tb.innerHTML = "";
    state.tasks.forEach(function (t) {
      var tr = document.createElement("tr");
      tr.dataset.id = t.id;
      tr.dataset.hay = [t.shot, t.task_name, t.task_type, t.bot, t.status, t.stage].join(" ");
      if (t.status === "running") tr.classList.add("running");
      if (t.status === "error") tr.classList.add("error");
      if (t.status === "done") tr.classList.add("done");
      var pct = Math.max(0, Math.min(100, Number(t.progress || 0)));
      tr.innerHTML =
        '<td class="num">' + t.id + "</td>" +
        '<td><b>' + esc(t.shot || "") + "</b></td>" +
        '<td class="wrap">' + (t.priority ? '<span class="badge purple" title="Приоритет: сообщение @p.robot">@p.robot</span> ' : "") + esc(t.task_name || "") + "</td>" +
        '<td>' + esc(t.task_type || "") + "</td>" +
        '<td>' + esc(t.bot || "") + "</td>" +
        '<td data-cell="status">' + taskBadge(t.status) + "</td>" +
        '<td data-cell="stage">' + esc(t.stage || "") + "</td>" +
        '<td data-cell="progress"><div class="progress" title="' + pct + '%"><i style="width:' + pct + '%"></i></div></td>' +
        '<td data-cell="tokens" class="num">' + (Number(t.tokens_in || 0) + Number(t.tokens_out || 0)) + "</td>" +
        '<td data-cell="time" class="num">' + fmtSec(t.elapsed_sec) + "</td>" +
        '<td>' + (t.synx_url ? '<a class="link" href="' + esc(t.synx_url) + '" data-open-url="' + esc(t.synx_url) + '">' + esc(t.version_code || "открыть") + "</a>" : "—") + "</td>" +
        '<td data-cell="updated">' + fmtTime(t.updated_at) + "</td>" +
        '<td><button class="btn" data-task-open="' + t.id + '">Папка</button></td>';
      tb.appendChild(tr);
    });
    RobotTables.refresh($("task-table"));
    RobotTables.filter($("task-table"), $("task-filter").value);
    updateTaskSelected();
  }
  function updateTaskSelected() {
    var n = RobotTables.selected($("task-table")).length;
    var el = $("task-check-all");
    if (el) el.checked = n > 0 && n === $("task-table").tBodies[0].rows.length;
  }
  $("task-table").addEventListener("click", function (e) {
    var ext = e.target.closest("[data-open-url]");
    if (ext) {
      e.preventDefault();
      openExternal(ext.dataset.openUrl);
      return;
    }
    var o = e.target.closest("[data-task-open]");
    if (o) { openFolder(Number(o.dataset.taskOpen)); return; }
    if (e.target.closest("button,input,a")) return;
    updateTaskSelected();
  });
  $("task-filter").addEventListener("input", function () { RobotTables.filter($("task-table"), this.value); });
  $("task-status").addEventListener("change", function () {
    var v = this.value;
    $("task-table").tBodies[0].querySelectorAll("tr").forEach(function (tr) {
      if (!v) { tr.style.display = ""; return; }
      var txt = (tr.dataset.hay || "").toLowerCase();
      tr.style.display = txt.indexOf(v.toLowerCase()) !== -1 ? "" : "none";
    });
  });
  $("task-refresh").addEventListener("click", loadTasks);
  $("task-delete-done").addEventListener("click", async function () {
    if (!confirm("Удалить все выполненные задачи из очереди?")) return;
    try { var d = await api("/api/tasks/delete-done", { method: "POST" }); toast("Удалено: " + d.deleted); await loadTasks(); }
    catch (e) { toast(e.message, true); }
  });

  function openExternal(url) {
    if (!url) return;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_url) {
      window.pywebview.api.open_url(url).then(function () {}, function () { window.open(url, "_blank"); });
    } else {
      window.open(url, "_blank");
    }
  }
  function synxBase() {
    return (state.config && state.config.synx && state.config.synx.base_url)
      ? String(state.config.synx.base_url).replace(/\/$/, "") : "https://synks.ru";
  }
  function openHref(href) {
    if (!href) return;
    if (href.indexOf("http://") === 0 || href.indexOf("https://") === 0 || href.indexOf("mailto:") === 0) {
      openExternal(href); return;
    }
    if (href.indexOf("/") === 0) {
      var local = href.indexOf("/api/") === 0 || href.indexOf("/static/") === 0;
      openExternal((local ? window.location.origin : synxBase()) + href); return;
    }
    if (href.indexOf("file://") === 0) { openPath(decodeURIComponent(href.slice(7))); return; }
    if (href.indexOf("/home/") === 0 || href.indexOf("/mnt/") === 0 || href.indexOf("~/") === 0) {
      openPath(href);
    }
  }
  function openPath(path) {
    if (!path) return;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_path) {
      window.pywebview.api.open_path(path).then(function (r) {
        if (r && !r.ok && !r.cancelled) toast(r.error || "Не удалось открыть файл", true);
      }, function () { toast("Не удалось открыть файл", true); });
    } else {
      window.open("file://" + path, "_blank");
    }
  }
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented) return;
    var a = e.target.closest("a[href]");
    if (a) {
      var href = a.getAttribute("href") || "";
      if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
      e.preventDefault(); openHref(href); return;
    }
    var code = e.target.closest("code, .mono");
    if (code) {
      var text = (code.textContent || "").trim();
      if (/^(\/|~\/).+\.pdf$/i.test(text)) {
        e.preventDefault(); openPdfViewer(text, "PDF"); return;
      }
      if (/^(\/|~\/).+\.(png|jpe?g|webp|gif|mp4|mov|webm|mkv|json|md|txt)$/i.test(text)) {
        e.preventDefault(); openPath(text);
      }
    }
  }, false);

  async function openFolder(id) {
    try { var d = await api("/api/tasks/" + id + "/open-folder", { method: "POST" }); toast("Открыто: " + d.path); }
    catch (e) { toast(e.message, true); }
  }

  // row context menu
  $("task-table").addEventListener("contextmenu", function (e) {
    var tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    e.preventDefault();
    var id = Number(tr.dataset.id);
    var sel = RobotTables.selected($("task-table"));
    if (sel.indexOf(String(id)) === -1) {
      RobotTables.selectOnly($("task-table"), id);
      updateTaskSelected();
    }
    var items = [
      { label: "Открыть рабочую папку", fn: function () { openFolder(id); } },
      { label: "Поставить на паузу", fn: function () { taskAction(id, "pause"); } },
      { label: "Продолжить задачу", fn: function () { taskAction(id, "resume"); } },
      { label: "Пропустить (skip)", fn: function () { taskAction(id, "skip"); } },
      { label: "Вернуть из skip", fn: function () { taskAction(id, "unskip"); } },
      { label: "Опубликовать результат", fn: function () { publishTask(id); } },
      { sep: true },
      { label: "Удалить задачу", danger: true, fn: function () { deleteTask(id, false); } },
      { label: "Удалить задачу + папку", danger: true, fn: function () { deleteTask(id, true); } },
      { label: "Удалить все выполненные", danger: true, fn: function () { deleteDone(); } }
    ];
    openContextMenu(e.clientX, e.clientY, items);
  });
  async function taskAction(id, action) {
    try { await api("/api/tasks/" + id + "/" + action, { method: "POST" }); toast(action + ": #" + id); await loadTasks(); }
    catch (e) { toast(e.message, true); }
  }
  async function publishTask(id) {
    try { var d = await api("/api/tasks/" + id + "/publish", { method: "POST" }); toast("Опубликовано: " + (d.version && d.version.version_code)); await loadTasks(); }
    catch (e) { toast(e.message, true); }
  }
  async function deleteTask(id, files) {
    if (!confirm("Удалить задачу #" + id + (files ? " вместе с папкой?" : "?"))) return;
    try { await api("/api/tasks/" + id + "?delete_files=" + (files ? "true" : "false"), { method: "DELETE" }); toast("Удалено #" + id); await loadTasks(); }
    catch (e) { toast(e.message, true); }
  }
  async function deleteDone() {
    if (!confirm("Удалить все выполненные задачи?")) return;
    try { var d = await api("/api/tasks/delete-done", { method: "POST" }); toast("Удалено: " + d.deleted); await loadTasks(); }
    catch (e) { toast(e.message, true); }
  }

  // ---------- active task polling (only active row, every 5s) ----------
  async function pollActive() {
    try {
      var d = await api("/api/tasks/active");
      var t = d.task;
      if (!t) return;
      var tr = $("task-table").tBodies[0].querySelector('tr[data-id="' + t.id + '"]');
      if (!tr) return;
      state.activeId = t.id;
      tr.classList.add("running");
      tr.querySelector('[data-cell="status"]').innerHTML = taskBadge(t.status);
      tr.querySelector('[data-cell="stage"]').textContent = t.stage || "";
      var pct = Math.max(0, Math.min(100, Number(t.progress || 0)));
      tr.querySelector('[data-cell="progress"] .progress').title = pct + "%";
      tr.querySelector('[data-cell="progress"] .progress > i').style.width = pct + "%";
      tr.querySelector('[data-cell="tokens"]').textContent = Number(t.tokens_in || 0) + Number(t.tokens_out || 0);
      tr.querySelector('[data-cell="time"]').textContent = fmtSec(t.elapsed_sec);
      tr.querySelector('[data-cell="updated"]').textContent = fmtTime(t.updated_at);
    } catch (e) { /* ignore transient */ }
  }
  setInterval(pollActive, 5000);

  // ---------- pipeline ----------
  async function pipelineStatus() {
    try {
      var d = await api("/api/pipeline/status");
      var dot = $("pipeline-dot"), txt = $("pipeline-status");
      if (d.running) { dot.className = "dot busy"; txt.textContent = "конвейер: " + (d.stage || "running") + " · очередь " + d.queue; }
      else { dot.className = "dot"; txt.textContent = "конвейер: остановлен · очередь " + d.queue; }
      $("pipeline-toggle").textContent = d.running ? "Остановить конвейер" : "Запустить конвейер";
      if (d.last_error) $("synx-status").title = d.last_error;
    } catch (e) {}
  }
  $("pipeline-toggle").addEventListener("click", async function () {
    try {
      var d = await api("/api/pipeline/status");
      await api(d.running ? "/api/pipeline/stop" : "/api/pipeline/start", { method: "POST" });
      await pipelineStatus();
    } catch (e) { toast(e.message, true); }
  });
  setInterval(pipelineStatus, 3000);

  // ---------- context menu / modal ----------
  function openContextMenu(x, y, items) {
    var m = $("ctx-menu");
    m.innerHTML = "";
    items.forEach(function (it) {
      if (it.sep) { var s = document.createElement("div"); s.className = "ctx-sep"; m.appendChild(s); return; }
      var d = document.createElement("div");
      d.className = "ctx-item" + (it.danger ? " danger" : "");
      d.textContent = it.label;
      d.addEventListener("click", function () { m.classList.remove("open"); it.fn(); });
      m.appendChild(d);
    });
    m.style.left = Math.min(x, window.innerWidth - 280) + "px";
    m.style.top = Math.min(y, window.innerHeight - 340) + "px";
    m.classList.add("open");
    setTimeout(function () { document.addEventListener("mousedown", closeCtx, { once: true }); }, 0);
    function closeCtx(e) { if (!m.contains(e.target)) m.classList.remove("open"); }
  }
  function openModal(title, bodyHtml, buttons) {
    $("modal-title").textContent = title;
    $("modal-body").innerHTML = bodyHtml;
    var foot = $("modal-foot"); foot.innerHTML = "";
    (buttons || []).forEach(function (b) {
      var el = document.createElement("button");
      el.className = "btn" + (b.primary ? " primary" : "");
      el.textContent = b.label;
      el.addEventListener("click", b.fn);
      foot.appendChild(el);
    });
    var close = document.createElement("button"); close.className = "btn"; close.textContent = "Закрыть"; close.addEventListener("click", closeModal); foot.appendChild(close);
    $("modal").classList.remove("hidden");
  }
  function closeModal() { $("modal").classList.add("hidden"); }
  $("modal-close").addEventListener("click", closeModal);
  $("modal").addEventListener("click", function (e) { if (e.target === $("modal")) closeModal(); });

  // ---------- Ctrl + wheel: interface/font zoom ----------
  var uiZoom = 1;
  try { uiZoom = parseFloat(localStorage.getItem("uiZoom") || "1"); } catch (e) { uiZoom = 1; }
  if (!isFinite(uiZoom) || uiZoom < 0.7 || uiZoom > 1.6) uiZoom = 1;
  function applyUiZoom() {
    // CSS zoom is not reliable in WebKitGTK, so scale the app container.
    // The container is stretched by 1/scale and then scaled, so it still
    // fills the window and fixed overlays keep covering the viewport.
    var app = $("app");
    if (!app) return;
    if (Math.abs(uiZoom - 1) < 0.001) {
      app.style.transform = "";
      app.style.transformOrigin = "";
      app.style.width = "";
      app.style.height = "";
    } else {
      app.style.transformOrigin = "0 0";
      app.style.transform = "scale(" + uiZoom + ")";
      app.style.width = (100 / uiZoom) + "vw";
      app.style.height = (100 / uiZoom) + "vh";
    }
  }
  function setUiZoom(next) {
    next = Math.round(Math.max(0.7, Math.min(1.6, next)) * 100) / 100;
    if (next === uiZoom) return;
    uiZoom = next;
    try { localStorage.setItem("uiZoom", String(uiZoom)); } catch (e) {}
    try {
      fetch("/api/ui/zoom", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({zoom: uiZoom})}).catch(function () {});
    } catch (e) {}
    applyUiZoom();
    updateZoomUi();
    toast("Масштаб: " + Math.round(uiZoom * 100) + "%");
  }
  applyUiZoom();
  window.addEventListener("wheel", function (e) {
    if (!e.ctrlKey && !e.metaKey) return;
    var t = e.target;
    if (t && t.closest && t.closest("#pdf-viewer:not(.hidden), #image-viewer:not(.hidden)")) return;
    e.preventDefault();
    e.stopPropagation();
    setUiZoom(uiZoom + (e.deltaY < 0 ? 0.05 : -0.05));
  }, { passive: false, capture: true });
  function updateZoomUi() {
    var el = $("zoom-value");
    if (el) el.textContent = Math.round(uiZoom * 100) + "%";
  }
  $("zoom-in").addEventListener("click", function () { setUiZoom(uiZoom + 0.05); });
  $("zoom-out").addEventListener("click", function () { setUiZoom(uiZoom - 0.05); });
  $("zoom-value").addEventListener("click", function () { setUiZoom(1); });
  updateZoomUi();
  try {
    fetch("/api/ui/zoom?t=" + Date.now()).then(function (r) { return r.json(); }).then(function (d) {
      var z = parseFloat(d && d.zoom);
      if (!isFinite(z) || z < 0.7 || z > 1.6 || Math.abs(z - uiZoom) < 0.001) return;
      uiZoom = z; applyUiZoom(); updateZoomUi();
    }).catch(function () {});
  } catch (e) {}
  document.addEventListener("keydown", function (e) {
    if (!(e.ctrlKey || e.metaKey)) return;
    if (e.key === "+" || e.key === "=") { e.preventDefault(); setUiZoom(uiZoom + 0.05); }
    else if (e.key === "-" || e.key === "_") { e.preventDefault(); setUiZoom(uiZoom - 0.05); }
    else if (e.key === "0") { e.preventDefault(); setUiZoom(1); }
  });

  // ---------- brand: stretch "production assistant" to the top row width ----------
  function fitBrandSub() {
    var row = document.querySelector(".brand-row");
    var sub = document.querySelector(".brand-sub");
    if (!row || !sub) return;
    sub.style.letterSpacing = "0px";
    var app = $("app");
    var scale = 1;
    if (app && app.offsetWidth) {
      scale = app.getBoundingClientRect().width / app.offsetWidth || 1;
    }
    var target = row.getBoundingClientRect().width / scale;
    var natural = sub.getBoundingClientRect().width / scale;
    var chars = (sub.textContent || "").replace(/\s+/g, " ").length;
    if (!(natural > 0) || chars < 2) return;
    var spacing = (target - natural) / chars;
    sub.style.letterSpacing = spacing.toFixed(2) + "px";
  }
  fitBrandSub();
  window.addEventListener("resize", fitBrandSub);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(fitBrandSub).catch(function () {});
  }

  setInterval(updateCorrectorStatus, 12000);

  // ---------- init ----------
  async function init() {
    await loadSettings();
    loadChatPanels();
    await loadTasks();
    await loadChatSessions();
    await loadChat();
    await pipelineStatus();
    try {
      var meta = await api("/api/meta");
      $("last-scan").textContent = meta.last_scan ? "scan: " + fmtTime(meta.last_scan) : "scan: ещё не было";
      $("synx-dot").className = "dot on";
      $("synx-status").textContent = "synx: " + (state.config.synx.base_url || "");
    } catch (e) {}
  }
  init();
})();
