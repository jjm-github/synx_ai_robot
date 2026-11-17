/* Synx AI Robot table behavior: widths, visibility, sticky header, sort/filter.
   The interaction model mirrors synx_engine: resize by dragging the right edge,
   right-click a header to toggle columns, settings stored per table in localStorage. */
(function () {
  "use strict";
  var PREFIX = "synx.robot.table.";
  var MENU_ID = "table-col-menu";

  function thName(th) {
    if (!th) return "col";
    var n = th.getAttribute("data-col");
    if (!n) {
      n = String(th.textContent || "").replace(/\s+/g, " ").trim() || "col";
      th.setAttribute("data-col", n);
    }
    return n;
  }

  function cols(table) {
    return Array.prototype.slice.call(table.tHead.rows[0].cells);
  }

  function load(table) {
    try {
      return JSON.parse(localStorage.getItem(PREFIX + table.id) || "null") || {};
    } catch (e) { return {}; }
  }

  function save(table, cfg) {
    try { localStorage.setItem(PREFIX + table.id, JSON.stringify(cfg)); } catch (e) {}
  }

  function naturalWidths(table) {
    var out = {};
    var ths = cols(table);
    var old = table.style.tableLayout;
    table.style.tableLayout = "auto";
    ths.forEach(function (th) {
      out[thName(th)] = Math.max(42, Math.min(520, Math.round(th.getBoundingClientRect().width || 120)));
    });
    table.style.tableLayout = old || "fixed";
    return out;
  }

  function ensureColgroup(table) {
    var cg = table.querySelector("colgroup");
    if (!cg) {
      cg = document.createElement("colgroup");
      table.insertBefore(cg, table.tHead);
    }
    var ths = cols(table);
    while (cg.children.length < ths.length) cg.appendChild(document.createElement("col"));
    while (cg.children.length > ths.length) cg.removeChild(cg.lastChild);
    return cg;
  }

  function apply(table) {
    var cfg = table._cfg || load(table);
    table._cfg = cfg;
    var ths = cols(table);
    var names = ths.map(thName);
    if (!table._natural) table._natural = naturalWidths(table);
    cfg.widths = cfg.widths || {};
    cfg.hidden = cfg.hidden || [];
    var cg = ensureColgroup(table);
    ths.forEach(function (th, i) {
      var n = names[i];
      var w = Math.round(cfg.widths[n] || table._natural[n] || 120);
      cg.children[i].style.width = w + "px";
      var hidden = cfg.hidden.indexOf(n) !== -1;
      th.classList.toggle("col-hidden", hidden);
      var cells = table.tBodies[0] ? table.tBodies[0].rows : [];
      for (var r = 0; r < cells.length; r++) {
        if (cells[r].cells[i]) cells[r].cells[i].classList.toggle("col-hidden", hidden);
      }
    });
    table.style.tableLayout = "fixed";
  }

  function addHandle(th, table) {
    if (th.querySelector(".col-resize")) return;
    var h = document.createElement("div");
    h.className = "col-resize";
    h.title = "Потянуть — ширина колонки";
    th.appendChild(h);
    h.addEventListener("mousedown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var startX = e.clientX;
      var idx = cols(table).indexOf(th);
      var cg = ensureColgroup(table);
      var startW = Math.round(th.getBoundingClientRect().width || 120);
      document.body.classList.add("col-resizing");
      function move(ev) {
        var w = Math.max(30, Math.round(startW + ev.clientX - startX));
        cg.children[idx].style.width = w + "px";
        th.style.width = w + "px";
        table._cfg.widths[thName(th)] = w;
      }
      function up() {
        document.body.classList.remove("col-resizing");
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
        save(table, table._cfg);
      }
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    });
  }

  function closeMenu() {
    var m = document.getElementById(MENU_ID);
    if (m) m.classList.remove("open");
  }

  function openHeaderMenu(table, th, x, y) {
    var old = document.getElementById(MENU_ID);
    if (old) old.remove();
    var m = document.createElement("div");
    m.id = MENU_ID;
    m.className = "ctx-menu open";
    m.style.left = Math.min(x, window.innerWidth - 300) + "px";
    m.style.top = Math.min(y, window.innerHeight - 420) + "px";
    var title = document.createElement("div");
    title.className = "ctx-label";
    title.textContent = "Колонки";
    m.appendChild(title);
    var cfg = table._cfg;
    cols(table).forEach(function (c) {
      var n = thName(c);
      var row = document.createElement("label");
      row.className = "ctx-item";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "ctx-check";
      cb.checked = cfg.hidden.indexOf(n) === -1;
      cb.addEventListener("change", function () {
        var idx = cfg.hidden.indexOf(n);
        if (cb.checked && idx !== -1) cfg.hidden.splice(idx, 1);
        if (!cb.checked && idx === -1) cfg.hidden.push(n);
        apply(table); save(table, cfg);
      });
      row.appendChild(cb);
      row.appendChild(document.createTextNode(String(c.textContent || n).trim()));
      m.appendChild(row);
    });
    var sep = document.createElement("div"); sep.className = "ctx-sep"; m.appendChild(sep);
    var reset = document.createElement("div");
    reset.className = "ctx-item";
    reset.textContent = "Сбросить ширины";
    reset.addEventListener("click", function () {
      table._cfg.widths = {};
      table._natural = naturalWidths(table);
      apply(table); save(table, table._cfg); closeMenu();
    });
    m.appendChild(reset);
    var show = document.createElement("div");
    show.className = "ctx-item";
    show.textContent = "Показать все колонки";
    show.addEventListener("click", function () {
      table._cfg.hidden = [];
      apply(table); save(table, table._cfg); closeMenu();
    });
    m.appendChild(show);
    document.body.appendChild(m);
    setTimeout(function () {
      document.addEventListener("mousedown", outside, { once: true });
    }, 0);
    function outside(ev) {
      if (!m.contains(ev.target)) closeMenu();
    }
  }

  function sortBy(table, th) {
    var idx = cols(table).indexOf(th);
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var dir = th.dataset.sortDir === "asc" ? "desc" : "asc";
    cols(table).forEach(function (x) {
      x.classList.remove("sorted-asc", "sorted-desc");
      delete x.dataset.sortDir;
    });
    th.classList.add(dir === "asc" ? "sorted-asc" : "sorted-desc");
    th.dataset.sortDir = dir;
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var av = (a.cells[idx] ? (a.cells[idx].dataset.sort || a.cells[idx].textContent) : "").trim();
      var bv = (b.cells[idx] ? (b.cells[idx].dataset.sort || b.cells[idx].textContent) : "").trim();
      var an = parseFloat(av.replace(/[^\d.,-]/g, "").replace(",", "."));
      var bn = parseFloat(bv.replace(/[^\d.,-]/g, "").replace(",", "."));
      var cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv, "ru");
      return dir === "asc" ? cmp : -cmp;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }

  function init(table) {
    if (!table || table._robotInit) return;
    table._robotInit = true;
    table._cfg = load(table);
    table._cfg.hidden = table._cfg.hidden || [];
    table._cfg.widths = table._cfg.widths || {};
    table._natural = naturalWidths(table);
    initSelection(table);
    cols(table).forEach(function (th) {
      addHandle(th, table);
      th.addEventListener("contextmenu", function (e) {
        e.preventDefault();
        openHeaderMenu(table, th, e.clientX, e.clientY);
      });
      th.addEventListener("click", function (e) {
        if (e.target.classList && e.target.classList.contains("col-resize")) return;
        if (e.target.tagName === "INPUT") return;
        sortBy(table, th);
      });
    });
    apply(table);
  }

  function refresh(table) {
    init(table);
    apply(table);
  }

  function filter(table, text) {
    var q = (text || "").trim().toLowerCase();
    var rows = table.tBodies[0] ? table.tBodies[0].rows : [];
    for (var i = 0; i < rows.length; i++) {
      var hay = (rows[i].dataset.hay || rows[i].textContent || "").toLowerCase();
      rows[i].style.display = (!q || hay.indexOf(q) !== -1) ? "" : "none";
    }
  }

  function rows(table) {
    return table.tBodies[0] ? Array.prototype.slice.call(table.tBodies[0].rows) : [];
  }

  function selected(table) {
    return rows(table).filter(function (r) { return r.classList.contains("selected"); })
      .map(function (r) { return r.dataset.id; });
  }

  function emit(table) {
    table.dispatchEvent(new CustomEvent("robot-selection", { detail: { table: table } }));
  }

  function setRow(r, on) {
    if (r) r.classList.toggle("selected", !!on);
  }

  function selectIds(table, ids) {
    var set = {};
    (ids || []).forEach(function (id) { set[String(id)] = 1; });
    rows(table).forEach(function (r) { r.classList.toggle("selected", !!set[String(r.dataset.id)]); });
  }

  function selectOnly(table, id) {
    rows(table).forEach(function (r) { setRow(r, String(r.dataset.id) === String(id)); });
    table._anchor = rows(table).find(function (r) { return String(r.dataset.id) === String(id); }) || null;
    emit(table);
  }

  function toggleRow(table, row) {
    setRow(row, !row.classList.contains("selected"));
    table._anchor = row;
    emit(table);
  }

  function selectRange(table, row, add) {
    var rs = rows(table);
    var a = rs.indexOf(table._anchor);
    var b = rs.indexOf(row);
    if (a < 0) { selectOnly(table, row.dataset.id); return; }
    if (a > b) { var t = a; a = b; b = t; }
    var allSelected = true;
    for (var i = a; i <= b; i++) if (!rs[i].classList.contains("selected")) { allSelected = false; break; }
    var want = !allSelected;
    if (!add) rs.forEach(function (r) { setRow(r, false); });
    for (var j = a; j <= b; j++) setRow(rs[j], want);
    table._anchor = row;
    emit(table);
  }

  function initSelection(table) {
    if (table._selInit) return;
    table._selInit = true;
    table.addEventListener("click", function (e) {
      if (e.target.closest("button,a,input,select,textarea,label")) return;
      var row = e.target.closest("tr[data-id]");
      if (!row || !table.contains(row)) return;
      if (e.shiftKey) {
        selectRange(table, row, e.ctrlKey || e.metaKey);
      } else if (e.ctrlKey || e.metaKey) {
        toggleRow(table, row);
      } else {
        selectOnly(table, row.dataset.id);
      }
    });
  }

  window.RobotTables = { init: init, refresh: refresh, filter: filter, selected: selected,
    selectIds: selectIds, selectOnly: selectOnly, closeMenu: closeMenu };
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table.data").forEach(init);
  });
})();
