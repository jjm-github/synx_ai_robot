(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = { config: null, bots: [], pubs: [], tasks: [], activeId: null, view: "publications",
                pubSyncAt: "", pubSyncId: 0 };

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
      publications: ["Публикации", "Новые комментарии и review-замечания из synx"],
      tasks: ["Задачи", "Очередь производства и этапы выполнения"],
      chat: ["Чат", "Диалог с моделью, референсы и генерация изображений в ComfyUI"],
      settings: ["Настройки", "AI, synx, ComfyUI, боты и системный промпт"]
    };
    $("view-title").textContent = titles[v][0];
    $("view-subtitle").textContent = titles[v][1];
    if (v === "tasks") loadTasks();
    if (v === "publications") loadPublications();
    if (v === "chat") loadChat();
    if (v === "settings") loadSettings();
  }
  document.querySelectorAll(".nav-btn").forEach(function (b) {
    b.addEventListener("click", function () { setView(b.dataset.view); });
  });


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
  function chatImageHtml(im, generated) {
    var url = im.url || ("/api/chat/file/images/" + im.name);
    return '<img src="' + esc(url) + '" data-chat-image="1" data-title="' + esc(im.name || "image") + '" alt="">';
  }
  function renderChatMessages(msgs) {
    var box = $("chat-messages");
    if (!msgs || !msgs.length) {
      box.innerHTML = '<div class="chat-empty"><b>Чат с моделью</b><br><br>Спроси что-нибудь или попроси:<br>«Нарисуй кадр в стиле чёрной туши по этим референсам».<br>Референсы можно прикрепить кнопкой «+ Референс».</div>';
      return;
    }
    box.innerHTML = "";
    msgs.forEach(function (m) {
      var isUser = m.role === "user";
      var wrap = document.createElement("div");
      wrap.className = "msg " + (isUser ? "user" : "assistant");
      var imgs = (isUser ? m.images : m.generated) || [];
      var imgHtml = imgs.length ? '<div class="msg-images">' + imgs.map(function (im) { return chatImageHtml(im, !isUser); }).join("") + "</div>" : "";
      var textHtml = isUser ? '<div class="msg-text">' + esc(m.content || "").replace(/\n/g, "<br>") + "</div>" : '<div class="msg-text markdown-body">' + mdHtml(m.content || "") + "</div>";
      var usage = m.usage && (m.usage.input || m.usage.output) ? '<div class="msg-meta">токены: ' + (m.usage.input || 0) + " / " + (m.usage.output || 0) + "</div>" : "";
      var progress = (m.progress || []).filter(function (e) { return e.type === "stage"; }).map(function (e) { return esc(e.text || ""); }).join("<br>");
      var progressHtml = progress ? '<details class="msg-progress"><summary>Этапы выполнения</summary><div>' + progress + '</div></details>' : "";
      var playlist = m.playlist_text ? '<div class="playlist-card"><div class="playlist-card-head"><b>Плейлист Synx</b><button class="btn" data-copy-playlist>Скопировать</button></div><details><summary>Показать текст плейлиста</summary><pre>' + esc(m.playlist_text) + '</pre></details></div>' : "";
      wrap.innerHTML = '<div class="msg-avatar">' + (isUser ? "Я" : "AI") + '</div><div class="msg-bubble">' + textHtml + imgHtml + progressHtml + playlist + usage + "</div>";
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
  }
  function renderChatChips() {
    var box = $("chat-chips"); box.innerHTML = "";
    chatAttachments.forEach(function (im, i) {
      var d = document.createElement("div"); d.className = "chat-chip";
      d.innerHTML = '<img src="' + esc(im.url) + '" alt=""><button data-chip="' + i + '">×</button>';
      box.appendChild(d);
    });
    $("chat-chips").querySelectorAll("[data-chip]").forEach(function (b) {
      b.addEventListener("click", function () { chatAttachments.splice(Number(b.dataset.chip), 1); renderChatChips(); });
    });
  }
  $("chat-files").addEventListener("change", async function () {
    var files = Array.from(this.files || []); this.value = "";
    for (var i = 0; i < files.length; i++) {
      try {
        var fd = new FormData(); fd.append("file", files[i]);
        var r = await fetch("/api/chat/upload", { method: "POST", body: fd });
        var d = await r.json();
        if (!r.ok) throw new Error(d.detail || "upload failed");
        chatAttachments.push(d);
      } catch (e) { toast("Референс: " + e.message, true); }
    }
    renderChatChips();
  });
  function appendUserBubble(text, images) {
    var box = $("chat-messages");
    var empty = box.querySelector(".chat-empty"); if (empty) box.innerHTML = "";
    var wrap = document.createElement("div"); wrap.className = "msg user";
    var imgs = (images || []).map(function (im) { return '<img src="' + esc(im.url) + '" data-chat-image="1" alt="">'; }).join("");
    wrap.innerHTML = '<div class="msg-avatar">Я</div><div class="msg-bubble"><div class="msg-text">' + esc(text || "").replace(/\n/g, "<br>") + '</div>' +
      (imgs ? '<div class="msg-images">' + imgs + '</div>' : "") + '</div>';
    box.appendChild(wrap); box.scrollTop = box.scrollHeight;
    return wrap;
  }
  function appendThinkingBubble() {
    var box = $("chat-messages");
    var wrap = document.createElement("div"); wrap.className = "msg assistant"; wrap.id = "chat-thinking";
    wrap.innerHTML = '<div class="msg-avatar">AI</div><div class="msg-bubble">' +
      '<div class="thinking-row"><span class="thinking-dots"><i></i><i></i><i></i></span><span id="thinking-text">Анализирую запрос...</span></div>' +
      '<div class="job-log" id="job-log"></div><div class="job-images" id="job-images"></div></div>';
    box.appendChild(wrap); box.scrollTop = box.scrollHeight;
    return wrap;
  }
  async function pollChatJob(jobId) {
    var seen = 0;
    for (;;) {
      var d;
      try { d = await api("/api/chat/jobs/" + encodeURIComponent(jobId)); }
      catch (e) { toast("Чат: " + e.message, true); break; }
      (d.events || []).forEach(function (evt) {
        if ((evt.seq || 0) <= seen) return;
        seen = evt.seq || seen;
        var th = $("thinking-text"); if (th && evt.text) th.textContent = evt.text;
        if (evt.type === "stage") {
          var log = $("job-log");
          if (log) { var line = document.createElement("div"); line.className = "job-log-line"; line.textContent = "• " + evt.text; log.appendChild(line); }
        }
        if (evt.type === "image" && evt.image) {
          var jb = $("job-images");
          if (jb) { var im = document.createElement("img"); im.src = evt.image.url; im.dataset.chatImage = "1"; im.dataset.title = evt.image.name || "image"; im.alt = ""; jb.appendChild(im); }
        }
        var box = $("chat-messages"); if (box) box.scrollTop = box.scrollHeight;
      });
      if (d.status === "done") break;
      if (d.status === "error") { toast("Ошибка чата: " + (d.error || ""), true); break; }
      await new Promise(function (r) { setTimeout(r, 800); });
    }
    var th = $("chat-thinking"); if (th) th.remove();
    await loadChatSessions(chatSession);
    await loadChat();
  }
  async function sendChat() {
    var text = $("chat-input").value.trim();
    if (!text && !chatAttachments.length) return;
    var images = chatAttachments.slice();
    var payload = { session_id: chatSession, text: text, images: images };
    chatAttachments = []; renderChatChips(); $("chat-input").value = "";
    appendUserBubble(text, images);
    appendThinkingBubble();
    $("chat-send").disabled = true; $("chat-send").textContent = "Думаю...";
    try {
      var d = await api("/api/chat/send", { method: "POST", body: JSON.stringify(payload) });
      await pollChatJob(d.job_id);
    } catch (e) {
      var th = $("chat-thinking"); if (th) th.remove();
      toast("Чат: " + e.message, true);
      await loadChat();
    } finally {
      $("chat-send").disabled = false; $("chat-send").textContent = "Отправить";
    }
  }
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
  function fallbackCopy(text) {
    var ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.left = "-9999px";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast("Скопировано"); } catch (e) { toast("Не удалось скопировать", true); }
    document.body.removeChild(ta);
  }
  $("chat-messages").addEventListener("click", function (e) {
    var cp = e.target.closest("[data-copy-playlist]");
    if (cp) {
      var pre = cp.closest(".playlist-card").querySelector("pre");
      copyText(pre ? pre.textContent : "");
      return;
    }
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
    if (!text) return;
    var isUser = msg.classList.contains("user");
    openContextMenu(e.clientX, e.clientY, [
      { label: isUser ? "Копировать запрос" : "Копировать ответ", fn: function () { copyText(text); } },
      { label: "Копировать как цитату", fn: function () {
        copyText(text.split("\n").map(function (line) { return "> " + line; }).join("\n"));
      } }
    ]);
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

  // ---------- config/settings ----------
  async function loadSettings() {
    try {
      state.config = await api("/api/config");
      state.bots = (await api("/api/bots")).bots || [];
      fillSettings();
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
    c.pipeline.auto_publish = $("pipe-publish").checked;
    c.pipeline.poll_seconds = Number($("pipe-poll").value || 5);
    c.pipeline.work_root = $("pipe-work").value.trim();
    return c;
  }
  $("settings-save").addEventListener("click", async function () {
    try {
      state.config = await api("/api/config", { method: "PUT", body: JSON.stringify({ config: collectConfig() }) });
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

  // ---------- publications ----------
  async function loadPublications() {
    try {
      var st = $("pub-status").value;
      var d = await api("/api/publications?limit=5000" + (st ? "&status=" + encodeURIComponent(st) : ""));
      state.pubs = d.publications || [];
      var maxTs = "", maxId = 0;
      state.pubs.forEach(function (p) {
        var ts = String(p.updated_db || "");
        if (ts > maxTs || (ts === maxTs && Number(p.id) > maxId)) { maxTs = ts; maxId = Number(p.id) || 0; }
      });
      state.pubSyncAt = maxTs; state.pubSyncId = maxId;
      renderPublications();
    } catch (e) { toast("Публикации: " + e.message, true); }
  }
  function pubBadge(s) {
    var map = { "new": "info", "analyzed": "warn", "task_created": "ok", "no_task": "", "analysis_error": "bad" };
    return '<span class="badge ' + (map[s] || "") + '">' + esc(s || "") + "</span>";
  }
  function threadLabel(kind) {
    if (kind === "version") return "версия";
    if (kind === "task") return "задача";
    if (kind === "entity") return "сущность";
    return kind || "";
  }
  function actionBadge(p) {
    if (p.answered_by_version) {
      var v = p.answer_version_number ? " v" + String(p.answer_version_number).padStart(3, "0") : "";
      return '<span class="badge ok" title="По комментарию уже выпущена версия">отвечено' + v + '</span>';
    }
    if (p.is_mention) return '<span class="badge purple" title="Приоритетное указание для @p.robot">@p.robot</span>';
    if (p.needs_action === 1) return '<span class="badge bad">продолжение</span>';
    if (p.needs_action === 0) return '<span class="badge ok">версия</span>';
    return '<span class="badge warn">не разобрано</span>';
  }
  function renderPublications() {
    var tb = $("pub-table").tBodies[0];
    tb.innerHTML = "";
    var ordered = state.pubs.slice().sort(function (a, b) {
      return (b.is_mention ? 1 : 0) - (a.is_mention ? 1 : 0);
    });
    ordered.forEach(function (p) {
      var tr = document.createElement("tr");
      tr.dataset.id = p.id;
      tr.dataset.action = p.needs_action === 1 ? "continuation" : (p.needs_action === 0 ? "version" : "unknown");
      tr.dataset.answered = p.answered_by_version ? "1" : "0";
      tr.dataset.mention = p.is_mention ? "1" : "0";
      tr.dataset.images = (p.attachments || []).length ? "1" : "0";
      tr.dataset.hay = [p.project_alias, p.shot, p.thread_kind, p.version_number, p.author, p.body].join(" ").toLowerCase();
      var att = (p.attachments || []).map(function (a) {
        return '<a class="link" target="_blank" href="' + esc(a.url || ("/files/" + a.file_id)) + '">' + esc(a.name || "file") + "</a>";
      }).join("<br>");
      tr.innerHTML =
        '<td>' + esc(p.project_alias || p.project_id) + "</td>" +
        '<td><b>' + esc(p.shot || "") + "</b></td>" +
        '<td>' + esc(threadLabel(p.thread_kind)) + "</td>" +
        '<td>' + esc(p.version_number ? "v" + String(p.version_number).padStart(3, "0") : "") + "</td>" +
        '<td>' + esc(p.author || "") + "</td>" +
        '<td class="wrap" title="' + esc(p.body || "") + '">' + esc((p.body || "").replace(/\n/g, " ").slice(0, 260)) + "</td>" +
        '<td class="wrap">' + att + "</td>" +
        '<td>' + pubBadge(p.status) + "</td>" +
        '<td>' + actionBadge(p) + "</td>" +
        '<td><button class="btn" data-pub-analyze="' + p.id + '">AI</button> ' +
        '<button class="btn" data-pub-details="' + p.id + '" title="Открыть план .md">MD</button></td>';
      tb.appendChild(tr);
    });
    RobotTables.refresh($("pub-table"));
    applyPubFilters();
    updatePubSelected();
  }
  function applyPubFilters() {
    var q = ($("pub-filter").value || "").trim().toLowerCase();
    var action = $("pub-action").value;
    var showNoAction = $("pub-show-noaction").checked;
    var showAnswered = $("pub-show-answered").checked;
    Array.prototype.forEach.call($("pub-table").tBodies[0].rows, function (tr) {
      var ok = true;
      if (q && (tr.dataset.hay || "").indexOf(q) === -1) ok = false;
      if (action === "continuation" && tr.dataset.action !== "continuation") ok = false;
      if (action === "version" && tr.dataset.action !== "version") ok = false;
      if (action === "unknown" && tr.dataset.action !== "unknown") ok = false;
      if (action === "answered" && tr.dataset.answered !== "1") ok = false;
      if (action === "mention" && tr.dataset.mention !== "1") ok = false;
      if (action === "images" && tr.dataset.images !== "1") ok = false;
      if (!showNoAction && tr.dataset.action === "version" && tr.dataset.answered !== "1") ok = false;
      if (!showAnswered && action !== "answered" && tr.dataset.answered === "1") ok = false;
      tr.style.display = ok ? "" : "none";
    });
  }
  async function pollPublicationChanges() {
    if (document.hidden || !state.pubSyncAt) return;
    try {
      var d = await api("/api/publications/changes?since=" + encodeURIComponent(state.pubSyncAt) +
                        "&since_id=" + state.pubSyncId + "&limit=1000");
      var changes = d.publications || [];
      if (!changes.length) return;
      var byId = {};
      state.pubs.forEach(function (p) { byId[p.id] = p; });
      var newCount = 0;
      changes.forEach(function (p) {
        if (!byId[p.id]) newCount += 1;
        byId[p.id] = p;
      });
      var last = changes[changes.length - 1];
      if (last && last.updated_db) {
        var ts = String(last.updated_db), id = Number(last.id) || 0;
        if (ts > state.pubSyncAt || (ts === state.pubSyncAt && id > state.pubSyncId)) {
          state.pubSyncAt = ts; state.pubSyncId = id;
        }
      }
      state.pubs = Object.keys(byId).map(function (k) { return byId[k]; });
      state.pubs.sort(function (a, b) {
        return String(b.created_at || "").localeCompare(String(a.created_at || "")) || (b.id - a.id);
      });
      var selected = RobotTables.selected($("pub-table"));
      renderPublications();
      if (selected.length && RobotTables.selectIds) RobotTables.selectIds($("pub-table"), selected);
      updatePubSelected();
      if (newCount) toast("Новых сообщений synx: " + newCount);
      try {
        var meta = await api("/api/meta");
        $("last-scan").textContent = meta.last_scan ? "scan: " + fmtTime(meta.last_scan) : "scan: ещё не было";
      } catch (e2) {}
    } catch (e) { /* silently retry on the next tick */ }
  }
  setInterval(pollPublicationChanges, 10000);

  function updatePubSelected() {
    var sel = RobotTables.selected($("pub-table"));
    $("pub-selected-count").textContent = sel.length + " выбрано";
  }
  $("pub-table").addEventListener("dblclick", function (e) {
    var tr = e.target.closest("tr[data-id]");
    if (tr) showPlan(Number(tr.dataset.id));
  });
  $("pub-table").addEventListener("robot-selection", updatePubSelected);
  $("task-table").addEventListener("robot-selection", updateTaskSelected);
  $("pub-table").addEventListener("click", function (e) {
    var a = e.target.closest("[data-pub-analyze]");
    if (a) { analyzeOne(Number(a.dataset.pubAnalyze)); return; }
    var d = e.target.closest("[data-pub-details]");
    if (d) { showPlan(Number(d.dataset.pubDetails)); return; }
    if (e.target.closest("a,button,input")) return;
    updatePubSelected();
  });
  $("pub-filter").addEventListener("input", applyPubFilters);
  $("pub-action").addEventListener("change", applyPubFilters);
  $("pub-show-noaction").addEventListener("change", applyPubFilters);
  $("pub-show-answered").addEventListener("change", applyPubFilters);
  $("pub-status").addEventListener("change", loadPublications);
  $("pub-refresh").addEventListener("click", loadPublications);
  $("pub-reclassify").addEventListener("click", async function () {
    if (!confirm("Переанализировать всю таблицу публикаций? Версии будут помечены как «просто версия», требующие правок — как «продолжение».")) return;
    try {
      var d = await api("/api/publications/reclassify?ai=true", { method: "POST" });
      toast("Разметка: версий " + d.counts.version + ", продолжений " + d.counts.continuation + ", AI в очереди " + d.ai_queued);
      await loadPublications();
    } catch (e) { toast(e.message, true); }
  });
  $("pub-analyze-batch").addEventListener("click", async function () {
    if (!confirm("Поставить в AI-разметку все неразобранные комментарии? Это может занять время и токены.")) return;
    try {
      var d = await api("/api/publications/analyze-batch?limit=200", { method: "POST" });
      toast("В разметку поставлено: " + d.queued);
    } catch (e) { toast(e.message, true); }
  });

  async function analyzeOne(id) {
    toast("AI анализирует комментарий...");
    try {
      var d = await api("/api/publications/" + id + "/analyze", { method: "POST" });
      toast(d.plan && d.plan.needs_task === false ? "AI: задача не нужна" : "AI: план готов");
      await loadPublications();
      showPlan(id);
    } catch (e) { toast("AI: " + e.message, true); }
  }
  $("pub-analyze-selected").addEventListener("click", async function () {
    var sel = RobotTables.selected($("pub-table"));
    if (!sel.length) return toast("Ничего не выбрано", true);
    for (var i = 0; i < sel.length; i++) await analyzeOne(sel[i]);
  });
  $("pub-create-selected").addEventListener("click", async function () {
    var sel = RobotTables.selected($("pub-table"));
    if (!sel.length) return toast("Ничего не выбрано", true);
    for (var i = 0; i < sel.length; i++) {
      var pubRow = state.pubs.find(function (x) { return x.id === sel[i]; });
      if (pubRow && pubRow.answered_by_version) {
        toast("Публикация " + sel[i] + ": уже отвечена версией — задача не нужна", true);
        continue;
      }
      try {
        var d = await api("/api/publications/" + sel[i] + "/create-task", { method: "POST", body: "{}" });
        toast("Задача создана: " + (d.task ? d.task.task_name : ""));
      } catch (e) { toast("Задача " + sel[i] + ": " + e.message, true); }
    }
    await loadPublications();
    await loadTasks();
  });

  async function showPlan(id) {
    var p = state.pubs.find(function (x) { return x.id === id; });
    if (!p) return;
    var plan = p.analysis;
    var md = p.analysis_md || "";
    var html = "";
    if (md && window.marked) {
      html = window.marked.parse(md);
      if (window.DOMPurify) html = window.DOMPurify.sanitize(html);
    }
    var body = md
      ? '<div class="markdown-body">' + html + '</div>' +
        '<div class="muted" style="margin-top:10px">Файл: <span class="mono">' + esc(p.analysis_md_path || "") + '</span></div>'
      : '<div class="pre">План ещё не готов. Нажми «Разобрать».</div>';
    if (p.answered_by_version) {
      body += '<div class="muted" style="margin-top:10px">По этому комментарию уже выпущена версия — создание задачи не требуется.</div>';
    }
    openModal("План задачи · " + (p.shot || ""), body,
      plan && !p.answered_by_version ? [{ label: "Создать задачу", primary: true, fn: async function () {
        try {
          var d = await api("/api/publications/" + id + "/create-task", { method: "POST", body: "{}" });
          toast("Задача создана: " + d.task.task_name); closeModal(); await loadPublications(); await loadTasks();
        } catch (e) { toast(e.message, true); }
      } }] : []);
  }

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
      if (href.indexOf("http://") === 0 || href.indexOf("https://") === 0 || href.indexOf("mailto:") === 0) {
        e.preventDefault(); openExternal(href); return;
      }
      if (href.indexOf("/") === 0) {
        e.preventDefault();
        var local = href.indexOf("/api/") === 0 || href.indexOf("/static/") === 0;
        openExternal((local ? window.location.origin : synxBase()) + href);
        return;
      }
      if (href.indexOf("file://") === 0) {
        e.preventDefault(); openPath(decodeURIComponent(href.slice(7))); return;
      }
      if (href.indexOf("/home/") === 0 || href.indexOf("/mnt/") === 0 || href.indexOf("~/") === 0) {
        e.preventDefault(); openPath(href); return;
      }
    }
    var code = e.target.closest("code, .mono");
    if (code) {
      var text = (code.textContent || "").trim();
      if (/^(\/|~\/).+\.(pdf|png|jpe?g|webp|gif|mp4|mov|webm|mkv|json|md|txt)$/i.test(text)) {
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

  // ---------- scan ----------
  $("scan-btn").addEventListener("click", async function () {
    var btn = this; btn.disabled = true; btn.textContent = "Сканирую...";
    try {
      var d = await api("/api/synx/scan", { method: "POST", body: JSON.stringify({}) });
      toast("Synx: " + d.comments + " комментариев, новых " + d.new);
      await loadPublications();
      var meta = await api("/api/meta");
      $("last-scan").textContent = "scan: " + fmtTime(meta.last_scan);
    } catch (e) { toast("Scan: " + e.message, true); }
    finally { btn.disabled = false; btn.textContent = "Сканировать synx"; }
  });

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

  // ---------- init ----------
  async function init() {
    await loadSettings();
    await loadPublications();
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
