(() => {
  "use strict";

  const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const hasFinePointer = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  if (prefersReduced) document.body.classList.add("reduced-motion");

  const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const smoothstep = (t) => {
    const x = clamp(t, 0, 1);
    return x * x * (3 - 2 * x);
  };

  /* ----------------------------------------------------------
     Starfield — living background
     ---------------------------------------------------------- */
  const canvas = document.getElementById("starfield");
  const ctx = canvas.getContext("2d");
  let stars = [];
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  function resizeCanvas() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
    canvas.style.width = window.innerWidth + "px";
    canvas.style.height = window.innerHeight + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seedStars();
  }

  function seedStars() {
    const count = Math.round((window.innerWidth * window.innerHeight) / 9000);
    stars = new Array(count).fill(0).map(() => ({
      x: Math.random() * window.innerWidth,
      y: Math.random() * window.innerHeight,
      r: Math.random() * 1.3 + 0.3,
      baseAlpha: Math.random() * 0.5 + 0.25,
      phase: Math.random() * Math.PI * 2,
      speed: Math.random() * 0.15 + 0.03,
      drift: Math.random() * 0.02 + 0.005,
    }));
  }

  function drawStars(t) {
    ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    for (const s of stars) {
      const twinkle = prefersReduced ? s.baseAlpha : s.baseAlpha + Math.sin(t * 0.001 * s.speed * 6 + s.phase) * 0.25;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(210, 240, 255, ${clamp(twinkle, 0, 1)})`;
      ctx.fill();
      if (!prefersReduced) {
        s.y -= s.drift;
        if (s.y < -4) s.y = window.innerHeight + 4;
      }
    }
  }

  window.addEventListener("resize", resizeCanvas, { passive: true });
  resizeCanvas();

  /* ----------------------------------------------------------
     Mouse tilt for 3D dashboard panels
     ---------------------------------------------------------- */
  const tiltRigs = Array.from(document.querySelectorAll("[data-tilt]"));
  let targetRX = 0, targetRY = 0, curRX = 0, curRY = 0;

  if (hasFinePointer && !prefersReduced) {
    window.addEventListener("pointermove", (e) => {
      const nx = (e.clientX / window.innerWidth) * 2 - 1;
      const ny = (e.clientY / window.innerHeight) * 2 - 1;
      targetRY = nx * 10;
      targetRX = -ny * 8;
    }, { passive: true });
  }

  function updateTilt(t) {
    if (prefersReduced) return;
    if (hasFinePointer) {
      curRX = lerp(curRX, targetRX, 0.06);
      curRY = lerp(curRY, targetRY, 0.06);
    } else {
      curRX = Math.cos(t / 4200) * 3;
      curRY = Math.sin(t / 3400) * 5;
    }
    const transform = `rotateX(${curRX.toFixed(2)}deg) rotateY(${curRY.toFixed(2)}deg)`;
    for (const rig of tiltRigs) rig.style.transform = transform;
  }

  /* ----------------------------------------------------------
     Funnel particles (generated once)
     ---------------------------------------------------------- */
  const funnelGroup = document.querySelector(".funnel-particles");
  if (funnelGroup) {
    const xs = [40, 70, 100, 130, 160, 190, 220, 60, 90, 150, 180, 120, 105, 135];
    xs.forEach((x, i) => {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", x);
      c.setAttribute("cy", "20");
      c.setAttribute("r", (1.6 + Math.random() * 1.6).toFixed(1));
      c.setAttribute("fill", "#00e5ff");
      c.style.animationDelay = `${(i * 0.19).toFixed(2)}s`;
      c.style.transformOrigin = `${x}px 20px`;
      funnelGroup.appendChild(c);
    });
  }

  /* ----------------------------------------------------------
     Orbit ring — JS-driven so labels never rotate (stay upright)
     ---------------------------------------------------------- */
  const orbitSpans = Array.from(document.querySelectorAll("#orbitRing span"));
  const ringStage = document.querySelector(".ring-stage");
  let ringHovered = false;
  if (ringStage) {
    ringStage.addEventListener("mouseenter", () => { ringHovered = true; });
    ringStage.addEventListener("mouseleave", () => { ringHovered = false; });
  }
  let ringAngle = 0;
  let lastT = 0;
  function updateOrbitRing(t) {
    if (!orbitSpans.length) return;
    const dt = lastT ? t - lastT : 0;
    lastT = t;
    if (!prefersReduced && !ringHovered) {
      ringAngle += (dt / 26000) * 360;
    }
    const baseRadius = window.innerWidth <= 640 ? 112 : 150;
    const stagger = window.innerWidth <= 640 ? 22 : 34;
    const step = 360 / orbitSpans.length;
    orbitSpans.forEach((span, i) => {
      const deg = ringAngle + i * step;
      const rad = (deg * Math.PI) / 180;
      const radius = baseRadius + (i % 2 === 0 ? stagger : -stagger);
      const x = Math.cos(rad) * radius;
      const y = Math.sin(rad) * radius;
      span.style.transform = `translate(calc(-50% + ${x.toFixed(1)}px), calc(-50% + ${y.toFixed(1)}px))`;
    });
  }

  /* ----------------------------------------------------------
     Counters
     ---------------------------------------------------------- */
  function formatValue(raw, el) {
    const prefix = el.dataset.prefix || "";
    const suffix = el.dataset.suffix || "";
    const decimals = el.dataset.decimals ? parseInt(el.dataset.decimals, 10) : 0;
    let n = raw;
    let str;
    if (el.dataset.format === "short" && n >= 1000) {
      str = (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    } else {
      str = decimals > 0 ? n.toFixed(decimals) : Math.round(n).toLocaleString("en-US");
    }
    return prefix + str + suffix;
  }

  function animateCounter(el) {
    const to = parseFloat(el.dataset.countTo);
    if (Number.isNaN(to)) return;
    if (prefersReduced) {
      el.textContent = formatValue(to, el);
      return;
    }
    const duration = 1500;
    const start = performance.now();
    function step(now) {
      const t = clamp((now - start) / duration, 0, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = formatValue(to * eased, el);
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  /* ----------------------------------------------------------
     Scene engine — single pinned stage, continuously-crossfading
     absolutely-stacked layers. Each scene "owns" a window of the
     global scroll-driven `sceneFloat` value: fade in over [-FADE,0],
     hold over [0,HOLD], fade out over [HOLD,HOLD+FADE]. Because
     scenes are spaced HOLD apart (not HOLD+FADE), neighboring
     scenes' fade windows genuinely overlap — a real crossfade,
     not a sequential blank gap.
     ---------------------------------------------------------- */
  const HOLD = 0.85;
  const FADE = 0.2;

  const stage = document.getElementById("stage");
  const main = document.querySelector("main");
  const sceneEls = Array.from(document.querySelectorAll(".scene-content[data-scene]"));
  const sceneData = sceneEls.map((el, i) => ({
    el,
    id: el.id,
    index: i,
    isLast: i === sceneEls.length - 1,
    triggered: false,
  }));
  sceneEls.forEach((el, i) => { el.style.zIndex = String(i + 1); });

  const maxSceneFloat = (sceneEls.length - 1) * HOLD;

  const fanCards = Array.from(document.querySelectorAll(".fan-card"));
  const priceFlip = document.getElementById("priceFlip");

  const floatingCta = document.getElementById("floatingCta");
  const progressFill = document.getElementById("progressFill");
  const dotButtons = Array.from(document.querySelectorAll(".dot-nav button"));

  function sceneFloatForIndex(i) {
    return main.offsetTop + (i * HOLD / maxSceneFloat) * (main.offsetHeight - window.innerHeight);
  }

  dotButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = sceneData.findIndex((s) => s.id === btn.dataset.target);
      if (idx === -1) return;
      window.scrollTo({ top: sceneFloatForIndex(idx), behavior: prefersReduced ? "auto" : "smooth" });
    });
  });

  function updateScenes() {
    if (!main) return;
    const mainRect = main.getBoundingClientRect();
    const scrollable = main.offsetHeight - window.innerHeight;
    const pTotal = scrollable > 0 ? clamp(-mainRect.top / scrollable, 0, 1) : 0;
    const sceneFloat = pTotal * maxSceneFloat;

    let activeId = sceneData[0].id;
    let activeBest = -Infinity;

    for (const s of sceneData) {
      const x = sceneFloat - s.index * HOLD;

      const eIn = smoothstep(clamp((x + FADE) / FADE, 0, 1));
      const eOut = s.isLast ? 0 : smoothstep(clamp((x - (HOLD - FADE)) / FADE, 0, 1));
      const opacity = clamp(eIn * (1 - eOut), 0, 1);

      if (!prefersReduced) {
        const scale = 0.86 + 0.14 * eIn + 0.16 * eOut;
        const blur = 10 * (1 - eIn) + 8 * eOut;
        const ty = 40 * (1 - eIn) - 40 * eOut;
        s.el.style.opacity = opacity.toFixed(3);
        s.el.style.transform = `translateY(${ty.toFixed(1)}px) scale(${scale.toFixed(3)})`;
        s.el.style.filter = blur > 0.05 ? `blur(${blur.toFixed(1)}px)` : "none";
        s.el.classList.toggle("pointer-active", opacity > 0.6);
      }

      if (!s.triggered && x > -FADE * 0.4) {
        s.triggered = true;
        s.el.classList.add("in-view");
        s.el.querySelectorAll("[data-count-to]").forEach(animateCounter);
      }

      // Scroll-scrubbed interactions, active only while roughly on-screen
      const holdProgress = clamp(x / HOLD, 0, 1);
      if (s.id === "scene-bonus" && fanCards.length) {
        const active = clamp(Math.floor(holdProgress * fanCards.length), 0, fanCards.length - 1);
        fanCards.forEach((card, i) => {
          const dist = i - active;
          const ry = dist * 16;
          const tz = dist === 0 ? 60 : -Math.abs(dist) * 60;
          const tx = dist * 26;
          const scaleC = dist === 0 ? 1 : 0.88;
          const cardOpacity = dist === 0 ? 1 : Math.abs(dist) === 1 ? 0.16 : 0;
          card.style.transform = `translateX(${tx}px) translateZ(${tz}px) rotateY(${ry}deg) scale(${scaleC})`;
          card.style.opacity = cardOpacity;
          card.style.zIndex = String(10 - Math.abs(dist));
        });
      }

      if (s.id === "scene-offer" && priceFlip) {
        const flipProgress = clamp((holdProgress - 0.2) / 0.6, 0, 1);
        priceFlip.style.transform = `rotateY(${(flipProgress * 180).toFixed(1)}deg)`;
      }

      if (opacity > activeBest) {
        activeBest = opacity;
        activeId = s.id;
      }
    }

    // Floating CTA visibility
    if (floatingCta) floatingCta.classList.toggle("visible", pTotal > 0.03);

    // Progress rail
    if (progressFill) progressFill.style.width = (pTotal * 100).toFixed(2) + "%";

    // Dot nav active state
    if (dotButtons.length) {
      dotButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.target === activeId));
    }
  }

  /* ----------------------------------------------------------
     Main loop
     ---------------------------------------------------------- */
  function tick(t) {
    drawStars(t);
    updateTilt(t);
    updateScenes();
    updateOrbitRing(t);
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  const yearEl = document.getElementById("year");
  if (yearEl) yearEl.textContent = new Date().getFullYear();
})();
