/* IIRobot · презентация способностей · 3 минуты
   Логика: сцены, автотаймлайн, оглавление, клавиатура. */
(() => {
  "use strict";

  const app = document.querySelector(".app");
  const scenes = Array.from(document.querySelectorAll(".scene"));
  const tocList = document.getElementById("toc-list");
  const clockEl = document.getElementById("clock");
  const clockTotalEl = document.getElementById("clock-total");
  const overallBar = document.getElementById("overall-bar");
  const chapterNum = document.getElementById("chapter-num");
  const chapterKicker = document.getElementById("chapter-kicker");
  const chapterTitle = document.getElementById("chapter-title");
  const playBtn = document.getElementById("play-btn");
  const tocToggle = document.getElementById("toc-toggle");
  const replayBtn = document.getElementById("replay");
  const hintbarRight = document.getElementById("hintbar-right");

  if (!scenes.length) return;

  const durations = scenes.map((s) => Math.max(2, Number(s.dataset.duration) || 10));
  const totalSeconds = durations.reduce((a, b) => a + b, 0);
  const lastIndex = scenes.length - 1;

  const state = {
    index: 0,
    elapsed: 0,
    paused: false,
    ended: false,
  };

  const pad2 = (n) => String(n).padStart(2, "0");
  const fmt = (sec) => {
    const s = Math.max(0, Math.floor(sec));
    return `${Math.floor(s / 60)}:${pad2(s % 60)}`;
  };
  const cumulative = (i) => durations.slice(0, i).reduce((a, b) => a + b, 0);

  /* ------------------------------ оглавление ------------------------------ */
  const tocItems = scenes.map((scene, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toc-item";
    btn.dataset.index = String(i);
    btn.innerHTML = `
      <span class="toc-num">${pad2(i + 1)}</span>
      <span class="toc-text">
        <span class="toc-title"></span>
        <span class="toc-meta">${durations[i]} с</span>
      </span>
      <span class="toc-progress"><i></i></span>`;
    btn.querySelector(".toc-title").textContent = scene.dataset.title || `Сцена ${i + 1}`;
    btn.setAttribute("aria-label", `Сцена ${i + 1}. ${scene.dataset.title || ""}`);
    btn.addEventListener("click", () => goTo(i));
    tocList.appendChild(btn);
    return btn;
  });

  clockTotalEl.textContent = fmt(totalSeconds);

  /* ------------------------------- сцены ------------------------------- */
  function updateChrome() {
    const scene = scenes[state.index];
    chapterNum.textContent = pad2(state.index + 1);
    chapterKicker.textContent = scene.dataset.kicker || "";
    chapterTitle.textContent = scene.dataset.title || "";
    hintbarRight.textContent = `${pad2(state.index + 1)} / ${pad2(scenes.length)}`;

    tocItems.forEach((item, i) => {
      item.classList.toggle("is-current", i === state.index);
      item.classList.toggle("is-past", i < state.index);
      if (i === state.index) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
  }

  function activate(index) {
    scenes.forEach((scene, i) => scene.classList.toggle("is-active", i === index));
    updateChrome();
  }

  function goTo(index, opts = {}) {
    const next = Math.max(0, Math.min(lastIndex, index));
    const restart = opts.restart !== false;
    const keepPaused = opts.keepPaused !== false;

    state.ended = false;
    state.index = next;
    state.elapsed = Math.max(0, Math.min(opts.elapsed || 0, durations[next] - 0.01));
    if (!keepPaused) setPaused(false);

    if (restart) {
      // перезапускаем анимации сцены: снимаем и возвращаем класс
      scenes[next].classList.remove("is-active");
      void scenes[next].offsetWidth;
    }
    activate(next);
    renderProgress();
    renderClock();
  }

  function next() {
    if (state.ended) return restart();
    if (state.index < lastIndex) goTo(state.index + 1);
    else finish();
  }

  function prev() {
    if (state.elapsed > 2.2 && state.index > 0) {
      goTo(state.index, { restart: true });
      return;
    }
    goTo(Math.max(0, state.index - 1));
  }

  function finish() {
    state.ended = true;
    state.elapsed = durations[lastIndex];
    renderProgress();
    renderClock();
  }

  function restart() {
    setPaused(false);
    goTo(0);
  }

  /* ------------------------------- время ------------------------------- */
  function globalElapsed() {
    return cumulative(state.index) + Math.min(state.elapsed, durations[state.index]);
  }

  function renderClock() {
    clockEl.textContent = fmt(globalElapsed());
  }

  function renderProgress() {
    const dur = durations[state.index];
    const p = Math.min(1, state.elapsed / dur);
    const overall = Math.min(1, globalElapsed() / totalSeconds);
    overallBar.style.width = `${(overall * 100).toFixed(2)}%`;

    tocItems.forEach((item, i) => {
      const bar = item.querySelector(".toc-progress i");
      let value = 0;
      if (i < state.index) value = 1;
      else if (i === state.index) value = state.ended ? 1 : p;
      bar.style.width = `${(value * 100).toFixed(1)}%`;
    });
  }

  let lastFrame = performance.now();
  function tick(now) {
    const dt = Math.min(0.12, (now - lastFrame) / 1000);
    lastFrame = now;

    if (!state.paused && !state.ended) {
      state.elapsed += dt;
      if (state.elapsed >= durations[state.index]) {
        if (state.index < lastIndex) {
          goTo(state.index + 1, { elapsed: 0 });
        } else {
          finish();
        }
      }
      renderClock();
      renderProgress();
    }
    requestAnimationFrame(tick);
  }

  /* ------------------------------- пауза ------------------------------- */
  function setPaused(value) {
    state.paused = value;
    document.body.classList.toggle("is-paused", value);
    playBtn.title = value ? "Продолжить (Space)" : "Пауза (Space)";
    playBtn.setAttribute("aria-label", value ? "Продолжить" : "Пауза");
  }
  const togglePause = () => {
    if (state.ended) return restart();
    setPaused(!state.paused);
  };

  /* ------------------------------- контролы ------------------------------- */
  playBtn.addEventListener("click", togglePause);
  replayBtn.addEventListener("click", restart);

  tocToggle.addEventListener("click", () => {
    const collapsed = app.classList.toggle("toc-collapsed");
    tocToggle.setAttribute("aria-expanded", String(!collapsed));
    tocToggle.setAttribute("aria-label", collapsed ? "Развернуть оглавление" : "Свернуть оглавление");
    tocToggle.title = collapsed ? "Развернуть оглавление (T)" : "Свернуть оглавление (T)";
    try { localStorage.setItem("iirobot.pres.toc", collapsed ? "collapsed" : "open"); } catch (e) {}
  });

  function applyStoredToc() {
    let stored = null;
    try { stored = localStorage.getItem("iirobot.pres.toc"); } catch (e) {}
    const collapsed = stored ? stored === "collapsed" : window.innerWidth < 1040;
    app.classList.toggle("toc-collapsed", collapsed);
    tocToggle.setAttribute("aria-expanded", String(!collapsed));
  }

  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || e.target.isContentEditable;

    if (e.key === " " || e.code === "Space") {
      if (typing) return;
      if (tag === "BUTTON" && e.target !== playBtn && e.target !== replayBtn) return;
      e.preventDefault();
      togglePause();
      return;
    }
    if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === "PageDown") {
      if (typing) return;
      e.preventDefault();
      next();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp" || e.key === "PageUp") {
      if (typing) return;
      e.preventDefault();
      prev();
    } else if (e.key === "Home") {
      e.preventDefault();
      goTo(0);
    } else if (e.key === "End") {
      e.preventDefault();
      goTo(lastIndex, { elapsed: durations[lastIndex] - 0.01 });
    } else if (e.key === "t" || e.key === "T" || e.key === "е" || e.key === "Е") {
      if (typing) return;
      tocToggle.click();
    } else if (e.key === "f" || e.key === "F" || e.key === "а" || e.key === "А") {
      if (typing) return;
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen && document.documentElement.requestFullscreen();
      } else {
        document.exitFullscreen && document.exitFullscreen();
      }
    }
  });

  // Пауза, если вкладку скрыли
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) setPaused(true);
  });

  /* ----------------------------- старт / deep link ----------------------------- */
  applyStoredToc();
  setPaused(false);

  const params = new URLSearchParams(location.search);
  let startIndex = 0;
  let startElapsed = 0;

  if (params.has("t")) {
    const t = Math.max(0, Number(params.get("t")) || 0);
    let acc = 0;
    for (let i = 0; i < durations.length; i++) {
      if (t < acc + durations[i]) { startIndex = i; startElapsed = t - acc; break; }
      acc += durations[i];
      startIndex = i;
      startElapsed = durations[i] - 0.01;
    }
  } else if (params.has("scene")) {
    startIndex = Math.max(0, Math.min(lastIndex, (Number(params.get("scene")) || 1) - 1));
  } else if (location.hash) {
    const m = location.hash.match(/s?(\d+)/);
    if (m) startIndex = Math.max(0, Math.min(lastIndex, Number(m[1]) - 1));
  }

  goTo(startIndex, { elapsed: startElapsed, keepPaused: true });
  requestAnimationFrame((now) => { lastFrame = now; tick(now); });
})();
